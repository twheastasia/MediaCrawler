"""WeChat record storage, isolated in wxarticle_* tables."""
from __future__ import annotations

import json
import asyncpg
import config
from collection.database import encoded


class WxArticleRecords:
    def __init__(self, pool, run_id, platform):
        if platform != 'weixin':
            raise ValueError('wxarticle_platform_must_be_weixin')
        self.pool, self.run_id, self.platform = pool, run_id, platform

    @classmethod
    async def open(cls, run_id, platform):
        c = config.postgres_db_config
        pool = await asyncpg.create_pool(host=c['host'],port=int(c['port']),user=c['user'],
                 password=c['password'],database=c['db_name'],min_size=1,max_size=2,
                 command_timeout=30)
        return cls(pool, run_id, platform)


    async def state(self, key, value=None):
        if value is None:
            raw = await self.pool.fetchval('SELECT state FROM wxarticle_progress '
                'WHERE run_id=$1 AND platform=$2 AND task_key=$3',self.run_id,self.platform,key)
            return json.loads(raw) if raw else None
        await self.pool.execute('INSERT INTO wxarticle_progress VALUES($1,$2,$3,$4::jsonb,now()) '
            'ON CONFLICT(run_id,platform,task_key) DO UPDATE SET state=excluded.state,updated_at=now()',
            self.run_id,self.platform,key,encoded(value))

    async def content(self, item):
        await self.pool.execute('''INSERT INTO wxarticle_content
         (platform,content_type,content_id,author_id,title,body,source_url,published_at,raw)
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
         ON CONFLICT(platform,content_type,content_id) DO UPDATE SET
         author_id=excluded.author_id,title=excluded.title,body=excluded.body,
         source_url=excluded.source_url,published_at=excluded.published_at,
         raw=excluded.raw,updated_at=now()''',self.platform,item['type'],item['id'],
         item.get('author_id'),item.get('title'),item.get('body'),item.get('url'),
         item.get('time'),encoded(item['raw']))

    async def cached(self, kind, content_id):
        row=await self.pool.fetchrow('SELECT * FROM wxarticle_content '
            'WHERE platform=$1 AND content_type=$2 AND content_id=$3',self.platform,kind,content_id)
        if not row:return None
        d=dict(row)
        return dict(type=kind,id=content_id,author_id=d['author_id'],title=d['title'],
                    body=d['body'],url=d['source_url'],time=d['published_at'],raw=json.loads(d['raw']))

    async def link(self, keyword, role, item):
        await self.pool.execute('INSERT INTO wxarticle_link VALUES($1,$2,$3,$4,$5,$6) '
            'ON CONFLICT DO NOTHING',self.run_id,self.platform,keyword,role,item['type'],item['id'])

    async def links(self, keyword):
        return await self.pool.fetch('SELECT content_type,content_id FROM wxarticle_link '
           "WHERE run_id=$1 AND platform=$2 AND keyword=$3 AND role='search'",
           self.run_id,self.platform,keyword)

    async def author(self, author_id, name, home, raw):
        await self.pool.execute('INSERT INTO wxarticle_author VALUES($1,$2,$3,$4,$5::jsonb,now()) '
            'ON CONFLICT(platform,author_id) DO UPDATE SET name=excluded.name,home_url=excluded.home_url,'
            'raw=excluded.raw,updated_at=now()',self.platform,author_id,name,home,encoded(raw))

    async def authors(self):
        return await self.pool.fetch('''SELECT DISTINCT c.author_id,l.keyword
          FROM wxarticle_content c JOIN wxarticle_link l USING(platform,content_type,content_id)
          WHERE l.run_id=$1 AND l.platform=$2 AND l.role='search'
          AND c.author_id IS NOT NULL AND c.author_id<>'' ''',self.run_id,self.platform)

    async def media(self, item, resource, path=None, error=None):
        await self.pool.execute('INSERT INTO wxarticle_media VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,now()) '
            'ON CONFLICT(platform,content_type,content_id,stem) DO UPDATE SET '
            'source_url=excluded.source_url,local_path=excluded.local_path,bytes=excluded.bytes,'
            'status=excluded.status,error=excluded.error,updated_at=now()',self.platform,item['type'],
            item['id'],resource.stem,resource.url,str(path) if path else None,
            path.stat().st_size if path else None,'downloaded' if path else 'failed',error)

    async def counts(self):
        rows=await self.pool.fetch('SELECT keyword,role,count(*) AS total FROM wxarticle_link '
            'WHERE run_id=$1 AND platform=$2 GROUP BY keyword,role ORDER BY keyword,role',
            self.run_id,self.platform)
        return [dict(x) for x in rows]

    async def close(self):
        await self.pool.close()

