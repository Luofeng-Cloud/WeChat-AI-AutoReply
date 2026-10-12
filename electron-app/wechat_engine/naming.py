"""Contact-name matching and the whitelist / blacklist firewall.

The matching rules are the safety core of the project: a false positive replies to the
wrong person, a false negative leaves a friend unanswered. Version 1.x had three
problems worth naming.

1. Digit stripping corrupted real names. It always removed a leading 1-2 digit run so
   that a docked unread badge would not break matching, which meant a whitelisted friend
   called "3号仓库" could never be matched, while "1号" could match "2号".
2. Prefix matching lacked a reliable ambiguity guard. Whitelist "张三" matched the group
   "张三的群聊" unless the tail happened to be short, and the guard compared a normalised
   haystack against a raw needle, so the offset it computed was meaningless.
3. OCR look-alike folding was unconditional. Mapping 0 to o and 1 to l before any exact
   comparison made "E0" and "Eo" the same contact.

The rewrite keeps every legitimate tolerance but layers them by confidence:

* tier 1 - exact match after only whitespace / punctuation removal and case folding;
* tier 2 - prefix match, with an explicit ambiguity guard;
* tier 3 - truncated OCR (one missing trailing character);
* tier 4 - OCR look-alike folding, used only when nothing above matched.

Leading digits are stripped as a *fallback variant* rather than replacing the original,
so "3张三" still matches "张三" while an honest "3号" keeps matching itself.
"""

from __future__ import annotations

import re

# Group-chat markers. Any of these in the *name segment* of a conversation row means the
# row is not a single friend. The name segment excludes the message preview, so a
# preview such as "一共(3)个文件" cannot trigger a false group classification.
GROUP_MARKERS = (
    "群聊",
    "交流群",
    "互助群",
    "通知群",
    "工作群",
    "家族群",
    "同乡群",
    "部门群",
    "项目群",
    "班级群",
    "业主群",
    "校友群",
    "拼车群",
    "团购群",
    "公众号",
    "订阅号",
    "服务号",
    "企业微信",
)

# A trailing "(3)" or "[3条]" on the name segment also marks a group.
GROUP_SUFFIX_RE = re.compile(r"[\\(（]\d+[\\)）]\s*$|\[\d+条\]")

SYSTEM_SENDER_NAMES = {
    "微信",
    "搜索",
    "文件传输助手",
    "朋友圈",
    "订阅号",
    "订阅号消息",
    "服务通知",
    "腾讯新闻",
    "微信团队",
    "微信支付",
    "小程序",
    "视频号",
    "企业微信",
    "微信运动",
}

_PUNCT_RE = re.compile(r"[\^_~\-.,'\"`*#@!:;|/\\()\[\]{}<>+=?%$&]")
_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_DIGITS_RE = re.compile(r"^\d{1,2}(?=\D)")
_NAME_SEGMENT_RE = re.compile(r"\s+")
_TOKEN_START_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")

_OCR_FOLD = str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s", "8": "b", "9": "g"})


def strip_whitespace(text) -> str:
    return _WHITESPACE_RE.sub("", str(text or ""))


def normalize_basic(text) -> str:
    """Case fold, drop whitespace, drop punctuation. No symbol substitution."""
    if text is None:
        return ""
    lowered = str(text).strip().lower()
    return _PUNCT_RE.sub("", strip_whitespace(lowered))


def normalize_ocr(text) -> str:
    """Aggressive normalisation for OCR look-alike folding."""
    return normalize_basic(text).translate(_OCR_FOLD)


def name_segment(text) -> str:
    """The name part of a conversation row (the row may carry a message preview)."""
    return _NAME_SEGMENT_RE.split(str(text or "").strip(), maxsplit=1)[0]


def looks_like_group(name) -> bool:
    """True when the row name segment carries a group marker.

    Groups are the one thing the project promises never to answer, and group names are
    chosen by users, so a keyword list can never be complete. The trailing-群 rule is the
    backstop for names such as 同学群 that match no keyword. A real person called 李群 has
    only two characters, so requiring three or more keeps people and rejects groups.
    """
    segment = name_segment(name)
    if not segment:
        return False
    if any(marker in segment for marker in GROUP_MARKERS):
        return True
    if GROUP_SUFFIX_RE.search(segment):
        return True
    stripped = segment.rstrip("0123456789 ()（）")
    return len(stripped) >= 3 and stripped.endswith("群")


def is_system_sender(name) -> bool:
    return str(name or "").strip() in SYSTEM_SENDER_NAMES


def candidate_variants(contact_name, allow_leading_digit_strip: bool = True):
    """Name forms to try, most trustworthy first."""
    raw = str(contact_name or "").strip()
    if not raw:
        return []
    variants = [raw]
    if allow_leading_digit_strip:
        stripped = _LEADING_DIGITS_RE.sub("", raw).strip()
        if stripped and len(stripped) >= 2 and stripped != raw:
            variants.append(stripped)
    return variants


def _ambiguous_prefix(raw_variant: str, raw_target: str) -> bool:
    """Reject a prefix hit that is probably a longer, different name.

    "张三" must not match the row "张三丰 ...": whatever immediately follows the target
    must not begin another token with only a couple of characters left over. The
    comparison uses the raw strings so the computed offset is meaningful.
    """
    if len(raw_variant) <= len(raw_target):
        return False
    tail = raw_variant[len(raw_target):]
    if not tail:
        return False
    if not _TOKEN_START_RE.match(tail[0]):
        return False
    # A short remainder is a message preview ("张三 好的"), a long one is a longer name.
    return len(tail.strip()) > 3


def match_contact(contact_name, target, allow_leading_digit_strip: bool = True):
    """Does one whitelist entry match this OCRed contact name?

    Returns (matched, tier). Kept separate from find_whitelist_match so it can be unit
    tested in isolation.
    """
    raw_target = str(target or "").strip()
    if not raw_target:
        return False, ""

    exact_target = normalize_basic(raw_target)
    if not exact_target:
        return False, ""
    folded_target = normalize_ocr(raw_target)

    for variant in candidate_variants(contact_name, allow_leading_digit_strip):
        exact_name = normalize_basic(variant)
        if not exact_name:
            continue

        if exact_name == exact_target:
            return True, "exact"

        if exact_name.startswith(exact_target):
            if not _ambiguous_prefix(variant, raw_target):
                return True, "prefix"

        if exact_target.startswith(exact_name) and len(exact_target) <= len(exact_name) + 1:
            return True, "truncated"

        folded_name = normalize_ocr(variant)
        if not folded_name or not folded_target:
            continue
        if folded_name == folded_target:
            return True, "ocr-fold"
        if folded_name.startswith(folded_target) and not _ambiguous_prefix(variant, raw_target):
            return True, "ocr-prefix"
        if folded_target.startswith(folded_name) and len(folded_target) <= len(folded_name) + 1:
            return True, "ocr-truncated"

    return False, ""


def find_whitelist_match(contact_name, whitelist, allow_leading_digit_strip: bool = True):
    """First whitelist entry that matches. Returns (matched, canonical_name, tier)."""
    raw = str(contact_name or "").strip()
    if len(raw) < 2:
        return False, "", ""
    for entry in whitelist or ():
        entry = str(entry or "").strip()
        if not entry:
            continue
        matched, tier = match_contact(raw, entry, allow_leading_digit_strip)
        if matched:
            return True, entry, tier
    return False, "", ""


def is_blocked(name, blacklist) -> bool:
    """Substring match against the blacklist (service accounts, groups, irritants)."""
    raw = str(name or "").strip()
    if not raw:
        return False
    for entry in blacklist or ():
        entry = str(entry or "").strip()
        if entry and entry in raw:
            return True
    return False


def is_replyable_contact(name, blacklist=()) -> bool:
    """Non-whitelist mode gate: single human friend, not a group or service account.

    Group detection stays heuristic (font rendering makes OCR unreliable), so the UI
    keeps warning that whitelist mode is the only hard guarantee.
    """
    raw = str(name or "").strip()
    if len(raw) < 2:
        return False
    if is_blocked(raw, blacklist):
        return False
    if is_system_sender(raw):
        return False
    if looks_like_group(raw):
        return False
    return True
