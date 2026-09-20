#!/usr/bin/env python
"""CI 冒烟校验：检查 CLI 产出的文件结构是否正确。

**为什么不直接在 CI 里用 shell 命令做这些检查？**

因为那会引入 shell 自身的编码与引号行为，而这些行为在不同 shell 之间
并不一致：

  * PowerShell 5.1 与 PowerShell 7 的 ``Out-File -Encoding`` 取值不同
    （``utf8NoBOM`` 只有 7 才有），写出的 BOM 也不一致
  * PowerShell 5.1 默认按 OEM 代码页解码原生命令的输出，中文会乱码
  * Git Bash（MSYS2）会做路径转换，且重定向/管道行为自成一格
  * ``$ErrorActionPreference='Stop'`` 下原生命令写 stderr 会被当成终止错误

把校验放进 Python，编码、引号、路径、退出码全都在一个可控环境里。
PowerShell 那层只负责两件事：跑命令、看退出码。

用法::

    python tools/ci_smoke.py --snapshot snapshot.json --html report.html --trend trend.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__all__ = ["main"]

MIN_HTML_BYTES = 2000  # 一份正常报告远大于此


def _configure_output_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def check_snapshot(path: Path) -> list[str]:
    """校验 ``memscope --json`` 的输出结构。"""
    problems: list[str] = []

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("snapshot.json 带 UTF-8 BOM（下游 JSON 解析器会报错）")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        problems.append("snapshot.json 是 UTF-16（重定向方式不对）")

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(f"snapshot.json 无法解析: {type(exc).__name__}: {exc}")
        return problems

    for key in ("version", "timestamp", "system", "applications"):
        if key not in data:
            problems.append(f"snapshot.json 缺少顶层字段 {key!r}")
    if problems:
        return problems

    system = data["system"]
    if not isinstance(system, dict):
        problems.append("system 不是对象")
        return problems

    if system.get("physical_total", 0) <= 0:
        problems.append(f"physical_total 异常: {system.get('physical_total')}")
    if system.get("commit_limit", 0) < system.get("physical_total", 0):
        problems.append("commit_limit 小于 physical_total（不可能）")

    apps = data["applications"]
    if not apps:
        problems.append("没有归因出任何应用")
        return problems

    total = sum(a.get("private_bytes", 0) for a in apps)
    if total <= 0:
        problems.append("所有应用的 private_bytes 之和为 0")
    if data.get("processes", 0) <= 0:
        problems.append("processes 为 0")

    # 守恒：所有进程的内存必须恰好分配到一个应用里
    pids: list[int] = []
    for app in apps:
        pids.extend(app.get("pids", []))
    if len(pids) != len(set(pids)):
        problems.append("有进程被归因到多个应用")
    if data.get("processes") != len(pids):
        problems.append(
            f"进程数不一致：采集 {data.get('processes')} 个，归因 {len(pids)} 个"
        )

    for app in apps:
        if app.get("category") not in ("user", "system", "runtime", "shared"):
            problems.append(f"应用 {app.get('name')!r} 的 category 非法: {app.get('category')}")
            break
        if app.get("confidence") not in ("high", "medium", "low"):
            problems.append(f"应用 {app.get('name')!r} 的 confidence 非法: {app.get('confidence')}")
            break

    print(
        f"  snapshot.json 通过：{len(apps)} 个应用 / {len(pids)} 个进程，"
        f"物理内存 {system['physical_total'] / 1024**3:.2f} GB"
    )
    return problems


def check_html(path: Path) -> list[str]:
    """校验 HTML 报告是自包含且完整的。"""
    problems: list[str] = []
    raw = path.read_bytes()
    if len(raw) < MIN_HTML_BYTES:
        problems.append(f"report.html 只有 {len(raw)} 字节，过小（可能没写成功）")
        return problems

    text = raw.decode("utf-8", "replace")
    for needle in ("<!DOCTYPE html>", "</html>", "<svg", 'charset="utf-8"'):
        if needle not in text:
            problems.append(f"report.html 缺少 {needle!r}")

    # 自包含：离线必须能打开
    lowered = text.lower()
    if "<script" in lowered or "<link" in lowered:
        problems.append("report.html 引用了外部资源（脚本或样式表），不再自包含")
    if "http://" in text or "https://" in text:
        problems.append("report.html 里出现了外部 URL")

    if not problems:
        print(f"  report.html 通过：{len(raw)} 字节，自包含且含内联 SVG")
    return problems


def check_trend(path: Path) -> list[str]:
    """校验 ``memscope analyze --json`` 的输出结构。"""
    problems: list[str] = []
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"trend.json 无法解析: {type(exc).__name__}: {exc}"]

    if data.get("samples", 0) <= 0:
        problems.append("采样条数为 0")
        return problems

    if not isinstance(data.get("trends"), list):
        problems.append("trends 不是数组")
        return problems

    for trend in data["trends"]:
        if trend.get("verdict") not in (
            "leak", "growing", "stable", "shrinking", "insufficient",
        ):
            problems.append(f"未知的 verdict: {trend.get('verdict')}")
            break
        if not 0.0 <= trend.get("r_squared", 0.0) <= 1.0:
            problems.append(f"r_squared 超出 [0,1]: {trend.get('r_squared')}")
            break

    if not problems:
        print(
            f"  trend.json 通过：{data['samples']} 条采样，"
            f"跨度 {data.get('span_seconds')} 秒，{len(data['trends'])} 条趋势"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    _configure_output_encoding()

    parser = argparse.ArgumentParser(
        prog="ci_smoke",
        description="校验 memscope CLI 的产出（编码与引号全部可控）",
    )
    parser.add_argument("--snapshot", help="memscope --json 的输出文件")
    parser.add_argument("--html", help="memscope --html 生成的报告")
    parser.add_argument("--trend", help="memscope analyze --json 的输出文件")
    args = parser.parse_args(argv)

    if not (args.snapshot or args.html or args.trend):
        parser.error("至少要指定 --snapshot / --html / --trend 之一")

    print("CI 冒烟校验：")
    problems: list[str] = []

    for flag, checker in (
        (args.snapshot, check_snapshot),
        (args.html, check_html),
        (args.trend, check_trend),
    ):
        if not flag:
            continue
        path = Path(flag)
        if not path.is_file():
            problems.append(f"找不到文件 {path}")
            continue
        problems.extend(checker(path))

    if problems:
        print()
        print("校验失败：", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return 1

    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
