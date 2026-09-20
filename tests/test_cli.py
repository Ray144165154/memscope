"""命令行接口的测试。

刻意**不**在测试里真的跑一次完整采集——那会依赖机器的实时状态，
既慢又不稳定。这里只测参数解析、序列化这类确定性的部分；
真正的采集由 ``test_winapi.py`` 的端到端用例覆盖。
"""

from __future__ import annotations

import io
import json
import sys
import unittest

from memscope.cli import (
    _snapshot_to_dict,
    _verdict_label,
    build_parser,
)
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

MB = 1024 ** 2
GB = 1024 ** 3


def demo_snapshot() -> Snapshot:
    return Snapshot(
        timestamp=1700000000.0,
        system=SystemMemory(
            page_size=4096,
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
        ),
        processes=[
            ProcessRecord(pid=1, ppid=0, name="steam.exe", private_bytes=900 * MB),
            ProcessRecord(pid=2, ppid=1, name="steamwebhelper.exe", private_bytes=100 * MB),
        ],
        attribution=AttributionResult(
            applications=[
                Application(
                    key="product:valve corporation|steam", name="Steam",
                    category=Category.USER, pids=[1, 2],
                    private_bytes=1000 * MB, working_set=1100 * MB,
                    confidence=Confidence.HIGH, role=Role.PRODUCT,
                    product="Steam", company="Valve Corporation",
                )
            ],
            explanations={2: ["共享运行时向上穿透"]},
            tree_repairs=["PID 9 的父子关系被判定为 PID 重用"],
        ),
    )


class TestArgumentParsing(unittest.TestCase):
    def test_no_arguments_gives_snapshot_mode(self):
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)

    def test_snapshot_flags(self):
        args = build_parser().parse_args(["--all", "-n", "5", "--json", "--fast"])
        self.assertTrue(args.all)
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.json)
        self.assertTrue(args.fast)

    def test_explain_is_repeatable(self):
        args = build_parser().parse_args(["--explain", "100", "--explain", "200"])
        self.assertEqual(args.explain, [100, 200])

    def test_watch_subcommand(self):
        args = build_parser().parse_args(["watch", "-i", "10", "-d", "60", "-o", "x.jsonl"])
        self.assertEqual(args.command, "watch")
        self.assertEqual(args.interval, 10.0)
        self.assertEqual(args.duration, 60.0)
        self.assertEqual(args.output, "x.jsonl")

    def test_watch_defaults_to_full_collection(self):
        """回归测试：--fast 会让应用归因碎片化，绝不能是默认值。

        曾经的实现里 watch 默认带 fast=True，结果是采到的数据里同一个软件
        被拆成几十条记录，泄漏检测随之失效。
        """
        args = build_parser().parse_args(["watch"])
        self.assertFalse(args.fast, "watch 默认必须走完整采集")

    def test_analyze_subcommand(self):
        args = build_parser().parse_args(["analyze", "samples.jsonl", "-n", "3"])
        self.assertEqual(args.command, "analyze")
        self.assertEqual(args.file, "samples.jsonl")
        self.assertEqual(args.limit, 3)

    def test_version_flag_exits(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--version"])

    def test_help_flag_exits(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--help"])


class TestSnapshotSerialization(unittest.TestCase):
    def setUp(self):
        self.payload = _snapshot_to_dict(demo_snapshot())

    def test_is_json_serializable(self):
        text = json.dumps(self.payload, ensure_ascii=False)
        self.assertIn("Steam", text)

    def test_top_level_keys(self):
        for key in ("version", "timestamp", "system", "processes", "applications",
                    "tree_repairs"):
            self.assertIn(key, self.payload)

    def test_system_section_is_flat_numbers(self):
        for key, value in self.payload["system"].items():
            self.assertIsInstance(value, (int, float), f"{key} 应为数字")

    def test_commit_excess_reported(self):
        self.assertEqual(
            self.payload["system"]["commit_exceeds_physical"], 4 * GB
        )

    def test_application_entries(self):
        app = self.payload["applications"][0]
        self.assertEqual(app["name"], "Steam")
        self.assertEqual(app["category"], "user")
        self.assertEqual(app["pids"], [1, 2])
        self.assertEqual(app["process_count"], 2)
        self.assertEqual(app["confidence"], "high")
        self.assertEqual(app["role"], "product")

    def test_process_counts(self):
        self.assertEqual(self.payload["processes"], 2)
        self.assertEqual(self.payload["accessible_processes"], 2)

    def test_tree_repairs_present(self):
        self.assertEqual(len(self.payload["tree_repairs"]), 1)

    def test_empty_snapshot_serializes(self):
        payload = _snapshot_to_dict(Snapshot(timestamp=0.0, system=SystemMemory()))
        self.assertEqual(payload["applications"], [])
        self.assertIsInstance(json.dumps(payload), str)


class TestVerdictLabels(unittest.TestCase):
    def test_all_verdicts_have_labels(self):
        from memscope.analysis import (
            VERDICT_GROWING,
            VERDICT_LEAK,
            VERDICT_SHRINKING,
            VERDICT_STABLE,
        )

        for verdict in (VERDICT_LEAK, VERDICT_GROWING, VERDICT_STABLE, VERDICT_SHRINKING):
            label = _verdict_label(verdict)
            self.assertNotEqual(label, verdict, f"{verdict} 缺少中文标签")

    def test_unknown_verdict_passes_through(self):
        self.assertEqual(_verdict_label("weird"), "weird")


class TestOutputEncoding(unittest.TestCase):
    def test_configure_does_not_break_streams(self):
        from memscope.cli import _configure_output_encoding

        before = sys.stdout
        _configure_output_encoding()
        self.assertIs(sys.stdout, before)
        self.assertIsNotNone(sys.stdout.encoding)

    def test_write_handles_unicode(self):
        from memscope.cli import _write

        captured = io.StringIO()
        original = sys.stdout
        sys.stdout = captured
        try:
            _write("中文测试 ✔")
        finally:
            sys.stdout = original
        self.assertIn("中文测试", captured.getvalue())


class TestHelpOutputEncoding(unittest.TestCase):
    """回归测试：帮助文本里有中文，在非 UTF-8 输出流上必须不崩。

    CI 的 Windows 任务就是在这里挂的——argparse 打印中文帮助时抛
    ``UnicodeEncodeError``，因为 ``build_parser()`` 自己不会重配输出编码。
    真实的命令行走 ``main()`` 没问题（那里配了），但任何直接使用
    ``build_parser()`` 的调用方（测试、嵌进别的程序）都会踩到。
    """

    @staticmethod
    def _ascii_only_stream():
        """一个只能输出 ASCII 的流，模拟 Windows 上重定向后的 cp1252。

        必须用 ``TextIOWrapper`` 而不是 ``codecs.getwriter``：只有前者有
        ``reconfigure()``，而 ``_configure_output_encoding()`` 正是靠它来修编码的。
        用 StreamWriter 会让这个测试变成"验证一个不可能被修复的场景"。
        """
        return io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")

    def test_help_does_not_crash_on_ascii_only_stdout(self):
        original = sys.stdout
        sys.stdout = self._ascii_only_stream()
        try:
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--help"])
            # build_parser() 应当已把编码重配为 UTF-8，写入才会成功
            self.assertEqual(sys.stdout.encoding.lower().replace("-", ""), "utf8")
        finally:
            sys.stdout = original

    def test_invalid_argument_usage_does_not_crash(self):
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout = self._ascii_only_stream()
        sys.stderr = self._ascii_only_stream()
        try:
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--definitely-not-a-flag"])
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_help_text_really_contains_chinese(self):
        """确认帮助里确实有中文——否则这个回归测试就没有保护意义。"""
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--help"])
        self.assertTrue(
            any("\u4e00" <= ch <= "\u9fff" for ch in buffer.getvalue()),
            "帮助文本应包含中文，否则本回归测试失去意义",
        )


class TestPlatformGuard(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "本用例验证非 Windows 的行为")
    def test_returns_2_on_non_windows(self):
        from memscope.cli import main

        self.assertEqual(main([]), 2)

    @unittest.skipUnless(sys.platform == "win32", "仅在 Windows 上有意义")
    def test_parser_works_on_windows(self):
        args = build_parser().parse_args(["--json"])
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
