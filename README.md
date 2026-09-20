# memscope

> **诚实的内存诊断器。** 观测、归因、找增长——**但不「释放内存」**。

[![CI](https://github.com/Ray144165154/memscope/actions/workflows/ci.yml/badge.svg)](https://github.com/Ray144165154/memscope/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078d4)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](#零依赖不是口号)

```powershell
python -m memscope
```

不需要 `pip install`，不需要**任何**第三方库。

---

## 先说清楚：它不是内存清理软件

市面上几乎所有的"内存优化"工具都在做同一件事：调用 `EmptyWorkingSet()`
把进程的工作集强行清空。后果是：

1. 那些页面被赶到**页面文件**（硬盘上的交换文件）
2. 程序下一瞬间又需要这些数据，于是触发**硬缺页**
3. 系统必须暂停程序、从硬盘把数据读回来
4. **内存数字变好看了，电脑变慢了**

`memscope` 站在对立面。它**不修改任何进程状态**——所有操作都是只读的。
它只做三件事：

| 能力 | 说明 |
|---|---|
| **把内存状态解释对** | 不说"你只剩 190 MB 了"，而是告诉你那 3 GB 在待机缓存里、随时可用 |
| **把开销归因到应用** | 通过进程树与产品签名，把 21 个 WebView2 进程正确拆到各自的宿主应用 |
| **找出正在增长的东西** | 持续采样 + 线性回归，区分"真泄漏"与"正常波动" |

它没有动机让任何数字变好看——**所以它的结论你可以自己核对。**

---

## 目录

- [快速开始](#快速开始)
- [它长什么样](#它长什么样)
- [应用归因是怎么做准的](#应用归因是怎么做准的)
- [命令行参考](#命令行参考)
- [Python API](#python-api)
- [它是怎么工作的](#它是怎么工作的)
- [零依赖不是口号](#零依赖不是口号)
- [测试](#测试)
- [与其他工具对比](#与其他工具对比)
- [内存模型与原理](docs/WINDOWS-MEMORY.md)
- [已知限制](docs/LIMITATIONS.md)
- [许可](#许可)

---

## 快速开始

```powershell
git clone https://github.com/Ray144165154/memscope.git
cd memscope

# 直接跑，不用安装
python -m memscope
```

想装成全局命令（可选）：

```powershell
pip install -e .
memscope
```

想找出内存泄漏：

```powershell
memscope watch -i 10 -d 3600 -o samples.jsonl    # 采样 1 小时
memscope analyze samples.jsonl                    # 分析增长趋势
```

---

## 它长什么样

```
── memscope — Windows 内存诊断 ─────────────────────────────────────────────
采集时间  2026-09-20 17:03:24

── 系统内存 ──────────────────────────────────────────────────────────────
物理内存总量            15.18 GB

  使用中                11.92 GB   ████████████████████████······
  待机缓存               3.26 GB   ██████························   随时可用
  空闲+零页              3.99 MB   ······························

  可用合计               3.27 GB   ██████························   (22%)

提交量（程序申请）      20.26 GB
提交上限                40.18 GB   (已用 50%)
  └ 超出物理内存         5.08 GB   只能在页面文件（硬盘）里

── 诊断结论 ──────────────────────────────────────────────────────────────
[!!]  提交量 (20.26 GB) 已超过物理内存 (15.18 GB)
      超出 5.08 GB。这部分数据不可能待在内存里，只能留在页面文件（硬盘）上。
      程序一旦访问到这部分内存，就会触发硬缺页，必须从硬盘读回来
      ——这正是切换程序时卡顿、响应变慢的主要来源。
      → 减少常驻程序比加内存更省事；如果长期如此且预算允许，加内存是最直接的解法。

[i]   「空闲内存」只有 4 MB，但这是正常的
      另外还有 3.26 GB 在待机缓存里。只要有程序需要内存，这部分会立刻被回收
      给它，速度与空闲内存没有区别。
      → 不要使用任何声称能「释放内存」的工具清理这部分。它们只是把数据丢掉、
        让程序重新从硬盘读一遍，数字好看了，系统反而更慢。

[!]   检测到 5 类「装了但未必需要常驻」的软件
        · 游戏平台：2373 MB — Steam(987MB)、EA(923MB)、Epic Online Services(125MB)
        · 外设驱动软件：1421 MB — RazerAppEngine(863MB)、LGHUB Agent(124MB)
        · 显卡增强/覆盖层：887 MB — NVIDIA App(398MB)、AMD Software(259MB)
        · 远程控制：523 MB — 贝锐向日葵(523MB)
        合计约 5.52 GB。
      → 这些软件多数提供「关闭开机自启」或「退出后台」选项。

── 应用占用排行（第三方软件） ───────────────────────────────────────────────
  应用                          进程       私有内存     占比    置信度
  ───────────────────────────────────────────────────────
  Steam                        8     987 MB 10.2%      高
  Microsoft Edge              15     931 MB  9.6%      高
  EA                           7     923 MB  9.5%      高
  RazerAppEngine               8     863 MB  8.9%      高
  贝锐向日葵                       10     523 MB  5.4%      高
  ...
```

注意那个 `[i]` 条目——**那正是"内存清理软件"会大喊"内存只剩 4MB 了！"的地方。**
`memscope` 告诉你的是完整事实。

---

## 应用归因是怎么做准的

这是整个项目最难做对、也最影响结论正确性的部分。

### 三种"想当然"的做法全是错的

| 做法 | 为什么错 |
|---|---|
| **按进程名分组** | Steam = `steam.exe` + 7 个 `steamwebhelper.exe`；而 `node.exe` 同时服务于好几种互不相关的程序 |
| **按可执行文件路径分组** | 21 个 `msedgewebview2.exe` 路径**完全相同**，却可能分属好几个不同应用的界面 |
| **按父进程树分组** | `services.exe` 有 138 个子进程、`explorer.exe` 有 12 个——合并起来就是一个没有意义的巨型条目 |

### 做法：角色决定策略

不存在一套万能规则，所以 `memscope` 先给每个进程**判定角色**，
再按角色分派不同的策略：

| 角色 | 归因策略 |
|---|---|
| **产品独占型**（有厂商签名） | 按产品签名合并；**同一产品族的不同产品名也能合并** |
| **通用运行时**（node / python / java） | 绝不按名字合并，改按命令行里的脚本路径区分 |
| **共享运行时**（WebView2 / conhost） | **向上穿透**父进程链，找到真正的宿主 |
| **服务宿主**（svchost） | 查询服务表，按承载的服务名细分 |
| **启动器**（explorer / services） | 不作为归因目标，子进程各自立根 |
| **系统组件** | 按可执行文件名合并 |

真实效果——同一台机器上的 21 个 `msedgewebview2.exe`：

```
9 个 → 归因到 '贝锐向日葵'
      依据: 向上穿透 msedgewebview2.exe(25080) → msedgewebview2.exe(28024) → AweSun.exe(28264)
6 个 → 归因到 'Widgets.exe'
      依据: 向上穿透 msedgewebview2.exe(5548) → msedgewebview2.exe(34724) → Widgets.exe(19200)
6 个 → 归因到 'SearchHost.exe'
      依据: 向上穿透 msedgewebview2.exe(18768) → msedgewebview2.exe(22124) → SearchHost.exe(18600)
```

**没有被错误地归成一个 "Microsoft Edge"。**

### 三个容易被忽略的坑，这里都处理了

**① PID 重用造成的假父子关系**

父进程早已退出，系统把它的 PID 分配给了一个新进程。此时进程树里那条边
指向的是一个毫不相干的东西。判据很朴素但可靠：**子进程不可能比父进程先出生**。

**② 同一个软件的不同组件写着不一样的产品名**

Steam 主程序写 `Steam`，界面进程写 `Steam Client WebHelper`。
只按产品名精确匹配会把同一个软件拆成两条记录。这里用"同厂商 + 产品名互为前缀
且边界落在词边界上"来合并（所以 `EA` 能合并 `EA Desktop`，但不会吞掉 `Eagle`）。

**③ 系统保护进程读不到任何信息**

一台真实机器上 350 个进程里有 172 个打不开——这是常态。与其产出上百条
名字都是猜的碎片记录，不如诚实地合并成一个「系统保护进程」桶并写明原因。

### 置信度与可审计

每个归因都有置信度（高 / 中 / 低），并且**每条结论都能追溯依据**：

```powershell
memscope --explain 22124
```

```
  PID 22124  msedgewebview2.exe  →  贝锐向日葵
      · msedgewebview2.exe 是共享运行时，真正的宿主在父进程链上游
      · 共享运行时向上穿透：msedgewebview2.exe(22124) → AweSun.exe(28264)，归因到该宿主
```

**这是"内存清理软件"的反面**：它们给你一个无法验证的"已释放 2GB"；
`memscope` 给你一条你可以自己核对的推理链。

---

## 命令行参考

```powershell
memscope                        快照报告（默认）
memscope --all                  展开系统组件明细
memscope -n 30                  每类显示 30 条
memscope --explain 1234         查看某个进程的归因依据（可重复）
memscope --json                 以 JSON 输出
memscope --html 报告.html       生成自包含的 HTML 报告
memscope --fast                 跳过命令行/版本信息（更快，但归因精度下降）
memscope --no-services          跳过服务表解析（svchost 将无法按服务名细分）

memscope watch -i 10 -d 3600 -o samples.jsonl   采样
memscope analyze samples.jsonl                  分析增长趋势
memscope analyze samples.jsonl --html 趋势.html
```

### 采样与泄漏检测

```powershell
> memscope watch -i 5 -d 600 -o samples.jsonl
开始采样：每 5 秒一次，共 600 秒，写入 samples.jsonl
  第    1 次  可用     3.31 GB  提交    20.30 GB  应用 170 个
  第    2 次  可用     3.33 GB  提交    20.29 GB  应用 170 个
  ...

> memscope analyze samples.jsonl
采样条数  120      时间跨度  0.17 小时

  发现 1 个持续增长的应用：

  应用                                     起始      结束   增长/小时    R²  判定
  ──────────────────────────────────────────────────────────────────────
  SomeApp.exe                            210 MB   285 MB     +450.0M  0.94  疑似泄漏

  判读方法：
    · 「疑似泄漏」= 每小时增长显著，且 R² 高（增长接近一条直线）
    · 「持续增长」= 在涨，但还不足以断定泄漏——可能只是缓存增长
    · R² 越接近 1，说明越不像随机波动

  注意：见到「疑似泄漏」不等于程序有 bug。
  缓存、连接池、会话表都会正常增长到一个稳定平台。
```

**为什么要看 R² 而不只看斜率？** 内存占用天然噪声很大，程序申请/释放内存的
锯齿会让斜率随机浮动。R² 接近 1 说明增长是**持续的、单调的**，这才像泄漏；
R² 很低说明只是波动，不该报警。

---

## Python API

```python
from memscope import take_snapshot, diagnose, analyze_samples

# 一次快照
snapshot = take_snapshot()
print(snapshot.system.commit_exceeds_physical)   # 超出物理内存的字节数

for app in snapshot.applications[:10]:
    print(f"{app.name:<30} {app.process_count:>3} 进程  "
          f"{app.private_bytes / 1024**2:>8.0f} MB  "
          f"置信度={app.confidence.value}")

# 诊断结论
for insight in diagnose(snapshot.system, snapshot.applications):
    print(f"[{insight.severity.value}] {insight.title}")
    print(f"    {insight.detail}")

# 分析采样文件
from memscope.sampling import read_samples

trends = analyze_samples(read_samples("samples.jsonl"))
for trend in trends:
    if trend.is_alert:
        print(f"{trend.name}: {trend.slope_mb_per_hour:+.1f} MB/小时 "
              f"(R²={trend.r_squared:.2f}, 判定={trend.verdict})")
```

只想要归因算法（纯函数，不碰系统 API，**可在任何平台运行**）：

```python
from memscope import attribute, ProcessRecord

processes = [
    ProcessRecord(pid=100, ppid=0, name="app.exe",
                  exe_path=r"C:\Program Files\MyApp\app.exe",
                  company="My Corp", product="My App", private_bytes=100_000_000),
    ProcessRecord(pid=101, ppid=100, name="msedgewebview2.exe",
                  command_line="msedgewebview2.exe --embedded-browser-webview",
                  private_bytes=50_000_000),
]
result = attribute(processes)
print(result.applications[0].name)   # 'My App'
print(result.applications[0].pids)   # [100, 101]
```

---

## 它是怎么工作的

```
                  Windows API（ctypes，全部只读）
   ┌──────────────────────────────────────────────────────────────┐
   │ psapi   GetPerformanceInfo      系统内存总览（单位是「页」）    │
   │ kernel32 CreateToolhelp32Snapshot  进程列表 + 父进程 PID        │
   │ psapi   GetProcessMemoryInfo    单进程内存明细                 │
   │ version GetFileVersionInfo      公司名 / 产品名（归因的关键）    │
   │ ntdll   NtQueryInformationProcess  命令行（判断 WebView2 宿主） │
   │ advapi32 EnumServicesStatusEx   服务表（svchost 细分 = 方案 B） │
   │ pdh     性能计数器               分页列表细分                  │
   └───────────────────────────┬──────────────────────────────────┘
                               ↓
   ┌──────────────────────────────────────────────────────────────┐
   │ collect.py   采集 + 缓存（静态信息按 (pid, 启动时间) 缓存）      │
   └───────────────────────────┬──────────────────────────────────┘
                               ↓
   ┌──────────────────────────────────────────────────────────────┐
   │ classify.py   判定角色（纯规则，不碰系统 API）                  │
   │ attribution.py 归因（纯函数，不碰系统 API）                     │
   └───────────────────────────┬──────────────────────────────────┘
                               ↓
   ┌──────────────────────────────────────────────────────────────┐
   │ diagnose.py   诊断规则 → 可核对的结论                          │
   │ sampling.py   时间序列 → JSONL                                │
   │ analysis.py   线性回归 → 泄漏判定                              │
   │ report.py     文本 / HTML+SVG（手写 SVG，无图表库）             │
   └──────────────────────────────────────────────────────────────┘
```

**关键的架构决定：归因是纯函数。**

`attribute(processes) -> applications` 只读数据、只写数据，不调用任何系统 API。
于是它可以：

- 用人工构造的真实拓扑做**完整测试**
- 在 **Linux / macOS 上运行**（CI 里三个平台都跑归因测试）
- 被任何人嵌进自己的程序里

Windows 那一层只负责**填数据**，这一层只负责**推理**——两边互不知道对方怎么实现。

---

## 零依赖不是口号

```powershell
> python tools/check_zero_deps.py
已检查 12 个源文件，运行时导入全部来自标准库 ✔
```

这个检查在 CI 的每个平台、每个 Python 版本上都会跑。任何第三方 import
都会让构建失败——包括未来的我自己加的那种。

> `ctypes` 和 `ctypes.wintypes` 都是**标准库**的一部分。
> 直接调用 Windows API 并不需要装任何东西，这正是本项目能"clone 下来直接跑"的原因。

---

## 测试

```powershell
> python run_tests.py
Ran 199 tests in 0.87s

OK (skipped=1)
```

- **归因测试**用真实拓扑做夹具：WebView2 三层穿透链、Steam 的 7 个助手进程、
  svchost 承载多服务、PID 重用造成的假父子、`services.exe` 的 138 个子进程……
  每个用例都对应一个**真实的失败模式**，不是为覆盖率写的
- **Windows API 测试**真的去调用系统 API（在非 Windows 上自动跳过），
  能发现"结构体字段定义错了""单位换算是页数不是字节"这类光看代码发现不了的问题
- 测试只用标准库 `unittest`——`pip install pytest` 不是运行测试的前提

> 本项目开发过程中，测试抓到了两个真实的单位换算 bug：
> `GetPerformanceInfo` 的字段单位是**页数而非字节**；
> 泄漏判定的斜率是**字节/小时**而门槛常量是**MB/小时**。
> 两种都会让结论完全错误，且肉眼极难发现。

---

## 与其他工具对比

| | memscope | 任务管理器 | "内存优化"软件 | RAMMap |
|---|---|---|---|---|
| 只读，不修改系统状态 | ✅ | ✅ | ❌ 强行清空工作集 | ✅ |
| 解释待机缓存不是浪费 | ✅ | ❌ | ❌ 反而利用这个误解 | ⚠️ 需要自己解读 |
| 跨进程归因到应用 | ✅ | ⚠️ 仅按进程 | ❌ | ❌ |
| 共享运行时正确归因 | ✅ | ❌ | ❌ | ❌ |
| svchost 按服务名细分 | ✅ | ⚠️ 部分 | ❌ | ❌ |
| 泄漏趋势检测 | ✅ | ❌ | ❌ | ❌ |
| 零依赖 / 脚本可调用 | ✅ | ❌ | ❌ | ❌ |
| 图形界面 | ❌ 命令行 | ✅ | ✅ | ✅ |

**说白了：**

- 你要**图形界面、点点看** → 用任务管理器 / Process Explorer
- 你要**深入内核对象**（句柄、驱动、物理页映射）→ 用 RAMMap / VMMap
- 你要**被解释清楚、能脚本化、能找泄漏、且不会被误导** → 用 `memscope`

---

## 已知限制

见 [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md)。几个要点：

- **只能运行在 Windows 上**（归因与分析算法本身跨平台，但采不到数据）
- 系统保护进程读不到详情（一台典型机器上约一半进程如此），
  只能合并成一个桶
- 关键词匹配的后台软件分类**可能认错**
- 泄漏检测需要足够长的采样窗口；样本不足时明确拒绝下结论
- 不做任何修改性操作——这不是缺陷，是设计

---

## 许可

[MIT](LICENSE)
