"""Pull an advertised pay range out of free text.

Used when a source gives no structured compensation. Deliberately
conservative: only explicit ranges, and only when the numbers look like pay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?([kK])?"
_RANGE_RE = re.compile(
    r"(?P<currency>USD|CAD|AUD|US\$|C\$|A\$)?\s?\$\s?"
    + _NUMBER
    + r"(?:\s?(?:/|per)\s?(?:hr|hour|year|yr|annum))?"
    + r"\s*(?:-|–|—|to|and)\s*"
    + r"(?:USD\s?|US)?\$?\s?"
    + _NUMBER,
)
_HOUR_RE = re.compile(r"\b(?:hr|hrs|hour|hourly|rate)\b|/\s?h\b", re.I)
_NOT_PAY_RE = re.compile(r"^\s?(?:million|billion|mm\b|m\b|b\b|bn\b)", re.I)
_CURRENCY = {"CAD": "CAD", "C$": "CAD", "AUD": "AUD", "A$": "AUD"}


@dataclass(frozen=True)
class Comp:
    minimum: float
    maximum: float
    currency: str
    period: str  # "year" | "hour"


def _value(number: str, kilo: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * 1000 if kilo else value


def extract_comp(text: str | None) -> Comp | None:
    if not text:
        return None
    for match in _RANGE_RE.finditer(text):
        low = _value(match.group(2), match.group(3))
        high = _value(match.group(4), match.group(5))
        # "$150 - $200K": the K applies to both ends.
        if match.group(5) and not match.group(3) and low < 1000:
            low *= 1000
        if high < low or high > low * 5:
            continue
        if _NOT_PAY_RE.match(text[match.end() : match.end() + 10]):
            continue  # "$10 - $20 million" is revenue, not pay
        context = text[max(0, match.start() - 40) : match.end() + 40]
        if low >= 10 and high <= 1000 and _HOUR_RE.search(context):
            period = "hour"
        elif low >= 20_000 and high <= 2_000_000:
            period = "year"
        else:
            continue
        tail = text[match.end() : match.end() + 12].upper()
        prefix = match.group("currency") or ""
        currency = _CURRENCY.get(prefix.upper(), "USD")
        for code in ("CAD", "AUD"):
            if code in tail:
                currency = code
        return Comp(low, high, currency, period)
    return None
