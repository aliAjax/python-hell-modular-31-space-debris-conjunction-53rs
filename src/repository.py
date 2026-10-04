import json
import sqlite3
from datetime import datetime, timezone

from . import scheduling
from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS directory_entries (
                    ref TEXT PRIMARY KEY,
                    satellite_id TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    directory_version TEXT,
                    source TEXT NOT NULL DEFAULT 'external',
                    note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS directory_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ref TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    satellite_id TEXT NOT NULL,
                    requested_slot_ref TEXT NOT NULL,
                    slot_ref TEXT NOT NULL,
                    slot_start TEXT,
                    slot_end TEXT,
                    assigned_slot_start TEXT,
                    assigned_slot_end TEXT,
                    directory_version TEXT,
                    requested_window TEXT NOT NULL,
                    assigned_window TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delay_reasons TEXT NOT NULL DEFAULT '[]',
                    void_reason TEXT,
                    fuel_cost_m_s REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    executed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_active
                    ON bookings(item_id) WHERE status IN ('held', 'queued');
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
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

    # ------------------------------------------------------------------ items

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    # ------------------------------------------------------------- directory

    def upsert_directory_entry(self, entry, actor, role):
        """外部指挥目录同步：按目录主键建/改，改期留版本痕迹。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM directory_entries WHERE ref=?", (entry["ref"],)
            ).fetchone()
            changed = False
            if row is None:
                conn.execute(
                    "INSERT INTO directory_entries(ref,satellite_id,window_start,window_end,capacity,status,directory_version,source,note,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry["ref"], entry["satellite_id"], entry["window_start"], entry["window_end"],
                        entry["capacity"], entry["status"], entry["directory_version"], entry["source"],
                        entry["note"], now_iso(), now_iso(),
                    ),
                )
                change = {"kind": "created"}
                changed = True
            else:
                change = {}
                if row["window_start"] != entry["window_start"] or row["window_end"] != entry["window_end"]:
                    change["window"] = {
                        "from": [row["window_start"], row["window_end"]],
                        "to": [entry["window_start"], entry["window_end"]],
                    }
                if int(row["capacity"]) != int(entry["capacity"]):
                    change["capacity"] = {"from": row["capacity"], "to": entry["capacity"]}
                if row["status"] != entry["status"]:
                    change["status"] = {"from": row["status"], "to": entry["status"]}
                if change:
                    changed = True
                conn.execute(
                    "UPDATE directory_entries SET satellite_id=?,window_start=?,window_end=?,capacity=?,status=?,"
                    "directory_version=?,note=?,updated_at=? WHERE ref=?",
                    (
                        entry["satellite_id"], entry["window_start"], entry["window_end"], entry["capacity"],
                        entry["status"], entry["directory_version"], entry["note"], now_iso(), entry["ref"],
                    ),
                )
            revision_snapshot = {
                key: entry[key]
                for key in ("ref", "satellite_id", "window_start", "window_end",
                            "capacity", "status", "directory_version", "source", "note")
            }
            conn.execute(
                "INSERT INTO directory_revisions(ref,revision,created_at) VALUES(?,?,?)",
                (entry["ref"], canonical_json(revision_snapshot), now_iso()),
            )
            self.append_audit(
                conn, None, "directory_upserted", actor, role,
                {"ref": entry["ref"], "changed": changed, "change": change,
                 "directory_version": entry["directory_version"]},
            )
            conn.execute("COMMIT")
            result = {key: value for key, value in entry.items() if key not in ("start", "end")}
            result["changed"] = changed
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_directory(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM directory_entries ORDER BY satellite_id, window_start"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_directory_entry(self, ref):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM directory_entries WHERE ref=?", (ref,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # --------------------------------------------------------------- ledger

    @staticmethod
    def _slot_from_row(row):
        return {
            "ref": row["ref"],
            "satellite_id": row["satellite_id"],
            "start": scheduling.parse_ts(row["window_start"], "window_start"),
            "end": scheduling.parse_ts(row["window_end"], "window_end"),
            "capacity": row["capacity"],
            "status": row["status"],
            "directory_version": row["directory_version"],
            "source": row["source"],
        }

    @staticmethod
    def _maneuver_payload(fuel, row, assignment):
        return {
            "fuel_cost_m_s": fuel,
            "directory_ref": row["slot_ref"],
            "satellite_id": row["satellite_id"],
            "requested_window": row["requested_window"],
            "maneuver_window": assignment["assigned_window"],
            "booking_status": assignment["status"],
            "delay_reasons": assignment["delay_reasons"],
        }

    def _load_slots(self, conn):
        return [self._slot_from_row(row)
                for row in conn.execute("SELECT * FROM directory_entries").fetchall()]

    @staticmethod
    def _booking_model(row):
        return {
            "id": row["id"],
            "item_id": row["item_id"],
            "requested_slot_ref": row["requested_slot_ref"],
            "slot_ref": row["slot_ref"],
            "satellite_id": row["satellite_id"],
            "req_start": scheduling.parse_ts(row["requested_window"].split("/", 1)[0], "requested_window"),
            "req_end": scheduling.parse_ts(row["requested_window"].split("/", 1)[1], "requested_window"),
            "assigned_start": scheduling.parse_ts(row["assigned_window"].split("/", 1)[0], "assigned_window"),
            "assigned_end": scheduling.parse_ts(row["assigned_window"].split("/", 1)[1], "assigned_window"),
        }

    def _replan(self, conn, quiet_item_ids=(), promoted_ids=()):
        """对全部有效占用重新分配圈次，并同步事件状态。

        quiet 集合内的事件刚刚由调用方自己升过版本（批准/改窗），
        这里只写结果，不再追加审计和版本。
        promoted 集合内的事件是排队补位回原申请圈次，不产生变更。
        """
        slots = self._load_slots(conn)
        active_rows = conn.execute(
            "SELECT * FROM bookings WHERE status IN ('held','queued') ORDER BY id"
        ).fetchall()
        executed_rows = conn.execute(
            "SELECT * FROM bookings WHERE status='executed'"
        ).fetchall()
        assignments = scheduling.replan(
            slots,
            [self._booking_model(row) for row in active_rows],
            [self._booking_model(row) for row in executed_rows],
        )

        changes = {}
        for row in active_rows:
            result = assignments[row["id"]]
            assigned_window = scheduling.format_window(result["assigned_start"], result["assigned_end"])
            slot_row = conn.execute(
                "SELECT * FROM directory_entries WHERE ref=?", (result["slot_ref"],)
            ).fetchone()
            changed = (
                row["status"] != result["status"]
                or row["slot_ref"] != result["slot_ref"]
                or row["assigned_window"] != assigned_window
            )
            conn.execute(
                "UPDATE bookings SET status=?,slot_ref=?,assigned_slot_start=?,assigned_slot_end=?,"
                "assigned_window=?,delay_reasons=?,updated_at=? WHERE id=?",
                (
                    result["status"], result["slot_ref"],
                    slot_row["window_start"], slot_row["window_end"],
                    assigned_window, canonical_json(result["reasons"]), now_iso(), row["id"],
                ),
            )

            item_id = row["item_id"]
            new_item_status = "queued" if result["status"] == "queued" else "coordinating"
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            payload = json.loads(item_row["payload"])
            assignment_payload = {
                "status": result["status"],
                "assigned_window": assigned_window,
                "delay_reasons": result["reasons"],
            }
            payload["approved_maneuver"] = {
                "fuel_cost_m_s": row["fuel_cost_m_s"],
                "directory_ref": result["slot_ref"],
                "satellite_id": row["satellite_id"],
                "requested_window": row["requested_window"],
                "maneuver_window": assigned_window,
                "booking_status": result["status"],
                "delay_reasons": result["reasons"],
            }
            if item_id in quiet_item_ids:
                conn.execute(
                    "UPDATE items SET status=?,payload=?,updated_at=? WHERE id=?",
                    (new_item_status, canonical_json(payload), now_iso(), item_id),
                )
            else:
                old_payload = json.loads(item_row["payload"])
                item_changed = changed or item_row["status"] != new_item_status or old_payload != payload
                if item_changed:
                    conn.execute(
                        "UPDATE items SET status=?,payload=?,version=version+1,updated_at=? WHERE id=?",
                        (new_item_status, canonical_json(payload), now_iso(), item_id),
                    )
                    self.append_audit(
                        conn, item_id, "booking_replanned", "system", "system", assignment_payload
                    )
            changes[item_id] = assignment_payload
        return changes

    @staticmethod
    def _check_version(row, expected_version):
        if expected_version is not None and int(expected_version) != int(row["version"]):
            raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")

    def approve_booking(self, item_id, actor, role, payload, fuel, window_start, window_end,
                        directory_ref, expected_version):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            self._check_version(row, expected_version)
            if row["status"] != "assessed":
                raise DomainError("invalid_state", "当前状态 %s 不允许批准" % row["status"], 409)

            local_slot = False
            if not directory_ref:
                directory_ref = "LOCAL-%d" % item_id
                local_slot = True
                conn.execute(
                    "INSERT INTO directory_entries(ref,satellite_id,window_start,window_end,capacity,status,"
                    "directory_version,source,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        directory_ref, payload["primary_object_id"],
                        window_start.isoformat(), window_end.isoformat(),
                        scheduling.SLOT_DEFAULT_CAPACITY, "scheduled", None, "local",
                        "电话约窗本地兜底圈次", now_iso(), now_iso(),
                    ),
                )
            slot_row = conn.execute(
                "SELECT * FROM directory_entries WHERE ref=?", (directory_ref,)
            ).fetchone()
            if slot_row is None:
                raise DomainError("directory_slot_not_found", "外部目录中没有该圈次，不能占座", 409)
            if slot_row["status"] != "scheduled":
                raise DomainError("directory_slot_unavailable", "目录圈次状态为 %s，不能占座" % slot_row["status"], 409)
            if slot_row["satellite_id"] != payload["primary_object_id"]:
                raise DomainError(
                    "satellite_mismatch",
                    "圈次归属卫星 %s 与接近事件主物体 %s 不一致"
                    % (slot_row["satellite_id"], payload["primary_object_id"]),
                    409,
                )
            slot = self._slot_from_row(slot_row)
            if not (slot["start"] <= window_start and window_end <= slot["end"]):
                raise DomainError("window_outside_slot", "规避窗口超出目录圈次范围", 409)

            try:
                conn.execute(
                    "INSERT INTO bookings(item_id,satellite_id,requested_slot_ref,slot_ref,slot_start,slot_end,"
                    "assigned_slot_start,assigned_slot_end,directory_version,requested_window,assigned_window,"
                    "status,delay_reasons,fuel_cost_m_s,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id, slot_row["satellite_id"], directory_ref, directory_ref,
                        slot_row["window_start"], slot_row["window_end"],
                        slot_row["window_start"], slot_row["window_end"], slot_row["directory_version"],
                        scheduling.format_window(window_start, window_end),
                        scheduling.format_window(window_start, window_end),
                        "held", "[]", fuel, now_iso(), now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("booking_exists", "该事件已有生效中的占座")
            booking_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

            changes = self._replan(conn, quiet_item_ids={item_id})
            assignment = changes[item_id]
            new_status = "queued" if assignment["status"] == "queued" else "coordinating"

            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            new_payload = json.loads(item_row["payload"])
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,updated_at=? WHERE id=?",
                (new_status, version, now_iso(), item_id),
            )
            event_payload = {
                "fuel_cost_m_s": fuel,
                "directory_ref": directory_ref,
                "requested_window": scheduling.format_window(window_start, window_end),
                "booking_id": booking_id,
                "assignment": assignment,
                "local_slot": local_slot,
            }
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "approve", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, "approve", actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def modify_booking_window(self, item_id, actor, role, new_payload, requested_window,
                              start, end, new_slot_ref, expected_version):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            self._check_version(row, expected_version)
            if row["status"] not in ("coordinating", "queued"):
                raise DomainError("invalid_state", "当前状态 %s 不允许修改窗口" % row["status"], 409)
            booking = conn.execute(
                "SELECT * FROM bookings WHERE item_id=? AND status IN ('held','queued')",
                (item_id,),
            ).fetchone()
            if booking is None:
                raise DomainError("booking_not_found", "没有生效中的占座", 409)

            target_ref = new_slot_ref or booking["requested_slot_ref"]
            target_row = conn.execute(
                "SELECT * FROM directory_entries WHERE ref=?", (target_ref,)
            ).fetchone()
            if target_row is None:
                raise DomainError("directory_slot_not_found", "外部目录中没有该圈次，不能改约", 409)
            if target_row["status"] != "scheduled":
                raise DomainError("directory_slot_unavailable",
                                  "目录圈次状态为 %s，不能改约" % target_row["status"], 409)
            if target_row["satellite_id"] != booking["satellite_id"]:
                raise DomainError("satellite_mismatch", "改约圈次必须属于同一颗卫星", 409)
            target_slot = self._slot_from_row(target_row)
            if not (target_slot["start"] <= start and end <= target_slot["end"]):
                raise DomainError("window_outside_slot", "新窗口超出目录圈次范围", 409)

            conn.execute(
                "UPDATE bookings SET requested_slot_ref=?,slot_start=?,slot_end=?,directory_version=?,"
                "requested_window=?,assigned_window=?,updated_at=? WHERE id=?",
                (target_ref, target_row["window_start"], target_row["window_end"],
                 target_row["directory_version"], requested_window, requested_window,
                 now_iso(), booking["id"]),
            )
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET payload=?,version=?,updated_at=? WHERE id=?",
                (canonical_json(new_payload), version, now_iso(), item_id),
            )
            event_payload = {
                "requested_window": requested_window,
                "directory_ref": target_ref,
                "rebooked": target_ref != booking["requested_slot_ref"],
            }
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "modify_window", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, "modify_window", actor, role, event_payload)
            changes = self._replan(conn, quiet_item_ids={item_id})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def cancel_booking(self, item_id, actor, role, new_payload, reason, expected_version):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            self._check_version(row, expected_version)
            booking = conn.execute(
                "SELECT * FROM bookings WHERE item_id=? AND status IN ('held','queued')",
                (item_id,),
            ).fetchone()
            if booking is not None:
                conn.execute(
                    "UPDATE bookings SET status='voided',void_reason=?,updated_at=? WHERE id=?",
                    (reason, now_iso(), booking["id"]),
                )
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status='cancelled',payload=?,version=?,updated_at=? WHERE id=?",
                (canonical_json(new_payload), version, now_iso(), item_id),
            )
            event_payload = {"reason": reason, "booking_id": booking["id"] if booking else None}
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "cancel", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, "cancel", actor, role, event_payload)
            self._replan(conn)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def execute_booking(self, item_id, actor, role, new_payload, command_ref, expected_version):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            self._check_version(row, expected_version)
            if row["status"] != "coordinating":
                raise DomainError("invalid_state", "当前状态 %s 不允许执行" % row["status"])
            booking = conn.execute(
                "SELECT * FROM bookings WHERE item_id=? AND status='held'",
                (item_id,),
            ).fetchone()
            if booking is None:
                raise DomainError("booking_not_held", "占座未生效（可能仍在排队），不能执行", 409)
            slot_row = conn.execute(
                "SELECT * FROM directory_entries WHERE ref=?", (booking["slot_ref"],)
            ).fetchone()
            if slot_row is None:
                raise DomainError("directory_slot_not_found", "占用圈次在目录中已不存在", 409)
            if slot_row["status"] != "scheduled":
                raise DomainError("stale_directory", "外部目录圈次已 %s，请先对账" % slot_row["status"], 409)
            # 占用圈次若已改期（与重排时的占用快照不一致），执行前拦截。
            if (slot_row["source"] != "local"
                    and (booking["assigned_slot_start"] != slot_row["window_start"]
                         or booking["assigned_slot_end"] != slot_row["window_end"])):
                raise DomainError(
                    "stale_directory",
                    "占用圈次已改期，本地必须先对账并重排",
                    409,
                )
            timestamp = now_iso()
            conn.execute(
                "UPDATE bookings SET status='executed',executed_at=?,updated_at=? WHERE id=?",
                (timestamp, timestamp, booking["id"]),
            )
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status='executing',payload=?,version=?,updated_at=? WHERE id=?",
                (canonical_json(new_payload), version, now_iso(), item_id),
            )
            event_payload = {"command_ref": command_ref, "booking_id": booking["id"],
                             "directory_ref": booking["slot_ref"]}
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "execute", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, "execute", actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def reconcile(self, actor, role, only_item_id=None):
        """与外部指挥目录对账。

        多占少占（消失/改期/取消/版本变化）：
        - 未执行的批准立即失效，事件退回 assessed 重议；
        - 已执行的保留原记录，只登记差异。
        失效腾出的容量随后触发全局重排，排队中的占用自动补位。
        """
        conn = self.connect()
        report = {"scanned": 0, "voided": [], "retained_executed": [], "version_refreshed": []}
        try:
            conn.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT b.*, d.source AS dir_source, d.status AS dir_status, d.directory_version AS dir_version,"
                " d.window_start AS dir_start, d.window_end AS dir_end"
                " FROM bookings b LEFT JOIN directory_entries d ON d.ref = b.requested_slot_ref"
                " WHERE b.status IN ('held','queued')"
            )
            params = ()
            if only_item_id is not None:
                query += " AND b.item_id=?"
                params = (only_item_id,)
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                report["scanned"] += 1
                if row["dir_source"] != "external":
                    continue
                discrepancy = None
                if row["dir_status"] is None:
                    discrepancy = ("directory_entry_missing", "外部目录圈次已消失")
                elif row["dir_status"] != "scheduled":
                    discrepancy = ("directory_status_changed",
                                   "外部目录圈次状态变为 %s" % row["dir_status"])
                elif row["dir_start"] != row["slot_start"] or row["dir_end"] != row["slot_end"]:
                    discrepancy = ("directory_rescheduled", "外部目录圈次已改期")
                elif row["dir_version"] != row["directory_version"]:
                    # 版本变了但时段未变：直接续用，不失效。
                    conn.execute(
                        "UPDATE bookings SET directory_version=?,updated_at=? WHERE id=?",
                        (row["dir_version"], now_iso(), row["id"]),
                    )
                    report["version_refreshed"].append(
                        {"item_id": row["item_id"], "slot_ref": row["slot_ref"]}
                    )
                    continue
                if discrepancy is None:
                    continue

                code, message = discrepancy
                entry = {
                    "item_id": row["item_id"],
                    "slot_ref": row["requested_slot_ref"],
                    "assigned_slot_ref": row["slot_ref"],
                    "reason": code,
                    "message": message,
                }
                conn.execute(
                    "UPDATE bookings SET status='voided',void_reason=?,updated_at=? WHERE id=?",
                    (code, now_iso(), row["id"]),
                )
                item_row = conn.execute(
                    "SELECT * FROM items WHERE id=?", (row["item_id"],)
                ).fetchone()
                payload = json.loads(item_row["payload"])
                voided = payload.pop("approved_maneuver", None)
                payload["voided_maneuvers"] = payload.get("voided_maneuvers", [])
                payload["voided_maneuvers"].append({
                    "maneuver": voided,
                    "reason": code,
                    "message": message,
                    "voided_at": now_iso(),
                })
                conn.execute(
                    "UPDATE items SET status='assessed',payload=?,version=version+1,updated_at=? WHERE id=?",
                    (canonical_json(payload), now_iso(), row["item_id"]),
                )
                event_payload = dict(entry)
                event_payload["voided_maneuver"] = voided
                conn.execute(
                    "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                    (row["item_id"], "approval_voided", actor, role,
                     canonical_json(event_payload), now_iso()),
                )
                self.append_audit(conn, row["item_id"], "approval_voided", actor, role, event_payload)
                report["voided"].append(entry)

            # 已执行占用的差异只登记，记录保留。
            executed_query = (
                "SELECT b.*, d.status AS dir_status, d.window_start AS dir_start, d.window_end AS dir_end"
                " FROM bookings b LEFT JOIN directory_entries d ON d.ref = b.slot_ref"
                " WHERE b.status='executed'"
            )
            params = ()
            if only_item_id is not None:
                executed_query += " AND b.item_id=?"
                params = (only_item_id,)
            for row in conn.execute(executed_query, params).fetchall():
                code = None
                if row["dir_status"] is None:
                    code = "directory_entry_missing"
                elif row["dir_status"] == "cancelled":
                    code = "directory_status_changed"
                elif (row["dir_start"] != row["assigned_slot_start"]
                      or row["dir_end"] != row["assigned_slot_end"]):
                    code = "directory_rescheduled"
                if code is None:
                    continue
                item_row = conn.execute(
                    "SELECT * FROM items WHERE id=?", (row["item_id"],)
                ).fetchone()
                payload = json.loads(item_row["payload"])
                note = {"reason": code, "slot_ref": row["slot_ref"], "noted_at": now_iso(),
                        "policy": "executed_record_retained"}
                payload.setdefault("reconciliation_notes", []).append(note)
                conn.execute(
                    "UPDATE items SET payload=?,version=version+1,updated_at=? WHERE id=?",
                    (canonical_json(payload), now_iso(), row["item_id"]),
                )
                self.append_audit(
                    conn, row["item_id"], "reconcile_retained_executed", actor, role, note
                )
                report["retained_executed"].append(
                    {"item_id": row["item_id"], "slot_ref": row["slot_ref"], "reason": code}
                )

            replanned = self._replan(conn)
            report["replanned"] = [
                {"item_id": item_id, "status": change["status"]}
                for item_id, change in replanned.items()
            ]
            conn.execute("COMMIT")
            return report
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -------------------------------------------------------------- generic

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            self._check_version(row, expected_version)
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            slot_counts = {}
            for row in conn.execute(
                "SELECT slot_ref, COUNT(*) AS total FROM bookings "
                "WHERE status IN ('held','queued','executed') GROUP BY slot_ref"
            ).fetchall():
                slot_counts[row["slot_ref"]] = row["total"]
            return {"counts": counts, "slot_occupancy": slot_counts, "items": self.list_items()}
        finally:
            conn.close()
