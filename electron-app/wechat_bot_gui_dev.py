"""Legacy entry point: launch the control centre.

The Tkinter control centre has been replaced by the Electron application. This shim
starts it, or explains what to install when the Node side has not been built yet.

Run `npm install && npm run dev` inside electron-app/ for a development launch, or
install the packaged app for normal use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "electron-app")


def _main() -> int:
    if not os.path.isdir(APP_DIR):
        sys.stderr.write("找不到 electron-app 目录。\n")
        return 2

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        sys.stderr.write(
            "控制中心现在是 Electron 应用，需要 Node.js 20+。\n"
            "请安装 Node.js，或在 Releases 页面下载 Windows 安装包直接使用。\n"
        )
        return 3

    if not os.path.isdir(os.path.join(APP_DIR, "node_modules")):
        sys.stderr.write("首次运行需要安装前端依赖，正在执行 npm install ...\n")
        install = subprocess.call([npm, "install"], cwd=APP_DIR)
        if install != 0:
            return install

    sys.stderr.write("正在启动 Electron 控制中心 ...\n")
    return subprocess.call([npm, "run", "dev"], cwd=APP_DIR)


if __name__ == "__main__":
    raise SystemExit(_main())
