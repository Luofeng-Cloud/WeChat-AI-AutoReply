"""Configuration: defaults, migration, atomic writes and merge semantics."""

from __future__ import annotations

import json
import threading
import time

import pytest

from wechat_engine import config as config_mod
from tests.conftest import ROOT


class TestDefaults:
    def test_defaults_are_complete(self):
        cfg = config_mod.default_config()
        for key in (
            "openai_api", "system_prompt", "whitelist_mode", "whitelist", "blacklist",
            "friend_personas", "check_interval_seconds", "reply_prefix",
        ):
            assert key in cfg

    def test_preset_count_is_preserved(self):
        """The 1.x GUI shipped 12 personas; dropping one would be a feature regression."""
        assert len(config_mod.DEFAULT_PROMPTS) == 12
        assert config_mod.DEFAULT_PRESET_KEY in config_mod.DEFAULT_PROMPTS
        assert config_mod.CUSTOM_PRESET_KEY in config_mod.DEFAULT_PROMPTS

    def test_preset_order_covers_every_prompt(self):
        assert set(config_mod.PROMPT_PRESET_ORDER) == set(config_mod.DEFAULT_PROMPTS)

    def test_deep_merge_keeps_siblings(self):
        merged = config_mod.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}})
        assert merged == {"a": {"b": 9, "c": 2}}

    def test_deep_merge_replaces_lists(self):
        merged = config_mod.deep_merge({"list": [1, 2, 3]}, {"list": [9]})
        assert merged["list"] == [9]


class TestMigration:
    def test_version_1_document_is_upgraded(self):
        legacy = {
            "ai_engine": "openai_api",
            "openai_api": {"api_key": "sk-test", "api_base": "https://x/v1", "model": "m"},
            "whitelist_mode": True,
            "whitelist": ["张三"],
            "friend_personas": {"张三": "死党模式"},
            "check_interval_seconds": 0.5,
        }
        migrated = config_mod.migrate(legacy)
        assert migrated["config_version"] == config_mod.CONFIG_VERSION
        assert migrated["openai_api"]["api_key"] == "sk-test"
        assert migrated["friend_personas"] == {"张三": "死党模式"}
        assert migrated["whitelist"] == ["张三"]

    def test_flat_legacy_api_keys_are_folded_in(self):
        legacy = {"api_key": "k", "api_url": "https://y/v1", "api_model": "m2"}
        migrated = config_mod.migrate(legacy)
        assert migrated["openai_api"]["api_key"] == "k"
        assert migrated["openai_api"]["api_base"] == "https://y/v1"
        assert migrated["openai_api"]["model"] == "m2"

    def test_numeric_strings_are_coerced(self):
        migrated = config_mod.migrate({"check_interval_seconds": "1.5", "reply_delay_seconds": "2"})
        assert migrated["check_interval_seconds"] == 1.5
        assert migrated["reply_delay_seconds"] == 2.0

    def test_invalid_numbers_fall_back_to_defaults(self):
        migrated = config_mod.migrate({"check_interval_seconds": "abc", "reply_delay_seconds": None})
        assert migrated["check_interval_seconds"] == config_mod.DEFAULT_CONFIG["check_interval_seconds"]
        assert migrated["reply_delay_seconds"] == 0.0

    def test_out_of_range_values_are_clamped(self):
        migrated = config_mod.migrate({"check_interval_seconds": 0.0, "reply_delay_seconds": 9999})
        assert migrated["check_interval_seconds"] == 0.1
        assert migrated["reply_delay_seconds"] == 120.0

    def test_string_booleans_are_parsed(self):
        assert config_mod.migrate({"whitelist_mode": "false"})["whitelist_mode"] is False
        assert config_mod.migrate({"whitelist_mode": "true"})["whitelist_mode"] is True

    def test_list_fields_are_cleaned(self):
        migrated = config_mod.migrate({"whitelist": [" 张三 ", "", None, "张三", 42]})
        assert migrated["whitelist"] == ["张三", "42"]

    def test_bad_window_rect_is_rejected(self):
        migrated = config_mod.migrate({"saved_window_rect": [0, 0, 10, 10]})
        assert migrated["saved_window_rect"] == config_mod.DEFAULT_CONFIG["saved_window_rect"]

    def test_garbage_input_yields_defaults(self):
        assert config_mod.migrate("not a dict") == config_mod.default_config()
        assert config_mod.migrate(None) == config_mod.default_config()


class TestValidation:
    def test_empty_prompt_falls_back(self):
        validated = config_mod.validate({"system_prompt": "   "})
        assert validated["system_prompt"] == config_mod.DEFAULT_PROMPTS[config_mod.DEFAULT_PRESET_KEY]

    def test_unknown_preset_becomes_custom(self):
        validated = config_mod.validate({"prompt_preset": "不存在的预设"})
        assert validated["prompt_preset"] == config_mod.CUSTOM_PRESET_KEY

    def test_empty_prefix_is_restored(self):
        validated = config_mod.validate({"reply_prefix": ""})
        assert validated["reply_prefix"] == "[AI自动回复] "


class TestStore:
    def test_store_creates_a_file_on_first_use(self, data_dir):
        path = data_dir / "cfg.json"
        config_mod.ConfigStore(path=path)
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == config_mod.CONFIG_VERSION
        assert path.read_text(encoding="utf-8").startswith("{")

    def test_partial_update_preserves_unknown_keys(self, store):
        """The 1.x GUI destroyed friend_personas whenever any other tab was saved."""
        store.update({"friend_personas": {"张三": "死党"}, "whitelist": ["张三"]})
        store.update({"reply_delay_seconds": 1.5})
        snapshot = store.snapshot()
        assert snapshot["friend_personas"] == {"张三": "死党"}
        assert snapshot["whitelist"] == ["张三"]
        assert snapshot["reply_delay_seconds"] == 1.5

    def test_file_on_disk_reflects_updates(self, store):
        store.update({"whitelist": ["李四"]})
        on_disk = json.loads(store.path.read_text(encoding="utf-8"))
        assert on_disk["whitelist"] == ["李四"]

    def test_corrupt_file_does_not_raise(self, data_dir):
        path = data_dir / "cfg.json"
        path.write_text("{ this is not json", encoding="utf-8")
        loaded = config_mod.ConfigStore(path=path)
        assert loaded.snapshot()["config_version"] == config_mod.CONFIG_VERSION

    def test_no_temporary_files_are_left_behind(self, store):
        for index in range(5):
            store.update({"reply_delay_seconds": index})
        leftovers = [p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_concurrent_updates_do_not_lose_keys(self, data_dir):
        """Two writers updating different keys must both land."""
        path = data_dir / "cfg.json"
        first = config_mod.ConfigStore(path=path)
        second = config_mod.ConfigStore(path=path)

        def writer(instance, patch):
            for _ in range(5):
                instance.update(patch)
                time.sleep(0.001)

        threads = [
            threading.Thread(target=writer, args=(first, {"reply_prefix": "[A] "})),
            threading.Thread(target=writer, args=(second, {"saved_window_rect": [1, 2, 900, 700]})),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = json.loads(path.read_text(encoding="utf-8"))
        assert final["reply_prefix"] == "[A] "
        assert final["saved_window_rect"] == [1, 2, 900, 700]

    def test_masked_hides_the_api_key(self, store):
        store.update({"openai_api": {"api_key": "sk-abcdefghijklmnop"}})
        masked = store.masked()
        assert "abcdefghijklmnop" not in json.dumps(masked)
        assert store.snapshot()["openai_api"]["api_key"] == "sk-abcdefghijklmnop"

    def test_maybe_reload_picks_up_external_edits(self, store):
        store.update({"reply_prefix": "[old] "})
        document = json.loads(store.path.read_text(encoding="utf-8"))
        document["reply_prefix"] = "[new] "
        store.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        store._watch_interval = 0
        # Windows file timestamps have coarse granularity; force a distinct value.
        import os
        import time as time_module
        stat = store.path.stat()
        os.utime(store.path, (stat.st_atime, stat.st_mtime + 2))
        assert store.maybe_reload() is True
        assert store.snapshot()["reply_prefix"] == "[new] "

    def test_legacy_config_is_imported_once(self, data_dir, monkeypatch, tmp_path):
        """A 1.x config sitting next to the scripts is picked up on first run."""
        monkeypatch.delenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", raising=False)
        legacy = ROOT / "wechat_config_dev.json"
        legacy.write_text(json.dumps({"reply_prefix": "[legacy] "}), encoding="utf-8", newline="")
        try:
            target = tmp_path / "moved" / "wechat_config_dev.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            imported = config_mod.ConfigStore(path=target)
            assert imported.snapshot()["reply_prefix"] == "[legacy] "
            # The migrated document is materialised at the canonical path so later
            # reads do not depend on the original file still existing.
            assert target.is_file()
            assert json.loads(target.read_text(encoding="utf-8"))["reply_prefix"] == "[legacy] "
        finally:
            if legacy.exists():
                legacy.unlink()

    def test_legacy_import_can_be_disabled(self, data_dir, monkeypatch, tmp_path):
        monkeypatch.setenv("WECHAT_ENGINE_DISABLE_LEGACY_IMPORT", "1")
        legacy = ROOT / "wechat_config_dev.json"
        legacy.write_text(json.dumps({"reply_prefix": "[legacy] "}), encoding="utf-8", newline="")
        try:
            target = tmp_path / "other" / "wechat_config_dev.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            store = config_mod.ConfigStore(path=target)
            assert store.snapshot()["reply_prefix"] == config_mod.DEFAULT_CONFIG["reply_prefix"]
        finally:
            if legacy.exists():
                legacy.unlink()
