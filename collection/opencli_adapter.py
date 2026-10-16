"""Reviewed OpenCLI read adapters; all browser I/O stays in our owned CDP page."""
import asyncio
import json
import os
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from collection.runtime import AccessRestricted, LoginRequired

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'data/opencli_runtime/node_modules/@jackwener/opencli'
ALLOWED = {'maimai': {'search-talents'}, 'linkedin': {'search', 'people-search', 'posts', 'search-posts'}}


class AdapterChanged(RuntimeError):
    requires_user_action = True


def check_page(site, value, barriers):
    if barriers or value.get('challenge'):
        raise AccessRestricted(site + '_access_restricted')
    if value.get('login_form') or re.search(r'/(login|checkpoint|authwall|uas|check)(?:/|$)', value.get('path', '') + '/'):
        raise LoginRequired(site + '_login_required')


GUARD = '''() => {const t=document.body?.innerText||'';const visible=e=>e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
return {path:location.pathname,login_form:[...document.querySelectorAll('input[type=password],input[name=session_key]')].some(visible),
challenge:/security verification|verify your identity|安全验证|人机验证|访问受限|异常访问|完成验证后|图形验证码|captcha/i.test(t)}}'''


class OpenCliAdapter:
    def __init__(self, site, chrome):
        self.site, self.chrome = site, chrome
        self.barriers, self.captures = [], []
        self.process = None

    def response(self, response):
        u = urlsplit(response.url)
        domain = self.site + ('.cn' if self.site == 'maimai' else '.com')
        if u.hostname == domain or (u.hostname or '').endswith('.' + domain):
            if response.request.resource_type in ('document', 'fetch', 'xhr') and response.status in (401, 403, 406, 412, 429, 461, 471, 999):
                self.barriers.append({'host': u.hostname, 'path': u.path, 'status': response.status})

    async def guard(self):
        check_page(self.site, await self.chrome.page.evaluate(GUARD), self.barriers)

    async def navigate(self, url):
        u = urlsplit(url)
        domain = 'maimai.cn' if self.site == 'maimai' else 'linkedin.com'
        if u.scheme != 'https' or not (u.hostname == domain or (u.hostname or '').endswith('.' + domain)):
            raise AdapterChanged('adapter_navigation_domain_changed')
        await self.chrome.gate.action('opencli_navigation', lambda: self.chrome.page.goto(url, wait_until='domcontentloaded', timeout=30000))
        await asyncio.sleep(2)
        await self.guard()

    async def dispatch(self, method, args):
        if method == 'goto':
            await self.navigate(args[0]); return None
        await self.guard()
        if method == 'getCookies':
            domain = 'https://maimai.cn' if self.site == 'maimai' else 'https://www.linkedin.com'
            return await self.chrome.context.cookies([domain])  # IPC in RAM only.
        if method in ('wait', 'waitForTimeout'):
            await asyncio.sleep(2); return None  # Fixed render wait; no random jitter.
        if method == 'autoScroll':
            times = int((args[0] or {}).get('times', 1))
            for _ in range(min(times, 40)):
                await self.chrome.gate.action('opencli_scroll', lambda: self.chrome.page.evaluate('() => window.scrollBy(0,innerHeight)'))
                await asyncio.sleep(2)
                await self.guard()
            return None
        if method != 'evaluate':
            raise AdapterChanged('adapter_browser_method_changed')
        script = args[0]
        # Upstream posts expands 20 broad controls inside one script. Forbid it;
        # collection details expand exactly one known text control per gate.
        unsafe = 'for (const expander of expanders.slice(0, 20)) {\n      try { expander.click(); } catch {}\n    }'
        script = script.replace(unsafe, '/* read only */')
        if re.search(r'\.click\s*\(|\.submit\s*\(|\.dispatchEvent\s*\(', script):
            raise AdapterChanged('adapter_uncontrolled_interaction')
        if re.search(r'\bfetch\s*\(|\bXMLHttpRequest\b|\.scroll(?:By|To)\s*\(', script):
            value = await self.chrome.gate.action('opencli_evaluate', lambda: self.chrome.page.evaluate(script))
        else:
            value = await self.chrome.page.evaluate(script)
        await self.guard()
        # Preserve API source records before upstream presentation mapping drops IDs.
        if self.site == 'maimai' and isinstance(value, dict):
            data = value.get('data') if isinstance(value.get('data'), dict) else value
            records = data.get('list', data.get('talent_list'))
            if isinstance(records, list):
                self.captures.extend(records)
        if isinstance(value, dict) and (value.get('securityBlock') or value.get('authRequired')):
            raise AccessRestricted(self.site + '_api_rejected')
        return value

    async def invoke(self, command, arguments):
        if command not in ALLOWED.get(self.site, set()):
            raise AdapterChanged('unsupported_opencli_command')
        if not (PACKAGE / 'dist/src/registry-api.js').exists():
            raise AdapterChanged('opencli_not_installed')
        self.captures = []
        env = {k: v for k, v in os.environ.items() if not k.startswith(('OPENCLI_', 'DEBUG_'))}
        self.process = await asyncio.create_subprocess_exec('node', str(Path(__file__).with_name('opencli_bridge.mjs')),
            str(PACKAGE), self.site, command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, limit=8*1024*1024)
        try:
            self.process.stdin.write((json.dumps({'args': arguments}) + '\n').encode())
            await self.process.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), 180)
                if not line:
                    raise AdapterChanged('opencli_protocol_closed')
                value = json.loads(line)
                if value.get('done'):
                    if value.get('error'):
                        code = value['error'].get('code')
                        if code == 'AUTH_REQUIRED': raise LoginRequired(self.site + '_login_required')
                        if code == 'SECURITY_BLOCK': raise AccessRestricted(self.site + '_access_restricted')
                        # EmptyResultError is ambiguous (auth/markup/empty), never
                        # convert it into successful zero results.
                        raise AdapterChanged('opencli_' + str(code))
                    return value['result']
                if 'rpc' not in value: raise AdapterChanged('opencli_protocol_changed')
                result = await self.dispatch(value['rpc'], value.get('args', []))
                self.process.stdin.write((json.dumps({'reply': value['id'], 'result': result}) + '\n').encode())
                await self.process.stdin.drain()
        finally:
            await self.close()

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                with suppress(ProcessLookupError):self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait()
            self.process = None
