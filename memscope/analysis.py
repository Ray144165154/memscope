"""趋势分析：从采样数据里找出正在增长的东西。

一次快照只能告诉你"现在谁占得多"，回答不了"谁在悄悄变大"。
后者才是真正需要修的问题——一个 200 MB 的进程如果每小时涨 100 MB，
一天后就是 2.6 GB。

方法：对每个应用的私有字节序列做**最小二乘线性回归**，用斜率表示增长
速度，用决定系数 R² 表示"这个增长有多像一条直线"。

为什么用 R² 而不是只看斜率？
    内存占用天然是噪声很大的信号——程序申请/释放内存的锯齿会让斜率
    随机浮动。一段平坦但抖动的序列也可能拟合出不为零的斜率。
    R² 接近 1 说明增长是**持续的、单调的**，这才像泄漏；
    R² 很低说明只是波动，不该报警。

同时要求最小样本数与最小时间跨度——样本太少时任何拟合都不可信。
"""

from __future__ import annotations

from dataclasses import dataclass

from .sampling import Sample, iter_app_series

__all__ = ["Trend", "Verdict", "fit_linear", "analyze_samples", "MIN_SAMPLES", "MIN_SPAN_SECONDS"]


# 判定门槛（集中定义，便于解释与调整）
MIN_SAMPLES = 8             # 少于这么多个采样点不做结论
MIN_SPAN_SECONDS = 120.0    # 跨度不足这么久不做结论
LEAK_SLOPE_MB_PER_HOUR = 20.0   # 每小时增长超过这么多视为泄漏
GROWING_SLOPE_MB_PER_HOUR = 5.0  # 每小时增长超过这么多视为增长
LEAK_R_SQUARED = 0.70       # 拟合优度门槛
LEAK_MIN_GROWTH = 30 * 1024 ** 2  # 总增长至少这么多字节才值得报


class Verdict(str):
    """趋势判定结果。"""


VERDICT_LEAK = "leak"            # 疑似泄漏
VERDICT_GROWING = "growing"      # 在增长，但还不足以断定泄漏
VERDICT_STABLE = "stable"        # 稳定
VERDICT_SHRINKING = "shrinking"  # 在减小
VERDICT_INSUFFICIENT = "insufficient"  # 样本不足


@dataclass(slots=True)
class Trend:
    """一个应用的占用趋势。"""

    key: str
    name: str
    category: str
    samples: int
    span_seconds: float
    first_bytes: int
    last_bytes: int
    min_bytes: int
    max_bytes: int
    slope_bytes_per_hour: float
    r_squared: float
    verdict: str

    @property
    def growth_bytes(self) -> int:
        return self.last_bytes - self.first_bytes

    @property
    def slope_mb_per_hour(self) -> float:
        return self.slope_bytes_per_hour / (1024 ** 2)

    @property
    def is_alert(self) -> bool:
        return self.verdict in (VERDICT_LEAK, VERDICT_GROWING)


def fit_linear(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """最小二乘拟合 ``y = slope*x + intercept``，返回 ``(slope, intercept, r²)``。

    不依赖 numpy——手写这几行公式即可，也符合本项目零依赖的定位。
    """
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    syy = sum((y - mean_y) ** 2 for y in ys)

    if sxx == 0:
        return 0.0, mean_y, 0.0

    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    # 全部点重合时 syy == 0，无法定义拟合优度；此时视为"完全稳定"
    r_squared = 0.0 if syy == 0 else (sxy * sxy) / (sxx * syy)
    return slope, intercept, r_squared


def _verdict(
    slope_bytes_per_hour: float,
    r_squared: float,
    growth: int,
) -> str:
    """给出趋势判定。

    ⚠️ **单位必须换算。** 回归出来的斜率是「字节/小时」，
    而门槛常量是「MB/小时」——直接把两者相比，会让 8 MB/小时 的温和增长
    （8,388,608 字节）看起来远超 20 的门槛，于是**任何增长都被误判为泄漏**。
    这是回归测试抓到的真实缺陷。
    """
    slope_mb = slope_bytes_per_hour / (1024 ** 2)

    if abs(growth) < LEAK_MIN_GROWTH:
        return VERDICT_STABLE
    if slope_mb <= -GROWING_SLOPE_MB_PER_HOUR:
        return VERDICT_SHRINKING
    if slope_mb >= LEAK_SLOPE_MB_PER_HOUR and r_squared >= LEAK_R_SQUARED:
        return VERDICT_LEAK
    if slope_mb >= GROWING_SLOPE_MB_PER_HOUR:
        return VERDICT_GROWING
    return VERDICT_STABLE


def analyze_samples(samples: list[Sample]) -> list[Trend]:
    """分析采样序列，返回按严重程度与增长量排序的趋势列表。"""
    if not samples:
        return []

    series = iter_app_series(samples)
    trends: list[Trend] = []

    for key, points in series.items():
        points.sort(key=lambda item: item[0])
        if len(points) < MIN_SAMPLES:
            continue

        times = [p[0] for p in points]
        values = [float(p[1]) for p in points]
        name = points[-1][2]

        span = times[-1] - times[0]
        if span < MIN_SPAN_SECONDS:
            continue

        # 时间以"小时"为单位，斜率就直接是字节/小时
        hours = [(t - times[0]) / 3600.0 for t in times]
        slope, _intercept, r_squared = fit_linear(hours, values)

        first, last = int(values[0]), int(values[-1])
        category = ""
        for sample in samples:
            for app in sample.apps:
                if app.key == key:
                    category = app.category
                    break
            if category:
                break

        trends.append(
            Trend(
                key=key,
                name=name,
                category=category,
                samples=len(points),
                span_seconds=span,
                first_bytes=first,
                last_bytes=last,
                min_bytes=int(min(values)),
                max_bytes=int(max(values)),
                slope_bytes_per_hour=slope,
                r_squared=r_squared,
                verdict=_verdict(slope, r_squared, last - first),
            )
        )

    order = {
        VERDICT_LEAK: 0,
        VERDICT_GROWING: 1,
        VERDICT_STABLE: 2,
        VERDICT_SHRINKING: 3,
        VERDICT_INSUFFICIENT: 4,
    }
    trends.sort(key=lambda t: (order.get(t.verdict, 9), -t.growth_bytes))
    return trends


def total_by_verdict(trends: list[Trend]) -> dict[str, int]:
    """按判定结果统计数量。"""
    counts: dict[str, int] = {}
    for trend in trends:
        counts[trend.verdict] = counts.get(trend.verdict, 0) + 1
    return counts
