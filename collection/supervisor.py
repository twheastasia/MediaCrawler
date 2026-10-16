"""Restart exited workers with bounded backoff; never retry access barriers."""
import argparse,fcntl,json,os,subprocess,sys,time
from pathlib import Path
from collection.launch import ROOT,DEFAULT_OUTPUT
from collection.settings import ORIGINAL_PLATFORMS

BARRIERS={'AccessRestricted','LoginRequired','PlatformAccessError','IPBlockError','RetryError'}
def alive(pid,platform=None):
    if not pid:return False
    try:os.kill(pid,0)
    except (ProcessLookupError,PermissionError):return False
    r=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
    expected='-m collection.worker' if platform else '-m collection.supervisor'
    return r.returncode==0 and expected in r.stdout and (not platform or '--platform '+platform in r.stdout)
def read(path,default):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,json.JSONDecodeError):return default
def save(path,value):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(path)
def inspect(output):
    now=time.time();manifest=read(output/'processes.json',{})
    records={r['platform']:r for r in manifest.get('processes',[])}
    history=read(output/'restart_history.json',{})
    report={'pid':os.getpid(),'checked_at':now,'platforms':{},'enabled':True}
    if read(output/'monitor_control.json',{}).get('enabled') is False:
        report['enabled']=False;save(output/'monitor_status.json',report);return report
    for platform in manifest.get('platforms',ORIGINAL_PLATFORMS):
        status=read(output/platform/'status.json',{})
        record=records.get(platform,{})
        info={'worker_status':status.get('status','not_started'),'alive':alive(record.get('pid'),platform)}
        report['platforms'][platform]=info
        if info['alive']:info['action']='running';continue
        if status.get('status')=='finished':info['action']='finished';continue
        if status.get('requires_user_action') or status.get('error_type') in BARRIERS or status.get('reason')=='verification_required':
            info['action']='user_attention';info['reason']=status.get('error_type') or status.get('reason');continue
        attempts=[t for t in history.get(platform,[]) if now-t<3600]
        cooldown=[300,900,1800][min(len(attempts),2)]
        if len(attempts)>=3:
            info['action']='user_attention';info['reason']='restart_limit';continue
        if attempts and now-attempts[-1]<cooldown:
            info['action']='backoff';info['retry_after_seconds']=round(cooldown-(now-attempts[-1]));continue
        history[platform]=attempts+[now]
        try:
            result=subprocess.run([sys.executable,'-m','collection.launch','--output',str(output),
                '--run-id',manifest.get('run_id','mit_ai_smallbatch_20261014'),
                '--allow-session-memory','--resume','--platform',platform],cwd=ROOT,capture_output=True,text=True,timeout=45)
            info['action']='restarted' if result.returncode==0 else 'restart_failed'
            info['exit_code']=result.returncode
        except subprocess.TimeoutExpired:
            info['action']='restart_failed';info['reason']='launch_timeout'
        # Do not persist subprocess output: only safe status metadata is needed.
    save(output/'restart_history.json',history)
    save(output/'monitor_status.json',report)
    return report
def main():
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    args=p.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    with (output/'monitor.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status':'monitor_already_running'}));return
        while True:
            report=inspect(output)
            if args.once:print(json.dumps(report,ensure_ascii=False));return
            if not report['enabled']:return
            if all(x['action']=='finished' for x in report['platforms'].values()):return
            time.sleep(60)
if __name__=='__main__':main()
