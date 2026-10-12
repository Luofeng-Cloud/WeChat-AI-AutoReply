"""Filesystem layout shared by every entry point.

Historically each script recomputed BASE_DIR and then re-derived five paths from it,
which drifted apart: the GUI wrote wechat_config_dev.json while the README told users
to create wechat_config.json.

Runtime files live next to the executable for a normal install, but inside
%LOCALAPPDATA%/WeChat-AI-AutoReply when the install directory is not writable
(Program Files, or a read-only packaged bundle). That indirection lets the packaged
Electron build keep user data out of resources/.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "WeChat-AI-AutoReply"

CONFIG_FILENAME = "wechat_config_dev.json"
CONVO_MAP_FILENAME = "wechat_convo_map.json"
LOG_FILENAME = "wechat_bot_dev.log"
PID_FILENAME = "wechat_bot_dev.pid"
MODELS_DIRNAME = "models"


def bundle_dir() -> Path:
    """Directory holding the engine's own assets (models, ico files)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def install_dir() -> Path:
    """Where the app was installed / cloned."""
    override = os.environ.get("WECHAT_ENGINE_APP_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def data_dir() -> Path:
    """Writable directory for config, logs and runtime state."""
    override = os.environ.get("WECHAT_ENGINE_DATA_DIR")
    if override:
        target = Path(override).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        return target

    local = install_dir()
    if _is_writable(local):
        return local

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        target = Path(base) / APP_DIR_NAME
    else:
        target = Path.home() / ("." + APP_DIR_NAME)
    target.mkdir(parents=True, exist_ok=True)
    return target


def models_dir() -> Path:
    """Preferred location of the bundled PP-OCR ONNX models."""
    for candidate in (data_dir() / MODELS_DIRNAME, bundle_dir() / MODELS_DIRNAME):
        if candidate.is_dir():
            return candidate
    return data_dir() / MODELS_DIRNAME


def config_file() -> Path:
    return data_dir() / CONFIG_FILENAME


def convo_map_file() -> Path:
    return data_dir() / CONVO_MAP_FILENAME


def log_file() -> Path:
    return data_dir() / LOG_FILENAME


def pid_file() -> Path:
    return data_dir() / PID_FILENAME


def icon_file():
    """First icon that actually exists, in descending preference."""
    names = (
        "wechat_sky_blue_unread_ai_v4.ico",
        "wechat_sky_blue_v2.ico",
        "wechat_sky_blue_ai_v3.ico",
        "wechat_blue.ico",
    )
    for base in (data_dir(), install_dir(), bundle_dir()):
        for name in names:
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


def legacy_config_candidates():
    """Config paths written by version 1.x, newest first.

    Set WECHAT_ENGINE_DISABLE_LEGACY_IMPORT=1 to suppress this. The test suite relies on
    it because a developer machine running the app does have a real config file next to
    the scripts, and importing it inside a test made the result depend on the machine.
    """
    if os.environ.get("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT") == "1":
        return []
    seen = []
    for base in (install_dir(), data_dir()):
        for name in ("wechat_config_dev.json", "wechat_config.json"):
            candidate = base / name
            if candidate.is_file() and candidate not in seen:
                seen.append(candidate)
    return seen
