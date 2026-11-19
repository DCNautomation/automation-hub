"""Response and request shapes for the dashboard API. Naming a field here is what lets it out:
anything not listed (password hashes, signing secrets) can't leak through a response."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["queued", "running", "failed", "succeeded", "dead"]


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    username: str
    role: Literal["admin", "viewer"]


class LoginOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


class RunItem(BaseModel):
    id: int
    status: Status
    source: str
    idempotency_key: str
    attempts: int
    max_attempts: int
    created_at: datetime
    run_at: datetime
    finished_at: datetime | None
    last_error: str | None


class RunPage(BaseModel):
    items: list[RunItem]
    next_before: int | None


class Summary(BaseModel):
    counts: dict[Status, int]
    total: int


class EventOut(BaseModel):
    event_id: int
    payload: dict[str, Any]
    headers: dict[str, Any]
    received_at: datetime


class AttemptOut(BaseModel):
    attempt_no: int
    worker_id: str
    started_at: datetime
    finished_at: datetime | None
    ok: bool | None
    error: str | None
    duration_ms: int | None


class AuditOut(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class RunDetail(RunItem):
    locked_by: str | None
    locked_at: datetime | None
    event: EventOut
    attempts_log: list[AttemptOut]
    audit: list[AuditOut]


class SourceOut(BaseModel):
    id: int
    name: str
    handler: str
    max_attempts: int
    active: bool
    created_at: datetime
