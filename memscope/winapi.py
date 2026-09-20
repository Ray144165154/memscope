"""Windows API 绑定（ctypes）—— 本项目唯一直接接触操作系统的一层。

设计原则：**这一层只负责"取数"，不做任何判断**。所有推理（归因、
分类、分析）都在别的模块里，那些模块不导入本模块，因此可以跨平台测试。

本模块用到的 API 及选择理由：

  ``psapi.GetPerformanceInfo``
      系统内存总览。**注意单位是"页"而非字节**——这个坑很隐蔽，
      所有字段都要乘以 ``PageSize`` 才是字节。
  ``kernel32.CreateToolhelp32Snapshot``
      进程列表，且**自带父进程 PID**。不用 ``NtQuerySystemInformation``
      是因为 SYSTEM_PROCESS_INFORMATION 结构体复杂且随版本变化，
      而 Toolhelp32 结构稳定、字段够用。
  ``psapi.GetProcessMemoryInfo``
      单进程内存明细（工作集、私有提交、缺页次数）。
  ``version.GetFileVersionInfo*``
      可执行文件的公司名/产品名/描述——把 ``steamwebhelper.exe``
      变成 "Steam · Valve Corporation" 的关键，也是产品归并的依据。
  ``ntdll.NtQueryInformationProcess(60)``
      命令行。用于判断 WebView2 之类的共享运行时到底被谁托管。
  ``advapi32.EnumServicesStatusEx``
      服务表（服务名 ↔ 宿主进程 PID）。这是"方案 B"的基础——
      没有它，89 个 svchost.exe 合计 1.5 GB 就是一个无法行动的黑盒。
  ``pdh`` 性能计数器
      分页列表细分（待机缓存、修改页、空闲页）。psapi 拿不到这些。

所有函数在非 Windows 平台调用时会抛出 :class:`UnsupportedPlatform`，
而不是在导入期就失败——这样采集层以外的东西仍可跨平台导入。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from dataclasses import dataclass

__all__ = [
    "UnsupportedPlatform",
    "is_supported",
    "PerformanceInfo",
    "ProcessEntry",
    "ProcessMemoryCounters",
    "ServiceEntry",
    "system_performance_info",
    "enumerate_process_entries",
    "process_memory",
    "process_exe_path",
    "process_command_line",
    "process_start_time",
    "process_session_id",
    "file_version_info",
    "enumerate_services",
    "memory_counters",
]

IS_WINDOWS = sys.platform == "win32"


class UnsupportedPlatform(RuntimeError):
    """在非 Windows 平台上调用了 Windows 专用 API。"""


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ProcessCommandLineInformation = 60

SC_MANAGER_ENUMERATE_SERVICE = 0x0004
SC_ENUM_PROCESS_INFO = 0
SERVICE_WIN32 = 0x00000030
SERVICE_ACTIVE = 0x00000001
SERVICE_INACTIVE = 0x00000002
ERROR_MORE_DATA = 234

# Windows FILETIME 纪元（1601-01-01）到 Unix 纪元（1970-01-01）的秒数
_FILETIME_EPOCH_DELTA = 11644473600


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------


class PERFORMANCE_INFORMATION(ctypes.Structure):
    """``GetPerformanceInfo`` 的返回结构。

    ⚠️ **除 ``PageSize`` 外，所有 SIZE_T 字段的单位都是"页数"，不是字节。**
    这是本模块最容易踩的坑：直接当成字节会得到看起来"很小"的数字，
    令人误以为系统内存几乎没被使用。
    """

    _fields_ = [
        ("cb", wt.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wt.DWORD),
        ("ProcessCount", wt.DWORD),
        ("ThreadCount", wt.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * MAX_PATH),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wt.USHORT),
        ("MaximumLength", wt.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wt.DWORD),
        ("dwHighDateTime", wt.DWORD),
    ]


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wt.DWORD),
        ("dwCurrentState", wt.DWORD),
        ("dwControlsAccepted", wt.DWORD),
        ("dwWin32ExitCode", wt.DWORD),
        ("dwServiceSpecificExitCode", wt.DWORD),
        ("dwCheckPoint", wt.DWORD),
        ("dwWaitHint", wt.DWORD),
        ("dwProcessId", wt.DWORD),
        ("dwServiceFlags", wt.DWORD),
    ]


class ENUM_SERVICE_STATUS_PROCESSW(ctypes.Structure):
    _fields_ = [
        ("lpServiceName", wt.LPWSTR),
        ("lpDisplayName", wt.LPWSTR),
        ("ServiceStatusProcess", SERVICE_STATUS_PROCESS),
    ]


# --------------------------------------------------------------------------
# DLL 延迟加载
# --------------------------------------------------------------------------

_dlls: dict[str, ctypes.WinDLL] = {}


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise UnsupportedPlatform(
            f"本功能依赖 Windows API，当前平台是 {sys.platform!r}。"
            "memscope 的采集层只能在 Windows 上运行；"
            "归因与分析部分（memscope.attribution / memscope.analysis）可跨平台使用。"
        )


def _dll(name: str) -> ctypes.WinDLL:
    """按需加载并缓存 DLL。延迟加载让本模块在非 Windows 上也能被导入。"""
    _require_windows()
    if name not in _dlls:
        _dlls[name] = ctypes.WinDLL(name, use_last_error=True)
    return _dlls[name]


def is_supported() -> bool:
    """当前平台是否支持采集。"""
    return IS_WINDOWS


# --------------------------------------------------------------------------
# 结果载体
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PerformanceInfo:
    """``GetPerformanceInfo`` 的结果，已换算为**字节**。"""

    page_size: int
    commit_total: int
    commit_limit: int
    commit_peak: int
    physical_total: int
    physical_available: int
    system_cache: int
    kernel_total: int
    kernel_paged: int
    kernel_nonpaged: int
    handle_count: int
    process_count: int
    thread_count: int


@dataclass(slots=True)
class ProcessEntry:
    """进程列表里的一条（只有 Toolhelp32 能直接给的信息）。"""

    pid: int
    ppid: int
    name: str
    thread_count: int = 0


@dataclass(slots=True)
class ProcessMemoryCounters:
    working_set: int = 0
    peak_working_set: int = 0
    private_bytes: int = 0
    peak_private_bytes: int = 0
    page_faults: int = 0


@dataclass(slots=True)
class ServiceEntry:
    name: str
    display_name: str
    pid: int
    state: str = ""


# --------------------------------------------------------------------------
# 系统内存
# --------------------------------------------------------------------------


def system_performance_info() -> PerformanceInfo:
    """取系统级内存总览。

    **关键点：``GetPerformanceInfo`` 的所有 SIZE_T 字段单位是页数**，
    必须乘以 ``PageSize`` 才是字节。踩过这个坑的表现是"内存看起来
    只用了几 MB"。
    """
    psapi = _dll("psapi")
    psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), wt.DWORD]
    psapi.GetPerformanceInfo.restype = wt.BOOL

    info = PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
    if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())

    page = info.PageSize or 4096

    def to_bytes(pages: int) -> int:
        return int(pages) * page

    return PerformanceInfo(
        page_size=page,
        commit_total=to_bytes(info.CommitTotal),
        commit_limit=to_bytes(info.CommitLimit),
        commit_peak=to_bytes(info.CommitPeak),
        physical_total=to_bytes(info.PhysicalTotal),
        physical_available=to_bytes(info.PhysicalAvailable),
        system_cache=to_bytes(info.SystemCache),
        kernel_total=to_bytes(info.KernelTotal),
        kernel_paged=to_bytes(info.KernelPaged),
        kernel_nonpaged=to_bytes(info.KernelNonpaged),
        handle_count=int(info.HandleCount),
        process_count=int(info.ProcessCount),
        thread_count=int(info.ThreadCount),
    )


# --------------------------------------------------------------------------
# 进程
# --------------------------------------------------------------------------


def enumerate_process_entries() -> list[ProcessEntry]:
    """枚举所有进程，含**父进程 PID**。"""
    k32 = _dll("kernel32")
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wt.BOOL
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    entries: list[ProcessEntry] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return entries
        while True:
            entries.append(
                ProcessEntry(
                    pid=int(entry.th32ProcessID),
                    ppid=int(entry.th32ParentProcessID),
                    name=entry.szExeFile,
                    thread_count=int(entry.cntThreads),
                )
            )
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)

    return entries


def _open_process(pid: int):
    k32 = _dll("kernel32")
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return handle or None


def process_memory(pid: int) -> ProcessMemoryCounters | None:
    """取单个进程的内存明细；打不开则返回 ``None``（系统进程常见）。"""
    handle = _open_process(pid)
    if not handle:
        return None

    psapi = _dll("psapi")
    k32 = _dll("kernel32")
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return ProcessMemoryCounters(
            working_set=int(counters.WorkingSetSize),
            peak_working_set=int(counters.PeakWorkingSetSize),
            private_bytes=int(counters.PrivateUsage),
            peak_private_bytes=int(counters.PeakPagefileUsage),
            page_faults=int(counters.PageFaultCount),
        )
    finally:
        k32.CloseHandle(handle)


def process_exe_path(pid: int) -> str | None:
    """取进程的可执行文件完整路径。"""
    handle = _open_process(pid)
    if not handle:
        return None

    k32 = _dll("kernel32")
    k32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
    ]
    k32.QueryFullProcessImageNameW.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buffer))
        if not k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value or None
    finally:
        k32.CloseHandle(handle)


def process_command_line(pid: int) -> str | None:
    """取进程命令行。

    用 ``NtQueryInformationProcess(ProcessCommandLineInformation)``（Windows 8.1+），
    比读 PEB 安全，也比 WMI 快几个数量级。
    """
    handle = _open_process(pid)
    if not handle:
        return None

    ntdll = _dll("ntdll")
    k32 = _dll("kernel32")
    ntdll.NtQueryInformationProcess.argtypes = [
        wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    k32.CloseHandle.argtypes = [wt.HANDLE]

    try:
        needed = wt.ULONG(0)
        ntdll.NtQueryInformationProcess(
            handle, ProcessCommandLineInformation, None, 0, ctypes.byref(needed)
        )
        if not needed.value:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        status = ntdll.NtQueryInformationProcess(
            handle, ProcessCommandLineInformation, buffer, needed.value,
            ctypes.byref(needed),
        )
        if status != 0:
            return None
        text = ctypes.cast(buffer, ctypes.POINTER(UNICODE_STRING)).contents
        if not text.Buffer or not text.Length:
            return None
        return ctypes.wstring_at(text.Buffer, text.Length // 2)
    finally:
        k32.CloseHandle(handle)


def process_start_time(pid: int) -> float | None:
    """取进程启动时间（Unix 时间戳，秒）。

    这是识别 **PID 重用造成的假父子关系** 的唯一可靠依据：
    子进程不可能比父进程先启动。
    """
    handle = _open_process(pid)
    if not handle:
        return None

    k32 = _dll("kernel32")
    k32.GetProcessTimes.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ]
    k32.GetProcessTimes.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    try:
        created = FILETIME()
        if not k32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(FILETIME()),
            ctypes.byref(FILETIME()), ctypes.byref(FILETIME()),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000 - _FILETIME_EPOCH_DELTA
    finally:
        k32.CloseHandle(handle)


def process_session_id(pid: int) -> int:
    k32 = _dll("kernel32")
    k32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
    k32.ProcessIdToSessionId.restype = wt.BOOL
    session = wt.DWORD(0)
    if k32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return int(session.value)
    return 0


# --------------------------------------------------------------------------
# 文件版本信息
# --------------------------------------------------------------------------


def _version_translations(buffer) -> list[tuple[int, int]]:
    """取版本资源里的语言/代码页组合。

    不同软件用不同语言写版本信息（中文软件常用 0804，英文用 0409），
    硬编码 ``040904B0`` 会让一部分软件取不到公司名。
    """
    version = _dll("version")
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.UINT),
    ]
    version.VerQueryValueW.restype = wt.BOOL

    pointer = ctypes.c_void_p()
    length = wt.UINT()
    if not version.VerQueryValueW(
        buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)
    ):
        return []
    if not pointer.value or length.value < 4:
        return []

    raw = ctypes.string_at(pointer.value, length.value)
    out: list[tuple[int, int]] = []
    for i in range(0, len(raw) - 3, 4):
        lang = int.from_bytes(raw[i : i + 2], "little")
        codepage = int.from_bytes(raw[i + 2 : i + 4], "little")
        out.append((lang, codepage))
    return out


def _clean_version_string(value: str) -> str:
    """清理版本资源里的字符串。

    真实文件里出现过 ``"NVIDIA App  <Product"`` 这种脏值——既有控制字符，
    又残留着没被替换的模板占位符。直接拿去当应用名会很难看，
    还可能破坏输出格式（例如把终端搞乱）。

    处理：去掉控制字符、折叠空白；若残留 ``<...>`` 占位符则从那里截断。
    """
    if not value:
        return ""

    # 去掉不可打印的控制字符，但**保留空白字符**（它们稍后会被折叠成空格）。
    # 直接丢掉 \t 会把 "A\t\tB" 粘成 "AB"，那比留个空格更糟。
    cleaned = "".join(ch for ch in value if ch.isprintable() or ch.isspace())
    cleaned = " ".join(cleaned.split()).strip()

    # 未替换的模板占位符：从第一个尖括号处截断。
    # 注意用 >= 0，这样 "<Product>" 这种整体就是占位符的值会被清空。
    for marker in ("<", ">"):
        index = cleaned.find(marker)
        if index >= 0:
            cleaned = cleaned[:index].rstrip(" -_/")
            break

    return cleaned


def file_version_info(exe_path: str | None) -> dict[str, str]:
    """读取可执行文件的版本资源。

    返回 ``{"CompanyName": ..., "ProductName": ..., "FileDescription": ...}``。

    这是把 ``steamwebhelper.exe`` 变成 "Steam · Valve Corporation" 的关键，
    也是产品归并（把 steam.exe 和 steamwebhelper.exe 归到一起）的依据。
    """
    if not exe_path:
        return {}

    version = _dll("version")
    version.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wt.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p
    ]
    version.GetFileVersionInfoW.restype = wt.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.UINT),
    ]
    version.VerQueryValueW.restype = wt.BOOL

    handle = wt.DWORD(0)
    size = version.GetFileVersionInfoSizeW(exe_path, ctypes.byref(handle))
    if not size:
        return {}

    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(exe_path, 0, size, buffer):
        return {}

    translations = _version_translations(buffer) or [(0x0409, 0x04B0)]
    wanted = ("CompanyName", "ProductName", "FileDescription", "InternalName",
              "OriginalFilename", "FileVersion", "ProductVersion")

    result: dict[str, str] = {}
    for lang, codepage in translations:
        prefix = f"\\StringFileInfo\\{lang:04X}{codepage:04X}\\"
        for key in wanted:
            if key in result:
                continue
            pointer = ctypes.c_void_p()
            length = wt.UINT()
            if version.VerQueryValueW(
                buffer, prefix + key, ctypes.byref(pointer), ctypes.byref(length)
            ):
                if pointer.value and length.value:
                    raw = ctypes.wstring_at(pointer.value, length.value)
                    cleaned = _clean_version_string(raw)
                    if cleaned:
                        result[key] = cleaned
        # 主要字段齐了就不用再试其它语言
        if "CompanyName" in result and "ProductName" in result:
            break

    return result


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------

_SERVICE_STATE_NAMES = {
    1: "stopped", 2: "start-pending", 3: "stop-pending", 4: "running",
    5: "continue-pending", 6: "pause-pending", 7: "paused",
}


def enumerate_services() -> list[ServiceEntry]:
    """枚举 Windows 服务，返回 ``(服务名, 显示名, 宿主进程 PID)``。

    这是"方案 B"的基础：一个 ``svchost.exe`` 可以承载多个服务，
    只有拿到这张表才能回答"到底是谁在吃内存"。
    未运行的服务 ``pid`` 为 0，调用方应过滤。
    """
    advapi32 = _dll("advapi32")
    advapi32.OpenSCManagerW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
    advapi32.OpenSCManagerW.restype = wt.HANDLE
    advapi32.EnumServicesStatusExW.argtypes = [
        wt.HANDLE, ctypes.c_int, wt.DWORD, wt.DWORD,
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), wt.LPCWSTR,
    ]
    advapi32.EnumServicesStatusExW.restype = wt.BOOL
    advapi32.CloseServiceHandle.argtypes = [wt.HANDLE]

    manager = advapi32.OpenSCManagerW(None, None, SC_MANAGER_ENUMERATE_SERVICE)
    if not manager:
        raise ctypes.WinError(ctypes.get_last_error())

    entries: list[ServiceEntry] = []
    try:
        size = 256 * 1024
        for _ in range(8):  # 缓冲不够就翻倍重试
            buffer = ctypes.create_string_buffer(size)
            needed = wt.DWORD(0)
            returned = wt.DWORD(0)
            resume = wt.DWORD(0)
            ok = advapi32.EnumServicesStatusExW(
                manager,
                SC_ENUM_PROCESS_INFO,
                SERVICE_WIN32,
                SERVICE_ACTIVE | SERVICE_INACTIVE,
                buffer,
                size,
                ctypes.byref(needed),
                ctypes.byref(returned),
                ctypes.byref(resume),
                None,
            )
            if ok:
                array_type = ENUM_SERVICE_STATUS_PROCESSW * max(1, returned.value)
                items = ctypes.cast(buffer, ctypes.POINTER(array_type)).contents
                for item in items[: returned.value]:
                    status = item.ServiceStatusProcess
                    entries.append(
                        ServiceEntry(
                            name=item.lpServiceName or "",
                            display_name=item.lpDisplayName or "",
                            pid=int(status.dwProcessId),
                            state=_SERVICE_STATE_NAMES.get(
                                int(status.dwCurrentState), "unknown"
                            ),
                        )
                    )
                return entries
            error = ctypes.get_last_error()
            if error == ERROR_MORE_DATA and needed.value > size:
                size = needed.value + 65536
                continue
            raise ctypes.WinError(error)
        return entries
    finally:
        advapi32.CloseServiceHandle(manager)


# --------------------------------------------------------------------------
# 性能计数器（PDH）—— 拿分页列表细分
# --------------------------------------------------------------------------


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000

# 计数器路径 -> 输出键。用英文计数器名，配合 PdhAddEnglishCounter
# 可以在任何语言版本的 Windows 上工作。
_COUNTER_PATHS: dict[str, str] = {
    r"\Memory\Available MBytes": "available_mb",
    r"\Memory\Committed Bytes": "committed_bytes",
    r"\Memory\Commit Limit": "commit_limit",
    r"\Memory\Standby Cache Core Bytes": "standby_core",
    r"\Memory\Standby Cache Normal Priority Bytes": "standby_normal",
    r"\Memory\Standby Cache Reserve Bytes": "standby_reserve",
    r"\Memory\Free & Zero Page List Bytes": "free_and_zero",
    r"\Memory\Modified Page List Bytes": "modified",
    r"\Memory\Cache Bytes": "cache_bytes",
    r"\Memory\Pool Paged Bytes": "pool_paged",
    r"\Memory\Pool Nonpaged Bytes": "pool_nonpaged",
    r"\Memory\% Committed Bytes In Use": "commit_percent",
}


def memory_counters(retries: int = 2) -> dict[str, float]:
    """读取 ``\\Memory\\*`` 性能计数器。

    psapi 拿不到分页列表的细分（待机缓存、修改页、空闲页），
    而这些恰恰是解释"为什么空闲内存看起来这么少"的关键。

    读取失败时返回**已成功取到的部分**，不抛异常——细分数据是锦上添花，
    不该因为它拿不到就让整个快照失败。
    """
    pdh = _dll("pdh")
    pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    pdh.PdhOpenQueryW.restype = ctypes.c_long
    pdh.PdhAddEnglishCounterW.argtypes = [
        ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
    pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    pdh.PdhCollectQueryData.restype = ctypes.c_long
    pdh.PdhGetFormattedCounterValue.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(PDH_FMT_COUNTERVALUE),
    ]
    pdh.PdhGetFormattedCounterValue.restype = ctypes.c_long
    pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]

    query = ctypes.c_void_p()
    if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
        return {}

    out: dict[str, float] = {}
    try:
        handles: list[tuple[str, ctypes.c_void_p]] = []
        for path, key in _COUNTER_PATHS.items():
            counter = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(query, path, None, ctypes.byref(counter)) == 0:
                handles.append((key, counter))

        # 某些计数器需要两次采样才有值
        import time

        for attempt in range(max(1, retries)):
            if pdh.PdhCollectQueryData(query) != 0:
                break
            if attempt + 1 < retries:
                time.sleep(0.15)

        for key, counter in handles:
            value = PDH_FMT_COUNTERVALUE()
            counter_type = wt.DWORD(0)
            status = pdh.PdhGetFormattedCounterValue(
                counter, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100,
                ctypes.byref(counter_type), ctypes.byref(value),
            )
            if status == 0:
                out[key] = float(value.doubleValue)
        return out
    finally:
        pdh.PdhCloseQuery(query)
