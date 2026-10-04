"""球面天文计算：时间、恒星时、目标与太阳的地平坐标、空气质量。

全部基于简化但精度足够（<0.1°）的球面公式，
太阳位置采用低精度天文历书算法（见 Meeus, *Astronomical Formulae for Calculators*）。
所有时间均为带时区的 UTC（datetime(tzinfo=timezone.utc)）。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

DEG = math.pi / 180.0


class PlanError(ValueError):
    """输入或计算过程中的可向用户展示的错误。"""


# 暮光模式 -> 太阳天顶方向高度角下限（度）
TWILIGHT_MODES = {
    "night": -18.0,          # 黑夜：太阳低于 -18°
    "astronomical": -12.0,   # 天文晨昏蒙影结束
    "nautical": -6.0,        # 航海晨昏蒙影结束
    "civil": 0.0,            # 民用晨昏蒙影结束
}


def julian_date(dt: datetime) -> float:
    """UTC datetime -> 儒略日 JD。"""
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    day_frac = (dt.hour + dt.minute / 60.0 + dt.second / 3600.0) / 24.0
    return (
        math.floor(365.25 * (y + 4716))
        + math.floor(30.6001 * (m + 1))
        + dt.day
        + day_frac
        + b
        - 1524.5
    )


def gmst_degrees(dt: datetime) -> float:
    """格林尼治平恒星时（度，0-360）。

    使用 USNO 多项式（IAU 1982），以 JD(UT1) 计。
    """
    jd = julian_date(dt)
    t = (jd - 2451545.0) / 36525.0
    # 单位：度
    g = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - t * t * t / 38710000.0
    )
    return g % 360.0


def equatorial_to_horizontal(
    dt: datetime,
    ra_deg: float,
    dec_deg: float,
    lat_deg: float,
    lon_deg: float,
) -> tuple[float, float]:
    """赤道坐标(ICRS/J2000 近似) -> 地平坐标 (alt_deg, az_deg)。

    忽略自行、章动与大气折射；方位角自正北向东量度（0=北, 90=东）。
    无日期网格、无迭代，逐分钟调用足够快。
    """
    phi = lat_deg * DEG
    dec = dec_deg * DEG
    ha = ((gmst_degrees(dt) + lon_deg - ra_deg) % 360.0) * DEG

    sin_alt = math.sin(dec) * math.sin(phi) + math.cos(dec) * math.cos(phi) * math.cos(ha)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)

    # 用 atan2 求方位角，避免近子午圈时退化
    sin_az = -math.cos(dec) * math.sin(ha) / max(1e-9, math.cos(alt))
    cos_az = (
        math.sin(dec) - math.sin(alt) * math.sin(phi)
    ) / max(1e-9, math.cos(alt) * math.cos(phi))
    az = (math.atan2(sin_az, cos_az)) / DEG % 360.0
    return alt / DEG, az


def airmass(alt_deg: float) -> float:
    """平面平行大气近似的空气质量 X = sec z；高度 <= 0 时返回 inf。"""
    if alt_deg <= 0.0:
        return math.inf
    return 1.0 / math.cos((90.0 - alt_deg) * DEG)


# ---------------------------------------------------------------------------
# 太阳低精度历书
# ---------------------------------------------------------------------------

def sun_ra_dec(dt: datetime) -> tuple[float, float]:
    """太阳视赤经/赤纬（度），低精度历书，误差约 0.01°。"""
    jd = julian_date(dt)
    n = jd - 2451545.0
    # 平黄经
    l = (280.460 + 0.9856474 * n) % 360.0
    # 平近点角
    g = (357.528 + 0.9856003 * n) % 360.0
    gr = g * DEG
    # 黄经（含中心差 + 章动/光行差常数 1.915/0.020/1.797 修正）
    lam = (l + 1.915 * math.sin(gr) + 0.020 * math.sin(2 * gr)) * DEG
    # 黄赤交角
    eps = (23.439 - 0.0000004 * n) * DEG
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)) / DEG % 360.0
    dec = math.asin(math.sin(eps) * math.sin(lam)) / DEG
    return ra, dec


def sun_altitude(dt: datetime, lat_deg: float, lon_deg: float) -> float:
    """给定时刻/地点的太阳高度角（度）。"""
    ra, dec = sun_ra_dec(dt)
    alt, _ = equatorial_to_horizontal(dt, ra, dec, lat_deg, lon_deg)
    return alt


# ---------------------------------------------------------------------------
# 分钟网格
# ---------------------------------------------------------------------------

def minute_grid(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """生成 [start, end) 内每分钟一个的 UTC datetime 网格（闭开区间）。"""
    if end_utc <= start_utc:
        raise PlanError("结束时间必须晚于开始时间")
    return [
        start_utc + timedelta(minutes=i)
        for i in range(int((end_utc - start_utc).total_seconds() // 60))
    ]
