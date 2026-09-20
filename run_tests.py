#!/usr/bin/env python
"""零依赖测试入口。

与项目本身一样，测试也只用标准库——clone 下来直接跑::

    python run_tests.py
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
TESTS = ROOT / "tests"

for path in (str(ROOT), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _configure_output_encoding() -> None:
    """把测试输出切到 UTF-8。

    ``TextTestRunner(verbosity=2)`` 会把测试的**文档字符串**打印出来，
    而本项目的测试文档字符串全是中文。Windows 上标准输出被重定向时
    默认编码是 GBK / cp1252，不重配会抛 UnicodeEncodeError。

    CI 的 Windows 任务正是这么挂的——本地之所以没发现，是因为开发时
    一直设着 ``PYTHONIOENCODING=utf-8``。
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

    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(TESTS))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
