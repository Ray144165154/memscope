"""归因算法测试。

每个用例都对应一个**真实的失败模式**——这些不是为覆盖率写的，
而是"如果不这样处理，归因就会给出错误答案"的具体场景。
"""

from __future__ import annotations

import unittest

from fixtures import (
    MB,
    ea_topology,
    fake_parent_topology,
    proc,
    steam_topology,
    svchost_with_services,
    webview2_topology,
)

from memscope.attribution import MAX_ANCESTOR_DEPTH, attribute
from memscope.classify import install_root, script_hint
from memscope.model import Category, Confidence, Role


def find_app(result, needle: str):
    """按名字片段找应用。"""
    for app in result.applications:
        if needle.lower() in app.name.lower():
            return app
    return None


class TestSharedRuntimePassthrough(unittest.TestCase):
    """共享运行时必须向上穿透，把开销算到真正的宿主头上。

    这是归因里最容易做错、也最影响结果正确性的一类。真实场景：
    WebView2 的 21 个进程可执行文件路径**完全相同**，但分属好几个宿主。
    """

    def test_webview2_grouped_under_host_not_edge(self):
        result = attribute(webview2_topology())

        self.assertEqual(len(result.applications), 1, "5 个进程应归成一个应用")
        app = result.applications[0]
        self.assertIn("AweSun", app.name)
        self.assertNotIn("Edge", app.name, "绝不能归成 Microsoft Edge")
        self.assertNotIn("WebView2", app.name)
        self.assertEqual(app.process_count, 5)

    def test_webview2_memory_counted_toward_host(self):
        procs = webview2_topology()
        expected = sum(p.private_bytes for p in procs)
        result = attribute(procs)
        self.assertEqual(result.applications[0].total_bytes, expected)

    def test_passthrough_chain_is_recorded_as_evidence(self):
        result = attribute(webview2_topology())
        renderer_pid = 18600 + 1400
        lines = result.explanations[renderer_pid]
        joined = " ".join(lines)
        self.assertIn("向上穿透", joined)
        self.assertIn("18600", joined, "证据里要写明归因到哪个宿主")

    def test_deep_chain_walks_multiple_levels(self):
        """renderer → browser → host 要走两层才到宿主。"""
        result = attribute(webview2_topology())
        app = result.applications[0]
        self.assertIn(18600, app.pids)          # 宿主
        self.assertIn(18600 + 3524, app.pids)   # 浏览器进程
        self.assertIn(18600 + 1400, app.pids)   # 渲染进程（隔了两层）

    def test_multiple_hosts_sharing_webview2_stay_separate(self):
        """两个不同的宿主各自有 WebView2 —— 必须分成两个应用。"""
        procs = webview2_topology(host_pid=18600, host_name="AweSun.exe")
        other = webview2_topology(host_pid=40000, host_name="OtherApp.exe")
        # 把第二个宿主换成另一款产品（拓扑里的父进程 PID 已按 host_pid 生成，无需改）
        other[0].product = "OtherApp"
        other[0].company = "Other Corp"
        other[0].exe_path = r"C:\Program Files\Other\OtherApp.exe"

        result = attribute(procs + other)
        self.assertEqual(len(result.applications), 2, "两个宿主必须分开")
        names = {a.name for a in result.applications}
        self.assertIn("AweSun", names)
        self.assertIn("OtherApp", names)


class TestProductSignatureMerge(unittest.TestCase):
    """同产品、不同可执行文件的进程要合并。

    真实场景：Steam = steam.exe + 7 个 steamwebhelper.exe（路径完全不同）；
    EA = EADesktop.exe + 5 个 EACefSubProcess.exe。
    """

    def test_steam_helpers_merge_with_main_process(self):
        result = attribute(steam_topology())
        self.assertEqual(len(result.applications), 1)
        app = result.applications[0]
        self.assertEqual(app.name, "Steam")
        self.assertEqual(app.process_count, 8)
        self.assertEqual(app.confidence, Confidence.HIGH)

    def test_ea_desktop_and_subprocess_merge(self):
        result = attribute(ea_topology())
        self.assertEqual(len(result.applications), 1)
        app = result.applications[0]
        self.assertEqual(app.process_count, 6)
        self.assertEqual(app.total_bytes, 413 * MB + 5 * 78 * MB)

    def test_different_products_do_not_merge(self):
        procs = [
            proc(1, 0, "a.exe", exe_path=r"C:\Program Files\Alpha\a.exe",
                 company="Alpha Inc", product="Alpha"),
            proc(2, 0, "b.exe", exe_path=r"C:\Program Files\Beta\b.exe",
                 company="Beta Inc", product="Beta"),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 2)

    def test_evidence_names_the_signature(self):
        result = attribute(steam_topology())
        app = result.applications[0]
        joined = " ".join(app.evidence)
        self.assertIn("Valve Corporation", joined)


class TestServiceHostResolution(unittest.TestCase):
    """方案 B：svchost 要解析出具体承载的服务名。

    真实场景：系统里有 89 个 svchost.exe 合计约 1.5 GB。
    不解析服务名，这就是一个完全无法行动的 1.5 GB 黑盒。
    """

    def test_svchost_named_after_its_services(self):
        procs, services = svchost_with_services()
        result = attribute(procs, services)

        app = find_app(result, "Windows Update")
        self.assertIsNotNone(app, "svchost 应以其承载的服务命名")
        self.assertEqual(app.process_count, 1)
        self.assertIn("3 个服务", app.name)
        self.assertEqual(app.category, Category.SYSTEM)

    def test_single_service_host_uses_service_name_directly(self):
        procs, services = svchost_with_services()
        result = attribute(procs, services)
        self.assertIsNotNone(find_app(result, "Windows Audio"))

    def test_two_svchost_hosts_stay_separate(self):
        """两个不同的 svchost 承载不同服务，不能合并。"""
        procs, services = svchost_with_services()
        result = attribute(procs, services)
        hosts = [a for a in result.applications if a.key.startswith("service-host:")]
        self.assertEqual(len(hosts), 2)

    def test_without_service_data_falls_back_and_says_so(self):
        procs, _ = svchost_with_services()
        result = attribute(procs)
        app = find_app(result, "svchost")
        self.assertIsNotNone(app, "拿不到服务表时应退回按进程名展示")
        self.assertIn("服务名未解析", app.name)
        joined = " ".join(app.evidence)
        self.assertIn("未能解析出", joined, "证据里要说明为什么没能细分")

    def test_service_evidence_lists_service_names(self):
        procs, services = svchost_with_services()
        result = attribute(procs, services)
        joined = " ".join(result.explanations[1000])
        self.assertIn("Windows Update", joined)
        self.assertIn("Cryptographic Services", joined)


class TestLauncherIsolation(unittest.TestCase):
    """启动器不能把子进程吸进去。

    真实场景：``services.exe`` 有 138 个子进程、``explorer.exe`` 有 12 个。
    如果按父子关系合并，会得到一个没有意义的巨型条目。
    """

    def test_services_exe_does_not_absorb_svchost(self):
        procs, services = svchost_with_services()
        result = attribute(procs, services)

        launcher = find_app(result, "services.exe")
        self.assertIsNotNone(launcher, "services.exe 本身也是一个应用")
        self.assertEqual(launcher.role, Role.LAUNCHER)
        self.assertEqual(launcher.process_count, 1, "只应包含它自己")

    def test_explorer_children_become_their_own_apps(self):
        procs = [
            proc(15864, 0, "explorer.exe",
                 exe_path=r"C:\Windows\explorer.exe",
                 description="Windows 资源管理器", private=180 * MB),
            proc(28064, 15864, "AweSun.exe",
                 exe_path=r"C:\Program Files\Oray\AweSun\AweSun.exe",
                 company="Oray", product="AweSun", private=295 * MB),
            proc(28388, 15864, "OneDrive.exe",
                 exe_path=r"C:\Users\Ray\AppData\Local\Microsoft\OneDrive\OneDrive.exe",
                 company="Microsoft Corporation", product="Microsoft OneDrive",
                 private=110 * MB),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 3)
        names = {a.name for a in result.applications}
        self.assertIn("AweSun", names)
        self.assertIn("Microsoft OneDrive", names)


class TestTreeRepair(unittest.TestCase):
    """进程树必须修复后再用于归因。"""

    def test_pid_reuse_fake_parent_is_discarded(self):
        """父进程比子进程晚启动 → PID 重用 → 这条边不能信。"""
        result = attribute(fake_parent_topology())

        self.assertEqual(len(result.tree_repairs), 1, "应报告一处修复")
        self.assertIn("PID 重用", result.tree_repairs[0])

        # 关键断言：WebView2 不能被算到 recycled.exe 头上。
        # recycled.exe 本身当然还是一个应用，但它只应包含它自己。
        recycled = find_app(result, "Recycled App")
        self.assertIsNotNone(recycled, "recycled.exe 自身仍是一个应用")
        self.assertEqual(recycled.pids, [800], "但不能吸走 WebView2 的进程")

        app = find_app(result, "msedgewebview2")
        self.assertIsNotNone(app, "父链断裂后应退化为未归因的共享运行时")
        self.assertIn(900, app.pids)

    def test_orphan_process_becomes_root(self):
        procs = [
            proc(500, 99999, "orphan.exe",
                 exe_path=r"C:\Program Files\Orphan\orphan.exe",
                 company="Orphan Inc", product="Orphan App", private=30 * MB),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1)
        self.assertEqual(result.applications[0].name, "Orphan App")

    def test_normal_parent_link_is_kept(self):
        """启动顺序正常的父子关系不能被误判为 PID 重用。"""
        procs = [
            proc(100, 0, "parent.exe", start_time=1000.0,
                 exe_path=r"C:\Program Files\P\parent.exe",
                 company="P Inc", product="P"),
            proc(200, 100, "msedgewebview2.exe", start_time=1100.0,
                 exe_path=r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\1\msedgewebview2.exe",
                 company="Microsoft Corporation", product="Microsoft Edge WebView2",
                 command_line="msedgewebview2.exe --embedded-browser-webview"),
        ]
        result = attribute(procs)
        self.assertEqual(result.tree_repairs, [])
        self.assertEqual(len(result.applications), 1)
        self.assertEqual(result.applications[0].name, "P")

    def test_parent_without_start_time_still_links(self):
        """拿不到启动时间时不能乱断边（否则会把正常的树拆散）。"""
        procs = [
            proc(100, 0, "parent.exe",
                 exe_path=r"C:\Program Files\P\parent.exe",
                 company="P Inc", product="P"),
            proc(200, 100, "msedgewebview2.exe",
                 exe_path=r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\1\msedgewebview2.exe",
                 company="Microsoft Corporation", product="Microsoft Edge WebView2",
                 command_line="msedgewebview2.exe --embedded-browser-webview"),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1)


class TestGenericRuntime(unittest.TestCase):
    """通用运行时的进程名没有区分度，身份取决于它跑的是什么。"""

    def test_runtime_grouped_by_script_hint(self):
        procs = [
            proc(100, 0, "node.exe",
                 exe_path=r"C:\Program Files\nodejs\node.exe",
                 command_line=r'"C:\Program Files\nodejs\node.exe" D:\proj\web\build.mjs'),
            proc(101, 0, "node.exe",
                 exe_path=r"C:\Program Files\nodejs\node.exe",
                 command_line=r'"C:\Program Files\nodejs\node.exe" D:\proj\web\serve.mjs'),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1, "同一脚本目录应归为一组")
        app = result.applications[0]
        self.assertEqual(app.category, Category.RUNTIME)
        self.assertIn("web", app.name)

    def test_runtime_with_different_scripts_stay_separate(self):
        procs = [
            proc(100, 0, "node.exe", exe_path=r"C:\Program Files\nodejs\node.exe",
                 command_line=r'"node.exe" D:\proj\web\build.mjs'),
            proc(101, 0, "node.exe", exe_path=r"C:\Program Files\nodejs\node.exe",
                 command_line=r'"node.exe" D:\proj\api\server.js'),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 2, "不同项目不能被合并")

    def test_python_runtimes_not_merged_by_name_alone(self):
        procs = [
            proc(100, 0, "python.exe", exe_path=r"C:\Python312\python.exe",
                 command_line=r'"C:\Python312\python.exe" D:\a\one.py'),
            proc(101, 0, "python.exe", exe_path=r"C:\Python312\python.exe",
                 command_line=r'"C:\Python312\python.exe" D:\b\two.py'),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 2)

    def test_runtime_without_hint_is_low_confidence(self):
        procs = [proc(100, 0, "node.exe", exe_path=r"C:\Program Files\nodejs\node.exe")]
        result = attribute(procs)
        self.assertEqual(result.applications[0].confidence, Confidence.LOW)


class TestSystemComponents(unittest.TestCase):
    """Windows 自带组件按可执行文件名合并。"""

    def test_same_named_system_components_group_together(self):
        procs = [
            proc(100, 600, "ShellHost.exe",
                 exe_path=r"C:\Windows\System32\ShellHost.exe",
                 company="Microsoft Corporation",
                 product="Microsoft® Windows® Operating System",
                 description="ShellHost", private=20 * MB),
            proc(101, 600, "ShellHost.exe",
                 exe_path=r"C:\Windows\System32\ShellHost.exe",
                 company="Microsoft Corporation",
                 product="Microsoft® Windows® Operating System",
                 description="ShellHost", private=25 * MB),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1)
        app = result.applications[0]
        self.assertEqual(app.process_count, 2)
        self.assertEqual(app.category, Category.SYSTEM)
        self.assertEqual(app.total_bytes, 45 * MB)

    def test_windows_os_product_does_not_merge_everything(self):
        """所有系统二进制共享同一个产品名，不能因此全部合并成一个。"""
        procs = [
            proc(100, 600, "ShellHost.exe", exe_path=r"C:\Windows\System32\ShellHost.exe",
                 company="Microsoft Corporation",
                 product="Microsoft® Windows® Operating System", description="ShellHost"),
            proc(101, 600, "sihost.exe", exe_path=r"C:\Windows\System32\sihost.exe",
                 company="Microsoft Corporation",
                 product="Microsoft® Windows® Operating System",
                 description="Shell Infrastructure Host"),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 2, "不同组件的产品名相同，但不能合并")


class TestRobustness(unittest.TestCase):
    def test_empty_input(self):
        result = attribute([])
        self.assertEqual(result.applications, [])
        self.assertEqual(result.total_bytes, 0)

    def test_every_process_lands_in_exactly_one_application(self):
        procs = steam_topology() + ea_topology() + webview2_topology()
        result = attribute(procs)
        seen: list[int] = []
        for app in result.applications:
            seen.extend(app.pids)
        self.assertEqual(sorted(seen), sorted(p.pid for p in procs))
        self.assertEqual(len(seen), len(set(seen)), "不能有进程被算两次")

    def test_total_bytes_conserved(self):
        procs = steam_topology() + ea_topology() + webview2_topology()
        result = attribute(procs)
        self.assertEqual(
            result.total_bytes, sum(p.private_bytes for p in procs)
        )

    def test_parent_cycle_does_not_hang(self):
        """互为父子的畸形数据必须能终止。"""
        procs = [
            proc(10, 11, "msedgewebview2.exe",
                 exe_path=r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\1\msedgewebview2.exe",
                 company="Microsoft Corporation", product="Microsoft Edge WebView2",
                 start_time=1000.0),
            proc(11, 10, "msedgewebview2.exe",
                 exe_path=r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\1\msedgewebview2.exe",
                 company="Microsoft Corporation", product="Microsoft Edge WebView2",
                 start_time=1000.0),
        ]
        result = attribute(procs)  # 不挂死即可
        self.assertIsInstance(result.applications, list)

    def test_self_parent_is_ignored(self):
        procs = [proc(42, 42, "weird.exe", exe_path=r"C:\Program Files\W\weird.exe",
                      company="W Inc", product="Weird")]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1)

    def test_inaccessible_process_still_reported(self):
        procs = [proc(4, 0, "System", accessible=False, private=0)]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1)

    def test_inaccessible_shared_runtime_has_hint_fallback(self):
        """父链断了，但命令行里有 user-data-dir —— 用它兜底。"""
        procs = [
            proc(900, 99999, "msedgewebview2.exe",
                 exe_path=r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\1\msedgewebview2.exe",
                 company="Microsoft Corporation", product="Microsoft Edge WebView2",
                 command_line=(
                     '"msedgewebview2.exe" --type=crashpad-handler '
                     r"--user-data-dir=C:\Users\Ray\AppData\Local\SomeApp\EBWebView"
                 )),
        ]
        result = attribute(procs)
        app = result.applications[0]
        self.assertIn("SomeApp", app.name, "应利用 user-data-dir 线索推断宿主")
        joined = " ".join(app.evidence)
        self.assertIn("user-data-dir", joined)

    def test_confidence_is_low_for_path_only_guess(self):
        procs = [proc(100, 0, "mystery.exe",
                      exe_path=r"C:\Program Files\Mystery\mystery.exe")]
        result = attribute(procs)
        self.assertEqual(result.applications[0].confidence, Confidence.LOW)


class TestClassifyHelpers(unittest.TestCase):
    def test_install_root_strips_packaging_dirs(self):
        self.assertEqual(
            install_root(r"C:\Program Files (x86)\Steam\bin\cef\cef.win7x64\steamwebhelper.exe"),
            r"C:\Program Files (x86)\Steam",
        )
        self.assertEqual(
            install_root(r"C:\Program Files\App\app.exe"),
            r"C:\Program Files\App",
        )

    def test_install_root_never_collapses_to_program_files(self):
        """回归测试：产品目录名恰好是打包目录名时，不能剥到安装根。

        曾经的实现把 "app" 当成打包目录，于是
        ``C:\\Program Files\\App\\app.exe`` 会退化成 ``C:\\Program Files``，
        把大量互不相关的软件合并成一组。
        """
        self.assertEqual(
            install_root(r"C:\Program Files\App\app.exe"),
            r"C:\Program Files\App",
        )

    def test_install_root_strips_version_dirs(self):
        root = install_root(
            r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\153.0.4234.32\msedgewebview2.exe"
        )
        self.assertNotIn("153.0.4234.32", root, "版本号目录必须被剥掉")
        self.assertTrue(
            root.startswith(r"C:\Program Files (x86)\Microsoft"),
            f"应停在安装根之下，实际得到 {root}",
        )

    def test_install_root_handles_none(self):
        self.assertIsNone(install_root(None))

    def test_script_hint_extracts_directory(self):
        self.assertEqual(
            script_hint(r'"C:\Program Files\nodejs\node.exe" D:\proj\web\build.mjs'),
            r"D:\proj\web",
        )

    def test_script_hint_skips_flags(self):
        self.assertEqual(
            script_hint(r'"python.exe" -X utf8 -m mymodule'),
            "-m mymodule",
        )

    def test_script_hint_returns_none_without_args(self):
        self.assertIsNone(script_hint(r'"node.exe"'))
        self.assertIsNone(script_hint(None))
        self.assertIsNone(script_hint(""))


class TestProductFamilyMerge(unittest.TestCase):
    """同一个软件的不同组件经常写着**不一样**的产品名。

    真实观测：Steam 主程序写 ``Steam``，而它的界面进程写
    ``Steam Client WebHelper``。只按产品名精确匹配会把同一个软件拆成两条，
    数据直接失真。
    """

    def test_steam_client_webhelper_merges_with_steam(self):
        result = attribute(steam_topology())
        self.assertEqual(len(result.applications), 1, "同一个软件不能被拆开")
        app = result.applications[0]
        self.assertEqual(app.name, "Steam", "展示名应取产品族里最短的那个")
        self.assertEqual(app.process_count, 8)

    def test_merge_evidence_explains_the_family_rule(self):
        result = attribute(steam_topology())
        joined = " ".join(result.applications[0].evidence)
        self.assertIn("产品族", joined, "证据里要说明为什么把两个名字并到一起")

    def test_prefix_must_land_on_a_word_boundary(self):
        """``EA`` 不能吞掉 ``Eagle``——前缀必须落在词边界上。"""
        procs = [
            proc(1, 0, "ea.exe", exe_path=r"C:\Program Files\EA\ea.exe",
                 company="Electronic Arts", product="EA"),
            proc(2, 0, "eagle.exe", exe_path=r"C:\Program Files\Eagle\eagle.exe",
                 company="Electronic Arts", product="Eagle"),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 2, "词边界不成立就不能合并")

    def test_word_boundary_accepts_space_dash_underscore(self):
        for suffix in (" Desktop", "-Pro", "_Helper", "(x64)"):
            with self.subTest(suffix=suffix):
                procs = [
                    proc(1, 0, "a.exe", exe_path=r"C:\Program Files\A\a.exe",
                         company="Same Co", product="Tool"),
                    proc(2, 0, "b.exe", exe_path=r"C:\Program Files\A\b.exe",
                         company="Same Co", product="Tool" + suffix),
                ]
                self.assertEqual(len(attribute(procs).applications), 1)

    def test_different_companies_never_merge(self):
        """厂商不同，即使产品名互为前缀也不能合并。"""
        procs = [
            proc(1, 0, "a.exe", exe_path=r"C:\Program Files\A\a.exe",
                 company="Alpha Inc", product="Tool"),
            proc(2, 0, "b.exe", exe_path=r"C:\Program Files\B\b.exe",
                 company="Beta Inc", product="Tool Pro"),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 2)

    def test_product_family_merge_does_not_affect_passthrough(self):
        """产品族合并不能干扰共享运行时的穿透。"""
        procs = webview2_topology()
        procs[0].product = "SomeApp"
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1)
        self.assertEqual(result.applications[0].process_count, 5)


class TestInaccessibleGrouping(unittest.TestCase):
    """系统保护进程应当合并成一个诚实的桶。

    真实观测：349 个进程里有 171 个打不开。若让它们各自成组，
    报告里会多出近百条名字都是猜的记录——噪音远大于信息量。
    """

    def test_inaccessible_processes_group_into_one_bucket(self):
        procs = [
            proc(pid, 4, f"sys{i}.exe", accessible=False)
            for i, pid in enumerate(range(100, 140))
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 1, "不应产出 40 条碎片")
        app = result.applications[0]
        self.assertEqual(app.process_count, 40)
        self.assertEqual(app.category, Category.SYSTEM)
        self.assertEqual(app.confidence, Confidence.LOW)
        self.assertIn("无法读取详情", app.name)

    def test_bucket_explains_why(self):
        procs = [proc(100, 4, "sys.exe", accessible=False)]
        result = attribute(procs)
        joined = " ".join(result.applications[0].evidence)
        self.assertIn("无法打开进程", joined)

    def test_accessible_unknown_process_is_still_individual(self):
        """能打开但认不出的进程**不应**被塞进保护进程桶——
        它们各自可能真的是不同的软件。"""
        procs = [
            proc(100, 0, "mystery1.exe", exe_path=r"C:\Program Files\M1\mystery1.exe"),
            proc(101, 0, "mystery2.exe", exe_path=r"C:\Program Files\M2\mystery2.exe"),
        ]
        result = attribute(procs)
        self.assertEqual(len(result.applications), 2)


class TestDepthLimit(unittest.TestCase):
    def test_max_depth_constant_is_sane(self):
        self.assertGreaterEqual(MAX_ANCESTOR_DEPTH, 8)
        self.assertLessEqual(MAX_ANCESTOR_DEPTH, 64)


if __name__ == "__main__":
    unittest.main()
