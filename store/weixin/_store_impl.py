"""WeChat tasks and data stored exclusively in wxarticle_* tables."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collection.database import encoded
from store.weixin.records import WxArticleRecords
from store.weixin.schema import DDL
from hashlib import sha256
from model.m_weixin import WeixinAuthor


def digest(value):
    return sha256(value.encode()).hexdigest()



def decode(row):
    if not row:
        return None
    result = dict(row)
    for key in ("payload", "evidence"):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result


class WeixinStore(WxArticleRecords):
    async def initialize(self, settings, resume=False):
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SET LOCAL lock_timeout='1s'")
                if await c.fetchval("SELECT to_regclass('acquisition_weixin_task')"):
                    raise RuntimeError("wxarticle_migration_required")
                await c.execute(DDL)
                existing = await c.fetchval("SELECT config FROM wxarticle_run WHERE run_id=$1", self.run_id)
                if existing:
                    old = json.loads(existing)
                    if old != settings:
                        raise ValueError("run_configuration_mismatch")
                    if not resume:
                        raise ValueError("existing_run_requires_resume")
                await c.execute("INSERT INTO wxarticle_run(run_id,config) VALUES($1,$2::jsonb) ON CONFLICT DO NOTHING", self.run_id, encoded(settings))
                if resume:
                    await c.execute("UPDATE wxarticle_task SET status='pending',attempts=0 WHERE run_id=$1 AND status IN ('running','blocked','failed')", self.run_id)
                    await c.execute("DELETE FROM wxarticle_progress WHERE run_id=$1 AND platform='weixin' AND state->>'reason'='search_failed'", self.run_id)

    async def enqueue(self, key, kind, payload):
        await self.pool.execute("""INSERT INTO wxarticle_task(run_id,task_key,kind,payload)
            VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING""", self.run_id, key, kind, encoded(payload))

    async def task(self, key):
        return decode(await self.pool.fetchrow("SELECT * FROM wxarticle_task WHERE run_id=$1 AND task_key=$2", self.run_id, key))

    async def tasks(self, kind):
        return [decode(row) for row in await self.pool.fetch("""SELECT * FROM wxarticle_task
            WHERE run_id=$1 AND kind=$2 AND status='pending' ORDER BY task_key""", self.run_id, kind)]

    async def start(self, key):
        await self.pool.execute("""UPDATE wxarticle_task SET status='running',attempts=attempts+1,
            error_code=NULL,updated_at=now() WHERE run_id=$1 AND task_key=$2""", self.run_id, key)

    async def finish(self, key, status, error=None):
        await self.pool.execute("""UPDATE wxarticle_task SET status=$3,error_code=$4,
            updated_at=now() WHERE run_id=$1 AND task_key=$2""", self.run_id, key, status, error)

    async def search_results(self, key, keyword, page, rows):
        # All candidate evidence and tasks commit before the search page advances.
        async with self.pool.acquire() as c:
            async with c.transaction():
                for row in rows:
                    article_key = "article:" + digest(row["url"])
                    await c.execute("""INSERT INTO wxarticle_task(run_id,task_key,kind,payload)
                        VALUES($1,$2,'article',$3::jsonb) ON CONFLICT(run_id,task_key) DO UPDATE SET
                        status=CASE WHEN wxarticle_task.status IN ('succeeded','deferred') THEN 'pending' ELSE wxarticle_task.status END,
                        attempts=CASE WHEN wxarticle_task.status IN ('succeeded','deferred') THEN 0 ELSE wxarticle_task.attempts END""", self.run_id, article_key, encoded({"url": row["url"]}))
                    await c.execute("""INSERT INTO wxarticle_source(run_id,source_key,candidate_key,evidence)
                        VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING""", self.run_id,
                        "search:" + keyword, article_key, encoded({**row, "page": page}))
                await c.execute("UPDATE wxarticle_task SET status='succeeded',updated_at=now() WHERE run_id=$1 AND task_key=$2", self.run_id, key)

    async def seed(self, url):
        key = "article:" + digest(url)
        await self.enqueue(key, "article", {"url": url})
        await self.pool.execute("""INSERT INTO wxarticle_source(run_id,source_key,candidate_key,evidence)
            VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING""", self.run_id, "seed:" + digest(url), key, encoded({"url": url}))

    async def known_article(self, aliases):
        return await self.pool.fetchval("SELECT content_id FROM wxarticle_alias WHERE alias_key=ANY($1::text[]) LIMIT 1", [digest(u) for u in aliases])

    async def persist_article(self, key, item, aliases, media=True):
        async with self.pool.acquire() as c:
            async with c.transaction():
                rows = await c.fetch("SELECT DISTINCT content_id FROM wxarticle_alias WHERE alias_key=ANY($1::text[])", [digest(u) for u in aliases])
                existing_ids = {r["content_id"] for r in rows}
                stable_ids = sorted(i for i in existing_ids if i.startswith("wx_"))
                preferred = item["id"] if item["id"].startswith("wx_") else (stable_ids or sorted(existing_ids) or [item["id"]])[0]
                item = {**item, "id": preferred}
                bound = WxArticleRecords(c, self.run_id, "weixin")
                item["raw"].setdefault("extracted_at", datetime.now(timezone.utc).isoformat())
                await bound.content(item)
                for old_id in existing_ids - {preferred}:
                    await self._merge(c, old_id, preferred)
                if item.get("author_id"):
                    meta = item["raw"]["metadata_raw"]
                    author = WeixinAuthor(author_id=item["author_id"], name=meta["account_name"],
                                          raw={"biz": item["author_id"], "name": meta["account_name"]})
                    await bound.author(author.author_id, author.name, author.home_url, author.raw)
                for url in set(aliases + [item["url"]]):
                    await c.execute("INSERT INTO wxarticle_alias(alias_key,content_id) VALUES($1,$2) ON CONFLICT DO NOTHING", digest(url), item["id"])
                await self._link_sources(c, key, item)
                for ref in item["raw"]["media_refs"]:
                    supported = media and ref["type"] == "image"
                    status = "reference" if ref["type"] == "link" else ("pending" if supported else ("unsupported" if ref["type"] != "image" else "disabled"))
                    await c.execute("""INSERT INTO wxarticle_media(platform,content_type,content_id,stem,source_url,status)
                        VALUES('weixin','article',$1,$2,$3,$4) ON CONFLICT(platform,content_type,content_id,stem) DO UPDATE
                        SET status=CASE WHEN excluded.status='reference' THEN 'reference' ELSE wxarticle_media.status END""", item["id"], ref["stem"], ref["url"], status)
                    await c.execute("""INSERT INTO wxarticle_media_detail(content_id,stem,media_type,status)
                        VALUES($1,$2,$3,$4) ON CONFLICT(content_id,stem) DO UPDATE
                        SET status=CASE WHEN excluded.status='reference' THEN 'reference' ELSE wxarticle_media_detail.status END""", item["id"], ref["stem"], ref["type"], status)
                    if supported:
                        cached = await c.fetchrow("""SELECT m.status,m.local_path,w.sha256 FROM wxarticle_media m
                            JOIN wxarticle_media_detail w USING(content_id,stem)
                            WHERE m.platform='weixin' AND m.content_type='article' AND m.content_id=$1 AND m.stem=$2""", item["id"], ref["stem"])
                        valid = False
                        if cached and cached["status"] == "downloaded" and cached["local_path"] and cached["sha256"]:
                            path = Path(cached["local_path"])
                            if path.is_file():
                                import hashlib
                                with path.open("rb") as stream:
                                    valid = hashlib.file_digest(stream, "sha256").hexdigest() == cached["sha256"]
                        await c.execute("""INSERT INTO wxarticle_task(run_id,task_key,kind,payload)
                            VALUES($1,$2,'media',$3::jsonb) ON CONFLICT DO NOTHING""", self.run_id,
                            "media:" + item["id"] + ":" + ref["stem"], encoded({**ref, "content_id": item["id"]}))
                        if valid:
                            await c.execute("UPDATE wxarticle_task SET status='succeeded',error_code=NULL WHERE run_id=$1 AND task_key=$2", self.run_id, "media:" + item["id"] + ":" + ref["stem"])
                await c.execute("UPDATE wxarticle_task SET status='succeeded',updated_at=now() WHERE run_id=$1 AND task_key=$2", self.run_id, key)
                return item

    async def _merge(self, c, old_id, new_id):
        """Resolve short/long URL identities in the same transaction as the article."""
        await c.execute("""INSERT INTO wxarticle_comment
            (platform,content_type,content_id,comment_id,parent_comment_id,author_id,author_name,
             body,published_at,like_count,raw,collected_at,updated_at)
            SELECT platform,content_type,$2,comment_id,parent_comment_id,author_id,author_name,
             body,published_at,like_count,raw,collected_at,updated_at FROM wxarticle_comment
            WHERE platform='weixin' AND content_type='article' AND content_id=$1 ON CONFLICT DO NOTHING""",old_id,new_id)
        await c.execute("DELETE FROM wxarticle_comment WHERE platform='weixin' AND content_type='article' AND content_id=$1",old_id)
        await c.execute("""INSERT INTO wxarticle_link(run_id,platform,keyword,role,content_type,content_id)
            SELECT run_id,platform,keyword,role,content_type,$2 FROM wxarticle_link
            WHERE platform='weixin' AND content_type='article' AND content_id=$1 ON CONFLICT DO NOTHING""", old_id, new_id)
        await c.execute("DELETE FROM wxarticle_link WHERE platform='weixin' AND content_type='article' AND content_id=$1", old_id)
        await c.execute("""INSERT INTO wxarticle_media(platform,content_type,content_id,stem,source_url,local_path,bytes,status,error,updated_at)
            SELECT platform,content_type,$2,stem,source_url,local_path,bytes,status,error,updated_at FROM wxarticle_media
            WHERE platform='weixin' AND content_type='article' AND content_id=$1
            ON CONFLICT(platform,content_type,content_id,stem) DO UPDATE SET
              local_path=CASE WHEN excluded.status='downloaded' THEN excluded.local_path ELSE wxarticle_media.local_path END,
              bytes=CASE WHEN excluded.status='downloaded' THEN excluded.bytes ELSE wxarticle_media.bytes END,
              status=CASE WHEN excluded.status='downloaded' THEN excluded.status ELSE wxarticle_media.status END,
              error=CASE WHEN excluded.status='downloaded' THEN NULL ELSE wxarticle_media.error END""", old_id, new_id)
        await c.execute("DELETE FROM wxarticle_media WHERE platform='weixin' AND content_type='article' AND content_id=$1", old_id)
        await c.execute("""INSERT INTO wxarticle_media_detail(content_id,stem,media_type,sha256,status,error_code)
            SELECT $2,stem,media_type,sha256,status,error_code FROM wxarticle_media_detail WHERE content_id=$1
            ON CONFLICT(content_id,stem) DO UPDATE SET
              sha256=CASE WHEN excluded.status='downloaded' THEN excluded.sha256 ELSE wxarticle_media_detail.sha256 END,
              status=CASE WHEN excluded.status='downloaded' THEN excluded.status ELSE wxarticle_media_detail.status END,
              error_code=CASE WHEN excluded.status='downloaded' THEN NULL ELSE wxarticle_media_detail.error_code END""", old_id, new_id)
        await c.execute("DELETE FROM wxarticle_media_detail WHERE content_id=$1", old_id)
        await c.execute("""DELETE FROM wxarticle_task old USING wxarticle_task target
            WHERE old.kind='media' AND target.kind='media' AND old.run_id=target.run_id
            AND old.payload->>'content_id'=$1 AND target.payload->>'content_id'=$2
            AND old.payload->>'stem'=target.payload->>'stem'""", old_id, new_id)
        await c.execute("""UPDATE wxarticle_task SET task_key='media:'||$2||':'||(payload->>'stem'),
            payload=jsonb_set(payload,'{content_id}',to_jsonb($2::text))
            WHERE kind='media' AND payload->>'content_id'=$1""", old_id, new_id)
        await c.execute("UPDATE wxarticle_source SET content_id=$2 WHERE content_id=$1", old_id, new_id)
        await c.execute("UPDATE wxarticle_alias SET content_id=$2 WHERE content_id=$1", old_id, new_id)
        await c.execute("DELETE FROM wxarticle_content WHERE platform='weixin' AND content_type='article' AND content_id=$1", old_id)

    async def _link_sources(self, c, key, item):
        rows = await c.fetch("UPDATE wxarticle_source SET content_id=$3 WHERE run_id=$1 AND candidate_key=$2 RETURNING source_key", self.run_id, key, item["id"])
        for row in rows:
            if row["source_key"].startswith("search:"):
                await WxArticleRecords(c, self.run_id, "weixin").link(row["source_key"][7:], "search", item)

    async def link_cached(self, key, content_id):
        async with self.pool.acquire() as c:
            async with c.transaction():
                await self._link_sources(c, key, {"id": content_id, "type": "article"})
                await c.execute("UPDATE wxarticle_task SET status='succeeded',updated_at=now() WHERE run_id=$1 AND task_key=$2", self.run_id, key)

    async def successful(self, keyword):
        return await self.pool.fetchval("SELECT count(DISTINCT content_id) FROM wxarticle_source WHERE run_id=$1 AND source_key=$2 AND content_id IS NOT NULL", self.run_id, "search:" + keyword)

    async def media_result(self, task, status, path=None, checksum=None, error=None):
        ref = task["payload"]
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("""UPDATE wxarticle_media SET status=$3,local_path=$4,bytes=$5,error=$6,updated_at=now()
                    WHERE platform='weixin' AND content_type='article' AND content_id=$1 AND stem=$2""",
                    ref["content_id"], ref["stem"], status, str(path) if path else None, path.stat().st_size if path else None, error)
                await c.execute("""UPDATE wxarticle_media_detail SET status=$3,sha256=$4,error_code=$5,updated_at=now()
                    WHERE content_id=$1 AND stem=$2""", ref["content_id"], ref["stem"], status, checksum, error)
                await c.execute("UPDATE wxarticle_task SET status=$3,error_code=$4,updated_at=now() WHERE run_id=$1 AND task_key=$2", self.run_id, task["task_key"], "succeeded" if status == "downloaded" else status, error)

    async def summary(self):
        tasks = await self.pool.fetch("SELECT kind,status,count(*) AS total FROM wxarticle_task WHERE run_id=$1 GROUP BY kind,status ORDER BY kind,status", self.run_id)
        sources = await self.pool.fetch("SELECT source_key,count(*) AS discovered,count(DISTINCT content_id) AS articles FROM wxarticle_source WHERE run_id=$1 GROUP BY source_key ORDER BY source_key", self.run_id)
        media = await self.pool.fetch("""SELECT m.status,count(*) AS total FROM wxarticle_media m
            WHERE m.platform='weixin' AND m.content_type='article' AND m.content_id IN
            (SELECT content_id FROM wxarticle_source WHERE run_id=$1 AND content_id IS NOT NULL)
            GROUP BY m.status ORDER BY m.status""", self.run_id)
        return {"tasks": [dict(r) for r in tasks], "sources": [dict(r) for r in sources], "media": [dict(r) for r in media]}

    async def repair_media(self):
        """Only requeue missing/corrupt files belonging to this run."""
        import hashlib
        rows = await self.pool.fetch("""SELECT t.*,m.local_path,w.sha256 FROM wxarticle_task t
            JOIN wxarticle_media m ON m.platform='weixin' AND m.content_type='article'
             AND m.content_id=t.payload->>'content_id' AND m.stem=t.payload->>'stem'
            JOIN wxarticle_media_detail w ON w.content_id=m.content_id AND w.stem=m.stem
            WHERE t.run_id=$1 AND t.kind='media' AND t.status='succeeded'""", self.run_id)
        for row in rows:
            valid = False
            path = Path(row["local_path"]) if row["local_path"] else None
            if path and path.is_file() and row["sha256"]:
                with path.open("rb") as stream:
                    valid = hashlib.file_digest(stream, "sha256").hexdigest() == row["sha256"]
            if not valid:
                await self.pool.execute("UPDATE wxarticle_task SET status='pending',attempts=0,error_code='file_missing_or_corrupt' WHERE run_id=$1 AND task_key=$2", self.run_id, row["task_key"])
