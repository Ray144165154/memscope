"""定时采样：把内存占用记录成时间序列。

"内存优化"这个词里，唯一真正有意义的形态是**找出正在悄悄增长的东西**。
一次快照看不出来，必须持续采样再分析趋势。

采样数据写成 JSONL（每行一个 JSON 对象），理由：

  * 追加写入，采样中途被打断也不会损坏已有的数据
  * 每行独立，出问题时能直接看出是哪一次采样坏了
  * 纯文本，用任何工具都能查看、过滤、画图

格式带 ``v`` 字段（版本号），以便将来扩展时仍能读旧文件。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .collect import Collector, take_snapshot
from .model import Snapshot

__all__ = [
    "SAMPLE_FORMAT_VERSION",
    "AppSample",
    "Sample",
    "sample_from_snapshot",
    "append_sample",
    "read_samples",
    "watch",
]

SAMPLE_FORMAT_VERSION = 1


@dataclass(slots=True)
class AppSample:
    """一次采样里某一个应用的占用。"""

    key: str
    name: str
    category: str
    processes: int
    private_bytes: int
    working_set: int


@dataclass(slots=True)
class Sample:
    """一次采样。"""

    timestamp: float
    system: dict[str, int] = field(default_factory=dict)
    apps: list[AppSample] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "v": SAMPLE_FORMAT_VERSION,
            "t": round(self.timestamp, 3),
            "sys": self.system,
            "apps": [
                {
                    "k": a.key,
                    "n": a.name,
                    "c": a.category,
                    "p": a.processes,
                    "priv": a.private_bytes,
                    "ws": a.working_set,
                }
                for a in self.apps
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Sample:
        apps = [
            AppSample(
                key=str(item.get("k", "")),
                name=str(item.get("n", "")),
                category=str(item.get("c", "")),
                processes=int(item.get("p", 0) or 0),
                private_bytes=int(item.get("priv", 0) or 0),
                working_set=int(item.get("ws", 0) or 0),
            )
            for item in data.get("apps", [])
        ]
        return cls(
            timestamp=float(data.get("t", 0.0)),
            system={k: int(v) for k, v in (data.get("sys") or {}).items()},
            apps=apps,
        )


def sample_from_snapshot(snapshot: Snapshot) -> Sample:
    """把一次快照压缩成一条采样记录。

    只保留分析趋势所需的字段。进程树、厂商信息这些在采样间不变的
    东西不写进去——否则文件会大到没法分析。
    """
    memory = snapshot.system
    system = {
        "physical_total": memory.physical_total,
        "physical_available": memory.physical_available,
        "commit_total": memory.commit_total,
        "commit_limit": memory.commit_limit,
        "standby_total": memory.standby_total,
        "free_and_zero": memory.free_and_zero,
        "kernel_paged": memory.kernel_paged,
        "kernel_nonpaged": memory.kernel_nonpaged,
    }
    apps = [
        AppSample(
            key=app.key,
            name=app.name,
            category=app.category.value,
            processes=app.process_count,
            private_bytes=app.private_bytes,
            working_set=app.working_set,
        )
        for app in snapshot.applications
    ]
    return Sample(timestamp=snapshot.timestamp, system=system, apps=apps)


def append_sample(handle: TextIO, sample: Sample) -> None:
    """把一条采样追加到文件，并立刻刷盘。

    立刻 flush 是有意的：采样常常是被用户按 Ctrl+C 打断的，
    缓冲区里丢掉最后几条就白采了。
    """
    handle.write(json.dumps(sample.to_dict(), ensure_ascii=False, separators=(",", ":")))
    handle.write("\n")
    handle.flush()


def read_samples(path: str | Path) -> list[Sample]:
    """读取 JSONL 采样文件。

    坏行会被跳过而不是让整个分析失败——采样文件常常末尾带半行
    （进程被杀），不该因此丢掉全部数据。
    """
    samples: list[Sample] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            try:
                samples.append(Sample.from_dict(data))
            except (TypeError, ValueError):
                continue
    return samples


def watch(
    interval: float = 5.0,
    duration: float = 300.0,
    output: str | Path = "memscope-samples.jsonl",
    *,
    fast: bool = True,
    with_services: bool = True,
    collector: Collector | None = None,
    on_sample: Callable[[Sample, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """持续采样并写入文件，返回写入的采样条数。

    :param interval: 采样间隔（秒）
    :param duration: 总时长（秒）；``<= 0`` 表示一直采到被打断
    :param fast: 跳过命令行与版本信息读取，只保留内存与拓扑
    :param should_stop: 可选回调，返回 True 则提前结束（供 CLI 处理 Ctrl+C）
    """
    collector = collector or Collector()
    started = time.monotonic()
    count = 0
    first = True

    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        while True:
            snapshot = take_snapshot(
                fast=fast,
                with_services=with_services,
                collector=collector,
                # 服务表变化很慢，第一次取到后复用即可
                services_cache=not first,
            )
            sample = sample_from_snapshot(snapshot)
            append_sample(handle, sample)
            count += 1
            first = False

            if on_sample is not None:
                try:
                    on_sample(sample, count)
                except Exception:
                    pass  # 回调出问题不该中断采样

            if duration and duration > 0:
                if time.monotonic() - started >= duration:
                    break
            if should_stop is not None and should_stop():
                break

            # 睡到下一个采样点，同时保持对中断的响应
            remaining = interval
            while remaining > 0:
                step = min(0.2, remaining)
                time.sleep(step)
                remaining -= step
                if should_stop is not None and should_stop():
                    return count

    return count


def iter_app_series(samples: Iterable[Sample]) -> dict[str, list[tuple[float, int, str]]]:
    """把采样拆成 ``{应用key: [(时间, 私有字节, 名字), ...]}``。"""
    series: dict[str, list[tuple[float, int, str]]] = {}
    for sample in samples:
        for app in sample.apps:
            series.setdefault(app.key, []).append(
                (sample.timestamp, app.private_bytes, app.name)
            )
    return series
