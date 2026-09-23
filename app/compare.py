"""两条已保存配平方案的对比数据构建。

纯函数模块: 只读取库中已保存的 request/result, 不改动求解逻辑与存储格式。

对比内容:
- 按试管编号(tube_id)合并两方案的摆放明细, 标记
  ``added``(新增) / ``removed``(移除) / ``moved``(换槽) / ``unchanged``(未变);
- 并列展示两方案的一阶、二阶不平衡量(及方向)、离心合力/力矩、偏心距等指标与差值;
- 对比转子(槽位几何/半径)、转速与求解输入(权重/搜索设置),
  当条件不一致时给出警告, 避免把指标差值误读为同条件优化的优劣结论。

差值约定: 所有 delta = 方案 B - 方案 A。
"""
from __future__ import annotations

from typing import Any, Optional

EPS = 1e-6

# (结果字段, 中文标签, 单位, 是否为角度)
METRIC_FIELDS: list[tuple[str, str, Optional[str], bool]] = [
    ("u1_mag_g_mm", "一阶不平衡量 |U₁|", "g·mm", False),
    ("u1_angle_deg", "U₁ 方向", "°", True),
    ("u2_mag_g_mm", "二阶不平衡量 |U₂|", "g·mm", False),
    ("u2_angle_deg", "U₂ 方向", "°", True),
    ("centrifugal_force_n", "离心合力 F", "N", False),
    ("resultant_moment_n_m", "合力矩 M", "N·m", False),
    ("equivalent_eccentricity_um", "等效偏心距", "μm", False),
    ("total_mass_g", "试管总质量", "g", False),
]

SOLVER_FIELDS: list[tuple[str, str, Optional[str]]] = [
    ("w1", "一阶权重 w₁", None),
    ("w2", "二阶权重 w₂", None),
    ("move_weight", "移动罚权", None),
    ("restarts", "随机起点数", None),
    ("chain_steps", "退火链长度", None),
    ("seed", "随机种子", None),
]

GROUP_LABELS = {
    "rotor": "转子参数",
    "runtime": "运行参数",
    "solver": "求解输入",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _num_eq(a: Any, b: Any, eps: float = EPS) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= eps


def _angle_delta(a: float, b: float) -> float:
    """角度差归一化到 [-180, 180]。"""
    return (float(b) - float(a) + 180.0) % 360.0 - 180.0


def _slots_map(req: dict[str, Any]) -> dict[int, tuple[float, bool]]:
    """槽位索引 → (归一化角度, 是否启用)。"""
    n = int(req.get("slot_count") or len(req.get("slots") or []))
    out: dict[int, tuple[float, bool]] = {}
    for s in req.get("slots") or []:
        idx = int(s["index"])
        ang = s.get("angle")
        if ang is None:
            ang = 360.0 * idx / max(n, 1)
        out[idx] = (round(float(ang) % 360.0, 6), bool(s.get("enabled", True)))
    return out


def _slot_list(indices: list[int], limit: int = 8) -> str:
    if not indices:
        return "无"
    head = ", ".join(str(i) for i in indices[:limit])
    if len(indices) > limit:
        head += f" 等 {len(indices)} 个"
    return head


def _id_list(ids: list[int], limit: int = 6) -> str:
    head = ", ".join(f"#{i}" for i in ids[:limit])
    if len(ids) > limit:
        head += f" 等 {len(ids)} 支"
    return head


def _entry(group: str, label: str, a: Any, b: Any,
           unit: Optional[str] = None) -> dict[str, Any]:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        differs = not _num_eq(a, b)
    else:
        differs = a != b
    return {
        "group": group,
        "label": label,
        "a": a,
        "b": b,
        "unit": unit,
        "differs": differs,
    }


# ---------------------------------------------------------------------------
# 参数差异
# ---------------------------------------------------------------------------
def _param_diffs(qa: dict[str, Any], qb: dict[str, Any]) -> list[dict[str, Any]]:
    sa, sb = _slots_map(qa), _slots_map(qb)
    entries: list[dict[str, Any]] = []

    entries.append(_entry("rotor", "槽位数", len(sa), len(sb)))

    dis_a = sorted(i for i, (_, en) in sa.items() if not en)
    dis_b = sorted(i for i, (_, en) in sb.items() if not en)
    entries.append(_entry("rotor", "禁用槽位", _slot_list(dis_a), _slot_list(dis_b)))

    changed = sorted(
        i for i in (set(sa) & set(sb)) if abs(sa[i][0] - sb[i][0]) > EPS
    )
    for i in changed[:10]:
        entries.append(_entry("rotor", f"槽 {i} 角度", sa[i][0], sb[i][0], "°"))
    if len(changed) > 10:
        entries.append({
            "group": "rotor",
            "label": f"其他角度差异({len(changed) - 10} 个槽位)",
            "a": "不同", "b": "不同", "unit": None, "differs": True,
        })

    entries.append(_entry(
        "rotor", "转子半径 R",
        float(qa.get("radius_mm", 100.0)), float(qb.get("radius_mm", 100.0)), "mm",
    ))
    entries.append(_entry(
        "runtime", "转速",
        float(qa.get("rpm", 4000.0)), float(qb.get("rpm", 4000.0)), "rpm",
    ))
    for key, label, unit in SOLVER_FIELDS:
        entries.append(_entry(
            "solver", label, qa.get(key), qb.get(key), unit,
        ))
    return entries


def _condition_flags(qa: dict[str, Any], qb: dict[str, Any]) -> dict[str, bool]:
    sa, sb = _slots_map(qa), _slots_map(qb)
    same_rotor = sa == sb and _num_eq(
        qa.get("radius_mm", 100.0), qb.get("radius_mm", 100.0)
    )

    ta = {int(t["id"]): t for t in qa.get("tubes", [])}
    tb = {int(t["id"]): t for t in qb.get("tubes", [])}
    same_tubes = set(ta) == set(tb)
    if same_tubes:
        for tid, x in ta.items():
            y = tb[tid]
            if not _num_eq(x.get("mass"), y.get("mass"), eps=1e-9):
                same_tubes = False
                break
            if bool(x.get("locked", False)) != bool(y.get("locked", False)):
                same_tubes = False
                break
            if bool(x.get("locked", False)) and x.get("slot") != y.get("slot"):
                same_tubes = False
                break

    same_runtime = _num_eq(qa.get("rpm", 4000.0), qb.get("rpm", 4000.0))
    same_solver = all(
        _num_eq(qa.get(key), qb.get(key)) for key, _, _ in SOLVER_FIELDS
    )
    return {
        "same_rotor": same_rotor,
        "same_tubes": same_tubes,
        "same_runtime": same_runtime,
        "same_solver": same_solver,
        "same_conditions": (
            same_rotor and same_tubes and same_runtime and same_solver
        ),
    }


# ---------------------------------------------------------------------------
# 摆放明细合并
# ---------------------------------------------------------------------------
def _placement_maps(rec: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """tube_id → 摆放记录, 角度缺失时回退到请求中的槽位角度。"""
    req = rec["request"]
    slot_angles = {
        int(s["index"]): s.get("angle")
        for s in req.get("slots", [])
    }
    out: dict[int, dict[str, Any]] = {}
    for p in rec["result"].get("placements", []):
        tid = int(p["tube_id"])
        slot = p.get("slot")
        angle = p.get("angle")
        if angle is None and slot is not None and slot in slot_angles:
            angle = slot_angles[slot]
        out[tid] = {
            "tube_id": tid,
            "mass": float(p.get("mass", 0.0)),
            "locked": bool(p.get("locked", False)),
            "slot": (int(slot) if slot is not None else None),
            "angle": (float(angle) if angle is not None else None),
        }
    return out


def _tube_changes(pa: dict[int, dict[str, Any]],
                  pb: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tid in sorted(set(pa) | set(pb)):
        a, b = pa.get(tid), pb.get(tid)
        if a is None:
            status = "added"
        elif b is None:
            status = "removed"
        elif a["slot"] != b["slot"]:
            status = "moved"
        else:
            status = "unchanged"
        mass_changed = bool(
            a and b and not _num_eq(a["mass"], b["mass"], eps=1e-9)
        )
        lock_changed = bool(a and b and a["locked"] != b["locked"])
        rows.append({
            "tube_id": tid,
            "status": status,
            "mass_a": a["mass"] if a else None,
            "mass_b": b["mass"] if b else None,
            "slot_a": a["slot"] if a else None,
            "slot_b": b["slot"] if b else None,
            "angle_a": a["angle"] if a else None,
            "angle_b": b["angle"] if b else None,
            "locked_a": bool(a and a["locked"]),
            "locked_b": bool(b and b["locked"]),
            "mass_changed": mass_changed,
            "lock_changed": lock_changed,
        })
    return rows


# ---------------------------------------------------------------------------
# 指标对比
# ---------------------------------------------------------------------------
def _metrics_compare(ma: dict[str, Any],
                     mb: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, label, unit, is_angle in METRIC_FIELDS:
        va, vb = ma.get(key), mb.get(key)
        if is_angle:
            delta: Optional[float] = (
                _angle_delta(va, vb) if va is not None and vb is not None else None
            )
        else:
            delta = (
                round(float(vb) - float(va), 9)
                if va is not None and vb is not None else None
            )
        out.append({
            "key": key,
            "label": label,
            "unit": unit,
            "is_angle": is_angle,
            "a": va,
            "b": vb,
            "delta": delta,
        })
    return out


# ---------------------------------------------------------------------------
# 警告文案
# ---------------------------------------------------------------------------
def _warnings(flags: dict[str, bool],
              diffs: list[dict[str, Any]],
              rows: list[dict[str, Any]]) -> list[str]:
    warns: list[str] = []

    rotor_labels = [
        e["label"] for e in diffs
        if e["group"] == "rotor" and e["differs"]
    ]
    if rotor_labels:
        warns.append(
            "转子参数不同(" + "、".join(rotor_labels[:4])
            + (" 等" if len(rotor_labels) > 4 else "")
            + "): 槽位几何与半径直接决定 U₁/U₂ 的绝对值与可行布局。"
        )

    if not flags["same_tubes"]:
        added = [r["tube_id"] for r in rows if r["status"] == "added"]
        removed = [r["tube_id"] for r in rows if r["status"] == "removed"]
        mass_chg = [
            r for r in rows
            if r["mass_changed"] and r["status"] != "added" and r["status"] != "removed"
        ]
        lock_chg = [r for r in rows if r["lock_changed"]]
        parts: list[str] = []
        if added:
            parts.append(f"新增 {_id_list(added)}")
        if removed:
            parts.append(f"移除 {_id_list(removed)}")
        if mass_chg:
            detail = ", ".join(
                f"#{r['tube_id']} {r['mass_a']:g}→{r['mass_b']:g} g"
                for r in mass_chg[:4]
            )
            if len(mass_chg) > 4:
                detail += f" 等 {len(mass_chg)} 支"
            parts.append(f"质量变化({detail})")
        if lock_chg:
            parts.append("锁定约束变化(" + _id_list([r["tube_id"] for r in lock_chg]) + ")")
        warns.append("试管清单不同: " + "; ".join(parts) + "。")

    if not flags["same_runtime"]:
        rpm = next(e for e in diffs if e["label"] == "转速")
        warns.append(
            f"转速不同({rpm['a']:g} → {rpm['b']:g} rpm): "
            "转速不改变布局, 但离心合力/力矩与 ω² 成正比, 其差值不能归因于布局。"
        )

    if not flags["same_solver"]:
        labels = [
            e["label"] for e in diffs
            if e["group"] == "solver" and e["differs"]
        ]
        warns.append(
            "求解输入不同(" + "、".join(labels)
            + "): 优化权重或搜索设置变化, 指标差异可能来自设置而非布局本身。"
        )
    return warns


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def compare_records(rec_a: dict[str, Any], rec_b: dict[str, Any]) -> dict[str, Any]:
    """构建两条已保存方案的对比数据。delta 约定: B - A。"""
    qa, qb = rec_a["request"], rec_b["request"]
    ma, mb = rec_a["result"]["metrics"], rec_b["result"]["metrics"]

    flags = _condition_flags(qa, qb)
    diffs = _param_diffs(qa, qb)
    rows = _tube_changes(_placement_maps(rec_a), _placement_maps(rec_b))

    counts = {
        "added": sum(1 for r in rows if r["status"] == "added"),
        "removed": sum(1 for r in rows if r["status"] == "removed"),
        "moved": sum(1 for r in rows if r["status"] == "moved"),
        "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
        "mass_changed": sum(1 for r in rows if r["mass_changed"]),
        "lock_changed": sum(1 for r in rows if r["lock_changed"]),
        "total": len(rows),
    }

    if flags["same_conditions"]:
        interpretation = (
            "两方案的转子、试管清单、转速与求解输入完全一致, "
            "下列指标差值可归因于摆放布局本身, 可作为同条件下的优化对比。"
        )
    else:
        interpretation = (
            "两方案输入条件不一致: 指标差值同时受摆放布局与输入参数影响, "
            "不能直接当作同条件优化结果的优劣结论, 请结合下列参数差异判读。"
        )

    return {
        "a": _brief(rec_a),
        "b": _brief(rec_b),
        "same_conditions": flags["same_conditions"],
        "condition_flags": flags,
        "param_diffs": diffs,
        "group_labels": GROUP_LABELS,
        "warnings": _warnings(flags, diffs, rows),
        "interpretation": interpretation,
        "tube_changes": rows,
        "counts": counts,
        "metrics": _metrics_compare(ma, mb),
        "grades": {"a": ma.get("grade"), "b": mb.get("grade")},
        "delta_convention": "B - A",
    }


def _brief(rec: dict[str, Any]) -> dict[str, Any]:
    cfg = rec["result"].get("config", {})
    return {
        "id": rec.get("id"),
        "name": rec.get("name"),
        "created_at": rec.get("created_at"),
        "incremental": bool(cfg.get("incremental", False)),
        "total_tubes": rec["result"].get("total_tubes"),
    }
