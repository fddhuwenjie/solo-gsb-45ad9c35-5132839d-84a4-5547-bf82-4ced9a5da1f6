"""实测曝光核验：对照已保存版本，核对实际观测时段的有效分钟数。

计时规则（全部为左闭右开 [start, end) 的整分钟区间）：
1. 同一目标的多条实测记录重叠部分只计一次（先取并集）；
2. 不同目标的实测时段相撞，重叠分钟从双方同时扣除；
3. 再剔除与固定校准重叠、以及落在自身可行窗口外的分钟；
4. 剩余为有效分钟，达到版本原定曝光时长即判定完成。

核验以版本保存的目标、可行窗口、校准时段与排程为准，只读，不改动版本。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .astronomy import PlanError
from .scheduling_utils import (
    intersect_intervals,
    merge_intervals,
    parse_utc,
    subtract_intervals,
)

# 单次核验的实测记录条数上限
MAX_VERIFY_RECORDS = 500


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _parse_whole_minute(value, grid_start, name: str, field: str) -> int:
    """解析 UTC 整分钟时间 -> 网格分钟；格式非法/非整分钟时抛 PlanError。"""
    dt = parse_utc(value)  # 格式非法时 parse_utc 已抛 PlanError
    if dt.second != 0 or dt.microsecond != 0:
        raise PlanError(
            f"目标「{name}」的实测{field}时间须为 UTC 整分钟：{value!r}"
        )
    return int((dt - grid_start).total_seconds() // 60)


def verify_exposures(saved_result: dict, records_in) -> dict:
    """以版本保存的排程结果为准核验实测记录，返回逐目标结果字典。

    任何一条记录非法（未知目标、非整分钟、范围外、倒置）都抛 PlanError，
    由路由层整批返回 400。
    """
    grid = saved_result.get("grid") or {}
    grid_start = parse_utc(grid.get("start"))
    n = int(grid.get("minutes") or 0)
    if n <= 0:
        raise PlanError("版本数据缺少有效的时间网格")

    targets = saved_result.get("targets") or []
    by_id = {str(t.get("id")): t for t in targets}
    calibrations = saved_result.get("calibrations") or []
    scheduled_by_id = {
        str(s.get("target_id")): s for s in (saved_result.get("scheduled") or [])
    }

    def _iv(a: int, b: int) -> dict:
        return {
            "start_min": a,
            "end_min": b,
            "start": _iso(grid_start + timedelta(minutes=a)),
            "end": _iso(grid_start + timedelta(minutes=b)),
            "minutes": b - a,
        }

    # ------------------------------------------------------------ 解析与校验
    if not isinstance(records_in, list) or not records_in:
        raise PlanError("请提供至少一条实测观测记录（records 数组）")
    if len(records_in) > MAX_VERIFY_RECORDS:
        raise PlanError(f"实测记录条数上限 {MAX_VERIFY_RECORDS} 条")

    per_target: dict[str, list[list[int]]] = {}
    record_counts: dict[str, int] = {}
    for i, rec in enumerate(records_in):
        if not isinstance(rec, dict):
            raise PlanError(f"第 {i + 1} 条实测记录不是 JSON 对象")
        tid = str(rec.get("target_id") or "").strip()
        if not tid:
            raise PlanError(f"第 {i + 1} 条实测记录缺少目标 ID")
        t = by_id.get(tid)
        if t is None:
            raise PlanError(f"未知目标：{tid!r}（不在该版本的目标列表中）")
        name = t.get("name") or tid
        m0 = _parse_whole_minute(rec.get("start"), grid_start, name, "开始")
        m1 = _parse_whole_minute(rec.get("end"), grid_start, name, "结束")
        if m1 <= m0:
            raise PlanError(f"目标「{name}」的实测时段结束须晚于开始")
        if m0 < 0 or m1 > n:
            raise PlanError(
                f"目标「{name}」的实测时段超出版本日期范围"
                f"（{grid.get('start')} – {grid.get('end')}）"
            )
        per_target.setdefault(tid, []).append([m0, m1])
        record_counts[tid] = record_counts.get(tid, 0) + 1

    # ============================================================ 逐目标核验
    # 1) 同一目标的重叠记录只计一次
    measured = {tid: merge_intervals(ivs) for tid, ivs in per_target.items()}

    # 2) 不同目标实测相撞的部分从双方扣除
    tids = sorted(measured)
    collision: dict[str, list[list[int]]] = {tid: [] for tid in measured}
    for i in range(len(tids)):
        for j in range(i + 1, len(tids)):
            inter = intersect_intervals(measured[tids[i]], measured[tids[j]])
            if inter:
                collision[tids[i]].extend(inter)
                collision[tids[j]].extend(inter)
    collision = {tid: merge_intervals(ivs) for tid, ivs in collision.items()}

    def _colliding_names(tid: str, a: int, b: int) -> str:
        names = [
            (by_id[other].get("name") or other)
            for other in tids
            if other != tid
            and any(c < b and a < d for c, d in measured[other])
        ]
        return "、".join(f"「{x}」" for x in names)

    results = []
    for t in targets:
        tid = str(t.get("id"))
        name = t.get("name") or tid
        planned = int(t.get("exposure_minutes") or 0)
        mine = measured.get(tid, [])
        excluded: list[dict] = []

        # 2) 跨目标相撞（双方均扣除）
        rest = mine
        if collision.get(tid):
            for a, b in collision[tid]:
                excluded.append(
                    {
                        **_iv(a, b),
                        "reason": f"与目标{_colliding_names(tid, a, b)}的实测时段相撞，双方均扣除",
                    }
                )
            rest = subtract_intervals(rest, collision[tid])

        # 3a) 与固定校准重叠
        for c in calibrations:
            civ = [[int(c["start_min"]), int(c["end_min"])]]
            hit = intersect_intervals(rest, civ)
            for a, b in hit:
                excluded.append(
                    {**_iv(a, b), "reason": f"与固定校准「{c.get('label')}」重叠"}
                )
            if hit:
                rest = subtract_intervals(rest, civ)

        # 3b) 落在自身可行窗口外
        windows = merge_intervals(
            [[int(w["start_min"]), int(w["end_min"])] for w in (t.get("windows") or [])]
        )
        outside = subtract_intervals(rest, windows)
        for a, b in outside:
            excluded.append({**_iv(a, b), "reason": "落在自身可行观测窗口外"})
        rest = subtract_intervals(rest, outside)

        measured_min = sum(b - a for a, b in mine)
        effective = sum(b - a for a, b in rest)
        sched = scheduled_by_id.get(tid)
        results.append(
            {
                "target_id": tid,
                "name": name,
                "planned_minutes": planned,
                "scheduled": (
                    {
                        "start_min": sched["start_min"],
                        "end_min": sched["end_min"],
                        "start": sched["start"],
                        "end": sched["end"],
                    }
                    if sched
                    else None
                ),
                "record_count": record_counts.get(tid, 0),
                "measured_minutes": measured_min,
                "effective_minutes": effective,
                "complete": effective >= planned,
                "measured": [_iv(a, b) for a, b in mine],
                "effective": [_iv(a, b) for a, b in rest],
                "excluded": sorted(excluded, key=lambda x: x["start_min"]),
            }
        )

    complete_count = sum(1 for r in results if r["complete"])
    return {
        "grid": grid,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "target_count": len(results),
            "record_count": len(records_in),
            "complete_count": complete_count,
            "incomplete_count": len(results) - complete_count,
        },
        "results": results,
    }
