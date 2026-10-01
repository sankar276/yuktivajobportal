"""The fact guard: a rewritten bullet may change wording, never facts.

Anything a language model hands back passes through here before it can reach
a resume. A rewrite is rejected (and the original kept) if it introduces a
number, a technology, or a name the original did not contain, or if it grows
suspiciously. The guard is deliberately strict: a false rejection costs
nothing, a false acceptance puts an untrue claim under your name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from jobportal.resume.vocab import COMMON_TERMS
from jobportal.text import canonical, find_terms

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*\s?(?:%|percent|x|k|m|bn|b)?", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?]\s+)([A-Za-z][A-Za-z0-9+#./-]*)")
MAX_GROWTH = 1.35


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", match.group(0)).lower() for match in _NUMBER_RE.finditer(text)}


def _proper_nouns(text: str) -> set[str]:
    """Capitalised or mixed-case words that are not just the start of a sentence."""
    starts = {match.start(1) for match in _SENTENCE_START_RE.finditer(text)}
    found: set[str] = set()
    for match in _WORD_RE.finditer(text):
        word = match.group(0).rstrip(".-/")
        if not word or word.islower():
            continue
        if match.start() in starts and word[1:].islower():
            continue  # an ordinary word capitalised because it opens a sentence
        found.add(word.lower())
    return found


def rewrite_violations(original: str, rewritten: str, vocabulary: Iterable[str] = ()) -> list[str]:
    """Why ``rewritten`` may not replace ``original``. Empty list = acceptable."""
    problems: list[str] = []
    rewritten = rewritten.strip()
    if not rewritten:
        return ["empty rewrite"]

    new_numbers = _numbers(rewritten) - _numbers(original)
    if new_numbers:
        problems.append("adds numbers: " + ", ".join(sorted(new_numbers)))

    terms = [*vocabulary, *COMMON_TERMS]
    had = {canonical(term) for term in find_terms(original, terms)}
    new_terms = [term for term in find_terms(rewritten, terms) if canonical(term) not in had]
    if new_terms:
        problems.append("adds skills: " + ", ".join(new_terms))

    new_names = (
        _proper_nouns(rewritten)
        - _proper_nouns(original)
        - {word.lower() for word in _WORD_RE.findall(original)}
    )
    if new_names:
        problems.append("adds names: " + ", ".join(sorted(new_names)))

    if len(rewritten) > max(len(original) * MAX_GROWTH, len(original) + 25):
        problems.append("much longer than the original")
    return problems


def accept_rewrite(original: str, rewritten: str, vocabulary: Iterable[str] = ()) -> str:
    """The rewrite if it passes the guard, otherwise the original."""
    return original if rewrite_violations(original, rewritten, vocabulary) else rewritten.strip()
