from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.config import settings
from .api.routes.health import router as health_router
from .api.routes.snapshots import router as snapshots_router

app = FastAPI(title="Snaplicator API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/health", tags=["health"]) 
app.include_router(snapshots_router, prefix="/snapshots", tags=["snapshots"]) 