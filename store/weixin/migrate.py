"""Move legacy WeChat rows into wxarticle_* without altering other platforms.

Run python -m store.weixin.migrate [--dry-run]. Each migration is one
transaction, protected by the same platform lock as the WeChat worker.
"""
from __future__ import annotations

import argparse
import asyncio
import json

import asyncpg
import config

from store.weixin.schema import DDL

SHARED = {"acquisition_" + name: "wxarticle_" + name for name in
          ("run", "content", "author", "link", "media", "progress")}
PRIVATE = {
    "acquisition_weixin_task": "wxarticle_task",
    "acquisition_weixin_source": "wxarticle_source",
    "acquisition_weixin_alias": "wxarticle_alias",
    "acquisition_weixin_media": "wxarticle_media_detail",
}


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


async def columns(connection, table):
    rows = await connection.fetch("""SELECT column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=$1 ORDER BY ordinal_position""", table)
    return [r["column_name"] for r in rows]


async def migrate(connection, dry_run=False):
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("SET LOCAL lock_timeout='1s'")
        await connection.execute("SET LOCAL statement_timeout='30s'")
        if not await connection.fetchval("SELECT pg_try_advisory_xact_lock(hashtext('acquisition:weixin'))"):
            raise RuntimeError("weixin_worker_running")
        present = {name for name in (*SHARED, *PRIVATE)
                   if await connection.fetchval("SELECT to_regclass($1)", name)}
        run_ids = set()
        for name in ("acquisition_link", "acquisition_progress", "acquisition_weixin_task", "acquisition_weixin_source"):
            if name in present:
                condition = " WHERE platform='weixin'" if name in SHARED else ""
                run_ids.update(r["run_id"] for r in await connection.fetch(f"SELECT DISTINCT run_id FROM {quoted(name)}{condition}") if r["run_id"])
        if "acquisition_run" in present:
            run_ids.update(r["run_id"] for r in await connection.fetch("SELECT run_id FROM acquisition_run WHERE config->'platforms' @> '[\"weixin\"]'::jsonb"))
        await connection.execute(DDL)
        copied = {}
        # Parent run/content records come before the rows referring to them.
        for source, target in {**SHARED, **PRIVATE}.items():
            if source not in present:
                continue
            source_columns = await columns(connection, source)
            if source_columns != await columns(connection, target):
                raise RuntimeError("column_mismatch:" + source)
            names = ",".join(map(quoted, source_columns))
            args = []
            if source == "acquisition_run":
                condition = " WHERE run_id=ANY($1::text[])"
                args = [sorted(run_ids)]
            elif source in SHARED:
                condition = " WHERE platform='weixin'"
            else:
                condition = ""
            select = f"SELECT {names} FROM {quoted(source)}{condition}"
            copied[target] = await connection.fetchval(f"SELECT count(*) FROM {quoted(source)}{condition}", *args)
            await connection.execute(f"INSERT INTO {quoted(target)}({names}) {select} ON CONFLICT DO NOTHING", *args)
            mismatch = await connection.fetchval(f"SELECT EXISTS({select} EXCEPT SELECT {names} FROM {quoted(target)})", *args)
            if mismatch:
                raise RuntimeError("row_verification_failed:" + source)
        # Every copied row has been compared, including JSONB and timestamps.
        # These DELETE predicates never target another platform's records.
        for name in ("link", "media", "progress", "content", "author"):
            table = "acquisition_" + name
            if table in present:
                await connection.execute(f"DELETE FROM {quoted(table)} WHERE platform='weixin'")
        if "acquisition_run" in present:
            filters = ["config->'platforms'='[\"weixin\"]'::jsonb", "run_id=ANY($1::text[])"]
            for child in ("acquisition_link", "acquisition_progress"):
                if child in present:
                    filters.append(f"NOT EXISTS(SELECT 1 FROM {quoted(child)} child WHERE child.run_id=acquisition_run.run_id)")
            await connection.execute("DELETE FROM acquisition_run WHERE " + " AND ".join(filters), sorted(run_ids))
        for table in PRIVATE:
            if table in present:
                # RESTRICT is intentional: unknown dependencies abort and roll back.
                await connection.execute(f"DROP TABLE {quoted(table)}")
        result = {"dry_run": dry_run, "copied_rows": copied,
                  "tables": list(SHARED.values()) + list(PRIVATE.values())}
        if dry_run:
            await transaction.rollback()
        else:
            await transaction.commit()
        return result
    except BaseException:
        await transaction.rollback()
        raise


async def run(dry_run):
    c = config.postgres_db_config
    connection = await asyncpg.connect(host=c["host"], port=int(c["port"]), user=c["user"],
                                       password=c["password"], database=c["db_name"], command_timeout=30)
    try:
        return await migrate(connection, dry_run)
    finally:
        await connection.close()


def main():
    parser = argparse.ArgumentParser(description="将微信数据和断点事务迁移至 wxarticle_* 表")
    parser.add_argument("--dry-run", action="store_true", help="核对并演练迁移，最终回滚")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args.dry_run)), ensure_ascii=False))
    except Exception as error:
        # Never print driver messages/connection configuration.
        code = str(error) if type(error) is RuntimeError else type(error).__name__
        print(json.dumps({"status": "migration_rolled_back", "error_code": code}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
