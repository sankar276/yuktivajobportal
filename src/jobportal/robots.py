"""robots.txt parsing and matching per RFC 9309.

The standard library's ``urllib.robotparser`` applies rules in file order; the
RFC says the longest matching rule wins and ``Allow`` wins ties. Career sites
routinely rely on that (``Disallow: /`` plus ``Allow: /jobs/``), so we
implement the RFC's algorithm directly.

The file is somebody else's input, so nothing here can be made slow or large
by it: patterns are matched without a regular expression, and the number of
rules kept is bounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

MAX_RULES = 2000
MAX_LINE_CHARS = 2000
_ESCAPE_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
_TOKEN_RE = re.compile(r"[a-z_-]+")


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        return _matches(self.pattern, path)


def _matches(pattern: str, path: str) -> bool:
    """Does ``pattern`` (``*`` wildcards, optional ``$`` end anchor) match from the start of ``path``?

    Each literal piece is located with a plain search, left to right. Taking
    the earliest place a piece fits never rules out a later match, so there is
    no backtracking and the time is bounded by pattern length times path length.
    """
    anchored = pattern.endswith("$")
    pieces = (pattern[:-1] if anchored else pattern).split("*")
    first, rest = pieces[0], pieces[1:]
    if not path.startswith(first):
        return False
    position = len(first)
    if not rest:
        return position == len(path) if anchored else True
    last = rest.pop()
    for piece in rest:
        found = path.find(piece, position)
        if found < 0:
            return False
        position = found + len(piece)
    if anchored:
        return len(path) - len(last) >= position and path.endswith(last)
    return path.find(last, position) >= 0


def _normalize(path: str) -> str:
    """One spelling per resource, without changing which resource it is.

    Escapes of ordinary characters are undone ("/a%2Db" is "/a-b"); escapes of
    characters with a meaning of their own, the slash above all, are kept, so
    "/a%2Fb" stays different from "/a/b". Characters outside ASCII are escaped.
    """

    def unescape(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    path = _ESCAPE_RE.sub(unescape, path)
    if not path.isascii():
        path = quote(path, safe="/%?=&*$-._~:@!'()+,;#[]")
    return path or "/"


@dataclass
class RobotsRules:
    """The rules that apply to one crawler on one host."""

    rules: list[Rule] = field(default_factory=list)
    #: Set when robots.txt could not be fetched (5xx / network): assume disallow.
    unreachable: bool = False

    @classmethod
    def allow_all(cls) -> RobotsRules:
        return cls()

    @classmethod
    def disallow_all(cls) -> RobotsRules:
        return cls(unreachable=True)

    @classmethod
    def parse(cls, text: str, product_token: str) -> RobotsRules:
        token = _product(product_token)
        groups: list[tuple[list[str], list[Rule]]] = []
        agents: list[str] = []
        rules: list[Rule] = []
        collecting_agents = False
        kept = 0

        # A byte-order mark in front of the first line would otherwise hide it.
        for raw_line in text.lstrip("﻿").splitlines():
            line = raw_line[:MAX_LINE_CHARS].split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not collecting_agents:
                    if agents:
                        groups.append((agents, rules))
                    agents, rules = [], []
                    collecting_agents = True
                agents.append(value.lower())
            elif key in ("allow", "disallow"):
                collecting_agents = False
                if not agents:
                    continue  # rule before any User-agent line: ignored
                if value and kept < MAX_RULES:  # an empty Disallow means "no restriction"
                    rules.append(Rule(allow=key == "allow", pattern=_normalize(value)))
                    kept += 1
            else:
                # sitemap, crawl-delay, host ... do not end a group of agents.
                continue
        if agents:
            groups.append((agents, rules))

        specific = [
            r for names, rs in groups if any(_agent_matches(n, token) for n in names) for r in rs
        ]
        has_specific = any(_agent_matches(n, token) for names, _ in groups for n in names)
        if has_specific:
            return cls(rules=specific)
        wildcard = [r for names, rs in groups if "*" in names for r in rs]
        return cls(rules=wildcard)

    def allowed(self, path: str) -> bool:
        if self.unreachable:
            return False
        path = _normalize(path)
        if path == "/robots.txt":
            return True
        best: Rule | None = None
        for rule in self.rules:
            if not rule.matches(path):
                continue
            if (
                best is None
                or rule.specificity > best.specificity
                or (rule.specificity == best.specificity and rule.allow and not best.allow)
            ):
                best = rule
        return True if best is None else best.allow


def _product(value: str) -> str:
    """The product token of a User-agent value: "YuktivaJobPortal/0.1 (+url)" names "yuktivajobportal"."""
    found = _TOKEN_RE.match(value.strip().lower())
    return found.group(0) if found else ""


def _agent_matches(name: str, token: str) -> bool:
    """A group names us when its product token equals ours (case-insensitive)."""
    return name != "*" and bool(token) and _product(name) == token
