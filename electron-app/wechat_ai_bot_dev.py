"""Legacy entry point: run the reply engine without any UI.

The original implementation lived in this file. It now forwards to the Python engine
package in electron-app/wechat_engine/, so `python wechat_ai_bot_dev.py` keeps working
for anyone scripting the daemon directly. The behaviour is the 2.0 engine: same Win32
message routing and OCR perception, with the safety fixes listed in CHANGELOG.md.

Prefer `npm run dev` (or the installed app) when you want the graphical control centre.
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
else:
    sys.stderr.write(
        "找不到 wechat_engine 包。请在仓库根目录运行，"
        "或先执行 pip install -e electron-app。\n"
    )
    raise SystemExit(2)

from wechat_engine.runner import run_headless  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run_headless(sys.argv[1:]))
