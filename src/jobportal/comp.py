"""Pull an advertised pay range out of free text.

Used when a source gives no structured compensation. Deliberately
conservative, because a lane's pay floor skips roles on what this returns: a
range only counts when the words around it say it is pay (salary, base,
compensation, rate, "per hour"), and never when they say it is something
else (a sign-on bonus, relocation, equity, a budget, revenue, deal sizes).
A bare pair of dollar figures is not reported at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SYMBOLS = {
    "US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD", "NZ$": "NZD",
    "S$": "SGD", "HK$": "HKD", "$": "USD", "£": "GBP", "€": "EUR", "₹": "INR",
}  # fmt: skip
_CODES = ("USD", "CAD", "AUD", "NZD", "SGD", "HKD", "GBP", "EUR", "INR")
_SYMBOL = r"(?:US\$|CA\$|C\$|AU\$|A\$|NZ\$|S\$|HK\$|\$|£|€|₹)"
_CODE = r"(?:USD|CAD|AUD|NZD|SGD|HKD|GBP|EUR|INR)"
# A number starts where a run of digits starts and has at most nine digits
# before the point, so a long run of digits is passed over once, not once per digit.
_NUMBER = r"(?<![\d.,])(\d{1,3}(?:,\d{3}){1,3}(?:\.\d{1,2})?|\d{1,9}(?:\.\d{1,2})?) ?([kK])?"
_UNIT = r"(?: ?(?:/|per) ?(?:hr|hour|year|yr|annum|day|month|mo|week|wk))?"
_RANGE_RE = re.compile(
    rf"(?P<code>{_CODE} ?)?(?P<symbol>{_SYMBOL})? ?"
    + _NUMBER
    + rf"(?P<unit>(?: ?{_CODE})?{_UNIT})"
    # hyphen, en and em dash, minus sign, non-breaking hyphen, "--", "~", words
    + r" ?(?:--|-|–|—|\u2212|\u2011|~|to|and|up to|through|thru) ?"
    + rf"(?:{_CODE} ?)?{_SYMBOL}? ?"
    + _NUMBER
    + rf"(?P<tail>{_UNIT}(?: ?\(?{_CODE}\)?)?)"
)
_ANY_CODE_RE = re.compile(rf"\b{_CODE}\b")
_SPACES_RE = re.compile(r"[^\S\n]+")
_HOUR_RE = re.compile(r"\b(?:hr|hrs|hour|hourly)\b|/ ?h\b", re.I)
_RATE_RE = re.compile(r"\brates?\b", re.I)
_OTHER_PERIOD_RE = re.compile(
    r"\b(?:per|a|each) (?:day|week|month)\b|/ ?(?:day|wk|week|mo|month)\b|\b(?:daily|weekly|monthly)\b|\bday rate\b",
    re.I,
)
_YEAR_RE = re.compile(
    r"\b(?:per|a) (?:year|annum)\b|/ ?(?:yr|year)\b|\bannual(?:ly)?\b|\byearly\b", re.I
)
_NOT_PAY_AFTER_RE = re.compile(r"^ ?(?:million|billion|mm\b|m\b|b\b|bn\b)", re.I)
_PAY_WORDS_RE = re.compile(
    r"\bsalar(?:y|ies)\b|\bbase\b|\bpay\b|\bpaid\b|\bcompensation\b|\bwages?\b|\brates?\b"
    r"|\bremuneration\b|\bhourly\b",
    re.I,
)
# Money that is not base pay. Total compensation and on-target earnings are
# here too: a lane's floor is about base pay, and those run higher.
_OTHER_WORDS_RE = re.compile(
    r"\btotal (?:cash |target )?comp(?:ensation)?\b|\btotal (?:rewards|package)\b|\bote\b"
    r"|\bon[- ]target(?: earnings?)?\b|\bearnings?\b|\bsav(?:e|es|ed|ing)\b|\blearning\b"
    r"|\bbonus(?:es)?\b|\bsign[- ]?on\b|\bsigning\b|\brelocation\b|\bequity\b|\bstock\b|\brsus?\b"
    r"|\bstipend\b|\bbudget\b|\barr\b|\brevenue\b|\bdeals?\b|\bquota\b|\bfunding\b|\braised\b"
    r"|\bvaluation\b|\b401\b|\ballowance\b|\breimburs\w*|\btuition\b|\breferrals?\b|\bgrants?\b"
    r"|\bseries [a-f]\b|\binvest\w*|\bpipeline\b|\bcontract value\b|\btcv\b|\bacv\b|\bportfolio\b"
    r"|\baum\b|\bsavings\b|\bspend\b|\bgift\b|\bprizes?\b|\bawards?\b|\bfees?\b|\bcosts?\b"
    r"|\bprice[sd]?\b|\bdiscount\b|\bcredits?\b|\bdonat\w*|\bmatch(?:ing)?\b",
    re.I,
)
_TRAILING_UNIT_RE = re.compile(
    r" ?(?:an?|per|/) ?(?:h(?:ou)?r|year|yr|annum)\b| ?annually\b"
    r"| ?\+ ?(?:equity|bonus|benefits|stock|options)\b",
    re.I,
)
LOOK_BACK = 160
LOOK_AHEAD = 30


@dataclass(frozen=True)
class Comp:
    minimum: float
    maximum: float
    currency: str
    period: str  # "year" | "hour"


def _value(number: str, kilo: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * 1000 if kilo else value


def _last_end(pattern: re.Pattern[str], text: str) -> int:
    end = -1
    for match in pattern.finditer(text):
        end = match.end()
    return end


def _is_pay(before: str, inside: str, after: str) -> bool:
    """Do the words around a range say it is pay, and not something else?"""
    if _OTHER_WORDS_RE.match(after.lstrip(" ")) or _NOT_PAY_AFTER_RE.match(after):
        return False  # "$40K - $60K bonus", "$100k to $500k ARR", "$10 - $20 million"
    pay, other = _last_end(_PAY_WORDS_RE, before), _last_end(_OTHER_WORDS_RE, before)
    if pay < 0 and other < 0:
        # Nothing before it says what the figures are. They count when they
        # carry their own unit ("$70/hr - $90/hr"), are followed at once by a
        # pay word ("... base salary") or a unit ("... a year", "... an hour"),
        # or by what comes on top of pay ("... + equity"). Anything else that
        # is counted by the year ("save $20,000 - $90,000 a year") has its own
        # word in front, and is ruled out by that word above.
        return bool(
            _HOUR_RE.search(inside)
            or _YEAR_RE.search(inside)
            or _PAY_WORDS_RE.match(after.lstrip(" "))
            or _TRAILING_UNIT_RE.match(after)
        )
    return pay > other  # the nearer word decides; a tie is not pay


def _currency(match: re.Match[str], after: str) -> str:
    whole = f"{match.group(0)} {after[:8]}".upper()
    for code in _CODES:
        if code != "USD" and re.search(rf"\b{code}\b", whole):
            return code
    return _SYMBOLS.get(match.group("symbol") or "$", "USD")


def _has_currency(match: re.Match[str]) -> bool:
    return bool(match.group("code") or match.group("symbol") or _ANY_CODE_RE.search(match.group(0)))


def extract_comp(text: str | None) -> Comp | None:
    """The advertised pay range, or ``None`` when the text does not plainly state one.

    Several pay ranges of one kind (location tiers, levels) are combined into
    the lowest low and the highest high.
    """
    if not text:
        return None
    text = _SPACES_RE.sub(" ", text)
    found: list[Comp] = []
    for match in _RANGE_RE.finditer(text):
        if not _has_currency(match):
            continue  # two plain numbers: "5 - 10 years"
        low_kilo, high_kilo = match.group(4), match.group(7)
        low, high = _value(match.group(3), low_kilo), _value(match.group(6), high_kilo)
        # "$150 - $200K": the K applies to both ends.
        if high_kilo and not low_kilo and low < 1000:
            low *= 1000
        if high < low or high > low * 5:
            continue
        paragraph = text[max(0, match.start() - LOOK_BACK) : match.start()].rsplit("\n\n", 1)[-1]
        sentence_end = re.search(r"[.;\n]", text[match.end() :])
        ahead = text[match.end() : match.end() + (sentence_end.start() if sentence_end else 400)]
        after = ahead[:LOOK_AHEAD]
        inside = match.group(0)
        if not _is_pay(paragraph, inside, after):
            continue
        around = f"{inside} {after}"
        if _OTHER_PERIOD_RE.search(around) or _OTHER_PERIOD_RE.search(paragraph[-25:]):
            continue  # a day or month rate is neither hourly nor annual
        hourly = _HOUR_RE.search(around) or (
            (_RATE_RE.search(paragraph[-40:]) or _HOUR_RE.search(paragraph[-40:]))
            and not _YEAR_RE.search(around)
        )
        if low >= 10 and high <= 1000 and hourly:
            period = "hour"
        elif low >= 20_000 and high <= 2_000_000:
            period = "year"
        else:
            continue
        found.append(Comp(low, high, _currency(match, text[match.end() :]), period))
    if not found:
        return None
    first = found[0]
    same = [c for c in found if (c.currency, c.period) == (first.currency, first.period)]
    return Comp(
        min(c.minimum for c in same), max(c.maximum for c in same), first.currency, first.period
    )
