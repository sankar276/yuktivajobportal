"""Command line: ``jobportal --help``."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal import __version__
from jobportal.config import (
    PROFILE_FILE,
    RESUME_FILE,
    SEARCH_FILE,
    ConfigError,
    UserConfig,
    load_user_config,
)
from jobportal.db import get_session_factory, init_db, utcnow
from jobportal.models import WAITING_STATUSES, Application, AppStatus, Job, JobScore, Source
from jobportal.settings import Settings, get_settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Watch career pages, score roles, tailor your resume, apply, and track.",
)
sources_app = typer.Typer(no_args_is_help=True, help="The career pages to watch.")
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(sources_app, name="sources")
app.add_typer(db_app, name="db")

EXAMPLES = Path(__file__).resolve().parents[2] / "config"
PACKAGED_EXAMPLES = Path(__file__).parent / "examples"


def _examples_dir() -> Path:
    return EXAMPLES if EXAMPLES.is_dir() else PACKAGED_EXAMPLES


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _config(settings: Settings) -> UserConfig:
    try:
        return load_user_config(settings.data_dir)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


@contextmanager
def _session() -> Iterator[Session]:
    init_db()
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show what is happening in detail.")
    ] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


# ------------------------------------------------------------------- set-up


@app.command()
def init() -> None:
    """Create the data folder with example profile, search and resume files."""
    settings = get_settings()
    settings.ensure_dirs()
    for name in (PROFILE_FILE, SEARCH_FILE, RESUME_FILE):
        target = settings.data_dir / name
        if target.exists():
            typer.echo(f"kept     {target}")
            continue
        shutil.copy(_examples_dir() / name.replace(".yaml", ".example.yaml"), target)
        typer.echo(f"created  {target}")
    init_db()
    typer.echo(f"database {settings.resolved_database_url.split('@')[-1]}")
    typer.echo(
        "\nNext: put your own details into the three YAML files, then\n"
        "  jobportal check          to confirm they are valid\n"
        "  jobportal sources add    to choose the career pages to watch\n"
        "  jobportal serve          to open the app"
    )


@app.command()
def check() -> None:
    """Validate your configuration and show what is switched on."""
    settings = get_settings()
    config = _config(settings)
    policy = config.search.policy
    typer.echo(f"Profile   {config.profile.name} <{config.profile.email}>")
    typer.echo(f"Lanes     {', '.join(lane.name for lane in config.search.lanes)}")
    typer.echo(
        f"Resume    {len(config.resume.experience)} roles, variants: {', '.join(config.resume.variants)}"
    )
    if policy.mode == "auto":
        typer.echo(
            f"Sending   AUTO: score >= {policy.auto.min_score:.0f}, up to {policy.auto.daily_cap} a day, "
            f"channels: {', '.join(str(c) for c in policy.auto.channels)}"
        )
    else:
        typer.echo("Sending   review: nothing goes out without your approval")
    typer.echo(
        f"Mail out  {'configured (' + str(settings.smtp_host) + ')' if settings.smtp_configured else 'not configured: email applications are saved as drafts'}"
    )
    typer.echo(
        f"Mail in   {'configured (' + settings.imap_folder + ')' if settings.imap_configured else 'not configured'}"
    )
    typer.echo(
        f"Claude    {'on (' + settings.llm_model + ')' if settings.llm_configured else 'off'}"
        + ("; rewording bullets" if settings.llm_configured and settings.llm_rephrase else "")
    )
    auth = config.profile.work_authorization
    if auth.authorized is None or auth.needs_sponsorship is None:
        typer.secho(
            "Note      work_authorization is not filled in: applications that ask will wait for you.",
            fg=typer.colors.YELLOW,
        )
    try:
        from jobportal.browser import launch_browser

        with launch_browser(settings, headless=True) as browser:
            typer.echo(f"Browser   Chromium {browser.version}")
    except Exception as exc:
        typer.secho(f"Browser   not available: {exc}", fg=typer.colors.YELLOW)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Create or upgrade the database schema."""
    init_db()
    typer.echo("Database is up to date.")


# ------------------------------------------------------------------ sources


@sources_app.command("add")
def sources_add(
    url: Annotated[
        str,
        typer.Argument(
            help="A job board URL (Greenhouse, Lever, Ashby, Workday) or a company careers page."
        ),
    ],
    name: Annotated[str, typer.Option(help="Company name to show.")] = "",
) -> None:
    """Watch a company's job board."""
    from jobportal.crawl import add_source
    from jobportal.http import FetchError, PoliteClient
    from jobportal.sources import discover_sources

    with PoliteClient(get_settings()) as client:
        try:
            specs = discover_sources(client, url)
        except FetchError as exc:
            raise _fail(f"Could not read {url}: {exc}") from exc
    if not specs:
        raise _fail(
            "No supported job board found there. Supported: Greenhouse, Lever, Ashby and Workday. "
            "Open the company's careers page, click a job, and pass that URL instead."
        )
    with _session() as session:
        for spec in specs:
            source, created = add_source(session, spec, name)
            typer.echo(
                f"{'added  ' if created else 'exists '} #{source.id} {source.kind} {source.token}"
            )


@sources_app.command("import")
def sources_import(
    file: Annotated[
        Path,
        typer.Argument(
            help="Text file with one board URL per line; optional ', Company Name' after it."
        ),
    ],
) -> None:
    """Add many boards from a file. Board URLs only (no page fetches)."""
    from jobportal.crawl import add_source
    from jobportal.sources import detect_source

    if not file.is_file():
        raise _fail(f"{file} not found")
    added = skipped = 0
    with _session() as session:
        for raw in file.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            url, _, label = (part.strip() for part in line.partition(","))
            spec = detect_source(url)
            if spec is None:
                typer.secho(f"not a supported board URL: {url}", fg=typer.colors.YELLOW)
                skipped += 1
                continue
            _source, created = add_source(session, spec, label)
            added += created
    typer.echo(f"Added {added} sources ({skipped} lines skipped).")


@sources_app.command("list")
def sources_list() -> None:
    """Show watched boards and how the last read went."""
    with _session() as session:
        rows = session.scalars(select(Source).order_by(Source.id)).all()
        if not rows:
            typer.echo("No sources yet. Add one with: jobportal sources add <url>")
            return
        for source in rows:
            state = source.last_status + ("" if source.enabled else " (paused)")
            typer.echo(
                f"#{source.id:<4} {source.kind:<11} {source.label:<32} {source.jobs_open:>5} open  {state}"
            )
            if source.last_error and source.last_status not in ("ok", "unchanged"):
                typer.echo(f"      {source.last_error}")


@sources_app.command("remove")
def sources_remove(source_id: int) -> None:
    """Stop watching a board and delete its postings."""
    with _session() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise _fail(f"No source #{source_id}")
        session.delete(source)
        typer.echo(f"Removed #{source_id} {source.label}")


# ----------------------------------------------------------------- pipeline


def _print(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


@app.command()
def crawl() -> None:
    """Read every watched board and score what is new."""
    from jobportal.http import PoliteClient
    from jobportal.pipeline import RunSummary, crawl_and_score

    settings = get_settings()
    config = _config(settings)
    summary = RunSummary()
    with _session() as session, PoliteClient(settings) as client:
        crawl_and_score(session, config, client, summary, now=utcnow())
    _print(summary.lines())


@app.command()
def score(force: Annotated[bool, typer.Option(help="Rescore everything.")] = False) -> None:
    """Score open postings against your lanes (no network)."""
    from jobportal.scoring import score_jobs
    from jobportal.users import get_default_user

    settings = get_settings()
    config = _config(settings)
    with _session() as session:
        user = get_default_user(session, config.profile)
        stats = score_jobs(session, user.id, config.search, profile=config.profile, force=force)
    typer.echo(
        f"Scored {stats.scored}: {stats.shortlisted} shortlisted, {stats.considered} to consider, "
        f"{stats.skipped} skipped ({stats.unchanged} unchanged)."
    )


@app.command()
def inbox() -> None:
    """Read new recruiter mail: requirements become jobs, replies move the tracker."""
    from jobportal.inbox.imap import InboxError
    from jobportal.inbox.ingest import ingest_inbox
    from jobportal.users import get_default_user

    settings = get_settings()
    config = _config(settings)
    with _session() as session:
        user = get_default_user(session, config.profile)
        try:
            stats = ingest_inbox(session, settings, config, user)
        except InboxError as exc:
            raise _fail(str(exc)) from exc
    typer.echo(stats.line())


@app.command()
def jobs(
    limit: Annotated[int, typer.Option(help="How many to show.")] = 25,
    everything: Annotated[
        bool, typer.Option("--all", help="Include roles that are not shortlisted.")
    ] = False,
) -> None:
    """Show the best open roles."""
    with _session() as session:
        query = (
            select(Job, JobScore)
            .join(JobScore, JobScore.job_id == Job.id)
            .where(Job.closed_at.is_(None), JobScore.hidden.is_(False))
            .order_by(JobScore.score.desc(), Job.id)
            .limit(limit)
        )
        if not everything:
            query = query.where(JobScore.decision == "shortlist")
        rows = session.execute(query).all()
        if not rows:
            typer.echo("Nothing on the shortlist yet. Run: jobportal crawl")
            return
        for job, job_score in rows:
            typer.echo(
                f"{job_score.score:5.1f}  #{job.id:<5} {job.title} - {job.company_name} ({job.location or 'location not stated'})"
            )
            if job_score.reasons:
                typer.echo(f"        {job_score.reasons[0]}")


@app.command()
def queue() -> None:
    """Show applications waiting on you."""
    with _session() as session:
        rows = session.scalars(
            select(Application)
            .where(
                Application.status.in_(
                    [s.value for s in WAITING_STATUSES] + [AppStatus.approved.value]
                )
            )
            .order_by(Application.status, Application.id)
        ).all()
        if not rows:
            typer.echo("Nothing is waiting on you.")
            return
        for application in rows:
            job = application.job
            typer.echo(
                f"#{application.id:<4} {application.status:<14} {application.channel:<6} {job.title} - {job.company_name}"
            )
            for blocker in application.blockers or []:
                typer.echo(f"      {blocker['detail']}")
            if application.error:
                typer.echo(f"      {application.error}")


@app.command()
def approve(
    ids: Annotated[
        list[int] | None, typer.Argument(help="Application numbers from `jobportal queue`.")
    ] = None,
    everything: Annotated[
        bool, typer.Option("--all", help="Approve everything waiting for review.")
    ] = False,
) -> None:
    """Clear reviewed applications for sending."""
    from jobportal.apply import service

    if not ids and not everything:
        raise _fail("Give application numbers, or --all.")
    with _session() as session:
        query = select(Application).where(Application.status == AppStatus.needs_review.value)
        if not everything:
            query = query.where(Application.id.in_(ids or []))
        rows = session.scalars(query).all()
        for application in rows:
            service.approve(application)
            typer.echo(
                f"approved #{application.id} {application.job.title} - {application.job.company_name}"
            )
        if not rows:
            typer.echo("Nothing matching is waiting for review.")


def _run(*, do_crawl: bool, do_prepare: bool, do_send: bool) -> None:
    from jobportal.browser import LazyBrowser
    from jobportal.http import PoliteClient
    from jobportal.llm import LLM
    from jobportal.pipeline import (
        RunSummary,
        crawl_and_score,
        make_transport,
        prepare_pending,
        send_approved,
    )

    settings = get_settings()
    settings.ensure_dirs()
    config = _config(settings)
    now = utcnow()
    summary = RunSummary()
    with _session() as session, PoliteClient(settings) as client, LazyBrowser(settings) as browser:
        if do_crawl:
            crawl_and_score(session, config, client, summary, now=now)
        if do_prepare:
            prepare_pending(
                session,
                settings,
                config,
                summary,
                browser=browser,
                client=client,
                llm=LLM(settings),
                now=now,
            )
        if do_send:
            send_approved(
                session, settings, config, summary,
                transport=make_transport(settings), browser=browser, client=client, now=now,
            )  # fmt: skip
    _print(summary.lines())


@app.command()
def prepare() -> None:
    """Tailor resumes and prepare applications for the best new roles. Sends nothing."""
    _run(do_crawl=False, do_prepare=True, do_send=False)


@app.command()
def send() -> None:
    """Send the applications you approved (or the auto policy cleared)."""
    _run(do_crawl=False, do_prepare=False, do_send=True)


@app.command()
def run() -> None:
    """One full pass: crawl, score, prepare, send what is approved."""
    _run(do_crawl=True, do_prepare=True, do_send=True)


@app.command()
def assist(
    application_id: Annotated[
        int | None, typer.Argument(help="Application number; default: the next one that needs you.")
    ] = None,
    minutes: Annotated[int, typer.Option(help="How long to wait for you to submit.")] = 15,
) -> None:
    """Open a form in a visible browser, pre-filled, for you to finish and submit."""
    from jobportal.apply import service
    from jobportal.apply.answers import AnswerBook, load_answers
    from jobportal.apply.forms import filler
    from jobportal.browser import launch_browser

    settings = get_settings()
    config = _config(settings)
    with _session() as session:
        query = select(Application).where(
            Application.channel == "form",
            Application.status.in_(
                [AppStatus.needs_human.value, AppStatus.needs_answers.value, AppStatus.failed.value]
            ),
        )
        if application_id is not None:
            query = query.where(Application.id == application_id)
        application = session.scalars(query.order_by(Application.id)).first()
        if application is None:
            typer.echo("No form application is waiting for you.")
            return
        variant = application.resume_variant
        url = (application.prepared or {}).get("url") or application.job.apply_url
        if not url or variant is None or not variant.pdf_path:
            raise _fail("This application has not been prepared yet. Run: jobportal prepare")
        book = AnswerBook(
            config.profile, load_answers(session, application.user_id), Path(variant.pdf_path)
        )
        typer.echo(f"Opening {application.job.title} - {application.job.company_name}")
        typer.echo("Review the form, complete anything outlined in orange, and submit it yourself.")
        with launch_browser(settings, headless=False) as browser:
            outcome = filler.assist(
                browser, url, book, settings=settings, wait_seconds=minutes * 60,
                screenshot_dir=settings.screenshots_dir, label=f"application-{application.id}",
            )  # fmt: skip
        if outcome.status == "submitted":
            service.mark_submitted(session, config, application, note=outcome.confirmation)
            typer.secho("Recorded as submitted.", fg=typer.colors.GREEN)
        elif typer.confirm("No confirmation page was seen. Did you submit it?", default=False):
            service.mark_submitted(
                session, config, application, note="Submitted by you in the browser."
            )
            typer.secho("Recorded as submitted.", fg=typer.colors.GREEN)
        else:
            typer.echo("Left in the queue.")


# ------------------------------------------------------------------ servers


@app.command()
def worker(
    crawl_minutes: Annotated[int, typer.Option(help="How often to re-read the sources.")] = 60,
) -> None:
    """Run the background worker (crawl, prepare, send) until stopped."""
    from jobportal.worker import Worker

    logging.getLogger().setLevel(logging.INFO)
    init_db()
    instance = Worker(get_settings(), crawl_minutes=crawl_minutes)
    try:
        instance.run_forever()
    except KeyboardInterrupt:
        instance.stop()


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Address to listen on.")] = None,
    port: Annotated[int | None, typer.Option(help="Port to listen on.")] = None,
    with_worker: Annotated[
        bool,
        typer.Option(
            "--worker/--no-worker", help="Also run the background worker in this process."
        ),
    ] = True,
    crawl_minutes: Annotated[
        int, typer.Option(help="How often the worker re-reads the sources.")
    ] = 60,
) -> None:
    """Open the web app (and, by default, the background worker)."""
    import uvicorn

    from jobportal.web.app import create_app
    from jobportal.web.security import require_safe_binding

    settings = get_settings()
    host = host or settings.host
    port = port or settings.port
    try:
        require_safe_binding(host, settings)
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    settings.host = host  # the address being served is an address the app answers to
    init_db()
    logging.getLogger().setLevel(logging.INFO)
    application = create_app(settings, worker_minutes=crawl_minutes if with_worker else None)
    typer.echo(f"Open http://{host}:{port}")
    uvicorn.run(application, host=host, port=port, log_level="warning")


@app.command("since")
def since(hours: Annotated[int, typer.Argument(help="Look back this many hours.")] = 24) -> None:
    """What happened lately: new roles, applications sent, replies."""
    with _session() as session:
        cutoff = utcnow() - timedelta(hours=hours)
        new_jobs = session.scalars(
            select(Job).where(Job.first_seen_at >= cutoff, Job.is_backfill.is_(False))
        ).all()
        sent = session.scalars(select(Application).where(Application.submitted_at >= cutoff)).all()
        typer.echo(
            f"In the last {hours}h: {len(new_jobs)} new postings, {len(sent)} applications sent."
        )
        for application in sent:
            how = "unattended" if application.auto else "by you"
            typer.echo(f"  sent ({how}): {application.job.title} - {application.job.company_name}")
