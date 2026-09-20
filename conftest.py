"""让 pytest 也能直接运行（把仓库根目录与 tests 加进导入路径）。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

for path in (ROOT, ROOT / "tests"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
