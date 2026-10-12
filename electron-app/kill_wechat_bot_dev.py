"""Legacy entry point: stop a running engine.

The engine now manages its own lifetime and the Electron app stops it on exit, so this
script is only needed for a daemon started by hand with wechat_ai_bot_dev.py.

The original version identified the target by matching "wechat_ai_bot_dev" in the
process command line. That string no longer appears anywhere, so it would silently kill
nothing. It now reads the engine PID file, and still verifies the process before
terminating it so a recycled PID cannot take down an unrelated program.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (os.path.join(HERE, "electron-app"), HERE):
    if os.path.isdir(os.path.join(candidate, "wechat_engine")):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
        break

from wechat_engine import paths  # noqa: E402


def _is_engine(process) -> bool:
    """Only treat a process as ours when its command line mentions the engine."""
    try:
        command = " ".join(process.cmdline() or []).lower()
    except Exception:
        return False
    return "wechat_engine" in command or "wechat-engine" in command


def kill_engine() -> int:
    import psutil

    me = os.getpid()
    killed = 0
    pid_file = paths.pid_file()

    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as handle:
                pid = int((handle.read() or "0").strip() or 0)
            if pid and pid != me and psutil.pid_exists(pid):
                process = psutil.Process(pid)
                if _is_engine(process):
                    process.kill()
                    killed += 1
                    print("已终止引擎进程 PID %s" % pid)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass

    for process in psutil.process_iter(["pid"]):
        try:
            if process.info["pid"] == me:
                continue
            candidate = psutil.Process(process.info["pid"])
            if _is_engine(candidate):
                candidate.kill()
                killed += 1
                print("已终止引擎进程 PID %s" % process.info["pid"])
        except Exception:
            pass

    if killed == 0:
        print("未发现正在运行的引擎进程。")
    return 0


if __name__ == "__main__":
    raise SystemExit(kill_engine())
