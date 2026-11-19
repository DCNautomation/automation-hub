# Next Actions: Automation Hub

_Started 2026-10-29. M2 to M5 finished the same day._

## Where things stand
- Stack locked: FastAPI + Postgres 16 + React/TS, Docker Compose, deploy to Railway or Render.
- Docker Desktop (WSL 2) installed and working. The whole stack runs locally: Postgres, API, worker.
- M2 core engine is done and verified: signed ingest, idempotency, SKIP LOCKED claiming, worker loop, retries with backoff and jitter, dead state, stale-job recovery, audit writes.
- 267 pytest tests pass against real Postgres at 98% line+branch coverage, plus 36 Vitest UI tests. Mutation-checked: deliberately breaking SKIP LOCKED, the signature check, the dead-state boundary, ON CONFLICT, retry delay, the ownership guard, finish retry, the migration lock, and each part of replay protection (key not signed, timestamp not signed, no freshness check, boundary off by one, one-sided window, missing timestamp allowed) each made a test fail.
- Replay protection is in (Diego said yes 2026-10-29): the signature now covers `timestamp
key
body`, and the timestamp must be within 5 minutes either side of the server clock. Smoke-tested live: fresh 202, resend 200, captured request under a new key 401, 1-hour-old request 401.
- Smoke-tested live over HTTP: signed webhook gets 202, repeat gets 200 with the same ids, bad signature and unknown source both get 401, NaN payload gets 422, and the real worker ran the job once.
- Postgres and FastAPI can now go on a resume (the engine has run for real), but only after Diego decides they count. See `references/resume-reference/facts.md`.

## Diego
- [ ] Pick Railway or Render (needed by the deploy milestone). Alerts are settled: Telegram.
- [ ] Create a GitHub repo for the hub (CI only runs from a repo root) and say if it should be public (portfolio) or private
- [ ] Make a Telegram bot with @BotFather and put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in `.env` (steps in the README), then run `python -m app.alerts test`
- [ ] Decide whether Postgres and FastAPI go on a resume now
- [ ] Create your real dashboard user so you can open the dashboard: `docker compose exec api python -m app.users create <name> --role admin` (it prompts for the password, so it never lands in this chat or a file)

## M3 notes
- Auth is opaque bearer tokens kept server-side (revocable, hash only in the DB), not JWTs and not cookies, so logout really works and there is no CSRF surface.
- Mutation-checked: skipping the advisory lock, lockout, password check, success reset, IP cap, session expiry, admin check, fresh retry budget, retry row lock, retryable statuses, keyset cursor, page-end logic and the proxy-header rule each made a test fail.
- Smoke-tested live: login, runs, summary, sources, detail, retry refused on a succeeded run (409), logout, token dead afterwards. Migration 002 applied. The throwaway smoke user was deleted. No real user exists yet.

## M4 notes
- Web: `web/` (Vite + React + TS). 36 Vitest tests; 11 deliberate breakages (retry for viewers, retry for succeeded runs, UTC instead of PHT, wrong attention filter, error boundary off, auth guard off, no sign-out on 401, lockout message, bad run id, missing 404 route, ignored page cursor) each failed a test.
- Looked at in a real browser (Edge via Playwright) at 1440, 768 and 375 wide: no horizontal overflow, no failed requests or console errors, keyboard focus reaches a run and Enter opens it, admin retry produced a real `job.manual_retry` audit row, viewer sees no Retry button, wrong password and 404 pages checked.
- Look: light concrete page with one dark departures-board table. Only Retrying and Gave up get solid status plates, so failures stand out. Archivo (variable width) self-hosted.
- Demo data from that walkthrough is still in the dev database: sources `invoices` and `crm-sync` (handler `nope`, so they fail on purpose), 17 runs. The temporary browser-test users were deleted, so there is still no login until you create yours. `crm-sync` runs keep retrying with growing delays.
- Not covered: no automated browser (end to end) tests in the repo, a screen reader pass, or a Windows high-contrast check.

## M5 notes
- Alerts are Telegram only (Diego's choice). The `alert_rules` table still allows an `email` channel, but nothing sends email.
- Flow: the worker runs `alerts.tick()` every 15 s: `evaluate()` queues messages in `alert_deliveries` (unique per rule and event, advisory-locked), `deliver()` claims them with SKIP LOCKED and a lease, sends, and retries with backoff. At-least-once.
- Mutation-checked (13): old deaths alerting, dedupe ignoring the attempt number, no burst cap, no SKIP LOCKED, no evaluate lock (found a missing spike-concurrency test, added), no spike cooldown, token not masked, no backoff, never giving up, alerts without a token, inactive rules still sending, retry-after ignored. Each failed a test. One real bug was found by the tests: spike dedupe keys used whole seconds, now microseconds.
- Live check against the real stack with a local fake Telegram: a run died, the alert was queued and retried through three failed sends (wrong port), then delivered with the right text and audit rows. Dev rule and fake server were removed afterwards.
- NOT tested: a real Telegram bot (no token yet; run `python -m app.alerts test` once you have one), and importing the n8n flow into a running n8n (its Code node logic was verified against the live hub outside n8n).
- Not built: alert rules in the dashboard (CLI only), email alerts, alerts when the worker itself is down.

## Build order
- [x] M0 to M1 Scaffold, schema, Docker
- [x] M2 Core engine and tests
- [x] M3 API: keyset-paginated runs list and filters, summary, run detail with attempts and audit trail, admin-only manual retry, login with roles, DB-stored lockout by username + IP, per-IP throttle, sessions, CORS
- [x] M4 Dashboard: runs board with filters and paging, run detail, needs-attention view, retry button, error boundary, 404 pages, PHT times
- [x] M5 Alerts: Telegram on dead runs and failure spikes (outbox, retries, rules by CLI), example n8n flow in `docs/n8n/`
- [ ] M6 Ship (Diego wants it built and checked locally first, nothing pushed): deploy live, screenshots, architecture diagram, README polish, then run site-qa on it
  - [x] GitHub Actions CI written (`.github/workflows/ci.yml`, 3 jobs). The exact pytest command was run locally with the coverage gate: 267 pass, 97.76%. The workflow itself has not run on GitHub yet because the hub has no repo of its own (it sits inside the EA JEGJEG repo, and Actions only reads `.github/` at a repo root). Needs Diego to create a GitHub repo for the hub, or confirm it should be split out.
- [ ] After the hub is live: add the app-picker link to Trusvana's landing page (`C:\CRM`, separate branch, Diego approves deploy)

## M6 notes (local, 2026-10-30)
- Least-privilege DB role `hub_app` (migration `004_app_role.sql`, `app/dbroles.py`): API, worker and CLIs run as it; only startup migrates as the owner. TRUNCATE on `audit_log` is also blocked by a trigger. 15 new tests; 3 migration mutants each failed a test (extra UPDATE grant, missing TRUNCATE trigger, forgotten grant).
- API headers (nosniff, no-referrer, no-store, `default-src 'none'` CSP, except /docs) and a `web` container (nginx, strict CSP in `web/nginx.conf.template`). Loaded in Edge: sign in, 19 runs, run detail, font loaded, no CSP violations, injected inline script refused.
- `.env` now has `APP_DB_PASSWORD` (generated locally, never printed). Compose refuses to start without it.
- Lesson: Postgres roles are server-wide, so a test must never change the real `hub_app` password. The role test uses a throwaway member role (`hub_app_test`).
- 288 API tests, 97.6% coverage at the end of the first M6 pass. Web tests/typecheck unchanged (36).

## M6 notes, second pass (local, 2026-10-30)
Diego un-parked the pre-deploy list ("please do this"). Everything that can be built and proven locally is done. 374 API tests, 97.03% coverage, 36 UI tests.
- **Worker health:** `worker_heartbeats` table (migration `005`), a heartbeat thread in the worker, `GET /healthz/worker` (503 when no beat for 60 s, or due jobs wait over 300 s). Checked live: graceful stop gives 503 at once, `kill -9` gives 503 after about 60 s. Found and fixed a shutdown race (a beat in flight could re-add the row after it was cleared): the worker now joins the thread first.
- **Cleanup:** `maintenance.purge()` hourly from the worker: expired sessions (1 day grace), login attempts older than 30 days (never below 1 day), old heartbeats. Runs, events, attempts, audit and alert rows are never purged.
- **Rate limits:** in-memory sliding window per client address, `INGEST_RATE_PER_MINUTE` and `READ_RATE_PER_MINUTE` (300 each, 0 = off). Throttle runs before the token lookup and before reading an ingest body. Per API process only (documented).
- **Sources CLI:** `python -m app.sources create|list|rotate|enable|disable`. This did not exist before: sources could only be made with SQL. Secrets print once and never reach the audit log, the API or the list.
- **Seed:** `python -m app.seed demo|reset`. 19 runs through real signed ingest and the real worker (a `demo` handler fails when the payload carries an `error`). `demo-viewer` read-only login. Reset needs the owner login.
- **Preflight:** `python -m app.preflight` (runs in the container start; only enforces when `HUB_ENV=production`). Fails on weak or default passwords, app not on `hub_app`, proxy hops 0, localhost or http origins, short signing secrets.
- **Docs and portfolio:** `docs/deploy.md`, `.env.production.example`, `docs/architecture.svg`, 5 screenshots in `docs/screenshots/` (taken from a scratch database `hub_demo`, since dropped; Diego's dev data untouched), README rewritten.
- **Mutation checks:** 17 deliberate breakages of the new guards, every one failed a test (stale heartbeats counted alive, lag never reported, endpoint always 200, no session grace, retention floor, off-by-one limit, window never slides, reads and ingest unthrottled, secret in audit, weak secrets, rotate not rotating, reset deleting everything, demo retries not scheduled, proxy hops unchecked, localhost accepted, short secrets pass).
- **Still blocked on Diego:** GitHub repo (CI has never run on GitHub), Railway vs Render, real Telegram bot (alerts only tested against a fake Telegram), importing the n8n flow into a real n8n, the managed-Postgres role check on a real host, `site-qa` on a live dashboard, and whether to add one real handler (today only `log` and the seeded `demo`).
- **Old dev data:** Diego's dev database still holds the earlier test sources `demo`, `invoices`, `crm-sync` (the last two use handler `nope`, so they keep retrying on purpose). Not touched. Ask before removing.
- Still to do locally: seed script and screenshots, architecture diagram, README polish. Needs Diego: repo, host, deploy.

## Known limits (not bugs, decide later)
- Lockout is per (username, ip) by design. A botnet spread over many addresses can still guess one account at a time per address; the 12 character minimum and scrypt are the defence there.
- `login_attempts` rows and expired sessions of users who never log in again are never purged. Add a cleanup job before it matters.
- No general rate limit on the read endpoints, only on login.
- If the worker is down, nothing alerts about that. Add an external uptime check on `/healthz` and a heartbeat before relying on this unattended.
- The dashboard keeps its token in `sessionStorage`, so an XSS bug would expose it. There is no user-supplied HTML rendered anywhere (React escapes everything), and the nginx container now sends a strict CSP (no inline script, no third-party origins). If it is hosted elsewhere (a static host), copy those headers.
- `audit_log` rejects UPDATE, DELETE and TRUNCATE (trigger) and the app role cannot even try. The table owner could still drop the trigger, so keep the owner login out of the running app (it only runs at startup).
- Handlers run at-least-once. Only a `log` handler exists so far.
- `last_error` and the audit log store handler error text as-is, so a future handler must not put secrets in exception messages.

## Rules
- The hub never touches Trusvana's files, DB or API, and the reverse.

## Status
🟢 Works locally (`docker compose up -d`, dashboard on http://127.0.0.1:8080), with the pre-deploy work done (second M6 pass, 2026-10-30). Blocked on Diego: GitHub repo, Railway vs Render, Telegram bot token, n8n import. Nothing is pushed or deployed.
