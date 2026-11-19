-- M6: least-privilege role for the running app.
--   * Migrations run as the database owner. The API, worker and CLIs connect as hub_app, which owns
--     nothing, so it cannot DROP, ALTER, TRUNCATE or disable triggers on any table.
--   * hub_app gets SELECT, INSERT and UPDATE on every table, DELETE only where the code deletes
--     (expired sessions, login attempts), and on audit_log only SELECT and INSERT.
--   * New tables get NO grants by default. A future migration must grant what it needs, and a test
--     fails if it forgets (so a missing grant shows up in CI, not in production).
--   * The password is set outside SQL: python -m app.dbroles (reads APP_DB_PASSWORD).
--   * TRUNCATE is also blocked on audit_log by a trigger, so even the owner cannot wipe it by accident.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_app') THEN
        CREATE ROLE hub_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO hub_app;

GRANT SELECT, INSERT, UPDATE ON
    users, login_attempts, sources, webhook_events, jobs, job_attempts,
    alert_rules, alert_deliveries, sessions
TO hub_app;

GRANT DELETE ON sessions, login_attempts TO hub_app;

GRANT SELECT, INSERT ON audit_log TO hub_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hub_app;

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_block_changes();
