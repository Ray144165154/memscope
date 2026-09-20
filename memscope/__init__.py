"""memscope —— 一个诚实的 Windows 内存诊断器。

它不"释放内存"，它只做三件事：**把系统内存状态解释对**、
**把内存开销归因到具体应用**、**找出正在悄悄增长的东西**。

绝大多数"内存优化"软件靠调用 ``EmptyWorkingSet`` 把进程的工作集强行清空，
看起来内存数字变好看了，实际上那些页面被赶到页面文件，程序下一秒就要
硬缺页从硬盘读回来——**结果是电脑更慢**。本工具的存在意义之一就是
把这件事说清楚。

用法::

    python -m memscope                  # 快照报告
    python -m memscope --all            # 展开系统组件与服务
    python -m memscope --explain        # 显示每条归因的依据
    python -m memscope watch -i 5 -d 300 -o samples.jsonl
    python -m memscope analyze samples.jsonl
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Application",
    "AttributionResult",
    "Category",
    "Confidence",
    "ProcessRecord",
    "Role",
    "ServiceRecord",
    "Snapshot",
    "SystemMemory",
    "Insight",
    "Severity",
    "Trend",
    "attribute",
    "diagnose",
    "analyze_samples",
    "take_snapshot",
    "render_text",
    "render_html",
]

from .analysis import Trend, analyze_samples
from .attribution import attribute
from .collect import take_snapshot
from .diagnose import Insight, Severity, diagnose
from .model import (
    Application,
    AttributionResult,
    Category,
    Confidence,
    ProcessRecord,
    Role,
    ServiceRecord,
    Snapshot,
    SystemMemory,
)
from .report import render_html, render_text
