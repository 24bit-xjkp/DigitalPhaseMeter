# DPM PC 侧上位机（`host/`）

DigitalPhaseMeter 的 PC 侧 Python 上位机：实现 `docs/ps-protocol.md` 定义的 PS ↔ PC 串口协议，
提供命令行（CLI）、Tk 图形界面（GUI），以及一个**可独立运行的寄存器级 + 测量级 PL 仿真后端**
（没有硬件时把整条上位机链路跑通）。

所有文档与代码注释均为中文。**本目录只依赖 Python 标准库 + `pyserial`**（GUI 用标准库 `tkinter`）。

---

## 1. 目录与模块

```text
host/
├── pyproject.toml              # 包定义（依赖只有 pyserial；测试只用标准库 unittest）
├── README.md                   # 本文件（含验收的原始输出）
├── dpm_host/
│   ├── __init__.py             # 版本与包说明
│   ├── protocol.py             # 帧编解码 + CRC-16/MODBUS + 流式解析器 + 各命令 PAYLOAD 编解码
│   ├── client.py               # 串口客户端：单事务串行、超时/重试、读线程、通知帧回调
│   ├── mathx.py                # 定点换算（与 ps/include/dpm_calc.h 同一契约）
│   ├── sig.py                  # MMCM 求解 + DRP 字（含独立毫度相位字）+ 相位语义 JSON
│   ├── sim_fpga.py             # 寄存器级 + 测量级 PL 模型（ControlRegs 语义 + 假 MMCM）
│   ├── sim_server.py           # 把 sim_fpga 接到 pty/串口上，模拟 PS 侧固件（--pty / --device）
│   ├── cli.py                  # `python3 -m dpm_host <子命令>`
│   ├── gui.py                  # Tk 图形界面
│   └── __main__.py             # `python3 -m dpm_host` 入口
└── tests/
    ├── conftest.py             # pytest 的 sys.path 兜底（本机没有 pytest，见下）
    ├── test_protocol.py        # 帧/CRC/解析器/PAYLOAD/编码空间（32 项）
    ├── test_mathx.py           # 定点换算 + 金标准向量逐位比对（14 项）
    ├── test_sig.py             # 冻结求解规则、DRP 字、相位字语义、45/D 网格（25 项）
    ├── test_sim.py             # 寄存器语义、测量模型 vs golden、PS 固件逻辑、流通知（31 项）
    └── test_client_over_pty.py # pty 交互（sim_server 15 项 + 外来对端通知分流 3 项 = 18 项）
└── tools/
    └── interop_host_main.py    # 验收 4 的可复跑互通脚本（--spawn ./ps/build/host_main）
```

## 2. 安装与运行

```bash
# 依赖：Python ≥3.10（本机 3.14.8）+ pyserial 3.5；GUI 需要 tkinter 与 DISPLAY
cd host
python3 -m dpm_host --help                    # 无需安装即可直接用（包就在当前目录）
# 可选：pip install -e .                      # 之后可在任意目录用 `dpm-host` 或 `python3 -m dpm_host`
```

**测试**（本机**没有安装 pytest**，`python3 -m pytest` 不存在，因此用标准库 unittest；
测试写法同时兼容 pytest 收集，装了 pytest 后 `python3 -m pytest host/tests -q` 也可）：

```bash
cd host && python3 -m unittest discover -s tests -t . -v
# 或在仓库根目录：python3 -m unittest discover -s host/tests -t host -v
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
| `gui [--port ...]` | 启动 Tk 界面 |

全局选项：`--port`（串口或 pty 路径）、`--baud 115200`、`--timeout`（单次请求超时）、
`--result-timeout`（等测量结果的总超时）、`--retries`、`--json`（机器可读输出）、`--debug`。

频率/相位参数写法：`--freq 25e6`、`--freq 25MHz`、`--freq 25000000`；`--phase 22.5`、`--phase 22.5deg`。

## 5. GUI（`python3 -m dpm_host gui`）

界面包含（任务书要求的全部元素）：

- **连接/断开**：串口路径、波特率、超时、连接状态（给出 `--port` 时启动后自动连接并 `IDENTIFY`）；
- **状态灯**：busy / MMCM 锁定 / 结果丢失 / sourceReady（绿=有效、橙=忙、红=丢失）；
- **频率显示**（大字号 + `freqHz` 原值与周期）与**相位差 0~360°**：数字 + 仪表盘弧（指针）+ 0~360° 条形；
- **测量控制**：单次测量、连续测量开关、CLEAR、复位统计、闸门目标设置；
- **输入源单选**：外部引脚 / 片内信号源；
- **片内源**：频率与相位差输入 + 「设置」；扫描频点/相位列表 + 「扫描」；相位不可控时相位输入**置灰**并提示；
- **结果表**（最近 200 条：序号/频率/相位差/gate/edge/phaseCnt/DSP 周期）+ **导出 CSV** + 清空表；
- **日志区**（带时间戳，含求解出的 VCO/MULT/D、量化提示、错误信息）。

线程模型：所有串口操作在后台工作线程里执行，结果经队列回到 Tk 主线程刷新界面，
因此界面不会因为请求超时而卡死。

**截图说明**：本环境（`DISPLAY=:0`）可以真实启动，但没有截图工具链，因此用**冒烟测试**代替：
进程被 `timeout` 杀掉之前**没有任何 traceback**，且仿真服务端日志能看到 GUI 自动发出的
`PING → IDENTIFY → GET_STATUS`，说明连接、握手与状态刷新都真实工作了（原始输出见第 8 节验收 3）。

## 6. 仿真后端模型：假设与不覆盖的东西

`sim_fpga.py` 分三块建模，语义分别对齐 RTL / 协议文档 / 实测结论：

1. **寄存器语义**（对齐 `src/main/scala/DigitalPhaseMeter/ControlRegs.scala` 与 `ps/include/dpm_regs.h`）：
   `CTRL.start` 写 1 触发（**busy 期间被忽略**）、`CTRL.clear` 在 busy 期间**挂起**、`STATUS` 四位、
   结果锁存（未读走即被覆盖 → `lostCount++`、`seq++`）、`IRQ_STATUS` 三位 **W1C**、
   `GATE_TARGET` 按字节选通合并、`SIG_CTRL` 生效前有 `configPending` 延迟，
   这段时间读 `SIG_STATUS.sourceReady` 为 **0**，之后才置 1。
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
3. **假 MMCM**（按实测结论 `spec/drp_semantics.json`）：低字节 = 分频比 `D` 本体、
   `LOAD(0x25C)` 的 bit0 = 触发 / bit1 = SADDR（0 = BD 静态配置，1 = 寄存器文件）、
   `locked` 在 **0x004**（bit0），重配置期间先掉后起；`0x27C` 恒为 0（保留对照）。
   相位由 `--phase-model` 决定：

   | 取值 | 行为 |
   | --- | --- |
   | `auto`（**默认**） | 跟随 `spec/drp_semantics.json` 的 `phase_controllable`——实测为 **false**，因此**相位字写进去也不生效，clk_out2 恒为静态 90°**，与真实 bitstream 一致 |
   | `static` | 同上，强制固定静态相位 |
   | `controllable` | 假装「打好 `PHASE_DUTY_CONFIG` 补丁、相位字 0x218 已接线」：按 `45/D` 度网格量化后生效，专门用于**上位机相位通路自测**（`spec/validate_sweep.py` 的相位判据也可用它跑） |

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
- **片内源**：`sig.py` 与 `ps/include/dpm_calc.h::dpm_sig_solve` / `dpm_regs.h` 同一实现——
  冻结求解规则（先最小化频率误差，再取距 BD 初始点 `(1000MHz, D=40)` 最近的解，
  `D ≤ 127`、`MULT = 12..24`、`VCO ∈ [600,1200]MHz`、无小数分频），
  DRP 写入顺序与 `docs/ps-protocol.md` 第 6.4 节一致：

  ```text
  0x200 = (MULT << 8) | DIVCLK      0x208 = D（clk_out1）      0x214 = D（clk_out2）
  0x20C = 0（clk_out1 相位）        0x218 = phase_mdeg（clk_out2 相位，毫度）
  0x25C = 0x07（触发 + SADDR=1）    状态：0x004 bit0 = locked
  ```

  相位字段的**单位**不写死：`sig.py` 从 `spec/drp_semantics.json` 读
  （`phase_unit_deg_milli` → `phase_unit_ps_per_lsb` → `phase_field_is_degrees` → 回退候选 B `45000`），
  并提供 `reload_semantics()` 在实测结论更新后热加载。

## 8. 验收证据（实际运行的原始输出）

> 环境：Python 3.14.8、pyserial 3.5、tkinter、`DISPLAY=:0`、Linux。
> **本机没有 pytest**（`python3 -m pytest` → `No module named pytest`），所以用标准库 unittest；
> 测试文件同时兼容 pytest 收集（`host/tests/conftest.py` 负责 `sys.path`）。

### 验收 1：全部单元/集成测试

```console
$ cd host && python3 -m unittest discover -s tests -t . -v
...
Ran 120 tests in 3.336s

OK

[test_mathx] 金标准向量来源：/home/luo/DigitalPhaseMeter/spec/golden/vectors.json（28 组）
```

（120 = 协议 32 + mathx 14 + sig 25 + sim 31 + pty 端到端 18；略去的 `-v` 明细是每项 `ok`。
这是**协议 v1.1（通知码 0xE1/0xE2）之后**的重跑结果。）
`python3 -m unittest discover -s host/tests -t host` 在仓库根目录同样 `OK`。）

`test_mathx.py` 与 Lead 重新生成的 `spec/golden/vectors.json` **28/28 逐位一致**
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
$ python3 -m dpm_host.sim_server --pty
启动仿真服务端：slow=50MHz dsp=400MHz gate=2000 f=25MHz phase=90°
PTY_LINK=/tmp/dpm_sim_pty
PTY_SLAVE=/dev/pts/2
PTY 从设备路径: /dev/pts/2

$ python3 -m dpm_host --port /dev/pts/2 scan --freqs 5e6,6.25e6,8e6,10e6,12.5e6,20e6,25e6 \
      --phases 0,22.5,45,67.5,90,112.5,135,157.5 --out /tmp/scan.csv
扫描计划：7 个频点 × 8 个相位点 = 56 行
相位可控性：否（实现相位恒为静态 90.000°），来源 /home/luo/DigitalPhaseMeter/spec/drp_semantics.json
     5.0000 MHz → VCO=600.0MHz MULT=12 DIVCLK=1 D=120 → f_out=5.000000MHz，相位字 0x218=0（实际 90.000°）
     6.2500 MHz → VCO=750.0MHz MULT=15 DIVCLK=1 D=120 → f_out=6.250000MHz，相位字 0x218=0（实际 90.000°）
     8.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=125 → f_out=8.000000MHz，相位字 0x218=0（实际 90.000°）
    10.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=100 → f_out=10.000000MHz，相位字 0x218=0（实际 90.000°）
    12.5000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=80 → f_out=12.500000MHz，相位字 0x218=0（实际 90.000°）
    20.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=50 → f_out=20.000000MHz，相位字 0x218=0（实际 90.000°）
    25.0000 MHz → VCO=1000.0MHz MULT=20 DIVCLK=1 D=40 → f_out=25.000000MHz，相位字 0x218=0（实际 90.000°）
[  1]    5.0000MHz     0.0° → f=   5.000000 MHz phase=   90.000° err=+90.0000° gate=2010 edge=201 phaseCnt=4020 cycles=20.0000  ⚠相位未生效
[  2]    5.0000MHz    22.5° → f=   5.000000 MHz phase=   90.000° err=+67.5000° gate=2010 edge=201 phaseCnt=4020 cycles=20.0000  ⚠相位未生效
[  3]    5.0000MHz    45.0° → f=   5.000000 MHz phase=   90.000° err=+45.0000° gate=2010 edge=201 phaseCnt=4020 cycles=20.0000  ⚠相位未生效
[  4]    5.0000MHz    67.5° → f=   5.000000 MHz phase=   90.000° err=+22.5000° gate=2010 edge=201 phaseCnt=4020 cycles=20.0000  ⚠相位未生效
[  5]    5.0000MHz    90.0° → f=   5.000000 MHz phase=   90.000° err=+0.0000° gate=2010 edge=201 phaseCnt=4020 cycles=20.0000
...
[ 56]   25.0000MHz   157.5° → f=  25.000000 MHz phase=   90.000° err=-67.5000° gate=2002 edge=1001 phaseCnt=4004 cycles=4.0000  ⚠相位未生效

CSV 前几行（freq_nominal,phase_nominal,f_meas_mHz,phase_meas_mdeg,gateCnt,edgeCnt,phaseCnt,phaseCycles,err_deg,phase_controllable）：
  5000000,0.0,5000000,90000,2010,201,4020,20.0,90.0,False
  5000000,22.5,5000000,90000,2010,201,4020,20.0,67.5,False
  5000000,45.0,5000000,90000,2010,201,4020,20.0,45.0,False
  5000000,67.5,5000000,90000,2010,201,4020,20.0,22.5,False
  5000000,90.0,5000000,90000,2010,201,4020,20.0,0.0,False
  5000000,112.5,5000000,90000,2010,201,4020,20.0,-22.5,False
  5000000,135.0,5000000,90000,2010,201,4020,20.0,-45.0,False
  5000000,157.5,5000000,90000,2010,201,4020,20.0,-67.5,False
  ...（共 56 行）

扫描完成：56 行，|err_deg| 最大 90.0000° 平均 45.0000°
⚠ 当前 bitstream 相位不可在线改（spec/drp_semantics.json: phase_controllable=false）：实现相位恒为静态 90°，49/56 行的相位与请求值不符（err_deg 已如实反映）
  要在线扫描相位需按 docs/ps-protocol.md 6.2 给 clk_wiz_src 加 CONFIG.PHASE_DUTY_CONFIG 并重建 IP/BD
CSV 已写入 /tmp/scan.csv
EXIT=0

$ head -4 /tmp/scan.csv
freq_nominal,phase_nominal,f_meas_mHz,phase_meas_mdeg,gateCnt,edgeCnt,phaseCnt,phaseCycles,err_deg,phase_controllable
5000000,0.0,5000000,90000,2010,201,4020,20.000000,90.0000,False
5000000,22.5,5000000,90000,2010,201,4020,20.000000,67.5000,False
5000000,45.0,5000000,90000,2010,201,4020,20.000000,45.0000,False

$ wc -l < /tmp/scan.csv
57
```

说明（很重要）：

- **频率与计数完全符合冻结模型**：例如 5MHz/90° → `gate=2010 edge=201 phaseCnt=4020`、25MHz/90° →
  `gate=2002 edge=1001 phaseCnt=4004`，与 `spec/golden/vectors.json` 的期望值逐位相同。
- **相位列如实反映硬件限制**：当前 bitstream 的相位字未接线（`phase_controllable=false`），
  所以实现相位恒为 90°；`err_deg` 就是"请求值 - 实现值"的真实差，没有假装成功。
  CSV 前 9 列是验收要求的列（列名保留，`f_meas_mHz` 的单位按 `docs/ps-protocol.md` 第 5 节
  字段单位表 = **Hz**），第 10 列 `phase_controllable` 是 Lead 要求追加的。

**相位通路自测**（仿真后端假装"补丁已生效"，用于验证上位机相位链路；真实硬件仍需重建 IP/BD）：

```console
$ python3 -m dpm_host.sim_server --pty --pty-link /tmp/dpm_sim_pty --phase-model controllable &
$ python3 -m dpm_host --port /dev/pts/2 scan --freqs 10e6,25e6 --phases 0,22.5,45,90,135 --out /tmp/scan_ctrl.csv
...
CSV 前几行（freq_nominal,phase_nominal,f_meas_mHz,phase_meas_mdeg,gateCnt,edgeCnt,phaseCnt,phaseCycles,err_deg,phase_controllable）：
  10000000,0.0,10000000,0,2005,401,0,0.0,0.0,False
  10000000,22.5,10000000,18000,2005,401,802,2.0,-4.5,False
  10000000,45.0,10000000,45000,2005,401,2005,5.0,0.0,False
  10000000,90.0,10000000,90000,2005,401,4010,10.0,0.0,False
  10000000,135.0,10000000,135000,2005,401,6015,15.0,0.0,False
  25000000,0.0,25000000,0,2002,1001,0,0.0,0.0,False
  25000000,22.5,25000000,22500,2002,1001,1001,1.0,0.0,False
  25000000,45.0,25000000,45000,2002,1001,2002,2.0,0.0,False
  ...（共 10 行）

扫描完成：10 行，|err_deg| 最大 4.5000° 平均 0.4500°
说明：spec/drp_semantics.json 记录当前 bitstream 相位不可在线改（phase_controllable=false），但本次扫描的设备接受了相位设置（仿真后端 --phase-model controllable 模拟的「补丁后」行为）；真实硬件上相位仍恒为 90°，需重建 IP/BD 后才生效
CSV 已写入 /tmp/scan_ctrl.csv
EXIT=0
```

`10000000,22.5°` 的 `-4.5°` 误差是**模型自带的 DSP 量化**（10MHz 下 22.5° = 2.5 个 DSP 周期，
半数取偶后取 2 个 → 18°，与 golden 的 `phase_quant_error_mdeg` 一致），不是扫描缺陷。

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
- 与 C 侧 `ps/sim/host_main` 的互通已验证（见验收 4），另有 3 处 C 侧差异已上报、
  上位机做了兼容；`STREAM` 通知在 C 侧需要持续请求推进其模型时间才会到点（见验收 4 说明）。
- `sim_fpga` 不含 MMCM 抖动/相位噪声、真实布线延迟差与 CDC 亚稳态（见第 6 节）；
  它的相位模型与真实 bitstream 的差异由 `--phase-model` 显式选择，默认跟随实测结论。
- `--phase-model controllable` 是**假设**（"补丁后相位字可用"），真实行为需重建 IP/BD 后实测。
- GUI 没有做视觉回归（无截图工具链），只有"启动无 traceback + 服务端日志可见命令流"的冒烟证据。
