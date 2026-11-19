"""Append-only audit trail. Call inside the same transaction as the change it describes."""
from psycopg.types.json import Jsonb


def record(conn, actor, action, entity_type, entity_id=None, before=None, after=None):
    conn.execute(
        "INSERT INTO audit_log (actor, action, entity_type, entity_id, before, after) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            actor,
            action,
            entity_type,
            entity_id,
            Jsonb(before) if before is not None else None,
            Jsonb(after) if after is not None else None,
        ),
    )
