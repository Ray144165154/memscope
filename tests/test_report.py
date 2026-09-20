"""报告渲染的测试。

HTML 报告有几条硬性要求必须守住：
  * 自包含——不能引用任何外部资源（否则离线打不开）
  * 转义——应用名里可能出现 ``<`` ``>`` ``&``，不转义会破坏页面
  * 不制造焦虑——绝不能出现把"空闲内存"单独渲染成几乎空掉的红色进度条
"""

from __future__ import annotations

import re
import unittest

from memscope.diagnose import diagnose
from memscope.model import (
    Application,
    AttributionResult,
    Category,
    Confidence,
    ProcessRecord,
    Role,
    Snapshot,
    SystemMemory,
)
from memscope.report import human_bytes, human_mb, render_html, render_text

GB = 1024 ** 3
MB = 1024 ** 2


def snapshot(
    apps: list[Application] | None = None,
    processes: list[ProcessRecord] | None = None,
    **memory_kw,
) -> Snapshot:
    base = dict(
        physical_total=16 * GB,
        physical_available=4 * GB,
        commit_total=20 * GB,
        commit_limit=32 * GB,
        standby_normal=3 * GB,
        free_and_zero=50 * MB,
        kernel_paged=600 * MB,
        kernel_nonpaged=800 * MB,
        handle_count=100000,
        process_count=300,
        thread_count=8000,
    )
    base.update(memory_kw)
    apps = apps if apps is not None else [
        Application(key="steam", name="Steam", category=Category.USER,
                    pids=[1, 2], private_bytes=900 * MB, working_set=1000 * MB,
                    confidence=Confidence.HIGH, role=Role.PRODUCT),
        Application(key="sys", name="RuntimeBroker.exe", category=Category.SYSTEM,
                    pids=[3], private_bytes=40 * MB, working_set=45 * MB,
                    confidence=Confidence.HIGH, role=Role.SYSTEM),
    ]
    processes = processes if processes is not None else [
        ProcessRecord(pid=1, ppid=0, name="steam.exe"),
        ProcessRecord(pid=2, ppid=1, name="steamwebhelper.exe"),
    ]
    return Snapshot(
        timestamp=1700000000.0,
        system=SystemMemory(**base),
        processes=processes,
        attribution=AttributionResult(
            applications=apps,
            explanations={1: ["按产品签名合并：Valve Corporation / Steam"]},
        ),
    )


class TestFormatting(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(1024), "1.00 KB")
        self.assertEqual(human_bytes(1024 ** 2), "1.00 MB")
        self.assertEqual(human_bytes(3 * 1024 ** 3), "3.00 GB")

    def test_human_bytes_negative(self):
        self.assertTrue(human_bytes(-1024 ** 3).startswith("-"))

    def test_human_mb(self):
        self.assertEqual(human_mb(500 * MB), "500 MB")


class TestTextReport(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot()
        self.text = render_text(self.snap, diagnose(self.snap.system, self.snap.applications))

    def test_has_all_sections(self):
        for heading in ("memscope", "系统内存", "诊断结论", "应用占用排行",
                        "系统组件", "说明"):
            self.assertIn(heading, self.text, f"缺少章节 {heading}")

    def test_shows_physical_and_commit(self):
        self.assertIn("物理内存总量", self.text)
        self.assertIn("提交量", self.text)

    def test_commit_excess_is_stated(self):
        self.assertIn("超出物理内存", self.text)
        self.assertIn("页面文件", self.text)

    def test_explains_standby_is_reclaimable(self):
        self.assertIn("待机缓存", self.text)
        self.assertIn("随时可用", self.text)

    def test_states_tool_does_not_free_memory(self):
        self.assertIn("不「释放内存」", self.text)

    def test_system_components_collapsed_by_default(self):
        self.assertIn("--all", self.text)

    def test_system_detail_shown_with_flag(self):
        text = render_text(self.snap, diagnose(self.snap.system, self.snap.applications),
                           show_system=True)
        self.assertIn("RuntimeBroker.exe", text)

    def test_explain_section(self):
        text = render_text(self.snap, None, explain_pids=[1])
        self.assertIn("归因依据", text)
        self.assertIn("Valve Corporation", text)
        self.assertIn("Steam", text)

    def test_explain_unknown_pid(self):
        text = render_text(self.snap, None, explain_pids=[999999])
        self.assertIn("不存在", text)

    def test_tree_repairs_shown_when_present(self):
        snap = snapshot()
        snap.attribution.tree_repairs = ["PID 100 与父进程 200 的启动时间矛盾"]
        self.assertIn("进程树修复", render_text(snap, None))

    def test_empty_applications_does_not_crash(self):
        snap = snapshot(apps=[])
        text = render_text(snap, None)
        self.assertIn("memscope", text)

    def test_no_trailing_whitespace_issue(self):
        self.assertTrue(self.text.endswith("\n") or not self.text.endswith(" "))


class TestHtmlReport(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot()
        self.html = render_html(self.snap, diagnose(self.snap.system, self.snap.applications))

    def test_is_a_complete_document(self):
        self.assertTrue(self.html.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", self.html)
        self.assertIn('charset="utf-8"', self.html)

    def test_self_contained_no_external_resources(self):
        """离线必须能打开——不能引用任何外部资源。"""
        self.assertNotIn("<script", self.html.lower())
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)
        self.assertNotIn("<link", self.html.lower())

    def test_has_inline_svg_chart(self):
        self.assertIn("<svg", self.html)
        self.assertIn("</svg>", self.html)

    def test_renders_memory_segments(self):
        self.assertIn("使用中", self.html)
        self.assertIn("待机缓存", self.html)

    def test_explains_standby(self):
        self.assertIn("随时可以被立即回收", self.html)

    def test_escapes_application_names(self):
        apps = [
            Application(key="x", name="<script>alert(1)</script>",
                        category=Category.USER, pids=[1], private_bytes=100 * MB),
            Application(key="y", name='Quote" & Amp & <tag>',
                        category=Category.USER, pids=[2], private_bytes=50 * MB),
        ]
        html = render_html(snapshot(apps=apps), [])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;", html)

    def test_memory_bar_uses_neutral_colour_for_free_space(self):
        """空闲段要用中性灰，不能用警示红。

        典型的"内存监控"会把空闲内存画成一条几乎空掉的红色进度条，
        暗示用户"内存快用光了"——那是制造焦虑，本工具刻意不这么做。
        """
        html = render_html(snapshot(apps=[], free_and_zero=1 * MB), [])
        svg = html[html.index("<svg") : html.index("</svg>")]
        self.assertNotIn("#e41e3f", svg, "内存条里不能出现警示红")
        self.assertIn("#dfe1e5", svg, "空闲段应使用中性灰")

    def test_memory_bar_labels_standby_as_reclaimable(self):
        html = render_html(snapshot(), [])
        svg = html[html.index("<svg") : html.index("</svg>")]
        self.assertIn("待机缓存", svg)

    def test_empty_snapshot_renders(self):
        html = render_html(snapshot(apps=[]), [])
        self.assertIn("memscope", html)

    def test_trends_section_renders(self):
        from memscope.analysis import Trend

        trend = Trend(
            key="leaky", name="Leaky App", category="user", samples=60,
            span_seconds=3600.0, first_bytes=100 * MB, last_bytes=900 * MB,
            min_bytes=100 * MB, max_bytes=900 * MB,
            slope_bytes_per_hour=800 * MB, r_squared=0.98, verdict="leak",
        )
        html = render_html(self.snap, [], [trend])
        self.assertIn("增长趋势", html)
        self.assertIn("Leaky App", html)
        self.assertIn("疑似泄漏", html)

    def test_no_alerts_message_when_clean(self):
        html = render_html(self.snap, [], [])
        self.assertIn("没有发现持续增长的进程", html)


class TestHtmlStructure(unittest.TestCase):
    def test_svg_viewbox_is_well_formed(self):
        html = render_html(snapshot(), [])
        match = re.search(r'viewBox="([^"]+)"', html)
        self.assertIsNotNone(match)
        parts = match.group(1).split()
        self.assertEqual(len(parts), 4)
        for part in parts:
            float(part)

    def test_bar_widths_are_numeric(self):
        html = render_html(snapshot(), [])
        for width in re.findall(r"width:([\d.]+)%", html):
            value = float(width)
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)


if __name__ == "__main__":
    unittest.main()
