"""What a job does with its payload. A source picks one by name (sources.handler).

A handler takes the parsed payload dict and raises on failure; the worker turns that into a
retry. Add real integrations here (n8n call, email, CRM write) and register them in HANDLERS.
"""
import logging

log = logging.getLogger("handlers")


def log_handler(payload):
    log.info("payload received: %s", payload)


def demo_handler(payload):
    """For the seeded demo data (python -m app.seed). Succeeds, unless the payload carries an
    "error" message, in which case it raises that, so demo failures come from the real retry path."""
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    log.info("demo payload handled: %s", payload)


HANDLERS = {"log": log_handler, "demo": demo_handler}
