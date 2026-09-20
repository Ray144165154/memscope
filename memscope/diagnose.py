"""诊断规则 —— 把观测数据变成可核对的结论。

这是本工具与"内存清理软件"最根本的区别所在。

那类软件产出的是 **"已释放 2 GB"** ——一个你无法验证、而且实际上让系统
变慢的许诺。本模块产出的是 **"条件 → 事实 → 因果解释 → 建议"**，
每一条都带着具体数字，你可以自己核对。

因此本模块的两条硬性纪律：

1. **绝不建议清理待机缓存或修剪工作集。**
   那正是流氓软件在做的事：把页面赶到页面文件，程序下一瞬间又要硬缺页
   读回来，结果是磁盘狂转、程序卡顿。这里不仅不建议，还要主动解释
   为什么不该做。

2. **数字必须能对上。**
   "空闲内存只剩 190 MB 了"这句话本身是误导——因为还有几个 GB 在待机
   缓存里随时可用。规则必须把这个关系讲清楚，而不是制造焦虑。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model import Application, SystemMemory

__all__ = ["Severity", "Insight", "diagnose", "background_groups"]


class Severity(str, Enum):
    OK = "ok"            # 一切正常
    INFO = "info"        # 值得知道，无需行动
    NOTICE = "notice"    # 值得注意
    WARNING = "warning"  # 建议处理


@dataclass(slots=True)
class Insight:
    """一条诊断结论。"""

    severity: Severity
    title: str
    detail: str
    suggestion: str | None = None

    def __str__(self) -> str:
        return self.title


# --------------------------------------------------------------------------
# 阈值（集中定义，便于调整与解释）
# --------------------------------------------------------------------------

# 物理可用低于此比例视为紧张
AVAILABLE_TIGHT = 0.10
AVAILABLE_LOW = 0.20

# 提交量占提交上限超过此比例视为接近上限
COMMIT_PRESSURE_HIGH = 0.90
COMMIT_PRESSURE_NOTICE = 0.75

# 内核非分页池超过此值值得关注（驱动泄漏的常见信号）
KERNEL_NONPAGED_NOTICE = 1024 ** 3  # 1 GB

# 待机缓存"看起来很大"的门槛——不是问题，但需要解释
STANDBY_NOTABLE = 1024 ** 3


# 常见"装了但未必需要常驻"的软件类别。
# 仅用于给出"可以考虑关闭"的建议，判断依据是厂商与产品名里的关键词。
BACKGROUND_GROUPS: dict[str, tuple[str, ...]] = {
    "游戏平台": (
        "steam", "electronic arts", "ea desktop", "epic games", "ubisoft",
        "battle.net", "blizzard", "gog galaxy", "riot client", "wegame",
    ),
    "外设驱动软件": (
        "razer", "logitech", "corsair", "steelseries", "armoury", "asus",
        "msi", "gigabyte", "hyperx", "coolermaster",
    ),
    "显卡增强/覆盖层": (
        "nvidia", "geforce", "radeon", "amd software", "overwolf",
    ),
    "远程控制": (
        "awesun", "向日葵", "oray", "teamviewer", "anydesk", "todesk",
        "rustdesk", "sunlogin",
    ),
    "云盘同步": (
        "onedrive", "dropbox", "google drive", "baidunetdisk", "百度网盘",
        "aliyundrive", "坚果云", "nutstore",
    ),
    "即时通讯/娱乐": (
        "tencent", "qq", "wechat", "微信", "discord", "spotify", "网易云",
        "腾讯视频", "iqiyi", "爱奇艺", "bilibili",
    ),
}


def background_groups(applications: list[Application]) -> dict[str, list[Application]]:
    """把应用按"后台常驻类别"归类。

    只做关键词匹配，**不保证准确**——它的用途是提示"这类软件你可能装了
    不止一个"，而不是替用户下结论。调用方在报告里应当明确这一点。
    """
    groups: dict[str, list[Application]] = {}
    for app in applications:
        haystack = " ".join(
            filter(None, [app.name, app.product or "", app.company or ""])
        ).lower()
        for group, keywords in BACKGROUND_GROUPS.items():
            if any(keyword in haystack for keyword in keywords):
                groups.setdefault(group, []).append(app)
                break
    return groups


def _gb(value: int) -> float:
    return value / (1024 ** 3)


def _mb(value: int) -> float:
    return value / (1024 ** 2)


def diagnose(
    memory: SystemMemory,
    applications: list[Application] | None = None,
) -> list[Insight]:
    """根据系统内存状态与归因结果产出诊断结论。"""
    insights: list[Insight] = []

    # ---- 规则 1：提交量 vs 物理内存（最重要的一条）----
    excess = memory.commit_exceeds_physical
    if excess > 0:
        insights.append(
            Insight(
                severity=Severity.WARNING if _gb(excess) > 1 else Severity.NOTICE,
                title=(
                    f"提交量 ({_gb(memory.commit_total):.2f} GB) 已超过物理内存 "
                    f"({_gb(memory.physical_total):.2f} GB)"
                ),
                detail=(
                    f"超出 {_gb(excess):.2f} GB。这部分数据**不可能**待在内存里，"
                    "只能留在页面文件（硬盘）上。程序一旦访问到这部分内存，"
                    "就会触发硬缺页，必须从硬盘读回来——这正是切换程序时卡顿、"
                    "响应变慢的主要来源。\n"
                    "注意：这是「提交量」（程序申请的内存总量），不是「物理占用」。"
                    "系统允许超额提交，所以这个数字超过物理内存本身并不算故障，"
                    "但它意味着系统已经在依赖页面文件工作了。"
                ),
                suggestion=(
                    "减少常驻程序（见下方列表）比加内存更省事；"
                    "如果长期如此且预算允许，加内存是最直接的解法。"
                    "另外可以检查页面文件是否放在了机械硬盘上——"
                    "那会显著放大硬缺页的代价。"
                ),
            )
        )
    else:
        insights.append(
            Insight(
                severity=Severity.OK,
                title="提交量未超过物理内存",
                detail=(
                    f"提交 {_gb(memory.commit_total):.2f} GB，"
                    f"物理内存 {_gb(memory.physical_total):.2f} GB，"
                    f"还有 {_gb(memory.physical_total - memory.commit_total):.2f} GB 余量。"
                    "系统不需要为了腾地方而把程序数据挪到硬盘上。"
                ),
            )
        )

    # ---- 规则 2：物理可用比例 ----
    # 只有拿得到物理内存总量时才判断——否则会把"取不到数据"误报成"内存紧张"。
    ratio = memory.available_ratio
    if memory.physical_total <= 0:
        pass
    elif ratio < AVAILABLE_TIGHT:
        insights.append(
            Insight(
                severity=Severity.WARNING,
                title=f"物理可用内存偏低（{ratio:.1%}，{_gb(memory.physical_available):.2f} GB）",
                detail=(
                    "低于 10% 时，系统会更频繁地回收页面，"
                    "内存分配变慢，新启动的程序会明显感觉迟钝。"
                ),
                suggestion="关闭当前不需要的大型程序。",
            )
        )
    elif ratio < AVAILABLE_LOW:
        insights.append(
            Insight(
                severity=Severity.NOTICE,
                title=f"物理可用内存中等（{ratio:.1%}，{_gb(memory.physical_available):.2f} GB）",
                detail="仍有余量，但不算宽裕。",
            )
        )

    # ---- 规则 3：解释"空闲内存为什么这么少"（主动破除误导）----
    if memory.free_and_zero < memory.standby_total and memory.standby_total > STANDBY_NOTABLE:
        insights.append(
            Insight(
                severity=Severity.INFO,
                title=(
                    f"「空闲内存」只有 {_mb(memory.free_and_zero):.0f} MB，"
                    "但这是正常的"
                ),
                detail=(
                    f"另外还有 {_gb(memory.standby_total):.2f} GB 在**待机缓存**里。"
                    "待机缓存装的是系统预测你马上会用到、但当前没人用的数据"
                    f"（核心 {_mb(memory.standby_core):.0f} MB + "
                    f"常规 {_mb(memory.standby_normal):.0f} MB + "
                    f"保留 {_mb(memory.standby_reserve):.0f} MB）。"
                    "只要有程序需要内存，这部分会**立刻**被回收给它，速度与空闲内存"
                    "没有区别。\n"
                    "换句话说：待机缓存越大，说明系统缓存做得越好，"
                    "而不是内存被浪费了。"
                ),
                suggestion=(
                    "不要使用任何声称能「释放内存」的工具清理这部分。"
                    "它们只是把数据丢掉、让程序重新从硬盘读一遍，"
                    "数字好看了，系统反而更慢。"
                ),
            )
        )

    # ---- 规则 4：提交上限压力 ----
    pressure = memory.commit_pressure
    if pressure >= COMMIT_PRESSURE_HIGH:
        insights.append(
            Insight(
                severity=Severity.WARNING,
                title=f"提交量已用掉提交上限的 {pressure:.1%}",
                detail=(
                    f"提交上限 {_gb(memory.commit_limit):.2f} GB（物理内存 + 页面文件）。"
                    "接近上限时，新程序可能直接申请内存失败并报错。"
                ),
                suggestion="增大页面文件，或关闭不需要的程序。",
            )
        )
    elif pressure >= COMMIT_PRESSURE_NOTICE:
        insights.append(
            Insight(
                severity=Severity.INFO,
                title=f"提交量占用上限的 {pressure:.1%}",
                detail=f"提交上限 {_gb(memory.commit_limit):.2f} GB，尚未接近极限。",
            )
        )

    # ---- 规则 5：内核池 ----
    if memory.kernel_nonpaged > KERNEL_NONPAGED_NOTICE:
        insights.append(
            Insight(
                severity=Severity.NOTICE,
                title=f"内核非分页池偏大（{_mb(memory.kernel_nonpaged):.0f} MB）",
                detail=(
                    "非分页池是内核与驱动申请、**不能被换出到硬盘**的内存。"
                    "正常情况下在几百 MB 量级；持续增长往往意味着某个驱动存在泄漏。"
                ),
                suggestion=(
                    "如果这个数字随时间不断增大，用 `memscope watch` 采样后分析，"
                    "再结合驱动列表排查。"
                ),
            )
        )

    # ---- 规则 6：后台常驻软件群 ----
    if applications:
        groups = background_groups(applications)
        crowded = {
            name: apps
            for name, apps in groups.items()
            if apps and sum(a.total_bytes for a in apps) > 200 * 1024 ** 2
        }
        if crowded:
            lines = []
            total = 0
            for name, apps in sorted(
                crowded.items(), key=lambda kv: -sum(a.total_bytes for a in kv[1])
            ):
                size = sum(a.total_bytes for a in apps)
                total += size
                detail = "、".join(
                    f"{a.name}({_mb(a.total_bytes):.0f}MB)" for a in apps[:4]
                )
                lines.append(f"  · {name}：{_mb(size):.0f} MB — {detail}")
            insights.append(
                Insight(
                    severity=Severity.NOTICE,
                    title=f"检测到 {len(crowded)} 类「装了但未必需要常驻」的软件",
                    detail="\n".join(lines) + f"\n合计约 {_gb(total):.2f} GB。",
                    suggestion=(
                        "这些软件多数提供「关闭开机自启」或「退出后台」选项。"
                        "注意：本分类只按厂商/产品名做关键词匹配，"
                        "**可能认错**——请自己核对列表再决定。"
                    ),
                )
            )

    return insights
