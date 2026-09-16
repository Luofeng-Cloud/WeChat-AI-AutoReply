import os
import sys
import subprocess
import psutil
import ctypes

# Windows Win32 Console Auto-Hide
try:
    hwnd_console = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd_console:
        ctypes.windll.user32.ShowWindow(hwnd_console, 0)
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(BASE_DIR, "wechat_bot_dev.pid")

def kill_wechat_bot():
    my_pid = os.getpid()
    
    # 1. Kill by PID file
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                pid_str = f.read().strip()
            if pid_str:
                pid = int(pid_str)
                if pid != my_pid and psutil.pid_exists(pid):
                    p = psutil.Process(pid)
                    try:
                        cmd = " ".join(p.cmdline() or []).lower()
                    except Exception:
                        cmd = ""
                    if "wechat_bot_gui" not in cmd:
                        p.kill()
                        print(f"Terminated WeChat Bot Dev by PID {pid}")
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

    # 2. Iterate processes and kill only wechat_ai_bot_dev
    for p in psutil.process_iter():
        try:
            if p.pid == my_pid:
                continue
            name = p.name().lower()
            try:
                cmd = " ".join(p.cmdline() or []).lower()
            except Exception:
                cmd = ""
            if ("wechat_ai_bot_dev" in name or "wechat_ai_bot_dev.py" in cmd) and ("wechat_bot_gui" not in cmd and "wechat_bot_gui" not in name):
                p.kill()
                print(f"Terminated WeChat Bot Dev PID: {p.pid}")
        except Exception:
            pass

if __name__ == "__main__":
    kill_wechat_bot()
