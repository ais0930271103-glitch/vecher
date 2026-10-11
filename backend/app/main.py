import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import create_all
from .routers import auth, pairs

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all()
    yield


settings = get_settings()
app = FastAPI(
    title="Вечер API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.env != "prod" else None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_dev_regex if settings.is_dev else None,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(pairs.router)


@app.get("/health", tags=["system"])
async def health() -> dict:
    return {"ok": True, "env": settings.env, "version": app.version}
