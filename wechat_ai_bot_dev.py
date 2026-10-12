"""Legacy entry point (repository root).

Forwards to the engine package under electron-app/. Kept so existing instructions and
scripts that call `python wechat_ai_bot_dev.py` continue to work.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENGINE_HOME = os.path.join(ROOT, "electron-app")
if os.path.isdir(os.path.join(ENGINE_HOME, "wechat_engine")):
    sys.path.insert(0, ENGINE_HOME)

from wechat_engine.runner import run_headless  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run_headless(sys.argv[1:]))
