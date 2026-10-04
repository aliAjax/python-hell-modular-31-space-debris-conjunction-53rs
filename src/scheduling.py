"""时段账纯规则：圈次容量、同卫星窗口重叠、排队顺延。

本模块不碰数据库，输入全部是普通 dict，时间字段统一为 aware datetime，
方便单独测试和复算。
"""

from datetime import datetime

from .domain import DomainError

ACTIVE_STATUSES = ("held", "queued")
SLOT_DEFAULT_CAPACITY = 1

REASON_SLOT_FULL = {
    "code": "slot_full",
    "message": "目标圈次容量不足，已排队顺延至后续圈次",
}
REASON_SATELLITE_OVERLAP = {
    "code": "satellite_window_overlap",
    "message": "同一颗卫星在该圈次存在重叠窗口，已排队顺延",
}
REASON_SLOT_TOO_SHORT = {
    "code": "slot_too_short",
    "message": "圈次时长不足以容纳规避窗口，已跳过",
}


def parse_ts(value, field="时间"):
    if not isinstance(value, str) or not value.strip():
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % field)
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % field)
    if moment.tzinfo is None:
        raise DomainError("invalid_timestamp", "%s 必须带时区" % field)
    return moment


def parse_window(payload):
    """接受 window_start/window_end，或 "start/end" 形式的 maneuver_window。"""
    if "window_start" in payload or "window_end" in payload:
        start_text = payload.get("window_start")
        end_text = payload.get("window_end")
    else:
        raw = payload.get("maneuver_window")
        if not isinstance(raw, str) or "/" not in raw:
            raise DomainError("invalid_window", "规避窗口必须是 start/end 的 ISO 时段")
        start_text, end_text = (part.strip() for part in raw.split("/", 1))
    start = parse_ts(start_text, "window_start")
    end = parse_ts(end_text, "window_end")
    if end <= start:
        raise DomainError("invalid_window", "规避窗口结束时间必须晚于开始时间")
    return start, end


def format_window(start, end):
    return "%s/%s" % (start.isoformat(), end.isoformat())


def overlaps(start_a, end_a, start_b, end_b):
    """端点相接（end == start）不算重叠。"""
    return start_a < end_b and start_b < end_a


def _place_window(booking, requested_slot, slot):
    """顺延到候选圈次时保持窗口在申请圈次中的相对偏移。

    这样原本互不重叠的多个窗口平移到下一圈次后仍然互不重叠（同一卫星
    同一圈次可容纳多个非重叠窗口）；偏移后超出圈次范围才贴齐起点。
    """
    duration = booking["req_end"] - booking["req_start"]
    if (slot["end"] - slot["start"]) < duration:
        return None
    if slot["ref"] == requested_slot["ref"]:
        return booking["req_start"], booking["req_end"]
    offset = booking["req_start"] - requested_slot["start"]
    start = slot["start"] + offset
    end = start + duration
    if start < slot["start"] or end > slot["end"]:
        start = slot["start"]
        end = start + duration
    return start, end


def _find_slot(slots_for_satellite, start, end):
    """找到能容纳申请窗口的圈次（窗口必须完全落在圈次内）。"""
    for slot in slots_for_satellite:
        if slot["start"] <= start and end <= slot["end"]:
            return slot
    return None


def replan(slots, active_bookings, executed_bookings=()):
    """按申请时间贪心分配圈次。

    - slots: 全部圈次（含本地兜底圈次）
    - active_bookings: 状态为 held/queued 的占用
    - executed_bookings: 已执行占用，历史记录不动，但仍占用容量用于计算

    同一圈次按容量计数；同一颗卫星再按窗口逐对判重叠。
    申请圈次不可用就沿该卫星后续圈次顺延，并记录顺延原因。
    返回 {booking_id: assignment}。
    """
    by_satellite = {}
    for slot in slots:
        if slot.get("status", "scheduled") != "scheduled":
            continue
        by_satellite.setdefault(slot["satellite_id"], []).append(slot)
    for chain in by_satellite.values():
        chain.sort(key=lambda item: (item["start"], item["end"]))

    # slot_usage[slot_ref] = {"count": int, "windows": [(start, end), ...]}
    # 容量按圈次计数；同卫星窗口重叠也按圈次逐对判断，允许同一圈次容纳
    # 多个彼此不重叠、且不超过容量的窗口。
    slot_usage = {}

    def seed(booking):
        usage = slot_usage.setdefault(booking["slot_ref"], {"count": 0, "windows": []})
        usage["count"] += 1
        usage["windows"].append((booking["assigned_start"], booking["assigned_end"]))

    for booking in executed_bookings:
        seed(booking)

    assignments = {}
    # 先批先占：按批准到达顺序（booking id）分配，而不是申请窗口时间。
    # 这样更早的 held 决定永远不会被后到的批准挤掉，避免后提交事务
    # 改写先提交事务已返回的占座结果（丢失更新）。
    ordered = sorted(active_bookings, key=lambda item: item["id"])
    for booking in ordered:
        chain = by_satellite.get(booking["satellite_id"], [])
        requested_slot = _find_slot(chain, booking["req_start"], booking["req_end"])
        if requested_slot is None:
            raise DomainError(
                "no_matching_slot",
                "外部目录中没有容纳该规避窗口的圈次，无法占座",
                409,
            )
        candidates = [slot for slot in chain if slot["start"] >= requested_slot["start"]]

        reasons = []
        assignment = None
        for slot in candidates:
            placed = _place_window(booking, requested_slot, slot)
            if placed is None:
                reasons.append(REASON_SLOT_TOO_SHORT)
                continue
            start, end = placed
            usage = slot_usage.get(slot["ref"], {"count": 0, "windows": []})
            if usage["count"] >= int(slot["capacity"]):
                reasons.append(REASON_SLOT_FULL)
                continue
            blocked_by_window = any(
                overlaps(start, end, used_start, used_end)
                for used_start, used_end in usage["windows"]
            )
            if blocked_by_window:
                reasons.append(REASON_SATELLITE_OVERLAP)
                continue
            assignment = (slot, start, end)
            break

        if assignment is None:
            raise DomainError(
                "no_available_slot",
                "目标圈次容量不足，且后续没有可顺延的圈次",
                409,
            )

        slot, start, end = assignment
        usage = slot_usage.setdefault(slot["ref"], {"count": 0, "windows": []})
        usage["count"] += 1
        usage["windows"].append((start, end))

        held = slot["ref"] == requested_slot["ref"] and start == booking["req_start"]
        deduped = []
        seen = set()
        for reason in reasons:
            if reason["code"] not in seen:
                seen.add(reason["code"])
                deduped.append(reason)
        assignments[booking["id"]] = {
            "slot_ref": slot["ref"],
            "assigned_start": start,
            "assigned_end": end,
            "status": "held" if held else "queued",
            "reasons": [] if held else deduped,
        }
    return assignments
