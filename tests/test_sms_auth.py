"""Synthetic SMS only; never use live inbox messages as test fixtures."""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from collection.sms import Settings,Code,SmsError,Inbox,code_in,select_code,identity,phone_lock
from collection.sms_login import SmsLogin,phone_matches,host_allowed,PROBE
from collection.runtime import AccessRestricted,LoginRequired,RequestGate

SETTINGS=Settings('http://127.0.0.1/sms/query','13800000000',timeout=30)
NOW=1792129000


def message(content='【抖音】验证码：123456，5分钟内有效。',**kwargs):
    return {'content':content,'date':NOW*1000,'number':'1069000000','sim_id':1,'sub_id':1,'type':1,**kwargs}


@pytest.mark.parametrize('platform,brand',[('bili','哔哩哔哩'),('dy','抖音'),('xhs','小红书'),
    ('zhihu','知乎'),('ks','快手'),('wb','微博'),('tieba','百度'),('weixin','微信')])
def test_each_platform_selects_matching_new_code(platform,brand):
    result=select_code([message(f'【{brand}】验证码为123456。')],platform,SETTINGS,NOW,now=NOW+2)
    assert result.value=='123456'
    assert '123456' not in repr(result)


@pytest.mark.parametrize('row',[
 message(date=(NOW-200)*1000),message(date=(NOW+20)*1000),
 message('【小红书】验证码123456'),message('【腾讯】验证码123456'),
 message(type=2),message(recipient='13900000000'),
 message('【抖音】验证码123456，备用验证码654321'),
 message('【抖音小红书】验证码123456'),message('【抖音】订单号123456'),
])
def test_stale_future_other_platform_receiver_or_ambiguous_rows_are_rejected(row):
    assert select_code([row],'dy',SETTINGS,NOW,now=NOW+3) is None


def test_baseline_is_excluded_and_latest_matching_code_wins():
    old=message();new=message('【抖音】验证码654321',date=(NOW+1)*1000)
    result=select_code([message('【知乎】验证码888888',date=(NOW+2)*1000),old,new],
        'dy',SETTINGS,NOW,{identity(old)},now=NOW+3)
    assert result.value=='654321'


def test_equal_timestamp_conflict_and_unbound_multiple_sim_fail_closed():
    with pytest.raises(SmsError,match='sms_code_ambiguous'):
        select_code([message(),message('【抖音】验证码654321')],'dy',SETTINGS,NOW,now=NOW+2)
    with pytest.raises(SmsError,match='sms_multiple_sim_partitions'):
        select_code([message(),message(sub_id=2)],'dy',SETTINGS,NOW,now=NOW+2)
    bound=Settings(SETTINGS.endpoint,SETTINGS.phone,sub_id=1)
    assert select_code([message(),message(sub_id=2)],'dy',bound,NOW,now=NOW+2).value=='123456'


@pytest.mark.parametrize('body,expected',[
 ('【抖音】验证码为：123456，请勿泄露','123456'),
 ('【抖音】123456是您的验证码','123456'),('验证码1234','1234'),
 ('验证码123456789',None),('联系电话13800000000',None),
 ('验证码123456，验证码654321',None),
])
def test_code_extraction_requires_one_explicit_otp(body,expected):assert code_in(body)==expected


@pytest.mark.parametrize('value,matched',[
 ('',True),('13800000000',True),('+86 13800000000',True),('138****0000',True),
 ('139****0000',False),('138****9999',False),('13900000000',False),('****0000',False),
])
def test_phone_and_masked_phone_match(value,matched):assert phone_matches(value,SETTINGS.phone)==matched


@pytest.mark.parametrize('platform,url,allowed',[
 ('xhs','https://www.xiaohongshu.com',True),('tieba','https://passport.baidu.com',True),
 ('wb','https://passport.weibo.cn/signin/login',True),('weixin','https://weixin.sogou.com',True),
 ('xhs','https://xiaohongshu.com.evil.test',False),('xhs','http://www.xiaohongshu.com',False),
 ('dy','https://www.douyin.com:444',False),('weixin','https://example.org',False),
])
def test_host_allowlist(platform,url,allowed):assert host_allowed(platform,url)==allowed


class Gate:
    def __init__(self):self.calls=[]
    async def action(self,label,fn):self.calls.append(label);return await fn()


@pytest.mark.asyncio
async def test_query_exact_payload_no_redirects_and_no_content_in_logs(tmp_path):
    calls=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(200,json={'code':0,'data':[message()]})
    def factory(**kwargs):
        assert not kwargs['trust_env'] and not kwargs['follow_redirects']
        return httpx.AsyncClient(transport=httpx.MockTransport(transport),**kwargs)
    gate=RequestGate(tmp_path/'requests.jsonl',minimum=15,jitter=0)
    rows=await Inbox(SETTINGS,gate,factory).query()
    assert rows==[message()]
    assert json.loads(calls[0].content)=={'data':{'type':1,'page_num':1,'page_size':10}}
    log=(tmp_path/'requests.jsonl').read_text()
    assert '123456' not in log and SETTINGS.phone not in log and '抖音' not in log


@pytest.mark.asyncio
async def test_query_errors_do_not_expose_response_or_exception_messages():
    def factory(**kwargs):
        def fail(request):raise httpx.ConnectError('private content 123456',request=request)
        return httpx.AsyncClient(transport=httpx.MockTransport(fail),**kwargs)
    with pytest.raises(SmsError) as error:await Inbox(SETTINGS,Gate(),factory).query()
    assert str(error.value)=='sms_query_failed'


@pytest.mark.asyncio
async def test_phone_lock_serializes_platforms(tmp_path):
    path=tmp_path/'phone.lock'
    async with phone_lock(SETTINGS,path):
        with pytest.raises(SmsError,match='sms_phone_busy'):
            async with phone_lock(SETTINGS,path,timeout=0):pass
    async with phone_lock(SETTINGS,path,timeout=0):pass


@pytest.mark.asyncio
async def test_graphical_captcha_never_queries_sms_or_sends(monkeypatch):
    frame=SimpleNamespace(url='https://www.douyin.com',evaluate=AsyncMock(return_value={'offered':True,'captcha':True}))
    owner=SimpleNamespace(page=SimpleNamespace(url=frame.url,is_closed=lambda:False,frames=[frame]))
    factory=AsyncMock()
    with pytest.raises(AccessRestricted,match='sms_requires_manual_captcha'):
        await SmsLogin(owner,'dy',Gate(),SETTINGS,factory).run()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_unoffered_form_never_clears_restriction_or_queries_sms():
    frame=SimpleNamespace(url='https://www.douyin.com',evaluate=AsyncMock(return_value={'offered':False,'captcha':False}))
    owner=SimpleNamespace(page=SimpleNamespace(url=frame.url,is_closed=lambda:False,frames=[frame]))
    factory=AsyncMock()
    assert not await SmsLogin(owner,'dy',Gate(),SETTINGS,factory).run()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_form_flow_sends_once_fills_once_verifies_cookie_and_forgets_code(tmp_path,monkeypatch):
    import collection.sms_login as module
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lock(_):yield
    monkeypatch.setattr(module,'phone_lock',lock)
    locators={k:SimpleNamespace(fill=AsyncMock(),click=AsyncMock()) for k in ['phone','code','send','submit']}
    frame=SimpleNamespace(url='https://www.douyin.com')
    frame.locator=lambda selector:next(v for k,v in locators.items() if '-'+k+'=' in selector)
    owner=SimpleNamespace(page=SimpleNamespace(url=frame.url,is_closed=lambda:False,frames=[frame],wait_for_timeout=AsyncMock(),evaluate=AsyncMock(return_value={'explicit':False})),
        credentials=AsyncMock(side_effect=[{'sessionid':'old-cookie'},{'sessionid':'new-cookie'}]))
    inbox=SimpleNamespace(query=AsyncMock(return_value=[]),wait=AsyncMock(return_value=Code('123456',NOW,'id')))
    gate=Gate();helper=SmsLogin(owner,'dy',gate,SETTINGS,lambda *_:inbox)
    state={'offered':True,'has_send':True,'phone_input':True,'phone_value':'','masked_phone':''}
    helper.find=AsyncMock(side_effect=[(frame,state),(frame,state),(None,None)])
    assert await helper.run()
    assert gate.calls==['sms_send_once','sms_submit_once']
    locators['send'].click.assert_awaited_once();locators['code'].fill.assert_awaited_once_with('123456',timeout=5000)
    locators['submit'].click.assert_awaited_once()
    assert helper.reason=='sms_verified'
    assert '123456' not in repr(helper.__dict__)


@pytest.mark.asyncio
async def test_real_chrome_dom_probe_handles_sms_and_countdown_without_reading_otp(tmp_path):
    if os.environ.get('SMS_TEST_CDP')!='1':pytest.skip('explicit existing Chrome CDP opt-in')
    from collection.chrome import ChromePage
    owner=ChromePage(RequestGate(tmp_path/'requests.jsonl'))
    await owner.connect('about:blank')
    try:
        await owner.page.set_content('''<form><input placeholder="手机号" value="13800000000">
          <input placeholder="请输入验证码" value="do-not-read"><span>获取验证码</span><button>登录</button></form>''')
        result=await owner.page.evaluate(PROBE)
        assert result['offered'] and result['has_send'] and result['phone_input']
        assert 'do-not-read' not in json.dumps(result)
        await owner.page.locator('span').evaluate('(e)=>e.textContent="59秒后重新获取"')
        result=await owner.page.evaluate(PROBE)
        assert result['offered'] and not result['has_send']
        await owner.page.locator('form').evaluate('(e)=>e.insertAdjacentHTML("beforeend","<img id=captcha-verify-image style=width:10px;height:10px>")')
        assert (await owner.page.evaluate(PROBE))['captcha']
    finally:await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('verified,expected_calls',[(False,1),(True,2)])
async def test_worker_retries_checkpoint_only_after_confirmed_sms(tmp_path,monkeypatch,verified,expected_calls):
    from argparse import Namespace
    from collection.worker import Job
    from collection.database import Store
    conn=SimpleNamespace(fetchval=AsyncMock(return_value=True),execute=AsyncMock())
    pool=SimpleNamespace(acquire=AsyncMock(return_value=conn),release=AsyncMock())
    store=SimpleNamespace(pool=pool,state=AsyncMock(),close=AsyncMock())
    monkeypatch.setattr(Store,'open',AsyncMock(return_value=store))
    worker=Job(Namespace(platform='dy',output=str(tmp_path),run_id='test',preflight=False))
    worker.setup=AsyncMock();worker.collect_phases=AsyncMock(side_effect=[AccessRestricted('test_barrier'),None])
    worker.sms_login=AsyncMock(return_value=verified);worker.refresh_session=AsyncMock()
    worker.page=SimpleNamespace(last_network=[],close=AsyncMock())
    worker.progress=lambda *a,**k:None
    await worker.run()
    assert worker.collect_phases.await_count==expected_calls
    assert worker.refresh_session.await_count==int(verified)
    assert worker.sms_login.await_count==1
