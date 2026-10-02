"""Text helpers: HTML to text, normalisation keys, and term matching."""

from __future__ import annotations

import hashlib
import html as html_lib
import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

# space, tab, no-break space, zero-width space
_WS_RE = re.compile("[ \\t" + chr(0xA0) + chr(0x200B) + "]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# "[^<>]" rather than "[^>]": a run of unterminated "<a<a<a" then fails
# fast instead of being rescanned from every "<".
_TAG_RE = re.compile(r"<[a-zA-Z/][^<>]*>")
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
        "article", "header", "footer", "blockquote", "hr", "pre", "dl", "dt", "dd", "figure",
        "main", "aside", "nav", "form", "fieldset", "address",
    }
)  # fmt: skip
_CELL_TAGS = frozenset({"td", "th"})
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "head", "title"})
#: Markup beyond this is cut before parsing; a posting is never this long.
MAX_HTML_CHARS = 400_000


def unescape_if_needed(value: str) -> str:
    """Some APIs (Greenhouse) return HTML with its markup entity-escaped."""
    if "&lt;" in value and not _TAG_RE.search(value):
        return html_lib.unescape(value)
    return value


def html_to_text(value: str | None) -> str:
    """Readable plain text from an HTML fragment, keeping paragraph and list breaks.

    One pass over the parsed tree, so the cost grows in step with the input.
    Table cells and elements that sit side by side with nothing between them
    (skill "pills") are kept apart, so their words do not run together.
    """
    if not value:
        return ""
    value = unescape_if_needed(value[:MAX_HTML_CHARS])
    if not _TAG_RE.search(value):
        return normalize_text(html_lib.unescape(value))
    soup = BeautifulSoup(value, "html.parser")
    pieces: list[str] = []
    # Explicit stack: deeply nested markup must not hit the recursion limit.
    stack: list[Any] = [soup]
    while stack:
        node = stack.pop()
        if isinstance(node, str):  # a closing marker pushed below, or a text node
            if isinstance(node, NavigableString):
                if type(node) is NavigableString:  # not a comment, doctype or CDATA
                    pieces.append(str(node))
            else:
                pieces.append(node)
            continue
        name = getattr(node, "name", None)
        if name in _SKIPPED_TAGS:
            continue
        if name == "br":
            pieces.append("\n")
            continue
        after = ""
        if name == "li":
            pieces.append("\n- ")  # the list around it supplies the closing break
        elif name in _BLOCK_TAGS:
            pieces.append("\n")
            after = "\n"
        elif name in _CELL_TAGS:
            pieces.append(" ")
            after = " "
        elif isinstance(getattr(node, "previous_sibling", None), Tag):
            pieces.append(" ")  # "<span>Kubernetes</span><span>AWS</span>"
        if after:
            stack.append(after)
        stack.extend(reversed(list(getattr(node, "children", ()))))
    return normalize_text("".join(pieces))


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\r", "")
    lines = [_WS_RE.sub(" ", line).strip() for line in value.split("\n")]
    # A lone "-" is a list marker whose text landed on a following line
    # (``<li><p>text</p></li>``); join them back together.
    merged: list[str] = []
    for line in lines:
        if merged and merged[-1] == "-":
            if line:
                merged[-1] = f"- {line}"
            continue
        merged.append(line)
    if merged and merged[-1] == "-":
        merged.pop()
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(merged)).strip()


def squash(value: str | None) -> str:
    """Single-line, single-spaced."""
    return re.sub(r"\s+", " ", value or "").strip()


def sha256_text(*parts: str | None) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


# ---------------------------------------------------------------- keys

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
    "company", "plc", "gmbh", "ag", "sa", "bv", "nv", "pty", "lp", "llp", "holdings",
    "group", "the",
}  # fmt: skip


def company_key(name: str | None) -> str:
    """Comparison key for a company name: 'Acme, Inc.' and 'ACME' are the same."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", _ascii_lower(name))
    words = [w for w in cleaned.split() if w not in _COMPANY_SUFFIXES]
    return " ".join(words) or cleaned.strip()


_TITLE_ALIASES = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "eng": "engineer",
    "engr": "engineer",
    "mgr": "manager",
    "dir": "director",
    "vp": "vp",
    "svp": "svp",
    "avp": "avp",
    "k8s": "kubernetes",
    "dev": "developer",
    "ops": "operations",
    "infra": "infrastructure",
    "sre": "sre",
    "ii": "2",
    "iii": "3",
    "iv": "4",
}


def title_words(title: str | None) -> list[str]:
    """Lower-cased, alias-expanded words of a job title."""
    cleaned = _ascii_lower(title).replace("&", " and ")
    cleaned = re.sub(r"\bvice[ -]president\b", "vp", cleaned)
    cleaned = re.sub(r"\bhead of\b", "head", cleaned)
    cleaned = re.sub(r"[^a-z0-9+#.]+", " ", cleaned)
    words = []
    for word in cleaned.split():
        word = word.strip(".")
        if word:
            words.append(_TITLE_ALIASES.get(word, word))
    return words


def title_key(title: str | None) -> str:
    return " ".join(title_words(title))


def job_fingerprint(company: str | None, title: str | None) -> str:
    """Same company + same title = the same role, even when posted per location."""
    return sha256_text(company_key(company), title_key(title))[:32]


def question_key(question: str | None) -> str:
    """Comparison key for an application question."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", _ascii_lower(question))
    cleaned = re.sub(r"\b(required|optional)\b", " ", cleaned)
    return " ".join(cleaned.split())[:300]


def _ascii_lower(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def slugify(value: str | None, *, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _ascii_lower(value)).strip("-")
    return slug[:max_length].rstrip("-") or "untitled"


# ------------------------------------------------------- term matching

#: Different spellings of the same thing, mapped to one canonical term.
ALIASES: dict[str, str] = {
    "k8s": "kubernetes",
    "amazon web services": "aws",
    "google cloud platform": "gcp",
    "google cloud": "gcp",
    "microsoft azure": "azure",
    "elastic kubernetes service": "eks",
    "amazon eks": "eks",
    "azure kubernetes service": "aks",
    "google kubernetes engine": "gke",
    "infrastructure as code": "iac",
    "infrastructure-as-code": "iac",
    "ci/cd": "cicd",
    "ci cd": "cicd",
    "continuous integration": "cicd",
    "continuous delivery": "cicd",
    "golang": "go",
    "node.js": "nodejs",
    "postgres": "postgresql",
    "open policy agent": "opa",
    "policy as code": "policy-as-code",
    "site reliability": "sre",
    "site reliability engineering": "sre",
    "large language models": "llm",
    "large language model": "llm",
    "llms": "llm",
    "generative ai": "genai",
    "gen ai": "genai",
    "machine learning": "ml",
    "artificial intelligence": "ai",
    "hashicorp vault": "vault",
    "argo cd": "argocd",
    "argo-cd": "argocd",
    "zero trust": "zero-trust",
    "identity and access management": "iam",
    "service level objectives": "slo",
    "infrastructure automation": "iac",
}

# Terms that are also ordinary English words need an exact-case match.
_CASE_SENSITIVE = {"go": "Go", "r": "R", "c": "C", "rust": "Rust", "swift": "Swift"}


def canonical(term: str) -> str:
    lowered = squash(term).lower()
    return ALIASES.get(lowered, lowered)


@lru_cache(maxsize=4096)
def _term_pattern(term: str) -> re.Pattern[str]:
    lowered = squash(term).lower()
    spellings = {lowered} | {alias for alias, target in ALIASES.items() if target == lowered}
    if lowered in ALIASES:  # the term itself is an alias: also accept its canonical form
        target = ALIASES[lowered]
        spellings |= {target} | {alias for alias, t in ALIASES.items() if t == target}
    parts = []
    for spelling in sorted(spellings, key=len, reverse=True):
        if spelling in _CASE_SENSITIVE:
            exact = re.escape(_CASE_SENSITIVE[spelling])
            # "Go" the language, not "Go to market"; "R" the language, not "R&D".
            tail = {"go": r"(?![\s-]+to\b)", "r": r"(?!&)", "c": r"(?![-&])"}.get(spelling, "")
            parts.append(f"(?-i:{exact}){tail}")
        else:
            escaped = re.escape(spelling).replace(r"\ ", r"[\s\-/]+")
            parts.append(escaped)
    body = "|".join(parts)
    # \b does not work next to symbols (c++, .net, ci/cd); use explicit guards.
    return re.compile(rf"(?<![A-Za-z0-9+#])(?:{body})(?![A-Za-z0-9+#])", re.IGNORECASE)


def has_term(text: str, term: str) -> bool:
    """True when ``term`` (or a known alias) occurs in ``text`` as a whole term."""
    if not term.strip():
        return False
    return bool(_term_pattern(term).search(text))


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """The subset of ``terms`` present in ``text``, in the order given, de-duplicated."""
    found: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = canonical(term)
        if key in seen:
            continue
        if has_term(text, term):
            seen.add(key)
            found.append(term)
    return found


def phrase_in_title(phrase: str, words: list[str]) -> bool:
    """Every word of ``phrase`` is in the title (order-free, aliases applied)."""
    needed = title_words(phrase)
    return bool(needed) and all(word in words for word in needed)
