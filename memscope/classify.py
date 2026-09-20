"""进程角色判定 —— 归因的枢纽。

为什么需要这一层？因为**不存在一套万能的分组规则**：

  * 按进程名分组 → 错。Steam 由 ``steam.exe`` + 7 个 ``steamwebhelper.exe``
    组成；而 ``node.exe`` 同时服务于好几种互不相关的程序。
  * 按可执行文件路径分组 → 错。21 个 ``msedgewebview2.exe`` 路径
    **完全相同**，但它们可能是好几个不同应用的界面。
  * 按父进程树分组 → 不全对。``services.exe`` 有 138 个子进程，
    全并起来就是一个毫无意义的巨型条目。

所以必须先判定每个进程**扮演什么角色**，再按角色分派不同的策略。

本模块是纯规则，不调用任何系统 API，因此可跨平台测试。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .model import Category, ProcessRecord, Role

__all__ = [
    "Classification",
    "classify_process",
    "GENERIC_RUNTIMES",
    "SHARED_HOSTS",
    "LAUNCHER_NAMES",
    "SERVICE_HOSTS",
    "windows_root",
    "script_hint",
    "install_root",
    "is_generic_product",
]


# --------------------------------------------------------------------------
# 知识表
# --------------------------------------------------------------------------

# 通用运行时：可执行文件被大量互不相关的程序复用。
# **绝不能按名字合并**——两个 node.exe 很可能毫无关系。
GENERIC_RUNTIMES: frozenset[str] = frozenset({
    "node.exe", "npm.exe", "npx.exe", "yarn.exe", "pnpm.exe",
    "python.exe", "pythonw.exe", "python3.exe", "py.exe",
    "java.exe", "javaw.exe", "javaws.exe",
    "dotnet.exe", "msbuild.exe", "vbcscompiler.exe",
    "ruby.exe", "perl.exe", "php.exe", "lua.exe", "rscript.exe",
    "julia.exe", "deno.exe", "bun.exe", "electron.exe",
    "bash.exe", "sh.exe", "zsh.exe", "fish.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe",
    "wsl.exe", "wslhost.exe",
})

# 共享运行时宿主：它们为**别的**应用提供界面或服务，
# 归因时必须向上穿透，把开销算到真正的宿主头上。
SHARED_HOSTS: frozenset[str] = frozenset({
    "msedgewebview2.exe",   # WebView2：被大量应用嵌入做界面
    "conhost.exe",          # 控制台宿主：服务于某个具体程序
    "openconsole.exe",      # Windows Terminal 的控制台宿主
    "runtimebroker.exe",    # UWP 权限代理
    "dllhost.exe",          # COM 代理
    "cefsharp.browsersubprocess.exe",
})

# 启动器 / 系统代理：**不作为归因目标**。
# 它们的子进程应当各自立根——否则会把整个系统合成一个条目。
LAUNCHER_NAMES: frozenset[str] = frozenset({
    "explorer.exe",      # 有 12+ 个子进程（开机自启程序都挂在它下面）
    "services.exe",      # 有 138 个子进程
    "wininit.exe", "winlogon.exe", "userinit.exe",
    "taskhostw.exe", "sihost.exe", "shellexperiencehost.exe",
    "wmiprvse.exe", "unsecapp.exe",
    "csrss.exe", "smss.exe", "lsass.exe", "fontdrvhost.exe",
    "spoolsv.exe", "searchindexer.exe",
})

# 服务宿主：一个进程承载多个 Windows 服务。
# 需要额外查询服务表才能知道"到底是谁在吃内存"。
SERVICE_HOSTS: frozenset[str] = frozenset({"svchost.exe"})

# 表示"这是 Windows 操作系统自身"的产品名。
# 它出现在所有系统二进制的版本资源里，因此**不能**拿它当产品身份来合并，
# 否则整个操作系统会变成一个条目。
_GENERIC_PRODUCT_MARKERS: tuple[str, ...] = (
    "microsoft® windows® operating system",
    "microsoft windows operating system",
    "microsoft® windows®",
)

# 版本资源里没有意义的描述，不能拿来当应用名
_USELESS_DESCRIPTIONS: frozenset[str] = frozenset({"", "-", "n/a", "unknown"})

# 路径中这些目录名属于"实现细节"，向上归纳安装根目录时应剥掉。
#
# 注意：**不要**把 "app" 放进来。它看起来像打包目录（Electron 的
# resources\app\），但同样经常是真正的产品目录名，一旦剥掉就会把安装根
# 退化成 "C:\Program Files"，从而把大量互不相关的软件合并成一组。
# Electron 那种 app-1.2.3 形式由下面的版本号规则单独处理。
_PACKAGING_DIRS: frozenset[str] = frozenset({
    "bin", "bin64", "binaries", "lib", "libs", "lib64", "plugins",
    "resources", "helpers", "modules", "x64", "x86", "amd64", "win64",
    "dist", "src", "cef", "cef.win7x64", "cef.win7", "chromium",
    "framework", "runtime", "service", "services", "current", "latest",
    "binaries.x86", "release", "debug", "windows", "win32",
})


def windows_root() -> str:
    """Windows 安装目录（默认 ``C:\\Windows``）。"""
    return os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"


def is_generic_product(product: str | None) -> bool:
    """判断产品名是否"过于笼统、不足以作为身份"。"""
    if not product:
        return True
    low = product.strip().lower()
    if not low or low in _USELESS_DESCRIPTIONS:
        return True
    return any(marker in low for marker in _GENERIC_PRODUCT_MARKERS)


def _is_under(path: str, root: str) -> bool:
    p = path.replace("/", "\\").rstrip("\\").lower()
    r = root.replace("/", "\\").rstrip("\\").lower()
    return p == r or p.startswith(r + "\\")


# --------------------------------------------------------------------------
# 路径与命令行分析
# --------------------------------------------------------------------------


def install_root(exe_path: str | None) -> str | None:
    """从可执行文件路径归纳出"安装根目录"。

    同一个产品的多个 exe 往往分散在多层子目录里，例如::

        C:\\Program Files (x86)\\Steam\\steam.exe
        C:\\Program Files (x86)\\Steam\\bin\\cef\\cef.win7x64\\steamwebhelper.exe

    两个人应该归到一起。这里从文件名往上走，逐层剥掉打包用的子目录名
    （bin / helpers / cef / 版本号目录…），停在第一个"像产品目录"的位置。

    这是个启发式，不保证对所有软件都对——所以它只作为**辅助**合并依据，
    产品签名（厂商 + 产品名）的优先级更高。
    """
    if not exe_path:
        return None

    parts = exe_path.replace("/", "\\").split("\\")
    if len(parts) < 2:
        return None

    # 先剥掉文件名
    dirs = parts[:-1]

    # 计算"最小可接受深度"：绝不能剥到安装根本身或更上层。
    # 否则 "C:\Program Files\App\app.exe" 会退化成 "C:\Program Files"，
    # 把所有软件合并成一组——这是真实踩过的坑。
    lowered = [d.lower() for d in dirs]
    min_depth = 1
    for marker in ("program files (x86)", "program files", "programdata", "windowsapps"):
        if marker in lowered:
            min_depth = lowered.index(marker) + 2  # 安装根 + 第一层产品目录
            break

    # 逐层剥掉打包目录，但守住最小深度
    while len(dirs) > min_depth:
        leaf = dirs[-1].lower()
        if leaf in _PACKAGING_DIRS:
            dirs.pop()
            continue
        # 纯版本号目录，如 153.0.4234.32 / v1.2.3
        if leaf.lstrip("v").replace(".", "").isdigit():
            dirs.pop()
            continue
        # Electron 的 app-1.2.3
        if leaf.startswith("app-") and leaf[4:].replace(".", "").isdigit():
            dirs.pop()
            continue
        break

    if not dirs:
        return None

    return "\\".join(dirs)


def script_hint(command_line: str | None) -> str | None:
    """从命令行里提取"这个运行时实例在跑什么"。

    对通用运行时（node / python）来说，进程名毫无区分度，
    真正的身份藏在命令行的脚本路径里::

        node.exe D:\\proj\\web\\build.mjs   ->  D:\\proj\\web
        python.exe -m http.server 8000      ->  -m http.server（退回整串摘要）

    返回 None 表示提取不到。
    """
    if not command_line:
        return None

    import shlex

    try:
        tokens = shlex.split(command_line, posix=False)
    except ValueError:
        tokens = command_line.split()

    if not tokens:
        return None

    args = tokens[1:]  # 跳过 argv[0]（可执行文件自身）

    # 优先识别 `-m <模块>`：Python 这种写法最常见，
    # 模块名才是真正的身份，比后面那些选项值可靠得多
    for index, token in enumerate(args[:-1]):
        if token in ("-m", "--module"):
            return f"{token} {args[index + 1]}"

    # 其次找"像路径"的参数
    for token in args:
        t = token.strip('"').strip("'")
        if not t or t.startswith("-"):
            continue
        if ("\\" in t or "/" in t) and len(t) > 3:
            normalized = t.replace("/", "\\").rstrip("\\")
            parent = normalized.rsplit("\\", 1)[0]
            return parent or normalized

    # 都没有：退回"前两个非选项参数"作为摘要
    rest = [t.strip('"') for t in args if not t.startswith("-")][:2]
    return " ".join(rest) if rest else None


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Classification:
    """一个进程的角色判定结果。"""

    role: Role
    category: Category
    reason: str
    # 归因时是否应"穿过"这个进程继续向上找宿主
    transparent: bool = False


def classify_process(rec: ProcessRecord, root: str | None = None) -> Classification:
    """判定一个进程的角色。规则按优先级从上到下，**先匹配者胜**。"""
    root = root or windows_root()
    name = rec.exe_name
    path = rec.exe_path or ""

    # 1) 服务宿主：能承载多个 Windows 服务，需要额外查服务表
    if name in SERVICE_HOSTS:
        return Classification(
            role=Role.SYSTEM,
            category=Category.SYSTEM,
            reason=f"{rec.name} 是 Windows 服务宿主，可能承载多个服务",
        )

    # 2) 共享运行时：为别的应用提供界面/服务，必须向上穿透
    if name in SHARED_HOSTS:
        return Classification(
            role=Role.SHARED,
            category=Category.SHARED,
            reason=f"{rec.name} 是共享运行时，真正的宿主在父进程链上游",
            transparent=True,
        )

    # 3) 通用运行时：名字没有区分度，身份取决于它跑的是什么
    if name in GENERIC_RUNTIMES:
        hint = script_hint(rec.command_line)
        detail = f"，脚本线索 {hint}" if hint else "（命令行无路径线索）"
        return Classification(
            role=Role.RUNTIME,
            category=Category.RUNTIME,
            reason=f"{rec.name} 是通用运行时，不按进程名合并{detail}",
        )

    # 4) 启动器 / 系统代理：不作为归因目标
    if name in LAUNCHER_NAMES:
        return Classification(
            role=Role.LAUNCHER,
            category=Category.SYSTEM,
            reason=f"{rec.name} 是启动器/系统代理，不作为归因目标",
        )

    # 5) 位于 Windows 目录下的系统二进制
    if path and _is_under(path, root):
        return Classification(
            role=Role.SYSTEM,
            category=Category.SYSTEM,
            reason=f"位于 Windows 目录（{root}）下，判定为系统组件",
        )

    # 6) 系统应用目录（UWP / 内置应用）
    if path and ("\\windowsapps\\" in path.lower() or "\\systemapps\\" in path.lower()):
        # 商店应用也可能是第三方，用厂商区分
        vendor = (rec.company or "").lower()
        is_ms = "microsoft" in vendor
        return Classification(
            role=Role.SYSTEM if is_ms else Role.PRODUCT,
            category=Category.SYSTEM if is_ms else Category.USER,
            reason=(
                f"位于应用包目录，厂商={rec.company or '未知'}"
                f" → {'系统应用' if is_ms else '第三方商店应用'}"
            ),
        )

    # 7) 有明确的产品签名 → 产品独占型
    if rec.product and not is_generic_product(rec.product):
        return Classification(
            role=Role.PRODUCT,
            category=Category.USER,
            reason=f"产品签名明确（{rec.company or '未知厂商'} / {rec.product}）",
        )

    # 8) 有厂商但产品名笼统 → 仍按厂商 + 安装目录合并
    if rec.company and not rec.product:
        return Classification(
            role=Role.PRODUCT,
            category=Category.USER,
            reason=f"有厂商信息（{rec.company}），按厂商 + 安装目录合并",
        )

    # 9) 什么都拿不到
    if not rec.accessible:
        return Classification(
            role=Role.UNKNOWN,
            category=Category.SYSTEM,
            reason="无法打开进程，取不到任何信息（多为系统保护进程）",
        )

    return Classification(
        role=Role.UNKNOWN,
        category=Category.USER,
        reason="缺少厂商与产品信息，只能按可执行文件路径推测",
    )
