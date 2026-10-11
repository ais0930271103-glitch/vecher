import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import create_all
from .routers import app_update, auth, invite_page, chat, city, places, invites, pairs, plans, push, settings as settings_router, solo, today, us, wishes
from .scheduler import run_forever

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all()
    task = asyncio.create_task(run_forever()) if get_settings().scheduler_enabled else None
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
app.include_router(today.router)
app.include_router(wishes.router)
app.include_router(plans.router)
app.include_router(places.router)
app.include_router(us.router)
app.include_router(solo.router)
app.include_router(push.router)
app.include_router(invites.router)
app.include_router(city.router)
app.include_router(chat.router)
app.include_router(app_update.router)
app.include_router(invite_page.router)
app.include_router(settings_router.router)


@app.get("/health", tags=["system"])
async def health() -> dict:
    return {"ok": True, "env": settings.env, "version": app.version}
