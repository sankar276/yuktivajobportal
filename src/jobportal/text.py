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


# Technology names written with a dot, kept whole when a title is split up.
_DOTTED_TERMS = (".net", "node.js", "vue.js", "next.js", "nuxt.js", "react.js", "asp.net")
_RANKS = (
    (re.compile(r"\b(?:assistant|associate|asst)\s+vp\b"), "avp"),
    (re.compile(r"\b(?:senior|sr)\s+vp\b"), "svp"),
    (re.compile(r"\bexecutive\s+vp\b"), "evp"),
)


def title_words(title: str | None) -> list[str]:
    """Lower-cased, alias-expanded words of a job title.

    Titles are written every which way: "Sr.Staff", "Staff+", "V.P.",
    "Architect\u2013Cloud". Anything that is not part of a word separates
    words, and the common spellings of a rank become one token.
    """
    # Dashes of every kind are separators; folding to ASCII would delete them
    # and glue their neighbours together.
    spaced = re.sub(r"[\u2010-\u2015\u2212]", " ", title or "")
    cleaned = _ascii_lower(spaced).replace("&", " and ")
    for index, term in enumerate(_DOTTED_TERMS):
        cleaned = cleaned.replace(term, f" dotted{index} ")
    cleaned = re.sub(r"\b((?:[a-z]\.){2,})", lambda m: m.group(1).replace(".", ""), cleaned)  # v.p.
    cleaned = re.sub(r"[^a-z0-9+#]+", " ", cleaned)
    cleaned = re.sub(r"\bvice president\b", "vp", cleaned)
    cleaned = re.sub(r"\bsr\b", "senior", cleaned)
    for pattern, rank in _RANKS:
        cleaned = pattern.sub(rank, cleaned)
    cleaned = re.sub(r"\bhead of\b", "head", cleaned)
    words = []
    for word in cleaned.split():
        if word.startswith("dotted") and word[6:].isdigit():
            word = _DOTTED_TERMS[int(word[6:])]
        elif word.endswith("+") and not word.endswith("++"):
            word = word.rstrip("+")  # "Staff+"
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
    "asp.net": ".net",
    "dotnet": ".net",
    "dot net": ".net",
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

# Terms that are also ordinary English words or letters. Written in the case
# given here they count where a skill would stand: in a list, or after "in",
# "with", "using". In lower case they count only between list separators
# ("python, go, rust"), never in running text.
_AMBIGUOUS = {"go": ("Go", "GO"), "r": ("R",), "c": ("C",), "rust": ("Rust",), "swift": ("Swift",)}
# Tool names that are also English words ("at the helm", "harness the power",
# "Hong Kong"). Capitalised inside a sentence they are the tool. In lower case,
# or where a sentence starts, they count only where a skill would stand.
_WORDLIKE = frozenset(
    {"helm", "vault", "flux", "harness", "spark", "chef", "puppet", "envoy", "nomad", "consul",
     "salt", "kong", "packer", "rancher"}
)  # fmt: skip
#: The ones that open a sentence as a verb: "Harness the power of data".
_VERBS = frozenset({"harness", "spark"})
_BULLETS = ("-", "*", "\u2022", "\u00b7")
_SKILL_BEFORE_RE = re.compile(
    r"(?:(?:^|[\s(])(?:in|with|using|and|or|of|like|plus|both|either)\s+|[,/(|:;\u2022\u00b7]\s*)$",
    re.IGNORECASE,
)
_WITH_BEFORE_RE = re.compile(r"(?:^|[\s(])(?:with|using)\s+$", re.IGNORECASE)
_SKILL_AFTER_RE = re.compile(r"^\s*(?:[,/);|]|\band\b|\bor\b|$|\(|\d)", re.IGNORECASE)
# What follows makes it an ordinary word after all: "Go-live", "Go to", "R&D",
# "C-suite", "Salt Lake City".
_WORD_AFTER_RE = re.compile(
    r"^(?:[-/]\w|\s*&|\s+(?:to|above|beyond|live|ahead|big|forward|further|back|home|public"
    r"|green|no[- ]go|us|lake)\b)",
    re.IGNORECASE,
)
_LIST_BEFORE_RE = re.compile(r"(?:[,/(|\u2022\u00b7]\s*|^\s*[-*]\s*)$")
_LIST_AFTER_RE = re.compile(r"^\s*(?:[,/)|]|$)")
# What precedes makes it a label or part of a name, not a skill: "Series C",
# "Toys R Us", "Hong Kong", "Head Chef".
_LABEL_BEFORE_RE = re.compile(
    r"(?:series|class|grade|vitamin|plan|phase|appendix|section|type|level|tier|round|toys|"
    r"objective|let['\u2019]s|lets|to|we|you|they|i|hong|king|head|sous|pastry|digital)[\s-]*$",
    re.IGNORECASE,
)


def canonical(term: str) -> str:
    lowered = squash(term).lower()
    return ALIASES.get(lowered, lowered)


def _spelling_pattern(spelling: str) -> str:
    """One spelling as a pattern: flexible about separators, "&" for "and", "SOC2" for "SOC 2"."""
    parts = spelling.split(" ")
    pattern = re.escape(parts[0])
    for part in parts[1:]:
        if part == "and":
            pattern += r"[\s\-/]*(?:and|&)"
            continue
        # A number may sit right against the word before it.
        gap = r"[\s\-/]*" if part[:1].isdigit() else r"[\s\-/]+"
        pattern += gap + re.escape(part)
    return pattern


@lru_cache(maxsize=4096)
def _term_pattern(term: str) -> re.Pattern[str] | None:
    """The ordinary spellings of a term (and its aliases) as one pattern."""
    lowered = squash(term).lower()
    spellings = {lowered} | {alias for alias, target in ALIASES.items() if target == lowered}
    if lowered in ALIASES:  # the term itself is an alias: also accept its canonical form
        target = ALIASES[lowered]
        spellings |= {target} | {alias for alias, t in ALIASES.items() if t == target}
    parts = [
        _spelling_pattern(spelling)
        for spelling in sorted(spellings, key=len, reverse=True)
        if spelling not in _AMBIGUOUS and spelling not in _WORDLIKE
    ]
    if not parts:
        return None
    body = "|".join(parts)
    # \b does not work next to symbols (c++, .net, ci/cd); use explicit guards.
    # A version number may follow directly: "C++17", "Java 21", "Python3".
    return re.compile(rf"(?<![A-Za-z0-9+#])(?:{body})(?![A-Za-z+#])", re.IGNORECASE)


def _ambiguous_names(term: str) -> tuple[str, ...]:
    """The spellings of ``term`` that are also ordinary words ("go" for "golang")."""
    lowered = squash(term).lower()
    names = {lowered, ALIASES.get(lowered, lowered)}
    names |= {alias for alias, target in ALIASES.items() if target in names}
    return tuple(sorted(name for name in names if name in _AMBIGUOUS or name in _WORDLIKE))


def _as_a_skill(text: str, name: str) -> bool:
    """Is ``name`` ("go", "r", "helm") used in ``text`` the way a skill is named?"""
    strict = name in _AMBIGUOUS
    proper = _AMBIGUOUS.get(name) or (name.capitalize(),)
    pattern = rf"(?<![A-Za-z0-9+#.]){re.escape(name)}(?![A-Za-z0-9+#])"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        found = match.group(0)
        before = text[max(0, match.start() - 14) : match.start()].split("\n")[-1]
        after = text[match.end() : match.end() + 14].split("\n")[0]
        if _WORD_AFTER_RE.match(after) or _LABEL_BEFORE_RE.search(before):
            continue
        listed = bool(_SKILL_AFTER_RE.match(after))
        if found in proper:
            if listed or _SKILL_BEFORE_RE.search(before):
                return True
            if strict:
                continue
            lead = before.strip()
            if lead in _BULLETS:
                if name not in _VERBS:
                    return True  # "- Helm chart development"
            elif lead and not lead.endswith((".", "!", "?")):
                return True  # a capital inside a sentence: "Deploying Helm charts"
        elif found.islower():
            if _LIST_BEFORE_RE.search(before) and _LIST_AFTER_RE.match(after):
                return True
            if not strict and listed and _WITH_BEFORE_RE.search(before):
                return True  # "experience with helm, kustomize"
    return False


def has_term(text: str, term: str) -> bool:
    """True when ``term`` (or a known alias) occurs in ``text`` as a whole term."""
    if not term.strip():
        return False
    pattern = _term_pattern(term)
    if pattern is not None and pattern.search(text):
        return True
    return any(_as_a_skill(text, name) for name in _ambiguous_names(term))


_LINKING = r"(?:\s+(?:is|are|will be|would be|to be))?"
_DENIAL_RE = re.compile(
    r"\b(?:no|not|never|without|non|neither|nor|none)\b|n['\u2019]t\b", re.IGNORECASE
)
_CLAUSE_END_RE = re.compile(r"[.;:!?\n]")


@lru_cache(maxsize=1024)
def _stated_pattern(phrase: str) -> re.Pattern[str] | None:
    """A phrase with room for "is"/"are" between its words: "relocation is required"."""
    words = squash(phrase).lower().split(" ")
    if len(words) < 2:
        return None
    body = (_LINKING + r"[\s\-/]+").join(re.escape(word) for word in words)
    return re.compile(rf"(?<![A-Za-z0-9+#])(?:{body})(?![A-Za-z+#])", re.IGNORECASE)


def states(text: str, phrase: str) -> bool:
    """Does ``text`` say ``phrase`` outright, rather than deny it?

    For rules of the form "skip a posting that says X". "Relocation is
    required" says "relocation required"; "No relocation required" and
    "relocation is not required" do not. A denial counts when it stands in
    the same clause within the three words before the phrase.
    """
    if not phrase.strip():
        return False
    patterns = [p for p in (_term_pattern(phrase), _stated_pattern(phrase)) if p is not None]
    if not patterns:
        return has_term(text, phrase)
    for pattern in patterns:
        for match in pattern.finditer(text):
            before = _CLAUSE_END_RE.split(text[max(0, match.start() - 60) : match.start()])[-1]
            if not _DENIAL_RE.search(" ".join(before.split()[-3:])):
                return True
    return False


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
