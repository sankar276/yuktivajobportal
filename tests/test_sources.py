from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from jobportal.http import PoliteClient
from jobportal.sources import ADAPTERS, CrawlContext, SourceRef, detect_source, discover_sources
from jobportal.sources.base import infer_remote, parse_datetime, parse_employment
from jobportal.sources.workday import _posted_on, _title_from_slug
from tests.conftest import FakeWeb, fixture_json, fixture_text

CTX = CrawlContext()


# --------------------------------------------------------------- greenhouse


def test_greenhouse_listing(client: PoliteClient, web: FakeWeb) -> None:
    web.json(
        "GET",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
        fixture_json("greenhouse_jobs.json"),
        etag='"v1"',
    )
    source = SourceRef(id=1, kind="greenhouse", token="acme")
    listing = ADAPTERS["greenhouse"].list_jobs(client, source, CTX)

    assert listing.complete and listing.etag == '"v1"'
    assert [job.external_id for job in listing.jobs] == ["8172508", "8172600", "8172777"]
    job = listing.jobs[0]
    assert job.title == "Principal Platform Engineer"
    assert job.company == "Acme Robotics"
    assert job.location == "Remote - US"
    assert job.remote is True
    assert job.department == "Infrastructure"
    assert job.requisition_id == "REQ-1042"
    # entity-escaped markup is unescaped
    assert job.description_html.startswith("<h2><strong>About the role")
    assert job.posted_at == datetime(2026, 9, 30, 13, 32, 53, tzinfo=UTC)
    assert job.url == "https://job-boards.greenhouse.io/acme/jobs/8172508"
    assert job.apply_url == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=8172508"
    assert "content" not in job.raw
    assert listing.jobs[1].remote is None  # "Dublin" says nothing either way


def test_greenhouse_sends_conditional_headers_and_handles_304(
    client: PoliteClient, web: FakeWeb
) -> None:
    web.add(
        "GET",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
        httpx.Response(304),
    )
    source = SourceRef(id=1, kind="greenhouse", token="acme", etag='"v1"')
    listing = ADAPTERS["greenhouse"].list_jobs(client, source, CTX)
    assert listing.not_modified and listing.etag == '"v1"'
    assert web.calls("/v1/boards")[0].headers["if-none-match"] == '"v1"'


# -------------------------------------------------------------------- lever


def test_lever_listing(client: PoliteClient, web: FakeWeb) -> None:
    web.json(
        "GET",
        "https://api.lever.co/v0/postings/globex?mode=json",
        fixture_json("lever_postings.json"),
    )
    source = SourceRef(id=2, kind="lever", token="globex", company_name="Globex")
    jobs = ADAPTERS["lever"].list_jobs(client, source, CTX).jobs

    architect, contract = jobs
    assert architect.title == "Platform Architect"
    assert architect.company == "Globex"
    assert architect.location == "Remote; Austin, TX"
    assert architect.remote is True
    assert architect.employment_type == "full_time"
    assert architect.department == "Engineering / Platform"
    assert (architect.comp_min, architect.comp_max) == (190000.0, 240000.0)
    assert (architect.comp_currency, architect.comp_period) == ("USD", "year")
    assert architect.apply_url.endswith("/apply")
    assert architect.posted_at == datetime(2026, 9, 30, 12, 10, 41, 800000, tzinfo=UTC)
    # the "lists" sections are folded into the description
    assert "<h3>What you will do</h3>" in architect.description_html
    assert "Zero trust networking" in architect.description_html
    assert "equal opportunity" in architect.description_html

    assert contract.employment_type == "contract"
    assert contract.remote is False  # hybrid
    assert contract.comp_min is None


def test_lever_eu_region_uses_eu_api(client: PoliteClient, web: FakeWeb) -> None:
    web.json("GET", "https://api.eu.lever.co/v0/postings/globex?mode=json", [])
    source = SourceRef(id=2, kind="lever", token="globex", config={"region": "eu"})
    assert ADAPTERS["lever"].list_jobs(client, source, CTX).jobs == []
    assert ADAPTERS["lever"].board_url(source) == "https://jobs.eu.lever.co/globex"


# -------------------------------------------------------------------- ashby


def test_ashby_listing(client: PoliteClient, web: FakeWeb) -> None:
    web.json(
        "GET",
        "https://api.ashbyhq.com/posting-api/job-board/initech?includeCompensation=true",
        fixture_json("ashby_board.json"),
    )
    source = SourceRef(id=3, kind="ashby", token="initech", company_name="Initech")
    jobs = ADAPTERS["ashby"].list_jobs(client, source, CTX).jobs

    assert [job.title for job in jobs] == [
        "Engineering Manager - EU",
        "Staff Platform Engineer",
    ]  # unlisted dropped
    manager, staff = jobs
    assert manager.location == "Remote - European Union; Spain; Germany"
    assert manager.remote is True
    assert manager.employment_type == "full_time"
    assert (manager.comp_min, manager.comp_max, manager.comp_currency) == (
        110000.0,
        185000.0,
        "EUR",
    )
    assert manager.comp_period == "year"
    assert staff.posted_at == datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    assert staff.apply_url.endswith("/application")
    assert "descriptionHtml" not in staff.raw


# ------------------------------------------------------------------ workday

WD = SourceRef(
    id=4,
    kind="workday",
    token="example.wd5.myworkdayjobs.com/example/ExampleExternalCareerSite",
    company_name="Example Corp",
)
WD_API = "https://example.wd5.myworkdayjobs.com/wday/cxs/example/ExampleExternalCareerSite"


def test_workday_queries_with_search_terms(client: PoliteClient, web: FakeWeb) -> None:
    web.json("POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json"))
    listing = ADAPTERS["workday"].list_jobs(client, WD, CrawlContext(search_terms=("architect",)))

    assert listing.complete is False
    architect, marketing = listing.jobs
    assert architect.external_id == "Senior-Software-Architect---Data-Center-Systems_JR1973150"
    assert architect.requisition_id == "JR1973150"
    assert architect.needs_detail is True
    assert architect.location == ""  # "6 Locations" is not a place
    assert marketing.location == "US, CA, Santa Clara"
    assert architect.url == (
        "https://example.wd5.myworkdayjobs.com/ExampleExternalCareerSite"
        "/job/US-CA-Santa-Clara/Senior-Software-Architect---Data-Center-Systems_JR1973150"
    )
    body = web.calls("/jobs")[0].read()
    assert b'"searchText":"architect"' in body.replace(b" ", b"")


def test_workday_detail(client: PoliteClient, web: FakeWeb) -> None:
    web.json("POST", f"{WD_API}/jobs", fixture_json("workday_jobs.json"))
    path = "/job/US-CA-Santa-Clara/Senior-Software-Architect---Data-Center-Systems_JR1973150"
    web.json("GET", f"{WD_API}{path}", fixture_json("workday_detail.json"))
    adapter = ADAPTERS["workday"]
    stub = adapter.list_jobs(client, WD, CTX).jobs[0]
    job = adapter.fetch_detail(client, WD, stub)

    assert job.needs_detail is False
    assert "Kubernetes platform" in job.description_html
    assert job.location.startswith("US, CA, Santa Clara; US, TX, Austin; US, TX, Remote")
    assert job.remote is True
    assert job.employment_type == "full_time"
    assert job.posted_at == datetime(2026, 9, 29, tzinfo=UTC)
    assert job.company == "Example Corp"  # not the legal-entity code in hiringOrganization


def test_workday_falls_back_to_sitemap(client: PoliteClient, web: FakeWeb) -> None:
    web.add("POST", f"{WD_API}/jobs", httpx.Response(400, text="bad request"))
    web.add(
        "GET",
        "https://example.wd5.myworkdayjobs.com/ExampleExternalCareerSite/siteMap.xml",
        httpx.Response(200, text=fixture_text("workday_sitemap.xml")),
    )
    jobs = ADAPTERS["workday"].list_jobs(client, WD, CTX).jobs
    assert [job.external_id for job in jobs] == [
        "Senior-Software-Architect---Data-Center-Systems_JR1973150",
        "Senior-DGX-Cloud-AI-Infrastructure-Software-Engineer_JR2012361",
    ]  # the same ids the listing gives
    assert [job.requisition_id for job in jobs] == ["JR1973150", "JR2012361"]
    assert jobs[0].title == "Senior Software Architect - Data Center Systems"
    assert jobs[0].raw["externalPath"].startswith("/job/US-CA-Santa-Clara/")
    assert all(job.needs_detail for job in jobs)


def test_workday_helpers() -> None:
    now = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    assert _posted_on("Posted Today", now) == datetime(2026, 9, 30, tzinfo=UTC)
    assert _posted_on("Posted Yesterday", now) == datetime(2026, 9, 29, tzinfo=UTC)
    assert _posted_on("Posted 3 Days Ago", now) == datetime(2026, 9, 27, tzinfo=UTC)
    assert _posted_on("Posted 30+ Days Ago", now) == datetime(2026, 8, 31, tzinfo=UTC)
    assert _posted_on(None, now) is None
    assert (
        _title_from_slug("Senior-Engineer--Next-Gen---EDA_JR2014880")
        == "Senior Engineer Next Gen - EDA"
    )


# ---------------------------------------------------------------- detection


@pytest.mark.parametrize(
    ("url", "kind", "token"),
    [
        ("https://boards.greenhouse.io/acme", "greenhouse", "acme"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse", "acme"),
        ("https://boards.greenhouse.io/embed/job_board?for=acme", "greenhouse", "acme"),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "greenhouse", "acme"),
        ("jobs.lever.co/globex", "lever", "globex"),
        ("https://jobs.lever.co/globex/681fbc53/apply", "lever", "globex"),
        ("https://jobs.ashbyhq.com/initech", "ashby", "initech"),
        ("https://jobs.ashbyhq.com/Initech%20Labs/abc", "ashby", "Initech Labs"),
        (
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
            "workday",
            "nvidia.wd5.myworkdayjobs.com/nvidia/NVIDIAExternalCareerSite",
        ),
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US/X_JR1",
            "workday",
            "nvidia.wd5.myworkdayjobs.com/nvidia/NVIDIAExternalCareerSite",
        ),
        (
            "https://wd5.myworkdaysite.com/recruiting/acme/Careers",
            "workday",
            "wd5.myworkdaysite.com/acme/Careers",
        ),
    ],
)
def test_detect_source(url: str, kind: str, token: str) -> None:
    spec = detect_source(url)
    assert spec is not None
    assert (spec.kind, spec.token) == (kind, token)


def test_detect_lever_eu_sets_region() -> None:
    spec = detect_source("https://jobs.eu.lever.co/globex")
    assert spec is not None and spec.config == {"region": "eu"}


@pytest.mark.parametrize(
    "url",
    ["https://example.com/careers", "https://www.linkedin.com/jobs/view/1", "not a url", ""],
)
def test_detect_source_rejects_everything_else(url: str) -> None:
    assert detect_source(url) is None


def test_discover_finds_boards_embedded_in_a_careers_page(
    client: PoliteClient, web: FakeWeb
) -> None:
    page = """
    <html><body>
      <a href="https://jobs.lever.co/globex/123">Open roles</a>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>
      <script>var x = "https:\\/\\/jobs.ashbyhq.com\\/initech";</script>
    </body></html>
    """
    web.add("GET", "https://acme.example/careers", httpx.Response(200, text=page))
    found = {(s.kind, s.token) for s in discover_sources(client, "https://acme.example/careers")}
    assert found == {("lever", "globex"), ("greenhouse", "acme"), ("ashby", "initech")}


def test_discover_short_circuits_on_a_board_url(client: PoliteClient, web: FakeWeb) -> None:
    assert [s.token for s in discover_sources(client, "https://jobs.lever.co/globex")] == ["globex"]
    assert web.requests == []


# ------------------------------------------------------------------ helpers


def test_parse_datetime() -> None:
    assert parse_datetime("2026-09-30T09:32:53-04:00") == datetime(
        2026, 9, 30, 13, 32, 53, tzinfo=UTC
    )
    assert parse_datetime("2026-09-30T08:00:00.000Z") == datetime(2026, 9, 30, 8, tzinfo=UTC)
    assert parse_datetime(1790770241800) == datetime(2026, 9, 30, 12, 10, 41, 800000, tzinfo=UTC)
    assert parse_datetime("2026-09-29") == datetime(2026, 9, 29, tzinfo=UTC)
    assert parse_datetime("soon") is None
    assert parse_datetime(None) is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Full-time", "full_time"),
        ("Full time", "full_time"),
        ("FullTime", "full_time"),
        ("Regular", "full_time"),
        ("Contract", "contract"),
        ("Contractor", "contract"),
        ("Part-time", "part_time"),
        ("Internship", "internship"),
        ("International", None),
        ("Temporary", "temporary"),
        ("", None),
        (None, None),
    ],
)
def test_parse_employment(label: str | None, expected: str | None) -> None:
    assert parse_employment(label) == expected


@pytest.mark.parametrize(
    ("location", "workplace", "expected"),
    [
        ("Remote - US", None, True),
        ("Austin, TX", "remote", True),
        ("Austin, TX", "hybrid", False),
        ("Austin, TX (Hybrid)", None, False),
        ("Austin, TX", None, None),
        ("", None, None),
        ("Work from home", "unspecified", True),
    ],
)
def test_infer_remote(location: str, workplace: str | None, expected: bool | None) -> None:
    assert infer_remote(location, workplace) is expected


# ----------------------------------------------------- reading what boards say


@pytest.mark.parametrize(
    ("location", "declared", "expected"),
    [
        ("", "Fully Remote", True),
        ("", "Remote Eligible", True),
        ("", "Virtual", True),
        ("", "REMOTE", True),
        ("New York", "On Site", False),
        ("New York", "on_site", False),
        ("New York", "Hybrid", False),
        ("Virtual - US", None, True),
        ("Home Based - US", None, True),
        ("US - Home Office", None, True),
        ("Telecommute - US", None, True),
        ("United States - Nationwide", None, True),
        ("Austin, TX (Hybrid)", None, False),
        ("Austin, TX", None, None),
    ],
)
def test_infer_remote_reads_declared_types_and_synonyms(
    location: str, declared: str | None, expected: bool | None
) -> None:
    assert infer_remote(location, declared) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("This is a fully remote role.", True),
        ("We are a remote-first company.", True),
        ("Please note this role is not fully remote; it is on-site in our New York office.", None),
        ("This is not a 100% remote position.", None),
        ("The team isn't fully remote yet.", None),
        ("Kubernetes platform work.", None),
    ],
)
def test_remote_from_description_respects_negation(text: str, expected: bool | None) -> None:
    from jobportal.sources.base import remote_from_description

    assert remote_from_description(text) is expected


def test_ashby_declared_workplace_outranks_the_remote_flag() -> None:
    from jobportal.sources.ashby import AshbyAdapter

    source = SourceRef(id=1, kind="ashby", token="acme", company_name="Acme")
    item = {
        "id": "1", "title": "Platform Architect", "location": "New York",
        "isRemote": True, "workplaceType": "Hybrid",
        "secondaryLocations": ["Boston", {"location": "Austin"}, None],
    }  # fmt: skip
    job = AshbyAdapter()._job(item, source)
    assert job is not None and job.remote is False
    assert job.location == "New York; Boston; Austin"
    remote = AshbyAdapter()._job({**item, "workplaceType": "Remote"}, source)
    assert remote is not None and remote.remote is True


@pytest.mark.parametrize(
    ("title", "contract"),
    [
        ("Cloud Architect (Contract)", True),
        ("Cloud Architect - Contract", True),
        ("Contract Cloud Architect", True),
        ("DevOps Contractor", True),
        ("Cloud Architect - C2C", True),
        ("Principal Engineer - Smart Contract Platform", False),
        ("Principal Engineer - Contract Lifecycle Team", False),
        ("Contracts Manager", False),
        ("Contract Manager", False),
        ("Principal Platform Engineer", False),
    ],
)
def test_contract_titles(title: str, contract: bool) -> None:
    from jobportal.crawl import is_contract_title

    assert is_contract_title(title) is contract


@pytest.mark.parametrize(
    ("text", "contract"),
    [
        ("This is a 6 month contract with possible extension.", True),
        ("Contract-to-hire opportunity.", True),
        ("Contract duration: 12 months.", True),
        ("We are unable to work with C2C or third-party agencies.", False),
        ("No C2C.", False),
        ("This is not a contract position.", False),
        ("You will negotiate vendor terms and contract length with cloud providers.", False),
        ("Full-time role with benefits.", False),
    ],
)
def test_contract_descriptions(text: str, contract: bool) -> None:
    from jobportal.crawl import is_contract_text

    assert is_contract_text(text) is contract


def test_workday_detail_with_structured_locations(client: PoliteClient, web: FakeWeb) -> None:
    from jobportal.sources.base import RawJob
    from jobportal.sources.workday import WorkdayAdapter

    source = WD
    path = "/job/US/Architect_R-1"
    web.json(
        "GET",
        f"{WD_API}{path}",
        {
            "jobPostingInfo": {
                "title": "Architect", "jobDescription": "<p>Kubernetes</p>",
                "location": "Austin, TX", "additionalLocations": [{"descriptor": "Dallas, TX"}, "Remote"],
                "remoteType": "Remote Eligible", "externalUrl": "javascript:alert(1)",
            }
        },
    )  # fmt: skip
    stub = RawJob(
        external_id="Architect_R-1",
        title="Architect",
        url="https://x.example/job",
        needs_detail=True,
        raw={"externalPath": path},
    )
    job = WorkdayAdapter().fetch_detail(client, source, stub)
    assert job.location == "Austin, TX; Dallas, TX; Remote" and job.remote is True
    assert job.url == "https://x.example/job"  # a script link is not a link
