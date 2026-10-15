"""Opt-in monitor exclusively for media_platform.weixin.core; never imports legacy supervisor."""
import fcntl
import json
import subprocess
import sys
import time


def action(status, retries):
    if status.get("requires_user_action") or status.get("status") == "blocked":
        return "user_attention"
    if status.get("status") in ("completed", "completed_with_gaps"):
        return "finished"
    return "restart" if retries < 3 else "restart_limit"


def monitor(args):
    from media_platform.weixin.core import ROOT, save_json
    directory = args.output
    with (directory / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"platform":"weixin","error_code":"monitor_already_running"}')
            return 2
        command = [sys.executable, "-m", "media_platform.weixin"] + [v for v in sys.argv[1:] if v not in ("--monitor", "--background")]
        retries = 0
        try:
            while True:
                control = directory / "monitor_control.json"
                if control.exists() and json.loads(control.read_text()).get("enabled") is False:
                    save_json(directory / "monitor_status.json", {"platform": "weixin", "action": "disabled"})
                    return 0
                with (directory / "worker.log").open("a") as stream:
                    child = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
                save_json(directory / "monitor_status.json", {"platform": "weixin", "worker_pid": child.pid, "action": "running", "retries": retries})
                while child.poll() is None:
                    time.sleep(5)
                    if control.exists() and json.loads(control.read_text()).get("enabled") is False:
                        # Disabling monitoring does not terminate the worker.
                        save_json(directory / "monitor_status.json", {"platform": "weixin", "worker_pid": child.pid, "action": "disabled"})
                        return 0
                path = directory / "status.json"
                status = json.loads(path.read_text()) if path.exists() else {}
                if status.get("pid") != child.pid:
                    # Another worker owns the run; never schedule competing retries.
                    return 2
                result = action(status, retries)
                save_json(directory / "monitor_status.json", {"platform": "weixin", "action": result, "retries": retries, "exit_code": child.returncode})
                if result != "restart":
                    return 0 if result == "finished" else 2
                if not (directory / "run.json").exists():
                    return 2
                deadline = time.monotonic() + (300, 900, 1800)[retries]
                while time.monotonic() < deadline:
                    time.sleep(min(5, max(0, deadline - time.monotonic())))
                    if control.exists() and json.loads(control.read_text()).get("enabled") is False:
                        return 0
                if "--resume" not in command:
                    command.append("--resume")
                retries += 1
        except KeyboardInterrupt:
            return 130
