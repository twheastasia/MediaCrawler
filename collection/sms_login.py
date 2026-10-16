"""SMS form assistance restricted to this worker's own Chrome CDP page."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit
from collection.runtime import AccessRestricted,LoginRequired
from collection.sms import Settings,Inbox,SmsError,phone_lock,identity

HOSTS={
 'bili':('bilibili.com',), 'dy':('douyin.com',), 'xhs':('xiaohongshu.com',),
 'zhihu':('zhihu.com',), 'ks':('kuaishou.com',),
 'wb':('weibo.cn','weibo.com','sina.com.cn'), 'tieba':('baidu.com',),
 'weixin':('mp.weixin.qq.com','weixin.sogou.com','account.sogou.com'),
}
MARKERS={'bili':('SESSDATA',),'dy':('sessionid','sessionid_ss','sid_guard'),
 'xhs':('web_session',),'zhihu':('z_c0',),'ks':('passToken','kuaishou.server.web_st','userId'),
 'wb':('SUB',),'tieba':('BDUSS','STOKEN'),'weixin':()}

# No input values except the phone field, no page HTML or arbitrary globals.
# Markers are placed only on elements on the worker-owned page/frame.
PROBE=r'''() => {
 const visible=e=>!!e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
 const captcha=[...document.querySelectorAll('#captcha-verify-image,.geetest_panel,.geetest_window,.captcha_verify_container,.yidun_panel,[class*="slider-verify"],iframe[src*="captcha"],iframe[src*="geetest"],iframe[src*="challenges.cloudflare.com"],img[id*="captcha"],img[src*="captcha"],img[id*="verifyCode"],img[src*="verifycode"],[class*="captcha"] img')].some(visible);
 const inputs=[...document.querySelectorAll('input')].filter(visible);
 const code=inputs.find(e=>e.autocomplete==='one-time-code'||/验证码|动态码|短信码|sms.?code|verify.?code|verification.?code/i.test([e.placeholder,e.name,e.id].join(' ')));
 const root=code?.closest('form,[role="dialog"],.login-container,.SignFlow,.web-login-mobile-code,.bili-mini-login,.login-panel,.login-box,.passport-login-container')||code?.parentElement?.parentElement;
 if(!code || !root) return {offered:false,captcha};
 const controls=[...root.querySelectorAll('button,a,span,[role="button"]')].filter(visible);
 const send=controls.find(e=>/^(?:获取验证码|发送验证码|获取短信验证码|发送短信验证码|发送短信|重新发送|重新获取|获取动态码)$/.test(e.textContent.trim()));
 const submit=controls.find(e=>/^(?:登录|登录\/注册|登录或注册|注册\/登录|验证|确定|确认|提交|下一步|验证并登录|立即登录|安全验证)$/.test(e.textContent.trim()) && e!==send);
 const phone=[...root.querySelectorAll('input')].filter(visible).find(e=>e!==code && /手机号|手机号码|phone|mobile|tel/i.test([e.placeholder,e.name,e.id,e.type].join(' ')));
 const masked=(root.innerText||'').match(/1\d{2}[*•xX·]{4,8}\d{2,4}/)?.[0]||'';
 if(!submit || (!phone && !masked)) return {offered:false,captcha};
 for(const key of ['phone','code','send','submit']) document.querySelectorAll('[data-mc-sms-'+key+']').forEach(e=>e.removeAttribute('data-mc-sms-'+key));
 for(const [key,e] of Object.entries({phone,code,send,submit})) if(e)e.setAttribute('data-mc-sms-'+key,'1');
 return {offered:true,captcha,phone_value:phone?.value||'',phone_input:!!phone,masked_phone:masked,has_send:!!send,
   send_disabled:!!send?.disabled||send?.getAttribute('aria-disabled')==='true',
   submit_disabled:!!submit.disabled||submit.getAttribute('aria-disabled')==='true'};
}'''
OPEN_ACTION=r'''kind => {
 const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
 const names=kind==='sms'?/^(?:手机号登录|手机登录|短信登录|验证码登录|手机验证码登录|免密登录)$/:/^(?:登录|登录\/注册|登录或注册|立即登录|请登录)$/;
 const nodes=[...document.querySelectorAll('button,a,span,[role="button"],li')].filter(e=>visible(e)&&names.test(e.textContent.trim())&&e.getAttribute('target')!=='_blank');
 nodes.sort((a,b)=>a.querySelectorAll('*').length-b.querySelectorAll('*').length);
 const node=nodes[0];if(!node)return false;
 document.querySelectorAll('[data-mc-sms-open]').forEach(e=>e.removeAttribute('data-mc-sms-open'));
 node.setAttribute('data-mc-sms-open','1');return true;
}'''
SUCCESS=r'''() => ({
 explicit:/验证成功|登录成功|验证已通过/.test(document.body?.innerText||''),
 article:!!document.querySelector('#js_content')?.textContent?.trim(),
 search:!!document.querySelector('.news-list li h3 a')
})'''


def host_allowed(platform,url):
    try:u=urlsplit(url);host=u.hostname or ''
    except ValueError:return False
    return u.scheme=='https' and u.port in (None,443) and any(host==d or host.endswith('.'+d) for d in HOSTS.get(platform,()))


def phone_matches(value,phone):
    value=re.sub(r'[ +()-]','',value or '')
    if not value:return True
    if re.fullmatch(r'(?:86)?1\d{10}',value):return value[-11:]==phone
    m=re.fullmatch(r'(1\d{2})[*•xX·]{4,8}(\d{2,4})',value)
    return bool(m and phone.startswith(m[1]) and phone.endswith(m[2]))


class SmsLogin:
    def __init__(self,owner,platform,gate,settings=None,inbox_factory=Inbox):
        self.owner,self.platform,self.gate=owner,platform,gate
        self.settings=settings if settings is not None else Settings.load()
        self.inbox_factory=inbox_factory
        self.reason='sms_not_offered'

    async def find(self):
        if not self.owner or not getattr(self.owner,'page',None) or self.owner.page.is_closed():return None,None
        for frame in getattr(self.owner.page,'frames',[]):
            if not host_allowed(self.platform,frame.url):continue
            state=await frame.evaluate(PROBE)
            if state.get('captcha'):raise AccessRestricted('sms_requires_manual_captcha')
            if state.get('offered'):return frame,state
        return None,None

    async def open_form(self):
        # Only explicit login/sms tab controls, no raw API navigation or logout.
        for kind in ('login','sms'):
            frame,state=await self.find()
            if frame:return frame,state
            for target in self.owner.page.frames:
                if not host_allowed(self.platform,target.url):continue
                if await target.evaluate(OPEN_ACTION,kind):
                    async def click():
                        await target.locator('[data-mc-sms-open="1"]').click(timeout=5000)
                        await self.owner.page.wait_for_timeout(1000)
                    await self.gate.action('sms_open_'+kind,click)
                    break
        return await self.find()

    async def run(self,open_login=False):
        if not self.settings or not self.settings.enabled:
            self.reason='sms_disabled';return False
        if self.platform not in HOSTS:
            self.reason='sms_platform_unsupported';return False
        if not self.owner or not getattr(self.owner,'page',None) or not host_allowed(self.platform,getattr(self.owner.page,'url','')):
            self.reason='sms_origin_not_allowed';return False
        try:
            frame,state=await (self.open_form() if open_login else self.find())
            if not frame:return False
            if not state.get('has_send') or state.get('send_disabled'):
                self.reason='sms_send_unavailable';return False
            if not phone_matches(state.get('phone_value'),self.settings.phone) or not phone_matches(state.get('masked_phone'),self.settings.phone):
                raise SmsError('sms_form_phone_mismatch')
            async with phone_lock(self.settings):
                inbox=self.inbox_factory(self.settings,self.gate)
                records=await inbox.query();baseline={identity(r) for r in records};records.clear()
                before=await self.owner.credentials(['https://'+d for d in HOSTS[self.platform]])
                requested_at=None
                async def send():
                    nonlocal requested_at
                    if state.get('phone_input'):
                        await frame.locator('[data-mc-sms-phone="1"]').fill(self.settings.phone,timeout=5000)
                    # Never invoke native slider solvers or repeatedly resend.
                    import time
                    requested_at=time.time()
                    await frame.locator('[data-mc-sms-send="1"]').click(timeout=5000)
                    await self.owner.page.wait_for_timeout(1000)
                await self.gate.action('sms_send_once',send)
                async def check():
                    await self.find()
                # Check for a newly displayed non-SMS captcha before every poll.
                code=await inbox.wait(self.platform,requested_at,baseline,before_query=check)
                try:
                    current,new_state=await self.find()
                    if current is not frame or not phone_matches(new_state.get('masked_phone'),self.settings.phone) or not phone_matches(new_state.get('phone_value'),self.settings.phone):
                        raise SmsError('sms_form_changed')
                    async def submit():
                        await current.locator('[data-mc-sms-code="1"]').fill(code.value,timeout=5000)
                        await current.locator('[data-mc-sms-submit="1"]').click(timeout=5000)
                        await self.owner.page.wait_for_timeout(1500)
                    await self.gate.action('sms_submit_once',submit)
                finally:code=None
                current,_=await self.find()
                after=await self.owner.credentials(['https://'+d for d in HOSTS[self.platform]])
                changed=any(after.get(k) and after.get(k)!=before.get(k) for k in MARKERS[self.platform])
                before.clear();after.clear()
                proof=await self.owner.page.evaluate(SUCCESS)
                if current is not None or not (changed or proof.get('explicit') or self.platform=='weixin' and (proof.get('article') or proof.get('search'))):
                    raise SmsError('sms_login_not_confirmed')
                self.reason='sms_verified';return True
        except AccessRestricted:raise
        except SmsError as error:
            self.reason=str(error);raise LoginRequired(self.reason) from None
        except Exception:
            self.reason='sms_form_operation_failed';raise LoginRequired(self.reason) from None
