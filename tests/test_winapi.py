"""Windows API 绑定层的测试。

只在 Windows 上运行（CI 的 ubuntu / macos 任务会自动跳过）。
这些测试**真的去调用系统 API**，因此能发现"类型定义写错""结构体对齐错了"
这类光看代码发现不了的问题——例如 ``GetPerformanceInfo`` 的字段单位是
**页数而不是字节**，就是靠实际比对才确认的。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

from memscope import winapi

WINDOWS_ONLY = unittest.skipUnless(
    sys.platform == "win32", "本组测试需要 Windows API"
)


@WINDOWS_ONLY
class TestPlatformDetection(unittest.TestCase):
    def test_is_supported(self):
        self.assertTrue(winapi.is_supported())


@WINDOWS_ONLY
class TestSystemMemory(unittest.TestCase):
    def setUp(self):
        self.info = winapi.system_performance_info()

    def test_page_size_is_sane(self):
        self.assertIn(self.info.page_size, (4096, 8192, 65536))

    def test_physical_total_is_plausible(self):
        """回归测试：字段单位是页数，忘了乘 PageSize 会得到几百 KB 这种荒唐值。"""
        gb = self.info.physical_total / (1024 ** 3)
        self.assertGreater(gb, 1.0, "物理内存不可能小于 1 GB —— 单位换算错了？")
        self.assertLess(gb, 4096.0)

    def test_physical_available_within_total(self):
        self.assertGreaterEqual(self.info.physical_available, 0)
        self.assertLessEqual(self.info.physical_available, self.info.physical_total)

    def test_commit_limit_at_least_physical(self):
        self.assertGreaterEqual(self.info.commit_limit, self.info.physical_total)

    def test_commit_total_is_positive(self):
        self.assertGreater(self.info.commit_total, 0)

    def test_counts_are_positive(self):
        self.assertGreater(self.info.process_count, 0)
        self.assertGreater(self.info.thread_count, 0)
        self.assertGreater(self.info.handle_count, 0)

    def test_kernel_pools_are_plausible(self):
        mb = 1024 ** 2
        self.assertGreater(self.info.kernel_nonpaged / mb, 1)
        self.assertLess(self.info.kernel_nonpaged / mb, 100 * 1024)


@WINDOWS_ONLY
class TestProcessEnumeration(unittest.TestCase):
    def setUp(self):
        self.entries = winapi.enumerate_process_entries()

    def test_returns_processes(self):
        self.assertGreater(len(self.entries), 5)

    def test_includes_current_process(self):
        me = os.getpid()
        found = [e for e in self.entries if e.pid == me]
        self.assertEqual(len(found), 1, "应能枚举到自己")
        self.assertTrue(found[0].name)

    def test_parent_pids_are_plausible(self):
        pids = {e.pid for e in self.entries}
        # 大部分进程应能指向一个存在的父进程
        with_parent = sum(1 for e in self.entries if e.ppid in pids)
        self.assertGreater(with_parent / len(self.entries), 0.5)

    def test_pids_are_unique(self):
        pids = [e.pid for e in self.entries]
        self.assertEqual(len(pids), len(set(pids)))


@WINDOWS_ONLY
class TestPerProcessQueries(unittest.TestCase):
    def test_memory_of_current_process(self):
        counters = winapi.process_memory(os.getpid())
        self.assertIsNotNone(counters)
        self.assertGreater(counters.working_set, 0)
        self.assertGreater(counters.private_bytes, 0)
        self.assertGreaterEqual(counters.working_set, counters.private_bytes * 0.3)

    def test_memory_of_nonexistent_process_returns_none(self):
        self.assertIsNone(winapi.process_memory(0x7FFFFFF0))

    def test_exe_path_of_current_process(self):
        path = winapi.process_exe_path(os.getpid())
        self.assertIsNotNone(path)
        self.assertTrue(path.lower().endswith(".exe"))
        self.assertTrue(os.path.isfile(path))

    def test_command_line_of_current_process(self):
        line = winapi.process_command_line(os.getpid())
        self.assertIsNotNone(line)
        self.assertIn(sys.executable.split("\\")[-1].lower(), line.lower())

    def test_command_line_of_protected_process_returns_none(self):
        # PID 4 是 System，打不开很正常——不该抛异常
        result = winapi.process_command_line(4)
        self.assertTrue(result is None or isinstance(result, str))

    def test_start_time_is_in_the_past(self):
        started = winapi.process_start_time(os.getpid())
        self.assertIsNotNone(started)
        self.assertLess(started, time.time())
        # 本测试进程启动不超过 1 小时
        self.assertLess(time.time() - started, 3600)

    def test_session_id_of_current_process(self):
        session = winapi.process_session_id(os.getpid())
        self.assertIsInstance(session, int)
        self.assertGreaterEqual(session, 0)


@WINDOWS_ONLY
class TestFileVersionInfo(unittest.TestCase):
    def test_system_binary_has_version_info(self):
        root = os.environ.get("SystemRoot", r"C:\Windows")
        target = os.path.join(root, "System32", "svchost.exe")
        if not os.path.isfile(target):
            self.skipTest("找不到 svchost.exe")
        info = winapi.file_version_info(target)
        self.assertTrue(info, "系统二进制应带版本资源")
        self.assertIn("CompanyName", info)
        self.assertIn("microsoft", info["CompanyName"].lower())

    def test_missing_path_returns_empty(self):
        self.assertEqual(winapi.file_version_info(None), {})
        self.assertEqual(winapi.file_version_info(""), {})

    def test_nonexistent_file_returns_empty(self):
        self.assertEqual(winapi.file_version_info(r"C:\no\such\file.exe"), {})

    def test_values_have_no_control_characters(self):
        """回归测试：真实文件里出现过带控制字符与未替换占位符的脏值。"""
        root = os.environ.get("SystemRoot", r"C:\Windows")
        for name in ("svchost.exe", "explorer.exe", "notepad.exe"):
            target = os.path.join(root, "System32", name)
            if not os.path.isfile(target):
                continue
            for value in winapi.file_version_info(target).values():
                self.assertTrue(
                    all(ch.isprintable() for ch in value),
                    f"{name} 的版本信息里有控制字符: {value!r}",
                )


class TestVersionStringCleaning(unittest.TestCase):
    """``_clean_version_string`` 是纯函数，可以跨平台测。"""

    def test_plain_string_unchanged(self):
        self.assertEqual(winapi._clean_version_string("Steam"), "Steam")

    def test_control_characters_removed(self):
        self.assertEqual(
            winapi._clean_version_string("NVIDIA App\x00\x01\r\n"), "NVIDIA App"
        )

    def test_whitespace_collapsed(self):
        self.assertEqual(winapi._clean_version_string("A   B\t\tC"), "A B C")

    def test_template_placeholder_truncated(self):
        """真实踩到过：'NVIDIA App  <Product' 这样的脏值。"""
        self.assertEqual(
            winapi._clean_version_string("NVIDIA App  <Product"), "NVIDIA App"
        )

    def test_placeholder_only_string_becomes_empty(self):
        self.assertEqual(winapi._clean_version_string("<Product>"), "")

    def test_empty_input(self):
        self.assertEqual(winapi._clean_version_string(""), "")
        self.assertEqual(winapi._clean_version_string(None), "")


@WINDOWS_ONLY
class TestServiceEnumeration(unittest.TestCase):
    def setUp(self):
        self.services = winapi.enumerate_services()

    def test_returns_services(self):
        self.assertGreater(len(self.services), 10)

    def test_running_services_have_pids(self):
        running = [s for s in self.services if s.state == "running"]
        self.assertGreater(len(running), 5)
        self.assertTrue(all(s.pid > 0 for s in running), "运行中的服务必须有宿主 PID")

    def test_services_share_host_processes(self):
        """svchost 的意义就是一个进程承载多个服务——这个前提必须成立。"""
        from collections import Counter

        counts = Counter(s.pid for s in self.services if s.pid)
        self.assertTrue(
            any(n > 1 for n in counts.values()),
            "应有多个服务共享同一个宿主进程，否则方案 B 没有意义",
        )

    def test_has_display_names(self):
        named = [s for s in self.services if s.display_name]
        self.assertGreater(len(named), len(self.services) * 0.8)

    def test_states_are_known(self):
        known = {"running", "stopped", "paused", "start-pending", "stop-pending",
                 "continue-pending", "pause-pending", "unknown"}
        for service in self.services:
            self.assertIn(service.state, known)


@WINDOWS_ONLY
class TestMemoryCounters(unittest.TestCase):
    def test_returns_counters(self):
        counters = winapi.memory_counters()
        self.assertTrue(counters, "性能计数器应至少能读到一部分")
        self.assertIn("available_mb", counters)
        self.assertGreater(counters["available_mb"], 0)

    def test_available_mb_matches_psapi_roughly(self):
        """两个独立来源应大体一致（允许采样时间差造成的偏差）。"""
        info = winapi.system_performance_info()
        counters = winapi.memory_counters()
        if "available_mb" not in counters:
            self.skipTest("计数器不可用")
        psapi_gb = info.physical_available / (1024 ** 3)
        pdh_gb = counters["available_mb"] * 1024 * 1024 / (1024 ** 3)
        self.assertLess(
            abs(psapi_gb - pdh_gb), max(2.0, psapi_gb * 0.5),
            f"两个来源差得太多：psapi={psapi_gb:.2f}GB pdh={pdh_gb:.2f}GB",
        )

    def test_standby_counters_present(self):
        counters = winapi.memory_counters()
        for key in ("standby_core", "standby_normal", "standby_reserve"):
            self.assertIn(key, counters)


@WINDOWS_ONLY
class TestEndToEndCollection(unittest.TestCase):
    def test_take_snapshot_produces_sane_data(self):
        from memscope.collect import take_snapshot

        snapshot = take_snapshot(fast=False, with_services=True)
        self.assertGreater(len(snapshot.processes), 5)
        self.assertGreater(snapshot.system.physical_total, 0)
        self.assertGreater(len(snapshot.applications), 0)

        # 守恒：所有进程的内存必须恰好分配到一个应用里
        self.assertEqual(
            sum(p.private_bytes for p in snapshot.processes),
            sum(a.total_bytes for a in snapshot.applications),
        )

        # 本进程自己必须被归因
        me = os.getpid()
        owners = [a for a in snapshot.applications if me in a.pids]
        self.assertEqual(len(owners), 1, "每个进程必须恰好属于一个应用")

    def test_collector_reuses_cache(self):
        from memscope.collect import Collector

        collector = Collector()
        first = collector.collect_processes(fast=False)
        second = collector.collect_processes(fast=False)
        self.assertEqual(len(first), len(second))


if __name__ == "__main__":
    unittest.main()
