from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes import approvals, audit, jobs, tasks
from src.api.schemas import HealthResponse
from src.common.config import settings
from src.common.logging import get_logger, setup_logging
from src.storage.rds import init_db
from src.storage.s3 import ensure_bucket_exists

_STATIC_DIR = Path(__file__).parent / "static"

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("app.startup", env=settings.APP_ENV, version="0.1.0")
    init_db()
    if settings.APP_ENV == "local":
        ensure_bucket_exists()
    yield
    logger.info("app.shutdown")


app = FastAPI(
    title="Autonomous Agent Platform",
    description="AI-powered autonomous task execution platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.APP_ENV == "local" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks.router, prefix="/v1/tasks", tags=["tasks"])
app.include_router(jobs.router, prefix="/v1/jobs", tags=["jobs"])
app.include_router(audit.router, prefix="/v1/audit", tags=["audit"])
app.include_router(approvals.router, prefix="/v1/approvals", tags=["approvals"])


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health():
    return HealthResponse(env=settings.APP_ENV)


@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
def serve_ui():
    return FileResponse(_STATIC_DIR / "index.html")
