"""Process settings, read from the environment (or a local ``.env`` file).

These are deployment knobs and secrets. What you are looking for (profile,
lanes, policy, resume) lives in YAML files under the data directory instead;
see :mod:`jobportal.config`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_URL = "https://github.com/sankar276/yuktivajobportal"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBPORTAL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -----------------------------------------------------------
    data_dir: Path = Path("data")
    #: SQLAlchemy URL. Defaults to a SQLite file inside ``data_dir``.
    #: For Postgres: ``postgresql+psycopg://user:pass@host:5432/jobportal``
    database_url: str | None = None

    # --- web ---------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    #: Required as soon as the app listens on anything other than loopback.
    password: SecretStr | None = None
    #: Session-cookie signing key. Generated into ``data_dir`` when unset.
    secret_key: SecretStr | None = None
    #: Extra host names the app may be reached under (reverse proxy, LAN name).
    allowed_hosts: list[str] = Field(default_factory=list)
    #: IANA time zone for dates shown in the app, e.g. ``America/Chicago``.
    timezone: str = "UTC"

    # --- crawler -----------------------------------------------------------
    #: Sent on every request so site operators can see who is asking and why.
    user_agent: str = f"YuktivaJobPortal/0.1 (+{REPO_URL})"
    #: Product token matched against robots.txt ``User-agent`` lines.
    robots_token: str = "YuktivaJobPortal"
    crawl_workers: int = 4
    per_host_delay_seconds: float = 1.0
    http_timeout_seconds: float = 20.0
    http_max_attempts: int = 3

    # --- browser -----------------------------------------------------------
    #: Path to a Chromium/Chrome binary. Unset = the one Playwright installed.
    chromium_path: str | None = None
    headless: bool = True
    #: Let the form filler open localhost / private addresses (tests, demos).
    allow_local_forms: bool = False

    # --- outgoing mail (email apply) ---------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    #: ``starttls`` (587), ``ssl`` (465) or ``none`` (local test servers only).
    smtp_security: str = "starttls"
    #: The From address. Defaults to the profile's email.
    mail_from: str | None = None

    # --- incoming mail (recruiter requirements) ----------------------------
    imap_host: str | None = None
    imap_port: int = 993
    imap_username: str | None = None
    imap_password: SecretStr | None = None
    #: Mailbox/label to read. Point it at a recruiter label, not the whole inbox.
    imap_folder: str = "INBOX"

    # --- optional LLM assistance -------------------------------------------
    #: Read from ``ANTHROPIC_API_KEY`` (or ``JOBPORTAL_ANTHROPIC_API_KEY``).
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("JOBPORTAL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    #: Any Claude API model ID; see https://platform.claude.com/docs/en/about-claude/models/overview
    llm_model: str = "claude-sonnet-5-5"
    #: Let the model reword selected resume bullets to mirror each posting.
    #: Every rewrite must pass the fact guard or the original text is kept.
    llm_rephrase: bool = False

    # ------------------------------------------------------------------ helpers
    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'jobportal.db').as_posix()}"

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_username and self.imap_password)

    @property
    def llm_configured(self) -> bool:
        return self.anthropic_api_key is not None

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.resumes_dir, self.screenshots_dir, self.outbox_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (tests change the environment between cases)."""
    get_settings.cache_clear()
