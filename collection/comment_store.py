"""Raw public comments, with each page and its checkpoint committed together."""
import json
from collection.native_comments import save_native


def encoded(value):
    return json.dumps(value,ensure_ascii=False,default=str)


def comment_ddl(prefix):
    if prefix not in ('acquisition', 'wxarticle'):
        raise ValueError('invalid_comment_namespace')
    return f'''
CREATE TABLE IF NOT EXISTS {prefix}_comment (
 platform text NOT NULL, content_type text NOT NULL, content_id text NOT NULL,
 comment_id text NOT NULL, parent_comment_id text, author_id text, author_name text,
 body text, published_at bigint, like_count bigint, raw jsonb NOT NULL,
 collected_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(platform,content_type,content_id,comment_id),
 FOREIGN KEY(platform,content_type,content_id)
 REFERENCES {prefix}_content(platform,content_type,content_id));
CREATE INDEX IF NOT EXISTS {prefix}_comment_parent_idx
 ON {prefix}_comment(platform,content_type,content_id,parent_comment_id);
'''


async def save_page(store, item, rows, key, state, prefix='acquisition'):
    if prefix not in ('acquisition', 'wxarticle'):
        raise ValueError('invalid_comment_namespace')
    async with store.pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                await conn.execute(f'''INSERT INTO {prefix}_comment
                 (platform,content_type,content_id,comment_id,parent_comment_id,author_id,
                  author_name,body,published_at,like_count,raw)
                 VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
                 ON CONFLICT(platform,content_type,content_id,comment_id) DO UPDATE SET
                  parent_comment_id=COALESCE(excluded.parent_comment_id,{prefix}_comment.parent_comment_id),
                  author_id=excluded.author_id,author_name=excluded.author_name,body=excluded.body,
                  published_at=excluded.published_at,like_count=excluded.like_count,
                  raw=excluded.raw,updated_at=now()''', store.platform, item['type'], item['id'],
                  row['id'],row.get('parent_id'),row.get('author_id'),row.get('author_name'),
                  row.get('body'),row.get('time'),row.get('likes'),encoded(row['raw']))
            if prefix == 'acquisition':
                await save_native(conn, store.platform, item, rows)
            await conn.execute(f'''INSERT INTO {prefix}_progress VALUES($1,$2,$3,$4::jsonb,now())
             ON CONFLICT(run_id,platform,task_key) DO UPDATE SET state=excluded.state,updated_at=now()''',
             store.run_id,store.platform,key,encoded(state))


async def summary(store, prefix='acquisition'):
    if prefix not in ('acquisition', 'wxarticle'):
        raise ValueError('invalid_comment_namespace')
    states=await store.pool.fetch(f'''SELECT state->>'status' AS status,count(*) AS contents
        FROM {prefix}_progress WHERE run_id=$1 AND platform=$2 AND task_key LIKE 'comments:%'
        GROUP BY state->>'status' ORDER BY status''',store.run_id,store.platform)
    content_scope=(f'''EXISTS (SELECT 1 FROM wxarticle_source s
        WHERE s.run_id=$1 AND s.content_id=c.content_id)''' if prefix=='wxarticle' else f'''EXISTS
        (SELECT 1 FROM {prefix}_link l WHERE l.run_id=$1 AND l.platform=c.platform
         AND l.content_type=c.content_type AND l.content_id=c.content_id)''')
    total=await store.pool.fetchval(f'''SELECT count(*) FROM {prefix}_comment c
        WHERE platform=$2 AND {content_scope}''',store.run_id,store.platform)
    return {'comments':total,'contents_by_status':[dict(r) for r in states]}
