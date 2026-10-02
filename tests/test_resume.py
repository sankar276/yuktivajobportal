from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from pypdf import PdfReader
from sqlalchemy.orm import Session

from jobportal.config import ConfigError, UserConfig
from jobportal.llm import LLM, rephrase_bullets
from jobportal.models import Job, Source, User
from jobportal.resume.guard import accept_rewrite, rewrite_violations
from jobportal.resume.render import render_html, resume_basename, write_docx, write_pdf
from jobportal.resume.service import build_resume
from jobportal.resume.tailor import tailor
from jobportal.settings import Settings
from tests.conftest import NOW

SECURITY_POSTING = """
We need a cloud security architect to own policy as code (OPA, Kyverno) and
zero trust identity with Vault and OIDC. Supply chain integrity (Sigstore, SBOM)
matters. You will also work with Snowflake and Airflow.
"""


def _all_bank_text(config: UserConfig) -> str:
    bank = config.resume
    parts = [b.text for role in bank.experience for b in role.bullets]
    parts += [item for group in bank.skills for item in group.items]
    return "\n".join(parts)


# ------------------------------------------------------------------ tailor


def test_tailor_selects_relevant_bullets_within_budget(user_config: UserConfig) -> None:
    result = tailor(
        user_config.resume,
        user_config.profile,
        title="Cloud Security Architect",
        description=SECURITY_POSTING,
        variant="security",
    )
    resume = result.resume
    assert resume.headline == "Cloud Security Architect"
    assert resume.summary.startswith("Cloud security architect who turns policy into code")

    northwind = resume.experience[0]
    assert len(northwind.bullets) == 6  # max_bullets_recent, out of 9 in the bank
    assert northwind.bullets[0].startswith("Own the architecture")  # pinned stays first
    kept = " ".join(northwind.bullets)
    for needed in ("zero-trust service identity", "policy-as-code guardrails", "Sigstore"):
        assert needed in kept
    assert "Cut AWS spend" not in kept  # irrelevant here, so it gave up its place
    assert len(resume.experience[2].bullets) == 2  # older role: its own (smaller) budget
    assert "Northwind Systems: kept 6 of 9 bullets" in result.changes


def test_tailor_never_adds_anything_that_is_not_in_the_bank(user_config: UserConfig) -> None:
    result = tailor(
        user_config.resume,
        user_config.profile,
        title="Cloud Security Architect",
        description=SECURITY_POSTING,
        variant="security",
    )
    bank_text = _all_bank_text(user_config)
    for role in result.resume.experience:
        for bullet in role.bullets:
            assert bullet in bank_text
    bank_skills = {item for group in user_config.resume.skills for item in group.items}
    tailored_skills = [item for group in result.resume.skills for item in group.items]
    assert set(tailored_skills) == bank_skills and len(tailored_skills) == len(bank_skills)
    # Asked for, not in the bank: reported as gaps, and absent from the resume.
    assert result.gaps == ["Airflow", "Snowflake"]
    rendered = render_html(result.resume)
    assert "Snowflake" not in rendered and "Airflow" not in rendered
    assert any("not in your bank (not added): Airflow, Snowflake" in c for c in result.changes)


def test_tailor_puts_matched_skills_first(user_config: UserConfig) -> None:
    result = tailor(
        user_config.resume,
        user_config.profile,
        title="Cloud Security Architect",
        description=SECURITY_POSTING,
        variant="security",
    )
    assert result.resume.skills[0].group == "Security"
    assert result.resume.skills[0].items[:5] == ["Zero trust", "OPA", "Kyverno", "Vault", "OIDC"]
    assert {"OPA", "Kyverno", "Vault", "Sigstore", "SBOM"} <= set(result.matched)


def test_different_postings_give_different_resumes(user_config: UserConfig) -> None:
    def cut(description: str) -> list[str]:
        result = tailor(
            user_config.resume,
            user_config.profile,
            title="Platform Engineer",
            description=description,
        )
        return result.resume.experience[0].bullets

    streaming = cut("Kafka and Flink streaming platform, regional failover, reliability.")
    cost = cut("AWS cost optimisation, FinOps, Terraform tagging standards, Savings Plans.")
    assert streaming != cost
    assert any("Kafka and Flink" in b for b in streaming[:3])
    assert any("Cut AWS spend" in b for b in cost[:3])


def test_variant_restricted_bullets_only_appear_in_their_variant(user_config: UserConfig) -> None:
    bank = user_config.resume.model_copy(deep=True)
    bank.experience[0].bullets[1].variants = ["security"]
    restricted = bank.experience[0].bullets[1].text
    default = tailor(bank, user_config.profile, title="x", description="GitOps ArgoCD migration")
    security = tailor(
        bank,
        user_config.profile,
        title="x",
        description="GitOps ArgoCD migration",
        variant="security",
    )
    assert restricted not in default.resume.experience[0].bullets
    assert restricted in security.resume.experience[0].bullets


def test_unknown_variant_is_a_config_error(user_config: UserConfig) -> None:
    with pytest.raises(ConfigError, match="executive"):
        tailor(
            user_config.resume, user_config.profile, title="x", description="y", variant="executive"
        )


def test_contact_line_strips_url_schemes(user_config: UserConfig) -> None:
    result = tailor(user_config.resume, user_config.profile, title="x", description="y")
    assert result.resume.contact == [
        "alex@example.com",
        "+1 512 555 0142",
        "Austin, TX",
        "linkedin.com/in/alex-example",
        "github.com/alex-example",
    ]


# ------------------------------------------------------------------ render


@pytest.mark.browser
def test_pdf_and_docx_contain_the_resume(
    user_config: UserConfig, settings: Settings, tmp_path: Path, browser
) -> None:
    result = tailor(
        user_config.resume,
        user_config.profile,
        title="Principal Platform Engineer",
        description="Kubernetes, GitOps, Terraform, Kafka",
    )
    html = render_html(result.resume)
    assert "<script" not in html

    pdf = write_pdf(html, tmp_path / "out" / "resume.pdf", settings=settings, browser=browser)
    reader = PdfReader(str(pdf))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert 1 <= len(reader.pages) <= 2
    for expected in (
        "Alex Example",
        "SUMMARY",
        "EXPERIENCE",
        "Northwind Systems",
        "alex@example.com",
    ):
        assert expected in text
    assert "Own the architecture of a Kubernetes platform" in " ".join(text.split())

    docx = write_docx(result.resume, tmp_path / "out" / "resume.docx")
    paragraphs = [p.text for p in Document(str(docx)).paragraphs]
    assert paragraphs[0] == "Alex Example"
    assert "EXPERIENCE" in paragraphs
    assert any(p.startswith("Principal Platform Engineer, Northwind Systems") for p in paragraphs)
    assert any("Own the architecture of a Kubernetes platform" in p for p in paragraphs)


def test_html_escapes_content(user_config: UserConfig) -> None:
    bank = user_config.resume.model_copy(deep=True)
    bank.experience[0].bullets[0].text = "Shipped <script>alert(1)</script> & more"
    result = tailor(bank, user_config.profile, title="x", description="y")
    html = render_html(result.resume)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html and "&amp; more" in html


def test_resume_basename() -> None:
    assert resume_basename("Alex Example") == "Alex_Example_Resume"
    assert resume_basename("José O'Brien-Smith") == "Jose_O_Brien_Smith_Resume"


# ------------------------------------------------------------------- guard

ORIGINAL = "Migrated delivery for 120 services to GitOps with ArgoCD, cutting deploy time from 45 to 9 minutes."


@pytest.mark.parametrize(
    "rewrite",
    [
        "Cut deploy time from 45 to 9 minutes by migrating delivery for 120 services to GitOps with ArgoCD.",
        "Cutting deploy time from 45 to 9 minutes, migrated delivery for 120 services to GitOps.",
    ],
)
def test_guard_accepts_rewording(rewrite: str) -> None:
    assert rewrite_violations(ORIGINAL, rewrite) == []
    assert accept_rewrite(ORIGINAL, rewrite) == rewrite


@pytest.mark.parametrize(
    ("rewrite", "problem"),
    [
        (ORIGINAL.replace("120", "200"), "adds numbers"),
        (ORIGINAL.replace("9 minutes", "9 minutes (80% faster)"), "adds numbers"),
        (ORIGINAL.replace("with ArgoCD", "with ArgoCD and Terraform"), "adds skills"),
        (ORIGINAL.replace("services", "services at Goldman Sachs"), "adds names"),
        (ORIGINAL.replace("services", "services at goldman sachs"), "adds words"),
        # New verbs are new claims too: only your own words, reordered or trimmed.
        (
            "Moved 120 services to GitOps delivery with ArgoCD; deploy time fell from 45 to 9 minutes.",
            "adds words",
        ),
        (ORIGINAL.replace("9 minutes", "9 months"), "changes what a number refers to"),
        (ORIGINAL.replace("from 45 to 9", "from 9 to 45"), "changes what a number refers to"),
        (ORIGINAL.replace("120 services", "120 teams"), "adds words"),
        (ORIGINAL + " Promoted to vice president.", "adds a sentence"),
        (
            ORIGINAL + " Led a team of engineers across several regions and business units.",
            "much longer",
        ),
        ("", "empty rewrite"),
    ],
)
def test_guard_rejects_new_facts(rewrite: str, problem: str) -> None:
    problems = rewrite_violations(ORIGINAL, rewrite, vocabulary=["Terraform"])
    assert any(problem in p for p in problems), problems
    assert accept_rewrite(ORIGINAL, rewrite, vocabulary=["Terraform"]) == ORIGINAL


# --------------------------------------------------------------------- llm


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic``; records the request it was sent."""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def test_llm_is_disabled_without_a_key(settings: Settings) -> None:
    llm = LLM(settings)
    assert not llm.enabled
    assert llm.complete(system="s", prompt="p") is None
    assert rephrase_bullets(llm, {"a": "x"}, title="t", description="d", vocabulary=[]) == ({}, [])


def test_rephrase_keeps_only_guarded_rewrites(settings: Settings) -> None:
    bullets = {
        "b1": ORIGINAL,
        "b2": "Ran the first production EKS clusters and wrote the onboarding guide.",
        "b3": "Automated server builds for 1,200 Linux hosts.",
    }
    reply = json.dumps(
        {
            "b1": "Cut deploy time from 45 to 9 minutes by migrating delivery for 120 services to GitOps with ArgoCD.",
            "b2": "Ran the first production EKS clusters for 300 engineers and wrote the onboarding guide.",
            "b3": bullets["b3"],
        }
    )
    fake = FakeAnthropic(f"Here you go:\n```json\n{reply}\n```")
    accepted, notes = rephrase_bullets(
        LLM(settings, client=fake),
        bullets,
        title="Platform Engineer",
        description="GitOps",
        vocabulary=[],
    )
    assert list(accepted) == ["b1"]  # b2 invented "300 engineers"; b3 was unchanged
    assert notes == [
        "Reworded 1 bullets to mirror the posting (facts unchanged)",
        "Refused 1 rewordings that would have added facts; originals kept",
    ]
    request = fake.requests[0]
    assert request["model"] == settings.llm_model
    assert "must not add any fact" in request["system"]
    assert "<posting>" in request["messages"][0]["content"]  # the posting is fenced off
    assert request["messages"][0]["role"] == "user"


def test_llm_failure_degrades_to_no_change(settings: Settings) -> None:
    llm = LLM(settings, client=FakeAnthropic(RuntimeError("boom")))
    accepted, notes = rephrase_bullets(
        llm, {"b1": ORIGINAL}, title="t", description="d", vocabulary=[]
    )
    assert accepted == {} and "no usable reply" in notes[0]


# ----------------------------------------------------------------- service


def _job(session: Session) -> Job:
    source = Source(kind="manual", token="manual", initialized=True)
    session.add(source)
    session.flush()
    job = Job(
        source_id=source.id,
        external_id="j1",
        company_name="Acme Robotics",
        company_key="acme robotics",
        title="Principal Platform Engineer",
        fingerprint="f1",
        description_text="Kubernetes platform on AWS with Terraform, GitOps and Kafka.",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(job)
    session.flush()
    return job


@pytest.mark.browser
def test_build_resume_stores_files_and_reuses_identical_content(
    session: Session, settings: Settings, user: User, user_config: UserConfig, browser
) -> None:
    job = _job(session)
    first = build_resume(session, settings, user_config, user, job, browser=browser)
    assert Path(first.pdf_path).name == "Alex_Example_Resume.pdf"
    assert Path(first.pdf_path).exists() and Path(first.docx_path).exists()
    assert f"job-{job.id}" in first.pdf_path
    assert "Kubernetes" in first.matched and first.content["name"] == "Alex Example"

    again = build_resume(session, settings, user_config, user, job, browser=browser)
    assert again.id == first.id  # nothing changed, nothing re-rendered

    job.description_text = "Vault, OPA and Kyverno policy as code. Zero trust."
    changed = build_resume(
        session, settings, user_config, user, job, variant="security", browser=browser
    )
    assert changed.id != first.id and changed.pdf_path != first.pdf_path
    assert Path(first.pdf_path).exists()  # the earlier version is kept as sent


@pytest.mark.browser
def test_build_resume_applies_guarded_rewrites_when_enabled(
    session: Session,
    settings: Settings,
    user: User,
    user_config: UserConfig,
    monkeypatch: pytest.MonkeyPatch,
    browser,
) -> None:
    job = _job(session)
    pinned = user_config.resume.experience[0].bullets[0]
    reworded = (
        "Own the architecture of a Kubernetes platform running 400 services across three "
        "regions, on-premises clusters included."
    )
    fake = FakeAnthropic(json.dumps({pinned.id: reworded}))
    monkeypatch.setattr(settings, "llm_rephrase", True)

    variant = build_resume(
        session, settings, user_config, user, job, llm=LLM(settings, client=fake), browser=browser
    )

    assert variant.content["experience"][0]["bullets"][0] == reworded
    assert any("Reworded 1 bullets" in change for change in variant.changes)
