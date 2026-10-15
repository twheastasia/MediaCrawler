"""Small Chrome CDP probe; separate from the three-platform production run."""
import asyncio,json,logging,time,inspect
from pathlib import Path
from types import MethodType
from urllib.parse import urlsplit
import httpx
from collection.chrome import ChromePage
from collection.runtime import RequestGate,AccessRestricted
from collection.database import Store
from media_platform.xhs.client import XiaoHongShuClient
import media_platform.xhs.client as xc

OUT=Path('data/xhs_probe_20261014')
RUN='xhs_probe_20261014'
class Transport(httpx.AsyncBaseTransport):
    def __init__(self,page,gate):self.page,self.gate=page,gate
    async def handle_async_request(self,request):
        u=urlsplit(str(request.url))
        forbidden={'cookie','user-agent','host','accept-encoding','connection','content-length','referer','origin'}
        opts={'method':request.method,'headers':{k:v for k,v in request.headers.items() if k not in forbidden},
              'credentials':'include','timeout':30000}
        if request.method not in ('GET','HEAD'):opts['body']=request.content.decode()
        result=await self.gate.action(request.method+' '+u.netloc+u.path,lambda:self.page.fetch(str(request.url),opts))
        if result['status'] in (401,403,406,412,429,461,471):raise AccessRestricted('HTTP_'+str(result['status']))
        h={k:v for k,v in result['headers'].items() if k not in ('content-encoding','content-length')}
        return httpx.Response(result['status'],headers=h,stream=httpx.ByteStream(result['body'].encode()))
async def main():
    logging.disable(logging.CRITICAL)
    OUT.mkdir(parents=True,exist_ok=True)
    gate=RequestGate(OUT/'requests.jsonl');page=ChromePage(gate);store=None;cookies={}
    report={'platform':'xhs','browser':'chrome_cdp','keyword':'AI课程','target_notes':3,'successful_notes':0,'status':'starting'}
    try:
        await page.connect('https://www.xiaohongshu.com')
        cookies=await page.credentials(['https://www.xiaohongshu.com'])
        report['session_available']=bool(cookies.get('web_session'))
        if not report['session_available']:raise RuntimeError('Chrome_login_required')
        headers={'User-Agent':await page.evaluate('() => navigator.userAgent'),
                 'Cookie':'; '.join(f'{k}={v}' for k,v in cookies.items()),
                 'Referer':'https://www.xiaohongshu.com/','Content-Type':'application/json;charset=UTF-8'}
        client=XiaoHongShuClient(headers=headers,playwright_page=page,cookie_dict=cookies,timeout=30)
        client.request=MethodType(inspect.unwrap(XiaoHongShuClient.request),client)
        def factory(**kwargs):
            kwargs.pop('proxy',None);kwargs['transport']=Transport(page,gate);kwargs['trust_env']=False
            return httpx.AsyncClient(**kwargs)
        xc.make_async_client=factory
        store=await Store.open(RUN,'xhs')
        await store.setup({'keywords':['AI课程'],'search_per_keyword':3,'browser':'chrome_cdp','probe':True})
        data=await client.get_note_by_keyword('AI课程',page_size=20)
        entries=[x for x in data.get('items',[]) if x.get('id') and x.get('note_card')]
        report['search_results']=len(entries)
        for candidate in entries:
            if report['successful_notes']>=3:break
            cid=candidate['id']
            raw=await client.get_note_by_id(cid,candidate.get('xsec_source') or 'pc_search',candidate.get('xsec_token',''))
            if not raw:continue
            raw['note_id']=cid;user=raw.get('user') or {};aid=user.get('user_id')
            item={'type':raw.get('type') or 'note','id':cid,'author_id':aid,
                  'title':raw.get('title',''),'body':raw.get('desc',''),'time':raw.get('time'),
                  'url':'https://www.xiaohongshu.com/explore/'+cid,'raw':raw}
            await store.content(item);await store.link('AI课程','search',item)
            if aid:await store.author(aid,user.get('nickname'),'https://www.xiaohongshu.com/user/profile/'+aid,user)
            (OUT/(cid+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2))
            report['successful_notes']+=1
            print(json.dumps({'status':'detail_saved','successful_notes':report['successful_notes']},ensure_ascii=False),flush=True)
        report['status']='completed' if report['successful_notes']==3 else 'partial'
    except Exception as e:
        report['status']='blocked';report['error_type']=type(e).__name__
        if isinstance(e,AccessRestricted):report['reason']=str(e)
        elif not report.get('session_available'):report['reason']='Chrome_login_required'
    finally:
        cookies.clear()
        if store:await store.close()
        await page.close()
        report['updated_at']=time.time()
        (OUT/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False),flush=True)
if __name__=='__main__':asyncio.run(main())
