# 上板记录：ALINX AX7010（xc7z010clg400-1）

> 本文记录 2026-10-15 在真实开发板上做的验证与发现。命令都可复现；
> 未完成的部分明确标为「未完成」，不要当成已验证。

> ⚠️ **历史记录说明**：本文全部上板数据都是 **DSP=400MHz 轮次**的历史记录，冻结验证频点也是
> 当时那一套（5/6.25/8/10/12.5/16/20/25/40/50MHz）。**当前设计 DSP 已改为 450MHz**、冻结验证
> 频点已更换（见 [`ps-protocol.md`](ps-protocol.md) §6.4 与 [`verify-plan.md`](verify-plan.md) §3），
> 本轮**未重新上板验证**；下面的实测数字保留原样，不要当作当前 450MHz 设计的测量结论。

## 0. 硬件与连接

| 项            | 值                                                                 |
| ------------- | ------------------------------------------------------------------ |
| 板卡          | ALINX AX7010，芯片 `xc7z010clg400-1`（JTAG IDCODE `0x03727093`）   |
| 启动模式      | SD 卡（卡内有 ALINX Debian 8 / U-Boot）                            |
| JTAG          | Digilent FT232H（序列号 210512180081），`hw_server` 经 `xsdb` 访问 |
| 板载 USB-UART | CP2102N → `/dev/ttyUSB0`，接 PS **UART1 / MIO48(tx) / MIO49(rx)**  |
| MIO 供电      | Bank0 = 3.3V、Bank1 = 1.8V（MIO48/49 在 Bank1）                    |

JTAG 电缆需要 root 装一次 udev 规则（本机已装）：

```
sudo /home/luo/Vivado/2025.2/Vivado/data/xicom/cable_drivers/lin64/install_script/install_drivers/install_drivers
```

## 1. bitstream 侧改动（已重建并上板）

> **注意（方案 A 之后）**：本节记录的是当时那一版 bitstream 的改动。`vivado/create_bd.tcl`
> 后来换成了「裸 MMCM + 极简 DRP 控制器」（方案 A），串口做成 `-uart` 参数；
> `clk_wiz_src` 与当时的两个补丁脚本都已不存在。当前脚本以
> [`../vivado/README.md`](../vivado/README.md) 与 `create_bd.tcl` 的注释为准。

* 当时改的 `vivado/create_bd.tcl`：
  * `clk_wiz_src.CONFIG.PHASE_DUTY_CONFIG = true` —— 让那一版的 DRP 相位字接入 MMCM 配置
    重建逻辑（原来写死静态 generic，相位恒 90°）；
  * PS7 使能 UART1（MIO48/49、115200 8N1），并把 MIO Bank0/Bank1 供电写成
    `PCW_PRESET_BANK0_VOLTAGE=LVCMOS 3.3V` / `BANK1=LVCMOS 1.8V`（Zynq-7000 的 MIO
    I/O 标准由供电轨决定，这两项是声明性属性），脚本里带回读校验——**这一步现在等价于
    `create_bd.tcl -uart uart1`（默认值）**。
* 重建产物：`build/vivado/dpm_system.xsa`、`bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit`
  （WNS **+0.110ns**、WHS +0.051ns）。
* 复验（当时用两个补丁脚本的 dry run）。**现在等价的做法**：`create_bd.tcl` 自带的回读校验
  （`PS7_UART_CHECK: EN_UART1=1 IO='MIO 48 .. 49'`）+
  `python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa`。

## 2. 上板启动流程（可复现）

```
xsdb -quiet ps/tools/jtag_boot.tcl     # rst -system -stop → fpga -f → ps7_init → dow -data → PC=0 → con
```

**踩过的坑（都写进脚本注释了）**：

1. 不能用 `rst -system` 后等 300ms 再 stop：SD 卡的 FSBL/U-Boot 会跑起来打开 MMU，
   之后 PC=0 走虚拟地址，程序**一个字节都不会执行**（现象极具误导性）。
   必须 `rst -system -stop` 一步到位。
2. `dow`（ELF 形式）在 MMU 开着时会写到虚拟地址，代码根本没进内存；
   改用 `dow -data ps/build/dpm_ps.bin 0x0` 经 APU 物理通路写入，并用 `mrd 0x0`
   与 ELF 的 `.vectors` 首字比对确认。
3. **运行中绝不要在 Cortex-A9 目标上读内存/寄存器**：APB-AP 会进入粘滞错误态，
   之后 `rst -dap`/`-system`/`-cores` 全部失效，只能按键复位板子。
   要看程序状态就读 APU 一侧的 UART/PL 寄存器。

## 3. 上板实测结论

* **串口通了**：`/dev/ttyUSB0` @115200 收到上电横幅并每秒重发（收到首帧后停发）：
  `DPM-PS ready: uart=UART1 baud=115200 uart_ref_hz=86400000 cpu_variant=3 proto=1.1`
* **协议双向通了**：`IDENTIFY` 返回 30 字节 payload（`0x82`）、`PING` 返回 `0x81`，
  CRC 全部正确。
* **UART 参考时钟实测 = 86.4MHz**（不是 PL 的 50MHz 慢时钟！）：
  把分频固定成 `CD=6、BAUDGEN=124`（K=750）后用 JTAG 直接往 TX FIFO 灌 `DPM-PS`，
  上位机在 112k–120k 波特率之间能清晰解出，中心即 115200 → 115200×750 = 86.4MHz。
  已写成 `DPM_UART_REF_HZ` 默认值，并留了重测工具 `ps/tests/find_uart_baud.py`。
* **PL 侧测量通路正常**：`PL_VERSION=0x44504D02`、`PL_SIG_STATUS` 显示片内源
  enable/select/locked 均正常、寄存器区能读到有效测量结果（`RESULT_GATE=2009`）。

## 4. 上板暴露并已修复的固件缺陷（8 处）

| #   | 缺陷                                                 | 证据/后果                                                                                   |
| --- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| 1   | `DPM_UART_STS_TX_FULL` 写成 `1<<3`（其实是 TXEMPTY） | `dpm_uart_putc` 第一个字节就死等，串口全哑                                                  |
| 2   | `ISR/IDR/IER` 偏移写成 0x20/0x24/0x28                | 0x24 实际是 MODEMCR，写全 1 打开硬件流控 → TX 永久阻塞                                      |
| 3   | UART0/UART1 基地址写反                               | UART1 实际在 `0xE0001000`（TRM/xuartps_hw.h 为准）                                          |
| 4   | 把 PL 慢时钟 50MHz 当 UART 参考时钟                  | 波特率错 1.7 倍                                                                             |
| 5   | `startup.c` 写 VBAR（`mcr p15,0,..,c12,c0,0`）       | **Zynq-7000 无 TrustZone，该寄存器未实现** → 未定义指令异常，程序卡死在 `dpm_undef_handler` |
| 6   | `.data` 拷贝源用了 `_etext`（指向 `.rodata` 开头）   | 把只读数据当初始值拷进 `.data`（横幅里 `cpu_variant` 打出 0x313D6F74）                      |
| 7   | `__aeabi_uldivmod` 是手写汇编                        | 与 AAPCS 栈参数/对齐耦合，上板在求解器做 64 位除法时触发数据访问异常；已改为纯 C 结构体返回 |
| 8   | `dpm_cmd.c` 的 SET_SIG 路径                          | **仍未解决**，见下节                                                                        |

## 4b. 相位控制与中断通信：已完成（上板实测）

### 相位控制（MMCM 补丁判决性验证）

纯 JTAG 实验（不经过 PS 程序）：写 DRP `0x218`（毫度）→ 写 `LOAD(0x25C)=0x07` →
clear → start → 读 `phaseCnt`（慢时钟 `f_slow=50MHz`、`f_dsp=400MHz`、8MHz 输出）：

| 写入相位 | 实测相位 | 说明                                               |
| -------- | -------- | -------------------------------------------------- |
| 0°       | 0.2°     |                                                    |
| 45°      | 43.2°    |                                                    |
| 90°      | 86.4°    |                                                    |
| 135°     | 129.5°   |                                                    |
| 180°     | 179.8°   |                                                    |
| 270°     | 86.4°    | 回绕：可表示范围 ≈ 64/D 个输出周期，D=125 时 ≈184° |

误差全部落在**1 个 DSP 周期（2.5ns）的量化**内（8MHz 时 1 计数 = 7.2°），不是控制误差。

### 上板 28 点扫描（7 频点 × 4 相位，走中断路径）

```
python3 -m dpm_host --port /dev/ttyUSB0 --baud 115200 scan \
  --freqs 5e6,6.25e6,8e6,10e6,12.5e6,20e6,25e6 --phases 0,45,90,135 --settle 0.1
```

**28/28 完成，全部 `phase_controllable=True`**；频率误差 ≤0.05%，相位误差
（最大 9.0°、平均 1.13°）**等于测量量化下界**：例如 8MHz/90° 实测 86.41°，
与模型预测的 86.367°（12 个 DSP 周期）一致；20MHz/135° 报 126° = 7×18°（20MHz
时 1 计数 = 18°），也在半计数范围内。CSV：`/tmp/board_sweep.csv`（复跑会重生成）。

### 中断通信（本轮修好并验证）

| 缺陷                                                                  | 后果                                          | 修法                         |
| --------------------------------------------------------------------- | --------------------------------------------- | ---------------------------- |
| IRQ 入口 `srsdb sp!, #0x12`（存 IRQ 栈）却用 `rfeia sp!`（取 SVC 栈） | 保存/恢复不是同一处，第一次中断就跳到垃圾地址 | 改成 `srsdb sp!, #0x13`      |
| GIC 只 `enable` 没设 `ITARGETSR`                                      | SPI 复位后目标 CPU 为 0，中断根本不送达       | 补 `set_priority/set_target` |
| 使能的是 UART0 的中断号（本工程用 UART1）                             | 串口中断永不触发                              | 改 `DPM_IRQ_ID_UART1`(82)    |

验证方式：上面的 28 点扫描与 PING/IDENTIFY/SET_SIG 全部在 `DPM_USE_IRQ=1`
（中断路径）构建上跑通，`ps/build.sh` 默认即该构建。

### 本轮另外修掉的三个缺陷

| #   | 缺陷                                                      | 证据                                                                                                                                                                         |
| --- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 9   | `__aeabi_uidivmod`/`__aeabi_uldivmod` 用 C 结构体返回实现 | AAPCS32 结构体返回是隐藏指针，与 RTABI 的「商/余数放寄存器」不符：GCC 生成的除法调用把被除数低字当指针，`str r0,[fp,#12]` 直接数据中止。改成 RTABI 汇编（自保 8 字节栈对齐） |
| 10  | `.data` 拷贝源用 `_etext`（在 `.rodata` 之前）            | 初始化数据被只读数据覆盖（横幅 `cpu_variant` 打出 0x313D6F74）。改 `LOADADDR(.data)`                                                                                         |
| 11  | `SET_SIG` 改完片内源不复位测量 FSM                        | DRP 一改，等 extSig_sig2 边沿的窗口永远等不到 → `busy` 恒 1、`GET_RESULT` 永远 `ERR_BUSY`。改成写 `CTRL.clear`                                                               |

### 语义/测试同步更新（有上板证据才改）

* `spec/drp_semantics.json`：`phase_controllable` 由 `false` 改 `true`，并写明上板实测数据；
* `host/dpm_host/sim_fpga.py`、`ps/sim/fpga_model.c`：默认相位模型跟随新语义（旧行为保留在
  `--phase-model static`，仍可复现 PHASE_DUTY_CONFIG=false 的 bitstream）；
* 宿主机 3 个编码旧结论的测试随之更新（`test_sig` / `test_sim` / `test_client_over_pty`）。

## 4c. 一条命令的验收：`ps/tools/onboard_accept.py`

```
.venv/bin/python ps/tools/onboard_accept.py --port /dev/ttyUSB0            # 含 JTAG 冷启动
.venv/bin/python ps/tools/onboard_accept.py --port /dev/ttyUSB0 --no-boot  # 板子已在跑，只做验收
```

流程：前置检查 → JTAG 冷启动（`ps/tools/jtag_boot.tcl`）→ 串口链路（PING + 上电横幅）→
协议与错误路径（`ERR_PARAM`）→ 连续模式 + 流上报 → **79 点扫描（10 频点 × 逐频点相位点，
含 31 个 ≥180° 的点）** → 写报告。
产物：`build/onboard_acceptance.{md,json,csv}` 与 `build/onboard_boot.log`；任一项失败退出码非 0。

**相位判据（2026-10-15 修订）**：期望值按 **MMCM 的移相精度**量化，不按 DSP 采样网格量化。

* MMCM 的相位最小步长是 1/8 VCO 周期（VCO=1GHz 时 125ps），折算到输出时钟（周期 = D 个
  VCO 周期）就是 `45°/D`：D=125 → 0.36°、D=50 → 0.9°、D=40 → 1.125°。相位字（毫度）经它量化
  之后才是 **MMCM 实际输出相位差**，即
  `exp = round(相位字 / (45°/D)) × (45°/D)`，其中 D 取 `SET_SIG` 回显的分频比；
* 容限 = **1 个 DSP 量化步长** `360·f_sig/f_dsp`（400MHz → 2.5ns）：这是测量端的分辨率下界
  （相位计数只能是整数），再加 1 毫度吸收定点取整。**DSP 量化是容限，不是期望值的一部分**；
* 量化公式复用 `host/dpm_host/sig.py::phase_word_value`（与 `ps/src/dpm_sig.c` 同一套冻结
  规则），CSV 里同时给出 `exp_deg`、`mmcm_grid_deg`、`dsp_quantum_deg`、`err_over_quantum`。

上板结果（JTAG 冷启动 + 中断路径构建，`DPM_USE_IRQ=1`）：**47/47 项通过**，扫描 32/32；
最大相位偏差 5.400°（8MHz/135°，0.75 个 DSP 量化步长），其余点 ≤0.5 步。

> **2026-10-16 第二轮更新**：频点扩到 10 个（5/6.25/8/10/12.5/16/20/25/40/50MHz）、相位点
> 逐频点生成（含 180°/225°/270°/315° 与相位码上界点 `183.96°/191.625°`），共 **79 点**
> （31 个 ≥180°）；上板 **93/93 项通过、扫描 79/79**。相位上界不再是写死的 180°，而是
> 9 位相位码 `m ≤ 511`（见 `docs/ps-protocol.md` §6.2/§6.4 与 `docs/verify-report.md` §11）。
> 本轮还修正了两条过紧的判据（计数自检按闸门区间折算、流上报 seq 判「单调不减 + 推进」），
> 详见 §11.4。

两个与判据无关但影响结论的修正：

* 流上报检查原先读 `StreamData.seq`（不存在），实际字段是 `data.meas.seq`，于是「有帧」
  被误报成「0 条」——上板与 C 验证台都会复现；改成分别统计帧数与 seq 后两项均通过；
* 脚本头部补了 `sys.path` 兜底，未装 `dpm-host` 包时也能直接跑（`.venv` 装了就仍用装好的）。

离线复跑（不接板）：`bash ps/tools/onboard_accept_sim.sh` —— 用 C 验证台
`ps/build/host_main`（与固件同一份 `dpm_cmd.c`）当服务端，经 socat 的一对 pty 对接验收脚本，
`--no-boot` 下 **93/93 项通过**（79 点扫描 79/79），报告落在 `build/onboard_accept_sim/`。
改脚本后先在这里过一遍再上板。

## 5. 未完成 / 未验证（重要）

* ~~**`SET_SIG` 的相位上界**：协议里 `phase < 180000`（180°）是有意为之……~~
  **已解决（2026-10-16 第二轮）**：上界改为硬件依据——9 位相位码
  `m = round(相位 × D / 45000) ≤ 511`，即可请求上界 `min(23017499/D, 359999)` 毫度
  （D=20/25/40/50 → 整个 0~360°；D=120 → 191.812°；D=125 → 184.139°）。越界请求显式回
  `ERR_PARAM`（字段截断会让相位回绕：旧 bitstream 实测 8MHz 写 270° 读到 86.4°，就是
  `m=750` 截成 238 步的结果）。**「请求必须 < 180°」的老规则已作废**：相位计测的是无符号
  间隔（`extSig_sig1` 沿 → 下一个 `extSig_sig2` 沿，落在 `(0, 360°]`），>180° 本来就可测；上板与 xsim
  联调都已覆盖 180°~315°（`docs/verify-report.md` §11）。
* ~~**`SET_SIG` 回包的 `phase_field` 仍为 0**~~ **已修复（2026-10-16）**：改为回报 MMCM 的
  `DELAY_TIME` 字段（= 相位码 >> 3），与硬件写入值一致（见 `docs/verify-report.md` §10.2 缺陷 6）。
* ~~**相位字仍被固件吸附到 45° 的整数倍**~~ **已修复（2026-10-16）**：改为吸附到硬件网格
  `45/D`（1/8 VCO 周期），并把 MMCM 编码里接反的 `PHASE_MUX`/`DELAY_TIME` 位域改正
  （旧实现把相位量化到 1 个 VCO 周期 = `360/D` 度，浪费 8 倍分辨率）。上板实测 8MHz 请求 22.5°
  → 实测 21.602°（3 个 DSP 周期），证明细调网格真的生效（旧固件会吸附到 45°）。
  详见 `docs/verify-report.md` §10.2 缺陷 4/5。
* ~~**`IDENTIFY.flags.b4`（phaseControllable）仍是 0**~~ **已修复（2026-10-16）**：
  `DPM_DRP_PHASE_WRITABLE=1` 后固件自动置位；重建并上板实测 `flags=0x001F`。
* **长稳/精度**：本轮只做了单次点测量，没有做长时间连续模式统计（抖动、丢结果）。
  （流上报期间连续模式会沿用固件默认闸门，`seq` 增长很快，属正常。）
* **0° 的边界约定**：旧 bitstream（2026-10 之前）上板实测请求 0° 读到**一个整周期**
  （CSV 里 `phase_meas_deg=360.000`），而 `ps/sim/fpga_model.c` 的模型给 0°。根因已定位为
  PL 相位计使能窗口的「清零/置位同拍竞争」：两沿落在同一个 DSP 采样拍时，置位优先级吞掉了
  清零，使能一直保持到下一个信号周期，于是读数跳成整周期（这也让 22.5° 这类小于 1 个 DSP
  周期的相位在高频点被读成整周期）。`PhaseMeter.scala` 已修复（清零与置位同延迟、清零优先），
  修复后两沿同拍时量化为 0 个周期，与 0° 同义；验收判据始终按取模 360 比较，两种约定都通过。
  **该行为变化尚未重新上板验证**（本轮只跑了 xsim 联调 `sim/run_handoff_tb.sh` 与 Verilator 单测）。
* **上电自启**：本流程依赖 JTAG 加载；SD 卡里的 BOOT.bin（FSBL + bitstream + 程序）
  仍未在板上验证（本机没有 Vitis/ARM 工具链生成 FSBL）。
* 上电横幅里的 `DPM_BRINGUP_TRACE` 调试钩子（写 PL 的 GATE_TARGET 寄存器）默认关闭，
  需要时用 `-DDPM_BRINGUP_TRACE=1` 打开；它会占用闸门目标寄存器，测完务必关掉。

---

## 6. 2026-10-16 复验：方案 A 的 bitstream 上板（新增）

上一轮（第 1~5 节）跑的是旧 `clk_wiz_src` 方案。本轮把**方案 A**（裸 MMCM + 极简 DRP 控制器）
真正烧进板子并复验，发现并修掉了 3 个只有上板才暴露的 PL 缺陷（详见 `docs/verify-report.md` §10）：

| #   | 现象                                        | 根因                                                                                                 | 修复                                                                                                          |
| --- | ------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| 1   | `SET_SIG` 恒回 `ERR_BUSY`，DRP 镜像读不回来 | `dpm_src_drp` 的窗口判断用 `addr[31:6]==0`，但 AXI 互连传的是完整地址 `0x43C1_00xx` → 所有访问被丢弃 | 只译码页内偏移 `addr[11:6]==0 && addr[1:0]==0`                                                                |
| 2   | 分频字生效、**相位字不生效**                | DRP 写期间没有拉高 MMCM 的 `RST`（unisim 模型与手册都要求器件处于复位）                              | 控制器新增 `mmcm_reset`，时序改为 RST=1 → 8 笔 DRP 写 → RST=0 → 重新锁定；`dpm_mmcm_src` 新增 `drpReset` 端口 |
| 3   | xsim 报 `DWE is high for more than 1 DCLK`  | `drp_dwe` 常置 1                                                                                     | DWE 与 DEN 一样只在写那一拍拉高                                                                               |

修复后同一套上板流程的结论（bitstream 2026-10-16 01:44，固件 `gitRev=0x6DA785A2`）：

* `IDENTIFY`：`flags=0x001F`（含 b4 phaseControllable）；
* 相位跟随请求：8MHz 请求 0/45/90/135/22.5° → 实测 360.22/43.205/86.367/129.487/21.602°；
  25MHz 0/90° → 360.179/90.134°（判据：1 个 DSP 周期 = 2.5ns 的测量量化下界）；
* `uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0 --no-boot` → **44/44 项通过**，
  32 点扫描 32/32（报告 `build/onboard_acceptance.md`）。

命令仍与第 2 节一致（`xsdb -quiet ps/tools/jtag_boot.tcl` 冷启动后跑验收），只是 bitstream 与固件
都是修复后的版本。**仍未做**：SD 卡 `BOOT.bin` 自启（本机没有 Vitis/FSBL 工具链）、长稳/温漂统计。

---

## 7. 2026-10-16 第二轮上板：更多频点 + >180° 相移（新增）

**PL 侧未改**（同一份 bitstream），只重编固件（`dpm_cmd.c` 的相位校验）并重新经 JTAG 加载。

1. 相位上界从「写死 <180°」改为硬件依据：9 位相位码 `m = round(相位 × D / 45000) ≤ 511`
   ⟺ 可请求 `min(23017499/D, 359999)` 毫度；越界回 `ERR_PARAM`（不静默改相位）。
2. 验收点表扩到 10 频点 × 逐频点相位点 = **79 点（31 个 ≥180°）**；
   `ps/tools/onboard_accept.py` 的扫描由 32 点变成 79 点。
3. 结果：**93/93 项通过、扫描 79/79**（报告 `build/onboard_acceptance.{md,json,csv}`）。
   >180° 的实测抽样：25MHz/270° → 270.000°、12.5MHz/270° → 270.000°、50MHz/315° → 315.000°、
   8MHz/183.96°（相位码上界 511）→ 179.999°（差 0.55 个 DSP 量化步长）、
   5MHz/191.625° → 189.000°（差 0.58 步）。
4. 两条判据被修正（不是硬件缺陷，见 `docs/verify-report.md` §11.4）：计数自检改为按闸门
   **区间**折算参考沿数（原判据在高频点忽略了相位计 DSP 窗口多出的几拍 CDC 延迟）、
   流上报 seq 判「单调不减 + 推进」（固件语义是周期性推送最新结果，可能重复同一 seq）。
5. **SD 卡未被改动**：全程 `xsdb -quiet ps/tools/jtag_boot.tcl`（先 `rst -system -stop` 停核，
   再配 PL、`dow` 到 OCM、`con`），没有任何 mmc/文件写入操作，卡上原有镜像保持原样。
