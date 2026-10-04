from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def positive_integer(payload, name):
    value = payload.get(name, 0)
    if isinstance(value, bool):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_integer", "%s 必须是整数" % name)
    if value < 0:
        raise DomainError("invalid_integer", "%s 不能为负数" % name)
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    primary = require_text(payload, "primary_object_id")
    secondary = require_text(payload, "secondary_object_id")
    if primary == secondary:
        raise DomainError("same_object", "接近事件的两个物体不能相同")
    tca = parse_timestamp(payload, "tca")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    fuel_budget = number(payload, "fuel_budget_m_s", 0)
    track_age = number(payload, "track_age_hours", 0)
    operators = payload.get("operating_organizations", [])
    if not isinstance(operators, list) or any(not isinstance(item, str) or not item.strip() for item in operators):
        raise DomainError("invalid_operators", "运营方必须是字符串列表")
    stable_key = "%s|%s|%s" % tuple(sorted([primary, secondary]) + [tca])
    return {
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "fuel_budget_m_s": fuel_budget,
        "track_age_hours": track_age,
        "operating_organizations": [item.strip() for item in operators],
        "revisions": [],
        "opinions": [],
        "conflict": False,
        "_stable_key": stable_key,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    distance = number(payload, "miss_distance_m", 0)
    covariance = number(payload, "covariance_m", 0)
    if covariance <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "miss_distance_m": distance,
        "covariance_m": covariance,
        "region": region,
        "operator": payload.get("operator"),
    }


def normalize_directory_entry(payload):
    """外部指挥目录圈次：归属看目录，容量和时段也由目录给定。"""
    ref = require_text(payload, "directory_ref")
    satellite_id = require_text(payload, "satellite_id")
    start_text = require_text(payload, "window_start")
    end_text = require_text(payload, "window_end")
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "目录圈次时间必须是 ISO 时间")
    if start.tzinfo is None or end.tzinfo is None:
        raise DomainError("invalid_timestamp", "目录圈次时间必须带时区")
    if end <= start:
        raise DomainError("invalid_window", "圈次结束时间必须晚于开始时间")
    capacity = int(positive_integer(payload, "capacity"))
    if capacity <= 0:
        raise DomainError("invalid_capacity", "圈次容量必须大于零")
    status = str(payload.get("status", "scheduled")).strip() or "scheduled"
    if status not in {"scheduled", "cancelled", "completed"}:
        raise DomainError("invalid_directory_status", "目录圈次状态非法")
    directory_version = payload.get("directory_version")
    if directory_version is not None:
        directory_version = str(directory_version).strip() or None
    note = payload.get("note")
    if note is not None:
        note = str(note).strip() or None
    return {
        "ref": ref,
        "satellite_id": satellite_id,
        "start": start,
        "end": end,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "capacity": capacity,
        "status": status,
        "directory_version": directory_version,
        "note": note,
        "source": "external",
    }
