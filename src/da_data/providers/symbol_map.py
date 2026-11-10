"""symbol 映射：内部 sh/sz 前缀 ↔ 同花顺 thscode 后缀。

内部格式：sh600036 / sz000001
同花顺 thscode：600036.SH / 000001.SZ
映射失败返回 None，由调用方落 error 表（04 §2.2 第 7 条）。
"""
from __future__ import annotations


def to_thscode(symbol: str) -> str | None:
    s = symbol.strip().lower()
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj"):
        return s[2:] + "." + s[:2].upper()
    # 已是 thscode 形式
    if "." in s and s.split(".")[1].upper() in ("SH", "SZ", "BJ"):
        return s.upper()
    return None


def to_internal(thscode: str) -> str | None:
    t = thscode.strip()
    if "." in t:
        code, exch = t.split(".", 1)
        exch = exch.lower()
        if len(code) == 6 and exch in ("sh", "sz", "bj"):
            return exch + code
    return None
