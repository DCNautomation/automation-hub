#!/bin/sh
# Free-host start: one container runs the migrations, the worker (background) and the API.
# Free hosts have no separate always-on worker service, so the worker shares the container.
# If the worker dies nothing restarts it: /healthz/worker goes 503 and the uptime monitor alerts.
set -e
python -m app.migrate
python -m app.dbroles
python -m app.preflight
python -m app.worker &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
