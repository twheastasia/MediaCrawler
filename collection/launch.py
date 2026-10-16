from __future__ import annotations
import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from collection.database import Store
from collection.settings import KEYWORDS,PAGES,ORIGINAL_PLATFORMS,SEARCH_PER_KEYWORD,HISTORY_PER_AUTHOR
ROOT=Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT=ROOT/'data'/'mit_ai_smallbatch_20261014'
RUN_ID='mit_ai_smallbatch_20261014'
async def prepare(output,run_id=RUN_ID,platforms=ORIGINAL_PLATFORMS,platform_options=None):
    store=await Store.open(run_id,'setup')
    try:
        await store.setup(dict(keywords=KEYWORDS,search_per_keyword=SEARCH_PER_KEYWORD,history_per_author=HISTORY_PER_AUTHOR,
            request_interval_minimum=15,request_interval_maximum=15,browser="chrome_cdp",
            platforms=list(platforms),media=True,comments=True,comment_replies=True,
            comment_scope='all_public_available',platform_options=platform_options or {},output=str(output)))
    finally:await store.close()
def command(platform,output,preflight=False,allow_session_memory=False,run_id=RUN_ID,xhs_mode='api'):
    args=[sys.executable,'-m','collection.worker','--platform',platform,
          '--run-id',run_id,'--output',str(output)]
    return args+(['--preflight'] if preflight else [])+(['--allow-session-memory'] if allow_session_memory else [])+(['--xhs-mode',xhs_mode] if platform=='xhs' and xhs_mode=='web' else [])
def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--run-id',default=RUN_ID)
    p.add_argument('--preflight',action='store_true');p.add_argument('--prepare-only',action='store_true')
    p.add_argument('--status',action='store_true');p.add_argument('--resume',action='store_true')
    p.add_argument('--allow-session-memory',action='store_true')
    p.add_argument('--platform',choices=PAGES,action='append')
    p.add_argument('--xhs-mode',choices=('api','web'),help='显式选择小红书正常网页或API方式；不自动绕过访问限制')
    args=p.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    selected=args.platform or list(ORIGINAL_PLATFORMS)
    manifest=output/'processes.json'
    old=json.loads(manifest.read_text()) if manifest.exists() else {}
    if old and old.get('run_id')!=args.run_id:raise SystemExit('Output belongs to a different run')
    configured=list(dict.fromkeys(old.get('platforms',[]) + [r['platform'] for r in old.get('processes',[])] + selected))
    platform_options=old.get('platform_options') or {}
    if args.xhs_mode:
        if 'xhs' not in selected:raise SystemExit('--xhs-mode requires selecting xhs')
        platform_options={**platform_options,'xhs':{'collection_mode':args.xhs_mode}}
    xhs_mode=(platform_options.get('xhs') or {}).get('collection_mode','api')
    if args.status:
        result={}
        for platform in PAGES:
            f=output/platform/'status.json'
            result[platform]=json.loads(f.read_text()) if f.exists() else {'status':'not_started'}
        print(json.dumps(result,ensure_ascii=False,indent=2));return
    if args.preflight:
        workers=[subprocess.Popen(command(platform,output,True,args.allow_session_memory,args.run_id,xhs_mode),cwd=ROOT) for platform in selected]
        codes=[w.wait() for w in workers]
        raise SystemExit(0 if all(c==0 for c in codes) else 2)
    asyncio.run(prepare(output,args.run_id,configured,platform_options))
    if args.prepare_only:
        print('PostgreSQL acquisition tables and run configuration initialized');return
    if not args.allow_session_memory:
        raise SystemExit('Session authorization is required before starting workers')
    manifest=output/'processes.json'
    previous=[]
    if manifest.exists():
        old=json.loads(manifest.read_text())
        previous=[r for r in old.get('processes',[]) if r['platform'] not in selected]
        for record in old.get('processes',[]):
            if record['platform'] not in selected:continue
            try:os.kill(record['pid'],0)
            except ProcessLookupError:continue
            raise SystemExit('A recorded worker is still alive; do not launch duplicate workers')
        if not args.resume:raise SystemExit('Previous run exists; use --resume after inspecting progress')
    records=[]
    for platform in selected:
        directory=output/platform;directory.mkdir(parents=True,exist_ok=True)
        with (directory/'worker.log').open('a') as log:
            worker=subprocess.Popen(command(platform,output,allow_session_memory=True,run_id=args.run_id,xhs_mode=xhs_mode),cwd=ROOT,
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        records.append(dict(platform=platform,pid=worker.pid,log=str(directory/'worker.log')))
    data={'run_id':args.run_id,'platforms':configured,'platform_options':platform_options,'created_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
          'browser':'chrome_cdp','processes':previous+records}
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    print(json.dumps(data,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
