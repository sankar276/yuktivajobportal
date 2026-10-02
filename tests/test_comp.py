from __future__ import annotations

import pytest

from jobportal.comp import Comp, extract_comp


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The base salary range is $210,000 - $265,000 per year.",
            Comp(210000, 265000, "USD", "year"),
        ),
        ("Compensation: $180K – $250K + equity", Comp(180000, 250000, "USD", "year")),
        ("Pay: $150 - $200K annually", Comp(150000, 200000, "USD", "year")),
        ("Salary between $150,000 and $200,000 USD", Comp(150000, 200000, "USD", "year")),
        ("Rate: $95 - $110 per hour on C2C", Comp(95, 110, "USD", "hour")),
        ("$70/hr - $90/hr W2", Comp(70, 90, "USD", "hour")),
        ("Rate: $70-80 C2C", Comp(70, 80, "USD", "hour")),
        ("Salary: CAD $140,000 - $170,000", Comp(140000, 170000, "CAD", "year")),
        ("Salary range: $120,000 to $150,000 CAD", Comp(120000, 150000, "CAD", "year")),
        ("Salary: 150,000 - 200,000 USD", Comp(150000, 200000, "USD", "year")),
        ("Base pay $180,000 USD - $220,000 USD", Comp(180000, 220000, "USD", "year")),
        ("Salary $180,000 \u2212 $220,000", Comp(180000, 220000, "USD", "year")),  # minus sign
        ("Salary $190k - 230k", Comp(190000, 230000, "USD", "year")),
        ("$180,000/yr - $220,000/yr", Comp(180000, 220000, "USD", "year")),
        ("Hourly: $62.50 - $78.25", Comp(62.5, 78.25, "USD", "hour")),
        # A bare range that says what it is by its unit, or by what comes on top.
        ("$150,000 - $200,000 a year", Comp(150000, 200000, "USD", "year")),
        ("$150,000 - $200,000 annually", Comp(150000, 200000, "USD", "year")),
        ("$150k-$200k + equity", Comp(150000, 200000, "USD", "year")),
        ("Compensation\n$180,000 - $220,000 + bonus + equity", Comp(180000, 220000, "USD", "year")),
        # Base pay, not the bigger numbers quoted next to it.
        (
            "On-target earnings: $300,000 - $350,000 (50/50 split). Base $150,000 - $175,000",
            Comp(150000, 175000, "USD", "year"),
        ),
        (
            "Total compensation $350,000 - $500,000 including equity; base salary range $210,000 - $250,000",
            Comp(210000, 250000, "USD", "year"),
        ),
        ("Base: C$140,000 - C$170,000", Comp(140000, 170000, "CAD", "year")),
        ("Salary: SGD $120,000 - $160,000", Comp(120000, 160000, "SGD", "year")),
        ("Pay range: £70,000 - £90,000", Comp(70000, 90000, "GBP", "year")),
        # The words nearest the figures decide what they are.
        (
            "Sign-on bonus of $20,000 - $40,000. Base salary $180,000 - $220,000.",
            Comp(180000, 220000, "USD", "year"),
        ),
        (
            "Relocation package of $25,000 - $50,000 available. Base: $190,000 - $230,000.",
            Comp(190000, 230000, "USD", "year"),
        ),
        (
            "We raised $10 - $20 million. Base pay is $190,000 - $230,000.",
            Comp(190000, 230000, "USD", "year"),
        ),
        # Pay bands by location are one range: the lowest low to the highest high.
        (
            "Salary by location. Tier 3: $150,000 - $175,000. Tier 1 (SF, NYC): $200,000 - $240,000.",
            Comp(150000, 240000, "USD", "year"),
        ),
    ],
)
def test_extracts_ranges(text: str, expected: Comp) -> None:
    assert extract_comp(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "We raised $10 - $20 million last year.",
        "Revenue grew from $1 to $2 billion",
        "A $50 - $100 gift card for referrals",  # small numbers without an hourly cue
        "Budget of $5,000 - $9,000 for training",  # neither hourly nor a salary
        "Up to $200,000",  # a single number is not a range
        "between $150,000 and $200,000",  # nothing says these are pay
        "Tier 3 locations: $150,000 - $175,000. Tier 1 (SF, NYC): $200,000 - $240,000.",
        "$210K base + $40K - $60K bonus.",  # the range is the bonus
        "Sign-on bonus of $20,000 - $40,000.",
        "Deals range from $100k to $500k ARR",
        "Daily rate $600 - $800",  # neither hourly nor annual
        "Customers save $20,000 - $90,000 a year",
        "$65,000 - $85,000 annual learning budget pool for the team",
        "$180,000 - $220,000 OTE",
        "Compensation: $45 - $55. Hours: 40/week",  # 45 dollars an hour, or 45 thousand a year?
        "Salary: $8,000 - $10,000 per month",
        "You will own a budget and salary planning for $2,000,000 - $4,000,000 in equity grants",
        "$200,000 - $100,000",  # reversed
        "$20,000 - $900,000",  # too wide to be one band
    ],
)
def test_ignores_things_that_are_not_pay_ranges(text: str) -> None:
    assert extract_comp(text) is None
