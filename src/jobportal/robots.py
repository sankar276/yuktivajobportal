"""robots.txt parsing and matching per RFC 9309.

The standard library's ``urllib.robotparser`` applies rules in file order; the
RFC says the longest matching rule wins and ``Allow`` wins ties. Career sites
routinely rely on that (``Disallow: /`` plus ``Allow: /jobs/``), so we
implement the RFC's algorithm directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        return bool(_compile(self.pattern).match(path))


_COMPILED: dict[str, re.Pattern[str]] = {}


def _compile(pattern: str) -> re.Pattern[str]:
    compiled = _COMPILED.get(pattern)
    if compiled is None:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
        compiled = re.compile(regex + ("$" if anchored else ""))
        _COMPILED[pattern] = compiled
    return compiled


def _normalize(path: str) -> str:
    # Compare on decoded paths so "/a%2Db" and "/a-b" are the same resource.
    return unquote(path) or "/"


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
        token = product_token.lower()
        groups: list[tuple[list[str], list[Rule]]] = []
        agents: list[str] = []
        rules: list[Rule] = []
        collecting_agents = False

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
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
                if value:  # an empty Disallow means "no restriction"
                    rules.append(Rule(allow=key == "allow", pattern=_normalize(value)))
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


def _agent_matches(name: str, token: str) -> bool:
    """A group names us when its product token equals ours (case-insensitive)."""
    return name != "*" and name == token
