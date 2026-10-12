"""Shared fixtures. Tests must never touch the real user configuration."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Point the engine at a scratch directory instead of %LOCALAPPDATA%."""
    target = tmp_path / "data"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WECHAT_ENGINE_DATA_DIR", str(target))
    monkeypatch.setenv("WECHAT_ENGINE_APP_DIR", str(ROOT))
    # Never import the developer's real config into a test.
    monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
    # The engine keeps one process-wide ConfigStore. Without this reset a test would
    # observe whatever directory the previously created store happened to point at,
    # which made results depend on test ordering.
    from wechat_engine import config as config_mod

    config_mod.reset_store_for_tests(None)
    yield target
    config_mod.reset_store_for_tests(None)


@pytest.fixture()
def store(data_dir):
    from wechat_engine.config import ConfigStore

    return ConfigStore(path=data_dir / "wechat_config_dev.json")


@pytest.fixture()
def quiet_log(data_dir):
    """Route the log bus into the scratch directory at warn level."""
    from wechat_engine import logutil

    bus = logutil.configure(path=data_dir / "test.log", level="error")
    yield bus
    bus.close()
