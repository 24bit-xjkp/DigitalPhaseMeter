# DPM PC 侧上位机（`host/`）

DigitalPhaseMeter 的 PC 侧 Python 上位机：实现 `docs/ps-protocol.md` 定义的 PS ↔ PC 串口协议，
提供命令行（CLI）、**PySide6 图形界面**（GUI），以及一个**可独立运行的寄存器级 + 测量级 PL 仿真后端**
（没有硬件时把整条上位机链路跑通）。

所有文档与代码注释均为中文。运行期依赖是 `pyserial` 与 **`PySide6`**（GUI），开发期用 `pytest`
（见仓库根 `pyproject.toml`）——环境由 `uv` 管理，直接 `uv run ...` 即可。

---

## 1. 目录与模块

```text
host/
├── ../pyproject.toml           # 仓库根：包定义（依赖只有 pyserial）+ pytest 配置（testpaths/pythonpath/markers）
├── README.md                   # 本文件（含验收的原始输出）
├── dpm_host/
│   ├── __init__.py             # 版本与包说明
│   ├── protocol.py             # 帧编解码 + CRC-16/MODBUS + 流式解析器 + 各命令 PAYLOAD 编解码
│   ├── client.py               # 串口客户端：单事务串行、超时/重试、读线程、通知帧回调
│   ├── mathx.py                # 定点换算（与 ps/include/dpm_calc.h 同一契约）
│   ├── sig.py                  # MMCM 求解 + 寄存器镜像编码（方案 A：Delays/Mux 拆分）+ 相位可控性 JSON
│   ├── sim_fpga.py             # 寄存器级 + 测量级 PL 模型（ControlRegs 语义 + 假 MMCM）
│   ├── sim_server.py           # 把 sim_fpga 接到 pty/串口上，模拟 PS 侧固件（--pty / --device）
│   ├── cli.py                  # `python3 -m dpm_host <子命令>`
│   ├── gui.py                  # PySide6 图形界面（工作线程 + Qt 信号回主线程刷新）
│   └── __main__.py             # `python3 -m dpm_host` 入口
└── tests/
    ├── conftest.py             # pytest 的 sys.path 兜底（正常由 pythonpath 生效，见第 2 节）
    ├── test_protocol.py        # 帧/CRC/解析器/PAYLOAD/编码空间（58 项）
    ├── test_mathx.py           # 定点换算 + 金标准向量逐位比对（95 项）
    ├── test_sig.py             # 冻结求解规则、方案 A 的 DRP 寄存器镜像、相位码与 45/D 网格、金标准相位字段（137 项）
    ├── test_sim.py             # 寄存器语义、测量模型 vs golden、假 MMCM/DRP 镜像、PS 固件逻辑、流通知（60 项）
    └── test_client_over_pty.py # pty 交互（sim_server 10 项 + 参数化 12 项 + 超时 4 项 + 外来对端通知分流 3 项 = 20 项）
└── tools/
    ├── interop_host_main.py    # 验收 4 的可复跑互通脚本（--spawn ./ps/build/host_main）
    └── gui_smoke.py            # GUI 冒烟 + 截图（自动起 pty 仿真后端，产出 gui.png 与步骤日志）
```

## 2. 安装与运行

```bash
# 依赖：Python ≥3.12（本机 3.14.8）+ pyserial 3.5 + PySide6（GUI）；由 uv 管理，别手工 pip install
uv run python -m dpm_host --help              # 从仓库根目录直接跑（包通过 .pth 指向 host/）
# 也可安装成命令：uv sync 之后 `uv run dpm-host --help`
```

**测试**（pytest 原生写法，由 `uv` 管理虚拟环境；pytest 配置在仓库根 `pyproject.toml`
的 `[tool.pytest.ini_options]`：`testpaths = ["host/tests"]`、`pythonpath = ["host"]`、
`addopts = "-ra --strict-markers"`，所以两种调用目录都行、不带参数即全量）：

```bash
cd /home/luo/DigitalPhaseMeter && uv run pytest -q          # 仓库根目录：全量
cd /home/luo/DigitalPhaseMeter/host && uv run pytest -q     # host 目录：同样全量
uv run pytest -q -m slow                                    # 只看标记用例（slow/pty 默认也执行）
uv run pytest --collect-only -q | tail -3                   # 用例数
```

## 3. 无硬件：`sim_server` + CLI

`sim_server` 把 PL 模型接到一条串口/pty 上，并模拟 PS 侧固件（命令分发、W1C 应答、
连续模式轮转、流通知）：

```bash
# 终端 A：起一个虚拟串口（会打印从设备路径）
cd host
python3 -m dpm_host.sim_server --pty
#   PTY_LINK=/tmp/xxx（--pty-link 时）
#   PTY_SLAVE=/dev/pts/2
#   PTY 从设备路径: /dev/pts/2

# 终端 B：把 CLI 连到那个 pty 上
python3 -m dpm_host --port /dev/pts/2 ping
python3 -m dpm_host --port /dev/pts/2 sig --freq 10e6 --phase 90
python3 -m dpm_host --port /dev/pts/2 start
python3 -m dpm_host --port /dev/pts/2 monitor -n 5
python3 -m dpm_host --port /dev/pts/2 gui --port /dev/pts/2
```

常用 `sim_server` 选项：

| 选项 | 作用 |
| --- | --- |
| `--pty` / `--device PATH` | 建一对 pty / 在已有串口或别人建好的 pty 上服务（`spec/validate_sweep.py` 用 `--device {device}`） |
| `--pty-link PATH` | 给 pty 从设备建一个固定路径的软链接（脚本里好引用） |
| `--freq HZ` `--phase DEG` `--gate N` | 片内源初始频率/相位与闸门目标 |
| `--mode single\|continuous` `--source ext\|int` | 初始模式与输入源 |
| `--phase-model auto\|controllable\|static` | 片内源相位模型，见第 6 节 |
| `--no-sig-enable` | 片内源不使能 → 两路恒低 → 测量**永不完成**（验证上位机超时退出） |
| `--ext-freq/--ext-phase/--ext-disable` | 外部输入的模拟信号（默认 10MHz/0°，`--ext-disable` = 没信号） |
| `--slow-hz/--dsp-hz/--gate/--meas-latency-ms/--config-delay-ms/--mmcm-lock-ms` | 模型参数 |
| `--verbose` | 把收发帧日志打到 stderr |

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
| `source ext\|int [--wait-ready]` | `SET_SOURCE` |
| `sig [--freq HZ] [--phase DEG] [--dry-run]` | `SET_SIG`/`GET_SIG`（先本地求解 MMCM 工作点并打印 DRP 字） |
| `gate N` | `SET_GATE` |
| `stream on\|off [-n N] [-d SEC]` | `STREAM`（on 时打印收到的 `STREAM_DATA`） |
| `clear` / `reset` | `CLEAR` / `RESET_COUNTERS` |
| `scan --freqs ... --phases ... --out CSV` | 多频点 × 多相位扫描 → CSV |
| `sigtable` | 离线：相位语义 + 冻结频点表 + 求解器自校验（不连串口） |
| `gui [--port ...]` | 启动 PySide6 界面 |

全局选项：`--port`（串口或 pty 路径）、`--baud 115200`、`--timeout`（单次请求超时）、
`--result-timeout`（等测量结果的总超时）、`--retries`、`--json`（机器可读输出）、`--debug`。

频率/相位参数写法：`--freq 25e6`、`--freq 25MHz`、`--freq 25000000`；`--phase 22.5`、`--phase 22.5deg`。

## 5. GUI（`python3 -m dpm_host gui`）

界面包含（任务书要求的全部元素）：

- **连接/断开**：串口路径、波特率、超时、连接状态（给出 `--port` 时启动后自动连接并 `IDENTIFY`）；
- **状态灯**：busy / MMCM 锁定 / 结果丢失 / sourceReady（绿=有效、橙=忙、红=丢失）；
- **频率显示**（大字号 + `freqHz` 原值与周期）与**相位差 0~360°**：数字 + 仪表盘弧（指针）+ 0~360° 条形；
- **测量控制**：单次测量、连续测量开关、CLEAR、复位统计、**闸门时间按秒设置**（输入框写 `40µs` 就填
  `0.00004`，下方实时显示等价的低速周期数与频率分辨率；下发时按低速时钟换算成 `SET_GATE` 的周期数）；
- **输入源单选**：外部引脚 / 片内信号源；
- **片内源**：频率与相位差输入 + 「设置」；扫描频点/相位列表 + 「扫描」；相位不可控时相位输入**置灰**并提示；
  **相位上界逐频点刷新**（>180° 的相位差是合法请求，上界 = `min(23017499/D, 359999)` 毫度：
  25MHz/D=40 → 359.999°，10MHz/D=100 → 230.174°，8MHz/D=125 → 184.139°），越界的请求会被界面先拦下
  并说明上界（否则 MMCM 的字段会截断回绕）；
- **结果表**（最近 200 条：序号/频率/相位差/gate/edge/phaseCnt/DSP 周期）+ **导出 CSV** + 清空表；
- **日志区**（带时间戳，含求解出的 VCO/MULT/D、量化提示、错误信息）。

线程模型：所有串口操作都在一个后台 `QThread` 里**串行**执行，结果通过 Qt 信号（跨线程队列连接）
回到界面线程刷新控件，因此界面不会因为请求超时而卡死，工作线程也不碰控件。PySide6 是运行期依赖
（`uv run` 自动装上），不需要额外系统包。

**冒烟 + 截图**（可复跑，产出真实窗口截图与逐步证据）：

```bash
uv run python host/tools/gui_smoke.py            # 自动起 pty 仿真后端 → 连接/测量/切源/表格
uv run python host/tools/gui_smoke.py --scan     # 再跑一次 2×3 扫描（含一个越界点）并导出 CSV
# 产物：build/gui_smoke/gui.png、gui_final.png、gui_stdout.txt、scan.csv
# 无显示环境（CI）加 QT_QPA_PLATFORM=offscreen 也能跑，截图用 QWidget.grab() 不依赖窗口系统
```

该脚本把「界面真的能用」变成可复现证据：连接、单次测量、22.5° 细调相位、**>180° 相位（25MHz/225°）**、
相位上界逐频点刷新（25MHz → 359.999°、8MHz → 184.139°）、**闸门按秒下发（40µs → 2000 个低速周期）**、
输入源切换、扫描（越界点被跳过）与 CSV 导出逐项断言，最后用 `QWidget.grab()` 直接抓窗口
（不依赖 `import`/`spectacle` 等截图工具）。界面换算的单元测试在 `host/tests/test_gui.py`。

## 6. 仿真后端模型：假设与不覆盖的东西

`sim_fpga.py` 分三块建模，语义分别对齐 RTL / 协议文档 / 实测结论：

1. **寄存器语义**（对齐 `src/main/scala/DigitalPhaseMeter/ControlRegs.scala` 与 `ps/include/dpm_regs.h`）：
   `CTRL.start` 写 1 触发（**busy 期间被忽略**）、`CTRL.clear` 在 busy 期间**挂起**、`STATUS` 四位、
   结果锁存（未读走即被覆盖 → `lostCount++`、`seq++`）、`IRQ_STATUS` 三位 **W1C**、
   `GATE_TARGET` 按字节选通合并、`SIG_CTRL` **同拍生效**（门控与选择都在主时钟域，
   与写寄存器的域相同，没有跨时钟域延迟；写完成后 `SIG_STATUS.sourceReady` 立即反映新配置）。
   片内源未使能/未锁定时两路恒低 → **测量永远不完成**（用来验证上位机能超时退出）。
2. **测量模型**（与 `spec/gen_golden.py` 的独立参考实现**逐位一致**）：

   ```text
   gateCnt   = targetGate + ceil(T_sig / T_slow)
   edgeCnt   = (gateCnt * f_sig) // f_slow
   dspCycles = RNE(T_phase / T_dsp)          # 相位先被 DSP 采样网格量化（整数 ps + 半数取偶）
   phaseCnt  = dspCycles * edgeCnt
   ```

   由此 `phaseCnt / edgeCnt` 恰好是整数个 DSP 周期（相位量化下界）。
   **不覆盖**：MMCM 抖动/相位噪声、真实布线延迟与边沿检测通路的固定延迟差、
   异步采样链的 CDC 亚稳态、闸门停止握手的额外低速周期（RTL 里 stop 要等 `sig2` 下一个上升沿，
   容限见 `.agents/skills/dpm-measurement-model`）。
3. **假 MMCM**（方案 A：MMCM 的 16 位寄存器镜像 + LOAD）：DRP 段在模型的平坦地址空间里是
   **`0x100 + off`**（模拟「另一段 AXI 基地址」，真实是 `0x43C1_0000`）：`0x100+0x00` ID、
   `0x100+0x04` STATUS（bit0 = locked、bit1/bit2 = busy）、`0x100+0x08..0x24` 八个 MMCM 寄存器镜像、
   `0x100+0x28` LOAD（写 1 整批生效并重新锁定）。相位按 `45/D` 网格量化后生效。
   相位由 `--phase-model` 决定：

   | 取值 | 行为 |
   | --- | --- |
   | `auto`（**默认**） | 跟随 `spec/drp_semantics.json` 的 `phase_controllable`（方案 A 实测为 **true**）：写相位字段即生效 |
   | `controllable` | 同 `auto` 的可控分支，强制「相位可写」 |
   | `static` | 复现旧 `clk_wiz_src` bitstream：CLKOUT1 的相位字段按 0 写，输出恒为静态 90°（用来验证上位机在「相位不可控」时的提示路径） |

## 7. 与 PS 侧的契约关系

- **帧与命令**：严格按 `docs/ps-protocol.md` 第 2、3 节；所有多字节字段小端；CRC-16/MODBUS
  计算范围 = `LEN + CMD + PAYLOAD`；错帧（CRC 错 / LEN 越界）丢弃并继续找下一个 SOF。
- **定点换算**：`mathx.py` 与 `ps/include/dpm_calc.h` 同一契约（整除截断），并用文档要求的
  **等价安全形式**避免 64 位溢出：

  ```text
  freqHz        = (edgeCnt * f_slow) // gateCnt
  c, r          = divmod(phaseCnt, gateCnt)
  v             = 360000*c + (360000*r) // gateCnt
  phaseMilliDeg = (v * (f_slow // 1000)) // (f_dsp // 1000)
  ```

  `freqMilliHz` 字段承载的是**整数 Hz**（25MHz → 25000000，名字是历史遗留），
  `phaseMilliDeg` 是毫度。
- **片内源（方案 A）**：`sig.py` 与 `ps/include/dpm_calc.h::dpm_sig_solve` /
  `dpm_regs.h::DPM_MMCM_ENCODE_CLKOUT` 同一实现——
  冻结求解规则（先最小化频率误差，再取距 BD 初始点 `(1000MHz, D=40)` 最近的解，
  `D ≤ 126`（分频编码成 `HIGH_TIME + LOW_TIME` 各 6 位）、`MULT = 12..24`、
  `VCO ∈ [600,1200]MHz`、无小数分频），写入顺序与 `docs/ps-protocol.md` §6.1 一致：

  ```text
  0x08/0x0C = CLKFBOUT   ClkReg1/ClkReg2           0x10/0x14 = CLKOUT0  （clk_out1，相位 0）
  0x18/0x1C = CLKOUT1    ClkReg1/ClkReg2           0x20 = DIVCLK = 0x1041
  0x24 = POWER = 0xFFFF                            0x28 = LOAD：写 1 整批生效
  ```

  相位不占独立寄存器：`m = round(phase_mdeg × D / 45000)`（9 位，单位 1/8 VCO 周期），
  `DELAY_TIME = m >> 3`（ClkReg2 低 6 位）、`PHASE_MUX = m & 7`（ClkReg1 位 [15:13]），
  可实现网格 `45/D` 度。`phaseField` 回包 = `DELAY_TIME`。

  `sig.py` 从 `spec/drp_semantics.json` 只读两个结论：`phase_controllable`（方案 A 为 true）
  与 `phase_static_deg_milli`（不可控时的固定相位）；`reload_semantics()` 可在结论更新后热加载。

## 8. 验收证据（实际运行的原始输出）

> 环境：Python 3.14.8、pyserial 3.5、tkinter、`DISPLAY=:0`、Linux；Python 依赖由 `uv` 管理（`.venv`）。
> 测试是 **pytest 原生**写法（模块级 `def test_*` + `assert` + `pytest.raises`/`parametrize`/fixture），
> 配置在仓库根 `pyproject.toml` 的 `[tool.pytest.ini_options]`，不再需要 `unittest`。

### 验收 1：全部单元/集成测试

```console
$ cd /home/luo/DigitalPhaseMeter && uv run pytest -q
[test_mathx] 金标准向量来源：/home/luo/DigitalPhaseMeter/spec/golden/vectors.json（79 组）
667 passed in 5.65s

$ cd /home/luo/DigitalPhaseMeter/host && uv run pytest -q
[test_mathx] 金标准向量来源：/home/luo/DigitalPhaseMeter/spec/golden/vectors.json（79 组）
667 passed in 5.65s
```

（667 项 = 协议 58 + mathx 248 + pty 端到端 29 + sig 221 + sim 111；两种调用目录都在仓库根
`pyproject.toml` 下收集到同一批用例（`rootdir: /home/luo/DigitalPhaseMeter`，`configfile: pyproject.toml`）。
五个测试文件都是 pytest 原生写法，`unittest` 的 `subTest` 已全部换成 `parametrize` 展开的独立用例。
金标准向量从 28 条扩到 **79 条**（10 频点 × 逐频点相位点，含 31 个 ≥180° 的点），
`test_mathx` / `test_sim` 因此大幅增加——这是扩表的直接结果，见 `docs/verify-report.md` §11。
D=127 的 `xfail` 已随 `DIV_MAX` 收到 126 变成普通断言，不再有预期失败项。）

`test_mathx.py` 与 `spec/golden/vectors.json` **79/79 逐位一致**
（`freqMilliHz` 与 `phaseMilliDeg` 都比对），另有 500 组随机用例验证「安全形式 == 朴素公式」逐位相同。

Lead 的独立自检同样通过：

```console
$ python3 spec/selftest_spec.py
[1] 金标准向量可复现
[2] 金标准向量自洽
[2b] 溢出安全形式 == 朴素公式（随机抽样，逐位一致）
[3] C 头文件：寄存器与 DRP 编码
[4] C 头文件命令码 vs 协议文档
[5] 协议文档冻结频点 vs gen_golden.py

共 686 项检查，失败 0 项
EXIT=0
```

### 验收 2：`sim_server --pty` + `scan`

```console
$ python3 -m dpm_host.sim_server --pty --freq 25e6 --phase 90e3 --gate 2000
启动仿真服务端：slow=50MHz dsp=400MHz gate=2000 f=25MHz phase=90000°
PTY_SLAVE=/dev/pts/1
PTY 从设备路径: /dev/pts/1

$ python3 -m dpm_host --port /dev/pts/1 scan --freqs 5e6,6.25e6,8e6,10e6,12.5e6,20e6,25e6 \
      --phases 0,22.5,45,90,135,180,225,270,315 --out /tmp/scan.csv
扫描计划：7 个频点 × 9 个相位点 = 63 行
相位可控性：是，来源 /home/luo/DigitalPhaseMeter/spec/drp_semantics.json
     5.0000 MHz → VCO=600.0MHz MULT=12 DIVCLK=1 D=120 → f_out=5.000000MHz，相位码 0（DELAY_TIME=0、PHASE_MUX=0，实际 0.000°）
     6.2500 MHz → VCO=750.0MHz MULT=15 DIVCLK=1 D=120 → f_out=6.250000MHz，相位码 0（DELAY_TIME=0、PHASE_MUX=0，实际 0.000°）
     8.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=125 → f_out=8.000000MHz，相位码 0（DELAY_TIME=0、PHASE_MUX=0，实际 0.000°）
    10.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=100 → f_out=10.000000MHz，相位码 0（DELAY_TIME=0、PHASE_MUX=0，实际 0.000°）
    12.5000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=80 → f_out=12.500000MHz，相位码 0（DELAY_TIME=0、PHASE_MUX=0，实际 0.000°）
    20.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=50 → f_out=20.000000MHz，相位码 0（DELAY_TIME=0、PHASE_MUX=0，实际 0.000°）
    25.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=40 → f_out=25.000000MHz，相位码 0（DELAY_TIME=0、PHASE_MUX=0，实际 0.000°）
  其中 12 个 (频点, 相位) 超出相位码上界（`m ≤ 511`），会被跳过：5MHz/225°、5MHz/270°、5MHz/315°、6.25MHz/225°、6.25MHz/270°、6.25MHz/315°…
  实际可扫 51 行
[  1]    5.0000MHz     0.0° → f=   5.000000 MHz phase=    0.000° err=+0.0000° gate=2010 edge=201 phaseCnt=0 cycles=0.0000
[  6]    5.0000MHz   180.0° → f=   5.000000 MHz phase=  180.000° err=+0.0000° gate=2010 edge=201 phaseCnt=8040 cycles=40.0000
[scan] 跳过 5MHz/225°：SET_SIG 回 ERR_PARAM（相位超出该频点的 9 位相位码上界）
...
[ 49]   25.0000MHz   225.0° → f=  25.000000 MHz phase=  225.000° err=+0.0000° gate=2002 edge=1001 phaseCnt=10010 cycles=10.0000
[ 51]   25.0000MHz   315.0° → f=  25.000000 MHz phase=  315.000° err=+0.0000° gate=2002 edge=1001 phaseCnt=14014 cycles=14.0000

CSV 前几行（freq_nominal,phase_nominal,f_meas_mHz,phase_meas_mdeg,gateCnt,edgeCnt,phaseCnt,phaseCycles,err_deg,phase_controllable）：
  5000000,0.0,5000000,0,2010,201,0,0.0,0.0,True
  5000000,22.5,5000000,22500,2010,201,1005,5.0,0.0,True
  5000000,45.0,5000000,45000,2010,201,2010,10.0,0.0,True
  5000000,90.0,5000000,90000,2010,201,4020,20.0,0.0,True
  5000000,135.0,5000000,135000,2010,201,6030,30.0,0.0,True
  5000000,180.0,5000000,180000,2010,201,8040,40.0,0.0,True
  6250000,0.0,6250000,0,2008,251,0,0.0,0.0,True
  6250000,22.5,6250000,22500,2008,251,1004,4.0,0.0,True
  ...（共 51 行）
  25000000,270.0,25000000,270000,2002,1001,12012,12.000000,0.0000,True
  25000000,315.0,25000000,315000,2002,1001,14014,14.000000,0.0000,True

扫描完成：51 行，|err_deg| 最大 9.0540° 平均 1.0444°
CSV 已写入 /tmp/scan.csv
```

> 这是**方案 A（裸 MMCM + 极简 DRP 控制器）之后**的输出（旧 `clk_wiz_src` 时代相位不可控，
> 每行都是 `⚠相位未生效`，那批原始输出保留在 `docs/verify-report.md` §3/§6 与 git 历史里）。
> 扫描现在按 `45°/D` 网格与 DSP 采样网格两重量化判误差，`err_deg` 最大 9.0540°（20MHz 下
> 1 个 DSP 周期就是 18°）。

说明（很重要）：

- **频率与计数符合冻结模型**：例如 5MHz/180° → `gate=2010 edge=201 phaseCnt=8040`、25MHz/315° →
  `gate=2002 edge=1001 phaseCnt=14014`，与 `spec/golden/vectors.json` 的期望值逐位相同。
- **相位列反映两重量化**：先吸附到 MMCM 的 `45°/D` 网格，再被测量端的 DSP 采样网格
  （1 个周期 = `360·f_sig/400MHz`）量化，所以 `err_deg` 在高频点自然变大（20MHz 下 1 个
  DSP 周期就是 18°）；`cycles` 列即 `phaseCnt / edgeCnt`（整数个 DSP 周期）。
  CSV 前 9 列是验收要求的列（`f_meas_mHz` 的单位按 `docs/ps-protocol.md` 第 5 节字段单位表
  = **Hz**），第 10 列 `phase_controllable` 由协议 `IDENTIFY.flags.b4` 决定。
- **越界组合会被跳过并记录**：相位上界是 9 位相位码 `m ≤ 511`（按 D 逐点，D=125 只到
  184.139°），越界请求服务端回 `ERR_PARAM`，`scan()` 记一条 `[scan] 跳过 …` 后继续扫。
  仿真后端（`dpm_host/sim_server.py`）与固件（`ps/src/dpm_cmd.c`）用同一判据，
  因此仿真里不会出现"真机会拒绝但仿真接受"的点——这条一致性由
  `host/tests/test_client_over_pty.py` 的 `test_phase_code_overflow_is_rejected` /
  `test_scan_skips_unrepresentable_points` 钉住。

### 验收 3：GUI 冒烟测试

```console
$ DISPLAY=:0 timeout 8 python3 -m dpm_host gui --port /dev/pts/2
EXIT=124（124 = 被 timeout 杀掉，符合预期）
traceback 计数: 0

$ DISPLAY=:0 timeout 5 python3 -m dpm_host gui        # 不带 --port（离线启动）
EXIT=124（124 预期），traceback=0
```

同时（`sim_server --verbose`）能看到 GUI 真的连上并工作了：

```text
[sim] ← PING a5 01 01 c1 e0
[sim] ← IDENTIFY a5 01 02 81 e1
[sim] ← GET_STATUS a5 01 03 40 21
```

### 验收 4：与 C 侧服务端互通（`ps/build/host_main`）


`ps/sim/host_main`（ps-firmware 的 C 服务端，独立实现）在单连接下完整跑通：**PING / IDENTIFY /
SET_GATE / SET_SOURCE / SET_MODE / SET_SIG / START / GET_MEAS / monitor（连续模式）/ GET_RESULT /
错误响应**，`freqMilliHz` 与 `phaseMilliDeg` 的线上值与上位机本地换算**完全一致**，
`gateCnt/edgeCnt/phaseCnt` 也与 `spec/golden/vectors.json` 的期望值逐位相同：

```console
$ ./ps/build/host_main --pty          →   /dev/pts/5
$ python3 host/tools/interop_host_main.py --spawn ./ps/build/host_main   # 可复跑脚本
=== 验收 4：host 侧 Python 客户端 ↔ C 侧服务端 ps/build/host_main（单连接）===

# PING / IDENTIFY
PING ok；IDENTIFY: ver=0x00010000 versionReg=0x44504D02 caps=0x00000037 capsReg=0x00030104 dsp=400MHz slow=50MHz flags=0x0007

# SET_GATE / SET_SOURCE / SET_MODE
SET_GATE(2000) → 2000
SET_SOURCE(int) → sigStatus=0x0000000F
SET_MODE(single) → 0

# SET_SIG 25MHz/90° → START → GET_MEAS
  SET_SIG 回显: f=25MHz phase=90° VCO=1000MHz D=40 phaseField=2
  GET_MEAS(0.000s): seq=0 gate=2002 edge=1001 phaseCnt=4004 wire(freqHz=25000000, phase_mdeg=90000)
  本地换算: f=25.000000 MHz phase=90.000° phaseCnt/edgeCnt=4.0000 DSP周期 量化步长=22.500°

# SET_SIG 10MHz/90° → START → GET_MEAS
  SET_SIG 回显: f=10MHz phase=90° VCO=1000MHz D=100 phaseField=2
  GET_MEAS(0.000s): seq=1 gate=2005 edge=401 phaseCnt=4010 wire(freqHz=10000000, phase_mdeg=90000)
  本地换算: f=10.000000 MHz phase=90.000° phaseCnt/edgeCnt=10.0000 DSP周期 量化步长=9.000°

# SET_SIG 6.25MHz/90° → START → GET_MEAS
  SET_SIG 回显: f=6.25MHz phase=90° VCO=750MHz D=120 phaseField=2
  GET_MEAS(0.000s): seq=2 gate=2008 edge=251 phaseCnt=4016 wire(freqHz=6250000, phase_mdeg=90000)
  本地换算: f=6.250000 MHz phase=90.000° phaseCnt/edgeCnt=16.0000 DSP周期 量化步长=5.625°

# monitor 连续测量 3 次
  seq=3 f=6.250000 MHz phase=90.000° gate=2008 edge=251 phaseCnt=4016
  seq=4 f=6.250000 MHz phase=90.000° gate=2008 edge=251 phaseCnt=4016
  seq=5 f=6.250000 MHz phase=90.000° gate=2008 edge=251 phaseCnt=4016

# 错误路径：SET_MODE(7) → ERR_PARAM
  ✓ PS 返回错误响应 0xFE（ERR_PARAM），请求 0x08（SET_MODE）

统计: {'tx': 38, 'rx': 38, 'parseErrors': 0, 'timeouts': 0, 'retries': 0, 'unexpected': 0, 'notifications': 0}
```

**STREAM 通知**：C 侧服务端的模型时间是「每次 AXI 事务推进 64 个慢周期」，因此
`intervalMs` 的 1ms **模型时间**需要约 780 次事务才会到点——空等不会收到通知。
把 `intervalMs` 设为 1 并持续发请求（推进其模型时间）后，收到 3 条通知，字段与本地换算一致：

```console
STREAM(1,1) → (1, 1)
发了 224751 个 PING（推进模型时间），收到 STREAM_DATA 3 条
  seq=5575 lostΔ=0 wire(freqHz=25000000, phase_mdeg=90000) local(f=25.000000 MHz, phase=90.000°)
  seq=13083 lostΔ=0 wire(freqHz=25000000, phase_mdeg=90000) local(f=25.000000 MHz, phase=90.000°)
  seq=22610 lostΔ=0 wire(freqHz=25000000, phase_mdeg=90000) local(f=25.000000 MHz, phase=90.000°)
统计: {'tx': 224762, 'rx': 224762, 'parseErrors': 2396, 'timeouts': 3, 'retries': 3, 'unexpected': 0, 'notifications': 3}
```

（最后一行的 2396 次解析噪声/3 次重发是「20 秒内 22 万次请求」这种非真实负载下的重发-清缓冲产物；
正常交互（几十个请求）统计里 `parseErrors=0`、`timeouts=0`。）

**协议 v1.1（通知码 0xE1/0xE2）后的重跑**：

```console
$ python3 host/tools/interop_host_main.py --spawn ./ps/build/host_main
# PING/IDENTIFY/SET_GATE/SET_SOURCE/SET_MODE/SET_SIG/START/GET_MEAS/GET_RESULT/monitor/错误码 全部通过
统计: {'tx': 33, 'rx': 33, 'parseErrors': 0, 'timeouts': 0, 'retries': 0, 'unexpected': 0, 'notifications': 0}

$ python3 host/tools/interop_host_main.py --spawn ./ps/build/host_main --stream
STREAM 通知 0 条
统计: {'tx': 312489, 'rx': 312489, 'parseErrors': 3314, 'timeouts': 0, 'retries': 0, 'unexpected': 0, 'notifications': 0}
```

`--stream` 没收到通知是**对端二进制未重建**导致的：`ps/build/host_main` 的时间戳（02:07:46）
早于 `ps/include/dpm_proto.h`（02:08:29）与 `ps/src/dpm_cmd.c`（02:10:34）。
用客户端回调统计线上出现过的命令码（39 万个 PING、25 秒）得到：

```text
线上命令码统计: {'0x81': 390790, '0x88': 2, '0x89': 1, '0x8a': 1, '0x8d': 2, '0xff': 1}
通知帧: 0xE1 收到 0 条；0xE2 收到 0 条
```

`0x81` 的计数正是 PING 响应（每个 PING 一条），**该二进制一个通知帧都没发**（新码旧码都没有），
所以 C 侧的流模式需要重建后再复测（属于 ps/ 的写入范围，本任务不动）。
上位机侧的 0xE1/0xE2 分流已用**独立对端**验证（见下）；旧码帧即使出现也只会被丢弃并计数，
不会被误当成通知（严格性测试）。

**通知分流的独立验证**（不依赖 `sim_server`，也不依赖 C 侧）：`tests/test_client_over_pty.py`
的 `NotificationDispatchTests` 用裸 pty 搭一个「外来迷你对端」，它回 PING 响应并主动发
`0xE1 STREAM_DATA` + `0xE2 EVENT`：客户端把两条都当通知回调（`notifications=2`、`unexpected=0`）；
再伪造一个 26 字节的 `0x81` 帧，客户端**不会**把它当 `STREAM_DATA`（只按字长不符丢弃并计数）。

**互通时发现、已上报 Lead 的 C 侧差异**（上位机已做兼容，不影响验收）：

1. **`STATUS.resultValid` 与结果锁存的时序**：轮询 `GET_STATUS` 1209 次都读不到 `resultValid=1`，
   而 `GET_RESULT.valid` 最终为 1（C 侧模型每次 AXI 事务才推进 64 个慢周期，锁存点落在
   同一次 `GET_STATUS` 的多笔读之间）。上位机的 `wait_result/wait_meas` 现在以
   **`GET_RESULT.valid` 为权威探针**（协议里它就是这个用途），两种实现都成立。
2. **`START` 可能回 `ERR_BUSY`**：`docs/ps-protocol.md` 第 3 节说 CTRL.start 在 busy 期间
   被 PL 忽略，而 C 侧固件直接回 `ERR_BUSY`（源不可用/已在测量时）。上位机把 `ERR_BUSY`
   当「已经在测量中」处理（等价语义），并在日志里说明。
3. **`IDENTIFY` 字段**：C 侧 `caps=0x00000037`、`flags=0x0007`（无 b3 中断驱动），
   与 `ps/include/dpm_regs.h` 的 `0x00030104` / 四位全 1 不一致；`SET_SIG` 的
   `phaseField` 回 2（历史字段的 45° 步数），本实现固定 0（实测已否定该字段是相位字段）。
   这些字段上位机只做展示，不影响交互，但建议两侧统一（已上报）。
4. **`GET_RESULT` 等不到结果时曾回 `ERR_BUSY`（已修复，两侧）**：切到**没接信号**的外部输入源
   （或选中的片内源没有边沿）时 PL 一直停在等边沿（`STATUS.busy = 1`、结果永不锁存），
   早期 C 侧固件把「等不到结果」表达成 `ERR_BUSY`，上位机把它当致命错误，于是整段测量程序中止：
   `设备错误：PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）`。
   现在两侧都按 `docs/ps-protocol.md` §3.0 对齐：**固件回 `valid = 0`**（`GET_MEAS` 没有
   `valid` 字段，仍用 `ERR_BUSY`），**上位机在所有调用点把 `ERR_BUSY` 都当「还没有结果」**
   （`DpmClient.wait_meas`/`wait_result` 轮询到自己的超时再抛 `DpmTimeoutError`；CLI 的
   `status`/`meas` 与 GUI 的连续测量循环改用 `try_get_meas()`，不再因一条 `ERR_BUSY` 中止），
   并把「当前选的是哪个源、为什么等不到、怎么恢复」写进消息。
   回归用例：`host/tests/test_client_over_pty.py` 的 `BusyResultPeer` 系列（模拟回 `ERR_BUSY`
   的固件，含 CLI 调用点）、`host/tools/interop_host_main.py` 的
   「外部输入源（无信号容错路径）」段、`host/tools/gui_smoke.py` 的「外部无信号时单次测量」步。

### 验收补充：`sigtable`（离线自校验）

```console
$ python3 -m dpm_host sigtable
相位不可控（mode=none）：当前 bitstream 的相位字未接线，clk_out2 相位固定 90.000°；来源：teammate drp-verify（共享任务 task-3）
结论文件：/home/luo/DigitalPhaseMeter/spec/drp_semantics.json

        目标频率   VCO/MHz  MULT     D      分频字段      相位步长/ps              求解器
         5MHz       600    12   120       120       208.33               一致
      6.25MHz       750    15   120       120       166.67               一致
         8MHz      1000    20   125       125       125.00               一致
        10MHz      1000    20   100       100       125.00               一致
      12.5MHz      1000    20    80        80       125.00               一致
        20MHz      1000    20    50        50       125.00               一致
        25MHz      1000    20    40        40       125.00               一致

冻结频点表自校验：全部通过（求解器输出与表逐位一致）
EXIT=0
```

## 9. 协议问题记录（发现即上报，未擅自改文档）

1. **响应码与通知帧曾经撞码 → 协议 v1.1 已修正**：旧版通知码 0x81/0x82 与
   `PING`（0x01|0x80）/`IDENTIFY`（0x02|0x80）的响应完全撞码，只能靠 PAYLOAD 长度猜。
   现在**通知码改为 0xE1/0xE2**（`cmd & 0xF0 == 0xE0` 即通知），编码空间不再有交集：
   `0x01..0x0E` 请求、`0x81..0x8E` 响应、`0xE1..0xE2` 通知、`0xFE/0xFF` 错误。
   本实现按码分流（`protocol.is_notification_code`），并有严格性测试保证
   「26 字节的 0x81」不会被当成 `STREAM_DATA`（见验收 4）。
2. **`SET_SOURCE` 同时负责使能（已写入协议 §3.0）**：`source=1 ⇒ SIG_CTRL=enable|srcSelect`、
   `source=0 ⇒ SIG_CTRL=0`，不额外加 enable 命令。`sim_server.py` / `sim_fpga.py` 按此实现。
3. **相位字段的历史包袱**：早期文档的「分频字 bit[15:8] 是相位字段」与候选 A/B 的 8 位字段
   都已被 xsim 实测否定（见 `spec/drp_semantics.json`）。现在相位是**独立的 32 位毫度字**
   （`0x20C`/`0x218`），本实现的 `phaseField(u8)` 是历史字段、固定写 0，
   相位以相位字为准；网格是 `45/D` 度。
4. **`STATUS.resultValid` 不作为唯一判据（已写入协议 §3.0）**：结果就绪的权威探针是
   `GET_RESULT.valid`；`START` 在不能启动时回 `ERR_BUSY`（上位机当「已在测量中」处理）；
   `IDENTIFY` 的 `capsReg/versionReg` 是从 PL 读回的原始值、`caps` 是固件能力字、
   `flags.b3` 只在真中断实现时置位；相位不可控时 `SET_SIG` 的 `phaseField` 回 0。
   本实现全部按此对齐（见验收 4 的重跑）。
5. **`freqMilliHz` 字段名与单位不符**：字段名是历史遗留，**单位是整数 Hz**
   （毫赫兹在 25MHz 时是 2.5e10，会溢出 u32）。`protocol.Meas.freq_milli_hz` 保留字段名，
   文档与测试都以 Hz 为准；显示一律用 `gateCnt/edgeCnt/phaseCnt` 本地换算，不依赖对端口径。

## 10. 已知限制 / 未验证

- 未在真实 Zynq 板卡（`/dev/ttyUSB*` UART）上跑过：串口路径、8N1/115200 由 pyserial 保证，
  但 `docs/ps-protocol.md` 第 1 节提到的 BD 尚未使能 PS UART（`PCW_EN_UART0/1 = 0`）。
- 与 C 侧 `ps/sim/host_main` 的互通已验证（见验收 4），另有 4 处 C 侧差异已上报
  （第 9 节；第 4 处「`GET_RESULT` 无结果回 `ERR_BUSY`」已两侧修复：固件改回 `valid = 0`，
  上位机对两种表达都容错）；`STREAM` 通知在 C 侧需要持续请求推进其模型时间才会到点（见验收 4 说明）。
- `sim_fpga` 不含 MMCM 抖动/相位噪声、真实布线延迟差与 CDC 亚稳态（见第 6 节）；
  它的相位模型与真实 bitstream 的差异由 `--phase-model` 显式选择，默认跟随实测结论。
- `--phase-model controllable` 是**假设**（"补丁后相位字可用"），真实行为需重建 IP/BD 后实测。
- GUI 没有做视觉回归（无截图工具链），只有"启动无 traceback + 服务端日志可见命令流"的冒烟证据。
