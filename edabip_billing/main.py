"""
Standalone demo app for the billing module.

This exists so the module can be run and tested on its own. To fold it
into the real EDABIP backend instead of running it standalone, skip this
file and in your existing `main.py` add:

    from app.database import Base, engine
    from app.billing import router as billing_router, webhooks as billing_webhooks

    Base.metadata.create_all(bind=engine)  # or use your Alembic migration instead
    app.include_router(billing_router.router)
    app.include_router(billing_webhooks.router)

Run this standalone demo with:
    uvicorn main:app --reload
"""
from dotenv import load_dotenv

load_dotenv()  # must run before any `app.*` import - app/config.py reads
                # environment variables at import time, so loading .env
                # any later would be too late for those values to apply

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, engine
from app.router import router as billing_router
from app.webhooks import router as webhook_router
from app import seed_plans


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)  # swap for an Alembic migration in production
    seed_plans.seed()
    yield


app = FastAPI(title="EDABIP Billing", version="1.0.0", lifespan=lifespan)

# Lets the React frontend (running on a different origin, e.g. the Vite
# dev server on localhost:5173) actually call this API from the browser.
# When this module gets folded into the main EDABIP backend, this can be
# removed if that app already sets up its own CORSMiddleware.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(billing_router)
app.include_router(webhook_router)


@app.get("/health")
def health():
    return {"status": "ok"}
