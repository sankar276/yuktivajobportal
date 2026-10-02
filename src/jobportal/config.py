"""What you are looking for: profile, search lanes, policy and resume.

These are plain YAML files in the data directory (``data/`` by default, which
is git-ignored), validated on load so that a typo fails loudly instead of
silently changing what gets sent under your name.

    data/profile.yaml   who you are and your standard application answers
    data/search.yaml    lanes (what to look for), blocklist, apply policy
    data/resume.yaml    the evidence bank the tailored resumes are cut from

``jobportal init`` copies the examples from ``config/`` to get you started.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(Exception):
    """A user config file is missing or invalid. The message is safe to show."""


class StrictModel(BaseModel):
    # Unknown keys are almost always typos ("min_scroe"); refuse them.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


# --------------------------------------------------------------------- enums


class Seniority(IntEnum):
    junior = 1
    mid = 2
    senior = 3
    staff = 4  # staff / lead
    principal = 5  # principal, architect-level IC
    director = 6  # director, head of, senior manager
    vp = 7
    executive = 8  # C-level, SVP


class Employment(StrEnum):
    full_time = "full_time"
    contract = "contract"
    part_time = "part_time"
    internship = "internship"
    temporary = "temporary"


class Channel(StrEnum):
    email = "email"
    form = "form"
    manual = "manual"


# ------------------------------------------------------------------- profile


class Location(StrictModel):
    city: str = ""
    region: str = ""
    country: str = ""
    postal_code: str = ""

    def display(self) -> str:
        return ", ".join(part for part in (self.city, self.region) if part) or self.country


class WorkAuthorization(StrictModel):
    """Your answers to the two questions nearly every US application asks.

    Left as ``null`` they are never guessed: an application that requires them
    waits in the queue until you answer.
    """

    country: str = "United States"
    #: "Are you legally authorized to work in <country>?"
    authorized: bool | None = None
    #: "Will you now or in the future require sponsorship?"
    needs_sponsorship: bool | None = None


class ContractTerms(StrictModel):
    """Logistics quoted to vendors in contract emails. Sent verbatim."""

    #: What you accept, e.g. ["c2c", "w2"].
    engagements: list[str] = Field(default_factory=list)
    availability: str = ""
    #: Leave empty to keep rate out of the first email.
    rate: str = ""
    #: Your own company name for corp-to-corp, if any.
    entity: str = ""


class Eeo(StrictModel):
    """Voluntary self-identification. ``decline`` picks the form's opt-out option."""

    gender: str = "decline"
    race: str = "decline"
    veteran: str = "decline"
    disability: str = "decline"


class StandardAnswer(StrictModel):
    #: Phrase to look for in a question, case-insensitive ("salary expectation").
    match: str
    answer: str

    @field_validator("match")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if len(value) < 3:
            raise ValueError("match must be at least 3 characters")
        return value


_NO_CLEARANCE = {"none", "no", "n/a", "na", "nil", "not applicable", "-", "false"}


class Profile(StrictModel):
    name: str
    preferred_name: str = ""
    email: str
    phone: str = ""
    location: Location = Field(default_factory=Location)
    #: linkedin, github, website, portfolio ...
    links: dict[str, str] = Field(default_factory=dict)
    current_company: str = ""
    current_title: str = ""
    years_experience: int | None = None
    #: A clearance you hold ("Secret", "TS/SCI"). Empty = none, and postings
    #: that require an active clearance, or one above yours, are skipped.
    security_clearance: str = ""
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    contract: ContractTerms = Field(default_factory=ContractTerms)
    eeo: Eeo = Field(default_factory=Eeo)
    answers: list[StandardAnswer] = Field(default_factory=list)
    #: Email signature. Built from name, phone and LinkedIn when empty.
    signature: str = ""

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        if not EMAIL_RE.match(value):
            raise ValueError(f"not an email address: {value!r}")
        return value

    @field_validator("security_clearance", mode="before")
    @classmethod
    def _no_clearance(cls, value: Any) -> Any:
        # "none", "n/a" and YAML's own `no` / `null` all mean you hold none.
        if value is None or value is False:
            return ""
        if isinstance(value, str) and value.strip().lower() in _NO_CLEARANCE:
            return ""
        return value

    @property
    def first_name(self) -> str:
        return (self.preferred_name or self.name).split()[0]

    @property
    def last_name(self) -> str:
        parts = self.name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    def email_signature(self) -> str:
        if self.signature:
            return self.signature
        lines = [self.name]
        if self.phone:
            lines.append(self.phone)
        if self.links.get("linkedin"):
            lines.append(self.links["linkedin"])
        return "\n".join(lines)


# -------------------------------------------------------------------- search


class TitleRules(StrictModel):
    """Phrases matched against the job title. Every word of a phrase must appear."""

    target: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    #: Skip jobs whose title matches neither list (the cheap, strong filter).
    required: bool = True


class SkillRules(StrictModel):
    core: list[str] = Field(default_factory=list)
    bonus: list[str] = Field(default_factory=list)
    #: At least one of these must appear in the posting, or it is skipped.
    must_have_any: list[str] = Field(default_factory=list)
    #: Core skills found in the posting for a full skills score.
    full_marks_at: int = Field(default=4, ge=1)


class SeniorityRange(StrictModel):
    min: Seniority | None = None
    max: Seniority | None = None

    @field_validator("min", "max", mode="before")
    @classmethod
    def _by_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return Seniority[value.strip().lower()]
            except KeyError:
                names = ", ".join(level.name for level in Seniority)
                raise ValueError(f"unknown seniority {value!r}; use one of: {names}") from None
        return value

    @model_validator(mode="after")
    def _ordered(self) -> SeniorityRange:
        if self.min and self.max and self.min > self.max:
            raise ValueError("seniority.min is above seniority.max")
        return self


class LocationRules(StrictModel):
    #: Accept remote roles.
    remote: bool = True
    #: A remote role naming a region must name one of these ("Remote - EU" is skipped).
    remote_regions: list[str] = Field(
        default_factory=lambda: ["United States", "US", "USA", "North America", "Americas"]
    )
    #: Places you would work on-site or hybrid, e.g. ["Dallas", "Plano", "TX"].
    onsite: list[str] = Field(default_factory=list)


class Compensation(StrictModel):
    #: Skip postings whose advertised annual maximum is below this.
    min_base: float | None = None
    #: Skip contract postings whose advertised hourly maximum is below this.
    min_hourly: float | None = None
    #: The currency your floors are in. Pay advertised in another is not compared.
    currency: str = "USD"

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        code = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError(f"currency must be a three-letter code such as USD, not {value!r}")
        return code


class Weights(StrictModel):
    title: float = 30
    skills: float = 35
    seniority: float = 10
    location: float = 15
    freshness: float = 10

    @model_validator(mode="after")
    def _positive(self) -> Weights:
        values = (self.title, self.skills, self.seniority, self.location, self.freshness)
        if any(v < 0 for v in values) or sum(values) <= 0:
            raise ValueError("weights must be non-negative and not all zero")
        return self

    def total(self) -> float:
        return self.title + self.skills + self.seniority + self.location + self.freshness


class Lane(StrictModel):
    """One kind of role you would take, scored on its own terms."""

    key: str
    name: str
    #: Which resume variant (``variants`` in resume.yaml) to tailor from.
    resume: str = "default"
    #: Empty = any employment type.
    employment: list[Employment] = Field(default_factory=list)
    titles: TitleRules = Field(default_factory=TitleRules)
    skills: SkillRules = Field(default_factory=SkillRules)
    seniority: SeniorityRange = Field(default_factory=SeniorityRange)
    locations: LocationRules = Field(default_factory=LocationRules)
    compensation: Compensation = Field(default_factory=Compensation)
    skip_if_description_has: list[str] = Field(default_factory=list)
    #: Skip postings that state more travel than this.
    max_travel_percent: int | None = Field(default=None, ge=0, le=100)
    weights: Weights = Field(default_factory=Weights)
    #: Score (0-100) from which a job lands on the shortlist.
    shortlist_at: float = Field(default=60, ge=0, le=100)

    @field_validator("key")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
            raise ValueError("lane key must be lowercase letters, digits, - or _")
        return value


class AutoPolicy(StrictModel):
    #: Only roles scoring at least this are sent without asking.
    min_score: float = Field(default=80, ge=0, le=100)
    #: Hard ceiling on unattended submissions per day, all channels together.
    daily_cap: int = Field(default=10, ge=0)
    per_company_per_week: int = Field(default=2, ge=1)
    channels: list[Channel] = Field(default_factory=lambda: [Channel.email, Channel.form])


class EmailPolicy(StrictModel):
    daily_cap: int = Field(default=30, ge=0)
    min_seconds_between_sends: int = Field(default=60, ge=0)
    #: Blind-copy yourself so every sent application is in your own mailbox too.
    bcc_self: bool = True
    #: Also attach the Word version (many staffing vendors ask for one).
    attach_docx: bool = False


class Policy(StrictModel):
    #: ``review``: everything waits for one click. ``auto``: roles that clear the
    #: auto rules are sent unattended; the rest still wait for you.
    mode: Literal["review", "auto"] = "review"
    auto: AutoPolicy = Field(default_factory=AutoPolicy)
    email: EmailPolicy = Field(default_factory=EmailPolicy)
    follow_up_days: int = Field(default=7, ge=1)
    #: How long a client submission blocks a second one through another vendor.
    ledger_window_days: int = Field(default=180, ge=1)
    #: What counts as "fresh" in the feed.
    fresh_hours: int = Field(default=24, ge=1)
    #: Nothing older than this is sent unattended. You can still apply by hand.
    max_job_age_days: int = Field(default=30, ge=1)
    #: How many shortlisted roles to prepare (resume + application) per run.
    prepare_per_run: int = Field(default=15, ge=0)


class SearchConfig(StrictModel):
    lanes: list[Lane]
    #: Never apply here (your current employer, past employers, clients ...).
    blocked_companies: list[str] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode="after")
    def _unique_lanes(self) -> SearchConfig:
        if not self.lanes:
            raise ValueError("define at least one lane")
        keys = [lane.key for lane in self.lanes]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise ValueError(f"duplicate lane keys: {', '.join(sorted(dupes))}")
        return self

    def lane(self, key: str | None) -> Lane | None:
        return next((lane for lane in self.lanes if lane.key == key), None)


# ------------------------------------------------------------------- loading

PROFILE_FILE = "profile.yaml"
SEARCH_FILE = "search.yaml"
RESUME_FILE = "resume.yaml"


def _format_validation_error(path: Path, error: ValidationError) -> str:
    lines = [f"{path} is not valid:"]
    for issue in error.errors():
        where = ".".join(str(part) for part in issue["loc"]) or "(top level)"
        lines.append(f"  - {where}: {issue['msg']}")
    return "\n".join(lines)


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML keeps the last of two equal keys without a word. Here that is an error.

    A second ``lanes:`` further down would otherwise replace the first, and
    a second ``exclude:`` would quietly undo the one above it.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"the key {key!r} appears twice", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"{path} not found. Run `jobportal init` to create it from the example.")
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level.")
    return data


ModelT = TypeVar("ModelT", bound=BaseModel)


def load_model(path: Path, model: type[ModelT]) -> ModelT:
    data = load_yaml(path)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_profile(data_dir: Path) -> Profile:
    return load_model(data_dir / PROFILE_FILE, Profile)


def load_search(data_dir: Path) -> SearchConfig:
    return load_model(data_dir / SEARCH_FILE, SearchConfig)


class UserConfig(BaseModel):
    """Everything the pipeline needs to act for one person."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    profile: Profile
    search: SearchConfig
    resume: Any  # jobportal.resume.model.ResumeBank (imported lazily to avoid a cycle)


def load_user_config(data_dir: Path) -> UserConfig:
    from jobportal.resume.model import load_resume_bank

    profile = load_profile(data_dir)
    search = load_search(data_dir)
    resume = load_resume_bank(data_dir / RESUME_FILE)
    missing = sorted({lane.resume for lane in search.lanes} - set(resume.variants))
    if missing:
        raise ConfigError(
            f"{data_dir / SEARCH_FILE}: lanes reference resume variants that "
            f"{data_dir / RESUME_FILE} does not define: {', '.join(missing)}"
        )
    return UserConfig(profile=profile, search=search, resume=resume)
