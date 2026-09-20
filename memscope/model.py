"""数据模型。

这个模块刻意**不导入任何 Windows 相关的东西**——它只是数据，
因此可以在 Linux / macOS 上导入和测试，也保证了归因逻辑与系统调用彻底解耦。

核心约定：**所有尺寸一律用字节表示**，不做单位换算。格式化只在报告层发生，
这样中间层永远不会因为"这里是 KB 还是字节"而算错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Role",
    "Category",
    "Confidence",
    "ProcessRecord",
    "Application",
    "SystemMemory",
    "ServiceRecord",
    "AttributionResult",
    "Snapshot",
]


class Role(str, Enum):
    """进程在归因时扮演的角色。**角色决定用哪条合并规则**，这是归因的枢纽。"""

    PRODUCT = "product"    # 产品独占型：厂商归属明确，可按产品签名合并
    RUNTIME = "runtime"    # 通用运行时：node / python 等，服务于谁取决于调用方
    SHARED = "shared"      # 共享运行时宿主：必须向上穿透，找到真正的宿主
    LAUNCHER = "launcher"  # 启动器 / 系统代理：不作为归因目标
    SYSTEM = "system"      # Windows 自带组件或服务宿主
    UNKNOWN = "unknown"    # 拿不到足够信息


class Category(str, Enum):
    """报告里的分组依据。"""

    USER = "user"        # 用户安装的第三方应用
    SYSTEM = "system"    # Windows 自带组件与服务
    RUNTIME = "runtime"  # 通用运行时的实例
    SHARED = "shared"    # 未能归因的共享运行时


class Confidence(str, Enum):
    """归因置信度。诚实地区分"确定"与"推测"。"""

    HIGH = "high"      # 父进程链完整，或产品签名一致
    MEDIUM = "medium"  # 只有单一信号（如仅路径推断）
    LOW = "low"        # 孤立进程，纯属推测


@dataclass(slots=True)
class ProcessRecord:
    """一个进程的原始观测数据。

    这是**采集层**（Windows API）与**推理层**（归因）之间唯一的接口。
    采集层只负责填字段，推理层只负责读字段——两边互不知道对方怎么实现，
    于是归因可以被任意构造的数据完整测试。
    """

    pid: int
    ppid: int = 0
    name: str = ""

    exe_path: str | None = None
    command_line: str | None = None

    # 来自可执行文件版本资源
    company: str | None = None
    product: str | None = None
    description: str | None = None

    # 启动时间（Unix 时间戳，秒）。用于识别 PID 重用造成的假父子关系
    start_time: float | None = None

    # 内存指标
    working_set: int = 0
    private_bytes: int = 0
    peak_working_set: int = 0
    page_faults: int = 0

    handle_count: int = 0
    thread_count: int = 0
    session_id: int = 0

    # 是否成功打开了进程（系统保护进程打不开，此时大部分字段为空）
    accessible: bool = True

    @property
    def exe_name(self) -> str:
        """可执行文件名（小写），优先取路径，退回进程名。"""
        if self.exe_path:
            tail = self.exe_path.replace("/", "\\").rsplit("\\", 1)[-1]
            if tail:
                return tail.lower()
        return self.name.lower()

    @property
    def total_bytes(self) -> int:
        """报告里默认展示的"占用"。

        用私有字节而非工作集：工作集会被系统按需修剪、波动很大；
        私有字节（提交量）才是这个进程真正向系统"要"走的量，
        也更接近任务管理器里"内存"那一列的语义。
        """
        return self.private_bytes or self.working_set


@dataclass(slots=True)
class ServiceRecord:
    """一个 Windows 服务。

    ``pid`` 是承载该服务的宿主进程（多数是某个 svchost.exe）。
    多个服务可以共享同一个宿主进程——这正是 svchost 的用途。
    """

    name: str
    display_name: str
    pid: int
    state: str = ""


@dataclass(slots=True)
class Application:
    """归因后的一个"应用"——若干进程的集合。"""

    key: str                      # 分组键，稳定标识
    name: str                     # 展示名
    category: Category
    pids: list[int] = field(default_factory=list)

    working_set: int = 0
    private_bytes: int = 0
    page_faults: int = 0

    confidence: Confidence = Confidence.MEDIUM
    role: Role = Role.UNKNOWN
    evidence: list[str] = field(default_factory=list)

    product: str | None = None
    company: str | None = None

    # 归因到的宿主进程 pid（若本应用是穿透共享运行时得到的）
    anchor_pid: int | None = None

    @property
    def process_count(self) -> int:
        return len(self.pids)

    @property
    def total_bytes(self) -> int:
        return self.private_bytes or self.working_set

    def __str__(self) -> str:
        return self.name


@dataclass(slots=True)
class SystemMemory:
    """系统级内存状态。

    全部以字节为单位。注意 ``commit_total`` 可以**大于** ``physical_total``
    ——差额就住在页面文件里，这正是"看起来内存够用但机器卡"的根源。
    """

    page_size: int = 4096

    physical_total: int = 0
    physical_available: int = 0

    commit_total: int = 0
    commit_limit: int = 0

    system_cache: int = 0
    kernel_paged: int = 0
    kernel_nonpaged: int = 0

    # 分页列表细分（来自性能计数器，可能取不到）
    standby_core: int = 0
    standby_normal: int = 0
    standby_reserve: int = 0
    free_and_zero: int = 0
    modified: int = 0

    handle_count: int = 0
    process_count: int = 0
    thread_count: int = 0

    @property
    def standby_total(self) -> int:
        """待机缓存总量。

        这块内存"看起来被占用"，但随时可以被立即回收——把它算成"已用"
        是绝大多数内存监控工具的误导来源。
        """
        return self.standby_core + self.standby_normal + self.standby_reserve

    @property
    def used_physical(self) -> int:
        return max(0, self.physical_total - self.physical_available)

    @property
    def commit_pressure(self) -> float:
        """提交量占提交上限的比例（0.0-1.0）。"""
        if self.commit_limit <= 0:
            return 0.0
        return self.commit_total / self.commit_limit

    @property
    def commit_exceeds_physical(self) -> int:
        """提交量超出物理内存的部分——这些数据**必须**待在页面文件里。

        大于 0 意味着系统正在依赖页面文件工作，碰这部分内存时会发生硬缺页。
        """
        return max(0, self.commit_total - self.physical_total)

    @property
    def available_ratio(self) -> float:
        if self.physical_total <= 0:
            return 0.0
        return self.physical_available / self.physical_total


@dataclass(slots=True)
class AttributionResult:
    """归因的整体结果。"""

    applications: list[Application] = field(default_factory=list)
    # pid -> 归因依据，供 --explain 审计
    explanations: dict[int, list[str]] = field(default_factory=dict)
    # 修复进程树时发现的问题，例如 PID 重用
    tree_repairs: list[str] = field(default_factory=list)
    # 无法归因的进程 pid
    unresolved: list[int] = field(default_factory=list)

    def by_category(self, category: Category) -> list[Application]:
        return [a for a in self.applications if a.category is category]

    @property
    def total_bytes(self) -> int:
        return sum(a.total_bytes for a in self.applications)


@dataclass(slots=True)
class Snapshot:
    """某一时刻的完整观测结果——采集的最终产物。"""

    timestamp: float
    system: SystemMemory
    processes: list[ProcessRecord] = field(default_factory=list)
    attribution: AttributionResult = field(default_factory=AttributionResult)

    @property
    def applications(self) -> list[Application]:
        return self.attribution.applications

    @property
    def services(self) -> list[ServiceRecord]:
        # 服务不在快照里直接保存，只作为归因的输入
        return []
