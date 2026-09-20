#!/usr/bin/env python
"""验证"运行时零依赖"这个承诺。

口号式的"零依赖"没有意义，必须能自动检查。本脚本静态扫描 ``memscope``
下的所有 import，确认它们**只来自标准库**。

注意：``ctypes`` 与 ``ctypes.wintypes`` 都是标准库的一部分，
因此本项目调用 Windows API 并不引入任何第三方依赖——这是它能做到
"clone 下来直接跑"的原因。

本地运行::

    python tools/check_zero_deps.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "memscope"

# 允许出现的非标准库顶层模块名
ALLOWED = {
    "__future__",   # 编译期指令，不产生运行时依赖
    "memscope",     # 包内部互相导入
}


def collect_imports(path: pathlib.Path) -> list[tuple[str, int]]:
    """返回文件里所有顶层模块名 ``(模块名, 行号)``。"""
    found: list[tuple[str, int]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            # level > 0 是包内相对导入（from . import x）
            if node.level == 0 and node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def _configure_output_encoding() -> None:
    """把标准输出与标准错误切到 UTF-8。

    Windows 上标准输出被重定向时的默认编码是 GBK / cp1252，
    而本脚本会输出中文与 ✔，不重配会直接抛 UnicodeEncodeError。
    上一版正是在 Windows CI 上这么挂的。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main() -> int:
    _configure_output_encoding()

    if not SRC.is_dir():
        print(f"找不到源码目录: {SRC}", file=sys.stderr)
        return 1

    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    if not stdlib:
        print("警告: 当前 Python 没有 sys.stdlib_module_names，只做白名单检查",
              file=sys.stderr)

    violations: list[str] = []
    checked = 0

    for path in sorted(SRC.rglob("*.py")):
        checked += 1
        for module, lineno in collect_imports(path):
            if module in ALLOWED:
                continue
            if stdlib and module in stdlib:
                continue
            violations.append(f"{path.relative_to(ROOT)}:{lineno}: 第三方导入 {module!r}")

    if violations:
        print("发现运行时第三方依赖，这违反了项目的零依赖承诺:", file=sys.stderr)
        for line in violations:
            print(f"  {line}", file=sys.stderr)
        print(
            "\n如果你确实需要这个依赖，请说明理由，或把它改成可选导入"
            "（在函数内部 import 并处理 ImportError）。",
            file=sys.stderr,
        )
        return 1

    print(f"已检查 {checked} 个源文件，运行时导入全部来自标准库 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
