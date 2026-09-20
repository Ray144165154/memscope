# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

## [0.1.0] - 2026-09-20

首个版本。实现了只读的 Windows 内存诊断：观测、归因、趋势分析。

### 新增

**系统内存模型**
- `GetPerformanceInfo` 封装（**注意字段单位是页数，需乘 `PageSize`**）
- `pdh` 性能计数器读取分页列表细分：待机缓存（核心/常规/保留）、
  空闲与零页、修改页
- 提交量 / 提交上限 / 超出物理内存的差额计算与解释
- `SystemMemory` 的 `commit_pressure`、`available_ratio`、
  `commit_exceeds_physical` 等派生属性

**进程采集**
- `CreateToolhelp32Snapshot` 进程枚举（含父进程 PID）
- `GetProcessMemoryInfo` 单进程内存明细（工作集、私有提交、缺页次数）
- `QueryFullProcessImageName` 可执行文件完整路径
- `NtQueryInformationProcess(ProcessCommandLineInformation)` 命令行
- `GetProcessTimes` 启动时间
- `GetFileVersionInfo` 版本资源（厂商 / 产品名 / 描述），
  支持多语言翻译表，并清理脏值（控制字符、未替换的模板占位符）
- 静态信息按 `(pid, 启动时间)` 缓存，采样时几乎无额外开销

**应用归因（纯函数，跨平台可测）**
- 进程角色判定：产品独占型 / 通用运行时 / 共享运行时 / 启动器 /
  服务宿主 / 系统组件
- 共享运行时**向上穿透**父进程链，找到真正的宿主
- PID 重用造成的假父子关系识别（依据"子进程不可能比父进程先出生"）
- 产品族合并：同厂商 + 产品名互为前缀且边界落在词边界上
  （`Steam` 与 `Steam Client WebHelper` 合并，`EA` 不吞 `Eagle`）
- 通用运行时按命令行脚本路径区分
- **方案 B**：`EnumServicesStatusEx` 解析 svchost 承载的服务名
- 系统保护进程合并成一个诚实的桶，而非上百条碎片
- 每条归因带置信度与可审计的证据链（`--explain`）

**诊断规则**
- 提交量 vs 物理内存（最重要的规则，解释硬缺页与卡顿的因果链）
- 物理可用比例分级
- **主动解释"空闲内存很少"是正常现象**，并明确劝阻清理待机缓存
- 提交上限压力
- 内核非分页池异常（驱动泄漏信号）
- 后台常驻软件群分类（游戏平台 / 外设驱动 / 显卡增强 / 远程控制 /
  云盘 / 通讯娱乐），并声明关键词匹配可能认错

**采样与泄漏检测**
- 采样写入 JSONL（追加 + 立即刷盘，抗 Ctrl+C 中断）
- 坏行跳过而非整体失败
- 最小二乘线性回归，用斜率表示增长速度、R² 表示"像不像泄漏"
- 样本不足时明确拒绝下结论

**报告**
- 文本报告：三段并列的内存条（使用中 / 待机缓存 / 空闲），
  刻意不把空闲内存画成红色警示条
- HTML 报告：自包含、无外部资源、**手写 SVG**，无图表库
- JSON 输出、`--explain` 归因审计

**工程**
- 运行时零依赖，`tools/check_zero_deps.py` 静态校验并在 CI 中强制执行
- 199 个测试，仅用标准库 `unittest`
- 测试夹具由真实机器拓扑构造（WebView2 穿透链、Steam 七进程、
  svchost 多服务、PID 重用假父子）
- GitHub Actions：三平台 × Python 3.10–3.13，
  Windows 上跑采集与 CLI 冒烟测试，非 Windows 上验证核心逻辑可导入

### 修复（开发期间被测试抓到）

- **`GetPerformanceInfo` 字段单位误当作字节**：实际是**页数**，
  不乘 `PageSize` 会得到"内存只用了 5 MB"这种荒唐结论。
  通过与 `pdh` 计数器交叉验证（提交上限 10,533,672 页 × 4096
  = 43,145,920,512 字节，与计数器完全一致）确认。
- **泄漏判定单位不一致**：回归出的斜率是**字节/小时**，而门槛常量是
  **MB/小时**，直接比较会让 8 MB/小时的温和增长（8,388,608 字节）
  看起来远超 20 的门槛，于是**任何增长都被误判为泄漏**。
- **`install_root` 把 `"app"` 当作打包目录**：产品目录名恰好是 `App` 时
  会被剥掉，导致安装根退化成 `C:\Program Files`，
  把大量互不相关的软件合并成一组。已加入"不得剥到安装根之上"的护栏。
- **`watch` 默认启用 `--fast`**：跳过版本资源会让应用归因碎片化
  （同一软件被拆成几十条），泄漏检测随之失效。已改为默认完整采集。
- **数据缺失时误报内存紧张**：`physical_total` 为 0 时
  `available_ratio` 算出 0，触发"物理可用偏低"警告。已加守卫。
- **argparse 打印中文帮助时崩溃（CI 的 4 个 Windows 任务全挂）**：
  `build_parser()` 自己不重配输出编码。Windows 上标准输出被重定向时的
  默认编码是 GBK / cp1252，argparse 打印中文帮助与用法文本时抛
  `UnicodeEncodeError`。真实的命令行走 `main()` 没问题（那里配了编码），
  但任何直接使用 `build_parser()` 的调用方（测试、嵌进别的程序）都会踩到。
  已让 `build_parser()` 与 `run_tests.py` 各自重配编码，并补了 3 个回归测试
  （用 `TextIOWrapper(encoding="ascii")` 模拟受限输出流）。

  > 第一版回归测试用 `codecs.getwriter("ascii")` 写过——`StreamWriter`
  > 没有 `reconfigure`，等于在验证一个不可能被修复的场景。换成
  > `TextIOWrapper` 才真正测到东西。

- **CI 冒烟测试里 PowerShell 把正常输出当成错误**：Windows 步骤原先设了
  `$ErrorActionPreference = 'Stop'`。这些步骤以原生命令为主，而 PowerShell
  会把原生命令写 stderr 当成**终止错误**——可 stderr 在很多情况下是正常输出：
  `memscope watch` 的采样进度、`unittest` 的测试结果都走 stderr。
  已改为不设该偏好，每个原生命令后显式检查 `$LASTEXITCODE`。

- **CI 的 Windows 步骤从 bash 改为 pwsh**：Git Bash（MSYS2）在路径转换、
  重定向与管道上有额外一层行为，出问题难以定位，而在开发机上又无法复现
  （本项目开发用的文件沙箱禁止创建命名管道，bash 根本跑不起来）。
  原生 PowerShell 的行为可以直接在开发机上验证。
- **CI 的结构校验从 shell 移到 Python**：新增 `tools/ci_smoke.py`。
  shell 的编码、引号与 BOM 行为在 PS 5.1、PS 7、bash 之间都不一致
  （`Out-File -Encoding utf8NoBOM` 只有 PS 7 支持，`-Encoding utf8`
  在 PS 5.1 下会写出 BOM；PS 5.1 默认按 OEM 代码页解码原生命令输出）。
  放进 Python 后编码、引号、路径、退出码全在一个可控环境里。
- **CI 的退出码检查依赖 shell 语义**：`python ... 2>&1 | Out-Null` 之后
  `$LASTEXITCODE` 在 CI 的 PowerShell 7 上不等于 2（本机 PS 5.1 上是 2）。
  根因未查明——GitHub 的 job 日志需要认证才能下载（403），而本机无法
  复现 PS 7。但依赖 shell 的退出码语义本身就是错的做法：PS 5.1 与 7 对
  "原生命令写 stderr"的处理不同，管道末尾接 cmdlet 后 `$LASTEXITCODE`
  的行为也不一致，bash 又是另一套规则。
  改用 `tools/ci_smoke.py --cli`，以 subprocess 取真实 `returncode`——
  操作系统层面的值，不经过任何 shell 解释，而且测的是**真正的入口点**
  （`python -m memscope`），不是进程内调用。

  > 定位手段值得一提：因为拿不到日志，把「编码韧性」检查**拆成 6 个
  > 细粒度步骤**，让失败的步骤名直接指出是哪一条命令挂了。
  > CI 随即报出"失败步骤: 编码韧性 6/6 · 非法参数退出码"，1~5 全过。

- **缺少厂商信息时分类错误**，以及若干 lint 与编码问题。

### 说明

- 运行时依赖数量：**0**
- 仅支持 Windows（归因与分析算法本身跨平台）
- 不包含任何修改系统状态的操作——这是设计，不是缺陷

[Unreleased]: https://github.com/Ray144165154/memscope/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Ray144165154/memscope/releases/tag/v0.1.0
