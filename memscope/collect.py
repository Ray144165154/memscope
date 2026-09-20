"""采集层：把 Windows API 的原始数据拼装成模型对象。

职责边界很清楚——**这里只做拼装，不做任何判断**。角色判定与归因在
:mod:`.classify` / :mod:`.attribution` 里，那两个模块不导入本模块。

两个重要的工程考虑：

1. **缓存**
   采样时每几秒就要重新采集一次。可执行文件路径、命令行、版本资源、
   启动时间这些东西**在一个进程的生命周期内不会变**，重复读取纯属浪费
   （读版本资源要打开文件、解析资源段，不便宜）。
   缓存以 ``(pid, start_time)`` 为键——PID 会被重用，但配上启动时间就不会认错。

2. **容错**
   任何一个进程取不到信息都不能让整次采集失败。真实机器上有近一半的进程
   （系统保护进程）根本打不开，这是常态而非异常。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import winapi
from .model import (
    ProcessRecord,
    ServiceRecord,
    Snapshot,
    SystemMemory,
)

__all__ = ["Collector", "collect_system_memory", "take_snapshot"]


def collect_system_memory() -> SystemMemory:
    """取系统级内存状态（psapi 主数据 + PDH 细分）。"""
    info = winapi.system_performance_info()

    memory = SystemMemory(
        page_size=info.page_size,
        physical_total=info.physical_total,
        physical_available=info.physical_available,
        commit_total=info.commit_total,
        commit_limit=info.commit_limit,
        system_cache=info.system_cache,
        kernel_paged=info.kernel_paged,
        kernel_nonpaged=info.kernel_nonpaged,
        handle_count=info.handle_count,
        process_count=info.process_count,
        thread_count=info.thread_count,
    )

    # 分页列表细分只有性能计数器能给。拿不到就算了，不影响主流程。
    try:
        counters = winapi.memory_counters()
    except Exception:
        counters = {}

    if counters:
        memory.standby_core = int(counters.get("standby_core", 0))
        memory.standby_normal = int(counters.get("standby_normal", 0))
        memory.standby_reserve = int(counters.get("standby_reserve", 0))
        memory.free_and_zero = int(counters.get("free_and_zero", 0))
        memory.modified = int(counters.get("modified", 0))

        # 计数器与 psapi 偶尔会有细微出入，用计数器校正"可用内存"
        available_mb = counters.get("available_mb")
        if available_mb:
            available = int(available_mb * 1024 * 1024)
            if available > 0:
                memory.physical_available = available

    return memory


@dataclass(slots=True)
class _StaticInfo:
    """进程生命周期内不变的字段，用于缓存。"""

    start_time: float | None = None
    exe_path: str | None = None
    command_line: str | None = None
    version: dict[str, str] = field(default_factory=dict)


class Collector:
    """采集器。持有缓存，适合反复调用（采样场景）。"""

    def __init__(self) -> None:
        self._static: dict[int, _StaticInfo] = {}
        self._version_by_path: dict[str, dict[str, str]] = {}
        self._service_cache: dict[int, list[ServiceRecord]] | None = None
        self.access_denied = 0

    # -- 静态信息 --------------------------------------------------------
    def _static_info(self, pid: int, fast: bool) -> _StaticInfo:
        start = None
        try:
            start = winapi.process_start_time(pid)
        except Exception:
            start = None

        cached = self._static.get(pid)
        # 键是 (pid, start_time)：PID 被重用时会自动失效
        if cached is not None and cached.start_time == start:
            return cached

        info = _StaticInfo(start_time=start)
        try:
            info.exe_path = winapi.process_exe_path(pid)
        except Exception:
            info.exe_path = None

        if not fast:
            try:
                info.command_line = winapi.process_command_line(pid)
            except Exception:
                info.command_line = None

            if info.exe_path:
                version = self._version_by_path.get(info.exe_path)
                if version is None:
                    try:
                        version = winapi.file_version_info(info.exe_path)
                    except Exception:
                        version = {}
                    self._version_by_path[info.exe_path] = version
                info.version = version

        self._static[pid] = info
        return info

    # -- 进程 ------------------------------------------------------------
    def collect_processes(self, fast: bool = False) -> list[ProcessRecord]:
        """采集所有进程。

        :param fast: 跳过命令行与版本资源，只取内存与拓扑。
                     采样时用它可以显著降低开销（这两项在进程生命周期内不变）。
        """
        entries = winapi.enumerate_process_entries()
        records: list[ProcessRecord] = []
        self.access_denied = 0

        for entry in entries:
            record = ProcessRecord(
                pid=entry.pid,
                ppid=entry.ppid,
                name=entry.name,
                thread_count=entry.thread_count,
            )

            try:
                counters = winapi.process_memory(entry.pid)
            except Exception:
                counters = None

            if counters is None:
                record.accessible = False
                self.access_denied += 1
            else:
                record.working_set = counters.working_set
                record.peak_working_set = counters.peak_working_set
                record.private_bytes = counters.private_bytes
                record.page_faults = counters.page_faults

            try:
                static = self._static_info(entry.pid, fast)
            except Exception:
                static = _StaticInfo()

            record.start_time = static.start_time
            record.exe_path = static.exe_path
            if not fast:
                record.command_line = static.command_line
                record.company = static.version.get("CompanyName")
                record.product = static.version.get("ProductName")
                record.description = static.version.get("FileDescription")

            try:
                record.session_id = winapi.process_session_id(entry.pid)
            except Exception:
                record.session_id = 0

            records.append(record)

        return records

    # -- 服务 ------------------------------------------------------------
    def collect_services(self, use_cache: bool = False) -> dict[int, list[ServiceRecord]]:
        """采集服务表，返回 ``{宿主进程 pid: [服务]}``。

        这是"方案 B"的数据来源——没有它，svchost 就只是一个黑盒进程名。
        """
        if use_cache and self._service_cache is not None:
            return self._service_cache

        mapping: dict[int, list[ServiceRecord]] = {}
        try:
            entries = winapi.enumerate_services()
        except Exception:
            entries = []

        for entry in entries:
            if not entry.pid:
                continue  # 未运行的服务没有宿主进程
            mapping.setdefault(entry.pid, []).append(
                ServiceRecord(
                    name=entry.name,
                    display_name=entry.display_name,
                    pid=entry.pid,
                    state=entry.state,
                )
            )

        self._service_cache = mapping
        return mapping

    def invalidate_static(self) -> None:
        """清空静态缓存（新进程出现时不必手动清，键含启动时间会自动失效）。"""
        self._static.clear()


def take_snapshot(
    *,
    fast: bool = False,
    with_services: bool = True,
    collector: Collector | None = None,
    services_cache: bool = False,
) -> Snapshot:
    """采集一次完整快照。

    :param fast: 跳过命令行与版本信息，只保留内存与拓扑（采样用）
    :param with_services: 是否解析服务表（方案 B）
    """
    from .attribution import attribute

    collector = collector or Collector()
    system = collect_system_memory()
    processes = collector.collect_processes(fast=fast)

    services = None
    if with_services:
        try:
            services = collector.collect_services(use_cache=services_cache)
        except Exception:
            services = None

    result = attribute(processes, services)
    return Snapshot(
        timestamp=time.time(),
        system=system,
        processes=processes,
        attribution=result,
    )
