from __future__ import annotations

import json
import asyncpg
import config

DDL = '''
CREATE TABLE IF NOT EXISTS acquisition_run (
 run_id text PRIMARY KEY, config jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS acquisition_content (
 platform text NOT NULL, content_type text NOT NULL, content_id text NOT NULL,
 author_id text, title text, body text, source_url text, published_at bigint,
 raw jsonb NOT NULL, updated_at timestamptz DEFAULT now(),
 PRIMARY KEY(platform,content_type,content_id));
CREATE TABLE IF NOT EXISTS acquisition_author (
 platform text NOT NULL, author_id text NOT NULL, name text, home_url text,
 raw jsonb NOT NULL, updated_at timestamptz DEFAULT now(), PRIMARY KEY(platform,author_id));
CREATE TABLE IF NOT EXISTS acquisition_link (
 run_id text REFERENCES acquisition_run(run_id), platform text, keyword text,
 role text CHECK(role IN ('search','history')), content_type text, content_id text,
 PRIMARY KEY(run_id,platform,keyword,role,content_type,content_id),
 FOREIGN KEY(platform,content_type,content_id)
 REFERENCES acquisition_content(platform,content_type,content_id));
CREATE TABLE IF NOT EXISTS acquisition_media (
 platform text, content_type text, content_id text, stem text,
 source_url text, local_path text, bytes bigint, status text, error text,
 updated_at timestamptz DEFAULT now(), PRIMARY KEY(platform,content_type,content_id,stem),
 FOREIGN KEY(platform,content_type,content_id)
 REFERENCES acquisition_content(platform,content_type,content_id));
CREATE TABLE IF NOT EXISTS acquisition_progress (
 run_id text REFERENCES acquisition_run(run_id), platform text, task_key text,
 state jsonb NOT NULL, updated_at timestamptz DEFAULT now(),
 PRIMARY KEY(run_id,platform,task_key));
CREATE INDEX IF NOT EXISTS acquisition_content_author_idx
 ON acquisition_content(platform,author_id,published_at DESC);
'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, default=str)


from collection.comment_store import comment_ddl
DDL += comment_ddl('acquisition')
from collection.native_comments import schema_ddl
DDL += schema_ddl()


class Store:
    def __init__(self, pool, run_id, platform):
        self.pool, self.run_id, self.platform = pool, run_id, platform

    @classmethod
    async def open(cls, run_id, platform):
        c = config.postgres_db_config
        pool = await asyncpg.create_pool(host=c['host'],port=int(c['port']),user=c['user'],
                 password=c['password'],database=c['db_name'],min_size=1,max_size=2,
                 command_timeout=30)
        return cls(pool, run_id, platform)

    async def setup(self, settings):
        async with self.pool.acquire() as c:
            await c.execute(DDL)
            await c.execute('INSERT INTO acquisition_run VALUES($1,$2::jsonb,now()) '
                            'ON CONFLICT(run_id) DO UPDATE SET config=excluded.config', self.run_id, encoded(settings))

    async def state(self, key, value=None):
        if value is None:
            raw = await self.pool.fetchval('SELECT state FROM acquisition_progress '
                'WHERE run_id=$1 AND platform=$2 AND task_key=$3',self.run_id,self.platform,key)
            return json.loads(raw) if raw else None
        await self.pool.execute('INSERT INTO acquisition_progress VALUES($1,$2,$3,$4::jsonb,now()) '
            'ON CONFLICT(run_id,platform,task_key) DO UPDATE SET state=excluded.state,updated_at=now()',
            self.run_id,self.platform,key,encoded(value))

    async def content(self, item):
        await self.pool.execute('''INSERT INTO acquisition_content
         (platform,content_type,content_id,author_id,title,body,source_url,published_at,raw)
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
         ON CONFLICT(platform,content_type,content_id) DO UPDATE SET
         author_id=excluded.author_id,title=excluded.title,body=excluded.body,
         source_url=excluded.source_url,published_at=excluded.published_at,
         raw=excluded.raw,updated_at=now()''',self.platform,item['type'],item['id'],
         item.get('author_id'),item.get('title'),item.get('body'),item.get('url'),
         item.get('time'),encoded(item['raw']))

    async def cached(self, kind, content_id):
        row=await self.pool.fetchrow('SELECT * FROM acquisition_content '
            'WHERE platform=$1 AND content_type=$2 AND content_id=$3',self.platform,kind,content_id)
        if not row:return None
        d=dict(row)
        return dict(type=kind,id=content_id,author_id=d['author_id'],title=d['title'],
                    body=d['body'],url=d['source_url'],time=d['published_at'],raw=json.loads(d['raw']))

    async def link(self, keyword, role, item):
        await self.pool.execute('INSERT INTO acquisition_link VALUES($1,$2,$3,$4,$5,$6) '
            'ON CONFLICT DO NOTHING',self.run_id,self.platform,keyword,role,item['type'],item['id'])

    async def links(self, keyword):
        return await self.pool.fetch('SELECT content_type,content_id FROM acquisition_link '
           "WHERE run_id=$1 AND platform=$2 AND keyword=$3 AND role='search'",
           self.run_id,self.platform,keyword)

    async def author(self, author_id, name, home, raw):
        await self.pool.execute('INSERT INTO acquisition_author VALUES($1,$2,$3,$4,$5::jsonb,now()) '
            'ON CONFLICT(platform,author_id) DO UPDATE SET name=excluded.name,home_url=excluded.home_url,'
            'raw=excluded.raw,updated_at=now()',self.platform,author_id,name,home,encoded(raw))

    async def authors(self):
        return await self.pool.fetch('''SELECT DISTINCT c.author_id,l.keyword
          FROM acquisition_content c JOIN acquisition_link l USING(platform,content_type,content_id)
          WHERE l.run_id=$1 AND l.platform=$2 AND l.role='search'
          AND c.author_id IS NOT NULL AND c.author_id<>'' ''',self.run_id,self.platform)

    async def media(self, item, resource, path=None, error=None):
        await self.pool.execute('INSERT INTO acquisition_media VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,now()) '
            'ON CONFLICT(platform,content_type,content_id,stem) DO UPDATE SET '
            'source_url=excluded.source_url,local_path=excluded.local_path,bytes=excluded.bytes,'
            'status=excluded.status,error=excluded.error,updated_at=now()',self.platform,item['type'],
            item['id'],resource.stem,resource.url,str(path) if path else None,
            path.stat().st_size if path else None,'downloaded' if path else 'failed',error)

    async def counts(self):
        rows=await self.pool.fetch('SELECT keyword,role,count(*) AS total FROM acquisition_link '
            'WHERE run_id=$1 AND platform=$2 GROUP BY keyword,role ORDER BY keyword,role',
            self.run_id,self.platform)
        return [dict(x) for x in rows]

    async def close(self):
        await self.pool.close()
