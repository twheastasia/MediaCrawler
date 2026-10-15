import asyncio
import json
import httpx
import pytest
from collection.runtime import RequestGate,GatedTransport
from collection.worker import candidates
@pytest.mark.asyncio
async def test_stream_stays_serial_and_next_request_waits_after_close(tmp_path):
    gate=RequestGate(tmp_path/'requests.jsonl',minimum=.06,jitter=0)
    seen=[]
    async def respond(request):
        seen.append(str(request.url));return httpx.Response(200,stream=httpx.ByteStream(b'payload'))
    async with httpx.AsyncClient(transport=GatedTransport(gate,httpx.MockTransport(respond))) as client:
        first=await client.send(client.build_request('GET','https://example.org/one'),stream=True)
        second=asyncio.create_task(client.get('https://example.org/two'))
        await asyncio.sleep(.02)
        assert len(seen)==1
        await first.aread();await asyncio.wait_for(second,2)
    starts=[json.loads(x) for x in (tmp_path/'requests.jsonl').read_text().splitlines() if json.loads(x)['event']=='start']
    assert starts[1]['gap_seconds']>=.06
@pytest.mark.asyncio
async def test_transport_error_releases_gate_but_retry_obeys_interval(tmp_path):
    gate=RequestGate(tmp_path/'requests.jsonl',minimum=.03,jitter=0)
    calls=0
    async def respond(request):
        nonlocal calls;calls+=1
        if calls==1:raise httpx.ConnectError('temporary')
        return httpx.Response(200,stream=httpx.ByteStream(b'ok'))
    async with httpx.AsyncClient(transport=GatedTransport(gate,httpx.MockTransport(respond))) as client:
        with pytest.raises(httpx.ConnectError):await client.get('https://example.org/one')
        assert (await client.get('https://example.org/one')).content==b'ok'
    starts=[json.loads(x) for x in (tmp_path/'requests.jsonl').read_text().splitlines() if json.loads(x)['event']=='start']
    assert starts[1]['gap_seconds']>=.03

def test_search_candidates_do_not_count_ads_duplicates_or_strip_author_identity():
    rows={'result':[{'type':'ad','title':'ad'}, {'bvid':'BV1','mid':123,'title':'one'},
                    {'bvid':'BV1','mid':123,'title':'same'}]}
    items=candidates('bili',rows)
    assert len(items)==1 and items[0]['author_id']=='123'
    zh={'data':[{'object':{'type':'answer','id':42,'author':{'url_token':'author-a'},'question':{'id':9,'title':'q'}}},
                {'object':{'type':'people','id':88}}]}
    assert candidates('zhihu',zh)[0]['author_id']=='author-a'

@pytest.mark.asyncio
async def test_browser_transport_uses_native_session_without_exporting_cookie(tmp_path):
    from collection.runtime import BrowserTransport
    class Page:
        def __init__(self):self.calls=[]
        async def evaluate(self,expression):return 'https://www.zhihu.com'
        async def fetch(self,url,options):
            self.calls.append(json.dumps(options))
            return {'status':200,'headers':{'content-encoding':'gzip','content-type':'application/json'},'body':'{"content":"中文"}'}
    page=Page();gate=RequestGate(tmp_path/'requests.jsonl',minimum=.01,jitter=0)
    async with httpx.AsyncClient(transport=BrowserTransport(page,gate,set())) as client:
        r=await client.get('https://www.zhihu.com/api/v4/test',headers={'Cookie':'credential-secret','User-Agent':'test-agent'})
        assert r.json()['content']=='中文'
    assert 'credential-secret' not in page.calls[0]
    assert not gate.lock.locked()

@pytest.mark.asyncio
async def test_bili_verification_payload_is_not_an_empty_search(tmp_path):
    from argparse import Namespace
    from collection.worker import Job
    from collection.runtime import AccessRestricted
    job=Job(Namespace(platform='bili',space=3,output=str(tmp_path)))
    request=httpx.Request('GET','https://api.bilibili.com/x/web-interface/wbi/search/type')
    with pytest.raises(AccessRestricted,match='verification_required'):
        await job.response(httpx.Response(200,json={'code':0,'data':{'v_voucher':'verification-token'}},request=request))
    assert ('api.bilibili.com','/x/web-interface/wbi/search/type') in job.blocked_paths
    # Douyin returns an array in data; it is not a Bili verification object.
    await job.response(httpx.Response(200,json={'data':[]},request=request))

@pytest.mark.asyncio
async def test_larger_search_quota_reopens_old_tasks_and_covers_keywords_first(tmp_path,monkeypatch):
    from argparse import Namespace
    import collection.worker as worker
    monkeypatch.setattr(worker,'KEYWORDS',['a','b'])
    monkeypatch.setattr(worker,'SEARCH_PER_KEYWORD',7)
    class Store:
        def __init__(self):self.states={k:{'done':True,'target':5} for k in ['search:a','search:b']};self.rows={k:[] for k in ['a','b']}
        async def state(self,key,value=None):
            if value is None:return self.states.get(key)
            self.states[key]=value
        async def links(self,kw):return self.rows[kw]
        async def link(self,kw,role,item):self.rows[kw].append({'content_type':item['type'],'content_id':item['id']})
    job=worker.Job(Namespace(platform='bili',output=str(tmp_path)))
    job.store=Store();calls=[]
    async def search(kw,page):
        calls.append(kw)
        return {'result':[{'bvid':kw+str(i),'mid':1} for i in range(10)]}
    async def detail(candidate):return candidate
    async def persist(item):pass
    job.raw_search=search;job.detail=detail;job.persist=persist;job.progress=lambda *a,**k:None
    await job.search_phase()
    assert calls==['a','b','a','b']
    assert all(len(rows)==7 for rows in job.store.rows.values())
    assert all(s['target']==7 and s['status']=='complete' for s in job.store.states.values())

def test_xhs_candidates_preserve_author_and_detail_token():
    rows={'items':[{'id':'note-1','xsec_token':'public-note-token',
                   'note_card':{'type':'normal','display_title':'title','user':{'user_id':'author-1','nickname':'原名'}}}]}
    result=candidates('xhs',rows)
    assert result[0]['id']=='note-1' and result[0]['author_id']=='author-1'
    assert result[0]['raw']['xsec_token']=='public-note-token'

def test_monitor_keeps_live_and_finished_jobs_and_never_retries_access_barriers(tmp_path,monkeypatch):
    import collection.supervisor as supervisor
    from types import SimpleNamespace
    monkeypatch.setattr(supervisor,'PAGES',{'bili':None,'dy':None,'zhihu':None,'xhs':None})
    monkeypatch.setattr(supervisor,'alive',lambda pid,platform:platform=='dy')
    (tmp_path/'processes.json').write_text(json.dumps({'processes':[{'platform':'dy','pid':1}]}))
    for p,status in {'bili':{'status':'blocked','error_type':'AccessRestricted'},
                     'dy':{'status':'searching'},'zhihu':{'status':'finished'},
                     'xhs':{'status':'blocked','error_type':'ConnectError'}}.items():
        d=tmp_path/p;d.mkdir();(d/'status.json').write_text(json.dumps(status))
    calls=[]
    def run(args,**kwargs):calls.append(args);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(supervisor.subprocess,'run',run)
    report=supervisor.inspect(tmp_path)
    assert report['platforms']['bili']['action']=='user_attention'
    assert report['platforms']['dy']['action']=='running'
    assert report['platforms']['zhihu']['action']=='finished'
    assert len(calls)==1 and calls[0][-1]=='xhs'
    assert supervisor.inspect(tmp_path)['platforms']['xhs']['action']=='backoff'
    assert len(calls)==1

def test_native_client_request_unwrap_removes_all_retry_layers():
    import inspect
    from media_platform.xhs.client import XiaoHongShuClient
    from media_platform.zhihu.client import ZhiHuClient
    for client in (XiaoHongShuClient,ZhiHuClient):
        native=inspect.unwrap(client.request)
        assert not hasattr(native,'retry')
        assert 'media_platform' in native.__code__.co_filename

@pytest.mark.asyncio
async def test_chrome_session_transport_uses_shared_session_and_honors_interval(tmp_path):
    from types import SimpleNamespace
    from collection.runtime import ChromeSessionTransport
    class Response:
        status=200
        headers={'content-encoding':'gzip','content-type':'application/json'}
        async def body(self):return b'{"status_code":0}'
        async def dispose(self):pass
    class ContextRequests:
        async def fetch(self,url,**options):return Response()
    page=SimpleNamespace(context=SimpleNamespace(request=ContextRequests()))
    gate=RequestGate(tmp_path/'requests.jsonl',minimum=.03,jitter=0)
    async with httpx.AsyncClient(transport=ChromeSessionTransport(page,gate,set())) as client:
        for _ in range(2):
            response=await client.get('https://www.douyin.com/api/test',headers={'Cookie':'session-secret'})
            assert response.json()['status_code']==0
    rows=[json.loads(x) for x in (tmp_path/'requests.jsonl').read_text().splitlines()]
    assert [r for r in rows if r['event']=='start'][1]['gap_seconds']>=.03
    assert 'session-secret' not in (tmp_path/'requests.jsonl').read_text()
