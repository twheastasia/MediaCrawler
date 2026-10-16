"""Monitor only explicitly launched people workers; never recover auth/control stops."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from collection.people_worker import ROOT,save


def read(path,default):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,json.JSONDecodeError):return default


def alive(record,run_id):
    pid=record.get('pid')
    if not pid:return False
    try:os.kill(pid,0)
    except (ProcessLookupError,PermissionError):return False
    args=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True,errors='replace').stdout
    return '-m collection.people_worker' in args and '--platform '+record['platform'] in args and '--run-id '+run_id in args


def inspect(out):
    manifest=read(out/'processes.json',{});now=time.time()
    report={'checked_at':now,'pid':os.getpid(),'platforms':{},'enabled':read(out/'control.json',{}).get('enabled',True)}
    if not report['enabled']:
        save(out/'monitor_status.json',report);return report
    history=read(out/'restart_history.json',{})
    records={r['platform']:r for r in manifest.get('processes',[])}
    for platform in manifest.get('platforms',[]):
        status=read(out/platform/'status.json',{});record=records.get(platform,{})
        info={'alive':alive(record,manifest.get('run_id','')),'worker_status':status.get('status')};report['platforms'][platform]=info
        if info['alive']:info['action']='running';continue
        if status.get('status') in ('finished','finished_with_shortfalls'):info['action']='finished';continue
        if status.get('requires_user_action') or status.get('status')=='paused':
            info.update(action='user_attention',reason=status.get('reason'));continue
        attempts=[x for x in history.get(platform,[]) if now-x<3600]
        if len(attempts)>=3:info.update(action='user_attention',reason='restart_budget_exhausted');continue
        delay=[300,900,1800][min(len(attempts),2)]
        if attempts and now-attempts[-1]<delay:info['action']='backoff';continue
        if not record:info.update(action='user_attention',reason='worker_never_started');continue
        history[platform]=attempts+[now]
        r=subprocess.run([sys.executable,'-m','collection.people_launch','--run-id',manifest['run_id'],
            '--output',str(out),'--platform',platform,'--browser',record.get('browser',manifest.get('browser','ego')),
            '--space-id',str(record.get('space_id') or 7),'--page',record['page'],'--resume'],
            cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45)
        info['action']='restarted' if r.returncode==0 else 'restart_failed'
    save(out/'restart_history.json',history);save(out/'monitor_status.json',report);return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--once',action='store_true')
    args=p.parse_args();out=args.output.resolve()
    with (out/'monitor.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        while True:
            r=inspect(out)
            if args.once:print(json.dumps(r,ensure_ascii=False));return
            if not r['enabled'] or all(x['action']=='finished' for x in r['platforms'].values()):return
            time.sleep(60)

if __name__=='__main__':main()
