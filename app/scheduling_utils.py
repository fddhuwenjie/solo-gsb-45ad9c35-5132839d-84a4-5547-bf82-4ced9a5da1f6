"""目标/约束输入解析与区间工具。

区间统一表示为 [start_epoch_minute, end_epoch_minute) 的半开整数区间，
单位为自计划起点（当天 00:00 UTC）起算的分钟数。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .astronomy import DEG, PlanError, TWILIGHT_MODES

# 计划跨度上限（分钟），14 天
MAX_SPAN_MINUTES = 14 * 24 * 60
# 目标数量上限
MAX_TARGETS = 200
# 校准时段数量上限
MAX_CALIBRATIONS = 50

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?Z?$"
)


def parse_utc(value: str, end_of_day: bool = False) -> datetime:
    """解析 ISO 风格 UTC 时间；只给日期时取 00:00（或 23:59）。"""
    if not isinstance(value, str):
        raise PlanError("日期格式不正确")
    m = _ISO_RE.match(value.strip())
    if not m:
        raise PlanError(f"无法解析 UTC 时间：{value!r}（示例 2026-09-24 或 2026-09-24T20:00Z）")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4) is not None:
        hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
    elif end_of_day:
        hh, mm, ss = 23, 59, 0
    else:
        hh, mm, ss = 0, 0, 0
    try:
        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
    except ValueError as exc:
        raise PlanError(f"非法日期：{value!r}（{exc}）") from exc
    return dt


def parse_coord(value, kind: str) -> float:
    """解析十进制经/纬度，支持可选的 N/S/E/W 后缀。kind: 'lat'|'lon'。"""
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str):
        s = value.strip().upper().replace("°", "")
        sign = 1.0
        if s[-1:] in ("N", "E"):
            s = s[:-1]
        elif s[-1:] in ("S", "W"):
            s = s[:-1]
            sign = -1.0
        try:
            v = sign * float(s.strip())
        except ValueError as exc:
            raise PlanError(f"无法解析坐标：{value!r}") from exc
    else:
        raise PlanError(f"无法解析坐标：{value!r}")
    lo, hi = (-90, 90) if kind == "lat" else (-180, 180)
    name = "纬度" if kind == "lat" else "经度"
    if not (lo <= v <= hi):
        raise PlanError(f"{name}超出范围 [{lo}, {hi}]：{v}")
    return v


def _parse_dms(parts: list[str], unit_deg: float) -> float:
    """度(时):分:秒 形式 -> 度（或小时对应的度）。parts 可为 1~3 段。"""
    vals = []
    for p in parts:
        vals.append(float(p))
    while len(vals) < 3:
        vals.append(0.0)
    d, mn, sec = vals
    sign = -1.0 if d < 0 else 1.0
    return sign * (abs(d) + mn / 60.0 + sec / 3600.0) * unit_deg


def parse_ra(value) -> float:
    """解析赤经为度（0-360）。

    支持：
      - 纯数字 > 24 或带 'd' 后缀：度数；
      - 纯数字 <= 24：小时（×15）；
      - ``10h41m30s`` / ``10 41 30``（小时:分:秒）；
      - ``160 25 30`` 三段但末段带 d：按度分秒。
    """
    if isinstance(value, (int, float)):
        v = float(value)
        v *= 15.0 if abs(v) <= 24 else 1.0
        return v % 360.0
    if not isinstance(value, str) or not value.strip():
        raise PlanError("赤经不能为空")
    s = value.strip().lower().replace("°", "d").replace("′", "m").replace("″", "s")
    is_deg = s.endswith("d") or "d" in s
    s2 = re.sub(r"[hms]", " ", s).strip()
    parts = [p for p in re.split(r"[\s:]+", s2) if p]
    try:
        if len(parts) > 1:
            v = _parse_dms(parts, 1.0 if is_deg else 15.0)
        else:
            v = float(parts[0])
            v *= 1.0 if (is_deg or abs(v) > 24) else 15.0
    except ValueError as exc:
        raise PlanError(f"无法解析赤经：{value!r}（示例 10h41m30s 或 160.39°）") from exc
    return v % 360.0


def parse_dec(value) -> float:
    """解析赤纬为度（-90..90），支持 -20d52m 或十进制。"""
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str) and value.strip():
        s = value.strip().upper().replace("°", "D").replace("′", "M").replace("″", "S")
        neg = s.startswith("-")
        s2 = re.sub(r"[DMS]", " ", s).strip()
        parts = [p for p in re.split(r"[\s:]+", s2) if p]
        try:
            if len(parts) > 1:
                v = _parse_dms(parts, 1.0)
            else:
                v = float(parts[0])
                v = -v if neg and v > 0 else v
        except ValueError as exc:
            raise PlanError(f"无法解析赤纬：{value!r}（示例 -20d52m 或 -20.87）") from exc
    else:
        raise PlanError("赤纬不能为空")
    if not (-90.0 <= v <= 90.0):
        raise PlanError(f"赤纬超出范围 [-90, 90]：{v}")
    return v


# ---------------------------------------------------------------------------
# 区间运算
# ---------------------------------------------------------------------------

def merge_intervals(ivs: list[list[int]]) -> list[list[int]]:
    """合并相交/相邻的半开整数区间。"""
    if not ivs:
        return []
    ivs = sorted(([int(a), int(b)] for a, b in ivs if b > a))
    out = [ivs[0][:]]
    for a, b in ivs[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def subtract_intervals(
    base: list[list[int]], blocked: list[list[int]]
) -> list[list[int]]:
    """从 base 中扣除 blocked（都须已合并）。"""
    result: list[list[int]] = []
    for a, b in base:
        cur = a
        for c, d in blocked:
            if d <= cur:
                continue
            if c >= b:
                break
            if c > cur:
                result.append([cur, min(c, b)])
            cur = max(cur, d)
            if cur >= b:
                break
        if cur < b:
            result.append([cur, b])
    return merge_intervals(result)


def intersect_intervals(
    a: list[list[int]], b: list[list[int]]
) -> list[list[int]]:
    out: list[list[int]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def mask_to_intervals(mask: list[bool]) -> list[list[int]]:
    """布尔逐分钟数组 -> True 的连续半开区间。"""
    out: list[list[int]] = []
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i + 1
            while j < n and mask[j]:
                j += 1
            out.append([i, j])
            i = j
        else:
            i += 1
    return out
