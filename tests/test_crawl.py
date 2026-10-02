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
    architect, marketing = (
        jobs["Senior-Software-Architect---Data-Center-Systems_JR1973150"],
        jobs["Marketing-Coordinator_JR2000001"],
    )
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
    assert (
        result.closed == 0 and jobs["Marketing-Coordinator_JR2000001"].closed_at is None
    )  # not seen != closed, yet
    assert (
        "Kubernetes platform"
        in jobs["Senior-Software-Architect---Data-Center-Systems_JR1973150"].description_text
    )  # detail kept
    assert jobs["Senior-Software-Architect---Data-Center-Systems_JR1973150"].needs_detail is False

    (result,) = crawl(session, client, now=NOW + timedelta(days=9), title_filter=lambda t: False)
    assert (
        result.closed == 1
        and _jobs(session)["Marketing-Coordinator_JR2000001"].closed_at is not None
    )


def test_a_posting_that_slips_out_of_the_search_results_is_asked_about_not_closed(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    """A search shows its first results only: missing from them is not the same as gone."""
    name = "Senior-Software-Architect---Data-Center-Systems_JR1973150"
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    web.json("POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json"))
    web.json("GET", f"{WD_API}{WD_PATH}", fixture_json("workday_detail.json"))
    wanted = {"title_filter": lambda title: "architect" in title.lower()}
    crawl(session, client, now=NOW, **wanted)
    assert _jobs(session)[name].needs_detail is False

    # The search stops returning it, but its own page still answers.
    web.json("POST", f"{WD_API}/jobs", {"total": 0, "jobPostings": []})
    later = NOW + timedelta(days=8)
    (result,) = crawl(session, client, now=later, **wanted)
    job = _jobs(session)[name]
    assert job.closed_at is None and job.last_seen_at == later
    assert result.closed == 1  # only the stub nobody read
    assert len(web.calls("/job/US-CA-Santa-Clara/")) == 2  # read once, asked about once

    # No answer either way: kept, and asked again later rather than every pass.
    web.add("GET", f"{WD_API}{WD_PATH}", httpx.Response(503))
    much_later = later + timedelta(days=8)
    crawl(session, client, now=much_later, **wanted)
    job = _jobs(session)[name]
    assert job.closed_at is None and job.detail_retry_at is not None
    asked = len(web.calls("/job/US-CA-Santa-Clara/"))
    crawl(session, client, now=much_later + timedelta(minutes=30), **wanted)
    assert len(web.calls("/job/US-CA-Santa-Clara/")) == asked

    # Its page is gone: now it is closed.
    web.json("GET", f"{WD_API}{WD_PATH}", {"error": "gone"}, status=404)
    (result,) = crawl(session, client, now=much_later + timedelta(days=2), **wanted)
    assert result.closed == 1 and _jobs(session)[name].closed_at is not None


def test_a_posting_missing_for_weeks_with_no_answer_is_closed(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    name = "Senior-Software-Architect---Data-Center-Systems_JR1973150"
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    web.json("POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json"))
    web.json("GET", f"{WD_API}{WD_PATH}", fixture_json("workday_detail.json"))
    wanted = {"title_filter": lambda title: "architect" in title.lower()}
    crawl(session, client, now=NOW, **wanted)
    web.json("POST", f"{WD_API}/jobs", {"total": 0, "jobPostings": []})
    web.add("GET", f"{WD_API}{WD_PATH}", httpx.Response(503))
    crawl(session, client, now=NOW + timedelta(days=10), **wanted)
    assert _jobs(session)[name].closed_at is None
    crawl(session, client, now=NOW + timedelta(days=22), **wanted)
    assert _jobs(session)[name].closed_at is not None


def test_detail_404_closes_the_job(session: Session, client: PoliteClient, web: FakeWeb) -> None:
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    web.json("POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json"))
    crawl(session, client, now=NOW, title_filter=lambda t: "architect" in t.lower())
    assert (
        _jobs(session)["Senior-Software-Architect---Data-Center-Systems_JR1973150"].closed_at == NOW
    )


def test_add_source_is_idempotent_and_special_sources_are_singletons(session: Session) -> None:
    first, created = add_source(session, SourceSpec(kind="lever", token="globex"))
    again, created_again = add_source(session, SourceSpec(kind="lever", token="globex"), "Globex")
    assert created and not created_again and first.id == again.id
    assert again.company_name == "Globex"  # filled in, not overwritten
    inbox = special_source(session, "email")
    assert special_source(session, "email").id == inbox.id
    assert inbox.initialized is True


# ------------------------------------------------- robustness (review findings)

ARCHITECT = "Senior-Software-Architect---Data-Center-Systems_JR1973150"


def test_a_board_whose_data_cannot_be_stored_does_not_stop_the_others(
    session: Session, client: PoliteClient, web: FakeWeb, monkeypatch
) -> None:
    from jobportal import crawl as crawl_module

    good = _greenhouse(session)
    bad, _ = add_source(session, SourceSpec(kind="lever", token="globex"))
    session.commit()
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))
    web.json(
        "GET",
        "https://api.lever.co/v0/postings/globex?mode=json",
        fixture_json("lever_postings.json"),
    )
    real = crawl_module._apply_listing

    def explode(session, source, listing, result, now):
        if source.kind == "lever":
            real(session, source, listing, result, now)  # half done, then it breaks
            raise RuntimeError("database said no")
        real(session, source, listing, result, now)

    monkeypatch.setattr(crawl_module, "_apply_listing", explode)
    results = {r.label: r for r in crawl(session, client, now=NOW)}

    assert results["acme"].status == "ok" and results["acme"].new == 3
    assert results["globex"].status == "error" and "database said no" in results["globex"].error
    assert results["globex"].new == 0
    session.expire_all()
    stored = {job.source_id for job in session.scalars(select(Job))}
    assert stored == {good.id}  # the failed board's half-applied listing was rolled back
    assert session.get(Source, bad.id).last_status == "error"
    assert session.get(Source, bad.id).last_crawled_at == NOW  # and it is not retried at once


def test_one_unreadable_posting_costs_only_itself(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    _greenhouse(session)
    listing = fixture_json("greenhouse_jobs.json")
    listing["jobs"][1]["location"] = ["not", "an", "object"]  # the adapter copes
    listing["jobs"].append({"id": 77, "title": "Broken", "departments": "nonsense"})
    listing["jobs"].append("not even an object")
    web.json("GET", GH_URL, listing)
    (result,) = crawl(session, client, now=NOW)
    assert result.status == "ok" and result.new >= 3


def test_text_a_database_cannot_hold_is_cleaned_not_fatal(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    _greenhouse(session)
    listing = fixture_json("greenhouse_jobs.json")
    listing["jobs"][0]["title"] = "Principal\x00 Platform Engineer"
    listing["jobs"][0]["content"] = "<p>Kubernetes\x00 platform</p>" + "<p>filler</p>" * 100_000
    listing["jobs"][0]["metadata"] = [{"name": "nul\x00inside", "value": "x\x00y"}]
    web.json("GET", GH_URL, listing, etag='"' + "e" * 400 + '"')
    (result,) = crawl(session, client, now=NOW)
    assert result.status == "ok"
    job = _jobs(session)["8172508"]
    assert job.title == "Principal Platform Engineer" and "\x00" not in job.description_html
    assert len(job.description_html) <= 200_000  # oversized markup is cut before parsing
    assert "\x00" not in str(job.raw)
    assert len(session.scalar(select(Source)).etag) == 255


def test_links_that_are_not_web_addresses_are_never_stored(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    _greenhouse(session)
    listing = fixture_json("greenhouse_jobs.json")
    listing["jobs"][0]["absolute_url"] = "javascript:alert(document.cookie)"
    web.json("GET", GH_URL, listing)
    crawl(session, client, now=NOW)
    job = _jobs(session)["8172508"]
    assert job.url == "https://job-boards.greenhouse.io/acme/jobs/8172508"  # the board's own page
    assert not any((j.url or "").startswith("javascript") for j in _jobs(session).values())


def test_an_empty_listing_closes_nothing_until_it_is_seen_twice(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    source = _greenhouse(session)
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))
    crawl(session, client, now=NOW)

    web.json("GET", GH_URL, {"jobs": []})
    (result,) = crawl(session, client, now=NOW + timedelta(hours=1))
    assert result.status == "empty" and result.closed == 0
    assert all(job.closed_at is None for job in _jobs(session).values())
    assert "kept" in session.get(Source, source.id).last_error

    (result,) = crawl(session, client, now=NOW + timedelta(hours=2))
    assert result.status == "ok" and result.closed == 3  # twice in a row: believed

    # A listing with postings in between starts the count again.
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))
    crawl(session, client, now=NOW + timedelta(hours=3))
    web.json("GET", GH_URL, {"jobs": []})
    (result,) = crawl(session, client, now=NOW + timedelta(hours=4))
    assert result.status == "empty" and result.closed == 0


def test_an_answer_without_a_list_of_postings_is_an_error_not_an_empty_board(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    _greenhouse(session)
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))
    crawl(session, client, now=NOW)
    web.json("GET", GH_URL, {"jobs": None})
    (result,) = crawl(session, client, now=NOW + timedelta(hours=1))
    assert result.status == "error" and "list of postings" in result.error
    assert all(job.closed_at is None for job in _jobs(session).values())


def test_a_reopened_and_changed_job_counts_once(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    _greenhouse(session)
    listing = fixture_json("greenhouse_jobs.json")
    web.json("GET", GH_URL, listing)
    crawl(session, client, now=NOW)
    for job in _jobs(session).values():
        job.closed_at = NOW
    session.commit()
    changed = copy.deepcopy(listing)
    for item in changed["jobs"]:
        item["title"] += " II"
    web.json("GET", GH_URL, changed)
    (result,) = crawl(session, client, now=NOW + timedelta(hours=1))
    assert result.updated == 3


def test_failed_detail_requests_are_capped_and_backed_off(
    session: Session, client: PoliteClient, web: FakeWeb, monkeypatch
) -> None:
    from jobportal import crawl as crawl_module

    monkeypatch.setattr(crawl_module, "MAX_DETAILS_PER_SOURCE", 5)
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    postings = [
        {
            "title": f"Architect {n}",
            "externalPath": f"/job/US/Architect-{n}_JR{n}",
            "postedOn": "Posted Today",
        }
        for n in range(12)
    ]
    web.json("POST", f"{WD_API}/jobs", {"total": 12, "jobPostings": postings})
    for posting in postings:
        web.add("GET", f"{WD_API}{posting['externalPath']}", httpx.Response(403))

    crawl(session, client, now=NOW)
    assert len(web.calls("/job/US/")) == 5  # attempts are counted, not successes
    crawl(session, client, now=NOW + timedelta(minutes=30))
    assert len(web.calls("/job/US/")) == 10  # the other postings get their turn
    failed = [job for job in _jobs(session).values() if job.detail_failures]
    assert len(failed) == 10 and all(job.detail_retry_at > NOW for job in failed)
    crawl(session, client, now=NOW + timedelta(minutes=45))
    assert len(web.calls("/job/US/")) == 12  # the last two; nothing is asked twice so soon
    crawl(session, client, now=NOW + timedelta(minutes=50))
    assert len(web.calls("/job/US/")) == 12


def test_a_posting_whose_page_is_gone_is_not_reopened_every_pass(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    web.json(
        "POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json")
    )  # the detail page is a 404
    keep = lambda title: "architect" in title.lower()  # noqa: E731
    crawl(session, client, now=NOW, title_filter=keep)
    assert _jobs(session)[ARCHITECT].closed_at == NOW

    (result,) = crawl(session, client, now=NOW + timedelta(hours=1), title_filter=keep)
    assert _jobs(session)[ARCHITECT].closed_at == NOW  # still listed, still closed
    assert result.updated == 0 and len(web.calls(WD_PATH)) == 1  # and not asked for again

    crawl(session, client, now=NOW + timedelta(days=8), title_filter=keep)
    assert len(web.calls(WD_PATH)) == 2  # one more look after a week


def test_a_failing_workday_query_reports_its_own_error(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    web.add("POST", f"{WD_API}/jobs", httpx.Response(503))  # and there is no sitemap either
    (result,) = crawl(session, client, now=NOW)
    assert result.status == "error" and "503" in result.error


def test_workday_postings_sharing_a_requisition_stay_separate(
    session: Session, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="workday", token=WD_TOKEN), "Example Corp")
    session.commit()
    postings = [
        {
            "title": "Architect, Austin",
            "externalPath": "/job/Austin/Architect_R-10234",
            "bulletFields": ["R-10234"],
        },
        {
            "title": "Architect, Dallas",
            "externalPath": "/job/Dallas/Architect_R-10234-1",
            "bulletFields": ["Full time", "R-10234"],
        },
        {
            "title": "Engineer",
            "externalPath": "/job/Dallas/Engineer_R-2",
            "bulletFields": ["Full time"],
        },
    ]
    web.json("POST", f"{WD_API}/jobs", {"total": 3, "jobPostings": postings})
    (result,) = crawl(session, client, now=NOW, title_filter=lambda _t: False)
    jobs = _jobs(session)
    assert result.new == 3 and set(jobs) == {
        "Architect_R-10234",
        "Architect_R-10234-1",
        "Engineer_R-2",
    }
    assert jobs["Architect_R-10234-1"].requisition_id == "R-10234"  # not "Full time"
    assert jobs["Engineer_R-2"].requisition_id == "R-2"
