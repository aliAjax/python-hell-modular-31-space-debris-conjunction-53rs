import hashlib
import json
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_hash(previous_hash, event):
    payload = canonical_json(event)
    return hashlib.sha256((previous_hash + payload).encode("utf-8")).hexdigest()


def append_audit_event(conn, item_id, event_type, actor, role, payload):
    """Append a hash-chained audit event on an existing DB connection/transaction."""
    row = conn.execute(
        "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    previous = row["event_hash"] if row else "GENESIS"
    event = {
        "item_id": item_id,
        "event_type": event_type,
        "actor": actor,
        "role": role,
        "payload": payload,
        "created_at": now_iso(),
    }
    event_hash = audit_hash(previous, event)
    conn.execute(
        "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
    )
    return event_hash
