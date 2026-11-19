"""Shared request helpers: who is calling, and from which address."""
from fastapi import Depends, HTTPException, Request

from app import auth, config, ratelimit

UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


def bearer_token(request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


def client_ip(request: Request):
    hops = config.TRUSTED_PROXY_HOPS
    if hops > 0:
        # Each trusted proxy appends the address it saw, so count from the right: anything further
        # left was supplied by the client and can be forged.
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "unknown"


def throttle(limiter, request: Request):
    """429 with Retry-After when this client address has used up its requests for the minute."""
    allowed, retry_after = limiter.check(client_ip(request))
    if not allowed:
        raise HTTPException(
            status_code=429, detail="too many requests, slow down", headers={"Retry-After": str(retry_after)}
        )


def current_user(request: Request):
    # Throttle before the token lookup, so a flood of guessed tokens can't hammer the database either.
    throttle(ratelimit.reads, request)
    principal = auth.authenticate(bearer_token(request))
    if principal is None:
        raise HTTPException(status_code=401, detail="not authenticated", headers=UNAUTHENTICATED)
    return principal


def require_admin(user=Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return user
