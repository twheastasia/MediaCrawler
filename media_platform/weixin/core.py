"""Isolated WeChat article worker. Does not import or change the four-site launcher.

Run `python -m media_platform.weixin --help` for URL/search and resume examples.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from collection.chrome import ChromePage
from collection.runtime import AccessRestricted, GatedTransport, RequestGate
from collection.settings import KEYWORDS
from media_platform.weixin.extractor import (
    ARTICLE_SNAPSHOT, SEARCH_SNAPSHOT, ArticleUnavailable, ParseError,
    access_check, article_url, candidate_url, digest, parse_article, parse_search,
    public_url, search_url,
)
from store.weixin import WeixinStore

ROOT = Path(__file__).resolve().parents[2]
GATE_LOG = ROOT / "data" / "weixin" / "requests.jsonl"
BARRIERS = {401, 403, 406, 412, 429, 461, 471}


class WorkerBusy(RuntimeError):
    pass


def save_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def media_allowed(url):
    p = urlsplit(url)
    host = p.hostname or ""
    return p.scheme == "https" and p.port in (None, 443) and any(host == domain or host.endswith("." + domain) for domain in ("qpic.cn", "qlogo.cn"))


class WeixinJob:
    def __init__(self, args, settings):
        self.args, self.settings = args, settings
        self.directory = Path(args.output).resolve()
        self.store = self.lock_connection = self.page = self.gate = None
        self.owns_lock = False
        self.initialized = False
        self.active_task = None
        self.keep_verification_page = False
        self.owned_target_id = None

    def progress(self, status, **fields):
        value = {"platform": "weixin", "run_id": self.args.run_id, "pid": os.getpid(),
                 "status": status, "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), **fields}
        if self.owned_target_id:
            value["page_target_id"] = self.owned_target_id
        save_json(self.directory / "status.json", value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    async def document(self, url, script):
        async def visit():
            response = await self.page.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response and response.status in BARRIERS:
                raise AccessRestricted("http_" + str(response.status))
            if response and response.status in (404, 410):
                raise ArticleUnavailable("http_" + str(response.status))
            if response and response.status >= 500:
                raise httpx.HTTPError("upstream_unavailable")
            # DOM may be populated shortly after DOMContentLoaded; no extra request.
            await self.page.page.wait_for_timeout(1500)
            if script == ARTICLE_SNAPSHOT:
                # Sogou uses a script-driven redirect. Wait for the destination DOM,
                # rather than sampling during its blank navigation interval.
                with suppress(Exception):
                    await self.page.page.wait_for_function("""() =>
                        !!document.querySelector('#js_content')?.innerHTML?.trim() ||
                        !!document.querySelector('#js_verify') ||
                        [...document.querySelectorAll('.weui-msg__title,.weui-msg__desc')].some(e=>e.textContent.trim())""", timeout=15000)
            return await self.page.evaluate(script)
        return await self.gate.action("weixin_document", visit)

    async def search_page(self, keyword, page):
        key = "search:" + digest(keyword) + ":" + str(page).zfill(6)
        await self.store.enqueue(key, "search", {"keyword": keyword, "page": page})
        task = await self.store.task(key)
        if task["status"] == "succeeded":
            return True
        if task["status"] != "pending":
            return False
        async def operation():
            snapshot = await self.document(search_url(keyword, page), SEARCH_SNAPSHOT)
            rows = parse_search(snapshot)
            await self.store.search_results(key, keyword, page, rows)
            if not rows:
                await self.store.state("exhausted:" + digest(keyword), {"page": page, "reason": "empty_results"})
        return await self.attempt(task, operation)

    async def article(self, task):
        url = task["payload"]["url"]
        known = await self.store.known_article([url])
        if known:
            # New run still needs its own source links and any missing media tasks.
            cached = await self.store.cached("article", known)
            if cached:
                await self.store.persist_article(task["task_key"], cached, [url], self.settings["media"])
                return
        snapshot = await self.document(url, ARTICLE_SNAPSHOT)
        def url_shape(value):
            parsed = urlsplit(value or "")
            return {"host": parsed.hostname, "path": parsed.path, "query_keys": sorted({key for key, _ in parse_qsl(parsed.query)})}
        await self.store.state("last_article_document", {
            "url_shape": url_shape(snapshot.get("url")), "canonical_shape": url_shape(snapshot.get("canonical")),
            "has_title": bool(snapshot.get("title")), "has_content": bool(snapshot.get("content_html")),
            "verify": bool(snapshot.get("verify")),
        })
        item = parse_article(snapshot)
        aliases = [url, article_url(snapshot["url"]), item["url"]]
        await self.store.persist_article(task["task_key"], item, aliases, self.settings["media"])

    async def articles(self):
        for task in await self.store.tasks("article"):
            if not task["payload"].get("seed"):
                sources = await self.store.pool.fetch("SELECT source_key FROM wxarticle_source WHERE run_id=$1 AND candidate_key=$2", self.args.run_id, task["task_key"])
                search_keywords = [r["source_key"][7:] for r in sources if r["source_key"].startswith("search:")]
                is_seed = any(r["source_key"].startswith("seed:") for r in sources)
                if search_keywords and not is_seed:
                    counts = [await self.store.successful(kw) for kw in search_keywords]
                    if all(count >= self.settings["limit"] for count in counts):
                        # Keep excess discovery evidence without fetching beyond quota.
                        await self.store.finish(task["task_key"], "deferred", "quota_reached")
                        continue
            await self.attempt(task, lambda task=task: self.article(task))

    async def attempt(self, task, operation):
        key = task["task_key"]
        self.active_task = key
        # Three total attempts across restarts; verification is never retried here.
        for number in range(task["attempts"], 3):
            await self.store.start(key)
            self.progress("running", stage=task["kind"], task_id=digest(key)[:16])
            try:
                await operation()
                self.active_task = None
                return True
            except AccessRestricted:
                await self.store.finish(key, "blocked", "access_restricted")
                raise
            except ParseError:
                await self.store.finish(key, "blocked", "parse_error")
                raise
            except ArticleUnavailable:
                await self.store.finish(key, "unavailable", "article_unavailable")
                self.active_task = None
                return False
            except (httpx.HTTPError, TimeoutError, OSError) as error:
                # Exception messages/URLs are deliberately not persisted.
                if number == 2:
                    await self.store.finish(key, "failed", type(error).__name__)
                    self.active_task = None
                    return False
            except Exception as error:
                from playwright.async_api import TimeoutError as BrowserTimeout
                if isinstance(error, BrowserTimeout):
                    if number < 2:
                        continue
                    await self.store.finish(key, "failed", "BrowserTimeout")
                    self.active_task = None
                    return False
                # Database errors are not mislabeled as successful collection.
                await self.store.finish(key, "failed", type(error).__name__)
                raise
        self.active_task = None
        return False

    async def download(self, task):
        ref = task["payload"]
        if not media_allowed(ref["url"]):
            await self.store.media_result(task, "unsupported", error="unsupported_media_host")
            return
        directory = self.directory / "media" / ref["content_id"]
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (ref["stem"] + ".part")
        url = ref["url"]
        try:
            async with httpx.AsyncClient(transport=GatedTransport(self.gate), timeout=60, follow_redirects=False,
                                         headers={"Referer": "https://mp.weixin.qq.com/"}) as client:
                for _ in range(5):
                    if not media_allowed(url):
                        await self.store.media_result(task, "unsupported", error="unsupported_redirect")
                        return
                    async with client.stream("GET", url) as response:
                        if response.status_code in BARRIERS:
                            raise AccessRestricted("media_access_restricted")
                        if response.status_code in (301, 302, 303, 307, 308):
                            url = public_url(urljoin(url, response.headers.get("location", "")))
                            continue
                        if response.status_code in (404, 410):
                            await self.store.media_result(task, "unavailable", error="media_unavailable")
                            return
                        response.raise_for_status()
                        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                                     "image/webp": ".webp", "image/avif": ".avif"}.get(mime)
                        if not extension:
                            body = (await response.aread())[:16384].decode("utf-8", errors="replace")
                            access_check(body)
                            await self.store.media_result(task, "failed", error="unexpected_media_type")
                            return
                        checksum = hashlib.sha256()
                        with temporary.open("wb") as stream:
                            async for block in response.aiter_bytes():
                                stream.write(block)
                                checksum.update(block)
                    if not temporary.stat().st_size:
                        raise httpx.HTTPError("empty_image")
                    path = temporary.with_suffix(extension)
                    temporary.replace(path)
                    await self.store.media_result(task, "downloaded", path, checksum.hexdigest())
                    return
                await self.store.media_result(task, "failed", error="redirect_limit")
        finally:
            temporary.unlink(missing_ok=True)

    async def media(self):
        for task in await self.store.tasks("media"):
            try:
                success = await self.attempt(task, lambda task=task: self.download(task))
                if not success:
                    await self.store.media_result(task, "failed", error="download_failed")
            except (AccessRestricted, ParseError):
                await self.store.media_result(task, "blocked", error="access_restricted")
                raise

    async def run(self):
        logging.disable(logging.CRITICAL)
        try:
            self.store = await WeixinStore.open(self.args.run_id, "weixin")
            self.lock_connection = await self.store.pool.acquire()
            self.owns_lock = await self.lock_connection.fetchval("SELECT pg_try_advisory_lock(hashtext('acquisition:weixin'))")
            if not self.owns_lock:
                raise WorkerBusy("weixin_worker_already_running")
            await self.store.initialize(self.settings, self.args.resume)
            self.initialized = True
            save_json(self.directory / "run.json", self.settings)
            GATE_LOG.parent.mkdir(parents=True, exist_ok=True)
            self.gate = RequestGate(GATE_LOG)
            self.page = ChromePage(self.gate)
            self.progress("connecting")
            await self.page.connect("about:blank")
            session = await self.page.context.new_cdp_session(self.page.page)
            try:
                self.owned_target_id = (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            finally:
                await session.detach()
            for url in self.settings["urls"]:
                await self.store.seed(url)
            if self.args.resume:
                await self.store.repair_media()
            await self.articles()
            for page in range(1, self.settings["max_pages"] + 1):
                any_needed = False
                for keyword in self.settings["keywords"]:
                    if await self.store.successful(keyword) >= self.settings["limit"]:
                        continue
                    if await self.store.state("exhausted:" + digest(keyword)):
                        continue
                    any_needed = True
                    if not await self.search_page(keyword, page):
                        await self.store.state("exhausted:" + digest(keyword), {"page": page, "reason": "search_failed"})
                    await self.articles()
                    # Process each page's media before doing more discovery.
                    await self.media()
                if not any_needed:
                    break
            await self.media()
            summary = await self.store.summary()
            gaps = any(r["status"] in ("failed", "unavailable", "blocked", "pending", "running") for r in summary["tasks"])
            gaps = gaps or any(r["status"] not in ("downloaded", "disabled", "reference") for r in summary["media"])
            quotas = [{"keyword": kw, "target": self.settings["limit"], "collected": await self.store.successful(kw)} for kw in self.settings["keywords"]]
            gaps = gaps or any(q["collected"] < q["target"] for q in quotas)
            status = "completed_with_gaps" if gaps else "completed"
            await self.store.state("worker", {"status": status, "summary": summary, "quotas": quotas})
            self.progress(status, summary=summary, quotas=quotas)
            return 0
        except WorkerBusy:
            # Do not overwrite the active worker's status or run configuration.
            print(json.dumps({"platform": "weixin", "error_code": "worker_already_running"}), flush=True)
            return 2
        except Exception as error:
            if self.initialized:
                self.keep_verification_page = isinstance(error, (AccessRestricted, ParseError))
                requires_action = isinstance(error, (AccessRestricted, ParseError))
                code = "access_restricted" if isinstance(error, AccessRestricted) else ("parse_error" if isinstance(error, ParseError) else type(error).__name__)
                self.progress("blocked" if requires_action else "failed", error_code=code, requires_user_action=requires_action)
                with suppress(Exception):
                    await self.store.state("worker", {"status": "blocked" if requires_action else "failed", "error_code": code, "requires_user_action": requires_action})
            else:
                print(json.dumps({"platform": "weixin", "error_code": type(error).__name__}), flush=True)
            return 2
        finally:
            if self.page:
                with suppress(Exception):
                    if self.keep_verification_page and self.page.driver:
                        # Leave only our verification tab available for manual resolution.
                        await self.page.driver.stop()
                    else:
                        await self.page.close()
            if self.store:
                if self.lock_connection:
                    if self.owns_lock:
                        with suppress(Exception):
                            await self.lock_connection.execute("SELECT pg_advisory_unlock(hashtext('acquisition:weixin'))")
                    await self.store.pool.release(self.lock_connection)
                await self.store.close()


def parser():
    result = argparse.ArgumentParser(description="微信公众号文章采集，独立于当前四平台任务")
    result.add_argument("--platform", choices=["weixin"], default="weixin")
    result.add_argument("--content-types", choices=["articles"], default="articles")
    result.add_argument("--run-id", default="weixin_" + datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"))
    result.add_argument("--output", type=Path, help="微信任务独立输出目录")
    result.add_argument("--url", action="append", default=[])
    result.add_argument("--keyword", action="append", default=[])
    result.add_argument("--keywords-default", action="store_true", help="使用项目现有的27个关键词")
    result.add_argument("--limit", type=int, default=100, help="每个关键词的正文成功数量目标")
    result.add_argument("--max-pages", type=int, default=100)
    result.add_argument("--no-media", action="store_true")
    result.add_argument("--resume", action="store_true", help="读取已保存的任务配置，显式恢复暂停任务")
    result.add_argument("--background", action="store_true")
    result.add_argument("--monitor", action="store_true", help="独立监控微信工作进程；遇验证或解析变化停止")
    result.add_argument("--status", action="store_true")
    return result


def main():
    p = parser()
    args = p.parse_args()
    if not re.fullmatch(r"weixin_[A-Za-z0-9_-]{1,100}", args.run_id):
        p.error("run-id 必须以 weixin_ 开头，仅使用字母、数字、下划线或连字符")
    args.output = (args.output or ROOT / "data" / "weixin" / args.run_id).resolve()
    if (args.output / "processes.json").exists():
        p.error("该目录属于已有采集任务，请使用独立的微信目录")
    if (args.output / "status.json").exists():
        if json.loads((args.output / "status.json").read_text()).get("platform") != "weixin":
            p.error("该目录包含其他平台状态，请使用独立的微信目录")
    if args.status:
        path = args.output / "status.json"
        print(path.read_text() if path.exists() else '{"platform":"weixin","status":"not_started"}')
        return
    if args.resume:
        path = args.output / "run.json"
        if not path.exists():
            p.error("找不到微信任务配置，无法恢复")
        settings = json.loads(path.read_text())
        if settings.get("run_id") != args.run_id:
            p.error("目录中的任务与 run-id 不一致")
    else:
        if args.limit < 1 or args.max_pages < 1:
            p.error("limit 和 max-pages 必须是正整数")
        try:
            urls = list(dict.fromkeys(article_url(u) for u in args.url))
        except ValueError:
            p.error("文章链接须为有效的 mp.weixin.qq.com/s 链接")
        keywords = list(dict.fromkeys([k.strip() for k in args.keyword if k.strip()] + (KEYWORDS if args.keywords_default else [])))
        if not urls and not keywords:
            p.error("请指定 --url、--keyword 或 --keywords-default")
        settings = {"run_id": args.run_id, "platforms": ["weixin"], "content_types": ["articles"],
                    "urls": urls, "keywords": keywords, "limit": args.limit, "max_pages": args.max_pages,
                    "media": not args.no_media, "browser": "chrome_cdp", "request_interval_minimum": 30}
    args.output.mkdir(parents=True, exist_ok=True)
    if args.background:
        command = [sys.executable, "-m", "media_platform.weixin"] + [value for value in sys.argv[1:] if value != "--background"]
        with (args.output / "worker.log").open("a") as stream:
            process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        print(json.dumps({"platform": "weixin", "run_id": args.run_id, "pid": process.pid, "output": str(args.output)}, ensure_ascii=False))
        return
    if args.monitor:
        from media_platform.weixin.monitor import monitor
        raise SystemExit(monitor(args))
    raise SystemExit(asyncio.run(WeixinJob(args, settings).run()))


if __name__ == "__main__":
    main()
