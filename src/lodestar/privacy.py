"""PII scrubbers for feedback snapshots.

脱敏规则：
  * 手机号（11 位，1 开头）→ 保留前 3 + 后 4，中间 ****
  * 身份证（18 位，末位数字或 X）→ [REDACTED_ID]
  * 银行卡（16-19 位连续数字）→ [REDACTED_CARD]
  * 邮箱 → 本地部分保留首字母，其余 *** 替换

顺序很重要：身份证 18 位 > 银行卡 16-19 位 > 手机 11 位，长的先匹配，
避免银行卡号被手机号 regex 截一半。
"""

from __future__ import annotations

import re

_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
_BANK_CARD_RE = re.compile(r"\b\d{16,19}\b")
_PHONE_RE = re.compile(r"\b(1\d{2})\d{4}(\d{4})\b")
_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9])([A-Za-z0-9._%+-]*)"
    r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
)


def scrub(text: str | None) -> str:
    """Best-effort PII scrub of a free-text string.

    先替换长 pattern（身份证、银行卡），再替换短 pattern（手机、邮箱），
    避免银行卡号被手机号 regex 截一半。
    """
    if not text:
        return text or ""
    out = _ID_CARD_RE.sub("[REDACTED_ID]", text)
    out = _BANK_CARD_RE.sub("[REDACTED_CARD]", out)
    out = _PHONE_RE.sub(r"\1****\2", out)
    out = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(3)}", out)
    return out
