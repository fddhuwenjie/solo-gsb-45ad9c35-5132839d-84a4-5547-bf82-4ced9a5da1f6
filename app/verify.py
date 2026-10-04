"""实测曝光核验：对照已保存版本，核算实际观测时段的有效曝光分钟。

规则（分钟一律按左闭右开 [start, end) 计）：
1. 同一目标的多条记录取并集，重叠部分只计一次 -> 实测分钟；
2. 不同目标实测时段相撞的部分从双方各扣除；
3. 再剔除与固定校准重叠、以及落在自身可行窗口外的分钟 -> 有效分钟；
4. 有效分钟达到该目标原定曝光时长即为「完成」。

核验只读取版本快照（保存时的目标、可行窗口、校准时段与排程），
不改动任何已存数据；偏离原排程但符合条件的实测同样计入，
当初未排上的目标也可核验。
"""

from __future__ import annotations

import re
from datetime import timedelta

from .astronomy import PlanError
from .scheduling_utils import (
    intersect_intervals,
    merge_intervals,
    parse_utc,
    subtract_intervals,
)

# 单批实测记录数量上限
MAX_VERIFY_RECORDS = 1000

# 必须精确到分钟：YYYY-MM-DDTHH:MM（秒可省略或为 :00）
_MINUTE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?Z?$"
)


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _parse_minute(value, label: str):
    """解析必须落在整分钟上的 UTC 时间；非整分钟整批报错。"""
    if not isinstance(value, str) or not _MINUTE_RE.match(value.strip()):
        raise PlanError(
            f"{label}须为精确到分钟的 UTC 时间（示例 2026-09-24T20:00Z）：{value!r}"
        )
    dt = parse_utc(value)
    if dt.second != 0:
        raise PlanError(f"{label}不是整分钟（秒须为 00）：{value!r}")
    return dt


def _sum(ivs) -> int:
    return sum(b - a for a, b in ivs)


def verify_exposure(saved: dict, payload: dict) -> dict:
    """以版本保存的 result 为准，核验一批实测观测时段。

    saved:   版本库存储的排程结果 JSON（grid/targets/calibrations/scheduled）。
    payload: {"records": [{"target_id", "start", "end"}, ...]}。
    任何一条记录非法（未知目标、非整分钟、范围外、倒置）都整批拒绝。
    """
    grid = saved.get("grid") or {}
    grid_start = parse_utc(grid.get("start"))
    n = int(grid.get("minutes") or 0)
    if n <= 0:
        raise PlanError("版本数据缺少有效的时间网格")
    grid_end = grid_start + timedelta(minutes=n)

    targets = saved.get("targets") or []
    by_id = {str(t["id"]): t for t in targets}

    # ------------------------------------------------------ 解析 + 整批校验
    records_in = payload.get("records")
    if not isinstance(records_in, list) or not records_in:
        raise PlanError("需要包含非空 records 数组（实测观测时段）")
    if len(records_in) > MAX_VERIFY_RECORDS:
        raise PlanError(f"单批实测记录数量上限 {MAX_VERIFY_RECORDS} 条")

    records = []
    for i, rec in enumerate(records_in, 1):
        label = f"第 {i} 条记录"
        if not isinstance(rec, dict):
            raise PlanError(f"{label}不是 JSON 对象")
        tid = str(rec.get("target_id") or "").strip()
        if not tid:
            raise PlanError(f"{label}缺少 target_id")
        if tid not in by_id:
            raise PlanError(f"{label}的目标「{tid}」不在该版本中（未知目标）")
        t0 = _parse_minute(rec.get("start"), f"{label}（{tid}）的开始时间")
        t1 = _parse_minute(rec.get("end"), f"{label}（{tid}）的结束时间")
        if t1 <= t0:
            raise PlanError(
                f"{label}（{tid}）时段倒置：结束 {_iso(t1)} 须晚于开始 {_iso(t0)}"
            )
        if t0 < grid_start or t1 > grid_end:
            raise PlanError(
                f"{label}（{tid}）{_iso(t0)}–{_iso(t1)} 超出版本日期范围 "
                f"{_iso(grid_start)}–{_iso(grid_end)}"
            )
        m0 = int((t0 - grid_start).total_seconds() // 60)
        m1 = int((t1 - grid_start).total_seconds() // 60)
        records.append({"target_id": tid, "start_min": m0, "end_min": m1})

    # ------------------------------------------------------ 同目标并集（去重）
    raw_by_target: dict[str, list] = {}
    for r in records:
        raw_by_target.setdefault(r["target_id"], []).append(
            [r["start_min"], r["end_min"]]
        )
    union_by_target = {
        tid: merge_intervals(ivs) for tid, ivs in raw_by_target.items()
    }

    calibrations = saved.get("calibrations") or []
    cal_ivs = merge_intervals(
        [[int(c["start_min"]), int(c["end_min"])] for c in calibrations]
    )

    sched_by_target = {
        str(s["target_id"]): s for s in (saved.get("scheduled") or [])
    }

    def _span(a: int, b: int) -> dict:
        return {
            "start": _iso(grid_start + timedelta(minutes=a)),
            "end": _iso(grid_start + timedelta(minutes=b)),
            "minutes": b - a,
        }

    # ------------------------------------------------------ 逐目标核算
    results = []
    for t in targets:
        tid = str(t["id"])
        mine = union_by_target.get(tid, [])
        raw = raw_by_target.get(tid, [])
        others = merge_intervals(
            [iv for otid, ivs in union_by_target.items() if otid != tid for iv in ivs]
        )

        excluded: list[dict] = []

        # 1) 异目标相撞：从本目标扣除（对方核算时同样扣除）
        collision = intersect_intervals(mine, others)
        for a, b in collision:
            names = [
                str(by_id[otid].get("name") or otid)
                for otid, ivs in union_by_target.items()
                if otid != tid and intersect_intervals([[a, b]], ivs)
            ]
            excluded.append(
                {
                    **_span(a, b),
                    "reason": "与其他目标实测时段相撞（"
                    + "、".join(names)
                    + "），双方均扣除",
                }
            )
        rest = subtract_intervals(mine, collision)

        # 2) 与固定校准重叠
        cal_hit = intersect_intervals(rest, cal_ivs)
        for a, b in cal_hit:
            labels = [
                str(c.get("label") or c.get("id"))
                for c in calibrations
                if int(c["start_min"]) < b and a < int(c["end_min"])
            ]
            excluded.append(
                {
                    **_span(a, b),
                    "reason": "与固定校准「" + "、".join(labels) + "」重叠",
                }
            )
        rest = subtract_intervals(rest, cal_hit)

        # 3) 落在自身可行窗口外
        win_ivs = merge_intervals(
            [[int(w["start_min"]), int(w["end_min"])] for w in (t.get("windows") or [])]
        )
        outside = subtract_intervals(rest, win_ivs)
        for a, b in outside:
            excluded.append({**_span(a, b), "reason": "落在自身可行窗口外"})
        effective = subtract_intervals(rest, outside)

        excluded.sort(key=lambda e: e["start"])

        planned = int(t.get("exposure_minutes") or 0)
        eff_minutes = _sum(effective)
        sched = sched_by_target.get(tid)
        results.append(
            {
                "target_id": tid,
                "name": str(t.get("name") or tid),
                "planned_minutes": planned,
                "was_scheduled": sched is not None,
                "scheduled": (
                    {"start": sched["start"], "end": sched["end"]}
                    if sched
                    else None
                ),
                "record_count": len(raw),
                "input_minutes": _sum(raw),
                "actual_minutes": _sum(mine),
                "effective_minutes": eff_minutes,
                "shortfall_minutes": max(0, planned - eff_minutes),
                "complete": eff_minutes >= planned,
                "records": [_span(a, b) for a, b in raw],
                "excluded": excluded,
            }
        )

    verified = [r for r in results if r["record_count"] > 0]
    return {
        "grid": {"start": _iso(grid_start), "end": _iso(grid_end)},
        "records_accepted": len(records),
        "results": results,
        "summary": {
            "targets": len(results),
            "verified": len(verified),
            "completed": sum(1 for r in verified if r["complete"]),
        },
    }
