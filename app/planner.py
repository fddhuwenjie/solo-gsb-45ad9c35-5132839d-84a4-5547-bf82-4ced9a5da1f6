"""观测窗口规划核心：逐分钟球面计算 -> 可行区间 -> 不重叠排程。"""

from __future__ import annotations

import math
from datetime import timedelta

import numpy as np

from .astronomy import (
    TWILIGHT_MODES,
    PlanError,
    gmst_degrees,
    sun_ra_dec,
)
from .scheduling_utils import (
    MAX_CALIBRATIONS,
    MAX_SPAN_MINUTES,
    MAX_TARGETS,
    intersect_intervals,
    merge_intervals,
    parse_coord,
    parse_dec,
    parse_ra,
    parse_utc,
    subtract_intervals,
)


def _mask_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """numpy 布尔数组中 True 连续段 -> [(start, end_exclusive), ...]。"""
    if mask.size == 0 or not bool(mask.any()):
        return []
    padded = np.r_[False, mask, False]
    diff = np.diff(padded.view(np.int8))
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)
    return list(zip(starts.tolist(), ends.tolist()))

# 太阳分层着色用的阈值（度），由严到宽
SUN_BAND_LEVELS = [
    ("night", -18.0),
    ("astronomical", -12.0),
    ("nautical", -6.0),
    ("civil", 0.0),
]


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _fmt_minute(m: int, grid_start) -> str:
    dt = grid_start + timedelta(minutes=m)
    return dt.strftime("%m-%d %H:%M")


def build_plan(payload: dict) -> dict:
    """根据完整输入 payload 计算并返回排程结果字典。"""
    warnings: list[str] = []

    # ------------------------------------------------------------------ site
    site = payload.get("site") or {}
    lat = parse_coord(site.get("lat"), "lat")
    lon = parse_coord(site.get("lon"), "lon")

    # ------------------------------------------------------------------ time
    start_day = parse_utc(payload.get("start_date"))
    end_day = parse_utc(payload.get("end_date"), end_of_day=True)
    if end_day.date() < start_day.date():
        raise PlanError("结束日期不能早于开始日期")
    grid_start = start_day.replace(hour=0, minute=0, second=0)
    grid_end = (end_day.replace(hour=0, minute=0, second=0)) + timedelta(days=1)
    n = int((grid_end - grid_start).total_seconds() // 60)
    if n <= 0:
        raise PlanError("日期范围无效")
    if n > MAX_SPAN_MINUTES:
        raise PlanError(f"规划范围最长 14 天（当前约 {n // 1440 + 1} 天）")

    # ------------------------------------------------------------- constraints
    mode = str(payload.get("twilight_mode", "night"))
    if mode not in TWILIGHT_MODES:
        raise PlanError(f"未知暮光模式：{mode}")
    sun_limit = TWILIGHT_MODES[mode]
    try:
        min_alt = float(payload.get("min_altitude", 20.0))
        max_x = float(payload.get("max_airmass", 2.5))
    except (TypeError, ValueError):
        raise PlanError("最低高度 / 最大空气质量必须是数字")
    if not (0.0 <= min_alt <= 90.0):
        raise PlanError("最低高度须在 0–90° 之间")
    if max_x < 1.0:
        raise PlanError("最大空气质量不能小于 1.0")
    # 空气质量对应的等效最低高度
    alt_from_x = 90.0 - math.degrees(math.acos(min(1.0, 1.0 / max_x)))
    eff_min_alt = max(min_alt, alt_from_x)

    # --------------------------------------------------------------- targets
    raw_targets = payload.get("targets") or []
    if not raw_targets:
        raise PlanError("请至少添加一个观测目标")
    if len(raw_targets) > MAX_TARGETS:
        raise PlanError(f"目标数量上限 {MAX_TARGETS} 个")

    targets = []
    seen_ids: set[str] = set()
    for i, t in enumerate(raw_targets):
        tid = str(t.get("id") or f"T{i + 1}").strip()
        if not tid or tid in seen_ids:
            raise PlanError(f"目标 id 重复或为空：{tid!r}")
        seen_ids.add(tid)
        name = str(t.get("name") or tid).strip() or tid
        ra = parse_ra(t.get("ra"))
        dec = parse_dec(t.get("dec"))
        try:
            expo = int(float(t.get("exposure_minutes")))
        except (TypeError, ValueError):
            raise PlanError(f"目标「{name}」的曝光时长不是数字")
        if expo <= 0:
            raise PlanError(f"目标「{name}」的曝光时长须为正整数分钟")
        if expo > n:
            raise PlanError(f"目标「{name}」的曝光时长超出整个计划范围")
        try:
            priority = int(t.get("priority", 100))
        except (TypeError, ValueError):
            raise PlanError(f"目标「{name}」的优先级不是整数")
        targets.append(
            {
                "id": tid,
                "name": name,
                "ra": ra,
                "dec": dec,
                "exposure": expo,
                "priority": priority,
                "order": i,
            }
        )

    # ----------------------------------------------------------- calibrations
    raw_cals = payload.get("calibrations") or []
    if len(raw_cals) > MAX_CALIBRATIONS:
        raise PlanError(f"校准时段数量上限 {MAX_CALIBRATIONS} 个")
    calibrations = []
    for i, c in enumerate(raw_cals):
        cid = str(c.get("id") or f"C{i + 1}").strip()
        label = str(c.get("label") or f"校准 {i + 1}").strip() or f"校准 {i + 1}"
        t0 = parse_utc(c.get("start"))
        t1 = parse_utc(c.get("end"))
        if t1 <= t0:
            raise PlanError(f"校准「{label}」结束时间须晚于开始时间")
        m0 = int((t0 - grid_start).total_seconds() // 60)
        m1 = int((t1 - grid_start).total_seconds() // 60)
        m0 = max(0, m0)
        m1 = min(n, m1)
        if m1 <= m0:
            warnings.append(f"校准「{label}」完全落在日期范围外，已忽略")
            continue
        calibrations.append(
            {
                "id": cid,
                "label": label,
                "start_min": m0,
                "end_min": m1,
                "start": _iso(grid_start + timedelta(minutes=m0)),
                "end": _iso(grid_start + timedelta(minutes=m1)),
                "kind": "calibration",
            }
        )
    # 校准之间若重叠，直接拒绝（固定时段本身不应冲突）
    cal_ivs = merge_intervals([[c["start_min"], c["end_min"]] for c in calibrations])
    if sum(c["end_min"] - c["start_min"] for c in calibrations) != sum(
        b - a for a, b in cal_ivs
    ):
        raise PlanError("校准时段之间存在重叠，请先修正固定校准安排")

    # ================================================================
    # 太阳高度（逐分钟，向量化）+ 夜间/暮光分层
    # ================================================================
    idx = np.arange(n, dtype=np.float64)
    # 本地恒星时（度）：GMST(起点) + 每平太阳分钟的恒星时增量
    gmst0 = gmst_degrees(grid_start)
    lst = (gmst0 + (360.98564736629 / 1440.0) * idx + lon) % 360.0

    # 太阳赤经赤纬每 5 分钟采样，再按分钟前向填充（误差远小于 1 分钟网格）
    sample_idx = np.minimum((idx // 5).astype(int), (n - 1) // 5)
    sun_eq = np.array(
        [sun_ra_dec(grid_start + timedelta(minutes=5 * k))
         for k in range(int(sample_idx[-1]) + 1)]
    )
    ra_s_all = np.deg2rad(sun_eq[sample_idx, 0])
    dec_s_all = np.deg2rad(sun_eq[sample_idx, 1])

    phi = math.radians(lat)
    sin_phi, cos_phi = math.sin(phi), math.cos(phi)
    ha_s = np.deg2rad(lst) - ra_s_all
    sin_h = np.sin(dec_s_all) * sin_phi + np.cos(dec_s_all) * cos_phi * np.cos(ha_s)
    sun_alt = np.degrees(np.arcsin(np.clip(sin_h, -1.0, 1.0)))

    # 四种太阳阈值各取区间（供前端分层着色）
    sun_bands = {}
    night_mask = None
    for key, limit in SUN_BAND_LEVELS:
        mask = (sun_alt < limit)
        sun_bands[key] = [
            {
                "start_min": int(a),
                "end_min": int(b),
                "start": _iso(grid_start + timedelta(minutes=int(a))),
                "end": _iso(grid_start + timedelta(minutes=int(b))),
            }
            for a, b in _mask_runs(mask)
        ]
        if key == mode:
            night_mask = mask

    # ================================================================
    # 逐目标高度 / 空气质量 / 可行区间（向量化）
    # ================================================================
    sample_step = max(10, int(math.ceil(n / 400 / 5.0)) * 5)
    sample_sel = ((idx % sample_step) == 0)
    sample_minutes = idx[sample_sel].astype(int)
    lst_rad = np.deg2rad(lst)

    for t in targets:
        dec_rad = math.radians(t["dec"])
        sin_dec, cos_dec = math.sin(dec_rad), math.cos(dec_rad)
        ha = lst_rad - math.radians(t["ra"])
        sin_h = sin_dec * sin_phi + cos_dec * cos_phi * np.cos(ha)
        alt_arr = np.degrees(np.arcsin(np.clip(sin_h, -1.0, 1.0)))
        with np.errstate(divide="ignore"):
            x_arr = 1.0 / np.cos(np.deg2rad(90.0 - alt_arr))
        x_arr = np.where(alt_arr > 0.0, x_arr, np.inf)

        best_alt = float(alt_arr.max())
        finite_x = x_arr[np.isfinite(x_arr)]
        best_x = float(finite_x.min()) if finite_x.size else math.inf

        m_alt = (alt_arr >= min_alt)
        m_air = (x_arr <= max_x)
        feasible_mask = night_mask & m_alt & m_air
        windows = [list(w) for w in _mask_runs(feasible_mask)]

        # 完全不可行时的归因
        no_window_reason = None
        if not windows:
            if not bool(m_alt.any()):
                no_window_reason = (
                    f"整个日期范围内最高高度仅 {best_alt:.1f}°，"
                    f"始终低于最低高度 {min_alt:.0f}°"
                )
            else:
                alt_ivs = [list(w) for w in _mask_runs(m_alt)]
                dark_ivs = [list(w) for w in _mask_runs(night_mask)]
                if not intersect_intervals(alt_ivs, dark_ivs):
                    no_window_reason = (
                        f"目标可升至 {min_alt:.0f}° 以上，但高度达标的时段全部处于"
                        f"白昼或不满足的暮光条件（当前要求太阳低于 {sun_limit:.0f}°）"
                    )
                elif not bool(m_air.any()):
                    no_window_reason = (
                        f"高度可超 {min_alt:.0f}°，但空气质量始终大于上限 {max_x:g}"
                        f"（等效要求高度 ≥ {alt_from_x:.1f}°，实际最高 {best_alt:.1f}°）"
                    )
                else:
                    no_window_reason = (
                        "高度、暮光、空气质量三类约束各自存在可行时段，但互不重合"
                    )

        # 原始窗口都太短 -> 记录最长窗口及其截断原因
        shortage = None
        longest = max(windows, key=lambda w: w[1] - w[0], default=None)
        if longest is not None and (longest[1] - longest[0]) < t["exposure"]:
            a, b = longest
            end_reasons = []
            if a <= 0:
                end_reasons.append("起点被计划日期边界截断")
            elif not bool(night_mask[a]):
                end_reasons.append("暮光/白昼开始")
            elif not bool(m_alt[a]):
                end_reasons.append(f"目标降至 {min_alt:.0f}° 以下")
            elif not bool(m_air[a]):
                end_reasons.append(f"空气质量超过 {max_x:g}")
            if b >= n:
                end_reasons.append("终点被计划日期边界截断（可尝试延长日期范围）")
            elif not bool(night_mask[b]):
                end_reasons.append("暮光/白昼开始")
            elif not bool(m_alt[b]):
                end_reasons.append(f"目标降至 {min_alt:.0f}° 以下")
            elif not bool(m_air[b]):
                end_reasons.append(f"空气质量超过 {max_x:g}")
            shortage = {
                "length": b - a,
                "start": a,
                "end": b,
                "why": "、".join(end_reasons) if end_reasons else "窗口本身不足",
            }

        samples = [
            {
                "minute": int(mm),
                "t": _iso(grid_start + timedelta(minutes=int(mm))),
                "alt": round(float(alt_arr[mm]), 2),
                "airmass": (None if not math.isfinite(float(x_arr[mm]))
                            else round(float(x_arr[mm]), 3)),
            }
            for mm in sample_minutes
        ]

        t.update(
            {
                "windows": windows,
                "best_alt": round(best_alt, 2),
                "best_airmass": None if math.isinf(best_x) else round(best_x, 3),
                "samples": samples,
                "no_window_reason": no_window_reason,
                "shortage": shortage,
            }
        )

    # ================================================================
    # 固定（手动拖放）的观测安排
    # ================================================================
    pins_in = payload.get("pins") or {}
    pinned: dict[str, dict] = {}
    by_id = {t["id"]: t for t in targets}
    busy: list[dict] = list(calibrations)  # 带标签的占用列表

    for tid, p in pins_in.items():
        t = by_id.get(str(tid))
        if t is None or not isinstance(p, dict):
            continue
        try:
            s = int(round(float(p.get("start_min"))))
        except (TypeError, ValueError):
            warnings.append(f"目标「{t['name']}」的固定时间无效，已忽略")
            continue
        e = s + t["exposure"]
        if s < 0 or e > n:
            warnings.append(
                f"目标「{t['name']}」的固定时间超出日期范围，已忽略"
            )
            continue
        # 必须整体落在自身可行区间内
        if not any(a <= s and e <= b for a, b in t["windows"]):
            warnings.append(
                f"目标「{t['name']}」的固定时间 {_fmt_minute(s, grid_start)} "
                f"不在其可行观测窗口内，已忽略"
            )
            continue
        # 不能与校准/其他固定时段撞车
        clash = next(
            (
                x
                for x in busy
                if s < x["end_min"] and x["start_min"] < e
            ),
            None,
        )
        if clash is not None:
            cname = (
                f"校准「{clash['label']}」"
                if clash["kind"] == "calibration"
                else f"已固定的目标「{clash['name']}」"
            )
            warnings.append(
                f"目标「{t['name']}」的固定时间与{cname}冲突，已忽略"
            )
            continue
        rec = {
            "target_id": tid,
            "name": t["name"],
            "start_min": s,
            "end_min": e,
            "start": _iso(grid_start + timedelta(minutes=s)),
            "end": _iso(grid_start + timedelta(minutes=e)),
            "kind": "target",
            "pinned": True,
        }
        pinned[tid] = rec
        busy.append(rec)

    busy_ivs = merge_intervals([[x["start_min"], x["end_min"]] for x in busy])

    # ================================================================
    # 贪心排程：优先级（小者优先）-> 曝光时长长者优先 -> 输入顺序
    # ================================================================
    scheduled_map: dict[str, dict] = {tid: rec for tid, rec in pinned.items()}
    unscheduled: list[dict] = []

    order = sorted(
        targets,
        key=lambda t: (t["priority"], -t["exposure"], t["order"]),
    )

    for t in order:
        if t["id"] in pinned:
            continue

        if t["no_window_reason"]:
            unscheduled.append(
                {"target_id": t["id"], "name": t["name"],
                 "reason": t["no_window_reason"]}
            )
            continue

        blocked_reason = None
        placed = False
        # 按时间顺序找到的第一个窗口即用于「首个冲突原因」归因
        first_window = t["windows"][0]
        for a, b in t["windows"]:
            length = b - a
            if length < t["exposure"]:
                continue
            pieces = subtract_intervals([[a, b]], busy_ivs)
            fit = next(
                (p for p in pieces if p[1] - p[0] >= t["exposure"]),
                None,
            )
            if fit is not None:
                s, e = fit[0], fit[0] + t["exposure"]
                rec = {
                    "target_id": t["id"],
                    "name": t["name"],
                    "start_min": s,
                    "end_min": e,
                    "start": _iso(grid_start + timedelta(minutes=s)),
                    "end": _iso(grid_start + timedelta(minutes=e)),
                    "kind": "target",
                    "pinned": False,
                }
                scheduled_map[t["id"]] = rec
                busy.append(rec)
                busy_ivs = merge_intervals(
                    busy_ivs + [[s, e]]
                )
                placed = True
                break
            # 足够长的原始窗口放不下 -> 找到首个占用者
            hit = next(
                (
                    x
                    for x in sorted(busy, key=lambda x: x["start_min"])
                    if x["start_min"] < b and a < x["end_min"]
                ),
                None,
            )
            if hit is not None:
                if hit["kind"] == "calibration":
                    who = f"固定校准时段「{hit['label']}」"
                elif hit.get("pinned"):
                    who = f"手动固定的目标「{hit['name']}」"
                else:
                    who = f"已安排的更高优先级目标「{hit['name']}」"
                blocked_reason = (
                    f"可行窗口 {_fmt_minute(a, grid_start)}–"
                    f"{_fmt_minute(b, grid_start)}（{length} 分钟）"
                    f"被{who} {_fmt_minute(hit['start_min'], grid_start)}–"
                    f"{_fmt_minute(hit['end_min'], grid_start)} UTC 占用，"
                    f"扣除后剩余片段不足 {t['exposure']} 分钟曝光"
                )
            else:
                blocked_reason = (
                    f"可行窗口 {_fmt_minute(a, grid_start)}–"
                    f"{_fmt_minute(b, grid_start)}（{length} 分钟）扣除占用后，"
                    f"无连续 {t['exposure']} 分钟片段"
                )
            break

        if placed:
            continue

        # 所有窗口都放不下：优先报告"被占用"原因；否则按窗口本身不足处理，
        # 并说明时间上第一个窗口的截断边界
        if blocked_reason is None:
            fa, fb = first_window
            def _edge_reason(k: int) -> str:
                if not bool(night_mask[k]):
                    return "进入白昼/不满足暮光条件"
                if not bool(m_alt[k]):
                    return f"高度降至 {min_alt:.0f}° 以下"
                if not bool(m_air[k]):
                    return f"空气质量超过 {max_x:g}"
                return ""
            boundary = []
            if fa <= 0:
                boundary.append("起点被计划日期边界截断")
            else:
                r0 = _edge_reason(fa)
                if r0:
                    boundary.append("起点处" + r0)
            if fb >= n:
                boundary.append("终点被计划日期边界截断（可尝试延长日期范围）")
            else:
                r1 = _edge_reason(fb)
                if r1:
                    boundary.append("终点处" + r1)
            blocked_reason = (
                f"首个可行窗口 {_fmt_minute(fa, grid_start)}–"
                f"{_fmt_minute(fb, grid_start)}（{fb - fa} 分钟）"
                f"短于所需 {t['exposure']} 分钟"
                + ("；" + "、".join(boundary) if boundary else "")
            )
        unscheduled.append(
            {"target_id": t["id"], "name": t["name"], "reason": blocked_reason}
        )

    # ---------------------------------------------------------------- output
    scheduled = sorted(scheduled_map.values(), key=lambda r: r["start_min"])
    targets_out = []
    for t in targets:
        targets_out.append(
            {
                "id": t["id"],
                "name": t["name"],
                "ra_deg": round(t["ra"], 4),
                "dec_deg": round(t["dec"], 4),
                "exposure_minutes": t["exposure"],
                "priority": t["priority"],
                "best_altitude": t["best_alt"],
                "best_airmass": t["best_airmass"],
                "windows": [
                    {
                        "start_min": a,
                        "end_min": b,
                        "start": _iso(grid_start + timedelta(minutes=a)),
                        "end": _iso(grid_start + timedelta(minutes=b)),
                        "minutes": b - a,
                    }
                    for a, b in t["windows"]
                ],
                "samples": t["samples"],
            }
        )

    return {
        "grid": {
            "start": _iso(grid_start),
            "end": _iso(grid_end),
            "minutes": n,
        },
        "site": {"lat": lat, "lon": lon},
        "constraints": {
            "twilight_mode": mode,
            "sun_altitude_limit": sun_limit,
            "min_altitude": min_alt,
            "max_airmass": max_x,
            "effective_min_altitude": round(eff_min_alt, 2),
        },
        "sun_bands": sun_bands,
        "targets": targets_out,
        "calibrations": calibrations,
        "scheduled": scheduled,
        "unscheduled": unscheduled,
        "warnings": warnings,
    }
