"""Identifier formats shared across modules."""

from __future__ import annotations

import string

_B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_SUFFIX = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
SF_INSTANCE = "5f"      # 2-char pod/instance segment after the key prefix
SF_RESERVED = "0"


def sf_checksum(id15: str) -> str:
    """Salesforce 15 -> 18 char suffix: one char per 5-char block, bit i set when char i is A-Z."""
    out = []
    for block in range(3):
        bits = 0
        for i, ch in enumerate(id15[block * 5 : block * 5 + 5]):
            if "A" <= ch <= "Z":
                bits |= 1 << i
        out.append(_SUFFIX[bits])
    return "".join(out)


def sf_id(key_prefix: str, counter: int) -> str:
    """18-char Salesforce id: prefix(3) + instance(2) + reserved(1) + base62 counter(9) + checksum(3)."""
    n, digits = counter, []
    for _ in range(9):
        n, r = divmod(n, 62)
        digits.append(_B62[r])
    id15 = f"{key_prefix}{SF_INSTANCE}{SF_RESERVED}{''.join(reversed(digits))}"
    return id15 + sf_checksum(id15)


def to_id18(id15: str) -> str:
    return id15 if len(id15) == 18 else id15 + sf_checksum(id15)


# AX RecIds start at the first value AX hands out in a fresh partition, per table.
AX_RECID_BASE = 5637144576

# Default AX partition RecId
AX_PARTITION = 5637144576
AX_EMPTY_DATE = "1900-01-01"
AX_MAX_DATETIME = "2154-12-31 23:59:59"
