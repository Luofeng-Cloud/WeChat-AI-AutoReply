"""Legacy entry point (repository root). Delegates to electron-app/kill_wechat_bot_dev.py."""

from __future__ import annotations

import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(ROOT, "electron-app", "kill_wechat_bot_dev.py")

if not os.path.isfile(TARGET):
    sys.stderr.write("找不到 electron-app/kill_wechat_bot_dev.py\n")
    raise SystemExit(2)

runpy.run_path(TARGET, run_name="__main__")
