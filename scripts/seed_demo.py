"""Fill a throwaway data folder with sample roles and applications.

For looking at the app before pointing it at real career pages:

    JOBPORTAL_DATA_DIR=demo-data python scripts/seed_demo.py
    JOBPORTAL_DATA_DIR=demo-data jobportal serve --no-worker

Everything in it is invented (the example profile, fictional companies).
Nothing is fetched from the network and nothing is sent.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobportal.apply import ledger  # noqa: E402
from jobportal.apply.answers import save_answer  # noqa: E402
from jobportal.config import load_user_config  # noqa: E402
from jobportal.crawl import add_source, crawl  # noqa: E402
from jobportal.db import get_session_factory, init_db, utcnow  # noqa: E402
from jobportal.http import PoliteClient  # noqa: E402
from jobportal.inbox.imap import Fetched  # noqa: E402
from jobportal.inbox.ingest import ingest_inbox  # noqa: E402
from jobportal.manual import add_manual_job  # noqa: E402
from jobportal.models import Application, ApplicationEvent, Job, LedgerEntry  # noqa: E402
from jobportal.scoring import score_jobs  # noqa: E402
from jobportal.settings import get_settings  # noqa: E402
from jobportal.sources import SourceSpec  # noqa: E402
from jobportal.text import company_key  # noqa: E402
from jobportal.users import get_default_user  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
BOARDS = {
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": "greenhouse_jobs.json",
    "https://api.lever.co/v0/postings/globex?mode=json": "lever_postings.json",
    "https://api.ashbyhq.com/posting-api/job-board/initech?includeCompensation=true": "ashby_board.json",
}
EXTRA = [
    ("Chief Architect", "Helix Systems", "Remote - US", "Lead architecture for a Kubernetes platform on AWS and GCP. Terraform, GitOps with ArgoCD, zero trust, Vault. 15+ years of experience. Up to 25% travel. Base salary $260,000 - $320,000.", 2),
    ("Principal Cloud Security Architect", "Meridian Bank", "Remote (United States)", "Policy as code with OPA and Kyverno, zero trust, Vault PKI, supply chain security with Sigstore and SBOM on Kubernetes. AWS and Azure. 12+ years of experience. $230,000 - $280,000.", 7),
    ("Staff Platform Engineer, Streaming", "Quartz Data", "Austin, TX (Hybrid)", "Kafka and Flink platform on Kubernetes. Go, Terraform, GitOps. On-call rotation. 8+ years of experience. $190,000 - $235,000 per year.", 20),
    ("Director of Platform Engineering", "Northlake Health", "Remote - US", "Lead platform engineering: Kubernetes, AWS, Terraform, platform engineering practice, GitOps. 12+ years of experience. Visa sponsorship is not available.", 30),
    ("Principal Platform Engineer", "Orbital Defense", "Remote - US", "Kubernetes platform on AWS GovCloud with Terraform. Active TS/SCI clearance required. 10+ years of experience.", 9),
    ("Senior Platform Engineer", "Brightline", "Remote - US", "Kubernetes, AWS, Terraform, Go. 6+ years of experience.", 50),
    ("Platform Architect", "Wanderly", "London, UK", "Kubernetes platform on GCP with Terraform and ArgoCD.", 5),
]  # fmt: skip


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    for name in ("profile", "search", "resume"):
        target = settings.data_dir / f"{name}.yaml"
        if not target.exists():
            shutil.copy(ROOT / "config" / f"{name}.example.yaml", target)
    init_db()
    config = load_user_config(settings.data_dir)
    now = utcnow()

    def handler(request: httpx.Request) -> httpx.Response:
        name = BOARDS.get(str(request.url))
        if name is None:
            return httpx.Response(404)
        payload = json.loads((FIXTURES / name).read_text())
        return httpx.Response(200, json=payload)

    with get_session_factory()() as session:
        if session.query(Job).count():
            print("This data folder already has jobs; leaving it alone.")
            return
        user = get_default_user(session, config.profile)
        for kind, token, label in (
            ("greenhouse", "acme", ""),
            ("lever", "globex", "Globex"),
            ("ashby", "initech", "Initech"),
        ):
            add_source(session, SourceSpec(kind=kind, token=token), label)
        session.commit()

        settings.per_host_delay_seconds = 0
        with PoliteClient(settings, transport=httpx.MockTransport(handler)) as client:
            crawl(session, client, now=now - timedelta(hours=30))  # first read: a backlog
            crawl(session, client, now=now)

        def fetch(_settings, *, uidvalidity, last_uid, now):
            raw = (FIXTURES / "mail" / "requirement.eml").read_bytes()
            html = (FIXTURES / "mail" / "requirement_html.eml").read_bytes()
            return Fetched(uidvalidity=1, messages=[] if last_uid else [(1, raw), (2, html)])

        ingest_inbox(session, settings, config, user, fetch=fetch, now=now)
        for title, company, location, description, hours in EXTRA:
            job = add_manual_job(
                session,
                title=title,
                company=company,
                location=location,
                description=description,
                now=now,
            )
            job.posted_at = now - timedelta(hours=hours)
            job.url = f"https://careers.example/{company_key(company).replace(' ', '-')}"
        score_jobs(session, user.id, config.search, now=now, profile=config.profile)
        session.commit()

        by_role = {(job.title, job.company_name): job for job in session.query(Job)}
        by_role[("Chief Architect", "Helix Systems")].posted_at = now - timedelta(days=6)
        by_role[("Principal Cloud Security Architect", "Meridian Bank")].posted_at = (
            now - timedelta(days=11)
        )

        def application(
            title: str, company: str, channel: str, status: str, **fields
        ) -> Application:
            job = by_role[(title, company)]
            row = Application(
                user_id=user.id, job_id=job.id, channel=channel, status=status, **fields
            )
            row.events.append(ApplicationEvent(kind="prepared", detail={"status": status}, at=now))
            session.add(row)
            session.flush()
            return row

        application(
            "Cloud Architect", "Odysseytec", "email", "needs_review",
            prepared={
                "channel": "email", "to": "sai.kumar@odysseytec.example",
                "subject": "Re: Urgent requirement: Cloud Architect (Remote) || C2C",
                "body": "Hi Sai,\n\nI am interested in the Cloud Architect role with Southwind Air. I am a Cloud and Kubernetes Architect with 15 years of experience. From the requirements, my strongest areas are AWS, Terraform, Kubernetes, EKS, Python, CI/CD.\n\nAvailability: 2 weeks from offer\nLocation: Austin, TX\nEngagement: C2C or W2\nWork authorization: authorized to work in the United States; no sponsorship needed\n\nMy resume is attached. I am happy to share more detail or set up a call.\n\nRegards,\nAlex Example\n+1 512 555 0142\nhttps://www.linkedin.com/in/alex-example\n",
                "attachments": ["Alex_Example_Resume.pdf"], "client": "Southwind Air", "vendor": "Odysseytec", "auto_notes": [],
            },
        )  # fmt: skip
        application(
            "Principal Platform Engineer", "Acme Robotics", "form", "needs_answers",
            prepared={
                "channel": "form", "url": "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=8172508",
                "fields": [
                    {"label": "First Name", "value": "Alex", "source": "profile", "required": True},
                    {"label": "Last Name", "value": "Example", "source": "profile", "required": True},
                    {"label": "Email", "value": "alex@example.com", "source": "profile", "required": True},
                    {"label": "Resume/CV", "value": "Alex_Example_Resume.pdf", "source": "tailored resume", "required": True},
                ],
                "unanswered": [
                    {"key": "why do you want to work at acme robotics", "label": "Why do you want to work at Acme Robotics?", "type": "textarea", "required": True, "options": []},
                    {"key": "how many people have you led directly", "label": "How many people have you led directly?", "type": "select", "required": True, "options": ["None", "1-5", "6-15", "More than 15"]},
                ],
                "left_blank": ["Cover Letter"],
            },
        )  # fmt: skip
        application(
            "Platform Architect", "Globex", "form", "needs_human",
            blockers=[{"kind": "bot_check", "detail": "The form is protected by a bot check (hCaptcha), so it is yours to submit."}],
            prepared={
                "channel": "form", "url": "https://jobs.lever.co/globex/681fbc53-1e34-4a46-8677-3a78118674eb/apply",
                "fields": [
                    {"label": "Full name", "value": "Alex Example", "source": "profile", "required": True},
                    {"label": "Email", "value": "alex@example.com", "source": "profile", "required": True},
                    {"label": "Current company", "value": "Northwind Systems", "source": "profile", "required": False},
                    {"label": "LinkedIn URL", "value": "https://www.linkedin.com/in/alex-example", "source": "profile", "required": False},
                ],
                "unanswered": [], "left_blank": ["Additional information"],
            },
        )  # fmt: skip
        sent = application(
            "Chief Architect", "Helix Systems", "form", "replied",
            submitted_at=now - timedelta(days=4), next_action="Reply to Dana (recruiter)",
            confirmation="Thank you for applying. Your application has been received.",
            prepared={"channel": "form", "fields": [{"label": "Full name", "value": "Alex Example", "source": "profile", "required": True}]},
        )  # fmt: skip
        ledger.record(session, sent, now=now - timedelta(days=4))
        waiting = application(
            "Principal Cloud Security Architect", "Meridian Bank", "form", "submitted", auto=True,
            submitted_at=now - timedelta(days=9), follow_up_at=now - timedelta(days=2),
            next_action="Follow up if there is no reply",
            confirmation="Thank you for applying.",
            prepared={"channel": "form", "fields": []},
        )  # fmt: skip
        ledger.record(session, waiting, now=now - timedelta(days=9))
        session.add(
            LedgerEntry(
                user_id=user.id,
                client_name="Southwind Air",
                client_key=company_key("Southwind Air"),
                role_title="AWS Platform Architect",
                vendor_name="Insight Partners",
                vendor_key=company_key("Insight Partners"),
                vendor_contact="pat@insight.example",
                channel="manual",
                engagement="c2c",
                rate="$105/hr",
                submitted_at=now - timedelta(days=21),
                notes="Submitted by phone",
            )
        )
        save_answer(session, user.id, "Do you have a non-compete agreement?", "No")
        save_answer(session, user.id, "Are you open to working Central Time hours?", "Yes")
        session.commit()
        print(f"Seeded {session.query(Job).count()} roles into {settings.data_dir.resolve()}")


if __name__ == "__main__":
    main()
