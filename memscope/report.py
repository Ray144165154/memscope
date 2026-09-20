"""报告渲染：文本与 HTML（零依赖）。

文本报告面向终端，HTML 报告面向分享与存档。两者都不依赖任何第三方库
——HTML 里的图表是**手写 SVG**，没有图表库。

设计上刻意避免的一件事：**不用进度条制造焦虑**。典型的"内存监控"会把
"空闲内存"画成一条几乎空掉的红色进度条，暗示你"内存快用光了"。
本工具的做法相反——把使用中、待机缓存、空闲三段**并列展示**，
并在旁边写明待机缓存随时可用，从视觉上就传达出正确的认知。
"""

from __future__ import annotations

import html
from datetime import datetime

from .analysis import Trend
from .diagnose import Insight, Severity
from .model import Application, Category, Snapshot

__all__ = ["human_bytes", "human_mb", "render_text", "render_html", "SEVERITY_LABEL"]

SEVERITY_LABEL = {
    Severity.OK: "正常",
    Severity.INFO: "说明",
    Severity.NOTICE: "注意",
    Severity.WARNING: "警告",
}

SEVERITY_MARK = {
    Severity.OK: "[OK]  ",
    Severity.INFO: "[i]   ",
    Severity.NOTICE: "[!]   ",
    Severity.WARNING: "[!!]  ",
}

CATEGORY_LABEL = {
    Category.USER: "用户应用",
    Category.SYSTEM: "系统组件",
    Category.RUNTIME: "通用运行时",
    Category.SHARED: "未归因的共享运行时",
}

CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}

WIDTH = 78


def human_bytes(value: int) -> str:
    """把字节数格式化成人读得懂的字符串。"""
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= scale:
            return f"{sign}{value / scale:.2f} {unit}"
    return f"{sign}{value} B"


def human_mb(value: int) -> str:
    return f"{value / (1024 ** 2):.0f} MB"


def _bar(fraction: float, width: int = 40, char: str = "█") -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return char * filled + "·" * (width - filled)


def _rule(title: str = "") -> str:
    if not title:
        return "─" * WIDTH
    left = f"── {title} "
    return left + "─" * max(0, WIDTH - len(left))


def _memory_section(snapshot: Snapshot) -> list[str]:
    memory = snapshot.system
    total = memory.physical_total or 1
    lines: list[str] = []

    used = memory.used_physical
    standby = memory.standby_total
    available = memory.physical_available

    lines.append(f"物理内存总量        {human_bytes(memory.physical_total):>12}")
    lines.append("")
    lines.append(
        f"  使用中            {human_bytes(used):>12}   {_bar(used / total, 30)}"
    )
    lines.append(
        f"  待机缓存          {human_bytes(standby):>12}   {_bar(standby / total, 30)}"
        "   随时可用"
    )
    lines.append(
        f"  空闲+零页         {human_bytes(memory.free_and_zero):>12}   "
        f"{_bar(memory.free_and_zero / total, 30)}"
    )
    lines.append("")
    lines.append(
        f"  可用合计          {human_bytes(available):>12}   "
        f"{_bar(available / total, 30)}   ({memory.available_ratio:.0%})"
    )
    lines.append("")
    lines.append(f"提交量（程序申请）  {human_bytes(memory.commit_total):>12}")
    lines.append(
        f"提交上限            {human_bytes(memory.commit_limit):>12}   "
        f"(已用 {memory.commit_pressure:.0%})"
    )

    excess = memory.commit_exceeds_physical
    if excess > 0:
        lines.append(
            f"  └ 超出物理内存    {human_bytes(excess):>12}   "
            "只能在页面文件（硬盘）里"
        )

    lines.append("")
    lines.append(
        f"内核池  分页 {human_mb(memory.kernel_paged)} / "
        f"非分页 {human_mb(memory.kernel_nonpaged)}       "
        f"句柄 {memory.handle_count:,}  进程 {memory.process_count:,}  "
        f"线程 {memory.thread_count:,}"
    )
    return lines


def _app_table(applications: list[Application], limit: int, show_confidence: bool) -> list[str]:
    if not applications:
        return ["  （无）"]

    lines: list[str] = []
    widest = max((len(a.name) for a in applications[:limit]), default=10)
    name_width = min(max(widest, 18), 42)

    header = f"  {'应用':<{name_width}} {'进程':>4} {'私有内存':>10} {'占比':>6}"
    if show_confidence:
        header += f" {'置信度':>6}"
    lines.append(header)
    lines.append("  " + "─" * (len(header) - 2))

    total = sum(a.total_bytes for a in applications) or 1
    for app in applications[:limit]:
        name = app.name
        if len(name) > name_width:
            name = name[: name_width - 1] + "…"
        lines.append(
            f"  {name:<{name_width}} {app.process_count:>4} "
            f"{human_mb(app.total_bytes):>10} {app.total_bytes / total:>5.1%}"
            + (f" {CONFIDENCE_LABEL.get(app.confidence.value, '?'):>6}" if show_confidence else "")
        )
    return lines


def render_text(
    snapshot: Snapshot,
    insights: list[Insight] | None = None,
    *,
    limit: int = 20,
    show_confidence: bool = True,
    show_system: bool = False,
    explain_pids: list[int] | None = None,
) -> str:
    """渲染文本报告。"""
    apps = snapshot.applications
    out: list[str] = []

    when = datetime.fromtimestamp(snapshot.timestamp).strftime("%Y-%m-%d %H:%M:%S")
    out.append(_rule("memscope — Windows 内存诊断"))
    out.append(f"采集时间  {when}")
    out.append("")

    out.append(_rule("系统内存"))
    out.extend(_memory_section(snapshot))
    out.append("")

    if insights:
        out.append(_rule("诊断结论"))
        for insight in insights:
            mark = SEVERITY_MARK.get(insight.severity, "      ")
            out.append(f"{mark}{insight.title}")
            for line in insight.detail.splitlines():
                out.append(f"      {line}" if line.strip() else "")
            if insight.suggestion:
                for line in insight.suggestion.splitlines():
                    out.append(f"      → {line}")
            out.append("")

    user_apps = [a for a in apps if a.category is Category.USER]
    runtime_apps = [a for a in apps if a.category is Category.RUNTIME]
    shared_apps = [a for a in apps if a.category is Category.SHARED]
    system_apps = [a for a in apps if a.category is Category.SYSTEM]

    out.append(_rule("应用占用排行（第三方软件）"))
    out.extend(_app_table(user_apps, limit, show_confidence))
    out.append("")

    if runtime_apps:
        out.append(_rule("通用运行时"))
        out.extend(_app_table(runtime_apps, limit, show_confidence))
        out.append("")

    if shared_apps:
        out.append(_rule("未归因的共享运行时"))
        out.append("  这些进程是 WebView2 之类的共享运行时，但父进程链已断，")
        out.append("  无法确定它们服务于哪个应用——单独列出而不是猜一个名字。")
        out.append("")
        out.extend(_app_table(shared_apps, limit, show_confidence))
        out.append("")

    system_total = sum(a.total_bytes for a in system_apps)
    out.append(_rule("系统组件"))
    out.append(
        f"  共 {len(system_apps)} 项，合计 {human_bytes(system_total)}"
        f"（占全部 {system_total / (sum(a.total_bytes for a in apps) or 1):.0%}）"
    )
    if show_system:
        out.append("")
        out.extend(_app_table(system_apps, limit, show_confidence))
    else:
        out.append("  用 --all 展开明细")
    out.append("")

    if explain_pids:
        out.append(_rule("归因依据（--explain）"))
        by_pid = {p.pid: p for p in snapshot.processes}
        for pid in explain_pids:
            proc = by_pid.get(pid)
            if proc is None:
                out.append(f"  PID {pid}: 不存在")
                continue
            owner = None
            for app in apps:
                if pid in app.pids:
                    owner = app
                    break
            out.append(
                f"  PID {pid}  {proc.name}"
                f"  →  {owner.name if owner else '未归因'}"
            )
            for line in snapshot.attribution.explanations.get(pid, []):
                out.append(f"      · {line}")
        out.append("")

    repairs = snapshot.attribution.tree_repairs
    if repairs:
        out.append(_rule("进程树修复"))
        for note in repairs[:10]:
            out.append(f"  · {note}")
        if len(repairs) > 10:
            out.append(f"  · …还有 {len(repairs) - 10} 处")
        out.append("")

    out.append(_rule("说明"))
    out.append(
        f"  共归因 {len(apps)} 个应用 / {len(snapshot.processes)} 个进程；"
        f"其中 {sum(1 for p in snapshot.processes if not p.accessible)} 个进程"
        "因系统保护无法读取详情。"
    )
    out.append("  本工具不「释放内存」。清理待机缓存或修剪工作集只会让系统变慢——")
    out.append("  详见 README 中的说明。")
    out.append(_rule())

    return "\n".join(out)


# --------------------------------------------------------------------------
# HTML 报告
# --------------------------------------------------------------------------

_HTML_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px;
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  background: #f6f7f9; color: #1c1e21; line-height: 1.6;
}
.wrap { max-width: 940px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 12px; padding-bottom: 6px;
     border-bottom: 1px solid #dfe1e5; }
.sub { color: #65676b; font-size: 13px; margin-bottom: 24px; }
.card { background: #fff; border: 1px solid #dfe1e5; border-radius: 10px;
        padding: 16px 18px; margin-bottom: 12px; }
.card.ok      { border-left: 4px solid #31a24c; }
.card.info    { border-left: 4px solid #1877f2; }
.card.notice  { border-left: 4px solid #f7b928; }
.card.warning { border-left: 4px solid #e41e3f; }
.card h3 { margin: 0 0 8px; font-size: 15px; }
.card p { margin: 0 0 8px; font-size: 14px; white-space: pre-wrap; }
.card .tip { font-size: 13px; color: #1c1e21; background: #eef2f7;
             border-radius: 6px; padding: 8px 10px; margin-top: 8px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px;
       border-radius: 10px; background: #e4e6eb; margin-right: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #ebedf0; }
th { color: #65676b; font-weight: 600; font-size: 12.5px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.total td { font-weight: 600; border-top: 2px solid #dfe1e5; }
.bar { height: 9px; border-radius: 5px; background: #1877f2; min-width: 2px; }
.bar.sys { background: #8a8d91; }
.bar.rt  { background: #9b59b6; }
.mono { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12.5px; }
.note { font-size: 12.5px; color: #65676b; margin-top: 20px;
        border-top: 1px solid #dfe1e5; padding-top: 14px; }
.legend { font-size: 12.5px; color: #65676b; margin-top: 6px; }
"""


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _svg_memory_bar(snapshot: Snapshot, width: int = 880, height: int = 46) -> str:
    """手写 SVG 内存条：把"使用中/待机缓存/空闲"三段并列画出来。

    刻意**不**把"空闲内存"单独画成一条几乎空掉的红色进度条——
    那正是制造焦虑的常见手法。三段并列才能传达正确认知：
    待机缓存随时可用，不是被浪费掉了。
    """
    memory = snapshot.system
    total = memory.physical_total or 1

    used = memory.used_physical
    standby = memory.standby_total
    free = max(0, total - used)

    segments = [
        ("使用中", used, "#1877f2"),
        ("待机缓存", standby, "#8bc34a"),
        ("空闲", max(0, free - standby), "#dfe1e5"),
    ]

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="物理内存构成">'
    ]
    x = 0.0
    for label, value, colour in segments:
        fraction = max(0.0, min(1.0, value / total))
        segment_width = fraction * width
        if segment_width <= 0:
            continue
        parts.append(
            f'<rect x="{x:.2f}" y="0" width="{segment_width:.2f}" height="30" '
            f'fill="{colour}" rx="3" />'
        )
        if segment_width > 70:
            parts.append(
                f'<text x="{x + segment_width / 2:.2f}" y="20" text-anchor="middle" '
                f'font-size="12" fill="#fff">{_esc(label)} '
                f'{human_bytes(value)}</text>'
            )
        x += segment_width
    parts.append("</svg>")
    return "".join(parts)


def render_html(
    snapshot: Snapshot,
    insights: list[Insight] | None = None,
    trends: list[Trend] | None = None,
) -> str:
    """渲染一份自包含的 HTML 报告（样式与图表全部内联，无外部依赖）。"""
    memory = snapshot.system
    when = datetime.fromtimestamp(snapshot.timestamp).strftime("%Y-%m-%d %H:%M:%S")
    apps = snapshot.applications
    largest = max((a.total_bytes for a in apps), default=1) or 1

    out: list[str] = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append("<title>memscope 内存诊断报告</title>")
    out.append(f"<style>{_HTML_STYLE}</style>")
    out.append("</head><body><div class='wrap'>")

    out.append("<h1>memscope 内存诊断报告</h1>")
    out.append(f"<div class='sub'>采集时间 {_esc(when)}　·　"
               f"{len(snapshot.processes)} 个进程　·　{len(apps)} 个应用</div>")

    # ---- 内存概览 ----
    out.append("<h2>物理内存构成</h2>")
    out.append("<div class='card'>")
    out.append(_svg_memory_bar(snapshot))
    out.append(
        "<div class='legend'>待机缓存中的内存<b>随时可以被立即回收</b>给需要的程序，"
        "速度与空闲内存没有区别。它的存在说明系统缓存做得很好，"
        "而不是内存被浪费了。</div>"
    )
    out.append("<table style='margin-top:12px'>")
    rows = [
        ("物理内存总量", human_bytes(memory.physical_total)),
        ("使用中", human_bytes(memory.used_physical)),
        ("待机缓存", human_bytes(memory.standby_total)),
        ("空闲 + 零页", human_bytes(memory.free_and_zero)),
        ("可用合计", f"{human_bytes(memory.physical_available)} "
                     f"({memory.available_ratio:.0%})"),
        ("提交量（程序申请）", human_bytes(memory.commit_total)),
        ("提交上限", f"{human_bytes(memory.commit_limit)} "
                     f"(已用 {memory.commit_pressure:.0%})"),
    ]
    if memory.commit_exceeds_physical > 0:
        rows.append(
            ("　└ 超出物理内存", human_bytes(memory.commit_exceeds_physical)
             + "（只能在页面文件里）")
        )
    rows.append(
        ("内核池", f"分页 {human_mb(memory.kernel_paged)} / "
                   f"非分页 {human_mb(memory.kernel_nonpaged)}")
    )
    for label, value in rows:
        out.append(f"<tr><td>{_esc(label)}</td><td class='num'>{_esc(value)}</td></tr>")
    out.append("</table></div>")

    # ---- 诊断结论 ----
    if insights:
        out.append("<h2>诊断结论</h2>")
        for insight in insights:
            severity = insight.severity.value
            out.append(f"<div class='card {severity}'>")
            out.append(
                f"<h3><span class='tag'>{_esc(SEVERITY_LABEL[insight.severity])}</span>"
                f"{_esc(insight.title)}</h3>"
            )
            out.append(f"<p>{_esc(insight.detail)}</p>")
            if insight.suggestion:
                out.append(f"<div class='tip'>→ {_esc(insight.suggestion)}</div>")
            out.append("</div>")

    # ---- 应用排行 ----
    for category, heading in (
        (Category.USER, "第三方应用"),
        (Category.RUNTIME, "通用运行时"),
        (Category.SHARED, "未归因的共享运行时"),
    ):
        subset = [a for a in apps if a.category is category]
        if not subset:
            continue
        out.append(f"<h2>{_esc(heading)}</h2>")
        out.append("<div class='card'><table>")
        out.append(
            "<tr><th>应用</th><th class='num'>进程</th>"
            "<th class='num'>私有内存</th><th style='width:26%'></th></tr>"
        )
        for app in subset[:30]:
            width = max(1, int(app.total_bytes / largest * 100))
            css = "bar" if category is Category.USER else (
                "bar rt" if category is Category.RUNTIME else "bar sys"
            )
            out.append(
                f"<tr><td>{_esc(app.name)}</td>"
                f"<td class='num'>{app.process_count}</td>"
                f"<td class='num'>{_esc(human_mb(app.total_bytes))}</td>"
                f"<td><div class='{css}' style='width:{width}%'></div></td></tr>"
            )
        out.append("</table></div>")

    # ---- 趋势 ----
    # trends 为 None 表示"没做过趋势分析"，为 [] 表示"分析过但什么都没有"。
    # 两者含义不同，报告里要区分开。
    if trends is not None:
        alerts = [t for t in trends if t.is_alert]
        out.append("<h2>增长趋势</h2>")
        if not alerts:
            out.append(
                "<div class='card ok'><h3>没有发现持续增长的进程</h3>"
                "<p>所有被分析的进程在采样期间都保持稳定。</p></div>"
            )
        else:
            out.append("<div class='card'><table>")
            out.append(
                "<tr><th>应用</th><th class='num'>起始</th><th class='num'>结束</th>"
                "<th class='num'>增长/小时</th><th class='num'>拟合 R²</th>"
                "<th>判定</th></tr>"
            )
            verdict_label = {
                "leak": "疑似泄漏", "growing": "持续增长",
                "stable": "稳定", "shrinking": "在减小",
            }
            for trend in alerts:
                out.append(
                    f"<tr><td>{_esc(trend.name)}</td>"
                    f"<td class='num'>{_esc(human_mb(trend.first_bytes))}</td>"
                    f"<td class='num'>{_esc(human_mb(trend.last_bytes))}</td>"
                    f"<td class='num'>{trend.slope_mb_per_hour:+.1f} MB</td>"
                    f"<td class='num'>{trend.r_squared:.2f}</td>"
                    f"<td>{_esc(verdict_label.get(trend.verdict, trend.verdict))}</td></tr>"
                )
            out.append("</table></div>")

    out.append(
        "<div class='note'>memscope 不「释放内存」。"
        "清理待机缓存或强制修剪进程工作集只会把数据赶到页面文件，"
        "程序下一次访问时触发硬缺页从硬盘读回——数字好看了，系统反而更慢。"
        "本报告只做观测、归因与趋势分析。</div>"
    )
    out.append("</div></body></html>")
    return "\n".join(out)
