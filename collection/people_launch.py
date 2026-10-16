"""Explicit launcher for the separate people run. Never expands old collections."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from collection.people_store import PeopleStore,PLATFORMS
from collection.people_worker import ROOT,save
from collection.settings import KEYWORDS


async def prepare(args):
    store=await PeopleStore.open(args.run_id,args.platform)
    try:
        await store.setup({'scope':'people','platforms':list(PLATFORMS),'keywords':KEYWORDS,'people_per_keyword':100,
            'request_interval':15,'browser':args.browser,'space_id':args.space_id if args.browser=='ego' else None,'pages':{'maimai':'p1','linkedin':'p2'},
            'opencli_commands':{'maimai':'search-talents','linkedin':'people-search'},'posts':False,'comments':False})
    finally:await store.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--platform',choices=PLATFORMS,required=True)
    p.add_argument('--run-id',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--browser',choices=('chrome_cdp','ego'),default='chrome_cdp')
    p.add_argument('--space-id',type=int,default=7);p.add_argument('--page',default='p1')
    p.add_argument('--resume',action='store_true');args=p.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);manifest=out/'processes.json'
    d=json.loads(manifest.read_text()) if manifest.exists() else {'run_id':args.run_id,'platforms':[],'processes':[],'requested_platforms':list(PLATFORMS)}
    if d['run_id']!=args.run_id:raise SystemExit('run_id_mismatch')
    for old in d['processes']:
        if old['platform']!=args.platform:continue
        try:os.kill(old['pid'],0)
        except ProcessLookupError:continue
        raise SystemExit('worker_already_alive')
    if args.platform in d['platforms'] and not args.resume:raise SystemExit('resume_required')
    asyncio.run(prepare(args))
    directory=out/args.platform;directory.mkdir(exist_ok=True)
    with (directory/'worker.log').open('a') as log:
        worker=subprocess.Popen([sys.executable,'-m','collection.people_worker','--platform',args.platform,'--run-id',args.run_id,
            '--output',str(out),'--browser',args.browser,'--space-id',str(args.space_id),'--page',args.page],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    d['platforms']=list(dict.fromkeys(d['platforms']+[args.platform]));d['browser']=args.browser;d['scope']='people'
    d['processes']=[r for r in d['processes'] if r['platform']!=args.platform]+[{'platform':args.platform,'pid':worker.pid,
        'browser':args.browser,'space_id':args.space_id if args.browser=='ego' else None,'page':args.page}]
    save(manifest,d);print(json.dumps(d,ensure_ascii=False))

if __name__=='__main__':main()
