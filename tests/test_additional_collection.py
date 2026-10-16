import json
from argparse import Namespace

import pytest

from collection.additional import normalize,extract_candidates,check_payload,AdditionalJob
from collection.runtime import AccessRestricted,LoginRequired


def test_nested_weibo_cards_preserve_author_and_raw_content():
    raw={'id':'123','user':{'id':456,'screen_name':'完整昵称'},'text':'原文<strong>内容</strong>',
         'created_at':'Thu Oct 15 09:00:00 +0800 2026','pics':[{'large':{'url':'https://image.test/a.jpg'}}]}
    items=extract_candidates('wb',{'cards':[{'card_group':[{'mblog':raw},{'mblog':raw}]}]})
    assert len(items)==1
    assert items[0]['author_id']=='456' and items[0]['raw']==raw
    assert items[0]['body']=='原文内容' and items[0]['time']


def test_kuaishou_timestamp_and_deleted_detail_do_not_forge_author():
    raw={'photo':{'id':'v1','caption':'作品','timestamp':1792055016000},'author':{'id':'u1','name':'完整昵称'}}
    item=extract_candidates('ks',{'feeds':[raw]})[0]
    assert item['time']==1792055016 and item['author_id']=='u1' and item['raw']==raw
    assert normalize('ks',{'photo':None,'author':None})['id']==''


def test_tieba_raw_api_keeps_public_author_routing_and_image():
    user={'portrait':'tb_public','id':'42','show_nickname':'完整昵称'}
    data={'data':{'card_list':[{'cardInfo':'thread','data':{'tid':'123','title':'标题','user':user}}]}}
    candidate=extract_candidates('tieba',data)[0]
    assert candidate['author_id']=='tb_public' and candidate['raw']['user']==user
    item=normalize('tieba',{'thread':{'id':'123','title':'标题'},'_author':user,
        'first_floor':{'time':1792055016,'content':[{'type':0,'text':'正文'},{'type':3,'src':'https://image.test/a.jpg'}]}})
    assert item['body']=='正文' and item['author_id']=='tb_public'


@pytest.mark.parametrize('platform,data,error',[
    ('ks',{'result':2},AccessRestricted),('ks',{'result':50},AccessRestricted),
    ('wb',{'ok':0,'msg':'请登录'},LoginRequired),('wb',{'ok':0,'msg':'访问频繁'},AccessRestricted),
    ('tieba',{'error_code':1},AccessRestricted)])
def test_barriers_stop_without_hidden_retries(platform,data,error):
    with pytest.raises(error):check_payload(platform,data)
    check_payload(platform,{'result':1,'ok':1,'error_code':0})


def test_supervisor_new_run_restarts_only_configured_platforms(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import collection.supervisor as supervisor
    (tmp_path/'processes.json').write_text(json.dumps({'run_id':'additional_run','platforms':['ks','wb','tieba'],'processes':[]}))
    monkeypatch.setattr(supervisor,'alive',lambda *_:False)
    calls=[]
    monkeypatch.setattr(supervisor.subprocess,'run',lambda args,**kw:(calls.append(args) or SimpleNamespace(returncode=0)))
    report=supervisor.inspect(tmp_path)
    assert set(report['platforms'])=={'ks','wb','tieba'}
    assert all('additional_run' in args for args in calls)
    assert len(calls)==3


@pytest.mark.asyncio
async def test_author_access_barrier_is_not_swallowed_as_missing_history(tmp_path):
    from collection.worker import Job
    job=Job(Namespace(platform='ks',output=str(tmp_path)))
    class Store:
        async def authors(self):return [{'author_id':'u1','keyword':'AI课程'}]
        async def state(self,*args):return None
    job.store=Store();job.progress=lambda *a,**kw:None
    async def restricted(*_):raise AccessRestricted('verification_required')
    job.history=restricted
    with pytest.raises(AccessRestricted):await job.author_phase()
