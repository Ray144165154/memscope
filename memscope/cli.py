"""命令行接口。

三个子命令：

    memscope                    快照：现在谁在占内存 + 诊断
    memscope watch              持续采样，写入 JSONL
    memscope analyze 文件       分析采样，找出正在增长的东西

不加子命令时默认执行快照。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__, winapi
from .analysis import analyze_samples
from .collect import take_snapshot
from .diagnose import diagnose
from .model import Category
from .report import human_bytes, render_html, render_text
from .sampling import read_samples, watch


def _configure_output_encoding() -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 上 stdout 被重定向时的默认编码是 GBK / cp1252，
    而本工具的输出（包括 argparse 的帮助与用法文本）全是中文——
    不重配会直接抛 UnicodeEncodeError。

    这个函数在 ``main()`` 与 ``build_parser()`` 里各调用一次。
    后者是为了让**任何**直接使用 ``build_parser()`` 的调用方
    （测试、把本工具嵌进别的程序）在打印 ``--help`` 时也不会崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _write(text: str) -> None:
    if not text:
        return
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
        else:
            buffer.write(text.encode("utf-8", "replace"))


def _error(message: str) -> None:
    print(message, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    # 先配好输出编码：帮助与用法文本里有中文，在非 UTF-8 输出流上
    # argparse 打印它们会抛 UnicodeEncodeError（CI 的 Windows 任务踩过）。
    _configure_output_encoding()

    parser = argparse.ArgumentParser(
        prog="memscope",
        description="诚实的内存诊断器 —— 观测、归因、找增长，不「释放内存」",
        epilog=(
            "示例:\n"
            "  memscope                      快照报告\n"
            "  memscope --all                展开系统组件明细\n"
            "  memscope --explain 1234       查看某个进程的归因依据\n"
            "  memscope --html 报告.html     生成可分享的 HTML 报告\n"
            "  memscope watch -i 5 -d 600    采样 10 分钟（每 5 秒一次）\n"
            "  memscope analyze samples.jsonl  分析增长趋势\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"memscope {__version__}")

    # 快照参数挂在外层，这样不带子命令也能用
    parser.add_argument("-n", "--limit", type=int, default=20,
                        help="每个分类最多显示多少条（默认 20）")
    parser.add_argument("-a", "--all", action="store_true",
                        help="展开系统组件明细（默认折叠）")
    parser.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--html", metavar="FILE", help="输出 HTML 报告到文件")
    parser.add_argument("-e", "--explain", metavar="PID", type=int, action="append",
                        help="显示指定进程的归因依据，可重复")
    parser.add_argument("--no-services", action="store_true",
                        help="跳过服务表解析（svchost 将无法按服务名细分）")
    parser.add_argument("--fast", action="store_true",
                        help="跳过命令行与版本信息读取，采集更快但归因精度下降")
    parser.add_argument("-q", "--quiet", action="store_true", help="抑制标准错误输出")

    sub = parser.add_subparsers(dest="command")

    watch_parser = sub.add_parser("watch", help="持续采样并写入 JSONL")
    watch_parser.add_argument("-i", "--interval", type=float, default=5.0,
                              help="采样间隔秒数（默认 5）")
    watch_parser.add_argument("-d", "--duration", type=float, default=300.0,
                              help="总时长秒数，0 表示一直采到 Ctrl+C（默认 300）")
    watch_parser.add_argument("-o", "--output", default="memscope-samples.jsonl",
                              help="输出文件（默认 memscope-samples.jsonl）")
    watch_parser.add_argument(
        "--fast", action="store_true",
        help=("跳过命令行与版本信息读取。**会显著降低归因精度**"
              "（应用无法按产品签名合并，置信度普遍降为「低」），"
              "因此默认关闭。由于静态信息有缓存，它只对首次采集有提速效果。"),
    )
    watch_parser.add_argument("--no-services", action="store_true",
                              help="跳过服务表解析")

    analyze_parser = sub.add_parser("analyze", help="分析采样文件，找出增长趋势")
    analyze_parser.add_argument("file", help="watch 生成的 JSONL 文件")
    analyze_parser.add_argument("-n", "--limit", type=int, default=20)
    analyze_parser.add_argument("--html", metavar="FILE", help="输出 HTML 报告")
    analyze_parser.add_argument("-j", "--json", action="store_true")

    return parser


def _snapshot_to_dict(snapshot) -> dict:
    memory = snapshot.system
    return {
        "version": __version__,
        "timestamp": snapshot.timestamp,
        "system": {
            "page_size": memory.page_size,
            "physical_total": memory.physical_total,
            "physical_available": memory.physical_available,
            "used_physical": memory.used_physical,
            "commit_total": memory.commit_total,
            "commit_limit": memory.commit_limit,
            "commit_pressure": round(memory.commit_pressure, 4),
            "commit_exceeds_physical": memory.commit_exceeds_physical,
            "standby_total": memory.standby_total,
            "standby_core": memory.standby_core,
            "standby_normal": memory.standby_normal,
            "standby_reserve": memory.standby_reserve,
            "free_and_zero": memory.free_and_zero,
            "modified": memory.modified,
            "kernel_paged": memory.kernel_paged,
            "kernel_nonpaged": memory.kernel_nonpaged,
            "handle_count": memory.handle_count,
            "process_count": memory.process_count,
            "thread_count": memory.thread_count,
        },
        "processes": len(snapshot.processes),
        "accessible_processes": sum(1 for p in snapshot.processes if p.accessible),
        "applications": [
            {
                "key": app.key,
                "name": app.name,
                "category": app.category.value,
                "role": app.role.value,
                "confidence": app.confidence.value,
                "process_count": app.process_count,
                "pids": app.pids,
                "private_bytes": app.private_bytes,
                "working_set": app.working_set,
                "product": app.product,
                "company": app.company,
                "evidence": app.evidence,
            }
            for app in snapshot.applications
        ],
        "tree_repairs": snapshot.attribution.tree_repairs,
    }


def _run_snapshot(args: argparse.Namespace) -> int:
    snapshot = take_snapshot(
        fast=args.fast,
        with_services=not args.no_services,
    )
    insights = diagnose(snapshot.system, snapshot.applications)

    if args.html:
        try:
            with open(args.html, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(render_html(snapshot, insights))
        except OSError as exc:
            _error(f"错误: 无法写入 {args.html}: {exc}")
            return 1
        if not args.quiet:
            _error(f"已写入 {args.html}")

    if args.json:
        _write(json.dumps(_snapshot_to_dict(snapshot), ensure_ascii=False, indent=2))
        _write("\n")
        return 0

    text = render_text(
        snapshot,
        insights,
        limit=args.limit,
        show_system=args.all,
        explain_pids=args.explain,
    )
    _write(text if text.endswith("\n") else text + "\n")
    return 0


def _run_watch(args: argparse.Namespace) -> int:
    import signal

    stopping = {"flag": False}

    def _handle(_signum, _frame):
        stopping["flag"] = True

    try:
        signal.signal(signal.SIGINT, _handle)
    except (ValueError, OSError):
        pass  # 非主线程等情况，忽略

    interval = max(0.5, args.interval)
    duration = args.duration
    # 默认走完整采集：应用归因依赖版本资源与命令行，跳过它们会让同一个
    # 软件被拆成几十条碎片，泄漏检测随之失效。静态信息有缓存，
    # 完整模式下除了首次采集，每次采样的开销与 --fast 相差无几。
    fast = args.fast

    if not args.quiet:
        _error(
            f"开始采样：每 {interval:g} 秒一次，"
            + (f"共 {duration:g} 秒" if duration > 0 else "按 Ctrl+C 结束")
            + f"，写入 {args.output}"
        )

    def on_sample(sample, count):
        if args.quiet:
            return
        memory = sample.system
        _error(
            f"  第 {count:>4} 次  "
            f"可用 {human_bytes(memory.get('physical_available', 0)):>10}  "
            f"提交 {human_bytes(memory.get('commit_total', 0)):>10}  "
            f"应用 {len(sample.apps):>3} 个"
        )

    count = watch(
        interval=interval,
        duration=duration,
        output=args.output,
        fast=fast,
        with_services=not args.no_services,
        on_sample=on_sample,
        should_stop=lambda: stopping["flag"],
    )

    if not args.quiet:
        _error(f"采样结束，共 {count} 条，已写入 {args.output}")
        _error(f"下一步：memscope analyze {args.output}")
    return 0


def _verdict_label(verdict: str) -> str:
    return {
        "leak": "疑似泄漏",
        "growing": "持续增长",
        "stable": "稳定",
        "shrinking": "在减小",
        "insufficient": "样本不足",
    }.get(verdict, verdict)


def _run_analyze(args: argparse.Namespace) -> int:
    try:
        samples = read_samples(args.file)
    except FileNotFoundError:
        _error(f"错误: 找不到采样文件 {args.file}")
        return 1
    except OSError as exc:
        _error(f"错误: 无法读取 {args.file}: {exc}")
        return 1

    if not samples:
        _error(f"错误: {args.file} 里没有可用的采样数据")
        return 1

    trends = analyze_samples(samples)
    span = samples[-1].timestamp - samples[0].timestamp

    if args.json:
        _write(json.dumps(
            {
                "file": args.file,
                "samples": len(samples),
                "span_seconds": round(span, 1),
                "trends": [
                    {
                        "key": t.key,
                        "name": t.name,
                        "category": t.category,
                        "samples": t.samples,
                        "span_seconds": round(t.span_seconds, 1),
                        "first_bytes": t.first_bytes,
                        "last_bytes": t.last_bytes,
                        "growth_bytes": t.growth_bytes,
                        "slope_bytes_per_hour": round(t.slope_bytes_per_hour, 1),
                        "r_squared": round(t.r_squared, 4),
                        "verdict": t.verdict,
                    }
                    for t in trends
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        _write("\n")
        return 0

    if args.html:
        # HTML 报告需要一个快照；这里从最后一条采样合成一个最小快照
        from .model import Application, AttributionResult, Snapshot, SystemMemory

        last = samples[-1]
        memory = SystemMemory(
            physical_total=last.system.get("physical_total", 0),
            physical_available=last.system.get("physical_available", 0),
            commit_total=last.system.get("commit_total", 0),
            commit_limit=last.system.get("commit_limit", 0),
            free_and_zero=last.system.get("free_and_zero", 0),
            kernel_paged=last.system.get("kernel_paged", 0),
            kernel_nonpaged=last.system.get("kernel_nonpaged", 0),
        )
        memory.standby_normal = last.system.get("standby_total", 0)

        apps = [
            Application(
                key=a.key, name=a.name,
                category=Category(a.category) if a.category in {c.value for c in Category}
                else Category.USER,
                pids=[], private_bytes=a.private_bytes, working_set=a.working_set,
            )
            for a in sorted(last.apps, key=lambda x: -x.private_bytes)[:40]
        ]
        snapshot = Snapshot(
            timestamp=last.timestamp, system=memory, processes=[],
            attribution=AttributionResult(applications=apps),
        )
        try:
            with open(args.html, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(render_html(snapshot, diagnose(memory, apps), trends))
        except OSError as exc:
            _error(f"错误: 无法写入 {args.html}: {exc}")
            return 1
        if not args.quiet:
            _error(f"已写入 {args.html}")
        return 0

    # ---- 文本输出 ----
    hours = span / 3600 if span else 0
    lines = [
        "─" * 78,
        "── memscope — 增长趋势分析 " + "─" * 48,
        f"采样文件  {args.file}",
        f"采样条数  {len(samples)}      时间跨度  {hours:.2f} 小时",
        "",
    ]

    alerts = [t for t in trends if t.is_alert]
    if not alerts:
        lines.append("  没有发现持续增长的进程。")
        lines.append("")
        lines.append("  这说明在本次采样期间，各应用的常驻内存保持稳定。")
        lines.append("  若怀疑存在泄漏，建议延长采样时间（例如 1 小时以上）重试——")
        lines.append("  缓慢的泄漏需要更长的窗口才能与正常波动区分开。")
    else:
        lines.append(f"  发现 {len(alerts)} 个持续增长的应用：")
        lines.append("")
        lines.append(
            f"  {'应用':<34} {'起始':>9} {'结束':>9} {'增长/小时':>10} "
            f"{'R²':>5}  判定"
        )
        lines.append("  " + "─" * 74)
        for trend in alerts[: args.limit]:
            name = trend.name if len(trend.name) <= 32 else trend.name[:31] + "…"
            lines.append(
                f"  {name:<34} {human_bytes(trend.first_bytes):>9} "
                f"{human_bytes(trend.last_bytes):>9} "
                f"{trend.slope_mb_per_hour:>+9.1f}M {trend.r_squared:>5.2f}  "
                f"{_verdict_label(trend.verdict)}"
            )
        lines.append("")
        lines.append("  判读方法：")
        lines.append("    · 「疑似泄漏」= 每小时增长显著，且 R² 高（增长接近一条直线）")
        lines.append("    · 「持续增长」= 在涨，但还不足以断定泄漏——可能只是缓存增长")
        lines.append("    · R² 越接近 1，说明越不像随机波动")
        lines.append("")
        lines.append("  注意：见到「疑似泄漏」不等于程序有 bug。")
        lines.append("  缓存、连接池、会话表都会正常增长到一个稳定平台。")
        lines.append("  建议延长采样窗口验证，观察它是否会自行收敛。")

    lines.append("")
    lines.append("─" * 78)
    _write("\n".join(lines) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not winapi.is_supported():
        _error(
            f"错误: memscope 的采集功能依赖 Windows API，当前平台是 {sys.platform!r}。\n"
            "归因与分析算法本身是跨平台的（见 tests/），但没有 Windows 就采不到数据。"
        )
        return 2

    try:
        if args.command == "watch":
            return _run_watch(args)
        if args.command == "analyze":
            return _run_analyze(args)
        return _run_snapshot(args)
    except KeyboardInterrupt:
        _error("已中断")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
