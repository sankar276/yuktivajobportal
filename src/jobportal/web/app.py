"""The web application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from jobportal import __version__
from jobportal.db import get_session_factory, init_db
from jobportal.settings import Settings, get_settings
from jobportal.web import queries
from jobportal.web.deps import STATIC_DIR, ConfigProblem, back, build_templates, flash
from jobportal.web.routes import admin, applications, feed
from jobportal.web.security import GuardMiddleware, LoginLimiter, session_secret
from jobportal.worker import Worker, start_in_thread

SESSION_MAX_AGE = 14 * 24 * 3600


def create_app(settings: Settings | None = None, *, worker_minutes: int | None = None) -> FastAPI:
    """Build the app. ``worker_minutes`` also runs the background worker in-process."""
    settings = settings or get_settings()
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db()
        if worker_minutes is not None:
            app.state.worker = Worker(settings, crawl_minutes=worker_minutes)
            start_in_thread(app.state.worker)
        try:
            yield
        finally:
            if app.state.worker is not None:
                app.state.worker.stop()

    app = FastAPI(
        title="Yuktiva Job Portal",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.templates = build_templates(settings)
    app.state.worker = None
    app.state.login_limiter = LoginLimiter()

    def nav_counts(request: Request) -> dict[str, int]:
        user_id = getattr(request.state, "user_id", None)
        if user_id is None:
            return {"queue": 0}
        with get_session_factory()() as session:
            return {"queue": queries.waiting_count(session, user_id)}

    app.state.nav_counts = nav_counts

    # Added last = outermost: the session must exist before the guard reads it.
    app.add_middleware(GuardMiddleware, settings=settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(settings),
        session_cookie="jobportal_session",
        same_site="strict",
        max_age=SESSION_MAX_AGE,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(feed.router)
    app.include_router(applications.router)
    app.include_router(admin.router)

    @app.exception_handler(ConfigProblem)
    async def config_problem(request: Request, exc: ConfigProblem) -> Response:
        return app.state.templates.TemplateResponse(
            request,
            "config_error.html",
            {"problem": str(exc), "data_dir": settings.data_dir.resolve()},
            status_code=503,
        )

    @app.exception_handler(StaleDataError)
    async def changed_meanwhile(request: Request, _exc: StaleDataError) -> Response:
        # Two writers met on one application; the later one (this request) loses.
        flash(request, applications.CHANGED_MEANWHILE, "error")
        return back(request, "/queue")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return app.state.templates.TemplateResponse(
                request, "not_found.html", {"detail": exc.detail}, status_code=404
            )
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)

    return app
