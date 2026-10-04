"""时段账：把接近事件、规避窗口与外部指挥目录接起来。

- 圈次（slot）按卫星的固定周期圈次划分，归属卫星以外部目录为准。
- 批准时占住圈次：申请圈次容量满或窗口重叠就顺延到下一个可用圈次并说明原因。
- 同一颗卫星的窗口时间不能重叠。
- 对账以外部目录为准：目录不再分配的圈次属多占，目录已分配而本地未占属少占；
  未执行的窗口立即失效并退回重议，已执行的留原记录。
"""

import json
import math
from datetime import datetime, timedelta, timezone

from .audit import append_audit_event, canonical_json, now_iso
from .domain import ConflictError, DomainError, NotFoundError

SLOT_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
SLOT_DURATION = timedelta(minutes=90)
ACTIVE_WINDOW_STATUSES = ("approved", "queued", "executed")


def parse_dt(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def parse_window(text):
    """把批准时给的规避窗口文本解析成 (start_iso, end_iso)。"""
    text = str(text).strip()
    if "/" in text:
        start_s, end_s = [part.strip() for part in text.split("/", 1)]
    else:
        start_s, end_s = text, None
    start_dt = parse_dt(start_s)
    end_dt = parse_dt(end_s) if end_s else start_dt + timedelta(hours=1)
    if end_dt <= start_dt:
        raise DomainError("invalid_window", "规避窗口结束时间必须晚于开始时间")
    return start_dt.isoformat(), end_dt.isoformat()


def revolution_for(dt):
    seconds = (parse_dt(dt) - SLOT_EPOCH).total_seconds()
    return int(math.floor(seconds / SLOT_DURATION.total_seconds()))


def slot_bounds(revolution_no):
    start = SLOT_EPOCH + revolution_no * SLOT_DURATION
    return start.isoformat(), (start + SLOT_DURATION).isoformat()


def find_or_create_slot(conn, satellite_id, revolution_no):
    row = conn.execute(
        "SELECT * FROM slots WHERE satellite_id=? AND revolution_no=?",
        (satellite_id, revolution_no),
    ).fetchone()
    if row is not None:
        return dict(row)
    start_s, end_s = slot_bounds(revolution_no)
    catalog = conn.execute(
        "SELECT satellite_id FROM catalog_entries WHERE satellite_id=? AND revolution_no=?",
        (satellite_id, revolution_no),
    ).fetchone()
    source = "catalog" if catalog is not None else "local"
    cur = conn.execute(
        "INSERT INTO slots(satellite_id,revolution_no,start_ts,end_ts,capacity,source,version,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (satellite_id, revolution_no, start_s, end_s, 1, source, 1, now_iso(), now_iso()),
    )
    return dict(conn.execute("SELECT * FROM slots WHERE id=?", (cur.lastrowid,)).fetchone())


def _active_windows(conn, satellite_id, exclude_window_id=None):
    rows = conn.execute(
        "SELECT w.* FROM windows w WHERE w.satellite_id=? AND w.status IN (%s)"
        % ",".join("?" * len(ACTIVE_WINDOW_STATUSES)),
        (satellite_id,) + ACTIVE_WINDOW_STATUSES,
    ).fetchall()
    result = []
    for row in rows:
        window = dict(row)
        if exclude_window_id is not None and window["id"] == exclude_window_id:
            continue
        result.append(window)
    return result


def _has_overlap(candidate_start, candidate_end, active):
    for window in active:
        existing_start = parse_dt(window["window_start"])
        existing_end = parse_dt(window["window_end"])
        if candidate_start < existing_end and candidate_end > existing_start:
            return True
    return False


def _candidate(slot, req_s, req_e, duration, start_rev):
    slot_start = parse_dt(slot["start_ts"])
    slot_end = parse_dt(slot["end_ts"])
    if slot["revolution_no"] == start_rev:
        candidate_start = max(req_s, slot_start)
        candidate_end = min(req_e, slot_end)
        if candidate_end <= candidate_start:
            return None
    else:
        candidate_start = slot_start
        candidate_end = candidate_start + duration
    return candidate_start, candidate_end


def _try_slot(slot, req_s, req_e, duration, start_rev, active):
    count = sum(1 for window in active if window["slot_id"] == slot["id"])
    if count >= slot["capacity"]:
        return None
    placed = _candidate(slot, req_s, req_e, duration, start_rev)
    if placed is None:
        return None
    candidate_start, candidate_end = placed
    if _has_overlap(candidate_start, candidate_end, active):
        return None
    queue_reason = None
    if slot["revolution_no"] != start_rev:
        queue_reason = "申请圈次 %s 容量已满或窗口重叠，顺延至第 %s 圈" % (start_rev, slot["revolution_no"])
    return slot, candidate_start.isoformat(), candidate_end.isoformat(), queue_reason


def find_feasible_slot(conn, satellite_id, req_start, req_end, exclude_window_id=None):
    """找到最早可用圈次，返回 (slot, window_start_iso, window_end_iso, queue_reason)。

    可用条件：该圈次未满容量，且放入的窗口与同卫星现有窗口不重叠。
    归属看外部目录：目录已覆盖该卫星时只占目录圈次；目录未覆盖时才用本地圈次。
    申请圈次可用时直接占用；否则顺延并记录原因。
    """
    req_s = parse_dt(req_start)
    req_e = parse_dt(req_end)
    duration = req_e - req_s
    start_rev = revolution_for(req_s)
    active = _active_windows(conn, satellite_id, exclude_window_id)
    catalog_cover = conn.execute(
        "SELECT 1 FROM catalog_entries WHERE satellite_id=? LIMIT 1", (satellite_id,)
    ).fetchone() is not None
    if catalog_cover:
        rows = conn.execute(
            "SELECT * FROM slots WHERE satellite_id=? AND source='catalog' AND revolution_no>=? ORDER BY revolution_no",
            (satellite_id, start_rev),
        ).fetchall()
        for row in rows:
            result = _try_slot(dict(row), req_s, req_e, duration, start_rev, active)
            if result is not None:
                return result
        raise DomainError("no_available_slot", "外部目录未授权卫星 %s 在该时间后的可用圈次" % satellite_id)
    for revolution_no in range(start_rev, start_rev + 512):
        slot = find_or_create_slot(conn, satellite_id, revolution_no)
        result = _try_slot(slot, req_s, req_e, duration, start_rev, active)
        if result is not None:
            return result
    raise DomainError("no_available_slot", "卫星 %s 在请求时间后没有可用圈次" % satellite_id)


def allocate_window(conn, item_id, satellite_id, req_start, req_end):
    slot, window_start, window_end, queue_reason = find_feasible_slot(conn, satellite_id, req_start, req_end)
    cur = conn.execute(
        "INSERT INTO windows(item_id,satellite_id,slot_id,window_start,window_end,status,queue_reason,version,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (item_id, satellite_id, slot["id"], window_start, window_end, "approved", queue_reason, 1, now_iso(), now_iso()),
    )
    return dict(conn.execute("SELECT * FROM windows WHERE id=?", (cur.lastrowid,)).fetchone())


def mark_windows_executed(conn, item_id):
    conn.execute(
        "UPDATE windows SET status='executed', version=version+1, updated_at=? "
        "WHERE item_id=? AND status IN ('approved','queued')",
        (now_iso(), item_id),
    )


def release_windows(conn, item_id):
    conn.execute(
        "UPDATE windows SET status='invalidated', version=version+1, updated_at=? "
        "WHERE item_id=? AND status IN ('approved','queued')",
        (now_iso(), item_id),
    )


def reschedule_window(conn, window_id, new_start, new_end, expected_version):
    row = conn.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
    if row is None:
        raise NotFoundError("window_not_found", "规避窗口不存在")
    window = dict(row)
    if expected_version is not None and int(expected_version) != int(window["version"]):
        raise ConflictError("version_conflict", "窗口已被其他调度员修改，请重新读取后重排")
    slot, window_start, window_end, queue_reason = find_feasible_slot(
        conn, window["satellite_id"], new_start, new_end, exclude_window_id=window_id
    )
    conn.execute(
        "UPDATE windows SET slot_id=?, window_start=?, window_end=?, queue_reason=?, version=version+1, updated_at=? WHERE id=?",
        (slot["id"], window_start, window_end, queue_reason, now_iso(), window_id),
    )
    return dict(conn.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone())


def sync_catalog_entry(conn, satellite_id, revolution_no, start_ts, end_ts, maneuver_ref, status):
    existing = conn.execute(
        "SELECT * FROM catalog_entries WHERE satellite_id=? AND revolution_no=?",
        (satellite_id, revolution_no),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE catalog_entries SET start_ts=?,end_ts=?,maneuver_ref=?,status=?,version=version+1,updated_at=? WHERE id=?",
            (start_ts, end_ts, maneuver_ref, status, now_iso(), existing["id"]),
        )
        entry_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO catalog_entries(satellite_id,revolution_no,start_ts,end_ts,maneuver_ref,status,version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (satellite_id, revolution_no, start_ts, end_ts, maneuver_ref, status, 1, now_iso(), now_iso()),
        )
        entry_id = cur.lastrowid
    # 外部目录是归属的权威来源：把对应圈次同步为 catalog 归属；不存在则建出。
    slot = conn.execute(
        "SELECT * FROM slots WHERE satellite_id=? AND revolution_no=?",
        (satellite_id, revolution_no),
    ).fetchone()
    if slot is None:
        slot_start, slot_end = slot_bounds(revolution_no)
        conn.execute(
            "INSERT INTO slots(satellite_id,revolution_no,start_ts,end_ts,capacity,source,version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (satellite_id, revolution_no, slot_start, slot_end, 1, "catalog", 1, now_iso(), now_iso()),
        )
    elif slot["source"] != "catalog":
        conn.execute(
            "UPDATE slots SET source='catalog', version=version+1, updated_at=? WHERE id=?",
            (now_iso(), slot["id"]),
        )
    return dict(conn.execute("SELECT * FROM catalog_entries WHERE id=?", (entry_id,)).fetchone())


def reconcile(conn):
    """以外部目录为准对账。

    返回 {"discrepancies": [...], "invalidated": [window_id...], "kept": [window_id...]}。
    """
    discrepancies = []
    invalidated = []
    kept = []
    rows = conn.execute(
        "SELECT w.*, s.revolution_no AS slot_rev FROM windows w "
        "JOIN slots s ON w.slot_id=s.id JOIN items i ON w.item_id=i.id "
        "WHERE i.status != 'cancelled' AND w.status IN ('approved','queued','executed')"
    ).fetchall()
    for row in rows:
        window = dict(row)
        catalog = conn.execute(
            "SELECT * FROM catalog_entries WHERE satellite_id=? AND revolution_no=?",
            (window["satellite_id"], window["slot_rev"]),
        ).fetchone()
        kind = None
        reason = None
        if catalog is None:
            kind, reason = "over", "外部目录无此圈次，属多占"
        elif catalog["status"] == "cancelled":
            kind, reason = "over", "外部目录已取消该圈次，属多占"
        if kind is None:
            if window["status"] == "executed":
                kept.append(window["id"])
            continue
        discrepancies.append({
            "type": kind,
            "window_id": window["id"],
            "item_id": window["item_id"],
            "satellite_id": window["satellite_id"],
            "reason": reason,
        })
        if window["status"] == "executed":
            kept.append(window["id"])
            continue
        conn.execute(
            "UPDATE windows SET status='invalidated', version=version+1, updated_at=? WHERE id=?",
            (now_iso(), window["id"]),
        )
        item_row = conn.execute("SELECT * FROM items WHERE id=?", (window["item_id"],)).fetchone()
        if item_row is not None:
            item = dict(item_row)
            payload = json.loads(item["payload"])
            payload["reopen_reason"] = reason
            payload.pop("approved_maneuver", None)
            conn.execute(
                "UPDATE items SET status='reopened', payload=?, version=version+1, updated_at=? WHERE id=?",
                (canonical_json(payload), now_iso(), window["item_id"]),
            )
            append_audit_event(conn, window["item_id"], "reconcile_invalidate", "system", "system",
                                {"window_id": window["id"], "reason": reason})
        invalidated.append(window["id"])
    # 少占：目录已分配圈次但本地没有任何有效窗口。
    for catalog in conn.execute("SELECT * FROM catalog_entries WHERE status='planned'").fetchall():
        catalog = dict(catalog)
        window = conn.execute(
            "SELECT w.id FROM windows w JOIN slots s ON w.slot_id=s.id "
            "WHERE w.satellite_id=? AND s.revolution_no=? AND w.status IN ('approved','queued','executed')",
            (catalog["satellite_id"], catalog["revolution_no"]),
        ).fetchone()
        if window is None:
            discrepancies.append({
                "type": "under",
                "satellite_id": catalog["satellite_id"],
                "revolution_no": catalog["revolution_no"],
                "reason": "目录已分配圈次但本地未占，属少占",
            })
    return {"discrepancies": discrepancies, "invalidated": invalidated, "kept": kept}
