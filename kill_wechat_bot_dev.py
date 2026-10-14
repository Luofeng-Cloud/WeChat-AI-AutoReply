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

def is_target_bot_process(proc):
    try:
        cmdline = proc.cmdline() or []
        cmd_str = " ".join(str(c) for c in cmdline).lower()
        name = (proc.name() or '').lower()
        if "gui" in name or "控制中心" in name or "wechat_bot_gui" in cmd_str:
            return False
        if "wechat_ai_bot_dev" in cmd_str or "wechat_ai_bot_dev" in name or "微信ai回复_开发版" in cmd_str or "微信ai回复(开发版)" in name or "微信ai自动回复" in name:
            return True
    except Exception:
        pass
    return False

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
                    if is_target_bot_process(p):
                        p.kill()
                        print(f"Terminated WeChat Bot Dev by PID {pid}")
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

    # 2. Iterate processes and kill only dev bot processes
    for p in psutil.process_iter():
        try:
            if p.pid == my_pid:
                continue
            if is_target_bot_process(p):
                p.kill()
                print(f"Terminated WeChat Bot Dev PID: {p.pid}")
        except Exception:
            pass

if __name__ == "__main__":
    kill_wechat_bot()
