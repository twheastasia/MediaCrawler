"""WeChat contracts: public extraction, recovery, transactionality and isolation."""
import asyncio
import json
import os
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from collection.runtime import AccessRestricted, RequestGate
from media_platform.weixin.core import WeixinJob, media_allowed
from media_platform.weixin.extractor import (
    ArticleUnavailable, ParseError, article_url, digest, parse_article, parse_search,
)


def snapshot(**changes):
    return {
        "url": "https://mp.weixin.qq.com/s/example", "title": "公开文章",
        "account_name": "公众号原名", "byline": "署名作者", "biz": "account-a",
        "mid": "123", "idx": "1", "publish_text": "2026-10-15", "timestamp": "1792022400",
        "content_html": '<h2>小标题</h2><p>原文<strong>内容</strong></p><pre><code>x = 1\ny = 2</code></pre>'
                        '<img data-src="https://mmbiz.qpic.cn/a?wx_fmt=png" alt="原图">',
        **changes,
    }


def test_article_preserves_public_content_and_distinguishes_account_from_byline():
    item = parse_article(snapshot())
    assert item["author_id"] == "account-a"
    assert item["raw"]["metadata_raw"]["account_name"] == "公众号原名"
    assert item["raw"]["metadata_raw"]["byline"] == "署名作者"
    assert "<strong>内容</strong>" in item["raw"]["article_html"]
    assert "## 小标题" in item["raw"]["article_markdown"]
    assert "x = 1\ny = 2" in item["raw"]["article_markdown"]
    assert item["raw"]["media_refs"][0]["url"] == "https://mmbiz.qpic.cn/a?wx_fmt=png"


def test_source_urls_and_html_never_persist_session_material():
    url = article_url('“http://mp.weixin.qq.com/s/example?pass_ticket=SECRET&uin=SECRET&key=SECRET”')
    assert url == "https://mp.weixin.qq.com/s/example"
    markup = '<p onclick="SECRET">原文</p><script>document.cookie="SECRET"</script><img src="https://mmbiz.qpic.cn/a?session_id=SECRET&wx_fmt=png">'
    item = parse_article(snapshot(content_html=markup))
    assert "SECRET" not in json.dumps(item)
    assert item["raw"]["media_refs"][0]["url"].endswith("wx_fmt=png")


def test_article_identity_does_not_depend_on_title_or_url_tracking():
    a = parse_article(snapshot())
    b = parse_article(snapshot(title="改标题", url="https://mp.weixin.qq.com/s/other"))
    assert a["id"] == b["id"]
    assert parse_article(snapshot(biz="other-account"))["id"] != a["id"]
    unknown = parse_article(snapshot(biz="", mid="", idx="", timestamp=""))
    assert unknown["author_id"] is None and unknown["time"] is None
    assert unknown["raw"]["time_precision"] == "day"


def test_sogou_signed_article_urls_without_biz_are_valid_public_articles():
    url = "https://mp.weixin.qq.com/s?src=11&timestamp=1792022400&ver=1&signature=public-signature&new=1"
    assert "signature=public-signature" in article_url(url)
    assert parse_article(snapshot(url=url))["type"] == "article"


@pytest.mark.parametrize("url", ["https://evil.example/s/a", "https://mp.weixin.qq.com.evil.example/s/a", "https://mp.weixin.qq.com:8443/s/a", "file:///tmp/a", "https://mp.weixin.qq.com/s?mid=1"])
def test_url_input_only_accepts_valid_wechat_articles(url):
    with pytest.raises(ValueError):
        article_url(url)


def test_verification_and_deleted_pages_cannot_be_counted_as_articles():
    with pytest.raises(AccessRestricted):
        parse_article(snapshot(page_text="环境异常，完成验证后即可继续访问"))
    with pytest.raises(ArticleUnavailable):
        parse_article(snapshot(page_text="该内容已被发布者删除"))
    with pytest.raises(ArticleUnavailable):
        parse_article(snapshot(page_text="The content has been deleted by the author."))
    with pytest.raises(AccessRestricted):
        parse_article(snapshot(page_text="Complete security verification before continuing."))
    with pytest.raises(ParseError):
        parse_article(snapshot(content_html=""))
    with pytest.raises(AccessRestricted):
        parse_search({"page_text": "请输入验证码"})
    with pytest.raises(ParseError):
        parse_search({"cards": []})
    assert parse_search({"cards": [], "empty": True}) == []


def test_search_retains_evidence_and_redirect_link_without_fetching_it():
    rows = parse_search({"cards": [{"title": "文章", "url": "/link?url=public-reference", "summary": "摘要", "html": "<p>原始卡片</p>"}]})
    assert rows[0]["url"] == "https://weixin.sogou.com/link?url=public-reference"
    assert rows[0]["card_html"] == "<p>原始卡片</p>"
    assert media_allowed("https://mmbiz.qpic.cn/a")
    assert not media_allowed("https://mmbiz.qpic.cn.evil.example/a")
    assert not media_allowed("https://127.0.0.1/a")


def test_monitor_only_restarts_non_barrier_failures():
    from media_platform.weixin.monitor import action
    assert action({"status": "blocked", "requires_user_action": True}, 0) == "user_attention"
    assert action({"status": "completed_with_gaps"}, 0) == "finished"
    assert action({"status": "failed"}, 0) == "restart"
    assert action({"status": "failed"}, 3) == "restart_limit"


def test_browser_extractor_reads_numeric_public_identity_without_returning_scripts():
    import shutil
    import subprocess
    from media_platform.weixin.extractor import ARTICLE_SNAPSHOT
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the browser JavaScript in isolation")
    fixture = r'''
      const content={innerHTML:'<p>原文</p>',querySelectorAll:()=>[]};
      global.location={href:'https://mp.weixin.qq.com/s/example'};
      global.document={
        scripts:[{textContent:'var biz="account-a"; var mid=123; var idx=2; var create_time=1792022400; var cookie="PRIVATE-SCRIPT";'}],
        body:{innerText:'原文'},
        querySelector:(s)=>s==='#js_content'?content:(s==='#activity-name'?{textContent:'标题'}:null)
      };
      process.stdout.write(JSON.stringify(eval('('+process.argv[1]+')')()));
    '''
    result = subprocess.run([node, "-e", fixture, ARTICLE_SNAPSHOT], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    assert value["biz"] == "account-a" and value["mid"] == "123" and value["idx"] == "2"
    assert value["timestamp"] == "1792022400"
    assert "PRIVATE-SCRIPT" not in result.stdout


@pytest.mark.asyncio
async def test_verification_pauses_after_one_attempt_without_retry(tmp_path):
    job = WeixinJob(Namespace(output=tmp_path, run_id="weixin_test"), {})
    events = []
    class Store:
        async def start(self, key): events.append("start")
        async def finish(self, key, status, error): events.append(status)
    job.store = Store()
    job.progress = lambda *a, **k: None
    async def operation():
        raise AccessRestricted("verification_required")
    with pytest.raises(AccessRestricted):
        await job.attempt({"task_key": "a", "kind": "article", "attempts": 0}, operation)
    assert events == ["start", "blocked"]


@pytest.mark.asyncio
async def test_retry_spacing_includes_failed_request(tmp_path):
    import httpx
    from collection.runtime import GatedTransport
    calls = []
    gate = RequestGate(tmp_path / "requests.jsonl", minimum=.02, jitter=0)
    async def serve(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("SECRET")
        return httpx.Response(200, content=b"ok")
    async with httpx.AsyncClient(transport=GatedTransport(gate, httpx.MockTransport(serve))) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://mmbiz.qpic.cn/a?token=SECRET")
        await client.get("https://mmbiz.qpic.cn/b")
    log = (tmp_path / "requests.jsonl").read_text()
    starts = [r for r in map(json.loads, log.splitlines()) if r["event"] == "start"]
    assert starts[1]["gap_seconds"] >= .02
    assert "SECRET" not in log


@pytest.mark.asyncio
async def test_worker_busy_never_creates_a_browser_or_overwrites_status(tmp_path, monkeypatch):
    import media_platform.weixin.core as module
    status = tmp_path / "status.json"
    status.write_text('{"status":"running","pid":123}')
    class Connection:
        async def fetchval(self, *a): return False
    class Pool:
        async def acquire(self): return Connection()
        async def release(self, c): pass
    class Store:
        pool = Pool()
        async def close(self): pass
    async def open_store(*a): return Store()
    monkeypatch.setattr(module.WeixinStore, "open", open_store)
    monkeypatch.setattr(module, "ChromePage", lambda *a: pytest.fail("browser must not be touched"))
    result = await WeixinJob(Namespace(output=tmp_path, run_id="weixin_test"), {}).run()
    assert result == 2 and json.loads(status.read_text())["pid"] == 123


@pytest.mark.asyncio
async def test_image_download_paces_redirects_and_records_checksum(tmp_path, monkeypatch):
    import hashlib
    import httpx
    import media_platform.weixin.core as module
    from collection.runtime import GatedTransport
    results = []
    image = b"\x89PNG\r\n\x1a\nimage-fixture"
    async def serve(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://mmbiz.qpic.cn/image"}, stream=httpx.ByteStream(b""))
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(image))
    monkeypatch.setattr(module, "GatedTransport", lambda gate: GatedTransport(gate, httpx.MockTransport(serve)))
    job = WeixinJob(Namespace(output=tmp_path, run_id="weixin_test"), {})
    job.gate = RequestGate(tmp_path / "requests.jsonl", minimum=.02, jitter=0)
    class Store:
        async def media_result(self, task, status, path=None, checksum=None, error=None):
            results.append((status, path, checksum, error))
    job.store = Store()
    await job.download({"task_key": "m", "payload": {"url": "https://mmbiz.qpic.cn/redirect", "stem": "image", "content_id": "wx_test"}})
    status, path, checksum, error = results[0]
    assert status == "downloaded" and path.read_bytes() == image
    assert checksum == hashlib.sha256(image).hexdigest()
    starts = [r for r in map(json.loads, (tmp_path / "requests.jsonl").read_text().splitlines()) if r["event"] == "start"]
    assert len(starts) == 2 and starts[1]["gap_seconds"] >= .02
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.asyncio
async def test_image_verification_response_never_becomes_a_downloaded_file(tmp_path, monkeypatch):
    import httpx
    import media_platform.weixin.core as module
    from collection.runtime import GatedTransport
    async def serve(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"Complete security verification"))
    monkeypatch.setattr(module, "GatedTransport", lambda gate: GatedTransport(gate, httpx.MockTransport(serve)))
    job = WeixinJob(Namespace(output=tmp_path, run_id="weixin_test"), {})
    job.gate = RequestGate(tmp_path / "requests.jsonl", minimum=.01, jitter=0)
    with pytest.raises(AccessRestricted):
        await job.download({"payload": {"url": "https://mmbiz.qpic.cn/image", "stem": "image", "content_id": "wx_test"}})
    assert not list((tmp_path / "media").rglob("*.*"))


@asynccontextmanager
async def database_store():
    """Disposable schema inside a rolled-back transaction; shared data untouched."""
    if os.getenv("WEIXIN_TEST_DB") != "1":
        pytest.skip("set WEIXIN_TEST_DB=1 for PostgreSQL integration")
    import asyncpg
    import config
    from collection.database import DDL
    from store.weixin._store_impl import WeixinStore
    c = config.postgres_db_config
    connection = await asyncpg.connect(host=c["host"], port=int(c["port"]), user=c["user"], password=c["password"], database=c["db_name"])
    tx = connection.transaction()
    await tx.start()
    try:
        schema = "weixin_test_" + os.urandom(6).hex()
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET LOCAL search_path TO "{schema}"')
        await connection.execute(DDL)
        class TestConnection:
            def __getattr__(self, name): return getattr(connection, name)
            async def fetchval(self, query, *args, **kwargs):
                if query == "SELECT pg_try_advisory_xact_lock(hashtext('acquisition:weixin'))":
                    # Do not compete with the currently running production worker.
                    return await connection.fetchval("SELECT pg_try_advisory_xact_lock(hashtext($1))", "weixin-test:" + schema)
                return await connection.fetchval(query, *args, **kwargs)
        test_connection = TestConnection()
        class Pool:
            @asynccontextmanager
            async def acquire(self): yield test_connection
            def __getattr__(self, name): return getattr(test_connection, name)
        store = WeixinStore(Pool(), "weixin_integration", "weixin")
        await store.initialize({"platforms": ["weixin"]})
        yield store, test_connection
    finally:
        await tx.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_postgres_article_sources_media_and_repeat_keyword_are_atomic():
    async with database_store() as (store, c):
        row = {"url": "https://mp.weixin.qq.com/s/example", "title": "文章", "card_html": "<p>发现证据</p>"}
        await store.enqueue("search-a", "search", {})
        await store.search_results("search-a", "关键词A", 1, [row])
        key = "article:" + digest(row["url"])
        item = await store.persist_article(key, parse_article(snapshot(media_refs=[{"type": "link", "url": "https://example.org/public-reference"}])), [row["url"]])
        assert await store.successful("关键词A") == 1
        assert len(await store.tasks("media")) == 1
        assert await c.fetchval("SELECT count(*) FROM wxarticle_media WHERE status='reference'") == 1
        # A later keyword finding the same URL must gain its own link, without refetching.
        await store.enqueue("search-b", "search", {})
        await store.search_results("search-b", "关键词B", 1, [row])
        assert (await store.task(key))["status"] == "pending"
        await store.persist_article(key, item, [row["url"]])
        assert await store.successful("关键词B") == 1
        assert await c.fetchval("SELECT count(*) FROM wxarticle_content") == 1
        assert await c.fetchval("SELECT count(*) FROM wxarticle_link") == 2
        assert await c.fetchval("SELECT count(*) FROM wxarticle_author") == 1
        # Existing positional writers must remain compatible.
        await c.execute("INSERT INTO acquisition_content(platform,content_type,content_id,raw) VALUES('bili','video','other','{}'::jsonb)")
        await c.execute("INSERT INTO acquisition_media VALUES('bili','video','other','image','https://example.org',NULL,NULL,'failed',NULL,now())")
        assert len(await store.tasks("media")) == 1


@pytest.mark.asyncio
async def test_postgres_mid_commit_failure_rolls_back_content_and_preserves_pending_task():
    async with database_store() as (store, c):
        url = "https://mp.weixin.qq.com/s/example"
        await store.seed(url)
        key = "article:" + digest(url)
        await c.execute("""CREATE FUNCTION reject_source() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'test rollback'; END $$;
            CREATE TRIGGER reject_source BEFORE UPDATE ON wxarticle_source FOR EACH ROW EXECUTE FUNCTION reject_source();""")
        with pytest.raises(Exception, match="test rollback"):
            await store.persist_article(key, parse_article(snapshot()), [url])
        assert await c.fetchval("SELECT count(*) FROM wxarticle_content") == 0
        assert await c.fetchval("SELECT count(*) FROM wxarticle_author") == 0
        assert await c.fetchval("SELECT count(*) FROM wxarticle_media") == 0
        assert (await store.task(key))["status"] == "pending"
        await c.execute("DROP TRIGGER reject_source ON wxarticle_source")
        await store.persist_article(key, parse_article(snapshot()), [url])
        assert (await store.task(key))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_postgres_resume_only_reopens_incomplete_tasks_and_checks_configuration():
    async with database_store() as (store, c):
        for key, status in (("ok", "succeeded"), ("barrier", "blocked"), ("crash", "running"), ("deleted", "unavailable")):
            await store.enqueue(key, "article", {})
            await store.finish(key, status)
        await store.initialize({"platforms": ["weixin"]}, resume=True)
        assert (await store.task("ok"))["status"] == "succeeded"
        assert (await store.task("deleted"))["status"] == "unavailable"
        assert {r["task_key"] for r in await store.tasks("article")} == {"barrier", "crash"}
        with pytest.raises(ValueError, match="run_configuration_mismatch"):
            await store.initialize({"platforms": ["bili"]}, resume=True)


@pytest.mark.asyncio
async def test_postgres_resume_requeues_corrupt_media_without_refetching_article(tmp_path):
    import hashlib
    async with database_store() as (store, c):
        url = "https://mp.weixin.qq.com/s/example"
        await store.seed(url)
        key = "article:" + digest(url)
        await store.persist_article(key, parse_article(snapshot()), [url])
        task = (await store.tasks("media"))[0]
        path = tmp_path / "image.png"
        path.write_bytes(b"image-content")
        await store.media_result(task, "downloaded", path, hashlib.sha256(path.read_bytes()).hexdigest())
        await store.repair_media()
        assert await store.tasks("media") == []
        path.write_bytes(b"corrupt")
        await store.repair_media()
        assert len(await store.tasks("media")) == 1
        assert (await store.task(key))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_postgres_late_short_long_identity_merges_sources_and_media():
    async with database_store() as (store, c):
        short = "https://mp.weixin.qq.com/s/example"
        long = "https://mp.weixin.qq.com/s?__biz=account-a&mid=123&idx=1"
        for url in (short, long):
            await store.seed(url)
        fallback = parse_article(snapshot(biz="", mid="", idx=""))
        await store.persist_article("article:" + digest(short), fallback, [short])
        stable = parse_article(snapshot(url=long))
        await store.persist_article("article:" + digest(long), stable, [long])
        assert await c.fetchval("SELECT count(*) FROM wxarticle_content") == 2
        await store.persist_article("article:" + digest(short), stable, [short, long])
        assert await c.fetchval("SELECT count(*) FROM wxarticle_content") == 1
        assert await c.fetchval("SELECT count(DISTINCT content_id) FROM wxarticle_source") == 1
        assert await store.known_article([short]) == await store.known_article([long]) == stable["id"]
        assert len(await store.tasks("media")) == 1


async def legacy_weixin_fixture(store, connection, tmp_path):
    """Build legacy WeChat plus another platform in the disposable schema."""
    import hashlib
    from types import SimpleNamespace
    from collection.database import Store, encoded
    from store.weixin.migrate import PRIVATE
    run = "weixin_legacy"
    for source, target in PRIVATE.items():
        await connection.execute(f'CREATE TABLE "{source}" (LIKE "{target}" INCLUDING ALL)')
    await connection.execute("INSERT INTO acquisition_run(run_id,config) VALUES($1,$2::jsonb)", run, encoded({"platforms": ["weixin"]}))
    old = Store(connection, run, "weixin")
    item = parse_article(snapshot())
    await old.content(item)
    await old.author(item["author_id"], "公众号原名", None, {"original": "公开资料"})
    await old.link("关键词", "search", item)
    await old.state("worker", {"status": "completed"})
    path = tmp_path / "legacy-image.png"
    path.write_bytes(b"existing-media")
    resource = SimpleNamespace(stem="image", url="https://mmbiz.qpic.cn/image")
    await old.media(item, resource, path)
    await connection.execute("INSERT INTO acquisition_weixin_task VALUES($1,'article:seed','article','{}','succeeded',1,NULL,now())", run)
    await connection.execute("INSERT INTO acquisition_weixin_source VALUES($1,'search:关键词','article:seed',$2,'{}')", run, item["id"])
    await connection.execute("INSERT INTO acquisition_weixin_alias VALUES($1,$2)", digest(item["url"]), item["id"])
    await connection.execute("INSERT INTO acquisition_weixin_media VALUES($1,'image','image',$2,'downloaded',NULL,now())", item["id"], hashlib.sha256(path.read_bytes()).hexdigest())
    await connection.execute("INSERT INTO acquisition_run(run_id,config) VALUES('other_run','{\"platforms\":[\"bili\"]}')")
    other = Store(connection, "other_run", "bili")
    await other.content({"type": "video", "id": "other", "raw": {"original": True}, "body": "其他平台原文"})
    await other.link("另一个关键词", "search", {"type": "video", "id": "other"})
    await other.state("worker", {"status": "running"})
    return run, item, path


@pytest.mark.asyncio
async def test_migration_preserves_content_files_checkpoints_and_other_platform(tmp_path):
    from store.weixin.migrate import migrate, PRIVATE
    from store.weixin._store_impl import WeixinStore
    async with database_store() as (store, c):
        run, item, path = await legacy_weixin_fixture(store, c, tmp_path)
        original = await c.fetchval("SELECT to_jsonb(c) FROM acquisition_content c WHERE platform='bili'")
        report = await migrate(c)
        migrated = WeixinStore(store.pool, run, "weixin")
        await migrated.initialize({"platforms": ["weixin"]}, resume=True)
        assert (await migrated.cached("article", item["id"]))["raw"] == item["raw"]
        assert await migrated.state("worker") == {"status": "completed"}
        assert (await migrated.task("article:seed"))["status"] == "succeeded"
        assert await migrated.successful("关键词") == 1
        assert await c.fetchval("SELECT local_path FROM wxarticle_media WHERE content_id=$1", item["id"]) == str(path)
        await migrated.repair_media()
        assert await migrated.tasks("media") == []
        for name in PRIVATE:
            assert await c.fetchval("SELECT to_regclass($1)", name) is None
        for name in ("content", "author", "link", "media", "progress"):
            assert await c.fetchval(f"SELECT count(*) FROM acquisition_{name} WHERE platform='weixin'") == 0
        assert await c.fetchval("SELECT count(*) FROM acquisition_run WHERE run_id=$1", run) == 0
        assert await c.fetchval("SELECT to_jsonb(c) FROM acquisition_content c WHERE platform='bili'") == original
        assert await c.fetchval("SELECT count(*) FROM acquisition_link WHERE platform='bili'") == 1
        assert await c.fetchval("SELECT count(*) FROM acquisition_progress WHERE platform='bili'") == 1
        assert report["copied_rows"]["wxarticle_content"] == 1
        repeated = await migrate(c)
        assert repeated["copied_rows"].get("wxarticle_content") == 0
        assert await migrated.successful("关键词") == 1


@pytest.mark.asyncio
async def test_migration_dry_run_rolls_back_before_actual_commit(tmp_path):
    from store.weixin.migrate import migrate
    async with database_store() as (store, c):
        run, item, path = await legacy_weixin_fixture(store, c, tmp_path)
        report = await migrate(c, dry_run=True)
        assert report["dry_run"]
        assert await c.fetchval("SELECT count(*) FROM acquisition_content WHERE platform='weixin'") == 1
        assert await c.fetchval("SELECT count(*) FROM wxarticle_content") == 0
        assert await c.fetchval("SELECT to_regclass('acquisition_weixin_task')")
        await migrate(c)
        assert await c.fetchval("SELECT count(*) FROM wxarticle_content") == 1


@pytest.mark.asyncio
async def test_migration_verification_failure_keeps_all_legacy_data(tmp_path):
    from store.weixin.migrate import migrate
    async with database_store() as (store, c):
        run, item, path = await legacy_weixin_fixture(store, c, tmp_path)
        await store.content({**item, "body": "conflicting_target"})
        with pytest.raises(RuntimeError, match="row_verification_failed"):
            await migrate(c)
        assert await c.fetchval("SELECT body FROM acquisition_content WHERE platform='weixin'") == item["body"]
        assert await c.fetchval("SELECT body FROM wxarticle_content") == "conflicting_target"
        assert await c.fetchval("SELECT to_regclass('acquisition_weixin_task')")
        assert await c.fetchval("SELECT count(*) FROM acquisition_media WHERE platform='weixin'") == 1
