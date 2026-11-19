# Deploying the Automation Hub

Written from the code and tested locally with Docker Compose. **Nothing here has been run on a real host yet**, so host-specific details below (Railway, Render, Neon and similar) are what to check, not what has been verified. Do the dry run in "Before the first real start" first.

## What has to run
| Service | Start command | Notes |
|---|---|---|
| API | `python -m app.migrate && python -m app.dbroles && python -m app.preflight && uvicorn app.main:app --host 0.0.0.0 --port 8000` | Runs migrations, then the app. Health check: `GET /healthz` |
| Worker | `python -m app.worker` | Needs its own always-on process, separate from the API. Run one or more copies |
| Dashboard | `web/Dockerfile` (nginx) | The Content-Security-Policy lives in `web/nginx.conf.template`. If it is hosted as plain static files instead, copy those headers or the CSP is lost |
| Postgres 16 | managed | Needs backups (see below) |

Build the dashboard with `VITE_API_URL` set to the API's public https address, and run it with `API_ORIGIN` set to the same value (the CSP names it).

## Settings
Copy `.env.production.example` into the host's environment settings. The ones that matter most:
- `MIGRATE_DATABASE_URL`: the database **owner**, used only at startup. `DATABASE_URL`: the limited **`hub_app`** role everything else runs as. `APP_DB_PASSWORD`: the password set on `hub_app` at every start.
- `TRUSTED_PROXY_HOPS=1` behind a host's proxy, or the login lockout sees the proxy's address for everybody.
- `CORS_ORIGINS` and `DASHBOARD_URL`: the real https dashboard address.
- `HUB_ENV=production` turns the preflight on: the API refuses to start with weak or default passwords, localhost or http origins, `TRUSTED_PROXY_HOPS=0`, or short signing secrets.

## Before the first real start (managed Postgres)
The role setup was only ever tested on local Postgres 16. On the host's database, check first:
1. Create an empty database and note the owner login.
2. Can the owner create roles? `SELECT rolcreaterole, rolsuper FROM pg_roles WHERE rolname = current_user;`. Migration `004` runs `CREATE ROLE hub_app`. If the host does not allow it, create the role by hand (`CREATE ROLE hub_app LOGIN;`) and the migration will skip that step and still do the grants.
3. Run `python -m app.migrate` then `python -m app.dbroles` with the owner URL, then `python -m app.preflight --force`. Fix every FAIL.
4. Confirm the app really runs limited: as `hub_app`, `TRUNCATE audit_log;` must fail with "permission denied".

## After the first start
- Create your login (prompts for the password): `python -m app.users create <name> --role admin`
- Create each real source (prints its signing secret once, copy it into the sender): `python -m app.sources create <name> --handler log --max-attempts 5`. Rotate with `python -m app.sources rotate <name>`.
- Alerts: make a Telegram bot with @BotFather, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, run `python -m app.alerts test`, then `python -m app.alerts rule add dead`.
- **Uptime monitor** (any free one works): watch `GET /healthz` (API and database) and `GET /healthz/worker` (a worker is alive and picking jobs up). The worker one returns 503 when no worker has reported in for 60 seconds, or when due jobs have waited more than 5 minutes. The worker cannot alert about its own death, so this outside check is the only thing that will.
- **Backups:** turn on the host's automated backups, then restore one into a scratch database once to prove it works (`pg_dump` and `pg_restore` are enough). An untested backup is a hope, not a backup.

## Public demo
To let visitors look around: `python -m app.seed demo` adds 19 realistic runs and a read-only `demo-viewer` login (the password is shown once, or set `DEMO_VIEWER_PASSWORD`). Viewers cannot retry runs. `python -m app.seed reset` removes the demo data again; it needs the owner login because the app role is not allowed to delete runs. Preflight warns while demo data is present, which is fine for a public demo.

## Known limits
- The request limits (`INGEST_RATE_PER_MINUTE`, `READ_RATE_PER_MINUTE`) are counted inside each API process. Several replicas each keep their own count.
- Jobs, events and the audit log are never deleted. How long to keep them is a decision for later.
- Handlers run at least once: a real handler must be safe to run twice.
- Only `log` (and the `demo` handler for seeded data) exist. Real integrations are added in `api/app/handlers.py`.
