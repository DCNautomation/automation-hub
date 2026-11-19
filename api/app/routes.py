"""Dashboard API: login, runs, sources. Everything here needs a bearer token; retry needs admin."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app import auth, runs, schemas
from app.deps import bearer_token, client_ip, current_user, require_admin

router = APIRouter()


def _utc(value):
    """A timestamp sent without an offset is read as UTC rather than guessed at."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@router.post("/auth/login", response_model=schemas.LoginOut)
def login(body: schemas.LoginIn, request: Request):
    try:
        session = auth.login(body.username, body.password, client_ip(request))
    except auth.LoginLocked as exc:
        raise HTTPException(
            status_code=429,
            detail="too many failed attempts, try again later",
            headers={"Retry-After": str(exc.retry_after)},
        )
    except auth.InvalidCredentials:
        raise HTTPException(status_code=401, detail="invalid username or password")
    return {
        "token": session.token,
        "expires_at": session.expires_at,
        "user": {"username": session.principal.username, "role": session.principal.role},
    }


@router.get("/auth/me", response_model=schemas.UserOut)
def me(user=Depends(current_user)):
    return {"username": user.username, "role": user.role}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, user=Depends(current_user)):
    auth.logout(bearer_token(request))
    return Response(status_code=204)


@router.get("/runs", response_model=schemas.RunPage)
def list_runs(
    status: list[schemas.Status] | None = Query(default=None),
    source: str | None = Query(default=None, max_length=40),
    since: datetime | None = None,
    until: datetime | None = None,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=25, ge=1, le=100),
    user=Depends(current_user),
):
    return runs.list_runs(status, source, _utc(since), _utc(until), before, limit)


# Declared before /runs/{job_id} so "summary" is never read as an id.
@router.get("/runs/summary", response_model=schemas.Summary)
def runs_summary(since: datetime | None = None, user=Depends(current_user)):
    return runs.summary(_utc(since))


@router.get("/runs/{job_id}", response_model=schemas.RunDetail)
def run_detail(job_id: int, user=Depends(current_user)):
    try:
        return runs.run_detail(job_id)
    except runs.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")


@router.post("/runs/{job_id}/retry", response_model=schemas.RunDetail)
def retry_run(job_id: int, user=Depends(require_admin)):
    try:
        return runs.retry(job_id, f"user:{user.username}")
    except runs.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    except runs.NotRetryable as exc:
        raise HTTPException(status_code=409, detail=f"only failed or dead runs can be retried (this one is {exc.status})")


@router.get("/sources", response_model=list[schemas.SourceOut])
def sources(user=Depends(current_user)):
    return runs.list_sources()
