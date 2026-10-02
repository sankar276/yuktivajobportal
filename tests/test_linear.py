"""Text from outside must never be able to stall the app.

Postings, locations, mail subjects and mail bodies are written by other
people. Every function that reads them is run here over long, repetitive
inputs of the kinds that make a careless pattern rescan its input from every
position. Each call has to finish quickly at a size where a quadratic one
would not.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from jobportal.comp import extract_comp
from jobportal.crawl import is_contract_text, is_contract_title
from jobportal.facts import extract_facts
from jobportal.inbox.parse import ParsedMail, _title_from_subject, extract_requirement
from jobportal.resume.guard import rewrite_violations
from jobportal.scoring import infer_seniority, is_blocked, mentions_us
from jobportal.sources.base import (
    infer_remote,
    parse_employment,
    remote_from_description,
    remote_unclear,
)
from jobportal.text import (
    company_key,
    find_terms,
    html_to_text,
    normalize_text,
    question_key,
    states,
    title_words,
)

SIZE = 40_000
#: Generous for a linear pass over 40,000 characters on a slow machine; a
#: quadratic one takes many times longer.
BUDGET_SECONDS = 2.0

SHAPES: dict[str, Callable[[int], str]] = {
    "letters": lambda n: "a" * n,
    "spaces": lambda n: " " * n,
    "form feeds": lambda n: "5 years" + "\x0c" * n,
    "digits": lambda n: "1" * n,
    "dollar digits": lambda n: "$" + "1" * n,
    "thousands": lambda n: "$1" + ",111" * (n // 4),
    "ranges": lambda n: "$1 - " * (n // 5),
    "dashes": lambda n: "- A" * (n // 3),
    "replies": lambda n: "re: " * (n // 4),
    "years": lambda n: "5+ years " * (n // 9),
    "years then words": lambda n: "5 years " + "a " * (n // 2),
    "negations": lambda n: "not " * (n // 4) + "sponsor",
    "travel": lambda n: "travel " + "1 " * (n // 2) + "%",
    "percent": lambda n: "1% " + "a " * (n // 2),
    "must have": lambda n: "must have " * (n // 10),
    "active": lambda n: "active " * (n // 7),
    "brackets": lambda n: "(" * n,
    "open remote": lambda n: "(remote" + "x" * n,
    "remote hybrid": lambda n: "remote hybrid " * (n // 14),
    "tags": lambda n: "<div>" * (n // 5),
    "open tags": lambda n: "<a" * (n // 2),
    "pipes": lambda n: " | " * (n // 3),
    "dotted": lambda n: "u.s." * (n // 4),
    "city codes": lambda n: "a, AB " * (n // 6),
    "cities": lambda n: "Austin " * (n // 7),
    "go": lambda n: "Go " * (n // 3),
    "with helm": lambda n: "with helm, " * (n // 11),
    "denials": lambda n: "no " * (n // 3) + "relocation required",
    "commas": lambda n: "5+ years of experience, " * (n // 24),
}


def _mail(text: str) -> ParsedMail:
    return ParsedMail(
        message_id="<1@x>", from_addr="a@b.co", subject=text[:5000], text=text[:20000]
    )


READERS: dict[str, Callable[[str], object]] = {
    "extract_facts": lambda s: extract_facts(s, location=s[:400]),
    "extract_comp": extract_comp,
    "html_to_text": html_to_text,
    "normalize_text": normalize_text,
    "title_words": title_words,
    "question_key": question_key,
    "company_key": company_key,
    "infer_remote": infer_remote,
    "remote_unclear": remote_unclear,
    "remote_from_description": remote_from_description,
    "parse_employment": parse_employment,
    "is_contract_title": is_contract_title,
    "is_contract_text": is_contract_text,
    "infer_seniority": infer_seniority,
    "mentions_us": mentions_us,
    "is_blocked": lambda s: is_blocked(s, ["Northwind Systems", "acme"]),
    "find_terms": lambda s: find_terms(
        s, ["kubernetes", "aws", "go", "c", "r", "helm", "ci/cd", "amazon web services", "soc 2"]
    ),
    "states": lambda s: states(s, "relocation required"),
    "title_from_subject": _title_from_subject,
    "extract_requirement": lambda s: extract_requirement(_mail(s)),
    "rewrite_violations": lambda s: rewrite_violations(s[:4000], s[:4000]),
}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_no_reader_of_outside_text_can_be_stalled(reader: str) -> None:
    read = READERS[reader]
    slow: list[tuple[str, float]] = []
    for shape, make in SHAPES.items():
        text = make(SIZE)
        started = time.perf_counter()
        read(text)
        elapsed = time.perf_counter() - started
        if elapsed > BUDGET_SECONDS:
            slow.append((shape, round(elapsed, 2)))
    assert not slow, f"{reader} is slow on: {slow}"
