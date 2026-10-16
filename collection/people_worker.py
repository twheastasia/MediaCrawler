"""One platform/process/page. OpenCLI people methods run through Ego, never Chrome."""
import argparse
import asyncio
import fcntl
import json
import os
import time
from pathlib import Path

from collection.people_store import PeopleStore,PLATFORMS
from collection.settings import KEYWORDS

ROOT=Path(__file__).resolve().parents[1]


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(path)


class Worker:
    def __init__(self,args):
        self.args=args;self.out=Path(args.output).resolve();self.directory=self.out/args.platform
        self.child=None;self.store=None;self.chrome_people=None

    def status(self,status,**fields):
        from collection.friendly_log import activity
        activity(self.directory,self.args.platform,status,**fields)
        data={'platform':self.args.platform,'run_id':self.args.run_id,'pid':os.getpid(),'status':status,
              'browser':getattr(self.args,'browser','ego'),'updated_at':time.time(),**fields}
        save(self.directory/'status.json',data);print(json.dumps(data,ensure_ascii=False),flush=True)

    def enabled(self):
        try:return json.loads((self.out/'control.json').read_text()).get('enabled',True)
        except FileNotFoundError:return True

    async def search(self,keyword,page):
        if getattr(self.args,'browser','ego')=='chrome_cdp':
            if not self.enabled():return {'ok':False,'errorCode':'USER_PAUSED'}
            if self.chrome_people is None:
                from collection.people_chrome import ChromePeople
                self.chrome_people=ChromePeople(self.args.platform,self.out)
            return await self.chrome_people.search(keyword,page)
        # The two processes own separate pages, while individual Ego rounds are
        # serialized to avoid a shared TaskSpace control/round race.
        lock=(self.out/'ego_round.lock').open('a')
        await asyncio.to_thread(fcntl.flock,lock,fcntl.LOCK_EX)
        try:
            if not self.enabled():return {'ok':False,'errorCode':'USER_PAUSED'}
            cfg=dict(spaceId=self.args.space_id,pageLabel=self.args.page,platform=self.args.platform,
                keyword=keyword,pageNumber=page,size=20 if self.args.platform=='maimai' else 10,output=str(self.out))
            module=str(ROOT/'collection/ego_people.mjs')
            script=(f'const {{peopleSearch}}=await import({json.dumps(module)});'
                f'try{{const r=await peopleSearch(taskSpace,{json.dumps(cfg,ensure_ascii=False)});'
                'console.log("MC_PEOPLE_RESULT:"+JSON.stringify(r));}'
                'catch(e){console.log("MC_PEOPLE_RESULT:"+JSON.stringify({ok:false,errorCode:'
                '/control|ownership|inactive|unassigned/i.test(e.message||"")?"EGO_CONTROL_REQUIRED":"EGO_OPERATION_FAILED",'
                'errorType:e.name||"Error"}));}')
            self.child=await asyncio.create_subprocess_exec('ego-browser','nodejs','-e',script,
                cwd=ROOT,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:stdout,stderr=await asyncio.wait_for(self.child.communicate(),240)
            except asyncio.TimeoutError:
                self.child.terminate();await self.child.wait()
                return {'ok':False,'errorCode':'BROWSER_OPERATION_TIMEOUT','requiresUserAction':True}
            # Ego's native CLI currently forwards script console output to stderr.
            # Parse only our JSON marker, never persist the other diagnostics.
            for line in (stdout+stderr).decode(errors='replace').splitlines():
                if line.startswith('MC_PEOPLE_RESULT:'):return json.loads(line[len('MC_PEOPLE_RESULT:'):])
            return {'ok':False,'errorCode':'EGO_PROTOCOL_ERROR','requiresUserAction':True}
        finally:
            if self.chrome_people:await self.chrome_people.close()
            if self.child and self.child.returncode is None:
                try:self.child.terminate()
                except ProcessLookupError:pass
                await self.child.wait()
            self.child=None;fcntl.flock(lock,fcntl.LOCK_UN);lock.close()

    async def run(self):
        locked=False
        try:
            self.store=await PeopleStore.open(self.args.run_id,self.args.platform)
            locked=await self.store.conn.fetchval('SELECT pg_try_advisory_lock(hashtext($1))','people:'+self.args.platform)
            if not locked:
                self.status('paused',reason='another_people_worker_owns_platform',requires_user_action=True);return
            # Continue the already verified keyword before the remaining list.
            keywords=['人工智能']+[k for k in KEYWORDS if k!='人工智能']
            for keyword in keywords:
                state=await self.store.state(keyword) or {}
                if state.get('status') in ('complete','partial','empty'):continue
                page=state.get('next_page',0);count=await self.store.count(keyword)
                while page<10 and count<self.args.target:
                    if not self.enabled():
                        self.status('paused',reason='USER_PAUSED',requires_user_action=True);return
                    self.status('searching',keyword=keyword,page_no=page,successful=count,target=self.args.target)
                    result=await self.search(keyword,page)
                    if not result.get('ok'):
                        reason=result.get('errorCode','ADAPTER_ERROR')
                        if reason=='PAGINATION_NOT_EXPOSED':
                            await self.store.state(keyword,{'status':'partial','next_page':page,'successful':count,
                                'target':self.args.target,'reason':reason});break
                        diagnostic={k:result[k] for k in ('errorType','network_metadata','page_path','login_attempted','login_submitted') if k in result}
                        if 'errorType' in diagnostic:diagnostic['error_type']=diagnostic.pop('errorType')
                        self.status('paused',reason=reason,keyword=keyword,page_no=page,requires_user_action=True,automatic_retry=False,**diagnostic)
                        await self.store.state('__worker__',{'status':'paused','reason':reason,'requires_user_action':True});return
                    before=count
                    next_state={'status':'running','next_page':page+1,'target':self.args.target,'last_page_size':len(result['rows'])}
                    if not result['rows'] and result.get('explicitEmpty'):next_state['status']='partial' if count else 'empty'
                    await self.store.save_page(keyword,page,result,next_state)
                    count=await self.store.count(keyword);page+=1
                    self.status('collecting',keyword=keyword,page_no=page,successful=count,total_people=await self.store.count())
                    if not result['rows']:break
                    if count==before:
                        await self.store.state(keyword,{'status':'partial','successful':count,'next_page':page,
                            'target':self.args.target,'reason':'duplicate_or_unresolved_page'});break
                end=await self.store.state(keyword) or {}
                if end.get('status')=='running' or count>=self.args.target:
                    await self.store.state(keyword,{**end,'status':'complete' if count>=self.args.target else 'partial',
                        'successful':count,'target':self.args.target,'next_page':page})
            states={k:await self.store.state(k) for k in KEYWORDS}
            self.status('finished' if all(s and s.get('status')=='complete' for s in states.values()) else 'finished_with_shortfalls',
                total_people=await self.store.count(),keywords=states)
            await self.store.state('__worker__',{'status':'finished','shortfalls':any(not s or s.get('status')!='complete' for s in states.values())})
        except asyncio.CancelledError:
            self.status('paused',reason='USER_PAUSED',requires_user_action=True);raise
        except Exception as e:
            # No SQL values, credentials or exception strings in diagnostics.
            restartable=type(e).__name__ in ('ConnectionDoesNotExistError','ConnectionResetError','InterfaceError')
            self.status('failed',reason='DATABASE_OR_WORKER_ERROR',error_type=type(e).__name__,
                requires_user_action=not restartable)
        finally:
            if self.child and self.child.returncode is None:
                self.child.terminate();await self.child.wait()
            if self.store:
                if locked:
                    try:await self.store.conn.execute('SELECT pg_advisory_unlock(hashtext($1))','people:'+self.args.platform)
                    except Exception:pass
                await self.store.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--platform',choices=PLATFORMS,required=True)
    p.add_argument('--run-id',required=True);p.add_argument('--output',required=True)
    p.add_argument('--browser',choices=('chrome_cdp','ego'),default='chrome_cdp')
    p.add_argument('--space-id',type=int,default=7);p.add_argument('--page',default='p1')
    p.add_argument('--target',type=int,default=100)
    asyncio.run(Worker(p.parse_args()).run())

if __name__=='__main__':main()
