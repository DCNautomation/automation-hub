"""Automation Hub API: signed webhook ingest, plus the authenticated dashboard API (see routes.py)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import config, db, deps, health, ingest, ratelimit, routes


@asynccontextmanager
async def lifespan(app):
    db.init_pool(config.DATABASE_URL)
    yield
    db.close_pool()


app = FastAPI(title="Automation Hub", version="0.2.0", lifespan=lifespan)
# Bearer tokens, not cookies, so no credentials mode is needed and there is no CSRF surface.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(routes.router)

# The API only ever returns JSON, so browsers are told to treat every response as inert data. The
# interactive docs page is the one exception: it loads Swagger UI from a CDN and needs scripts.
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"  # tokens and run payloads must not sit in a shared cache
    if request.url.path not in DOCS_PATHS:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response

# Only harmless headers are kept with the stored event. Signature and auth headers never are.
STORED_HEADERS = ("content-type", "user-agent")

# Fixed, generic messages: the response never echoes input or internals back to the sender.
INGEST_ERRORS = {
    # An unknown source answers exactly like a bad signature, so names can't be enumerated.
    ingest.UnknownSource: (401, "invalid signature"),
    ingest.BadSignature: (401, "invalid signature"),
    ingest.StaleTimestamp: (401, "timestamp outside the allowed window, check the sender's clock"),
    ingest.MissingIdempotencyKey: (400, "X-Idempotency-Key header is required (1 to 200 characters)"),
    ingest.PayloadTooLarge: (413, "payload too large"),
    ingest.InvalidPayload: (422, "payload must be a JSON object"),
}


@app.get("/healthz")
def healthz():
    try:
        with db.connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.get("/healthz/worker")
def healthz_worker():
    """For an uptime monitor: 200 when a worker is alive and keeping up, 503 when it is not."""
    try:
        status = health.worker_status()
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return JSONResponse(status_code=200 if status["ok"] else 503, content=status)


@app.post("/ingest/{source}")
async def ingest_webhook(
    source: str,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_hub_timestamp: str | None = Header(default=None),
    x_idempotency_key: str | None = Header(default=None),
):
    deps.throttle(ratelimit.ingest, request)  # before reading any body, so a flood costs almost nothing
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > ingest.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    # Content-Length can be missing (chunked) or wrong, so count what actually arrives and stop
    # reading at the limit instead of buffering an unauthenticated multi-GB body.
    chunks, received = [], 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > ingest.MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    headers = {k: v for k, v in request.headers.items() if k in STORED_HEADERS}
    try:
        receipt = await run_in_threadpool(
            ingest.receive, source, body, x_hub_signature_256, x_hub_timestamp, x_idempotency_key, headers
        )
    except ingest.IngestError as exc:
        # Most specific class first: PayloadTooLarge subclasses InvalidPayload.
        for error_class, (status, message) in INGEST_ERRORS.items():
            if isinstance(exc, error_class):
                raise HTTPException(status_code=status, detail=message)
        raise
    return JSONResponse(
        status_code=200 if receipt.duplicate else 202,
        content={"event_id": receipt.event_id, "job_id": receipt.job_id, "duplicate": receipt.duplicate},
    )
