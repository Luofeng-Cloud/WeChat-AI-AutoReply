"""CLI contract: stdout must carry exactly one JSON document.

Both of these were real bugs: the one-shot CLI printed the daemon banner first, and
the --call option string was bound as the method name so it quietly started a daemon
instead. A test that reads stdout as JSON catches both.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_cli(args, timeout=90):
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "wechat_engine", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


class TestCallMode:
    def test_call_prints_only_the_result_object(self, data_dir, monkeypatch):
        monkeypatch.setenv("WECHAT_ENGINE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
        completed = run_cli(["--call", "ping"])
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip())
        assert payload["pong"] is True

    def test_call_does_not_emit_protocol_notifications(self, data_dir, monkeypatch):
        monkeypatch.setenv("WECHAT_ENGINE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
        completed = run_cli(["--call", "ping"])
        assert "engine.ready" not in completed.stdout
        assert "jsonrpc" not in completed.stdout

    def test_call_accepts_json_params(self, data_dir, monkeypatch):
        monkeypatch.setenv("WECHAT_ENGINE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
        completed = run_cli(["--call", "naming_check", '{"contact": "3张三", "whitelist": ["张三"]}'])
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip())
        assert payload["matched"] is True
        assert payload["canonical"] == "张三"

    def test_unknown_method_fails_loudly(self, data_dir, monkeypatch):
        monkeypatch.setenv("WECHAT_ENGINE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
        completed = run_cli(["--call", "no_such_method"])
        assert completed.returncode != 0
        payload = json.loads(completed.stdout.strip())
        assert payload["ok"] is False

    def test_missing_method_name_is_reported(self, data_dir, monkeypatch):
        monkeypatch.setenv("WECHAT_ENGINE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
        completed = run_cli(["--call"])
        assert completed.returncode != 0


class TestSelftest:
    def test_selftest_reports_without_crashing(self, data_dir, monkeypatch):
        monkeypatch.setenv("WECHAT_ENGINE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
        completed = run_cli(["--selftest"])
        assert completed.returncode == 0, completed.stderr
        report = json.loads(completed.stdout)
        assert report["ok"] is True
        assert "sitePackages" in report["diagnostics"]
