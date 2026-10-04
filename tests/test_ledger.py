import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def make_item_payload(primary="SAT-1", secondary="DEB-1", tca="2026-10-10T12:00:00+00:00"):
    return {
        "primary_object_id": primary,
        "secondary_object_id": secondary,
        "tca": tca,
        "miss_distance_m": 50,
        "covariance_m": 100,
        "fuel_budget_m_s": 5,
        "track_age_hours": 1,
        "operating_organizations": ["Org-A"],
    }


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _assessed_item(self, primary="SAT-1", secondary="DEB-1", tca="2026-10-10T12:00:00+00:00"):
        item = self.service.create_item(make_item_payload(primary, secondary, tca), "a", "analyst")
        return self.service.act(item["id"], "assess", {"hours_to_tca": 2}, "a", "analyst", item["version"])

    def _approve(self, item, window, directory_ref, actor="c-1"):
        start, end = window
        return self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2,
            "window_start": start,
            "window_end": end,
            "directory_ref": directory_ref,
        }, actor, "coordinator", item["version"])

    def _sync_slot(self, ref, satellite, start, end, capacity=1, version="v1"):
        return self.service.sync_directory({
            "directory_ref": ref,
            "satellite_id": satellite,
            "window_start": start,
            "window_end": end,
            "capacity": capacity,
            "directory_version": version,
        }, "a", "analyst")

    def test_capacity_full_queues_and_reasons_then_promotes_on_release(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T10:00:00+00:00", capacity=1)
        self._sync_slot("PASS-B", "SAT-1",
                        "2026-10-10T10:00:00+00:00", "2026-10-10T14:00:00+00:00", capacity=1)

        first = self._assessed_item(secondary="DEB-1")
        first = self._approve(first, ("2026-10-10T07:00:00+00:00", "2026-10-10T07:30:00+00:00"), "PASS-A")
        self.assertEqual(first["status"], "coordinating")

        second = self._assessed_item(secondary="DEB-2")
        second = self._approve(second, ("2026-10-10T08:00:00+00:00", "2026-10-10T08:30:00+00:00"), "PASS-A")
        self.assertEqual(second["status"], "queued")
        maneuver = second["payload"]["approved_maneuver"]
        self.assertEqual(maneuver["directory_ref"], "PASS-B")
        self.assertTrue(
            maneuver["maneuver_window"].startswith("2026-10-10T12:00:00"),
            "排队窗口应保持相对偏移顺延到下一圈次，实际：%s" % maneuver["maneuver_window"],
        )
        self.assertIn("slot_full", [r["code"] for r in maneuver["delay_reasons"]])

        # 前序取消 → 重排 → 排队者自动补位回原圈次
        self.service.act(first["id"], "cancel", {"reason": "no longer needed"},
                         "c-1", "coordinator", first["version"])
        promoted = self.service.get_item(second["id"])
        self.assertEqual(promoted["status"], "coordinating")
        self.assertEqual(promoted["payload"]["approved_maneuver"]["directory_ref"], "PASS-A")

    def test_same_satellite_overlapping_windows_queue(self):
        # 容量足够大，但同一颗卫星窗口不能重叠
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T12:00:00+00:00", capacity=5)
        self._sync_slot("PASS-B", "SAT-1",
                        "2026-10-10T12:00:00+00:00", "2026-10-10T18:00:00+00:00", capacity=5)

        first = self._assessed_item(secondary="DEB-1")
        first = self._approve(first, ("2026-10-10T08:00:00+00:00", "2026-10-10T09:00:00+00:00"), "PASS-A")
        second = self._assessed_item(secondary="DEB-2")
        second = self._approve(second, ("2026-10-10T08:30:00+00:00", "2026-10-10T09:00:00+00:00"), "PASS-A")

        self.assertEqual(second["status"], "queued")
        self.assertEqual(second["payload"]["approved_maneuver"]["directory_ref"], "PASS-B")
        self.assertIn(
            "satellite_window_overlap",
            [r["code"] for r in second["payload"]["approved_maneuver"]["delay_reasons"]],
        )

        # 不同卫星可以同圈次并行
        other = self._assessed_item(primary="SAT-2", secondary="DEB-9")
        self._sync_slot("PASS-C", "SAT-2",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T12:00:00+00:00", capacity=5)
        other = self._approve(other, ("2026-10-10T08:30:00+00:00", "2026-10-10T09:00:00+00:00"),
                              "PASS-C")
        self.assertEqual(other["status"], "coordinating")

    def test_directory_reschedule_voids_unexecuted_but_keeps_executed(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T10:00:00+00:00")
        self._sync_slot("PASS-B", "SAT-1",
                        "2026-10-10T10:00:00+00:00", "2026-10-10T14:00:00+00:00")

        executed_item = self._assessed_item(secondary="DEB-1")
        executed_item = self._approve(
            executed_item, ("2026-10-10T07:00:00+00:00", "2026-10-10T07:30:00+00:00"), "PASS-A")
        executed_item = self.service.act(
            executed_item["id"], "execute", {"command_ref": "CMD-1"},
            "op-1", "operator", executed_item["version"])

        queued_item = self._assessed_item(secondary="DEB-2")
        queued_item = self._approve(
            queued_item, ("2026-10-10T08:00:00+00:00", "2026-10-10T08:30:00+00:00"), "PASS-A")
        self.assertEqual(queued_item["status"], "queued")

        # 外部目录改期：第二个事件的占用（在 PASS-A 上排队）未执行 → 立即失效
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T05:00:00+00:00", "2026-10-10T06:30:00+00:00", version="v2")

        report = self.service.reconcile("c-1", "coordinator")
        self.assertEqual(
            [entry["item_id"] for entry in report["voided"]], [queued_item["id"]]
        )

        voided = self.service.get_item(queued_item["id"])
        self.assertEqual(voided["status"], "assessed", "未执行的批准应立即失效并退回重议")
        self.assertNotIn("approved_maneuver", voided["payload"])
        self.assertTrue(voided["payload"]["voided_maneuvers"])
        self.assertEqual(voided["payload"]["voided_maneuvers"][0]["reason"], "directory_rescheduled")
        event_types = [event["event_type"] for event in voided["audit"]]
        self.assertIn("approval_voided", event_types)

        retained = self.service.get_item(executed_item["id"])
        self.assertEqual(retained["status"], "executing", "已执行的保留原记录")
        self.assertEqual(retained["payload"]["approved_maneuver"]["directory_ref"], "PASS-A")
        self.assertTrue(retained["payload"]["reconciliation_notes"])

    def test_concurrent_window_modification_first_write_wins(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T18:00:00+00:00", capacity=5)
        item = self._assessed_item()
        item = self._approve(item, ("2026-10-10T08:00:00+00:00", "2026-10-10T09:00:00+00:00"),
                             "PASS-A")
        stale_version = item["version"]

        first = self.service.act(item["id"], "modify_window", {
            "window_start": "2026-10-10T10:00:00+00:00",
            "window_end": "2026-10-10T10:30:00+00:00",
        }, "c-1", "coordinator", stale_version)
        self.assertEqual(first["version"], stale_version + 1)

        with self.assertRaises(ConflictError) as context:
            self.service.act(item["id"], "modify_window", {
                "window_start": "2026-10-10T11:00:00+00:00",
                "window_end": "2026-10-10T11:30:00+00:00",
            }, "c-2", "coordinator", stale_version)
        self.assertEqual(context.exception.code, "version_conflict")

        # 另一方重新读取后再改 → 生效
        latest = self.service.get_item(item["id"])
        retried = self.service.act(item["id"], "modify_window", {
            "window_start": "2026-10-10T11:00:00+00:00",
            "window_end": "2026-10-10T11:30:00+00:00",
        }, "c-2", "coordinator", latest["version"])
        self.assertTrue(
            retried["payload"]["approved_maneuver"]["requested_window"]
            .startswith("2026-10-10T11:00:00")
        )

    def test_execute_rejected_after_directory_reschedule(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T10:00:00+00:00")
        item = self._assessed_item()
        item = self._approve(item, ("2026-10-10T07:00:00+00:00", "2026-10-10T07:30:00+00:00"),
                             "PASS-A")
        # 外部目录改期，本地未对账：执行必须被拦截
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:30:00+00:00", "2026-10-10T10:30:00+00:00", version="v2")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "execute", {"command_ref": "CMD-2"},
                             "op-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "stale_directory")

    def test_execute_allowed_after_version_only_change_and_reconcile(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T10:00:00+00:00")
        item = self._assessed_item()
        item = self._approve(item, ("2026-10-10T07:00:00+00:00", "2026-10-10T07:30:00+00:00"),
                             "PASS-A")
        # 仅版本号变化、时段不变：对账续用，不失效
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T10:00:00+00:00", version="v2")
        report = self.service.reconcile("c-1", "coordinator")
        self.assertEqual(report["voided"], [])
        self.assertEqual(report["version_refreshed"][0]["item_id"], item["id"])
        latest = self.service.get_item(item["id"])
        executed = self.service.act(item["id"], "execute", {"command_ref": "CMD-9"},
                                    "op-1", "operator", latest["version"])
        self.assertEqual(executed["status"], "executing")

    def test_approve_rejects_window_outside_slot(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T07:00:00+00:00")
        item = self._assessed_item()
        with self.assertRaises(DomainError) as context:
            self._approve(item, ("2026-10-10T06:30:00+00:00", "2026-10-10T08:00:00+00:00"),
                          "PASS-A")
        self.assertEqual(context.exception.code, "window_outside_slot")

    def test_modify_window_can_rebook_to_another_slot_and_triggers_replan(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T10:00:00+00:00")
        self._sync_slot("PASS-B", "SAT-1",
                        "2026-10-10T10:00:00+00:00", "2026-10-10T14:00:00+00:00")
        item = self._assessed_item()
        item = self._approve(item, ("2026-10-10T07:00:00+00:00", "2026-10-10T07:30:00+00:00"),
                             "PASS-A")
        item = self.service.act(item["id"], "modify_window", {
            "window_start": "2026-10-10T11:00:00+00:00",
            "window_end": "2026-10-10T11:30:00+00:00",
            "directory_ref": "PASS-B",
        }, "c-2", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["payload"]["approved_maneuver"]["directory_ref"], "PASS-B")
        self.assertTrue(
            item["payload"]["approved_maneuver"]["requested_window"]
            .startswith("2026-10-10T11:00:00")
        )

    def test_concurrent_approvals_same_slot_serialized(self):
        import threading

        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T12:00:00+00:00", capacity=1)
        self._sync_slot("PASS-B", "SAT-1",
                        "2026-10-10T12:00:00+00:00", "2026-10-10T18:00:00+00:00", capacity=1)
        first = self._assessed_item(secondary="DEB-1")
        second = self._assessed_item(secondary="DEB-2")
        results = {}

        def approve(key, item):
            try:
                results[key] = self._approve(item, ("2026-10-10T08:00:00+00:00",
                                                     "2026-10-10T09:00:00+00:00"), "PASS-A")
            except DomainError as exc:
                results[key] = exc

        threads = [
            threading.Thread(target=approve, args=("a", first)),
            threading.Thread(target=approve, args=("b", second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = sorted(
            value["status"] if isinstance(value, dict) else "error"
            for value in results.values()
        )
        # 容量 1：无论谁的 BEGIN IMMEDIATE 先拿到写锁，结果都是一个占住、一个排队
        self.assertEqual(statuses, ["coordinating", "queued"])

    def test_queued_item_cannot_execute_until_promoted(self):
        self._sync_slot("PASS-A", "SAT-1",
                        "2026-10-10T06:00:00+00:00", "2026-10-10T10:00:00+00:00")
        self._sync_slot("PASS-B", "SAT-1",
                        "2026-10-10T10:00:00+00:00", "2026-10-10T14:00:00+00:00")
        first = self._assessed_item(secondary="DEB-1")
        first = self._approve(first, ("2026-10-10T07:00:00+00:00", "2026-10-10T08:30:00+00:00"),
                              "PASS-A")
        second = self._assessed_item(secondary="DEB-2")
        second = self._approve(second, ("2026-10-10T08:00:00+00:00", "2026-10-10T08:30:00+00:00"),
                               "PASS-A")
        self.assertEqual(second["status"], "queued")
        with self.assertRaises(DomainError) as context:
            self.service.act(second["id"], "execute", {"command_ref": "CMD-3"},
                             "op-1", "operator", second["version"])
        self.assertEqual(context.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
