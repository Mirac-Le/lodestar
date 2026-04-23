"""PII scrubbing rules for feedback snapshots."""

from __future__ import annotations

from lodestar.privacy import scrub


def test_scrub_phone_keeps_last_four() -> None:
    assert scrub("电话 13812348888") == "电话 138****8888"


def test_scrub_id_card_fully_redacted() -> None:
    assert scrub("身份证 110101199001011234") == "身份证 [REDACTED_ID]"
    assert scrub("身份证 11010119900101123X") == "身份证 [REDACTED_ID]"


def test_scrub_bank_card_fully_redacted() -> None:
    assert scrub("卡号 6228480402564890018") == "卡号 [REDACTED_CARD]"


def test_scrub_email_masks_middle() -> None:
    assert scrub("联系 wanglei@gmail.com") == "联系 w***@gmail.com"


def test_scrub_preserves_harmless_text() -> None:
    text = "董淑佳 副校长 上海 UWC"
    assert scrub(text) == text


def test_scrub_multiple_in_one_string() -> None:
    text = "手机 13812348888 邮箱 abc@x.com"
    assert scrub(text) == "手机 138****8888 邮箱 a***@x.com"
