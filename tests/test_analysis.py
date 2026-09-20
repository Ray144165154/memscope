"""趋势分析与泄漏检测的测试。

重点验证"什么时候该报警、什么时候不该"——这是最容易产生假阳性的地方。
"""

from __future__ import annotations

import unittest

from memscope.analysis import (
    LEAK_SLOPE_MB_PER_HOUR,
    MIN_SAMPLES,
    MIN_SPAN_SECONDS,
    VERDICT_GROWING,
    VERDICT_LEAK,
    VERDICT_SHRINKING,
    VERDICT_STABLE,
    analyze_samples,
    fit_linear,
)
from memscope.sampling import AppSample, Sample

MB = 1024 ** 2


def make_samples(series: dict[str, list[tuple[float, int]]], name: str = "") -> list[Sample]:
    """把 ``{key: [(时间, 字节), ...]}`` 转成采样列表。

    用"第 i 条采样里各应用的取值"的形式组织，便于直接写期望的曲线。
    """
    if not series:
        return []
    length = max(len(points) for points in series.values())
    samples: list[Sample] = []
    for index in range(length):
        timestamp = 0.0
        apps: list[AppSample] = []
        for key, points in series.items():
            if index >= len(points):
                continue
            timestamp, value = points[index]
            apps.append(
                AppSample(
                    key=key, name=name or key, category="user",
                    processes=1, private_bytes=value, working_set=value,
                )
            )
        samples.append(Sample(timestamp=timestamp, system={}, apps=apps))
    return samples


def linear_series(
    key: str, start: int, per_hour: int, count: int, interval: float = 60.0
) -> dict[str, list[tuple[float, int]]]:
    """构造完美线性的增长序列。"""
    points = []
    for i in range(count):
        t = i * interval
        points.append((t, int(start + per_hour * (t / 3600.0))))
    return {key: points}


class TestFitLinear(unittest.TestCase):
    def test_perfect_line(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [10.0, 20.0, 30.0, 40.0]
        slope, intercept, r2 = fit_linear(xs, ys)
        self.assertAlmostEqual(slope, 10.0, places=6)
        self.assertAlmostEqual(intercept, 10.0, places=6)
        self.assertAlmostEqual(r2, 1.0, places=6)

    def test_flat_line(self):
        slope, _intercept, r2 = fit_linear([0.0, 1.0, 2.0], [5.0, 5.0, 5.0])
        self.assertAlmostEqual(slope, 0.0, places=6)
        self.assertEqual(r2, 0.0, "完全无变化时不应虚报拟合优度")

    def test_descending(self):
        slope, _intercept, _r2 = fit_linear([0.0, 1.0, 2.0], [30.0, 20.0, 10.0])
        self.assertAlmostEqual(slope, -10.0, places=6)

    def test_noisy_series_has_low_r_squared(self):
        xs = [float(i) for i in range(10)]
        ys = [10.0, -5.0, 40.0, 0.0, 25.0, -20.0, 30.0, 5.0, 15.0, -10.0]
        _slope, _intercept, r2 = fit_linear(xs, ys)
        self.assertLess(r2, 0.5, "噪声大的序列不能有高 R²")

    def test_single_point(self):
        slope, intercept, r2 = fit_linear([1.0], [42.0])
        self.assertEqual(slope, 0.0)
        self.assertEqual(intercept, 42.0)
        self.assertEqual(r2, 0.0)

    def test_empty(self):
        self.assertEqual(fit_linear([], []), (0.0, 0.0, 0.0))

    def test_all_x_identical(self):
        slope, _intercept, r2 = fit_linear([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])
        self.assertEqual(slope, 0.0)
        self.assertEqual(r2, 0.0)


class TestLeakDetection(unittest.TestCase):
    def test_clear_leak_is_flagged(self):
        """每小时涨 100 MB、持续 6 小时 —— 必须报警。"""
        data = linear_series("app", 100 * MB, 100 * MB, count=60, interval=360.0)
        trends = analyze_samples(make_samples(data, "Leaky"))
        self.assertEqual(len(trends), 1)
        trend = trends[0]
        self.assertEqual(trend.verdict, VERDICT_LEAK)
        self.assertGreater(trend.slope_mb_per_hour, LEAK_SLOPE_MB_PER_HOUR)
        self.assertGreater(trend.r_squared, 0.9)

    def test_stable_process_is_not_flagged(self):
        points = [(i * 60.0, 500 * MB + (i % 3) * MB) for i in range(40)]
        trends = analyze_samples(make_samples({"app": points}, "Stable"))
        self.assertEqual(trends[0].verdict, VERDICT_STABLE)
        self.assertFalse(trends[0].is_alert)

    def test_slow_growth_is_growing_not_leak(self):
        """每小时只涨 8 MB：算「持续增长」，但不到泄漏门槛。"""
        data = linear_series("app", 100 * MB, 8 * MB, count=60, interval=360.0)
        trends = analyze_samples(make_samples(data, "Slow"))
        self.assertEqual(trends[0].verdict, VERDICT_GROWING)

    def test_noisy_growth_is_not_a_leak_even_if_slope_is_high(self):
        """斜率大但抖动剧烈（R² 低）不能判定为泄漏。"""
        points = []
        for i in range(60):
            t = i * 360.0
            value = 100 * MB + (200 * MB if i % 2 else -100 * MB) + i * 3 * MB
            points.append((t, max(0, value)))
        trends = analyze_samples(make_samples({"app": points}, "Noisy"))
        self.assertNotEqual(
            trends[0].verdict, VERDICT_LEAK,
            "抖动大的序列不该被判为泄漏——那只是正常的内存申请/释放",
        )

    def test_shrinking_is_detected(self):
        data = linear_series("app", 900 * MB, -200 * MB, count=60, interval=360.0)
        trends = analyze_samples(make_samples(data, "Shrinking"))
        self.assertEqual(trends[0].verdict, VERDICT_SHRINKING)
        self.assertFalse(trends[0].is_alert)

    def test_small_total_growth_is_not_flagged(self):
        """斜率够快但总增长很小（刚启动的场景）不报。"""
        data = linear_series("app", 100 * MB, 100 * MB, count=MIN_SAMPLES, interval=20.0)
        trends = analyze_samples(make_samples(data, "Tiny"))
        self.assertFalse(trends[0].is_alert)


class TestInsufficientEvidence(unittest.TestCase):
    def test_too_few_samples_are_skipped(self):
        data = linear_series("app", 100 * MB, 100 * MB, count=MIN_SAMPLES - 1, interval=600.0)
        self.assertEqual(analyze_samples(make_samples(data, "Few")), [])

    def test_too_short_span_is_skipped(self):
        count = 30
        interval = (MIN_SPAN_SECONDS / 2) / count
        data = linear_series("app", 100 * MB, 100 * MB, count=count, interval=interval)
        self.assertEqual(analyze_samples(make_samples(data, "Short")), [])

    def test_empty_input(self):
        self.assertEqual(analyze_samples([]), [])

    def test_apps_with_no_samples_are_ignored(self):
        self.assertEqual(analyze_samples(make_samples({"a": []})), [])


class TestTrendProperties(unittest.TestCase):
    def test_growth_bytes(self):
        data = linear_series("app", 100 * MB, 200 * MB, count=60, interval=360.0)
        trend = analyze_samples(make_samples(data, "G"))[0]
        self.assertEqual(trend.growth_bytes, trend.last_bytes - trend.first_bytes)
        self.assertGreater(trend.growth_bytes, 0)

    def test_min_max_recorded(self):
        points = [(i * 60.0, 100 * MB + (i % 5) * 10 * MB) for i in range(40)]
        trend = analyze_samples(make_samples({"app": points}, "M"))[0]
        self.assertLessEqual(trend.min_bytes, trend.last_bytes)
        self.assertGreaterEqual(trend.max_bytes, trend.last_bytes)

    def test_sorted_with_alerts_first(self):
        data = {
            "stable": [(i * 360.0, 100 * MB) for i in range(60)],
            "leaky": [(i * 360.0, 100 * MB + i * 60 * MB) for i in range(60)],
        }
        samples = make_samples(data, "X")
        # 给每个应用不同的名字
        for sample in samples:
            for app in sample.apps:
                app.name = "Leaky" if app.key == "leaky" else "Stable"
        trends = analyze_samples(samples)
        self.assertEqual(trends[0].key, "leaky", "报警的必须排在最前")

    def test_slope_mb_per_hour_conversion(self):
        data = linear_series("app", 0, 50 * MB, count=60, interval=360.0)
        trend = analyze_samples(make_samples(data, "C"))[0]
        self.assertAlmostEqual(trend.slope_mb_per_hour, 50.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
