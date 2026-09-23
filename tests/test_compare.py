"""方案对比模块测试: python -m unittest discover -s tests"""
import copy
import json
import unittest

from app.balancer import run_plan
from app.compare import compare_records


def base_payload(**over):
    p = {
        "slot_count": 12,
        "slots": [{"index": i, "angle": i * 30, "enabled": True} for i in range(12)],
        "radius_mm": 100.0,
        "rpm": 4000,
        "w1": 1.0,
        "w2": 0.000001,
        "move_weight": 0.0,
        "restarts": 4,
        "chain_steps": 600,
        "seed": 20260923,
    }
    p.update(over)
    return p


def record(pid, payload, result=None):
    if result is None:
        result = run_plan(payload)
    return {
        "id": pid,
        "name": f"方案 {pid}",
        "created_at": "2026-09-23T00:00:00+00:00",
        "parent_id": None,
        "request": copy.deepcopy(payload),
        "result": result,
    }


def metric_cmp(cmp, key):
    return next(m for m in cmp["metrics"] if m["key"] == key)


class TestCompare(unittest.TestCase):
    def six_payload(self, **over):
        p = base_payload(**over)
        p["tubes"] = [
            {"id": 1, "mass": 50}, {"id": 2, "mass": 50},
            {"id": 3, "mass": 51}, {"id": 4, "mass": 49},
            {"id": 5, "mass": 30}, {"id": 6, "mass": 30},
        ]
        return p

    def test_identical_plans_are_unchanged_same_conditions(self):
        p = self.six_payload()
        cmp = compare_records(record(1, p), record(2, copy.deepcopy(p)))
        self.assertTrue(cmp["same_conditions"])
        self.assertEqual(cmp["counts"]["unchanged"], 6)
        self.assertEqual(cmp["counts"]["total"], 6)
        self.assertTrue(all(e["differs"] is False for e in cmp["param_diffs"]))
        self.assertEqual(cmp["warnings"], [])
        self.assertAlmostEqual(metric_cmp(cmp, "u1_mag_g_mm")["delta"], 0.0)
        self.assertIn("同条件", cmp["interpretation"])

    def test_added_and_removed_tubes(self):
        pa = self.six_payload()
        pb = copy.deepcopy(pa)
        pb["tubes"] = [t for t in pb["tubes"] if t["id"] != 2]
        pb["tubes"].append({"id": 7, "mass": 20})
        cmp = compare_records(record(1, pa), record(2, pb))
        rows = {r["tube_id"]: r for r in cmp["tube_changes"]}
        self.assertEqual(rows[7]["status"], "added")
        self.assertIsNone(rows[7]["slot_a"])
        self.assertIsNotNone(rows[7]["slot_b"])
        self.assertEqual(rows[2]["status"], "removed")
        self.assertIsNone(rows[2]["slot_b"])
        self.assertEqual(cmp["counts"]["added"], 1)
        self.assertEqual(cmp["counts"]["removed"], 1)
        self.assertFalse(cmp["condition_flags"]["same_tubes"])
        self.assertFalse(cmp["same_conditions"])
        self.assertTrue(any("试管清单不同" in w for w in cmp["warnings"]))

    def test_moved_tube_detected(self):
        # 增量重排: 去掉一支试管, 其原搭档可能换槽
        pa = self.six_payload()
        ra = run_plan(pa)
        pb = copy.deepcopy(pa)
        pb["tubes"] = [t for t in pb["tubes"] if t["id"] != 6]
        pb["previous"] = [
            {"tube_id": x["tube_id"], "slot": x["slot"]}
            for x in ra["placements"]
        ]
        rb = run_plan(pb)
        cmp = compare_records(record(1, pa, ra), record(2, pb, rb))
        statuses = {r["tube_id"]: r["status"] for r in cmp["tube_changes"]}
        self.assertEqual(statuses[6], "removed")
        for tid in range(1, 6):
            self.assertIn(statuses[tid], ("moved", "unchanged"))
        # 每个非增删试管的 a/b 槽位与状态自洽
        for r in cmp["tube_changes"]:
            if r["status"] == "moved":
                self.assertNotEqual(r["slot_a"], r["slot_b"])
            elif r["status"] == "unchanged":
                self.assertEqual(r["slot_a"], r["slot_b"])
        self.assertEqual(cmp["counts"]["moved"],
                         sum(1 for s in statuses.values() if s == "moved"))

    def test_mass_change_flagged(self):
        pa = self.six_payload()
        pb = copy.deepcopy(pa)
        next(t for t in pb["tubes"] if t["id"] == 3)["mass"] = 60
        cmp = compare_records(record(1, pa), record(2, pb))
        row = next(r for r in cmp["tube_changes"] if r["tube_id"] == 3)
        self.assertTrue(row["mass_changed"])
        self.assertEqual(row["mass_a"], 51.0)
        self.assertEqual(row["mass_b"], 60.0)
        self.assertFalse(cmp["condition_flags"]["same_tubes"])

    def test_rotor_difference_flagged(self):
        pa = self.six_payload()
        pb = copy.deepcopy(pa)
        pb["radius_mm"] = 120.0
        cmp = compare_records(record(1, pa), record(2, pb))
        self.assertFalse(cmp["condition_flags"]["same_rotor"])
        self.assertTrue(cmp["condition_flags"]["same_tubes"])
        self.assertFalse(cmp["same_conditions"])
        entry = next(e for e in cmp["param_diffs"] if e["label"] == "转子半径 R")
        self.assertTrue(entry["differs"])
        self.assertEqual(entry["a"], 100.0)
        self.assertEqual(entry["b"], 120.0)
        self.assertTrue(any("转子参数不同" in w for w in cmp["warnings"]))

    def test_disabled_slot_difference(self):
        pa = self.six_payload()
        pb = copy.deepcopy(pa)
        pb["slots"][6]["enabled"] = False
        cmp = compare_records(record(1, pa), record(2, pb))
        self.assertFalse(cmp["condition_flags"]["same_rotor"])
        entry = next(e for e in cmp["param_diffs"] if e["label"] == "禁用槽位")
        self.assertTrue(entry["differs"])
        self.assertEqual(entry["b"], "6")

    def test_rpm_warning_force_delta_not_attributable(self):
        pa = self.six_payload(rpm=4000)
        pb = copy.deepcopy(pa)
        pb["rpm"] = 6000
        ra, rb = run_plan(pa), run_plan(pb)
        cmp = compare_records(record(1, pa, ra), record(2, pb, rb))
        self.assertTrue(cmp["condition_flags"]["same_rotor"])
        self.assertTrue(cmp["condition_flags"]["same_tubes"])
        self.assertFalse(cmp["condition_flags"]["same_runtime"])
        # 布局与 U1/U2 不随转速变化
        self.assertAlmostEqual(metric_cmp(cmp, "u1_mag_g_mm")["delta"], 0.0)
        self.assertNotAlmostEqual(
            metric_cmp(cmp, "centrifugal_force_n")["delta"], 0.0
        )
        self.assertTrue(any("转速不同" in w for w in cmp["warnings"]))

    def test_solver_difference_flagged(self):
        pa = self.six_payload()
        pb = copy.deepcopy(pa)
        pb["w2"] = 0.1
        cmp = compare_records(record(1, pa), record(2, pb))
        self.assertTrue(cmp["condition_flags"]["same_rotor"])
        self.assertFalse(cmp["condition_flags"]["same_solver"])
        self.assertTrue(any("求解输入不同" in w for w in cmp["warnings"]))

    def test_metric_delta_convention_b_minus_a(self):
        pa = self.six_payload()
        pb = copy.deepcopy(pa)
        pb["tubes"] = [{"id": i, "mass": 50} for i in range(1, 5)]
        cmp = compare_records(record(1, pa), record(2, pb))
        for m in cmp["metrics"]:
            if m["a"] is None or m["b"] is None:
                continue
            if m["is_angle"]:
                self.assertGreaterEqual(m["delta"], -180.0)
                self.assertLessEqual(m["delta"], 180.0)
            else:
                self.assertAlmostEqual(m["delta"], m["b"] - m["a"], places=6)
        self.assertEqual(cmp["grades"]["a"], "D")
        self.assertEqual(cmp["grades"]["b"], "A")

    def test_brief_contains_incremental_flag(self):
        pa = self.six_payload()
        ra = run_plan(pa)
        pb = copy.deepcopy(pa)
        pb["tubes"].append({"id": 7, "mass": 20})
        pb["previous"] = [
            {"tube_id": x["tube_id"], "slot": x["slot"]}
            for x in ra["placements"]
        ]
        rb = run_plan(pb)
        cmp = compare_records(record(1, pa, ra), record(2, pb, rb))
        self.assertFalse(cmp["a"]["incremental"])
        self.assertTrue(cmp["b"]["incremental"])
        self.assertEqual(cmp["a"]["id"], 1)
        self.assertEqual(cmp["b"]["id"], 2)

    def test_serializable(self):
        p = self.six_payload()
        cmp = compare_records(record(1, p), record(2, copy.deepcopy(p)))
        json.dumps(cmp, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
