"""Public web DOM, pagination, process mode and access barriers."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from collection.comments import CommentCollector,CommentUnavailable
from collection.runtime import AccessRestricted,LoginRequired
from collection.xhs_web import XhsWebJob,SNAPSHOT,check_snapshot,card_payload


@pytest.mark.parametrize('snapshot,barriers,error',[
    ({'blocked':True},[],AccessRestricted),
    ({'loggedOut':True},[],LoginRequired),
    ({'blocked':False},[{'status':461}],AccessRestricted),
])
def test_web_restriction_is_never_ignored_for_visible_content(snapshot,barriers,error):
    with pytest.raises(error):check_snapshot(snapshot,barriers)


def test_launcher_only_uses_web_mode_when_explicit_and_persisted():
    from collection.launch import command
    assert command('xhs','data',xhs_mode='web')[-2:]==['--xhs-mode','web']
    assert '--xhs-mode' not in command('dy','data',xhs_mode='web')
    assert '--xhs-mode' not in command('xhs','data')


def test_card_retains_public_identity_and_original_link():
    card={'note_id':'n','source_url':'https://www.xiaohongshu.com/explore/n?xsec_token=public-routing',
          'type':'normal','user':{'user_id':'u'},'title':'标题','cover_url':'https://cdn.example/image.jpg'}
    value=card_payload(card)
    assert value['note_card']['source_url']==card['source_url']
    assert value['note_card']['user']['user_id']=='u'
    assert 'Cookie' not in str(value)


@pytest.mark.parametrize('explicit',[False,True])
def test_launcher_preserves_web_mode_for_supervisor_resume(tmp_path,monkeypatch,explicit):
    import json,sys
    from pathlib import Path
    from collection import launch
    manifest=tmp_path/'processes.json'
    manifest.write_text(json.dumps({'run_id':'r','platforms':['dy','xhs'],
        'platform_options':{'xhs':{'collection_mode':'web'}},
        'processes':[{'platform':'dy','pid':11},{'platform':'xhs','pid':12}]}))
    argv=['launch','--run-id','r','--output',str(tmp_path),'--resume','--allow-session-memory','--platform','xhs']
    if explicit:argv+=['--xhs-mode','web']
    monkeypatch.setattr(sys,'argv',argv)
    monkeypatch.setattr(launch,'prepare',AsyncMock())
    monkeypatch.setattr(launch.os,'kill',lambda *_:(_ for _ in ()).throw(ProcessLookupError()))
    calls=[]
    def start(args,**kwargs):calls.append(args);return SimpleNamespace(pid=99)
    monkeypatch.setattr(launch.subprocess,'Popen',start)
    launch.main()
    data=json.loads(manifest.read_text())
    assert data['platforms']==['dy','xhs']
    assert data['platform_options']['xhs']['collection_mode']=='web'
    assert data['processes'][0]=={'platform':'dy','pid':11}
    assert len(calls)==1 and calls[0][-2:]==['--xhs-mode','web']


def web_job(snap):
    job=object.__new__(XhsWebJob)
    job.web_note_id='n';job.web_comment_step=0;job.web_barriers=[];job.web_link_lookups=set();job.store=None
    job.snapshot=AsyncMock(return_value=snap)
    job.open_note=AsyncMock(return_value=snap)
    job.page=SimpleNamespace(page=SimpleNamespace(evaluate=AsyncMock(),wait_for_timeout=AsyncMock(),locator=AsyncMock()))
    actions=[]
    async def action(label,fn):actions.append(label);return await fn()
    job.gate=SimpleNamespace(action=action);job.actions=actions
    return job


@pytest.mark.asyncio
async def test_visible_comments_have_real_ids_users_and_replies():
    snap={'comments':[{'id':'c','content':'评论 <b>字面符号</b>','user':{'user_id':'u'}},
                      {'id':'r','parent_id':'c','content':'回复','user':{'user_id':'v'}}],
          'noComments':False,'expandCount':0,'end':False,'total':2}
    job=web_job(snap)
    page=await job.web_comment_page({'id':'n'},{})
    assert page.next_cursor is None
    assert page.rows[0]['body']=='评论 <b>字面符号</b>'
    assert page.rows[0]['author_id']=='u' and page.rows[1]['parent_id']=='c'
    assert page.rows[1]['body']=='回复' and page.rows[1]['raw']['user']['user_id']=='v'


@pytest.mark.asyncio
async def test_unfinished_or_stalled_visible_comments_are_partial():
    snap={'comments':[{'id':'c','content':'评论','user':{'user_id':'u'}}],
          'noComments':False,'expandCount':0,'end':False,'total':10}
    job=web_job(snap)
    first=await job.web_comment_page({'id':'n'},{})
    assert first.next_cursor['cursor']==1
    second=await job.web_comment_page({'id':'n'},first.next_cursor)
    assert second.next_cursor['stalls']==1
    third=await job.web_comment_page({'id':'n'},second.next_cursor)
    assert third.next_cursor is None and third.gaps[0]['reason']=='xhs_web_visible_comments_stalled'
    assert job.actions==['xhs_web_comments_expand_or_scroll']*2


@pytest.mark.asyncio
async def test_dom_path_never_calls_api_adapter():
    job=web_job({'comments':[],'noComments':True,'expandCount':0,'end':False,'total':0})
    job.platform='xhs';job.client=SimpleNamespace(get_note_comments=AsyncMock(side_effect=AssertionError('API called')))
    page=await CommentCollector(job).page({'id':'n'},{})
    assert page.rows==[] and page.next_cursor is None
    job.client.get_note_comments.assert_not_called()


@pytest.mark.asyncio
async def test_stale_public_link_uses_only_exact_visible_search_match():
    job=object.__new__(XhsWebJob)
    job.web_note_id=None;job.web_link_lookups=set();job.store=None
    job.visit=AsyncMock(side_effect=[{'unavailable':True},
        {'cards':[{'note_id':'n','source_url':'https://www.xiaohongshu.com/explore/n?xsec_token=public'}]},
        {'unavailable':False,'root':True,'note':{'note_id':'n'}}])
    result=await job.open_note({'id':'n','type':'normal','title':'公开标题','raw':{}})
    assert result['note']['note_id']=='n'
    assert job.visit.await_count==3 and job.web_note_id=='n'
    job.web_note_id=None
    job.visit=AsyncMock(return_value={'unavailable':True})
    with pytest.raises(CommentUnavailable):await job.open_note({'id':'n','type':'normal','title':'公开标题','raw':{}})
    assert job.visit.await_count==1


@pytest.mark.asyncio
async def test_access_barrier_never_falls_back_to_title_search():
    job=object.__new__(XhsWebJob)
    job.web_note_id=None;job.web_link_lookups=set();job.store=None
    job.visit=AsyncMock(side_effect=AccessRestricted('xhs_web_document_restricted'))
    with pytest.raises(AccessRestricted):await job.open_note({'id':'n','type':'normal','title':'公开标题','raw':{}})
    assert job.visit.await_count==1


@pytest.mark.asyncio
async def test_search_waits_for_native_render_instead_of_misreading_empty_page():
    job=object.__new__(XhsWebJob)
    job.page=SimpleNamespace(page=SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)),wait_for_timeout=AsyncMock()))
    job.snapshot=AsyncMock(side_effect=[{'path':'/search_result','cards':[],'emptySearch':False},
                                      {'path':'/search_result','cards':[{'note_id':'n'}]}])
    async def action(label,fn):return await fn()
    job.gate=SimpleNamespace(action=action)
    result=await job.visit('https://www.xiaohongshu.com/search_result?keyword=test','test')
    assert result['cards'][0]['note_id']=='n' and job.snapshot.await_count==2
    job.page.page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_uses_only_owned_cdp_dom(tmp_path):
    if os.environ.get('XHS_WEB_TEST_CDP')!='1':pytest.skip('explicit Chrome CDP fixture test opt-in')
    from collection.chrome import ChromePage
    from collection.runtime import RequestGate
    page=ChromePage(RequestGate(tmp_path/'requests.jsonl'))
    try:
        await page.connect('about:blank')
        await page.page.set_content('''<section class="note-item"><a class="cover" href="https://www.xiaohongshu.com/search_result/card-id?xsec_token=public">公开笔记</a></section>
          <div class="note-container"><div id="detail-desc">正文</div>
          <div class="author-container"><a class="name" href="https://www.xiaohongshu.com/user/profile/owner">作者</a></div>
          <div class="comments-container">共 2 条评论
           <div class="parent-comment"><div class="comment-item" id="comment-c">
            <a class="name" href="https://www.xiaohongshu.com/user/profile/u">昵称</a><div class="content">主评论</div>
            <svg class="more-icon"></svg></div>
            <div class="reply-container"><div class="comment-item comment-item-sub" id="comment-r">
             <a class="name" href="https://www.xiaohongshu.com/user/profile/v">回复人</a><div class="content">回复</div></div></div>
           </div></div></div>''')
        snap=await page.evaluate(SNAPSHOT)
        assert snap['note']['desc']=='正文' and snap['note']['user']['user_id']=='owner'
        assert snap['comments'][0]['id']=='c' and snap['comments'][1]['parent_id']=='c'
        assert snap['comments'][1]['user']['user_id']=='v' and snap['total']==2
        assert not snap['blocked']
        assert snap['cards'][0]['note_id']=='card-id'
        assert '/search_result/card-id' in snap['cards'][0]['source_url']
    finally:await page.close()
