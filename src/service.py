from . import domain, rules, scheduling
from .domain import DomainError


DIRECTORY_ROLES = {"analyst", "coordinator"}
RECONCILE_ROLES = {"coordinator"}


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

    def _check_region(self, item, action, region, role):
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        item = self.repository.get_item(item_id)
        self._check_region(item, action, region, role)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        if action == "approve":
            return self._approve(item, payload, actor, role, expected_version)
        if action == "modify_window":
            return self._modify_window(item, payload, actor, role, expected_version)
        if action == "execute":
            return self._execute(item, payload, actor, role, expected_version)
        if action == "cancel":
            return self._cancel(item, payload, actor, role, expected_version)

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def _approve(self, item, payload, actor, role, expected_version):
        current = item["payload"]
        # 意见冲突、燃料校验在占座事务前完成，给出最相关的错误。
        fuel = rules.validate_approve(current, payload)
        window_start, window_end = scheduling.parse_window(payload)
        directory_ref = payload.get("directory_ref")
        if directory_ref is not None:
            directory_ref = str(directory_ref).strip() or None
        return self.repository.approve_booking(
            item["id"], actor, role, current, fuel, window_start, window_end,
            directory_ref, expected_version,
        )

    def _modify_window(self, item, payload, actor, role, expected_version):
        new_status, new_payload, event_payload = rules.apply_action(
            item, "modify_window", payload, actor, role
        )
        start, end = scheduling.parse_window(payload)
        requested_window = scheduling.format_window(start, end)
        new_slot_ref = payload.get("directory_ref")
        if new_slot_ref is not None:
            new_slot_ref = str(new_slot_ref).strip() or None
        return self.repository.modify_booking_window(
            item["id"], actor, role, new_payload, requested_window,
            start, end, new_slot_ref, expected_version,
        )

    def _execute(self, item, payload, actor, role, expected_version):
        new_status, new_payload, event_payload = rules.apply_action(
            item, "execute", payload, actor, role
        )
        command_ref = event_payload["command_ref"]
        return self.repository.execute_booking(
            item["id"], actor, role, new_payload, command_ref, expected_version
        )

    def _cancel(self, item, payload, actor, role, expected_version):
        new_status, new_payload, event_payload = rules.apply_action(
            item, "cancel", payload, actor, role
        )
        return self.repository.cancel_booking(
            item["id"], actor, role, new_payload, event_payload["reason"], expected_version
        )

    def sync_directory(self, payload, actor, role):
        """同步外部指挥目录的圈次（归属看外部目录）。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in DIRECTORY_ROLES:
            raise DomainError("forbidden", "当前角色不能同步外部指挥目录", 403)
        entry = domain.normalize_directory_entry(payload)
        return self.repository.upsert_directory_entry(entry, actor, role)

    def list_directory(self):
        return self.repository.list_directory()

    def reconcile(self, actor, role, item_id=None):
        """与外部目录对账：未执行的多占/少占失效重议，已执行的留痕保留。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in RECONCILE_ROLES:
            raise DomainError("forbidden", "当前角色不能执行对账", 403)
        return self.repository.reconcile(actor, role, item_id)

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
