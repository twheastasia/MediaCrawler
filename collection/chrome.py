from __future__ import annotations
import asyncio
from playwright.async_api import async_playwright
import config
from urllib.parse import urlsplit
from contextlib import suppress

class ChromePage:
    """One owned tab per worker; never close the shared Chrome browser."""
    def __init__(self,gate):
        self.gate=gate
        self.driver=None
        self.browser=None
        self.context=None
        self.page=None
        self.last_network=[]
    async def connect(self,origin):
        self.driver=await async_playwright().start()
        endpoint=f'http://127.0.0.1:{config.CDP_DEBUG_PORT}'
        self.browser=await self.driver.chromium.connect_over_cdp(endpoint,timeout=15000)
        if not self.browser.contexts:raise RuntimeError('existing Chrome context missing')
        self.context=self.browser.contexts[0]
        self.page=await self.context.new_page()
        if getattr(self,'response_observer',None):
            self.page.on('response',self.response_observer)
        # new_page() is already about:blank: no network request or extra pacing slot.
        if origin != 'about:blank':
            await self.gate.action('chrome_initial_page',lambda:self.page.goto(origin,wait_until='domcontentloaded',timeout=30000))
    async def evaluate(self,expression,argument=None):
        if expression=='() => window.localStorage':
            expression="() => Object.fromEntries(['wbi_img_urls','wbi_img_url','wbi_sub_url','xmst','HasUserLogin'].map(k=>[k,localStorage.getItem(k)]))"
        return await self.page.evaluate(expression,argument)
    async def credentials(self,urls):
        cookies=await self.context.cookies(urls)
        return {c['name']:c['value'] for c in cookies}
    async def navigate(self,url):
        await self.page.goto(url,wait_until='domcontentloaded',timeout=30000)
    async def document(self,url):
        await self.navigate(url)
        return dict(status=200,headers={'content-type':'text/html'},body=await self.page.content())
    async def fetch(self,url,options):
        # Observe only safe metadata from this worker's own tab; never record headers.
        session=await self.context.new_cdp_session(self.page)
        self.last_network=[]
        def received(event):
            response=event.get('response') or {};u=urlsplit(response.get('url',''))
            self.last_network.append({'event':'response','host':u.netloc,'path':u.path,
                'status':response.get('status'),'mime':response.get('mimeType')})
            self.last_network=self.last_network[-10:]
        session.on('Network.responseReceived',received)
        await session.send('Network.enable')
        try:
            return await asyncio.wait_for(self.page.evaluate('''async ({url,options}) => {
          const controller=new AbortController();
          const timer=setTimeout(()=>controller.abort(),options.timeout||30000);
          try {const r=await fetch(url,{...options,signal:controller.signal});
            return {status:r.status,headers:Object.fromEntries(r.headers.entries()),body:await r.text()};
          } finally {clearTimeout(timer);}
        }''',dict(url=url,options=options)),45)
        finally:
            with suppress(Exception):await asyncio.wait_for(session.detach(),5)
    async def visible(self,url):
        async def visit():
            await self.navigate(url)
            return await self.evaluate("""() => ({url:location.href,title:document.title,text:document.body.innerText,
                links:[...document.querySelectorAll('a[href]')].map(a=>({text:a.innerText,url:a.href})),
                images:[...document.querySelectorAll('img[src]')].map(i=>i.src),
                videos:[...document.querySelectorAll('video')].map(v=>v.currentSrc||v.src)})""")
        value=await self.gate.action('browser_page',visit)
        if any(x in value['text'] for x in ('安全验证','访问受限','完成验证后')):
            from collection.runtime import AccessRestricted
            raise AccessRestricted('verification_required')
        return value
    async def close(self):
        try:
            if self.page and not self.page.is_closed():await self.page.close()
        finally:
            if self.driver:await self.driver.stop()
