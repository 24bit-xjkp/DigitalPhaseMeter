# DPM PC 侧上位机（`host/`）

DigitalPhaseMeter 的 PC 侧 Python 上位机：实现 [`docs/ps-protocol.md`](../docs/ps-protocol.md)
定义的 PS ↔ PC 串口协议，提供命令行（CLI）、PySide6 图形界面（GUI），以及一个可独立运行的
寄存器级 + 测量级 PL 仿真后端（没有硬件时把整条上位机链路跑通）。
工程已完成并通过实测验收（2026-10-19）。

运行期依赖 `pyserial` 与 `PySide6`，开发期依赖 `pytest` / `black` / `isort`（见仓库根
`pyproject.toml`），环境由 `uv` 管理，直接 `uv run ...` 即可。协议契约以
[`docs/ps-protocol.md`](../docs/ps-protocol.md) 为准；无硬件验证、pty 后端与超时口径的工具细节
见 [`.agents/skills/dpm-host-protocol`](../.agents/skills/dpm-host-protocol/SKILL.md)。

## 1. 目录与模块

```text
host/
├── dpm_host/
│   ├── protocol.py    帧编解码 + CRC-16/MODBUS + 流式解析器 + 各命令 PAYLOAD 编解码
│   ├── client.py      串口客户端：单事务串行、超时/重试、后台读线程、通知帧回调
│   ├── mathx.py       定点换算（与 ps/include/dpm_calc.h 同一契约）
│   ├── sig.py         MMCM 工作点求解 + 寄存器镜像编码（方案 A）+ 相位可控性读取
│   ├── sim_fpga.py    寄存器级 + 测量级 PL 模型（ControlRegs 语义 + 假 MMCM）
│   ├── sim_server.py  把 sim_fpga 接到 pty/串口上，模拟 PS 侧固件（--pty / --device）
│   ├── cli.py         `python3 -m dpm_host <子命令>` 入口（含 __main__.py）
│   └── gui.py         PySide6 图形界面（后台工作线程 + Qt 信号回主线程刷新）
├── tests/             pytest 用例（协议/换算/求解/模型/pty 端到端/GUI/长闸门等待）
└── tools/
    ├── interop_host_main.py  与 C 侧服务端（ps/build/host_main）的互通脚本
    └── gui_smoke.py          GUI 冒烟 + 截图（自动起 pty 仿真后端）
```

## 2. 安装与运行

```bash
uv run python -m dpm_host --help            # 仓库根目录直接跑（包已由 uv 装进 .venv）
uv run dpm-host --help                      # 同一入口的控制台脚本形式
uv run python -m dpm_host --port /dev/ttyUSB0 ping
```

测试是 pytest 原生写法，配置在仓库根 `pyproject.toml` 的 `[tool.pytest.ini_options]`
（`testpaths = ["host/tests"]`、`pythonpath = ["host"]`、`addopts = "-ra --strict-markers"`），
在仓库根或 `host/` 下执行都收集同一批用例；`slow` / `pty` 标记默认也执行：

```bash
uv run pytest -q                # 全量（当前 726 项通过）
uv run pytest -q -m slow        # 只跑 slow 标记（长耗时，默认也会执行）
```

格式检查覆盖整棵 Python 脚本树（行宽 140、isort profile = black，参数取自 `pyproject.toml`）：

```bash
uv run black --check host/ spec/ ps/       # 期望 exit 0（全部 left unchanged）
uv run isort --check-only host/ spec/ ps/  # 期望 exit 0（正常时会打印 Skipped N files）
```

## 3. 无硬件：`sim_server` + CLI

`sim_server` 把 PL 模型接到一条串口/pty 上，并模拟 PS 侧固件（命令分发、W1C 应答、连续模式轮转、
流通知）：

```bash
# 终端 A：起一条虚拟串口（打印从设备路径与固定软链接）
uv run python -m dpm_host.sim_server --pty --pty-link /tmp/dpm_pty

# 终端 B：CLI 连上去
uv run python -m dpm_host --port /tmp/dpm_pty ping
uv run python -m dpm_host --port /tmp/dpm_pty sig --freq 10e6 --phase 90
uv run python -m dpm_host --port /tmp/dpm_pty gate 2000
uv run python -m dpm_host --port /tmp/dpm_pty meas
uv run python -m dpm_host --port /tmp/dpm_pty monitor -n 3
uv run python -m dpm_host --port /tmp/dpm_pty scan --freqs 6e6,25e6 --phases 0,45,225 --out /tmp/scan.csv
uv run python -m dpm_host --port /tmp/dpm_pty gui
```

常用 `sim_server` 选项：

| 选项 | 作用 |
| --- | --- |
| `--pty` / `--device PATH` | 建一对 pty / 在已有串口或别人建好的 pty 上服务 |
| `--pty-link PATH` | 给 pty 从设备建一个固定路径的软链接（脚本里好引用） |
| `--freq HZ` `--phase DEG` `--gate N` | 片内源初始频率/相位与初始闸门目标 |
| `--mode single\|continuous` `--source ext\|int` | 初始测量模式与输入源 |
| `--phase-model auto\|controllable\|static` | 片内源相位模型，见第 7 节 |
| `--no-sig-enable` / `--ext-disable` | 源恒低 → 测量永不完成（验证上位机超时退出） |
| `--ext-freq` `--ext-phase` | 外部输入的模拟信号（默认 10MHz/0°） |
| `--slow-hz` `--dsp-hz` | 模型时钟（默认 50MHz / 450MHz） |
| `--meas-latency-ms` `--config-delay-ms` `--mmcm-lock-ms` | 模型时序参数（锁存延迟、配置送达、重锁定） |
| `--run-for SEC` `--verbose` `--quiet` | 定时退出、把收发帧日志打到 stderr、静默启动信息 |

## 4. CLI 子命令

| 子命令 | 对应协议/功能 |
| --- | --- |
| `ping` | `PING`（连通性 + 往返时间 + 收发统计） |
| `identify` | `IDENTIFY`（版本、caps、两侧时钟、flags） |
| `status` | `GET_STATUS`（状态位 + `SIG_STATUS` 解读） |
| `start [--no-wait]` | `START`（默认等结果并打印） |
| `meas [--expect-deg X] [--no-start]` | `GET_MEAS`（无结果时自动测一次；`phaseCnt/edgeCnt` 主判据 + 期望值判据） |
| `monitor [-n N] [-d SEC] [--stream] [--csv PATH]` | 连续测量（轮询或流模式通知） |
| `mode single\|continuous` | `SET_MODE` |
| `source ext\|int [--wait-ready]` | `SET_SOURCE`（同时负责片内源使能） |
| `sig [--freq HZ] [--phase DEG] [--dry-run]` | `SET_SIG`/`GET_SIG`（先本地求解 MMCM 工作点并打印 DRP 字） |
| `gate N` | `SET_GATE`（同时打印闸门时间与等结果预算） |
| `stream on\|off [-n N] [-d SEC]` | `STREAM`（on 时打印收到的 `STREAM_DATA`） |
| `clear` / `reset` | `CLEAR` / `RESET_COUNTERS` |
| `scan --freqs ... --phases ... --out CSV` | 多频点 × 多相位扫描 → CSV |
| `sigtable` | 离线：相位语义 + 冻结频点表 + 求解器自校验（不连串口） |
| `gui [--port ...]` | 启动 PySide6 界面 |

全局选项：`--port`（串口或 pty 路径）、`--baud`（默认 115200）、`--timeout`（单次请求超时）、
`--result-timeout`（等测量结果的总超时）、`--retries`、`--slow-hz`/`--dsp-hz`（`IDENTIFY` 会覆盖）、
`--json`（机器可读输出）、`--debug`。频率/相位写法：`--freq 25e6|25MHz|25000000`、`--phase 22.5|22.5deg`。

## 5. 等待预算：长闸门不误报超时

一次测量的时长就是闸门窗口 `T_gate = gateTarget / f_slow`（协议第 5 节），而 RTL 的闸门计数器
是 28 位（50MHz 下最长 ≈5.37s）。因此 `--timeout` / `--result-timeout` 都只是**下限**，实际预算
不小于 `2 × T_gate + 0.25s`（`DpmClient.gate_wait_budget()`；闸门目标由 `GET_STATUS.gateTarget` /
`SET_GATE` 回显自动缓存）。默认闸门 2000 周期 = 40µs 时预算 ≈0.25s。

- 单次请求超时只当「这一拍没问出结果」，继续轮询到总预算为止；
- 「结果就绪」的权威探针是 `GET_RESULT.valid`（`GET_MEAS` 没有 `valid` 字段，用序号变化判断）；
- `ERR_BUSY` 一律当「还没有结果 / 已在测量中」处理，不当致命错误；调用方要区分时用
  `DpmClient.try_get_meas()` / `accept_busy=True`。

回归入口：`tests/test_gate_timeout.py`（纯单元：预算换算、闸门缓存、超时诊断）与
`tests/test_client_over_pty.py` 的长闸门用例（pty 对端 + `sim_server --gate 120000000` 的 2.4s
端到端测量）。

## 6. GUI（`uv run python -m dpm_host gui`）

界面包含：

- **连接/断开**：串口路径、波特率、超时、连接状态（给出 `--port` 时启动后自动连接并 `IDENTIFY`）；
- **状态灯**：busy / MMCM 锁定 / 结果丢失 / sourceReady（绿=有效、橙=忙、红=丢失）；
- **频率显示**与**相位差 0~360°**：数字 + 仪表盘弧 + 0~360° 条形；
- **测量控制**：单次测量、连续测量开关、CLEAR、复位统计、闸门时间按秒设置（实时显示等价的低速
  周期数、频率分辨率与等结果等待预算；上限是 28 位计数器的真实上界 ≈5.37s @50MHz）；
- **输入源单选**：外部引脚 / 片内信号源；
- **片内源**：频率与相位差输入 + 「设置」；扫描频点/相位列表 + 「扫描」；相位不可控时相位输入置灰；
  **相位上界逐频点刷新**（上界 = `min(23017499/D, 359999)` 毫度），越界请求由界面先拦下并说明上界；
- **结果表**（最近 200 条：序号/频率/相位差/gate/edge/phaseCnt/DSP 周期）+ **导出 CSV** + 清空表；
- **日志区**（带时间戳，含求解出的 VCO/MULT/D、量化提示、错误信息）。

线程模型：所有串口操作都在一个后台 `QThread` 里**串行**执行，结果通过 Qt 信号（跨线程队列连接）
回到界面线程刷新控件，因此请求超时不会卡死界面，工作线程也不碰控件。

冒烟 + 截图（可复跑）：

```bash
uv run python host/tools/gui_smoke.py            # 自动起 pty 仿真后端 → 连接/测量/切源/表格
uv run python host/tools/gui_smoke.py --scan     # 再跑一次 2×3 扫描并导出 CSV
# 产物在 build/gui_smoke/（gui.png 等）；无显示环境加 QT_QPA_PLATFORM=offscreen 也能跑
```

界面换算规则的单元测试在 `tests/test_gui.py`。

## 7. 仿真后端模型：假设与不覆盖

`sim_fpga.py` 分三块建模，语义分别对齐 RTL、协议文档与当前实现：

1. **寄存器语义**（对齐 `src/main/scala/DigitalPhaseMeter/ControlRegs.scala` 与
   `ps/include/dpm_regs.h`）：`CTRL.start` 写 1 触发（busy 期间被忽略）、`CTRL.clear` 在 busy 期间
   挂起、`STATUS` 四位、结果未读走即被覆盖时 `lostCount++` / `seq++`、`IRQ_STATUS` 三位 W1C、
   `GATE_TARGET` 按字节选通合并、`SIG_CTRL` 同拍生效（门控与选择都在主时钟域，与写寄存器的域相同，
   写完成后 `SIG_STATUS.sourceReady` 立即反映新配置）。片内源未使能/未锁定时两路恒低 →
   **测量永远不完成**（用来验证上位机能超时退出）。
2. **测量模型**（与 `spec/gen_golden.py` 的独立参考实现逐位一致）：

   ```text
   gateCnt   = targetGate + ceil(T_sig / T_slow)
   edgeCnt   = (gateCnt * f_sig) // f_slow
   dspCycles = RNE(T_phase / T_dsp)      # 相位先被 DSP 采样网格量化（有理数精确运算 + 半数取偶）
   phaseCnt  = dspCycles * edgeCnt
   ```

   由此 `phaseCnt / edgeCnt` 恰好是整数个 DSP 周期（相位量化下界）。**不覆盖**：MMCM 抖动/相位噪声、
   真实布线延迟与边沿检测通路的固定延迟差、异步采样链的 CDC 亚稳态、闸门停止握手的额外低速周期
   （容限见 [`.agents/skills/dpm-measurement-model`](../.agents/skills/dpm-measurement-model/SKILL.md)）。
3. **假 MMCM**（方案 A：MMCM 的 16 位寄存器镜像 + LOAD）：DRP 段在模型的平坦地址空间里是
   **`0x100 + off`**（模拟「另一段 AXI 基地址」，真实是 `0x43C1_0000`）：`0x100+0x00` ID、
   `0x100+0x04` STATUS（bit0 = locked、bit1/bit2 = busy）、`0x100+0x08..0x24` 八个 MMCM 寄存器镜像、
   `0x100+0x28` LOAD（写 1 整批生效并重新锁定）。相位按 `45/D` 网格量化后生效。
   相位模型由 `--phase-model` 决定：

   | 取值 | 行为 |
   | --- | --- |
   | `auto`（默认） | 跟随 `spec/drp_semantics.json` 的 `phase_controllable`（当前为 true）：写 MMCM 相位寄存器镜像即生效 |
   | `controllable` | 强制「相位可写」（上位机相位通路自测） |
   | `static` | 模拟「相位不可控」的行为（输出恒为固定相位，用于验证提示路径） |

## 8. 与 PS 侧的契约

- **帧与命令**：严格按 `docs/ps-protocol.md` 第 2、3 节；所有多字节字段小端；CRC-16/MODBUS
  计算范围 = `LEN + CMD + PAYLOAD`；错帧（CRC 错 / LEN 越界）丢弃并继续找下一个 SOF。
- **定点换算**：`mathx.py` 与 `ps/include/dpm_calc.h` 同一契约（整除截断），用等价安全形式避免
  64 位溢出：

  ```text
  freqHz        = (edgeCnt * f_slow) // gateCnt
  c, r          = divmod(phaseCnt, gateCnt)
  v             = 360000*c + (360000*r) // gateCnt
  phaseMilliDeg = (v * (f_slow // 1000)) // (f_dsp // 1000)
  ```

  `freqMilliHz` 字段承载的是**整数 Hz**（25MHz → 25000000，名字是历史遗留），`phaseMilliDeg` 是毫度。
- **片内源（方案 A）**：`sig.py` 与 `ps/include/dpm_regs.h` 的 `DPM_MMCM_ENCODE_CLKOUT` 同一实现——
  冻结求解规则（先最小化频率误差，再取距 BD 初始点 `(1000MHz, D=40)` 最近的解，`D ≤ 126`
  （分频编码成 `HIGH_TIME + LOW_TIME` 各 6 位）、`MULT = 12..24`、`VCO ∈ [600,1200]MHz`、无小数分频），
  寄存器镜像写入顺序与协议 §6.1 一致：

  ```text
  0x08/0x0C = CLKFBOUT   ClkReg1/ClkReg2      0x10/0x14 = CLKOUT0（clk_out1，相位 0）
  0x18/0x1C = CLKOUT1    ClkReg1/ClkReg2      0x20 = DIVCLK = 0x1041
  0x24 = POWER = 0xFFFF                       0x28 = LOAD：写 1 整批生效
  ```

  相位不占独立寄存器：`m = round(phase_mdeg × D / 45000)`（9 位，单位 1/8 VCO 周期），
  `DELAY_TIME = m >> 3`（ClkReg2 低 6 位）、`PHASE_MUX = m & 7`（ClkReg1 位 [15:13]），
  可实现网格 `45/D` 度。`SET_SIG` 回包的 `phaseField` = `DELAY_TIME`。请求相位超出
  `min(23017499/D, 359999)` 毫度（9 位相位码放不下）时固件回 `ERR_PARAM`，`scan()` 跳过并记日志。
- **协议要点**（细节见协议 §3.0）：通知帧是 `0xE1`（`STREAM_DATA`）/ `0xE2`（`EVENT`），
  响应是 `请求码 | 0x80`，错误 `0xFE/0xFF`；`SET_SOURCE` 同时负责使能；`START` 在不能启动时回
  `ERR_BUSY`（当「已在测量中」处理）；「暂无结果」用 `GET_RESULT.valid = 0` 表达（`GET_MEAS` 无
  `valid` 字段，例外用 `ERR_BUSY`）；`IDENTIFY` 的 `capsReg` / `versionReg` 是从 PL 读回的原始值，
  `flags` 的 b3 只在固件实现中断路径时置位、b4 表示片内源相位是否可控（当前为 1）。
- `sig.py` 从 `spec/drp_semantics.json` 只读两个结论：`phase_controllable` 与
  `phase_static_deg_milli`（相位不可控时的固定相位）；`reload_semantics()` 可在结论更新后热加载。

## 9. 已知限制

- 仿真后端不建模 MMCM 抖动/相位噪声、真实布线延迟差与 CDC 亚稳态（见第 7 节）；
  `--phase-model static` 只是「相位不可控」的模拟，不代表当前硬件。
- C 侧验证台（`ps/build/host_main`）的模型时间由 AXI 事务推进，需要持续请求才会到达
  `intervalMs`；这是验证台的简化，不是协议行为。
- GUI 不做视觉回归，只有 `gui_smoke.py` 的可复跑冒烟（截图 + 逐步断言）。

## 10. 回归入口

工程已完成并通过实测验收（2026-10-19）；以下入口当前有效、可复跑：

| 入口 | 覆盖 |
| --- | --- |
| `uv run pytest -q` | 上位机与协议全量单测/pty 端到端（当前 726 项通过） |
| `uv run black --check host/ spec/ ps/`、`uv run isort --check-only host/ spec/ ps/` | 脚本树格式与导入顺序 |
| `uv run python spec/selftest_spec.py` | 文档 ↔ C 头文件 ↔ 金标准三方一致性（当前 0 失败） |
| `uv run python host/tools/gui_smoke.py [--scan]` | GUI 冒烟 + 截图 |
| `uv run python host/tools/interop_host_main.py --spawn ./ps/build/host_main` | 与 C 侧服务端（同一份 `dpm_cmd.c`）互通 |
| `uv run python spec/validate_sweep.py --server "./ps/build/host_main --device {device}"` | 独立参考实现 80 点扫描逐点判定 |
| `bash ps/tools/onboard_accept_sim.sh` | 离线验收演练（C 验证台经 pty，80 点扫描） |
