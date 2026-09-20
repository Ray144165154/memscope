"""应用归因：把一堆进程还原成"哪个应用占了多少内存"。

这是整个项目最难做对的一层。核心思想是**角色决定策略**（见 :mod:`.classify`），
再加上一套带优先级、且每一步都留下证据的推理流程：

    第 1 步  修复进程树
             · 丢弃"父进程启动时间晚于自己"的边 —— 那是 PID 重用造出的假父子
             · 父进程已消失的，该进程成为根（孤儿）
    第 2 步  判定每个进程的角色
    第 3 步  解析"归因锚点"
             · 共享运行时（WebView2 等）向上穿透，找到真正的宿主
             · 其余进程自己就是锚点
    第 4 步  按签名合并锚点
             · 产品型：厂商 + 产品名（产品名笼统时退回安装目录）
             · 运行时型：可执行文件名 + 命令行里的脚本目录
             · 服务宿主：每个宿主单独成组，名字取自它承载的服务
             · 系统组件：按可执行文件名合并
    第 5 步  计算置信度并记录证据

**本模块是纯函数**：只读 :class:`~memscope.model.ProcessRecord`，只写
:class:`~memscope.model.Application`，不调用任何系统 API。因此可以用人工
构造的真实拓扑做完整测试，也能在 Linux / macOS 上跑。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .classify import (
    Classification,
    classify_process,
    install_root,
    is_generic_product,
    script_hint,
)
from .model import (
    Application,
    AttributionResult,
    Category,
    Confidence,
    ProcessRecord,
    Role,
    ServiceRecord,
)

__all__ = ["attribute", "MAX_ANCESTOR_DEPTH"]

# 向上穿透的最大层数，防止畸形数据造成死循环
MAX_ANCESTOR_DEPTH = 16


# --------------------------------------------------------------------------
# 产品签名合并
# --------------------------------------------------------------------------


def _signature(proc: ProcessRecord) -> tuple[str, str] | None:
    """取进程的 ``(厂商, 产品名)`` 签名；信息不足时返回 None。"""
    company = (proc.company or "").strip()
    product = (proc.product or "").strip()
    if not company or not product or is_generic_product(product):
        return None
    return (company, product)


def _related_product(a: str, b: str) -> bool:
    """判断两个产品名是否属于同一个产品族。

    真实问题：**同一个软件的不同组件经常写着不一样的产品名**。
    例如 Steam 的主程序产品名是 ``Steam``，而它的界面进程写的是
    ``Steam Client WebHelper``——只按产品名精确匹配就会把同一个软件
    拆成两条记录，数据直接失真。

    判据：较短者是较长者的**前缀，且边界落在非字母数字字符上**。

        Steam            / Steam Client WebHelper   → 相关 ✓
        EA               / EA Desktop               → 相关 ✓
        EA               / Eagle                    → 不相关 ✓（边界是字母）
    """
    a_low, b_low = a.strip().lower(), b.strip().lower()
    if a_low == b_low:
        return True
    shorter, longer = (a_low, b_low) if len(a_low) <= len(b_low) else (b_low, a_low)
    if len(shorter) < 2 or not longer.startswith(shorter):
        return False
    rest = longer[len(shorter) :]
    return bool(rest) and not rest[0].isalnum()


def _merge_product_signatures(
    processes: Sequence[ProcessRecord],
) -> dict[tuple[str, str], tuple[str, str]]:
    """把同一产品族的签名合并，返回 ``{原签名: 代表签名}``。

    代表签名里的产品名取该族中**最短**的那个——它通常就是软件的正式名称
    （"Steam" 而不是 "Steam Client WebHelper"，"EA" 而不是 "EA Desktop"）。
    """
    signatures = {sig for sig in (_signature(p) for p in processes) if sig}
    if not signatures:
        return {}

    by_company: dict[str, list[tuple[str, str]]] = {}
    for sig in signatures:
        by_company.setdefault(sig[0].lower(), []).append(sig)

    parent = {sig: sig for sig in signatures}

    def find(item: tuple[str, str]) -> tuple[str, str]:
        root = item
        while parent[root] != root:
            root = parent[root]
        # 路径压缩
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    # 只允许在**同一厂商内部**合并，跨厂商绝不合并
    for company_signatures in by_company.values():
        for i, left in enumerate(company_signatures):
            for right in company_signatures[i + 1 :]:
                if _related_product(left[1], right[1]):
                    union(left, right)

    families: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for sig in signatures:
        families.setdefault(find(sig), []).append(sig)

    merged: dict[tuple[str, str], tuple[str, str]] = {}
    for root, members in families.items():
        shortest = min((m[1] for m in members), key=len)
        representative = (root[0], shortest)
        for member in members:
            merged[member] = representative
    return merged


def _repair_tree(
    processes: Sequence[ProcessRecord],
    by_pid: dict[int, ProcessRecord],
) -> tuple[dict[int, int | None], list[str]]:
    """修复父子关系，返回 ``({pid: 父pid 或 None}, [修复说明])``。

    真实世界的进程树有两类失真的边，都必须处理，否则归因会错：

    1. **PID 重用造成的假父子**：父进程早已退出，系统把它的 PID 分配给了
       另一个新进程。此时 ``th32ParentProcessID`` 指向的是一个毫不相干的东西。
       判别方法：**子进程不可能比父进程先出生**。拿两个启动时间一比就知道。
    2. **孤儿**：父进程已经不存在了。

    这两类进程都不应该被并入某个"父应用"，而应当各自立根。
    """
    parent_of: dict[int, int | None] = {}
    notes: list[str] = []

    for proc in processes:
        if proc.ppid == 0 or proc.ppid == proc.pid:
            parent_of[proc.pid] = None
            continue

        parent = by_pid.get(proc.ppid)
        if parent is None:
            parent_of[proc.pid] = None  # 孤儿
            continue

        # 启动时间可证伪时，优先相信时间
        if (
            proc.start_time is not None
            and parent.start_time is not None
            and parent.start_time > proc.start_time + 1.0
        ):
            parent_of[proc.pid] = None
            notes.append(
                f"PID {proc.pid} ({proc.name}) 声称的父进程 {proc.ppid} "
                f"反而晚启动 {parent.start_time - proc.start_time:.0f} 秒 → "
                f"判定为 PID 重用，已断开这条假父子关系"
            )
            continue

        parent_of[proc.pid] = proc.ppid

    return parent_of, notes


def _resolve_anchor(
    pid: int,
    by_pid: dict[int, ProcessRecord],
    parent_of: dict[int, int | None],
    classes: dict[int, Classification],
    evidence: dict[int, list[str]],
) -> int | None:
    """解析归因锚点：这个进程的开销应当算到哪个进程头上。

    对绝大多数进程，答案就是它自己。**只有共享运行时需要向上穿透**：

        renderer(23000) → browser(22124, --embedded-browser-webview) → 宿主(18600)

    若不穿透，21 个 WebView2 进程会被错误地归成一个叫"Microsoft Edge"的东西，
    而它们其实是好几个不同应用的界面。

    返回 ``None`` 表示穿透失败（父链已断或撞上启动器），调用方应改用兜底线索。
    """
    trail: list[str] = []
    current = pid

    for _ in range(MAX_ANCESTOR_DEPTH):
        classification = classes[current]

        if not classification.transparent:
            if trail:
                host = by_pid[current]
                evidence.setdefault(pid, []).append(
                    "共享运行时向上穿透："
                    + " → ".join(trail)
                    + f" → {host.name}({current})，归因到该宿主"
                )
            return current

        trail.append(f"{by_pid[current].name}({current})")
        parent_pid = parent_of.get(current)

        if parent_pid is None:
            evidence.setdefault(pid, []).append(
                "共享运行时的父进程链已断开（宿主可能已退出），无法确定宿主"
            )
            return None

        parent_class = classes[parent_pid]
        if parent_class.role is Role.LAUNCHER:
            evidence.setdefault(pid, []).append(
                f"共享运行时的父进程是启动器 {by_pid[parent_pid].name}，不作为宿主"
            )
            return None

        current = parent_pid

    evidence.setdefault(pid, []).append(
        f"向上穿透超过 {MAX_ANCESTOR_DEPTH} 层仍未找到宿主，放弃"
    )
    return None


def _runtime_hint(proc: ProcessRecord) -> str | None:
    """通用运行时的"在跑什么"线索。"""
    return script_hint(proc.command_line)


def _short(path: str, limit: int = 34) -> str:
    """把长路径缩成末尾一段，便于在报告里显示。"""
    tail = path.replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]
    return tail if len(tail) <= limit else tail[: limit - 1] + "…"


def _service_names(services: Sequence[ServiceRecord] | None) -> list[str]:
    if not services:
        return []
    names = [(s.display_name or s.name) for s in services if (s.display_name or s.name)]
    return names


def _service_host_label(
    proc: ProcessRecord,
    services: Sequence[ServiceRecord] | None,
    evidence: dict[int, list[str]],
) -> str:
    """服务宿主的展示名（方案 B：解析出具体承载了哪些服务）。"""
    names = _service_names(services)
    if not names:
        evidence.setdefault(proc.pid, []).append(
            f"未能解析出 {proc.name}({proc.pid}) 承载的服务名，退化为按进程展示"
        )
        return f"{proc.name}（服务名未解析）"

    evidence.setdefault(proc.pid, []).append(
        f"服务宿主 {proc.name}({proc.pid}) 承载 {len(names)} 个服务："
        + "、".join(names[:4])
        + ("…" if len(names) > 4 else "")
    )
    if len(names) == 1:
        return names[0]
    return f"{names[0]} 等 {len(names)} 个服务"


def _describe(
    proc: ProcessRecord,
    classification: Classification,
    services: Sequence[ServiceRecord] | None,
    evidence: dict[int, list[str]],
    sig_map: dict[tuple[str, str], tuple[str, str]],
) -> tuple[str, str, Category, Role, Confidence]:
    """给锚点进程产出 ``(分组键, 展示名, 类别, 角色, 置信度)``。"""
    role = classification.role
    name = proc.exe_name
    conf = Confidence.MEDIUM

    # ---- 服务宿主：一个进程承载多个服务（方案 B）----
    if name in ("svchost.exe",):
        label = _service_host_label(proc, services, evidence)
        key = f"service-host:{proc.pid}"
        return key, label, Category.SYSTEM, role, Confidence.HIGH

    # ---- 通用运行时：按"跑的是什么"分组 ----
    if role is Role.RUNTIME:
        hint = _runtime_hint(proc)
        if hint:
            key = f"runtime:{name}:{hint.lower()}"
            label = f"{proc.name} · {_short(hint)}"
            conf = Confidence.MEDIUM
            evidence.setdefault(proc.pid, []).append(f"按命令行线索归组：{hint}")
        else:
            key = f"runtime:{name}:pid{proc.pid}"
            label = f"{proc.name}（无脚本线索）"
            conf = Confidence.LOW
        return key, label, Category.RUNTIME, role, conf

    # ---- Windows 自带组件：按可执行文件名合并 ----
    if role is Role.SYSTEM:
        label = proc.description or proc.name or name
        key = f"system:{name}"
        conf = Confidence.HIGH if proc.exe_path else Confidence.MEDIUM
        return key, label, Category.SYSTEM, role, conf

    # ---- 产品独占型：厂商 + 产品名优先 ----
    if role is Role.PRODUCT:
        signature = _signature(proc)

        if signature is not None:
            representative = sig_map.get(signature, signature)
            company, product = representative
            key = f"product:{company.lower()}|{product.lower()}"
            label = product
            conf = Confidence.HIGH
            note = f"按产品签名合并：{company} / {product}"
            if representative != signature:
                note += (
                    f"（本进程写的产品名是 {signature[1]!r}，"
                    f"与同厂商的 {product!r} 判定为同一产品族，已合并）"
                )
            evidence.setdefault(proc.pid, []).append(note)
            return key, label, Category.USER, role, conf

        # 产品名缺失或笼统 → 退回"厂商 + 安装目录"
        company = (proc.company or "").strip()
        root = install_root(proc.exe_path)
        if company and root:
            key = f"vendor:{company.lower()}|{root.lower()}"
            label = proc.description or proc.name or name
            conf = Confidence.MEDIUM
            evidence.setdefault(proc.pid, []).append(
                f"产品名缺失或缺省，按厂商 {company} + 安装目录 {root} 合并"
            )
        elif root:
            key = f"path:{root.lower()}"
            label = proc.description or proc.name or name
            conf = Confidence.LOW
            evidence.setdefault(proc.pid, []).append(
                f"缺少厂商信息，仅按安装目录 {root} 推测"
            )
        else:
            key = f"proc:{proc.pid}"
            label = proc.description or proc.name or name
            conf = Confidence.LOW
        return key, label, Category.USER, role, conf

    # ---- 共享运行时但穿透失败：尝试用命令行线索兜底 ----
    if role is Role.SHARED:
        hint = _webview_host_hint(proc)
        if hint:
            key = f"shared-host:{hint.lower()}"
            label = f"（宿主：{_short(hint)}）"
            evidence.setdefault(proc.pid, []).append(
                f"父链已断，改用命令行 user-data-dir 线索推断宿主：{hint}"
            )
            return key, label, Category.SHARED, role, Confidence.LOW
        key = f"shared:{name}"
        label = f"{proc.name}（未归因的共享运行时）"
        evidence.setdefault(proc.pid, []).append("父链已断且无兜底线索，单独成组")
        return key, label, Category.SHARED, role, Confidence.LOW

    # ---- 启动器（explorer.exe 等）：本身也是真实的内存消费者 ----
    if role is Role.LAUNCHER:
        return f"launcher:{name}", proc.description or proc.name or name, Category.SYSTEM, role, Confidence.HIGH

    # ---- 信息不足 ----
    if not proc.accessible:
        # 系统保护进程打不开，我们**确实**区分不了它们。
        # 与其产出上百个低置信度的碎片条目制造噪音，不如诚实地合并成一个桶，
        # 并在证据里写明原因——用户看到"171 个进程、X MB"比看到 78 条
        # 名字都是猜的记录有用得多。
        evidence.setdefault(proc.pid, []).append(
            "无法打开进程读取任何详情（系统保护进程），"
            "无法与同类进程区分，统一计入本桶"
        )
        return (
            "system:inaccessible",
            "系统保护进程（无法读取详情）",
            Category.SYSTEM,
            role,
            Confidence.LOW,
        )

    key = f"proc:{proc.pid}"
    label = proc.description or proc.name or f"PID {proc.pid}"
    return key, label, Category.USER, role, Confidence.LOW


def _webview_host_hint(proc: ProcessRecord) -> str | None:
    """从 WebView2 进程的命令行里挖出宿主线索。

    ``--user-data-dir`` 通常指向宿主的本地数据目录，例如::

        ...\\Packages\\MicrosoftWindows.Client.CBS_cw5n1h2txyewy\\LocalState\\EBWebView

    当宿主进程已经退出、父链断裂时，这是唯一还能用的证据。
    """
    cmd = proc.command_line or ""
    low = cmd.lower()
    marker = "--user-data-dir="
    idx = low.find(marker)
    if idx == -1:
        return None
    value = cmd[idx + len(marker) :].strip().strip('"')
    value = value.split(" --")[0].strip().strip('"')
    if not value:
        return None
    # 去掉 EBWebView 这一层，拿到宿主的目录
    parts = value.replace("/", "\\").rstrip("\\").split("\\")
    if parts and parts[-1].lower() in ("ebwebview", "webview2"):
        parts = parts[:-1]
    if not parts:
        return None
    return "\\".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


def _confidence_rank(value: Confidence) -> int:
    return {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}[value]


def attribute(
    processes: Sequence[ProcessRecord],
    services: Mapping[int, Sequence[ServiceRecord]] | None = None,
    *,
    windows_root: str | None = None,
) -> AttributionResult:
    """把进程列表归因成应用列表。

    :param processes: 采集到的进程记录
    :param services: 可选，``{宿主进程 pid: [该宿主承载的服务]}``，用于方案 B
    :param windows_root: 可选，Windows 安装目录（默认取环境变量）
    """
    result = AttributionResult()
    if not processes:
        return result

    services = services or {}
    by_pid = {p.pid: p for p in processes}

    # 第 1 步：修复进程树
    parent_of, repairs = _repair_tree(processes, by_pid)
    result.tree_repairs = repairs

    # 第 2 步：判定角色
    classes = {p.pid: classify_process(p, windows_root) for p in processes}
    for proc in processes:
        result.explanations.setdefault(proc.pid, []).append(classes[proc.pid].reason)

    # 第 3 步：解析锚点
    anchors: dict[int, int | None] = {}
    for proc in processes:
        anchors[proc.pid] = _resolve_anchor(
            proc.pid, by_pid, parent_of, classes, result.explanations
        )

    # 第 4 步：先合并"同一产品族"的签名，再按签名分组
    #
    # 必须先把签名归一化：同一个软件的不同组件经常写着不一样的产品名
    # （Steam 主程序写 "Steam"，界面进程写 "Steam Client WebHelper"），
    # 只做精确匹配会把一个软件拆成好几条记录。
    sig_map = _merge_product_signatures(processes)

    groups: dict[str, Application] = {}
    for proc in processes:
        anchor_pid = anchors[proc.pid]

        if anchor_pid is None:
            # 穿透失败 → 该进程自成一组（用兜底线索决定名字）
            key, label, category, role, conf = _describe(
                proc, classes[proc.pid], services.get(proc.pid),
                result.explanations, sig_map,
            )
            app = groups.get(key)
            if app is None:
                app = Application(
                    key=key, name=label, category=category, role=role, confidence=conf,
                    product=proc.product, company=proc.company, anchor_pid=proc.pid,
                )
                groups[key] = app
            app.pids.append(proc.pid)
            app.working_set += proc.working_set
            app.private_bytes += proc.private_bytes
            app.page_faults += proc.page_faults
            continue

        host = by_pid[anchor_pid]
        host_services = services.get(anchor_pid)
        key, label, category, role, conf = _describe(
            host, classes[anchor_pid], host_services, result.explanations, sig_map
        )

        app = groups.get(key)
        if app is None:
            app = Application(
                key=key, name=label, category=category, role=role, confidence=conf,
                product=host.product, company=host.company, anchor_pid=anchor_pid,
            )
            groups[key] = app

        app.pids.append(proc.pid)
        app.working_set += proc.working_set
        app.private_bytes += proc.private_bytes
        app.page_faults += proc.page_faults

        if proc.pid != anchor_pid:
            result.explanations.setdefault(proc.pid, []).append(
                f"开销计入 {host.name}({anchor_pid})"
            )

    # 置信度取组内最低（一个只能靠推测的成员会拉低整组的可信度）
    for app in groups.values():
        app.pids.sort()
        app.evidence = sorted(
            {line for pid in app.pids for line in result.explanations.get(pid, [])}
        )

    result.applications = sorted(
        groups.values(),
        key=lambda a: (-a.total_bytes, _confidence_rank(a.confidence), a.name.lower()),
    )
    return result
