from . import domain, ledger, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        ledger_op = self._ledger_operation(action, item, payload)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version, ledger_op
        )
        return self.get_item(item_id)

    def _ledger_operation(self, action, item, payload):
        if action == "approve":
            satellite_id = payload.get("satellite_id") or item["payload"].get("primary_object_id")
            req_start, req_end = ledger.parse_window(payload["maneuver_window"])

            def allocate(conn):
                ledger.allocate_window(conn, item["id"], satellite_id, req_start, req_end)

            return allocate
        if action == "execute":
            return lambda conn: ledger.mark_windows_executed(conn, item["id"])
        if action == "cancel":
            return lambda conn: ledger.release_windows(conn, item["id"])
        return None

    def sync_catalog(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CATALOG_ROLES:
            raise DomainError("forbidden", "当前角色不能同步外部指挥目录", 403)
        satellite_id = domain.require_text(payload, "satellite_id")
        revolution_no = domain.positive_integer(payload, "revolution_no")
        start_ts = domain.parse_timestamp(payload, "start_ts")
        end_ts = domain.parse_timestamp(payload, "end_ts")
        if ledger.parse_dt(end_ts) <= ledger.parse_dt(start_ts):
            raise DomainError("invalid_window", "圈次结束时间必须晚于开始时间")
        status = payload.get("status", "planned")
        if status not in {"planned", "executed", "cancelled"}:
            raise DomainError("invalid_status", "目录状态必须是 planned、executed 或 cancelled")
        maneuver_ref = payload.get("maneuver_ref")
        if maneuver_ref is not None:
            maneuver_ref = str(maneuver_ref).strip() or None
        return self.repository.sync_catalog(satellite_id, revolution_no, start_ts, end_ts, maneuver_ref, status)

    def list_catalog(self):
        return self.repository.list_catalog()

    def list_windows(self, satellite_id=None):
        return self.repository.list_windows(satellite_id=satellite_id)

    def reschedule_window(self, window_id, payload, actor, role, expected_version=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.WINDOW_ROLES:
            raise DomainError("forbidden", "当前角色不能修改规避窗口", 403)
        if expected_version is None:
            raise DomainError("expected_version_required", "修改窗口需要 expected_version", 400)
        start_ts = domain.parse_timestamp(payload, "window_start")
        end_ts = domain.parse_timestamp(payload, "window_end")
        if ledger.parse_dt(end_ts) <= ledger.parse_dt(start_ts):
            raise DomainError("invalid_window", "窗口结束时间必须晚于开始时间")
        return self.repository.reschedule_window(window_id, start_ts, end_ts, expected_version)

    def reconcile(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.RECONCILE_ROLES:
            raise DomainError("forbidden", "当前角色不能执行对账", 403)
        return self.repository.reconcile()

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
