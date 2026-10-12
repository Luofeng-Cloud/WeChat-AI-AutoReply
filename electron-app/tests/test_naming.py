"""Name matching and the whitelist firewall.

Every case here is a behaviour the 1.x matcher either got wrong or was never explicit
about. The tier assertions double as documentation of the confidence order.
"""

from __future__ import annotations

import pytest

from wechat_engine import naming


class TestBasicNormalisation:
    def test_case_and_whitespace_folding(self):
        assert naming.normalize_basic("  Zhang San ") == "zhangsan"

    def test_punctuation_is_dropped(self):
        assert naming.normalize_basic("a-b_c.d") == "abcd"

    def test_digits_are_preserved(self):
        assert naming.normalize_basic("123") == "123"

    def test_ocr_fold_is_separate(self):
        assert naming.normalize_basic("E0") != naming.normalize_basic("Eo")
        assert naming.normalize_ocr("E0") == naming.normalize_ocr("Eo")


class TestExactMatching:
    def test_exact_hit(self):
        matched, tier = naming.match_contact("张三", "张三")
        assert matched and tier == "exact"

    def test_whitespace_insensitive(self):
        matched, _tier = naming.match_contact("张 三", "张三")
        assert matched

    def test_single_character_name_is_rejected_by_find(self):
        matched, _canonical, _tier = naming.find_whitelist_match("张", ["张"])
        assert not matched

    def test_empty_entries_are_skipped(self):
        matched, canonical, _tier = naming.find_whitelist_match("张三", ["", "   ", "张三"])
        assert matched and canonical == "张三"


class TestLeadingDigitVariants:
    def test_docked_badge_digit_still_matches(self):
        """WeChat renders the unread count directly before the name."""
        matched, tier = naming.match_contact("3张三", "张三")
        assert matched
        assert tier in ("exact", "prefix", "truncated")

    def test_real_name_starting_with_digit_matches_itself(self):
        """The 1.x code stripped the digit first, so "3号仓库" could never match."""
        matched, tier = naming.match_contact("3号仓库", "3号仓库")
        assert matched and tier == "exact"

    def test_digit_named_contacts_do_not_cross_match(self):
        matched, _tier = naming.match_contact("2号", "1号")
        assert not matched

    def test_strip_can_be_disabled(self):
        matched, _tier = naming.match_contact("3张三", "张三", allow_leading_digit_strip=False)
        assert not matched


class TestPrefixAmbiguity:
    def test_preview_text_does_not_break_matching(self):
        matched, tier = naming.match_contact("张三 好的收到", "张三")
        assert matched and tier == "prefix"

    def test_longer_different_name_is_rejected(self):
        """Whitelist 张三 must not answer 张三丰."""
        matched, _tier = naming.match_contact("张三丰 在吗", "张三")
        assert not matched

    def test_short_tail_is_a_preview_not_a_name(self):
        matched, _tier = naming.match_contact("张三 在", "张三")
        assert matched


class TestTruncationTolerance:
    def test_one_missing_trailing_character(self):
        matched, tier = naming.match_contact("王小明", "王小明明")
        assert matched and tier == "truncated"

    def test_more_than_one_missing_character_is_rejected(self):
        matched, _tier = naming.match_contact("王", "王小明")
        assert not matched


class TestOcrLookAlike:
    def test_folding_matches_confusable_pairs(self):
        matched, tier = naming.match_contact("Eo", "E0")
        assert matched and tier.startswith("ocr")

    def test_exact_match_wins_over_folding(self):
        _matched, tier = naming.match_contact("E0", "E0")
        assert tier == "exact"


class TestGroupAndSystemFirewall:
    @pytest.mark.parametrize(
        "name",
        [
            "同学交流群",
            "项目群",
            "家族群",
            "部门群 (3)",
            "技术分享群聊",
        ],
    )
    def test_group_names_are_detected(self, name):
        assert naming.looks_like_group(name)

    def test_preview_with_parenthesised_number_is_not_a_group(self):
        """A friend can send "一共(3)个文件"; only the name segment may be examined."""
        assert not naming.looks_like_group("张三 一共(3)个文件")

    def test_trailing_count_marker_is_a_group(self):
        assert naming.looks_like_group("设计部(12)")

    @pytest.mark.parametrize("name", ["同学群", "客户群", "小组群", "车友群"])
    def test_unknown_group_names_are_caught_by_the_backstop(self, name):
        assert naming.looks_like_group(name)

    @pytest.mark.parametrize("name", ["李群", "群众", "群群"])
    def test_short_names_ending_in_qun_are_people(self, name):
        """The backstop must not swallow a real contact called 李群."""
        assert not naming.looks_like_group(name)

    @pytest.mark.parametrize("name", ["微信团队", "文件传输助手", "订阅号消息", "腾讯新闻"])
    def test_system_senders_are_detected(self, name):
        assert naming.is_system_sender(name)

    def test_blacklist_substring_blocks(self):
        assert naming.is_blocked("微信支付提醒", ["微信支付"])
        assert not naming.is_blocked("张三", ["微信支付"])

    def test_empty_blacklist_entry_does_not_block_everything(self):
        """The 1.x code checked "if b_name in n" after a truthiness filter; an empty
        string would have matched any name had the filter ever been removed."""
        assert not naming.is_blocked("张三", ["", "   "])
        assert not naming.is_blocked("", [""])


class TestReplyableContact:
    def test_plain_friend_is_replyable(self):
        assert naming.is_replyable_contact("李四")

    @pytest.mark.parametrize("name", ["同学群", "客户群", "微信团队", "文件传输助手", "A"])
    def test_non_contacts_are_not_replyable(self, name):
        assert not naming.is_replyable_contact(name)

    def test_blacklist_applies(self):
        assert not naming.is_replyable_contact("讨厌的人", ["讨厌"])
