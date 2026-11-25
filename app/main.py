"""Application entry point: ``uvicorn app.main:app --no-proxy-headers``."""
from __future__ import annotations

import logging
import mimetypes
import shutil
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes_audio, routes_health, routes_legacy, routes_text, routes_video
from app.core.config import FRONTEND_DIR, Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging, log_event, register_secret
from app.core.rate_limit import build_limiter
from app.core.security import BodyLimitMiddleware, SecurityHeadersMiddleware
from app.providers.base import AnalysisProvider
from app.providers.gemini import GeminiProvider
from app.services.analysis_pipeline import AnalysisPipeline
from app.services.video_inspection import Runner as VideoRunner
from app.services.video_jobs import JobManager
from app.utils.temp_files import ensure_upload_dir, sweep_stale_uploads

logger = logging.getLogger("scamshield.app")

# ES modules are only executed when served with a JavaScript MIME type; Windows can map .js to text/plain.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# The ONLY directories served as static files. The frontend root, the upload directory and the source tree are not.
STATIC_DIRECTORIES = ("css", "js")


def create_app(settings: Settings | None = None, provider: AnalysisProvider | None = None,
               video_runner: VideoRunner | None = None) -> FastAPI:
    """Build the application. Tests pass their own ``settings``, a fake ``provider`` and (for video) a fake ffprobe runner."""
    settings = settings if settings is not None else get_settings()
    configure_logging(settings.log_level)
    register_secret(settings.gemini_api_key)
    provider = provider if provider is not None else GeminiProvider(settings)
    limiter = build_limiter(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log_event(
            logger, logging.INFO, "startup",
            env=settings.app_env, model=settings.gemini_model, gemini_configured=settings.gemini_configured,
            rate_limiting=settings.rate_limit_enabled, docs=settings.docs_enabled,
        )
        if not settings.gemini_configured:
            logger.warning(
                "GEMINI_API_KEY is not set: analysis requests will return unable_to_assess "
                "with reason CONFIGURATION_ERROR until it is configured."
            )
        if shutil.which(settings.ffprobe_path) is None and not Path(settings.ffprobe_path).is_file():
            logger.warning("ffprobe was not found (%s): video uploads will be refused with VIDEO_VALIDATION_UNAVAILABLE until it is installed.", settings.ffprobe_path)
        try:
            sweep_stale_uploads(ensure_upload_dir(settings.upload_dir))
        except OSError as exc:
            log_event(logger, logging.WARNING, "upload_dir_unavailable", error=type(exc).__name__)
        app.state.jobs.start()
        yield
        await app.state.jobs.aclose()          # stops workers, fails unfinished jobs, deletes their videos
        await provider.aclose()
        log_event(logger, logging.INFO, "shutdown")

    docs = settings.docs_enabled          # the interactive API docs are not exposed in production
    app = FastAPI(
        title="ScamShield AI", version="1.2.0", lifespan=lifespan,
        docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.limiter = limiter
    app.state.pipeline = AnalysisPipeline(
        provider, max_concurrency=settings.gemini_max_concurrency, max_queue=settings.gemini_max_queue,
    )

    app.state.video_runner = video_runner
    app.state.jobs = JobManager(
        settings, lambda path, size, duration, on_stage: app.state.pipeline.run_video(path, size=size, duration=duration, on_stage=on_stage),
    )

    # Added innermost -> outermost: body limit, CORS, security headers, request context.
    app.add_middleware(BodyLimitMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),     # explicit origins only; never "*"
        allow_credentials=False,                             # no cookies or credentials are used
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    text_router, text_handler = routes_text.build_router(limiter, settings)
    audio_router, audio_handler = routes_audio.build_router(limiter, settings)
    app.include_router(routes_health.build_router(limiter, settings))
    app.include_router(text_router)
    app.include_router(audio_router)
    app.include_router(routes_video.build_router(limiter, settings))
    app.include_router(routes_legacy.build_router(text_handler, audio_handler))

    @app.get("/")
    @limiter.limit(settings.rate_limit_default)
    async def home(request: Request) -> FileResponse:
        index = FRONTEND_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(404, "The web interface is not available.")
        return FileResponse(index, media_type="text/html")

    for name in STATIC_DIRECTORIES:
        directory = FRONTEND_DIR / name
        if directory.is_dir():
            app.mount(f"/{name}", StaticFiles(directory=directory), name=f"frontend-{name}")

    return app


app = create_app()
