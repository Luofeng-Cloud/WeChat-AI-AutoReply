"""Configuration store: schema, defaults, migration, atomic persistence.

Defects this module removes from the 1.x design:

* **Whole-file clobber.** wechat_bot_gui_dev.py kept a full in-memory copy of the
  config and wrote all of it back on every save. Any key the GUI did not know about
  (friend_personas, custom_prompt, saved_window_rect) was silently dropped the moment
  the user clicked "save" in an unrelated tab. Here, save() merges over the latest
  on-disk document instead of overwriting it.
* **Non-atomic writes.** A crash or a concurrent reader could observe a truncated
  JSON file, after which load_config() returned defaults and the user's API key
  appeared to vanish. Writes now go to a sibling temp file plus os.replace().
* **Read/write races.** The daemon read the file on every poll while the GUI wrote it,
  so Windows could raise a sharing violation. Reads retry with backoff and fall back
  to the last known good snapshot.
* **Silent type drift.** A user typing "abc" into a spinbox stored a string where a
  float was expected. Values are now coerced and clamped on write.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import logutil, paths

CONFIG_VERSION = 2

# ---------------------------------------------------------------------------
# Persona presets (verbatim from the 1.x GUI so saved selections keep matching)
# ---------------------------------------------------------------------------
PROMPT_PRESET_ORDER = (
    "🌟 真人日常 (默认)",
    "🤣 随和幽默 / 接梗达人",
    "🌹 暧昧推拉 / 情感高手",
    "💕 甜蜜恋人 / 女友专属",
    "😂 毒舌互怼 / 死党互损",
    "⚔️ 阴阳怪气 / 仇敌对线",
    "💼 职场专业 / 严谨得体",
    "⏳ 忙碌代接 / 稍后细聊",
    "🧊 高冷简洁 / 话少字精",
    "☕ 温和体贴 / 治愈倾听",
    "✨ 元气可爱 / 活泼软萌",
    "✏️ 自由定制",
)

CUSTOM_PRESET_KEY = "✏️ 自由定制"
DEFAULT_PRESET_KEY = "🌟 真人日常 (默认)"

DEFAULT_PROMPTS = {
    "🌟 真人日常 (默认)": "你现在是微信账号的主人（真实本人）。请以我（主人本人）的第一人称、自然口语、亲切随和的日常语气直接回复微信好友的消息。就像我本人在用手机打字聊天一样自然。严禁透露任何AI、机器人或助理身份，严禁使用客套机械的客服腔，严禁输出任何解释或markdown格式，直接输出我回复的那句话。",
    "🤣 随和幽默 / 接梗达人": "你现在是微信账号的主人（真实本人）。请以极具幽默感、机智开朗、会接梗的真人语气回复好友。善于敏锐捕捉对方话里的笑点与细节，秒懂热梗并顺势幽默抛梗接招，说话风趣机敏、轻松洒脱、情商极高，让人觉得和你聊天特别有意思、超解压。严禁严肃机械，严禁使用客服腔，严禁透露AI身份，直接输出幽默自然的回复短句。",
    "🌹 暧昧推拉 / 情感高手": "你现在是真实号主本人，正在回复暧昧对象。语气要有高情商的推拉感和适度的情感张力，既幽默风趣又带一点恰到好处的撩人和嘴甜。懂分寸、不油腻、欲擒故纵，善于发现对方话里的细节并给出有趣的互动。严禁生硬直白，严禁透露AI身份，直接输出真实聊天的短句。",
    "💕 甜蜜恋人 / 女友专属": "你现在是我的替身，正在回复我的女朋友/老婆。请以真实男友的第一人称回复，语气超级宠溺、温柔、体贴且求生欲拉满。多用爱称（如宝贝、宝宝、乖等），多给足情绪价值，关心她的状态并主动报备，说话带点撒娇和甜意，严禁任何直男式说教或敷衍，严禁透露AI身份，直接输出日常打字的一两句话。",
    "😂 毒舌互怼 / 死党互损": "你现在是真实号主本人，正在回复多年的铁哥们/死党。语气要毒舌、嘴硬、爱开玩笑互损，毫不留情面地疯狂吐槽和接招，对方发什么都要幽默地怼回去或调侃两句，满嘴跑火车但感情很铁。严禁客套礼貌，严禁使用敬语，严禁透露AI身份，直接输出回怼的微信短句。",
    "⚔️ 阴阳怪气 / 仇敌对线": "你现在是真实号主本人，正在回复讨厌的人或仇敌。语气要高冷、带有极具杀伤力的高级阴阳怪气与不屑。回怼精准有力、直击要害、体面扎心且绝不吃亏，不带脏字却能把对方气得半死。严禁客套，严禁热情，严禁透露AI身份，直接输出冷嘲热讽的回复。",
    "💼 职场专业 / 严谨得体": "你现在是微信账号的主人（真实本人）。请以成熟稳重、严谨得体、高效专业的职场沟通语气回复消息。态度谦逊有礼，表达逻辑清晰、条理分明，用词规范严谨，展现出极高的职业素养。严禁轻浮玩笑，严禁透露AI身份，直接输出专业得体的回复。",
    "⏳ 忙碌代接 / 稍后细聊": "你现在是微信账号的主人（真实本人）。请以短平快的日常语气回复好友，告知自己当前正处于忙碌状态（如正在开会、开车或手头有急事），先简要确认已收到消息，并主动说明稍后空下来立刻详细回复。态度亲切真诚，严禁透露AI身份，直接输出1句话。",
    "🧊 高冷简洁 / 话少字精": "你现在是微信账号的主人（真实本人）。请以高冷、极简、话少字精的语气回复好友。回答直奔主题，通常只用几个字或一小句话（如“好”、“收到”、“晚点说”、“行”），绝不多说半句废话，言简意赅。严禁热情啰嗦，严禁透露AI身份，直接输出极短的微信回复。",
    "☕ 温和体贴 / 治愈倾听": "你现在是微信账号的主人（真实本人）。请以温暖、沉稳、善解人意且富有同理心的语气回复好友。认真倾听对方的心情，给予充分的理解、肯定与情绪支撑，让人感到被重视和治愈。严禁冷淡评判或讲大道理，严禁透露AI身份，直接输出温暖真诚的话。",
    "✨ 元气可爱 / 活泼软萌": "你现在是微信账号的主人（真实本人）。请以元气满满、甜美活泼、软萌可爱的语气回复好友。说话积极开朗，适当带上可爱的语气助词（如呀、呢、哇、啦、~、哈哈等），像一个阳光充满活力的小太阳。严禁死板严肃，严禁透露AI身份，直接输出充满元气的可爱短句。",
    "✏️ 自由定制": "你现在是微信账号的主人（真实本人）。请以我本人的第一人称、自然随和的语气直接回复好友，直接输出回复内容。",
}

DEFAULT_BLACKLIST = [
    "微信团队",
    "文件传输助手",
    "腾讯新闻",
    "微信支付",
    "服务通知",
    "订阅号消息",
]

DEFAULT_IGNORED_KEYWORDS = [
    "按住鼠标",
    "语音输入文字",
    "按住说话",
    "按Enter发送",
    "发送(S)",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "ai_engine": "openai_api",
    "openai_api": {
        "api_key": "",
        "api_base": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.6-flash",
        "temperature": 0.7,
        "timeout_seconds": 12.0,
        "max_retries": 2,
    },
    "system_prompt": DEFAULT_PROMPTS[DEFAULT_PRESET_KEY],
    "prompt_preset": DEFAULT_PRESET_KEY,
    "custom_prompt": DEFAULT_PROMPTS[CUSTOM_PRESET_KEY],
    "reply_prefix": "[AI自动回复] ",
    "include_prefix": False,
    "whitelist_mode": True,
    "whitelist": [],
    "blacklist": list(DEFAULT_BLACKLIST),
    "friend_personas": {},
    "reply_delay_seconds": 0.0,
    "reply_delay_jitter_seconds": 0.0,
    "check_interval_seconds": 0.8,
    "settle_delay_seconds": 0.35,
    "auto_focus_wechat": False,
    "ignored_keywords": list(DEFAULT_IGNORED_KEYWORDS),
    "saved_window_rect": [100, 100, 1100, 750],
    "offscreen": False,
    "input_injection_mode": "auto",
    "max_history_turns": 8,
    "log_level": "info",
    "ui": {"theme": "dark", "compact": False, "log_tail_lines": 500},
}

# Bounds keep a typo in a spinbox from turning into a busy loop or a 10-minute stall.
_NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "reply_delay_seconds": (0.0, 120.0),
    "reply_delay_jitter_seconds": (0.0, 60.0),
    "check_interval_seconds": (0.1, 60.0),
    "settle_delay_seconds": (0.0, 10.0),
    "max_history_turns": (0.0, 40.0),
}

_INJECTION_MODES = ("auto", "foreground", "background")

LIST_FIELDS = ("whitelist", "blacklist", "ignored_keywords")


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into a copy of base.

    Lists are replaced wholesale (a whitelist edit is an intentional replacement),
    dicts are merged key-by-key so a partial update cannot drop sibling settings.
    """
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def _as_float(value, fallback):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if result != result or result in (float("inf"), float("-inf")):  # NaN / inf
        return float(fallback)
    return result


def _as_bool(value, fallback):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开启")
    return fallback


def _as_str_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def migrate(raw: dict) -> dict:
    """Bring a version 1.x document up to the current schema.

    Every 1.x key that the GUI understood is preserved. Keys the daemon used but the
    GUI never surfaced (friend_personas) are preserved too, which is what the old
    whole-file save silently destroyed.
    """
    if not isinstance(raw, dict):
        return default_config()

    cfg = default_config()
    version = raw.get("config_version")
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1

    # --- openai_api: flat legacy keys were supported by the daemon ----------
    api = dict(cfg["openai_api"])
    legacy_api = raw.get("openai_api")
    if isinstance(legacy_api, dict):
        api.update({k: v for k, v in legacy_api.items() if v is not None})
    for legacy_key, target in (("api_key", "api_key"), ("api_url", "api_base"), ("api_model", "model")):
        if raw.get(legacy_key):
            api[target] = raw[legacy_key]
    api["api_key"] = str(api.get("api_key") or "")
    api["api_base"] = str(api.get("api_base") or DEFAULT_CONFIG["openai_api"]["api_base"]).strip()
    api["model"] = str(api.get("model") or DEFAULT_CONFIG["openai_api"]["model"]).strip()
    api["temperature"] = min(2.0, max(0.0, _as_float(api.get("temperature"), 0.7)))
    api["timeout_seconds"] = min(300.0, max(1.0, _as_float(api.get("timeout_seconds"), 12.0)))
    try:
        api["max_retries"] = int(min(8, max(0, int(api.get("max_retries", 2)))))
    except (TypeError, ValueError):
        api["max_retries"] = 2
    cfg["openai_api"] = api

    for key in ("system_prompt", "custom_prompt", "prompt_preset", "reply_prefix", "ai_engine"):
        if raw.get(key) is not None:
            cfg[key] = str(raw[key])

    for key in ("include_prefix", "whitelist_mode", "auto_focus_wechat", "offscreen"):
        if key in raw:
            cfg[key] = _as_bool(raw.get(key), cfg[key])

    for key in LIST_FIELDS:
        if key in raw:
            cfg[key] = _as_str_list(raw.get(key))

    for key in _NUMERIC_BOUNDS:
        if key in raw:
            low, high = _NUMERIC_BOUNDS[key]
            value = _as_float(raw.get(key), cfg[key])
            cfg[key] = min(high, max(low, value))
    cfg["max_history_turns"] = int(cfg["max_history_turns"])

    if isinstance(raw.get("friend_personas"), dict):
        cfg["friend_personas"] = {
            str(k): str(v) for k, v in raw["friend_personas"].items() if str(k).strip()
        }

    rect = raw.get("saved_window_rect")
    if isinstance(rect, (list, tuple)) and len(rect) == 4:
        try:
            values = [int(v) for v in rect]
        except (TypeError, ValueError):
            values = None
        if values and values[2] >= 320 and values[3] >= 240:
            cfg["saved_window_rect"] = values

    if isinstance(raw.get("ui"), dict):
        cfg["ui"] = deep_merge(cfg["ui"], raw["ui"])

    if raw.get("input_injection_mode") in _INJECTION_MODES:
        cfg["input_injection_mode"] = raw["input_injection_mode"]

    if raw.get("log_level") in ("debug", "info", "warn", "error"):
        cfg["log_level"] = raw["log_level"]

    cfg["config_version"] = CONFIG_VERSION
    return cfg


def validate(cfg: dict) -> dict:
    """Coerce and clamp a document. Returns a new document; never raises.

    Used by the JSON-RPC layer so a renderer bug cannot persist nonsense.

    The document must win over the defaults: merging the other way round silently reset
    any key already present in the defaults back to its default value on every save, so
    a custom prompt or a saved window rectangle was discarded by an unrelated edit.
    """
    merged = migrate(deep_merge(default_config(), cfg or {}))
    # migrate() already starts from the defaults and overlays the document, so the
    # values above survive; the merge here only fills in keys migrate() did not know.
    # A cleared prompt must fall back to the default rather than reach the model
    # as an empty system message.
    if not str(merged.get("system_prompt") or "").strip():
        merged["system_prompt"] = DEFAULT_PROMPTS[DEFAULT_PRESET_KEY]
        merged["prompt_preset"] = DEFAULT_PRESET_KEY
    if merged.get("prompt_preset") not in DEFAULT_PROMPTS:
        merged["prompt_preset"] = CUSTOM_PRESET_KEY
    if not str(merged.get("reply_prefix") or "").strip():
        merged["reply_prefix"] = "[AI自动回复] "
    return merged


class ConfigStore:
    """Thread-safe, atomically persisted configuration."""

    def __init__(self, path=None, autosave: bool = True, watch_interval: float = 1.0):
        self.path = Path(path) if path is not None else paths.config_file()
        self._lock = threading.RLock()
        self._autosave = autosave
        self._watch_interval = watch_interval
        self._data = default_config()
        self._mtime_ns = 0
        self._last_check = 0.0
        self._degraded = False
        self.load(initial=True)

    # -- reading ----------------------------------------------------------
    def load(self, initial: bool = False) -> dict:
        """Read from disk, migrating as needed. Never raises."""
        with self._lock:
            data = None
            if self.path.exists():
                data = self._read_json_with_retry(self.path)
            if data is None and initial:
                # One-time import of a pre-2.0 config sitting next to the scripts.
                for candidate in paths.legacy_config_candidates():
                    if candidate.resolve() == self.path.resolve():
                        continue
                    data = self._read_json_with_retry(candidate)
                    if data is not None:
                        logutil.log("📦 [配置迁移] 已从 %s 导入旧配置" % candidate)
                        break
            if data is None:
                if initial:
                    # First run: seed the file so the UI can show a real path and the
                    # daemon cannot race a "does it exist yet" check.
                    self._data = default_config()
                    self._manages_own_file = True
                    self.save()
                    return copy.deepcopy(self._data)
                return copy.deepcopy(self._data)

            self._data = migrate(data)
            self._touch_mtime()
            self._degraded = False
            if initial and not self.path.exists():
                # A migrated 1.x document lives elsewhere; materialise it at the
                # current path so every later read hits the same file.
                self.save()
            return copy.deepcopy(self._data)

    def _touch_mtime(self) -> None:
        try:
            self._mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime_ns = 0

    @staticmethod
    def _read_json_with_retry(path: Path, attempts: int = 4):
        """Read a JSON document, tolerating a writer holding the file open."""
        last_error = None
        for attempt in range(attempts):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except FileNotFoundError:
                return None
            except json.JSONDecodeError as exc:
                last_error = exc
                # A half-written file is the classic failure; a short wait usually
                # lands after os.replace() completes.
                time.sleep(0.05 * (attempt + 1))
            except OSError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        logutil.get_bus().warn("⚠️ [配置读取失败] %s (%s)，沿用内存中的配置" % (path, last_error))
        return None

    def maybe_reload(self) -> bool:
        """Poll for external edits (a user editing the JSON by hand)."""
        now = time.time()
        if now - self._last_check < self._watch_interval:
            return False
        self._last_check = now
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return False
        if mtime == self._mtime_ns:
            return False
        self.load()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    # -- writing ----------------------------------------------------------
    def update(self, patch: dict, source: str = "ui") -> dict:
        """Deep-merge a partial document and persist it if anything changed."""
        if not isinstance(patch, dict):
            raise TypeError("config patch must be an object")
        with self._lock:
            # Re-read first so a concurrent writer is not clobbered.
            if self.path.exists():
                disk = self._read_json_with_retry(self.path)
                if isinstance(disk, dict):
                    self._data = migrate(disk)
            merged = validate(deep_merge(self._data, patch))
            changed = merged != self._data
            self._data = merged
            if changed and self._autosave:
                self.save()
            return copy.deepcopy(self._data)

    def replace(self, document: dict) -> dict:
        with self._lock:
            self._data = validate(document or {})
            if self._autosave:
                self.save()
            return copy.deepcopy(self._data)

    def save(self) -> bool:
        """Atomically persist. Returns True on success."""
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, indent=4, sort_keys=False)
            tmp_path = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
                )
                tmp_path = Path(tmp_name)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self.path)
                self._touch_mtime()
                self._degraded = False
                return True
            except Exception as exc:
                logutil.get_bus().error("❌ [配置保存失败] %s" % exc)
                if tmp_path is not None:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                self._degraded = True
                return False

    @property
    def degraded(self) -> bool:
        """True when the most recent save failed (disk full, ACL, read-only)."""
        with self._lock:
            return self._degraded

    def masked(self) -> dict:
        """Snapshot with the API key partially hidden, for diagnostics exports."""
        data = self.snapshot()
        api = data.get("openai_api") or {}
        key = str(api.get("api_key") or "")
        if len(key) > 10:
            api["api_key"] = key[:6] + "…" + key[-4:]
        elif key:
            api["api_key"] = "…"
        data["openai_api"] = api
        return data


_STORE = None
_STORE_LOCK = threading.Lock()


def get_store() -> ConfigStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ConfigStore()
        return _STORE


def reset_store_for_tests(store=None) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = store
