"""One place that starts Chromium (for PDF rendering and for application forms).

The browser is launched exactly as Playwright ships it. No stealth plugins, no
fingerprint spoofing, no automation-flag removal: if a site checks for
automation it is entitled to see it, and the form flow hands such pages to you.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import Browser, Error, sync_playwright

from jobportal.settings import Settings, get_settings


class BrowserUnavailable(RuntimeError):
    """Chromium could not be started. The message says how to install it."""


@contextmanager
def launch_browser(
    settings: Settings | None = None, *, headless: bool | None = None
) -> Iterator[Browser]:
    settings = settings or get_settings()
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=settings.headless if headless is None else headless,
                executable_path=settings.chromium_path or None,
            )
        except Error as exc:
            raise BrowserUnavailable(
                "Chromium could not be started. Install it with `playwright install chromium` "
                f"or set JOBPORTAL_CHROMIUM_PATH. ({exc.message.splitlines()[0]})"
            ) from exc
        try:
            yield browser
        finally:
            browser.close()
