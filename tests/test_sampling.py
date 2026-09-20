"""采样序列化的测试。

采样文件常常被 Ctrl+C 打断，末尾可能留着半行 JSON——
读取器必须能跳过坏行而不是整个失败，否则几小时的采样就白费了。
"""

from __future__ import annotations

import json
import unittest

from fixtures import temp_dir

from memscope.model import (
    Application,
    AttributionResult,
    Category,
    ProcessRecord,
    Snapshot,
    SystemMemory,
)
from memscope.sampling import (
    SAMPLE_FORMAT_VERSION,
    AppSample,
    Sample,
    append_sample,
    iter_app_series,
    read_samples,
    sample_from_snapshot,
)

MB = 1024 ** 2


class TestSampleSerialization(unittest.TestCase):
    def make(self) -> Sample:
        return Sample(
            timestamp=1700000000.5,
            system={"physical_total": 16 * 1024 ** 3, "commit_total": 20 * 1024 ** 3},
            apps=[
                AppSample(key="k1", name="Steam", category="user",
                          processes=8, private_bytes=987 * MB, working_set=1200 * MB),
                AppSample(key="k2", name="节点", category="runtime",
                          processes=2, private_bytes=100 * MB, working_set=110 * MB),
            ],
        )

    def test_roundtrip_preserves_everything(self):
        original = self.make()
        restored = Sample.from_dict(original.to_dict())
        self.assertEqual(restored.timestamp, original.timestamp)
        self.assertEqual(restored.system, original.system)
        self.assertEqual(len(restored.apps), len(original.apps))
        for a, b in zip(restored.apps, original.apps, strict=True):
            self.assertEqual((a.key, a.name, a.category), (b.key, b.name, b.category))
            self.assertEqual(a.private_bytes, b.private_bytes)
            self.assertEqual(a.working_set, b.working_set)

    def test_dict_has_version_field(self):
        self.assertEqual(self.make().to_dict()["v"], SAMPLE_FORMAT_VERSION)

    def test_non_ascii_survives_json(self):
        payload = json.dumps(self.make().to_dict(), ensure_ascii=False)
        self.assertIn("节点", payload)
        restored = Sample.from_dict(json.loads(payload))
        self.assertEqual(restored.apps[1].name, "节点")

    def test_from_dict_tolerates_missing_fields(self):
        sample = Sample.from_dict({"t": 1.0})
        self.assertEqual(sample.timestamp, 1.0)
        self.assertEqual(sample.apps, [])
        self.assertEqual(sample.system, {})

    def test_from_dict_tolerates_bad_app_entries(self):
        sample = Sample.from_dict({"t": 1, "apps": [{"k": "a"}, {}]})
        self.assertEqual(len(sample.apps), 2)


class TestSampleFromSnapshot(unittest.TestCase):
    def test_extracts_expected_fields(self):
        snapshot = Snapshot(
            timestamp=123.0,
            system=SystemMemory(
                physical_total=16 * 1024 ** 3,
                physical_available=4 * 1024 ** 3,
                commit_total=20 * 1024 ** 3,
                commit_limit=32 * 1024 ** 3,
                standby_normal=3 * 1024 ** 3,
                free_and_zero=50 * MB,
                kernel_paged=600 * MB,
                kernel_nonpaged=800 * MB,
            ),
            processes=[ProcessRecord(pid=1, name="x")],
            attribution=AttributionResult(
                applications=[
                    Application(key="a", name="Steam", category=Category.USER,
                                pids=[1, 2], private_bytes=900 * MB,
                                working_set=1100 * MB),
                ]
            ),
        )
        sample = sample_from_snapshot(snapshot)
        self.assertEqual(sample.timestamp, 123.0)
        self.assertEqual(sample.system["physical_total"], 16 * 1024 ** 3)
        self.assertEqual(sample.system["standby_total"], 3 * 1024 ** 3)
        self.assertEqual(len(sample.apps), 1)
        app = sample.apps[0]
        self.assertEqual(app.name, "Steam")
        self.assertEqual(app.processes, 2)
        self.assertEqual(app.private_bytes, 900 * MB)
        self.assertEqual(app.category, "user")

    def test_empty_snapshot(self):
        sample = sample_from_snapshot(Snapshot(timestamp=1.0, system=SystemMemory()))
        self.assertEqual(sample.apps, [])


class TestFileIO(unittest.TestCase):
    """采样文件的读写。

    临时目录放在仓库内部（见 fixtures.temp_dir），因为受限环境下
    系统临时目录可能不可写。
    """

    def setUp(self):
        self._ctx = temp_dir()
        self.dir = self._ctx.__enter__()
        self.path = self.dir / "samples.jsonl"

    def tearDown(self):
        self._ctx.__exit__(None, None, None)

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_append_and_read_back(self):
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            for i in range(3):
                append_sample(handle, Sample(
                    timestamp=float(i), system={"a": i},
                    apps=[AppSample(key="k", name="n", category="user",
                                    processes=1, private_bytes=i * MB, working_set=0)],
                ))
        samples = read_samples(self.path)
        self.assertEqual(len(samples), 3)
        self.assertEqual([s.timestamp for s in samples], [0.0, 1.0, 2.0])

    def test_truncated_last_line_is_skipped(self):
        """模拟 Ctrl+C 打断，末尾留下半行。"""
        self.write(
            '{"v":1,"t":1.0,"sys":{"a":1},"apps":[]}\n'
            '{"v":1,"t":2.0,"sys":{"a":2},"apps":[]}\n'
            '{"v":1,"t":3.0,"sys":{"a"'
        )
        samples = read_samples(self.path)
        self.assertEqual(len(samples), 2, "坏行要跳过，好行必须保留")

    def test_blank_lines_ignored(self):
        self.write('\n\n{"v":1,"t":1.0,"apps":[]}\n\n')
        self.assertEqual(len(read_samples(self.path)), 1)

    def test_non_object_json_ignored(self):
        self.write('[1,2,3]\n"a string"\n42\n{"v":1,"t":1.0}\n')
        self.assertEqual(len(read_samples(self.path)), 1)

    def test_empty_file(self):
        self.write("")
        self.assertEqual(read_samples(self.path), [])


class TestIterAppSeries(unittest.TestCase):
    def test_groups_points_by_key(self):
        samples = [
            Sample(timestamp=0.0, apps=[AppSample("a", "A", "user", 1, 100, 100),
                                        AppSample("b", "B", "user", 1, 200, 200)]),
            Sample(timestamp=1.0, apps=[AppSample("a", "A", "user", 1, 150, 150)]),
        ]
        series = iter_app_series(samples)
        self.assertEqual(len(series["a"]), 2)
        self.assertEqual(len(series["b"]), 1)
        self.assertEqual(series["a"][1][1], 150)

    def test_empty(self):
        self.assertEqual(iter_app_series([]), {})


if __name__ == "__main__":
    unittest.main()
