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
from collection.settings import KEYWORDS,PAGES,SEARCH_PER_KEYWORD,HISTORY_PER_AUTHOR
ROOT=Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT=ROOT/'data'/'mit_ai_smallbatch_20261014'
RUN_ID='mit_ai_smallbatch_20261014'
async def prepare(output):
    store=await Store.open(RUN_ID,'setup')
    try:
        await store.setup(dict(keywords=KEYWORDS,search_per_keyword=SEARCH_PER_KEYWORD,history_per_author=HISTORY_PER_AUTHOR,
            request_interval_minimum=30,request_interval_maximum=45,browser="chrome_cdp",
            platforms=list(PAGES),media=True,output=str(output)))
    finally:await store.close()
def command(platform,output,preflight=False,allow_session_memory=False):
    args=[sys.executable,'-m','collection.worker','--platform',platform,
          '--run-id',RUN_ID,'--output',str(output)]
    return args+(['--preflight'] if preflight else [])+(['--allow-session-memory'] if allow_session_memory else [])
def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--preflight',action='store_true');p.add_argument('--prepare-only',action='store_true')
    p.add_argument('--status',action='store_true');p.add_argument('--resume',action='store_true')
    p.add_argument('--allow-session-memory',action='store_true')
    p.add_argument('--platform',choices=PAGES,action='append')
    args=p.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    selected=args.platform or list(PAGES)
    if args.status:
        result={}
        for platform in PAGES:
            f=output/platform/'status.json'
            result[platform]=json.loads(f.read_text()) if f.exists() else {'status':'not_started'}
        print(json.dumps(result,ensure_ascii=False,indent=2));return
    if args.preflight:
        workers=[subprocess.Popen(command(platform,output,True,args.allow_session_memory),cwd=ROOT) for platform in selected]
        codes=[w.wait() for w in workers]
        raise SystemExit(0 if all(c==0 for c in codes) else 2)
    asyncio.run(prepare(output))
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
            worker=subprocess.Popen(command(platform,output,allow_session_memory=True),cwd=ROOT,
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        records.append(dict(platform=platform,pid=worker.pid,log=str(directory/'worker.log')))
    data={'run_id':RUN_ID,'created_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
          'browser':'chrome_cdp','processes':previous+records}
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    print(json.dumps(data,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
