"""诊断规则的测试。

这些规则是本工具与"内存清理软件"的分界线，所以测试的重点不只是
"能不能跑"，而是**结论是否正确、是否诚实**：

  * 提交量超过物理内存时必须报警，且数字要对得上
  * 空闲内存很少但待机缓存很大时，必须解释成正常，**绝不能**建议清理
  * 样本不足时不能硬下结论
"""

from __future__ import annotations

import unittest

from memscope.diagnose import (
    BACKGROUND_GROUPS,
    Severity,
    background_groups,
    diagnose,
)
from memscope.model import Application, Category, SystemMemory

GB = 1024 ** 3
MB = 1024 ** 2


def memory(**kw) -> SystemMemory:
    base = dict(
        page_size=4096,
        physical_total=16 * GB,
        physical_available=8 * GB,
        commit_total=10 * GB,
        commit_limit=32 * GB,
        kernel_paged=500 * MB,
        kernel_nonpaged=400 * MB,
    )
    base.update(kw)
    return SystemMemory(**base)


def find(insights, needle: str):
    for insight in insights:
        if needle in insight.title:
            return insight
    return None


def all_text(insights) -> str:
    return "\n".join(i.title + " " + i.detail + " " + (i.suggestion or "") for i in insights)


class TestCommitPressure(unittest.TestCase):
    def test_commit_over_physical_is_flagged(self):
        mem = memory(physical_total=16 * GB, commit_total=20 * GB)
        insights = diagnose(mem)
        insight = find(insights, "已超过物理内存")
        self.assertIsNotNone(insight, "提交量超过物理内存必须报警")
        self.assertIn(insight.severity, (Severity.WARNING, Severity.NOTICE))
        self.assertIn("4.00 GB", insight.detail, "超出的差额要算对")
        self.assertIn("硬缺页", insight.detail, "必须解释后果的因果链")

    def test_commit_under_physical_is_ok(self):
        mem = memory(physical_total=16 * GB, commit_total=10 * GB)
        insight = find(diagnose(mem), "未超过物理内存")
        self.assertIsNotNone(insight)
        self.assertEqual(insight.severity, Severity.OK)

    def test_boundary_equal_is_not_flagged(self):
        mem = memory(physical_total=16 * GB, commit_total=16 * GB)
        self.assertIsNotNone(find(diagnose(mem), "未超过物理内存"))


class TestStandbyExplanation(unittest.TestCase):
    """最容易做错、也最影响结论价值的一条规则。"""

    def test_small_free_memory_with_large_standby_is_explained_as_normal(self):
        mem = memory(
            physical_total=16 * GB,
            physical_available=4 * GB,
            standby_core=50 * MB,
            standby_normal=3 * GB,
            standby_reserve=300 * MB,
            free_and_zero=50 * MB,
        )
        insights = diagnose(mem)
        insight = find(insights, "正常的")
        self.assertIsNotNone(insight, "空闲内存很少但待机缓存大，必须解释成正常")
        self.assertEqual(insight.severity, Severity.INFO, "这是说明，不是警告")

    def test_never_suggests_clearing_standby(self):
        """绝不能建议清理待机缓存——那正是让系统变慢的做法。"""
        mem = memory(
            physical_total=16 * GB,
            physical_available=4 * GB,
            standby_normal=3 * GB,
            free_and_zero=50 * MB,
        )
        text = all_text(diagnose(mem))
        self.assertIn("不要使用任何声称能「释放内存」的工具", text)
        self.assertNotIn("建议清理待机", text)
        self.assertNotIn("释放待机缓存", text)

    def test_standby_explanation_mentions_reclaim_is_free(self):
        mem = memory(
            physical_total=16 * GB,
            physical_available=4 * GB,
            standby_normal=3 * GB,
            free_and_zero=50 * MB,
        )
        insight = find(diagnose(mem), "正常的")
        self.assertIn("立刻", insight.detail)
        self.assertIn("没有区别", insight.detail)


class TestAvailableRatio(unittest.TestCase):
    def test_tight_available_is_warning(self):
        mem = memory(physical_total=16 * GB, physical_available=1 * GB)
        insight = find(diagnose(mem), "物理可用内存偏低")
        self.assertIsNotNone(insight)
        self.assertEqual(insight.severity, Severity.WARNING)

    def test_medium_available_is_notice(self):
        mem = memory(physical_total=16 * GB, physical_available=2 * GB)
        insight = find(diagnose(mem), "物理可用内存中等")
        self.assertIsNotNone(insight)
        self.assertEqual(insight.severity, Severity.NOTICE)

    def test_plenty_available_is_silent(self):
        mem = memory(physical_total=16 * GB, physical_available=10 * GB)
        self.assertIsNone(find(diagnose(mem), "物理可用内存"))


class TestCommitLimit(unittest.TestCase):
    def test_near_limit_is_warning(self):
        mem = memory(commit_total=30 * GB, commit_limit=32 * GB)
        insight = find(diagnose(mem), "提交上限的")
        self.assertIsNotNone(insight)
        self.assertEqual(insight.severity, Severity.WARNING)

    def test_moderate_use_is_info(self):
        mem = memory(commit_total=26 * GB, commit_limit=32 * GB)
        insight = find(diagnose(mem), "占用上限的")
        self.assertIsNotNone(insight)
        self.assertEqual(insight.severity, Severity.INFO)

    def test_zero_commit_limit_does_not_crash(self):
        diagnose(memory(commit_limit=0))


class TestKernelPool(unittest.TestCase):
    def test_large_nonpaged_pool_is_noticed(self):
        mem = memory(kernel_nonpaged=2 * GB)
        insight = find(diagnose(mem), "内核非分页池偏大")
        self.assertIsNotNone(insight)
        self.assertIn("驱动", insight.detail)

    def test_normal_pool_is_silent(self):
        self.assertIsNone(find(diagnose(memory(kernel_nonpaged=300 * MB)), "内核非分页池"))


class TestBackgroundGroups(unittest.TestCase):
    def test_grouping_by_keyword(self):
        apps = [
            Application(key="a", name="Steam", category=Category.USER, private_bytes=900 * MB),
            Application(key="b", name="RazerAppEngine", category=Category.USER,
                        private_bytes=800 * MB),
            Application(key="c", name="SomeRandomApp", category=Category.USER,
                        private_bytes=100 * MB),
        ]
        groups = background_groups(apps)
        self.assertIn("游戏平台", groups)
        self.assertIn("外设驱动软件", groups)
        self.assertNotIn("SomeRandomApp", str(groups.get("游戏平台", [])))

    def test_matches_company_field_too(self):
        app = Application(key="a", name="OrayHelper", category=Category.USER,
                          company="Oray", private_bytes=200 * MB)
        groups = background_groups([app])
        self.assertIn("远程控制", groups)

    def test_crowded_groups_produce_an_insight(self):
        apps = [
            Application(key=f"g{i}", name=f"GameLauncher{i}", category=Category.USER,
                        private_bytes=400 * MB, company="Epic Games")
            for i in range(3)
        ]
        insight = find(diagnose(memory(), apps), "未必需要常驻")
        self.assertIsNotNone(insight)
        self.assertIn("合计约", insight.detail)

    def test_small_groups_do_not_produce_noise(self):
        apps = [
            Application(key="a", name="Steam", category=Category.USER,
                        private_bytes=20 * MB)
        ]
        self.assertIsNone(find(diagnose(memory(), apps), "未必需要常驻"))

    def test_insight_admits_classification_may_be_wrong(self):
        apps = [
            Application(key="a", name="Steam", category=Category.USER,
                        private_bytes=900 * MB)
        ]
        insight = find(diagnose(memory(), apps), "未必需要常驻")
        self.assertIn("可能认错", insight.suggestion, "必须承认关键词匹配会认错")


class TestDiagnoseRobustness(unittest.TestCase):
    def test_empty_memory_does_not_crash(self):
        diagnose(SystemMemory())

    def test_no_applications_is_fine(self):
        diagnose(memory(), None)
        diagnose(memory(), [])

    def test_every_insight_is_actionable_or_informative(self):
        mem = memory(
            physical_total=16 * GB,
            physical_available=500 * MB,
            commit_total=20 * GB,
            commit_limit=21 * GB,
            kernel_nonpaged=2 * GB,
            standby_normal=3 * GB,
            free_and_zero=10 * MB,
        )
        insights = diagnose(mem)
        self.assertGreater(len(insights), 3, "极端情况下应产出多条结论")
        for insight in insights:
            self.assertTrue(insight.title)
            self.assertTrue(insight.detail)
            self.assertIsInstance(insight.severity, Severity)

    def test_conservative_when_data_missing(self):
        """数据缺失时不能凭空报警。"""
        insights = diagnose(SystemMemory())
        for insight in insights:
            self.assertNotEqual(insight.severity, Severity.WARNING)


class TestBackgroundGroupsTable(unittest.TestCase):
    def test_table_is_non_empty_and_well_formed(self):
        self.assertGreater(len(BACKGROUND_GROUPS), 3)
        for name, keywords in BACKGROUND_GROUPS.items():
            self.assertTrue(name)
            self.assertTrue(keywords)
            self.assertTrue(all(k == k.lower() for k in keywords),
                            f"{name} 的关键词必须是小写，否则匹配会失效")


if __name__ == "__main__":
    unittest.main()
