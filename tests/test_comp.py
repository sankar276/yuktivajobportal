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
        ("between $150,000 and $200,000 USD", Comp(150000, 200000, "USD", "year")),
        ("Rate: $95 - $110 per hour on C2C", Comp(95, 110, "USD", "hour")),
        ("$70/hr - $90/hr W2", Comp(70, 90, "USD", "hour")),
        ("Rate: $70-80 C2C", Comp(70, 80, "USD", "hour")),
        ("Salary: CAD $140,000 - $170,000", Comp(140000, 170000, "CAD", "year")),
        ("$120,000 to $150,000 CAD", Comp(120000, 150000, "CAD", "year")),
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
        "$200,000 - $100,000",  # reversed
        "$20,000 - $900,000",  # too wide to be one band
    ],
)
def test_ignores_things_that_are_not_pay_ranges(text: str) -> None:
    assert extract_comp(text) is None


def test_takes_the_first_plausible_range() -> None:
    text = "We raised $10 - $20 million. Base pay is $190,000 - $230,000."
    assert extract_comp(text) == Comp(190000, 230000, "USD", "year")
