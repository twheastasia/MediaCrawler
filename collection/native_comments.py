"""Platform comment tables used by readers; writes share the page transaction."""
from functools import lru_cache
import json
import time

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from database import models


# All SQL identifiers come from this fixed mapping, never from collected data.
TABLES = {
    'bili': (models.BilibiliVideoComment, 'video_id'),
    'dy': (models.DouyinAwemeComment, 'aweme_id'),
    'ks': (models.KuaishouVideoComment, 'video_id'),
    'wb': (models.WeiboNoteComment, 'note_id'),
    'xhs': (models.XhsNoteComment, 'note_id'),
    'tieba': (models.TiebaComment, 'note_id'),
    'zhihu': (models.ZhihuComment, 'content_id'),
    'maimai': (models.MaimaiComment, 'content_id'),
    'linkedin': (models.LinkedInComment, 'content_id'),
}


def public_user_id(raw, fallback=None):
    user = raw.get('user') or raw.get('member') or raw.get('author') or raw.get('userInfo') or {}
    if not isinstance(user, dict):
        user = {}
    user = user.get('member') or user
    if not isinstance(user, dict):
        user = {}
    # Douyin uid is the public user ID; sec_uid remains a usable fallback.
    values = [user.get(k) for k in ('uid', 'user_id', 'id', 'mid', 'sec_uid')]
    values += [raw.get('author_id'), raw.get('authorId'), fallback]
    return next((str(v) for v in values if v is not None and str(v) != ''), None)


def migration_ddl(platforms=('bili','dy','ks','wb','xhs','tieba','zhihu')):
    statements = []
    for platform, (model, content_column) in TABLES.items():
        if platform not in platforms:continue
        table = model.__tablename__
        statements.append(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS user_id text;')
        statements.append(f"COMMENT ON COLUMN {table}.user_id IS '评论用户的公开平台ID；缺失时为NULL，不以匿名哈希代替';")
        if platform == 'ks':
            statements.append(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS parent_comment_id varchar(255);')
        keys = f'{content_column}, comment_id'
        if platform == 'zhihu':
            keys = 'content_type, ' + keys
        statements.append(f'CREATE UNIQUE INDEX IF NOT EXISTS {table}_content_comment_uidx ON {table} ({keys});')
    return '\n'.join(statements)


@lru_cache(maxsize=1)
def schema_ddl():
    # A fresh database gets the full existing ORM tables, not partial lookalikes.
    tables = [str(CreateTable(model.__table__, if_not_exists=True).compile(dialect=postgresql.dialect()))+';'
              for model, _ in TABLES.values()]
    return '\n'.join(tables) + '\n' + migration_ddl(TABLES)


@lru_cache(maxsize=None)
def upsert_sql(platform):
    model, content_column = TABLES[platform]
    table = model.__tablename__
    columns = [content_column, 'comment_id', 'user_id', 'content', 'parent_comment_id', 'add_ts', 'last_modify_ts']
    if platform == 'zhihu':
        columns.append('content_type')
    keys = [content_column, 'comment_id']
    if platform == 'zhihu':
        keys.insert(0, 'content_type')
    updates = [f'{c}=COALESCE(excluded.{c},{table}.{c})' for c in ('user_id','content','parent_comment_id')]
    updates += [f'add_ts=COALESCE({table}.add_ts,excluded.add_ts)',
                f'last_modify_ts=GREATEST({table}.last_modify_ts,excluded.last_modify_ts)']
    return (f'INSERT INTO {table} ({",".join(columns)}) VALUES '
            f'({",".join("$"+str(n) for n in range(1,len(columns)+1))}) '
            f'ON CONFLICT ({",".join(keys)}) DO UPDATE SET {",".join(updates)}')


def native_values(platform, item, row, collected_ms, updated_ms):
    args = [item['id'], row['id'], public_user_id(row.get('raw') or {}, row.get('author_id')),
            row.get('body'), row.get('parent_id'), collected_ms, updated_ms]
    if platform == 'zhihu':
        args.append(item['type'])
    return args


async def save_native(conn, platform, item, rows, collected_ms=None, updated_ms=None):
    if platform not in TABLES:
        raise ValueError('unsupported_native_comment_platform')
    collected_ms = collected_ms if collected_ms is not None else int(time.time()*1000)
    updated_ms = updated_ms if updated_ms is not None else collected_ms
    values = [native_values(platform,item,row,collected_ms,updated_ms) for row in rows]
    if values:
        await conn.executemany(upsert_sql(platform), values)


async def backfill(conn, run_ids):
    """Only comments linked to the requested runs; replay is idempotent."""
    if not run_ids:
        raise ValueError('comment_backfill_requires_run_ids')
    counts = {}
    for platform in TABLES:
        after = ('','','')
        total = 0
        while True:
            rows = await conn.fetch('''SELECT c.* FROM acquisition_comment c
                WHERE c.platform=$1 AND (c.content_type,c.content_id,c.comment_id)>($3,$4,$5)
                AND EXISTS(SELECT 1 FROM acquisition_link l WHERE l.run_id=ANY($2::text[])
                    AND l.platform=c.platform AND l.content_type=c.content_type AND l.content_id=c.content_id)
                ORDER BY c.content_type,c.content_id,c.comment_id LIMIT 500''',platform,run_ids,*after)
            if not rows:
                break
            values = []
            for row in rows:
                raw = json.loads(row['raw']) if isinstance(row['raw'],str) else row['raw']
                values.append(native_values(platform, {'id':row['content_id'],'type':row['content_type']},
                    {'id':row['comment_id'],'author_id':row['author_id'],'body':row['body'],
                     'parent_id':row['parent_comment_id'],'raw':raw},
                    int(row['collected_at'].timestamp()*1000),int(row['updated_at'].timestamp()*1000)))
            async with conn.transaction():
                await conn.executemany(upsert_sql(platform), values)
            last = rows[-1]
            after = (last['content_type'],last['content_id'],last['comment_id'])
            total += len(rows)
        counts[platform] = total
    return counts
