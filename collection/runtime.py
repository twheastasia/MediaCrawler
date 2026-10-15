from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class RequestGate:
    """One request at a time, including streaming transfers and retries."""
    def __init__(self, log_path: Path, minimum: float = 30, jitter: float = 15):
        self.lock = asyncio.Lock()
        self.minimum = minimum
        self.jitter = jitter
        self.log_path = log_path
        self.due = 0.0
        self.started = 0.0
        self.label = ""
        self.last_finished = None
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                row = json.loads(line)
                if row.get("event") == "end":
                    self.last_finished = row["time"]
            if self.last_finished:
                self.due = time.monotonic() + max(0, self.last_finished + minimum - time.time())

    def log(self, event, **values):
        with self.log_path.open("a") as stream:
            stream.write(json.dumps({"event": event, "time": time.time(), **values}) + "\n")

    async def enter(self, label):
        await self.lock.acquire()
        try:
            delay = self.due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.started = time.monotonic()
            self.label = label
            gap = time.time() - self.last_finished if self.last_finished else None
            self.log("start", operation=label, gap_seconds=gap)
        except BaseException:
            self.lock.release()
            raise

    def leave(self, status=None):
        self.last_finished = time.time()
        self.due = time.monotonic() + self.minimum + random.uniform(0, self.jitter)
        self.log("end", operation=self.label, status=status,
                 duration_seconds=time.monotonic() - self.started)
        self.lock.release()

    async def action(self, label, function):
        await self.enter(label)
        try:
            return await function()
        finally:
            self.leave()


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, inner, gate, status):
        self.inner, self.gate, self.status = inner, gate, status
        self.closed = False

    async def __aiter__(self):
        async for data in self.inner:
            yield data

    async def aclose(self):
        if not self.closed:
            self.closed = True
            try:
                await self.inner.aclose()
            finally:
                self.gate.leave(self.status)


class AccessRestricted(RuntimeError):
    pass


class LoginRequired(RuntimeError):
    pass


class GatedTransport(httpx.AsyncBaseTransport):
    def __init__(self, gate, inner=None, blocked_paths=None):
        self.gate = gate
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)
        self.blocked_paths = blocked_paths if blocked_paths is not None else set()

    async def handle_async_request(self, request):
        u = urlsplit(str(request.url))
        if (u.netloc, u.path) in self.blocked_paths:
            raise AccessRestricted("restricted endpoint paused")
        await self.gate.enter(f"{request.method} {u.netloc}{u.path}")
        try:
            response = await self.inner.handle_async_request(request)
            response.stream = GatedStream(response.stream, self.gate, response.status_code)
            return response
        except BaseException:
            self.gate.leave("transport_error")
            raise

    async def aclose(self):
        await self.inner.aclose()


class ChromeSessionTransport(httpx.AsyncBaseTransport):
    """MediaCrawler API requests using the CDP context's shared cookie session."""
    def __init__(self,page,gate,blocked_paths):
        self.page,self.gate,self.blocked_paths=page,gate,blocked_paths

    async def handle_async_request(self,request):
        u=urlsplit(str(request.url))
        if (u.netloc,u.path) in self.blocked_paths:
            raise AccessRestricted('restricted endpoint paused')
        async def send():
            response=await self.page.context.request.fetch(str(request.url),method=request.method,
                headers=dict(request.headers),
                data=request.content if request.method not in ('GET','HEAD') else None,timeout=30000)
            try:
                body=await response.body()
                headers={k:v for k,v in response.headers.items()
                    if k.lower() not in ('content-encoding','content-length')}
                return httpx.Response(response.status,headers=headers,stream=httpx.ByteStream(body))
            finally:await response.dispose()
        return await self.gate.action(request.method+' '+u.netloc+u.path,send)


class BrowserTransport(httpx.AsyncBaseTransport):
    """Use Chrome's existing network session; no Cookie is passed to page scripts."""
    def __init__(self, page, gate, blocked_paths):
        self.page, self.gate, self.blocked_paths = page, gate, blocked_paths

    async def handle_async_request(self, request):
        u=urlsplit(str(request.url))
        if (u.netloc,u.path) in self.blocked_paths:
            raise AccessRestricted('restricted endpoint paused')
        is_html=bool(re.match(r'^/(?:question/\d+/answer/\d+|p/\d+|zvideo/\d+|people/[^/]+|user/profile/[^/]+)',u.path))
        if is_html:
            async def document():
                return await self.page.document(str(request.url))
            result=await self.gate.action('browser_document '+u.path,document)
        else:
            origin=u.scheme+'://'+u.netloc
            if u.netloc=='edith.xiaohongshu.com':origin='https://www.xiaohongshu.com'
            current=await self.page.evaluate('() => location.origin')
            if current!=origin:
                async def navigate():
                    return await self.page.navigate(origin)
                await self.gate.action('browser_origin',navigate)
            forbidden={'cookie','user-agent','host','accept-encoding','connection','content-length','referer','origin'}
            headers={k:v for k,v in request.headers.items() if k.lower() not in forbidden}
            options={'method':request.method,'headers':headers,'timeout':30000}
            if u.netloc=='edith.xiaohongshu.com':options['credentials']='include'
            if request.method not in ('GET','HEAD'):options['body']=request.content.decode()
            async def fetch():
                return await self.page.fetch(str(request.url),options)
            result=await self.gate.action(request.method+' '+u.netloc+u.path,fetch)
        headers={k:v for k,v in (result.get('headers') or {}).items()
                 if k.lower() not in ('content-encoding','content-length')}
        body=result.get('body','')
        if not isinstance(body,str):body=json.dumps(body,ensure_ascii=False)
        return httpx.Response(result['status'],headers=headers,stream=httpx.ByteStream(body.encode()))
