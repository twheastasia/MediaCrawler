"""Independent comment tables preserve public IDs, replay and page atomicity."""
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from collection.native_comments import TABLES, backfill, migration_ddl, public_user_id


@pytest.mark.parametrize('raw,fallback,expected',[
    ({'user':{'uid':'100','sec_uid':'public-route'}},None,'100'),
    ({'user':{'sec_uid':'public-route'}},None,'public-route'),
    ({'author':{'member':{'id':'zh-user'}}},None,'zh-user'),
    ({'member':{'mid':123}},None,'123'),
    ({'userInfo':{'user_id':'x-user'}},None,'x-user'),
    ({'authorId':456},None,'456'),
    ({},'saved-public-id','saved-public-id'),
    ({},None,None),
])
def test_public_user_id_is_never_an_anonymous_hash(raw,fallback,expected):
    assert public_user_id(raw,fallback)==expected


def test_migration_file_matches_runtime():
    from pathlib import Path
    path=Path(__file__).parents[1]/'database/migrations/20261016_platform_comment_user_id.sql'
    assert path.read_text().strip()==migration_ddl().strip()


@pytest.mark.asyncio
async def test_postgres_platform_tables_page_atomicity_and_scoped_backfill():
    if os.environ.get('COMMENT_TEST_DB')!='1':
        pytest.skip('explicit PostgreSQL integration opt-in')
    import asyncpg,config
    from collection.database import DDL
    from collection.comment_store import save_page
    from collection.sync_comments import audit
    settings=config.postgres_db_config
    conn=await asyncpg.connect(host=settings['host'],port=int(settings['port']),
        user=settings['user'],password=settings['password'],database=settings['db_name'])
    schema='test_native_comments_'+uuid.uuid4().hex
    class Pool:
        @asynccontextmanager
        async def acquire(self):yield conn
    try:
        await conn.execute('CREATE SCHEMA '+schema)
        await conn.execute('SET search_path TO '+schema)
        # Exercise ALTER on an old native table as well as fresh table creation.
        await conn.execute('''CREATE TABLE douyin_aweme_comment (id serial PRIMARY KEY,
            aweme_id varchar(255),comment_id varchar(255),content text,parent_comment_id varchar(255),
            add_ts bigint,last_modify_ts bigint)''')
        await conn.execute(DDL)
        await conn.execute(DDL)  # Schema and unique indexes are replayable.
        await conn.execute("INSERT INTO acquisition_run(run_id,config) VALUES('selected','{}'),('other','{}')")
        for platform,(model,content_column) in TABLES.items():
            table=model.__tablename__
            await conn.execute("INSERT INTO acquisition_content(platform,content_type,content_id,raw) VALUES($1,'video','v','{}')",platform)
            await conn.execute("INSERT INTO acquisition_link VALUES('selected',$1,'keyword','search','video','v')",platform)
            store=SimpleNamespace(pool=Pool(),platform=platform,run_id='selected')
            row={'id':'root','author_id':'original-public-id','body':'评论正文','raw':{},'parent_id':None}
            child={**row,'id':'child','parent_id':'root','body':'回复正文',
                   'raw':{'user':{'uid':'numeric-id','sec_uid':'route-id'}}}
            await save_page(store,{'id':'v','type':'video'},[row,child],'comments:video:v',{'done':True})
            saved=await conn.fetch(f'SELECT * FROM {table} ORDER BY comment_id')
            assert len(saved)==2 and saved[0]['user_id']=='numeric-id'
            assert saved[0]['parent_comment_id']=='root' and saved[0]['content']=='回复正文'
            original_ts=saved[0]['add_ts']
            # A second content may legitimately reuse a comment ID.
            await conn.execute("INSERT INTO acquisition_content(platform,content_type,content_id,raw) VALUES($1,'video','v2','{}')",platform)
            await save_page(store,{'id':'v2','type':'video'},[row],'comments:video:v2',{'done':True})
            assert await conn.fetchval(f'SELECT count(*) FROM {table}')==3
            # Fail after both comment writes, at the progress FK; everything rolls back.
            invalid=SimpleNamespace(pool=Pool(),platform=platform,run_id='missing')
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await save_page(invalid,{'id':'v','type':'video'},[{**row,'id':'rollback'}],
                                'comments:video:v',{'done':True})
            assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE comment_id='rollback'")==0
            assert await conn.fetchval("SELECT count(*) FROM acquisition_comment WHERE platform=$1 AND comment_id='rollback'",platform)==0
            # Remove one native row to emulate historical unified-only data.
            await conn.execute(f"DELETE FROM {table} WHERE comment_id='root' AND {content_column}='v'")
            copied=await backfill(conn,['selected'])
            assert copied[platform]==2
            # Replaying either backfill or pages neither duplicates nor loses first timestamp.
            await backfill(conn,['selected'])
            await save_page(store,{'id':'v','type':'video'},[child],'comments:video:v',{'done':True})
            assert await conn.fetchval(f'SELECT count(*) FROM {table}')==3
            assert await conn.fetchval(f"SELECT add_ts FROM {table} WHERE comment_id='child'")==original_ts
            metrics=(await audit(conn,['selected']))[platform]
            assert metrics['source_comments']==2 and metrics['native_comments']==2
            assert metrics['mismatched']==0 and metrics['missing_user_id']==0
        # Unlinked source records are never included in a requested-run backfill.
        await conn.execute("DELETE FROM douyin_aweme_comment WHERE aweme_id='v2'")
        await backfill(conn,['selected'])
        assert await conn.fetchval("SELECT count(*) FROM douyin_aweme_comment WHERE aweme_id='v2'")==0
        with pytest.raises(ValueError):await backfill(conn,[])
    finally:
        await conn.execute('SET search_path TO public')
        await conn.execute('DROP SCHEMA '+schema+' CASCADE')
        await conn.close()
