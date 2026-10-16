"""Comment pagination, resume and barrier contracts for the production jobs."""
import copy
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from collection.comments import CommentCollector,CommentParseError,Page,normalize,public_raw
from collection.runtime import AccessRestricted


@pytest.mark.parametrize('historical,path,status,visible_wall,code,skippable',[
 (True,'/api/sns/web/v2/comment/page',461,False,-1,True),
 (True,'/api/sns/web/v2/comment/sub/page',461,False,-1,True),
 (False,'/api/sns/web/v2/comment/page',461,False,-1,False),
 (True,'/api/sns/web/v1/search/notes',461,False,-1,False),
 (True,'/api/sns/web/v2/comment/page',471,False,-1,False),
 (True,'/api/sns/web/v2/comment/page',461,True,-1,False),
 (True,'/api/sns/web/v2/comment/page',461,False,300011,False),
])
@pytest.mark.asyncio
async def test_xhs_skip_is_limited_to_single_historical_comment_461(tmp_path,historical,path,status,visible_wall,code,skippable):
    import httpx
    from collection.worker import Job
    from collection.comments import HistoricalCommentUnavailable
    worker=Job(SimpleNamespace(platform='xhs',output=tmp_path))
    worker.historical_comment_phase=historical;worker.current_operation='comments'
    worker.page.evaluate=AsyncMock(return_value=visible_wall)
    response=httpx.Response(status,json={'code':code},request=httpx.Request('GET','https://edith.xiaohongshu.com'+path))
    with pytest.raises(HistoricalCommentUnavailable if skippable else AccessRestricted):
        await worker.response(response)
    assert bool(worker.blocked_paths)==(not skippable)


@pytest.mark.asyncio
async def test_historical_comment_skip_retains_gap_and_advances_to_distinct_notes(monkeypatch):
    import collection.comments as module
    worker=job('xhs',None);worker.store=MemoryStore();worker.historical_comment_phase=True
    async def save(store,item,rows,key,state):store.states[key]=copy.deepcopy(state)
    monkeypatch.setattr(module,'save_page',save)
    collector=CommentCollector(worker)
    collector.page=AsyncMock(side_effect=module.HistoricalCommentUnavailable('xhs_historical_comment_http_461'))
    for cid in ['first','second','third']:
        state=await collector.collect({'id':cid,'type':'normal'})
        assert state['status']=='partial' and state['skipped'] and state['done']
        assert state['pending_after_skip'] and not state['queue']
    assert worker.store.states['comments:normal:third']['status']=='partial'
    assert collector.page.await_count==3
    await collector.collect({'id':'first','type':'normal'})
    assert collector.page.await_count==3  # resume never retries a skipped note


def job(platform,client):
    return SimpleNamespace(platform=platform,client=client,progress=lambda *a,**k:None)


@pytest.mark.parametrize('platform,method,payload,raw,cursor',[
 ('dy','get_aweme_comments',{'comments':[{'cid':'c1','text':'评论','reply_comment_total':2}], 'has_more':1,'cursor':20}, {},20),
 ('xhs','get_note_comments',{'comments':[{'id':'c1','content':'评论','sub_comments':[{'id':'s1','content':'回复'}], 'sub_comment_has_more':True,'sub_comment_cursor':'s2'}],'has_more':True,'cursor':'p2'}, {'xsec_token':'memory'},'p2'),
 ('ks','get_video_comments',{'rootCommentsV2':[{'comment_id':1,'content':'评论','hasSubComments':True}], 'pcursorV2':'next'}, {},'next'),
 ('wb','get_note_comments',{'data':[{'id':1,'text':'评论','total_number':3,'comments':[{'id':2,'text':'回复'}]}], 'max_id':20,'max_id_type':1}, {},20),
 ('zhihu','get_root_comments',{'data':[{'id':1,'content':'评论','child_comment_count':1}], 'paging':{'is_end':False,'next':'https://www.zhihu.com/api/v4/comment?offset=abc'}}, {},'abc'),
 ('bili','get_video_comments',{'replies':[{'rpid':1,'content':{'message':'评论'},'rcount':2}], 'cursor':{'is_end':False,'next':2}}, {'View':{'aid':123}},2),
])
@pytest.mark.asyncio
async def test_root_page_uses_native_single_request_and_schedules_children(platform,method,payload,raw,cursor):
    client=SimpleNamespace(**{method:AsyncMock(return_value=payload)})
    result=await CommentCollector(job(platform,client)).page({'id':'v','type':'video','raw':raw},{'cursor':None})
    assert result.rows[0]['body']=='评论'
    assert result.next_cursor['cursor']==cursor
    assert len(result.children)==1
    getattr(client,method).assert_awaited_once()
    if platform in ('xhs','wb'):assert result.rows[1]['parent_id']==result.rows[0]['id']


@pytest.mark.parametrize('platform,method,payload',[
 ('dy','get_sub_comments',{'comments':[{'cid':'s1','text':'回复'}],'has_more':0,'cursor':2}),
 ('xhs','get_note_sub_comments',{'comments':[{'id':'s1','content':'回复'}],'has_more':False}),
 ('ks','get_video_sub_comments',{'subCommentsV2':[{'comment_id':2,'content':'回复'}],'pcursorV2':'no_more'}),
 ('zhihu','get_child_comments',{'data':[{'id':'s1','content':'回复'}],'paging':{'is_end':True}}),
 ('bili','get_video_level_two_comments',{'replies':[{'rpid':2,'content':{'message':'回复'}}],'page':{'count':1}}),
 ('wb','get',{'data':[{'id':'s1','text':'回复'}],'max_id':0}),
])
@pytest.mark.asyncio
async def test_child_pages_preserve_parent_and_terminate(platform,method,payload):
    client=SimpleNamespace(**{method:AsyncMock(return_value=payload)})
    result=await CommentCollector(job(platform,client)).page({'id':'v','type':'answer','raw':{'View':{'aid':123}}},{'parent':'1','cursor':None})
    assert result.rows[0]['parent_id']=='1' and result.next_cursor is None


@pytest.mark.asyncio
async def test_tieba_first_floor_is_not_comment_but_its_replies_are_scheduled():
    payload={'post_list':[{'id':1,'floor':1,'content':[{'text':'正文'}],'sub_post_number':2},
        {'id':2,'floor':2,'content':[{'text':'评论'}],'author_id':3}],
        'page':{'total_page':2},'forum':{'id':9},'user_list':[{'id':3,'name':'完整昵称'}]}
    client=SimpleNamespace(_get_pc_page_data=AsyncMock(return_value=payload))
    result=await CommentCollector(job('tieba',client)).page({'id':'v'},{})
    assert [r['id'] for r in result.rows]==['2']
    assert result.rows[0]['author_name']=='完整昵称'
    assert result.children[0]=={'parent':'1','cursor':1,'total':2,'fid':'9'}
    assert result.next_cursor=={'cursor':2}


@pytest.mark.asyncio
async def test_missing_list_never_becomes_empty_success():
    collector=CommentCollector(job('dy',SimpleNamespace(get_aweme_comments=AsyncMock(return_value={'has_more':False}))))
    with pytest.raises(CommentParseError):await collector.page({'id':'v','raw':{}},{})


class MemoryStore:
    def __init__(self):self.states={};self.rows={}
    async def state(self,key):return copy.deepcopy(self.states.get(key))


@pytest.mark.asyncio
async def test_checkpoint_resume_includes_replies_and_deduplicates(monkeypatch):
    import collection.comments as module
    store=MemoryStore();worker=job('dy',None);worker.store=store
    async def save(store,item,rows,key,state):
        for r in rows:store.rows[r['id']]=r
        store.states[key]=copy.deepcopy(state)
    monkeypatch.setattr(module,'save_page',save)
    calls=[]
    class Collector(CommentCollector):
        async def page(self,item,stream):
            calls.append(copy.deepcopy(stream))
            if stream.get('parent'):return Page([{'id':'reply','parent_id':'c1'}])
            if stream.get('cursor')==2:return Page([{'id':'c1'},{'id':'c2'}])
            return Page([{'id':'c1'}],{'cursor':2},[{'parent':'c1','cursor':0}])
    item={'id':'v','type':'video'}
    await Collector(worker).collect(item,max_pages=1)
    assert not store.states['comments:video:v']['done']
    await Collector(worker).collect(item)
    assert calls==[{'parent':'','cursor':None},{'parent':'c1','cursor':0},{'parent':'','cursor':2}]
    assert set(store.rows)=={'c1','c2','reply'}
    assert store.states['comments:video:v']['status']=='complete'
    await Collector(worker).collect(item)
    assert len(calls)==3


@pytest.mark.asyncio
async def test_barrier_propagates_without_retry_or_advancing_checkpoint(monkeypatch):
    import collection.comments as module
    worker=job('dy',None);worker.store=MemoryStore()
    spy=AsyncMock();monkeypatch.setattr(module,'save_page',spy)
    collector=CommentCollector(worker);collector.page=AsyncMock(side_effect=AccessRestricted('verification_required'))
    with pytest.raises(AccessRestricted):await collector.collect({'id':'v','type':'video'})
    assert collector.page.await_count==1 and spy.await_count==0


@pytest.mark.asyncio
async def test_cursor_cycle_is_partial_not_infinite_retry(monkeypatch):
    import collection.comments as module
    worker=job('dy',None);worker.store=MemoryStore()
    async def save(store,item,rows,key,state):store.states[key]=copy.deepcopy(state)
    monkeypatch.setattr(module,'save_page',save)
    collector=CommentCollector(worker)
    collector.page=AsyncMock(return_value=Page([],{'cursor':1}))
    result=await collector.collect({'id':'v','type':'video'})
    assert collector.page.await_count==2
    assert result['done'] and result['status']=='partial'
    assert result['gaps'][0]['reason']=='comment_cursor_not_advancing'


def test_public_fields_and_raw_survive_without_session_data():
    raw={'comment_id':1,'author_id':2,'author_name':'公开昵称','content':'原文<b>评论</b>',
         'timestamp':1792110000000,'cookie':'secret','user':{'access_token':'secret'},'extra':['原始值']}
    row=normalize('ks',raw)
    assert row['body']=='原文评论' and row['time']==1792110000
    assert row['raw']['extra']==['原始值']
    assert 'secret' not in json.dumps(row)
    assert 'cookie' in raw


@pytest.mark.parametrize('value',['123','0','null','true','[1,2]','{"a":1}','"评论"','',None])
def test_json_shaped_comments_stay_text_instead_of_becoming_json(value):
    from collection.worker import text_of
    from collection.comments import text
    expected='' if value is None else value
    assert text(value)==expected
    assert text_of(value)==expected
    row=normalize('dy',{'cid':'numeric-text','text':value})
    assert row['body']==expected and row['raw']['text']==value


@pytest.mark.asyncio
async def test_weixin_not_exposed_is_not_zero_comment_success(monkeypatch):
    import media_platform.weixin.comments as wx
    worker=SimpleNamespace(store=MemoryStore())
    worker.page=SimpleNamespace(evaluate=AsyncMock(side_effect=[{}, {'comments':[],'area_present':False,'more':False}]),
        page=SimpleNamespace(evaluate=AsyncMock(),wait_for_timeout=AsyncMock()))
    worker.gate=SimpleNamespace(action=AsyncMock(side_effect=lambda label,fn:fn()))
    # AsyncMock side_effect must await the action to avoid leaking coroutines.
    async def action(label,fn):return await fn()
    worker.gate.action=action
    monkeypatch.setattr(wx,'check_article_page',lambda *_:None)
    captured=[]
    async def save(*args,**kwargs):captured.append(copy.deepcopy(args[4]))
    monkeypatch.setattr(wx,'save_page',save)
    result=await wx.collect(worker,{'id':'wx1','type':'article'},current_page=True)
    assert result['done'] and result['status']=='not_exposed'
    assert not result['all_platform_comments_verified']


@pytest.mark.asyncio
async def test_postgres_page_transaction_idempotence_and_rollback():
    if os.environ.get('COMMENT_TEST_DB')!='1':pytest.skip('explicit PostgreSQL integration opt-in')
    import asyncpg,config,uuid
    from collection.database import DDL
    from collection.comment_store import save_page
    c=config.postgres_db_config
    conn=await asyncpg.connect(host=c['host'],port=int(c['port']),user=c['user'],password=c['password'],database=c['db_name'])
    schema='test_comments_'+uuid.uuid4().hex
    class Pool:
        @asynccontextmanager
        async def acquire(self):yield conn
    try:
        await conn.execute('CREATE SCHEMA '+schema)
        await conn.execute('SET search_path TO '+schema)
        await conn.execute(DDL)
        await conn.execute("INSERT INTO acquisition_run(run_id,config) VALUES('test','{}')")
        await conn.execute("INSERT INTO acquisition_content(platform,content_type,content_id,raw) VALUES('dy','video','v','{}')")
        store=SimpleNamespace(pool=Pool(),run_id='test',platform='dy')
        item={'id':'v','type':'video'};row=normalize('dy',{'cid':'1','text':'评论'})
        await save_page(store,item,[row],'comments:video:v',{'cursor':1})
        await save_page(store,item,[row],'comments:video:v',{'cursor':2})
        assert await conn.fetchval('SELECT count(*) FROM acquisition_comment')==1
        with pytest.raises(KeyError):await save_page(store,item,[{**row,'id':'2'},{}],'comments:video:v',{'cursor':3})
        assert await conn.fetchval('SELECT count(*) FROM acquisition_comment')==1
        assert json.loads(await conn.fetchval('SELECT state FROM acquisition_progress'))=={'cursor':2}
    finally:
        await conn.execute('SET search_path TO public')
        await conn.execute('DROP SCHEMA '+schema+' CASCADE')
        await conn.close()


@pytest.mark.asyncio
async def test_weixin_public_dom_replies_and_load_more_use_gate(monkeypatch):
    import media_platform.weixin.comments as wx
    base={'id':'1','parent_id':'','content':'留言','user':{'name':'公开昵称'},'publish_text':'今天'}
    reply={'id':'2','parent_id':'1','content':'公开回复','user':{'name':'作者'},'publish_text':'今天'}
    snapshots=[{}, {'comments':[base],'area_present':True,'more':True},
               {'comments':[base,reply],'area_present':True,'more':False}]
    worker=SimpleNamespace(store=MemoryStore())
    worker.page=SimpleNamespace(evaluate=AsyncMock(side_effect=snapshots),
        page=SimpleNamespace(evaluate=AsyncMock(return_value=True),wait_for_timeout=AsyncMock()))
    actions=[]
    async def action(label,fn):actions.append(label);return await fn()
    worker.gate=SimpleNamespace(action=action)
    monkeypatch.setattr(wx,'check_article_page',lambda *_:None)
    writes=[]
    async def save(store,item,rows,key,state,**kwargs):writes.append((copy.deepcopy(rows),copy.deepcopy(state)))
    monkeypatch.setattr(wx,'save_page',save)
    result=await wx.collect(worker,{'id':'wx1','type':'article'},current_page=True)
    assert result['status']=='public_visible_complete' and result['public_visible']==2
    assert writes[-1][0][1]['parent_id']=='1'
    assert actions==['weixin_public_comments_scroll','weixin_public_comments_more']


def test_weibo_explicit_empty_comments_never_weakens_access_barriers():
    from collection.additional import check_payload
    check_payload('wb',{'ok':0,'msg':'暂无评论'},allow_empty_comments=True)
    with pytest.raises(AccessRestricted):check_payload('wb',{'ok':0,'msg':'暂无评论'})
    with pytest.raises(AccessRestricted):check_payload('wb',{'ok':0,'msg':'访问频繁'},allow_empty_comments=True)
