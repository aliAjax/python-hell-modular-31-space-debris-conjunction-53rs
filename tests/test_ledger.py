import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self._seq = 0

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _item(self, satellite="SAT-1", tca=None):
        self._seq += 1
        if tca is None:
            tca = "2026-11-%02dT12:00:00+00:00" % (10 + self._seq)
        return self.service.create_item({
            "primary_object_id": satellite,
            "secondary_object_id": "DEB-%d" % self._seq,
            "tca": tca,
            "miss_distance_m": 120,
            "covariance_m": 100,
            "fuel_budget_m_s": 50,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A"],
        }, "analyst-1", "analyst")

    def _approve(self, item, window, fuel=2.5):
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        return self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": fuel, "maneuver_window": window,
        }, "coordinator-1", "coordinator", item["version"])

    def _catalog(self, satellite, revolution, status="planned"):
        return self.service.sync_catalog({
            "satellite_id": satellite,
            "revolution_no": revolution,
            "start_ts": "2026-11-10T07:30:00Z",
            "end_ts": "2026-11-10T09:00:00Z",
            "maneuver_ref": "EXT-%d" % revolution,
            "status": status,
        }, "coordinator-1", "coordinator")

    def test_approve_occupies_slot(self):
        item = self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        windows = self.service.list_windows(satellite_id="SAT-1")
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["status"], "approved")
        self.assertIsNone(windows[0]["queue_reason"])
        self.assertEqual(item["status"], "coordinating")

    def test_capacity_full_postpones_with_reason(self):
        first = self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        second = self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        windows = self.service.list_windows(satellite_id="SAT-1")
        by_item = {w["item_id"]: w for w in windows}
        self.assertIsNone(by_item[first["id"]]["queue_reason"])
        self.assertIsNotNone(by_item[second["id"]]["queue_reason"])
        self.assertIn("顺延", by_item[second["id"]]["queue_reason"])
        self.assertNotEqual(by_item[first["id"]]["slot_rev"], by_item[second["id"]]["slot_rev"])

    def test_same_satellite_windows_do_not_overlap(self):
        self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        second = self._approve(self._item(), "2026-11-10T08:30:00Z/2026-11-10T09:30:00Z")
        windows = self.service.list_windows(satellite_id="SAT-1")
        placed = [w for w in windows if w["item_id"] == second["id"]][0]
        # 申请窗口与已占窗口时间重叠，必须顺延到不重叠的圈次
        self.assertIsNotNone(placed["queue_reason"])
        self.assertIn("重叠", placed["queue_reason"])
        for w in windows:
            if w["item_id"] == second["id"]:
                continue
            self.assertFalse(
                placed["window_start"] < w["window_end"] and placed["window_end"] > w["window_start"],
                "同卫星窗口不能重叠",
            )

    def test_catalog_ownership_and_slot_source(self):
        from src import ledger
        rev = ledger.revolution_for("2026-11-10T08:00:00+00:00")
        self._catalog("SAT-1", rev)
        conn = self.repo.connect()
        try:
            slot = conn.execute("SELECT * FROM slots WHERE satellite_id='SAT-1' AND revolution_no=%d" % rev).fetchone()
        finally:
            conn.close()
        self.assertEqual(slot["source"], "catalog")
        # 目录覆盖该卫星后，批准只占目录圈次
        self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        window = self.service.list_windows(satellite_id="SAT-1")[0]
        self.assertEqual(window["slot_rev"], rev)

    def test_reconcile_over_invalidates_unexecuted_and_keeps_executed(self):
        unexecuted = self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        executed = self._approve(self._item(), "2026-11-11T08:00:00Z/2026-11-11T09:00:00Z")
        executed = self.service.act(executed["id"], "execute", {"command_ref": "CMD-1"}, "operator-1", "operator", executed["version"])
        result = self.service.reconcile("coordinator-1", "coordinator")
        self.assertEqual(len(result["invalidated"]), 1)
        self.assertEqual(len(result["kept"]), 1)
        refreshed = self.service.get_item(unexecuted["id"])
        self.assertEqual(refreshed["status"], "reopened")
        self.assertIsNotNone(refreshed["payload"].get("reopen_reason"))
        self.assertEqual(self.service.get_item(executed["id"])["status"], "executing")

    def test_reconcile_under_reports_missing_occupation(self):
        self._catalog("SAT-1", 4321)
        result = self.service.reconcile("coordinator-1", "coordinator")
        self.assertTrue(any(d["type"] == "under" and d["revolution_no"] == 4321 for d in result["discrepancies"]))

    def test_reapprove_after_reopen_follows_catalog(self):
        self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        local_rev = self.service.list_windows(satellite_id="SAT-1")[0]["slot_rev"]
        self._catalog("SAT-1", local_rev + 5)
        item = self.service.list_items()[0]
        item = self.service.reconcile("coordinator-1", "coordinator") and self.service.get_item(item["id"])
        self.assertEqual(item["status"], "reopened")
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5, "maneuver_window": "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z",
        }, "coordinator-1", "coordinator", item["version"])
        active = [w for w in self.service.list_windows(satellite_id="SAT-1") if w["status"] == "approved"]
        self.assertEqual(active[0]["slot_rev"], local_rev + 5)

    def test_reschedule_first_write_wins_then_conflict(self):
        self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        window = self.service.list_windows(satellite_id="SAT-1")[0]
        updated = self.service.reschedule_window(
            window["id"],
            {"window_start": "2026-12-01T08:00:00Z", "window_end": "2026-12-01T09:00:00Z"},
            "coordinator-1", "coordinator", window["version"],
        )
        self.assertEqual(updated["version"], window["version"] + 1)
        with self.assertRaises(ConflictError) as context:
            self.service.reschedule_window(
                window["id"],
                {"window_start": "2026-12-02T08:00:00Z", "window_end": "2026-12-02T09:00:00Z"},
                "coordinator-1", "coordinator", window["version"],
            )
        self.assertEqual(context.exception.code, "version_conflict")

    def test_cancel_releases_window(self):
        item = self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        self.service.act(item["id"], "cancel", {"reason": "mission aborted"}, "coordinator-1", "coordinator", item["version"])
        windows = self.service.list_windows(satellite_id="SAT-1")
        self.assertEqual(windows[0]["status"], "invalidated")

    def test_catalog_requires_coordinator(self):
        with self.assertRaises(DomainError) as context:
            self.service.sync_catalog({
                "satellite_id": "SAT-1", "revolution_no": 1,
                "start_ts": "2026-11-10T07:30:00Z", "end_ts": "2026-11-10T09:00:00Z",
            }, "analyst-1", "analyst")
        self.assertEqual(context.exception.status, 403)

    def test_reschedule_requires_expected_version(self):
        self._approve(self._item(), "2026-11-10T08:00:00Z/2026-11-10T09:00:00Z")
        window = self.service.list_windows(satellite_id="SAT-1")[0]
        with self.assertRaises(DomainError) as context:
            self.service.reschedule_window(
                window["id"],
                {"window_start": "2026-12-01T08:00:00Z", "window_end": "2026-12-01T09:00:00Z"},
                "coordinator-1", "coordinator", None,
            )
        self.assertEqual(context.exception.code, "expected_version_required")


if __name__ == "__main__":
    unittest.main()
