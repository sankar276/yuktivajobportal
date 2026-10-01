"""Optional Claude assistance.

Nothing in the pipeline depends on this module: without an API key every
caller falls back to its deterministic behaviour. Where a model's output can
reach something sent under your name, it goes through
:mod:`jobportal.resume.guard` first.

API reference: https://platform.claude.com/docs/en/api/messages
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jobportal.resume.guard import rewrite_violations
from jobportal.settings import Settings, get_settings

log = logging.getLogger(__name__)

_REPHRASE_SYSTEM = (
    "You edit resume bullets so their wording mirrors a job posting. You may reorder a "
    "bullet and swap in the posting's vocabulary for things the bullet already says. "
    "You must not add any fact: no new numbers, tools, technologies, employers, team "
    "sizes, scope or outcomes, and no claims the bullet does not already make. If a "
    "bullet cannot be improved without adding facts, return it unchanged. Reply with "
    "only a JSON object that maps each bullet id to its text."
)


class LLM:
    """A thin wrapper over the Anthropic Messages API."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client is not None or self.settings.llm_configured

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # the "llm" extra is not installed
                raise RuntimeError("install the LLM extra: pip install 'jobportal[llm]'") from exc
            key = self.settings.anthropic_api_key
            assert key is not None
            self._client = Anthropic(api_key=key.get_secret_value(), max_retries=2, timeout=60.0)
        return self._client

    def complete(self, *, system: str, prompt: str, max_tokens: int = 2000) -> str | None:
        """The model's text reply, or ``None`` when unavailable or failing."""
        if not self.enabled:
            return None
        try:
            message = self._get_client().messages.create(
                model=self.settings.llm_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # network, auth, rate limit: degrade, never crash the pipeline
            log.warning(
                "LLM request failed (%s: %s); continuing without it", type(exc).__name__, exc
            )
            return None
        return "".join(block.text for block in message.content if block.type == "text") or None


def _json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def rephrase_bullets(
    llm: LLM,
    bullets: dict[str, str],
    *,
    title: str,
    description: str,
    vocabulary: list[str],
) -> tuple[dict[str, str], list[str]]:
    """Ask for posting-aligned wording; keep only rewrites that pass the fact guard.

    Returns ``(accepted, notes)``: ``accepted`` maps bullet id to its new text,
    ``notes`` says what was changed and what was refused, for the change list.
    """
    if not llm.enabled or not bullets:
        return {}, []
    prompt = (
        f"Job title: {title}\n\nJob posting (excerpt):\n{description[:6000]}\n\n"
        f"Bullets:\n{json.dumps(bullets, indent=2, ensure_ascii=False)}"
    )
    reply = llm.complete(system=_REPHRASE_SYSTEM, prompt=prompt)
    data = _json_object(reply) if reply else None
    if data is None:
        return {}, ["Rewording skipped: no usable reply from the model"]

    accepted: dict[str, str] = {}
    refused = 0
    for bullet_id, original in bullets.items():
        candidate = data.get(bullet_id)
        if not isinstance(candidate, str) or candidate.strip() == original.strip():
            continue
        problems = rewrite_violations(original, candidate, vocabulary)
        if problems:
            refused += 1
            log.info("rewrite of %s refused: %s", bullet_id, "; ".join(problems))
        else:
            accepted[bullet_id] = candidate.strip()
    notes = []
    if accepted:
        notes.append(f"Reworded {len(accepted)} bullets to mirror the posting (facts unchanged)")
    if refused:
        notes.append(f"Refused {refused} rewordings that would have added facts; originals kept")
    return accepted, notes
