import json
import os
import uuid
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from collection.people_store import person,PeopleStore,schema_ddl


def test_ego_invokes_opencli_arrow_source_but_preserves_linkedin_iife():
    script='''
import assert from 'node:assert/strict';
import {egoEvaluationSource} from './collection/ego_people.mjs';
assert.deepEqual(await eval(egoEvaluationSource('async () => ({probe:1})')),{probe:1});
assert.deepEqual(eval(egoEvaluationSource('() => ({probe:2})')),{probe:2});
const iife='(() => ({probe:3}))()';
assert.equal(egoEvaluationSource(iife),iife);
assert.deepEqual(eval(egoEvaluationSource(iife)),{probe:3});
'''
    subprocess.run(['node','--input-type=module','-e',script],
        cwd=Path(__file__).resolve().parents[1],check=True,capture_output=True)


@pytest.mark.asyncio
async def test_chrome_people_stops_on_access_barrier_without_login_retry(tmp_path):
    from collection.people_chrome import ChromePeople
    from collection.runtime import AccessRestricted
    p=ChromePeople('linkedin',tmp_path);p.connected=True
    p.adapter=SimpleNamespace(barriers=[],invoke=AsyncMock(side_effect=AccessRestricted('HTTP 461')))
    p.login=AsyncMock()
    result=await p.search('AI',0)
    assert result['errorCode']=='ACCESS_RESTRICTED'
    p.adapter.invoke.assert_awaited_once();p.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_chrome_maimai_does_not_attempt_automatic_login(tmp_path):
    from collection.people_chrome import ChromePeople
    from collection.runtime import LoginRequired
    p=ChromePeople('maimai',tmp_path);p.connected=True
    p.adapter=SimpleNamespace(barriers=[],invoke=AsyncMock(side_effect=LoginRequired()))
    result=await p.search('AI',0)
    assert result['errorCode']=='AUTH_REQUIRED' and not p.login_attempted
    p.adapter.invoke.assert_awaited_once()


def test_friendly_log_is_readable_and_drops_credential_values(tmp_path):
    from collection.friendly_log import activity
    activity(tmp_path/'xhs','xhs','blocked',keyword='人工智能',reason='HTTP 461',error_type='AccessRestricted')
    activity(tmp_path/'xhs','xhs','failed',reason='password=should-never-appear')
    value=(tmp_path/'collection_readable.log').read_text()
    assert '小红书' in value and '人工智能' in value and 'HTTP 461' in value
    assert 'should-never-appear' not in value and '敏感字段已省略' in value


def test_linkedin_profile_is_real_identity_and_source_is_preserved():
    raw={'name':'姓名','profile_url':'https://www.linkedin.com/in/public-person/?tracking=x','headline':'职业信息'}
    p=person('linkedin',raw,raw)
    assert p['user_id']=='public-person'
    assert p['profile_url']=='https://www.linkedin.com/in/public-person/'
    assert p['raw'] is raw and p['identity_kind']=='public_profile_handle'


def test_maimai_original_api_id_survives_display_mapping():
    raw={'id':123,'name':'姓名','position':'Engineer','company':'Example'}
    p=person('maimai',{'name':'姓名','job_title':'Engineer'},raw)
    assert p['user_id']=='123' and p['person_key']=='123' and p['raw'] is raw


@pytest.mark.parametrize('site',['maimai','linkedin'])
def test_missing_public_identity_is_explicit_and_never_fabricated(site):
    p=person(site,{'name':'Anonymous'},{'name':'Anonymous'})
    assert p['user_id'] is None and p['identity_kind']=='unresolved_fingerprint'
    assert p['person_key'].startswith('unresolved:')


def test_identical_names_with_different_public_ids_are_distinct():
    a=person('maimai',{'name':'Same'},{'id':1,'name':'Same'})
    b=person('maimai',{'name':'Same'},{'id':2,'name':'Same'})
    assert a['person_key']!=b['person_key']


def test_migration_matches_runtime():
    assert Path('database/migrations/20261016_people_tables.sql').read_text()==schema_ddl()


@pytest.mark.asyncio
async def test_worker_never_retries_failed_authentication(tmp_path,monkeypatch):
    from collection import people_worker as m
    store=SimpleNamespace(conn=SimpleNamespace(fetchval=AsyncMock(return_value=True),execute=AsyncMock()),
        state=AsyncMock(return_value=None),count=AsyncMock(return_value=0),close=AsyncMock())
    monkeypatch.setattr(m.PeopleStore,'open',AsyncMock(return_value=store))
    monkeypatch.setattr(m,'KEYWORDS',['人工智能'])
    worker=m.Worker(SimpleNamespace(platform='linkedin',output=tmp_path,run_id='r',target=100))
    worker.search=AsyncMock(return_value={'ok':False,'errorCode':'AUTH_REQUIRED'})
    worker.status=lambda status,**fields: setattr(worker,'last_status',(status,fields))
    await worker.run()
    worker.search.assert_awaited_once()
    assert worker.last_status[0]=='paused' and worker.last_status[1]['requires_user_action']


def test_supervisor_does_not_start_unrecorded_or_auth_paused_platforms(tmp_path,monkeypatch):
    from collection import people_supervisor as m
    (tmp_path/'processes.json').write_text(json.dumps({'run_id':'r','platforms':['linkedin'],
        'requested_platforms':['maimai','linkedin'],'processes':[{'platform':'linkedin','pid':1}]}))
    (tmp_path/'linkedin').mkdir();(tmp_path/'maimai').mkdir()
    (tmp_path/'linkedin/status.json').write_text(json.dumps({'status':'paused','reason':'AUTH_REQUIRED','requires_user_action':True}))
    monkeypatch.setattr(m,'alive',lambda *_:False)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:pytest.fail('no restart allowed'))
    report=m.inspect(tmp_path)
    assert list(report['platforms'])==['linkedin']
    assert report['platforms']['linkedin']['action']=='user_attention'


@pytest.mark.asyncio
async def test_ego_script_result_on_stderr_is_parsed_without_logging(tmp_path,monkeypatch):
    from collection import people_worker as m
    process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b'',
        b'[ego-browser:notice] notice\nMC_PEOPLE_RESULT:{"ok":true,"rows":[]}\n')))
    start=AsyncMock(return_value=process);monkeypatch.setattr(m.asyncio,'create_subprocess_exec',start)
    worker=m.Worker(SimpleNamespace(platform='linkedin',output=tmp_path,run_id='r',target=100,space_id=7,page='p2'))
    result=await worker.search('AI',0)
    assert result=={'ok':True,'rows':[]}
    assert start.call_args.kwargs['stderr']==m.asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_people_postgres_atomicity_dedup_and_keyword_sources():
    if os.environ.get('PEOPLE_TEST_DB')!='1':pytest.skip('explicit isolated PostgreSQL test opt-in')
    import asyncpg,config
    c=config.postgres_db_config
    con=await asyncpg.connect(host=c['host'],port=int(c['port']),user=c['user'],password=c['password'],database=c['db_name'])
    schema='test_people_'+uuid.uuid4().hex
    try:
        await con.execute('CREATE SCHEMA '+schema);await con.execute('SET search_path TO '+schema)
        for platform in ('maimai','linkedin'):
            s=PeopleStore(con,'r',platform);await s.setup({'scope':'people'})
            source={'id':'public-id','name':'人名','profile_url':'https://www.linkedin.com/in/public-id/'}
            result={'rows':[source],'rawRecords':[source]}
            await s.save_page('AI',0,result,{'next_page':1})
            await s.save_page('AI',0,result,{'next_page':1})
            await s.save_page('教育',0,result,{'next_page':1})
            assert await s.count()==1 and await s.count('AI')==1
            assert await con.fetchval(f'SELECT count(*) FROM {platform}_person_source')==2
            assert (await s.state('AI'))['next_page']==1
            broken=PeopleStore(con,'missing',platform)
            another={**source,'id':'new','profile_url':'https://www.linkedin.com/in/new/'}
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await broken.save_page('AI',0,{'rows':[another],'rawRecords':[another]},{'next_page':1})
            assert await con.fetchval(f'SELECT count(*) FROM {platform}_person')==1
            await s.save_page('AI',1,{'rows':[{'name':'No id'}],'rawRecords':[{'name':'No id'}]},{'next_page':2})
            assert await s.count()==1
        # Unknown row pairings may not silently merge the wrong raw user.
        with pytest.raises(ValueError):await s.save_page('AI',1,{'rows':[source],'rawRecords':[source,source]}, {})
    finally:
        await con.execute('SET search_path TO public');await con.execute('DROP SCHEMA '+schema+' CASCADE');await con.close()
