from __future__ import annotations

import copy
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.crawl import add_source, crawl, special_source
from jobportal.http import PoliteClient
from jobportal.models import Job, Source
from jobportal.sources import CrawlContext, SourceSpec
from tests.conftest import NOW, FakeWeb, fixture_json

GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
WD_TOKEN = "example.wd5.myworkdayjobs.com/example/ExampleExternalCareerSite"
WD_API = "https://example.wd5.myworkdayjobs.com/wday/cxs/example/ExampleExternalCareerSite"
WD_PATH = "/job/US-CA-Santa-Clara/Senior-Software-Architect---Data-Center-Systems_JR1973150"


def _greenhouse(session: Session) -> Source:
    source, created = add_source(session, SourceSpec(kind="greenhouse", token="acme"))
    assert created
    session.commit()
    return source


def _jobs(session: Session) -> dict[str, Job]:
    session.expire_all()
    return {job.external_id: job for job in session.scalars(select(Job))}


def test_first_crawl_creates_jobs_as_backlog(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    source = _greenhouse(session)
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"), etag='"v1"')

    (result,) = crawl(session, client, now=NOW)

    assert (result.status, result.found, result.new, result.closed) == ("ok", 3, 3, 0)
    jobs = _jobs(session)
    job = jobs["8172508"]
    assert job.company_name == "Acme Robotics" and job.company_key == "acme robotics"
    assert job.remote is True
    assert job.description_text.startswith("About the role")
    assert "- Write operators in Go" in job.description_text
    # pay was pulled from the description because Greenhouse gave none
    assert (job.comp_min, job.comp_max, job.comp_period) == (210000.0, 265000.0, "year")
    assert job.is_backfill is True
    assert job.first_seen_at == NOW
    session.refresh(source)
    assert source.initialized and source.etag == '"v1"' and source.jobs_open == 3
    assert source.last_status == "ok" and source.last_crawled_at == NOW


def test_second_crawl_finds_new_changed_and_closed(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    _greenhouse(session)
    first = fixture_json("greenhouse_jobs.json")
    web.json("GET", GH_URL, first)
    crawl(session, client, now=NOW)

    second = copy.deepcopy(first)
    second["jobs"] = [j for j in second["jobs"] if j["id"] != 8172600]  # sales role closed
    second["jobs"][0]["title"] = "Principal Platform Engineer, Core"  # retitled
    second["jobs"].append(
        {
            "id": 9000001,
            "title": "Staff Platform Engineer",
            "company_name": "Acme Robotics",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/9000001",
            "first_published": "2026-09-30T14:00:00-04:00",
            "updated_at": "2026-09-30T14:00:00-04:00",
            "content": "&lt;p&gt;Kubernetes.&lt;/p&gt;",
        }
    )
    web.json("GET", GH_URL, second)
    later = NOW + timedelta(hours=1)

    (result,) = crawl(session, client, now=later)

    assert (result.found, result.new, result.updated, result.closed) == (3, 1, 1, 1)
    jobs = _jobs(session)
    assert jobs["8172600"].closed_at == later
    assert jobs["8172508"].title == "Principal Platform Engineer, Core"
    assert jobs["8172508"].first_seen_at == NOW and jobs["8172508"].last_seen_at == later
    new = jobs["9000001"]
    assert new.is_backfill is False and new.first_seen_at == later
    assert new.effective_posted_at is not None
    assert jobs["8172777"].closed_at is None


def test_closed_job_reopens_when_it_returns(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    _greenhouse(session)
    full = fixture_json("greenhouse_jobs.json")
    web.json("GET", GH_URL, full)
    crawl(session, client, now=NOW)
    web.json("GET", GH_URL, {"jobs": full["jobs"][:1]})
    crawl(session, client, now=NOW + timedelta(hours=1))
    assert _jobs(session)["8172777"].closed_at is not None
    web.json("GET", GH_URL, full)
    crawl(session, client, now=NOW + timedelta(hours=2))
    assert _jobs(session)["8172777"].closed_at is None


def test_not_modified_only_touches_last_seen(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    source = _greenhouse(session)
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"), etag='"v1"')
    crawl(session, client, now=NOW)
    web.add("GET", GH_URL, httpx.Response(304))
    later = NOW + timedelta(hours=1)

    (result,) = crawl(session, client, now=later)

    assert result.status == "unchanged"
    assert web.calls("/v1/boards")[-1].headers["if-none-match"] == '"v1"'
    assert all(
        job.last_seen_at == later and job.closed_at is None for job in _jobs(session).values()
    )
    session.refresh(source)
    assert source.etag == '"v1"'  # kept for the next conditional request


def test_errors_are_recorded_and_do_not_close_jobs(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    source = _greenhouse(session)
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))
    crawl(session, client, now=NOW)
    web.add("GET", GH_URL, httpx.Response(503))

    (result,) = crawl(session, client, now=NOW + timedelta(hours=1))

    assert result.status == "error" and "503" in result.error
    assert all(job.closed_at is None for job in _jobs(session).values())
    session.refresh(source)
    assert source.last_status == "error" and source.last_ok_at == NOW


def test_missing_board_and_robots_block_are_distinguished(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="greenhouse", token="gone"))
    add_source(session, SourceSpec(kind="lever", token="globex"))
    session.commit()
    web.robots["https://api.lever.co"] = httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    results = {r.label: r.status for r in crawl(session, client, now=NOW)}

    assert results == {"gone": "not_found", "globex": "robots_blocked"}
    assert web.calls("api.lever.co/v0") == []  # never requested


def test_one_bad_source_does_not_stop_the_others(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="ashby", token="initech"), "Initech")
    _greenhouse(session)
    web.add(
        "GET",
        "https://api.ashbyhq.com/posting-api/job-board/initech?includeCompensation=true",
        httpx.Response(200, text="not json"),
    )
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))

    results = {r.label: r for r in crawl(session, client, now=NOW)}

    assert results["Initech"].status == "error"
    assert results["acme"].new == 3


def test_disabled_and_recently_crawled_sources_are_skipped(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    source = _greenhouse(session)
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))
    crawl(session, client, now=NOW)
    assert (
        crawl(session, client, now=NOW + timedelta(minutes=5), min_interval=timedelta(minutes=30))
        == []
    )
    source.enabled = False
    session.commit()
    assert crawl(session, client, now=NOW + timedelta(hours=2)) == []


def test_workday_stubs_are_hydrated_only_when_the_title_matches(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    web.json("POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json"))
    web.json("GET", f"{WD_API}{WD_PATH}", fixture_json("workday_detail.json"))

    (result,) = crawl(
        session,
        client,
        now=NOW,
        context=CrawlContext(search_terms=("architect",)),
        title_filter=lambda title: "architect" in title.lower(),
    )

    assert (result.new, result.hydrated) == (2, 1)
    jobs = _jobs(session)
    architect, marketing = jobs["JR1973150"], jobs["JR2000001"]
    assert architect.needs_detail is False
    assert "Kubernetes platform" in architect.description_text
    assert architect.remote is True and architect.employment_type == "full_time"
    assert architect.posted_at is not None and architect.posted_at.day == 29
    assert marketing.needs_detail is True and marketing.description_text == ""
    assert len(web.calls("/job/US-CA-Santa-Clara/")) == 1  # no detail request for marketing


def test_hydrated_job_survives_relisting_and_partial_listing_closes_slowly(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    listing = fixture_json("workday_jobs.json")
    web.json("POST", f"{WD_API}/jobs", listing)
    web.json("GET", f"{WD_API}{WD_PATH}", fixture_json("workday_detail.json"))
    crawl(session, client, now=NOW, title_filter=lambda t: "architect" in t.lower())

    # The marketing role drops out of the query results.
    web.json("POST", f"{WD_API}/jobs", {"total": 1, "jobPostings": listing["jobPostings"][:1]})
    (result,) = crawl(session, client, now=NOW + timedelta(days=1), title_filter=lambda t: False)
    jobs = _jobs(session)
    assert result.closed == 0 and jobs["JR2000001"].closed_at is None  # not seen != closed, yet
    assert "Kubernetes platform" in jobs["JR1973150"].description_text  # detail kept
    assert jobs["JR1973150"].needs_detail is False

    (result,) = crawl(session, client, now=NOW + timedelta(days=9), title_filter=lambda t: False)
    assert result.closed == 1 and _jobs(session)["JR2000001"].closed_at is not None


def test_detail_404_closes_the_job(session: Session, client: PoliteClient, web: FakeWeb) -> None:
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    web.json("POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json"))
    crawl(session, client, now=NOW, title_filter=lambda t: "architect" in t.lower())
    assert _jobs(session)["JR1973150"].closed_at == NOW


def test_add_source_is_idempotent_and_special_sources_are_singletons(session: Session) -> None:
    first, created = add_source(session, SourceSpec(kind="lever", token="globex"))
    again, created_again = add_source(session, SourceSpec(kind="lever", token="globex"), "Globex")
    assert created and not created_again and first.id == again.id
    assert again.company_name == "Globex"  # filled in, not overwritten
    inbox = special_source(session, "email")
    assert special_source(session, "email").id == inbox.id
    assert inbox.initialized is True
