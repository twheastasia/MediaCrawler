"""Official OpenCLI people commands on one worker-owned Chrome CDP page."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from collection.chrome import ChromePage
from collection.runtime import RequestGate,AccessRestricted,LoginRequired
from collection.opencli_adapter import OpenCliAdapter,AdapterChanged


class PeopleAdapter(OpenCliAdapter):
    page_no=0
    keyword=''

    async def navigate(self,url):
        if self.site!='linkedin':
            return await super().navigate(url)
        target=urlsplit(url)
        if target.scheme!='https' or target.hostname!='www.linkedin.com':
            raise AdapterChanged('unexpected_people_navigation')
        page=self.chrome.page
        current=urlsplit(page.url)
        if current.path!=target.path or current.query!=target.query:
            await self.chrome.gate.action('opencli_linkedin_search',lambda:page.goto(url,wait_until='domcontentloaded',timeout=30000))
            # LinkedIn sometimes settles from /check/ to the normal page itself.
            try:
                await page.wait_for_function("() => document.body && document.body.innerText.trim().length>80 && !location.pathname.startsWith('/check/')",timeout=15000)
            except Exception:pass
        await self.guard()
        if not self.page_no:return
        number=self.page_no+1
        selector=await page.evaluate('''n=>{
          const labels=[`第 ${n} 页`,`Page ${n}`];
          const b=[...document.querySelectorAll('button')].find(b=>!b.disabled&&labels.includes(b.getAttribute('aria-label')));
          return b?'button[aria-label='+JSON.stringify(b.getAttribute('aria-label'))+']':null;
        }''',number)
        if not selector:raise AdapterChanged('PAGINATION_NOT_EXPOSED')
        before=await page.evaluate("() => [...document.querySelectorAll('a[href*=\"/in/\"]')].map(a=>a.href).join('|')")
        async def advance():
            await page.click(selector)
            await page.wait_for_function('''({number,before})=>
              Number(new URL(location.href).searchParams.get('page')||1)===number&&
              [...document.querySelectorAll('a[href*="/in/"]')].map(a=>a.href).join('|')!==before''',
              arg={'number':number,'before':before},timeout=15000)
        await self.chrome.gate.action('opencli_linkedin_people_page',advance)
        await self.guard()


class ChromePeople:
    def __init__(self,platform,output):
        self.platform=platform;self.directory=Path(output)/platform
        self.directory.mkdir(parents=True,exist_ok=True)
        self.chrome=ChromePage(RequestGate(self.directory/'requests_chrome.jsonl',15,0))
        self.adapter=PeopleAdapter(platform,self.chrome)
        self.chrome.response_observer=self.adapter.response
        self.connected=False;self.login_attempted=False;self.login_submitted=False

    async def login(self):
        if self.platform!='linkedin' or self.login_attempted:
            raise LoginRequired('AUTH_REQUIRED')
        self.login_attempted=True
        from dotenv import dotenv_values
        env=dotenv_values(Path(__file__).resolve().parents[1]/'.env')
        username,password=env.get('LINKEDIN_USERNAME'),env.get('LINKEDIN_PASSWORD')
        if not username or not password:raise LoginRequired('LOGIN_CREDENTIALS_MISSING')
        p=self.chrome.page
        await self.chrome.gate.action('linkedin_login_navigation',lambda:p.goto('https://www.linkedin.com/login',wait_until='domcontentloaded'))
        try:
            await p.wait_for_function('''()=>!!document.querySelector('input[name=session_password],iframe[src*=captcha],iframe[src*=challenge]')||
              (!/\\/(login|checkpoint|authwall|check)(\\/|$)/.test(location.pathname)&&document.body?.innerText.trim().length>80)''',timeout=20000)
        except Exception:pass
        state=await p.evaluate('''() => ({captcha:!!document.querySelector('iframe[src*=captcha],iframe[src*=challenge],.geetest_panel'),
          inputs:!!document.querySelector('input[name=session_key]')&&!!document.querySelector('input[name=session_password]')})''')
        if state['captcha']:raise AccessRestricted('ACCESS_RESTRICTED')
        if not state['inputs']:
            await self.adapter.guard()
            return
        async def submit():
            await p.fill('input[name=session_key]',username)
            await p.fill('input[name=session_password]',password)
            self.login_submitted=True
            await p.click('button[type=submit]')
            try:
                await p.wait_for_function("() => !document.querySelector('input[name=session_password]')&&!location.pathname.startsWith('/check/')",timeout=20000)
            except Exception:pass
        await self.chrome.gate.action('linkedin_login_once',submit)
        self.adapter.barriers.clear()
        await self.adapter.guard()

    async def search(self,keyword,page_no):
        try:
            if not self.connected:
                await self.chrome.connect('about:blank');self.connected=True
            self.adapter.page_no=page_no;self.adapter.keyword=keyword
            command='search-talents' if self.platform=='maimai' else 'people-search'
            args={'query':keyword,'page':page_no,'size':20} if self.platform=='maimai' else {'keywords':keyword,'limit':10}
            try:rows=await self.adapter.invoke(command,args)
            except LoginRequired:
                await self.login()
                rows=await self.adapter.invoke(command,args)
            return {'ok':True,'rows':rows,'rawRecords':self.adapter.captures or rows,
                    'command':self.platform+'/'+command,'browser':'chrome_cdp'}
        except Exception as e:
            reason='ACCESS_RESTRICTED' if isinstance(e,AccessRestricted) else 'AUTH_REQUIRED' if isinstance(e,LoginRequired) else 'ADAPTER_ERROR'
            if self.adapter.barriers:reason='ACCESS_RESTRICTED'
            if isinstance(e,AdapterChanged) and str(e)=='PAGINATION_NOT_EXPOSED':reason='PAGINATION_NOT_EXPOSED'
            current_path=urlsplit(self.chrome.page.url).path if self.chrome.page else None
            return {'ok':False,'errorCode':reason,'errorType':type(e).__name__,
                    'network_metadata':self.adapter.barriers[-5:], 'page_path':current_path,
                    'login_attempted':self.login_attempted,'login_submitted':self.login_submitted,'requiresUserAction':True}

    async def close(self):
        await self.adapter.close()
        await self.chrome.close()
