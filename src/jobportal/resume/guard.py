"""The fact guard: a rewritten bullet may change the order of your words, not the facts.

Anything a language model hands back passes through here before it can reach
a resume, and the posting it was shown is somebody else's text that may be
written to steer it. So the guard does not try to recognise invented facts;
it only lets through what cannot be one:

* every word of the rewrite is a word of the original bullet (or a different
  form of the same word, or another spelling of a term the bullet already
  names), apart from a short list of joining words;
* every number is in the original, followed by the same word as there, so
  "45 minutes" cannot become "45 months" and "120 services" cannot become
  "120 teams";
* no extra sentence, and no real growth.

That leaves reordering, trimming and matching the posting's spelling of a
term. A false rejection costs nothing (the original is kept); a false
acceptance would put an untrue claim under your name.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from jobportal.resume.vocab import COMMON_TERMS
from jobportal.text import canonical, find_terms

_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*%?|[A-Za-z][A-Za-z0-9+#]*(?:[./'’-][A-Za-z0-9+#]+)*")
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
#: Words that only join other words. Nothing here can carry a claim: no
#: negations, no quantities, no verbs.
_JOINING_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "at",
        "from",
        "as",
        "into",
        "across",
        "through",
        "per",
        "via",
        "using",
        "that",
        "which",
        "while",
        "its",
        "their",
        "our",
    ]
)
MAX_GROWTH = 1.35


def _tokens(text: str) -> list[str]:
    return [match.group(0) for match in _TOKEN_RE.finditer(text)]


def _is_number(token: str) -> bool:
    return token[:1].isdigit()


def _number(token: str) -> str:
    return token.replace(",", "")


def _stem(word: str) -> str:
    """A crude common form, so that "migrated", "migrating" and "migrate" are one word."""
    stem = word.lower().replace("’", "'")
    for suffix in ("ing", "ed", "es", "s"):
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 3:
            stem = stem[: -len(suffix)]
            break
    if len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
        stem = stem[:-1]  # cutt(ing) -> cut
    if len(stem) > 3 and stem.endswith("e"):
        stem = stem[:-1]
    return stem


def _parts(token: str) -> list[str]:
    """A compound and its pieces: "cross-team" also allows "cross" and "team"."""
    pieces = [piece for piece in re.split(r"[./'’-]", token) if piece]
    return [token, *pieces] if len(pieces) > 1 else [token]


def _number_pairs(tokens: list[str]) -> Counter[tuple[str, str]]:
    """Each number with the word that follows it."""
    pairs: Counter[tuple[str, str]] = Counter()
    for index, token in enumerate(tokens):
        if not _is_number(token):
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if following.lower() in _JOINING_WORDS:
            following = ""  # "35% by ...": nothing the number counts
        # The same thing under another spelling (k8s, Kubernetes) is the same word.
        word = following if _is_number(following) else _stem(canonical(following))
        pairs[(_number(token), word)] += 1
    return pairs


def rewrite_violations(original: str, rewritten: str, vocabulary: Iterable[str] = ()) -> list[str]:
    """Why ``rewritten`` may not replace ``original``. Empty list = acceptable."""
    rewritten = rewritten.strip()
    if not rewritten:
        return ["empty rewrite"]
    problems: list[str] = []
    terms = [*vocabulary, *COMMON_TERMS]
    before, after = _tokens(original), _tokens(rewritten)

    had_numbers = {_number(token) for token in before if _is_number(token)}
    new_numbers = sorted({_number(token) for token in after if _is_number(token)} - had_numbers)
    if new_numbers:
        problems.append("adds numbers: " + ", ".join(new_numbers))
    moved = [
        f"{number} {word}".strip()
        for (number, word), count in (_number_pairs(after) - _number_pairs(before)).items()
        if number in had_numbers
        for _ in range(count)
    ]
    if moved:
        problems.append("changes what a number refers to: " + ", ".join(sorted(moved)))

    words_before = [part for token in before if not _is_number(token) for part in _parts(token)]
    stems = {_stem(word) for word in words_before}
    names = {canonical(word) for word in words_before}
    names |= {canonical(term) for term in find_terms(original, terms)}
    new_skills = [term for term in find_terms(rewritten, terms) if canonical(term) not in names]
    skill_words = {word.lower() for term in new_skills for word in _tokens(term)}
    new_names: list[str] = []
    new_words: list[str] = []

    def known(word: str) -> bool:
        return word.lower() in _JOINING_WORDS or _stem(word) in stems or canonical(word) in names

    for position, token in enumerate(after):
        if _is_number(token):
            continue
        pieces = _parts(token)
        if known(token) or (len(pieces) > 1 and all(known(piece) for piece in pieces[1:])):
            continue
        if token.lower() in skill_words:
            continue  # reported once, as a skill
        capitalised = not token.islower() and position > 0
        (new_names if capitalised else new_words).append(token)
    if new_skills:
        problems.append("adds skills: " + ", ".join(new_skills))
    if new_names:
        problems.append("adds names: " + ", ".join(dict.fromkeys(new_names)))
    if new_words:
        problems.append("adds words: " + ", ".join(dict.fromkeys(new_words)))

    if len(_SENTENCE_END_RE.findall(rewritten)) > max(1, len(_SENTENCE_END_RE.findall(original))):
        problems.append("adds a sentence")
    if len(rewritten) > max(len(original) * MAX_GROWTH, len(original) + 25):
        problems.append("much longer than the original")
    return problems


def accept_rewrite(original: str, rewritten: str, vocabulary: Iterable[str] = ()) -> str:
    """The rewrite if it passes the guard, otherwise the original."""
    return original if rewrite_violations(original, rewritten, vocabulary) else rewritten.strip()
