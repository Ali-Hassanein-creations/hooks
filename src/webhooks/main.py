from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from webhooks import endpoints, events
from webhooks.db import engine

app = FastAPI(title="Webhook Delivery Platform", version="0.1.0")
app.include_router(endpoints.router)
app.include_router(events.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    # Postgres is also the job queue, so one check covers both.
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready"}
