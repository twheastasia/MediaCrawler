"""Backfill platform comment tables from the selected collection runs."""
import argparse
import asyncio
import json

import asyncpg
import config
from collection.native_comments import TABLES, backfill, schema_ddl


async def audit(conn, run_ids):
    result = {}
    for platform, (model, content_column) in TABLES.items():
        type_join = ' AND n.content_type=c.content_type' if platform == 'zhihu' else ''
        rows = await conn.fetch(f'''SELECT c.comment_id,c.body,c.author_id,c.raw,
            c.parent_comment_id,n.id AS native_id,n.content,n.user_id,n.parent_comment_id AS native_parent
            FROM acquisition_comment c LEFT JOIN {model.__tablename__} n
              ON n.{content_column}=c.content_id AND n.comment_id=c.comment_id {type_join}
            WHERE c.platform=$1 AND EXISTS(SELECT 1 FROM acquisition_link l
              WHERE l.run_id=ANY($2::text[]) AND l.platform=c.platform
                AND l.content_type=c.content_type AND l.content_id=c.content_id)''',platform,run_ids)
        from collection.native_comments import public_user_id
        missing = mismatch = missing_user = replies = 0
        for row in rows:
            raw = json.loads(row['raw']) if isinstance(row['raw'],str) else row['raw']
            expected_user = public_user_id(raw, row['author_id'])
            missing += row['native_id'] is None
            missing_user += not row['user_id']
            replies += bool(row['parent_comment_id'])
            mismatch += (row['native_id'] is not None and
                         (row['body'] != row['content'] or expected_user != row['user_id'] or
                          row['parent_comment_id'] != row['native_parent']))
        result[platform] = {'table':model.__tablename__,'source_comments':len(rows),
            'native_comments':len(rows)-missing,'missing':missing,'mismatched':mismatch,
            'missing_user_id':missing_user,'replies':replies}
    return result


async def run(run_ids):
    settings = config.postgres_db_config
    conn = await asyncpg.connect(host=settings['host'],port=int(settings['port']),
        user=settings['user'],password=settings['password'],database=settings['db_name'],
        timeout=10,command_timeout=30)
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute(schema_ddl())
        copied = await backfill(conn, run_ids)
        return {'run_ids':run_ids,'backfilled':copied,'audit':await audit(conn,run_ids)}
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',action='append',required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.run_id)),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
