import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from collection.opencli_adapter import AdapterChanged, OpenCliAdapter, PACKAGE, check_page
from collection.opencli_platforms import post_item, OpenCLIJob, COMMENTS
from collection.runtime import AccessRestricted, LoginRequired


@pytest.mark.parametrize('site,path', [('linkedin','/check/china/add-phone'),('linkedin','/checkpoint/challenge'),
    ('linkedin','/authwall'),('maimai','/platform/login')])
def test_actual_account_pages_pause(site,path):
    with pytest.raises(LoginRequired):check_page(site,{'path':path},[])


def test_http_barrier_cannot_fall_through_visible_content():
    with pytest.raises(AccessRestricted):check_page('linkedin',{'path':'/feed/'},[{'status':999}])


def test_post_uses_public_identity_and_preserves_raw_text():
    raw={'url':'https://www.linkedin.com/feed/update/urn:li:activity:123/',
         'author_url':'https://www.linkedin.com/in/public-name/','raw_body':'a\n  b','body':'a b'}
    item=post_item(raw)
    assert item['id']=='123' and item['author_id']=='in/public-name'
    assert item['body']=='a\n  b' and item['raw'] is raw


@pytest.mark.parametrize('url',['https://example.com/posts/abc','https://www.linkedin.com/feed/',''])
def test_no_fake_post_identity(url):
    with pytest.raises(AdapterChanged):post_item({'url':url,'body':'hello'})


def test_new_comment_tables_support_same_atomic_native_mapping():
    from collection.native_comments import TABLES, native_values, upsert_sql
    for site in ('maimai','linkedin'):
        assert TABLES[site][0].__tablename__==site+'_comment'
        values=native_values(site,{'id':'p'}, {'id':'c','body':'text','author_id':'public','parent_id':'parent','raw':{}},1,2)
        assert values[:5]==['p','c','public','text','parent']
        assert 'ON CONFLICT (content_id,comment_id)' in upsert_sql(site)


@pytest.mark.asyncio
async def test_maimai_never_substitutes_talent_data():
    job=object.__new__(OpenCLIJob);job.platform='maimai';job.store=SimpleNamespace(state=AsyncMock())
    with pytest.raises(AdapterChanged):await job.collect_phases()
    assert job.blocked_reason=='maimai_opencli_post_adapter_unavailable'
    assert job.store.state.call_args.args[1]['talent_search_used'] is False


@pytest.mark.asyncio
async def test_bridge_rejects_upstream_write_and_retry_commands():
    adapter=OpenCliAdapter('linkedin',None)
    with pytest.raises(AdapterChanged):await adapter.invoke('safe-send',{})
    adapter=OpenCliAdapter('xiaohongshu',None)
    with pytest.raises(AdapterChanged):await adapter.invoke('comments',{})


@pytest.mark.asyncio
async def test_rpc_refuses_uncontrolled_clicks():
    adapter=OpenCliAdapter('linkedin',SimpleNamespace())
    adapter.guard=AsyncMock()
    with pytest.raises(AdapterChanged):await adapter.dispatch('evaluate',['() => button.click()'])


@pytest.mark.asyncio
async def test_api_scripts_are_paced_but_dom_reads_add_no_requests():
    calls=[]
    async def action(label,func):calls.append(label);return await func()
    chrome=SimpleNamespace(page=SimpleNamespace(evaluate=AsyncMock(return_value={'ok':True})),
        gate=SimpleNamespace(action=action))
    adapter=OpenCliAdapter('linkedin',chrome);adapter.guard=AsyncMock()
    await adapter.dispatch('evaluate',['() => document.title'])
    assert calls==[]
    await adapter.dispatch('evaluate',["async () => fetch('/api/example')"])
    assert calls==['opencli_evaluate']


@pytest.mark.asyncio
async def test_official_adapter_runs_over_host_rpc_without_browser_runtime():
    if not PACKAGE.exists():pytest.skip('isolated OpenCLI not installed')
    adapter=OpenCliAdapter('maimai',None)
    calls=[]
    async def dispatch(method,args):
        calls.append(method)
        if method=='getCookies':return [{'name':'csrftoken','value':'in-memory-fixture'}]
        if method=='evaluate':return {'code':200,'data':{'list':[{'id':'public123','name':'Person','company':'Company'}]}}
    adapter.dispatch=dispatch
    rows=await adapter.invoke('search-talents',{'query':'fixture','size':1})
    assert rows[0]['name']=='Person' and calls==['goto','waitForTimeout','getCookies','evaluate']
    assert adapter.process is None


@pytest.mark.asyncio
async def test_project_keyword_extension_is_post_search_not_job_search():
    if not PACKAGE.exists():pytest.skip('isolated OpenCLI not installed')
    adapter=OpenCliAdapter('linkedin',None);calls=[]
    async def dispatch(method,args):
        calls.append((method,args))
        if method=='evaluate':
            assert 'expander.click()' not in args[0]
            return {'rows':[{'url':'https://www.linkedin.com/feed/update/urn:li:activity:123/','body':'post'}]}
    adapter.dispatch=dispatch
    rows=await adapter.invoke('search-posts',{'query':'人工智能','limit':1})
    assert rows[0]['body']=='post'
    assert '/search/results/content/' in calls[0][1][0]


def test_temporary_dom_ids_are_never_public_comment_ids():
    assert 'urn:li:(?:fsd_)?comment:' in COMMENTS


def test_launch_allows_only_explicit_new_platform_selection():
    from collection.settings import ORIGINAL_PLATFORMS, ADDITIONAL_PLATFORMS, PAGES
    assert 'linkedin' in PAGES and 'maimai' in PAGES
    assert 'linkedin' not in ORIGINAL_PLATFORMS and 'maimai' not in ADDITIONAL_PLATFORMS


@pytest.mark.asyncio
async def test_owned_chrome_dom_fixture_and_official_parser(tmp_path):
    if os.environ.get('OPENCLI_TEST_CDP')!='1':pytest.skip('explicit CDP fixture opt-in')
    from collection.chrome import ChromePage
    from collection.runtime import RequestGate
    from collection.opencli_platforms import DETAIL
    chrome=ChromePage(RequestGate(tmp_path/'requests.jsonl',minimum=0))
    html='''<article class="feed-shared-update-v2">
      <a href="https://www.linkedin.com/in/public-author/">Author</a>
      <a href="https://www.linkedin.com/feed/update/urn:li:activity:123/">Public post</a>
      <div class="update-components-text">Raw post body with enough text to test the upstream OpenCLI card reader.</div>
      <div class="comments-comment-item" data-urn="urn:li:comment:root">
        <a href="https://www.linkedin.com/in/user-root/">Root</a>
        <div class="comments-comment-item__main-content">Root text</div>
        <div class="comments-comment-item" data-urn="urn:li:comment:child">
          <a href="https://www.linkedin.com/in/user-child/">Child</a>
          <div class="comments-comment-item__main-content">Reply text</div></div></div>
      <div class="comments-comment-item" id="ember123"><div class="comments-comment-item__main-content">No stable id</div></div>
    </article>'''
    try:
        await chrome.connect('about:blank')
        await chrome.page.route('https://mediacrawler-fixture.invalid/',lambda route:route.fulfill(body=html,content_type='text/html'))
        await chrome.page.goto('https://mediacrawler-fixture.invalid/')
        snapshot=await chrome.page.evaluate(COMMENTS)
        assert snapshot['missing']==1 and len(snapshot['rows'])==2
        assert snapshot['rows'][1]['parent_id']=='urn:li:comment:root'
        assert snapshot['rows'][1]['author_id']=='in/user-child'
        assert (await chrome.page.evaluate(DETAIL))['raw_body'].startswith('Raw post body')
        adapter=OpenCliAdapter('linkedin',chrome)
        async def fixture_navigation(url):
            assert '/search/results/content/' in url
        adapter.navigate=fixture_navigation
        result=await adapter.invoke('search-posts',{'query':'fixture','limit':1})
        assert result[0]['url'].endswith('urn:li:activity:123/')
        assert result[0]['author_url']=='https://www.linkedin.com/in/public-author/'
        assert 'Raw post body' in result[0]['raw_rendered_text']
    finally:await chrome.close()
