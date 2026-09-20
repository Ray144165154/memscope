"""测试夹具：用真实机器的拓扑构造进程数据。

为什么不用录制的真实快照？
  * 一份快照只能反映某一时刻，改不了、看不清在测什么
  * 真实快照里有用户名、路径等隐私信息，不适合进仓库
  * 关键场景（PID 重用、父链断裂）在快照里**未必出现**，但必须被测到

所以这里用工厂函数把拓扑写成代码。下面每一条夹具都来自在一台真实 Windows
机器上观测到的结构，注释里注明了来源。
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memscope.model import ProcessRecord, ServiceRecord  # noqa: E402

MB = 1024 * 1024
GB = 1024 * 1024 * 1024

# 测试用临时目录放在仓库内部，而不是系统临时目录。
# 原因：受限环境（文件沙箱、某些 CI 配置）可能不允许写入 $TEMP，
# 而仓库目录在跑测试时一定是可写的。用 tests/_tmp 并加进 .gitignore。
_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
_counter = [0]

__all__ = ["proc", "MB", "GB", "temp_dir", "steam_topology", "webview2_topology",
           "ea_topology", "svchost_with_services", "fake_parent_topology"]


@contextmanager
def temp_dir(name: str = "case"):
    """创建一个用完即删的临时目录。

    刻意**不用** ``tempfile.mkdtemp``：本项目开发用的文件沙箱会拒绝写入
    动态创建的随机目录（普通子目录则正常）。用一个确定性的名字加计数器
    既避开了这个问题，也便于并发时区分。

    临时目录放在仓库内部而不是系统临时目录——受限环境下后者可能整体不可写。
    """
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    _counter[0] += 1
    path = _TMP_ROOT / f"{name}_{os.getpid()}_{_counter[0]}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def proc(
    pid: int,
    ppid: int = 0,
    name: str = "",
    *,
    exe_path: str | None = None,
    command_line: str | None = None,
    company: str | None = None,
    product: str | None = None,
    description: str | None = None,
    start_time: float | None = None,
    private: int = 0,
    working: int | None = None,
    page_faults: int = 0,
    accessible: bool = True,
) -> ProcessRecord:
    """构造一条进程记录。``private`` 单位是字节。"""
    return ProcessRecord(
        pid=pid,
        ppid=ppid,
        name=name,
        exe_path=exe_path,
        command_line=command_line,
        company=company,
        product=product,
        description=description,
        start_time=start_time,
        private_bytes=private,
        working_set=private if working is None else working,
        page_faults=page_faults,
        accessible=accessible,
    )


def steam_topology() -> list[ProcessRecord]:
    """Steam：一个主进程 + 7 个界面助手。

    真实观测：``steamwebhelper.exe`` 有 7 个实例，合计约 817 MB；
    ``steam.exe`` 是它们的父进程。

    **注意产品名不一样**：主程序写 ``Steam``，界面进程写
    ``Steam Client WebHelper``。只按产品名精确匹配就会把同一个软件
    拆成两条记录——这正是产品族合并规则要解决的问题。
    """
    procs = [
        proc(
            30900, 15864, "steam.exe",
            exe_path=r"C:\Program Files (x86)\Steam\steam.exe",
            company="Valve Corporation", product="Steam",
            start_time=1000.0, private=120 * MB,
        )
    ]
    for i in range(7):
        procs.append(
            proc(
                30930 + i, 30900, "steamwebhelper.exe",
                exe_path=r"C:\Program Files (x86)\Steam\bin\cef\cef.win7x64\steamwebhelper.exe",
                company="Valve Corporation", product="Steam Client WebHelper",
                command_line=f'"...\\steamwebhelper.exe" --type=renderer --pid={30930 + i}',
                start_time=1010.0 + i, private=100 * MB,
            )
        )
    return procs


def webview2_topology(host_pid: int = 18600, host_name: str = "AweSun.exe") -> list[ProcessRecord]:
    """WebView2：宿主 → 浏览器进程 → 渲染/GPU/工具进程。

    真实观测（这是归因最难的一类）::

        PID 22124  父PID 18600   --embedded-browser-webview   ← 浏览器进程
        PID 18768  父PID 22124   --type=crashpad-handler
        PID 22600  父PID 22124   --type=gpu-process
        PID 23000  父PID 22124   --type=renderer

    这 4 个进程的**可执行文件路径完全相同**，但真正的宿主是 18600。
    任何"按进程名分组"或"按路径分组"的做法都会把它们错归成
    "Microsoft Edge WebView2"，而实际上它们是宿主应用的界面。
    """
    wv = r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\153.0.4234.32\msedgewebview2.exe"
    return [
        proc(
            host_pid, 15864, host_name,
            exe_path=r"C:\Program Files\Oray\AweSun\AweSun.exe",
            company="Oray", product="AweSun",
            start_time=2000.0, private=295 * MB,
        ),
        proc(
            host_pid + 3524, host_pid, "msedgewebview2.exe",
            exe_path=wv,
            company="Microsoft Corporation", product="Microsoft Edge WebView2",
            command_line=f'"{wv}" --embedded-browser-webview=1 --no-sandbox',
            start_time=2100.0, private=88 * MB,
        ),
        proc(
            host_pid + 168, host_pid + 3524, "msedgewebview2.exe",
            exe_path=wv,
            company="Microsoft Corporation", product="Microsoft Edge WebView2",
            command_line=(
                f'"{wv}" --type=crashpad-handler '
                r"--user-data-dir=C:\Users\Ray\AppData\Local\Oray\AweSun\EBWebView"
            ),
            start_time=2101.0, private=12 * MB,
        ),
        proc(
            host_pid + 1000, host_pid + 3524, "msedgewebview2.exe",
            exe_path=wv,
            company="Microsoft Corporation", product="Microsoft Edge WebView2",
            command_line=f'"{wv}" --type=gpu-process --embedded-browser-webview',
            start_time=2102.0, private=64 * MB,
        ),
        proc(
            host_pid + 1400, host_pid + 3524, "msedgewebview2.exe",
            exe_path=wv,
            company="Microsoft Corporation", product="Microsoft Edge WebView2",
            command_line=f'"{wv}" --type=renderer --embedded-browser-webview',
            start_time=2103.0, private=140 * MB,
        ),
    ]


def ea_topology() -> list[ProcessRecord]:
    """EA 游戏平台：主程序 + 5 个 CEF 子进程。

    真实观测：``EADesktop.exe`` (391 MB) + ``EACefSubProcess.exe`` ×5 (389 MB)。
    两者**可执行文件名不同**，但厂商同为 Electronic Arts——
    这时厂商就是唯一可用的合并依据。
    """
    procs = [
        proc(
            33976, 15864, "EADesktop.exe",
            exe_path=r"C:\Program Files\Electronic Arts\EA Desktop\EADesktop.exe",
            company="Electronic Arts", product="EA Desktop",
            start_time=3000.0, private=413 * MB,
        )
    ]
    for i in range(5):
        procs.append(
            proc(
                34000 + i, 33976, "EACefSubProcess.exe",
                exe_path=r"C:\Program Files\Electronic Arts\EA Desktop\EACefSubProcess.exe",
                company="Electronic Arts", product="EA Desktop",
                command_line=f'"...\\EACefSubProcess.exe" --type=renderer --id={i}',
                start_time=3010.0 + i, private=78 * MB,
            )
        )
    return procs


def svchost_with_services() -> tuple[list[ProcessRecord], dict[int, list[ServiceRecord]]]:
    """svchost：一个宿主进程承载多个 Windows 服务（方案 B 的核心场景）。

    真实观测：系统里有 89 个 svchost.exe，合计约 1.5 GB。
    不解析服务名的话，这就是一个 1.5 GB 的黑盒。
    """
    procs = [
        proc(600, 4, "services.exe",
             exe_path=r"C:\Windows\System32\services.exe",
             company="Microsoft Corporation",
             product="Microsoft® Windows® Operating System",
             start_time=100.0, private=9 * MB),
        proc(1000, 600, "svchost.exe",
             exe_path=r"C:\Windows\System32\svchost.exe",
             company="Microsoft Corporation",
             product="Microsoft® Windows® Operating System",
             start_time=200.0, private=180 * MB),
        proc(1001, 600, "svchost.exe",
             exe_path=r"C:\Windows\System32\svchost.exe",
             company="Microsoft Corporation",
             product="Microsoft® Windows® Operating System",
             start_time=201.0, private=95 * MB),
    ]
    services = {
        1000: [
            ServiceRecord("wuauserv", "Windows Update", 1000, "running"),
            ServiceRecord("BITS", "Background Intelligent Transfer Service", 1000, "running"),
            ServiceRecord("CryptSvc", "Cryptographic Services", 1000, "running"),
        ],
        1001: [
            ServiceRecord("Audiosrv", "Windows Audio", 1001, "running"),
        ],
    }
    return procs, services


def fake_parent_topology() -> list[ProcessRecord]:
    """PID 重用造成的假父子关系。

    场景：PID 800 的进程早已退出，系统把 800 分配给了新进程 ``recycled.exe``
    （启动时间 5000）。而 ``msedgewebview2.exe`` 记录里仍然写着父进程是 800，
    它自己的启动时间是 2000 —— **子进程不可能比父进程晚出生**，
    所以这条父子关系是假的。

    不处理这种情况，WebView2 就会被错误地归到 ``recycled.exe`` 头上。
    """
    return [
        proc(800, 4, "recycled.exe",
             exe_path=r"C:\Program Files\Other\recycled.exe",
             company="Other Corp", product="Recycled App",
             start_time=5000.0, private=50 * MB),
        proc(900, 800, "msedgewebview2.exe",
             exe_path=r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\153.0.4234.32\msedgewebview2.exe",
             company="Microsoft Corporation", product="Microsoft Edge WebView2",
             command_line='"msedgewebview2.exe" --embedded-browser-webview=1',
             start_time=2000.0, private=70 * MB),
    ]
