from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.models import TiebaNote
from store.tieba import _store_impl as storage


@pytest.mark.asyncio
async def test_first_collection_time_is_written_and_not_replaced_on_refresh(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    try:
        async with engine.begin() as connection:
            await connection.run_sync(TiebaNote.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def session():
            async with factory() as value:
                yield value

        monkeypatch.setattr(storage, 'get_session', session)
        monkeypatch.setattr(storage.utils, 'get_current_timestamp', lambda: 1792055016000)
        store = storage.TieBaDbStoreImplement()
        payload = {'note_id': '123', 'title': '原始内容', 'add_ts': None}
        await store.store_content(payload)
        assert payload['add_ts'] is None  # Do not mutate caller-owned raw data.
        monkeypatch.setattr(storage.utils, 'get_current_timestamp', lambda: 1792056016000)
        await store.store_content({'note_id': '123', 'title': '更新内容', 'add_ts': 1792056016000})
        async with factory() as db:
            rows = (await db.execute(select(TiebaNote))).scalars().all()
            assert len(rows) == 1
            assert rows[0].add_ts == 1792055016000
            assert rows[0].title == '更新内容'
            # Direct ORM insertion also gets a millisecond timestamp.
            direct = TiebaNote(note_id='456', title='直接写入')
            db.add(direct)
            await db.commit()
            assert direct.add_ts > 1_000_000_000_000
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_migration_backfills_today_defaults_and_is_repeatable():
    import os
    from pathlib import Path
    from uuid import uuid4
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import asyncpg
    import config
    if os.getenv('TIEBA_TEST_DB') != '1':
        pytest.skip('set TIEBA_TEST_DB=1 for isolated PostgreSQL migration test')
    cfg = config.postgres_db_config
    connection = await asyncpg.connect(host=cfg['host'],port=int(cfg['port']),user=cfg['user'],
        password=cfg['password'],database=cfg['db_name'],command_timeout=15)
    schema = 'tieba_ts_test_' + uuid4().hex
    migration = Path('database/migrations/20261015_tieba_note_add_ts.sql').read_text()
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await connection.execute('CREATE TABLE tieba_note(id serial PRIMARY KEY,title text)')
        await connection.execute("INSERT INTO tieba_note(title) VALUES('保留正文')")
        await connection.execute(migration)
        first = await connection.fetchrow('SELECT * FROM tieba_note')
        assert datetime.fromtimestamp(first['add_ts']/1000,ZoneInfo('Asia/Shanghai')).date() == datetime.now(ZoneInfo('Asia/Shanghai')).date()
        assert first['title'] == '保留正文'
        assert await connection.fetchval("INSERT INTO tieba_note(title) VALUES('默认时间') RETURNING add_ts") > 1_000_000_000_000
        with pytest.raises(asyncpg.NotNullViolationError):
            await connection.execute("INSERT INTO tieba_note(title,add_ts) VALUES('缺失',NULL)")
        await connection.execute(migration)
        assert await connection.fetchval('SELECT add_ts FROM tieba_note WHERE id=1') == first['add_ts']
        assert await connection.fetchval('SELECT count(*) FROM tieba_note') == 2
    finally:
        await connection.execute('ROLLBACK')
        await connection.execute('SET search_path TO public')
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
