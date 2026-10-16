"""Repair the historical &times entity corruption, preserving IDs and old aliases.

Defaults to a transactionally rolled-back preview; pass --apply to commit.
This repairs syntax only, not the lifetime of Sogou's signed URLs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import asyncpg
import config

from media_platform.weixin.extractor import article_url, digest

BROKEN = re.compile(r"%c3%97tamp%3d|×tamp=", re.I)
AFFECTED = """platform='weixin' AND content_type='article' AND
    (strpos(lower(source_url),'%c3%97tamp%3d')>0 OR strpos(source_url,'×tamp=')>0)"""


def repaired_url(value):
    if not BROKEN.search(value):
        return value
    parsed = urlsplit(value)
    if parsed.hostname != "mp.weixin.qq.com" or parsed.path != "/s":
        raise ValueError("unexpected_corrupt_url")
    if len(BROKEN.findall(value)) != 1 or "timestamp" in parse_qs(parsed.query):
        raise ValueError("ambiguous_timestamp")
    fixed = BROKEN.sub("&timestamp=", value)
    timestamps = parse_qs(urlsplit(fixed).query).get("timestamp", [])
    if len(timestamps) != 1 or not re.fullmatch(r"\d{10}", timestamps[0]):
        raise ValueError("invalid_timestamp")
    article_url(fixed)  # Validate without rewriting the signed query string.
    return fixed


async def repair(connection, dry_run=True):
    tx = connection.transaction()
    await tx.start()
    try:
        await connection.execute("SET LOCAL lock_timeout='1s'")
        await connection.execute("SET LOCAL statement_timeout='30s'")
        if not await connection.fetchval("SELECT pg_try_advisory_xact_lock(hashtext('acquisition:weixin'))"):
            raise RuntimeError("weixin_worker_running")
        rows = await connection.fetch("SELECT content_id,source_url FROM wxarticle_content WHERE " + AFFECTED + " FOR UPDATE")
        for row in rows:
            old, content_id = row["source_url"], row["content_id"]
            fixed = repaired_url(old)
            # Keep legacy aliases so existing checkpoints still resolve.
            for url in {old, fixed, article_url(fixed)}:
                key = digest(url)
                await connection.execute("INSERT INTO wxarticle_alias(alias_key,content_id) VALUES($1,$2) ON CONFLICT DO NOTHING", key, content_id)
                actual = await connection.fetchval("SELECT content_id FROM wxarticle_alias WHERE alias_key=$1", key)
                if actual != content_id:
                    raise RuntimeError("url_alias_collision")
            await connection.execute("UPDATE wxarticle_content SET source_url=$2,updated_at=now() WHERE platform='weixin' AND content_type='article' AND content_id=$1", content_id, fixed)
        remaining = await connection.fetchval("SELECT count(*) FROM wxarticle_content WHERE " + AFFECTED)
        if remaining:
            raise RuntimeError("corrupt_urls_remain")
        result = {"dry_run": dry_run, "repaired": len(rows), "remaining": remaining}
        await (tx.rollback() if dry_run else tx.commit())
        return result
    except BaseException:
        await tx.rollback()
        raise


async def main(apply):
    c = config.postgres_db_config
    connection = await asyncpg.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                                       password=c["password"], database=c["db_name"], command_timeout=30)
    try:
        print(json.dumps(await repair(connection, dry_run=not apply)))
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
