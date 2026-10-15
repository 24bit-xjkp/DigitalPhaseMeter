# 上板记录：ALINX AX7010（xc7z010clg400-1）

> 本文记录 2026-10-15 在真实开发板上做的验证与发现。命令都可复现；
> 未完成的部分明确标为「未完成」，不要当成已验证。

## 0. 硬件与连接

| 项 | 值 |
| --- | --- |
| 板卡 | ALINX AX7010，芯片 `xc7z010clg400-1`（JTAG IDCODE `0x03727093`） |
| 启动模式 | SD 卡（卡内有 ALINX Debian 8 / U-Boot） |
| JTAG | Digilent FT232H（序列号 210512180081），`hw_server` 经 `xsdb` 访问 |
| 板载 USB-UART | CP2102N → `/dev/ttyUSB0`，接 PS **UART1 / MIO48(tx) / MIO49(rx)** |
| MIO 供电 | Bank0 = 3.3V、Bank1 = 1.8V（MIO48/49 在 Bank1） |

JTAG 电缆需要 root 装一次 udev 规则（本机已装）：

```
sudo /home/luo/Vivado/2025.2/Vivado/data/xicom/cable_drivers/lin64/install_script/install_drivers/install_drivers
```

## 1. bitstream 侧改动（已重建并上板）

* `vivado/create_bd.tcl`：
  * `clk_wiz_src.CONFIG.PHASE_DUTY_CONFIG = true` —— 这是让 DRP 相位字**真正接入**
    MMCM 配置重建逻辑的开关（原来写死静态 generic，相位恒 90°）；
  * PS7 使能 UART1（MIO48/49、115200 8N1），并把 MIO Bank0/Bank1 供电写成
    `PCW_PRESET_BANK0_VOLTAGE=LVCMOS 3.3V` / `BANK1=LVCMOS 1.8V`（Zynq-7000 的 MIO
    I/O 标准由供电轨决定，这两项是声明性属性），脚本里带回读校验。
* 重建产物：`build/vivado/dpm_system.xsa`、`bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit`
  （WNS **+0.110ns**、WHS +0.051ns）。
* 复验：`patch_enable_ps_uart.tcl`（dry）回读 `PCW_EN_UART1=1 / MIO 48 .. 49`；
  `patch_enable_src_phase.tcl`（dry）回读 `PHASE_DUTY_CONFIG=true`。

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

| # | 缺陷 | 证据/后果 |
| --- | --- | --- |
| 1 | `DPM_UART_STS_TX_FULL` 写成 `1<<3`（其实是 TXEMPTY） | `dpm_uart_putc` 第一个字节就死等，串口全哑 |
| 2 | `ISR/IDR/IER` 偏移写成 0x20/0x24/0x28 | 0x24 实际是 MODEMCR，写全 1 打开硬件流控 → TX 永久阻塞 |
| 3 | UART0/UART1 基地址写反 | UART1 实际在 `0xE0001000`（TRM/xuartps_hw.h 为准） |
| 4 | 把 PL 慢时钟 50MHz 当 UART 参考时钟 | 波特率错 1.7 倍 |
| 5 | `startup.c` 写 VBAR（`mcr p15,0,..,c12,c0,0`） | **Zynq-7000 无 TrustZone，该寄存器未实现** → 未定义指令异常，程序卡死在 `dpm_undef_handler` |
| 6 | `.data` 拷贝源用了 `_etext`（指向 `.rodata` 开头） | 把只读数据当初始值拷进 `.data`（横幅里 `cpu_variant` 打出 0x313D6F74） |
| 7 | `__aeabi_uldivmod` 是手写汇编 | 与 AAPCS 栈参数/对齐耦合，上板在求解器做 64 位除法时触发数据访问异常；已改为纯 C 结构体返回 |
| 8 | `dpm_cmd.c` 的 SET_SIG 路径 | **仍未解决**，见下节 |

## 4b. 相位控制与中断通信：已完成（上板实测）

### 相位控制（MMCM 补丁判决性验证）

纯 JTAG 实验（不经过 PS 程序）：写 DRP `0x218`（毫度）→ 写 `LOAD(0x25C)=0x07` →
clear → start → 读 `phaseCnt`（慢时钟 `f_slow=50MHz`、`f_dsp=400MHz`、8MHz 输出）：

| 写入相位 | 实测相位 | 说明 |
| --- | --- | --- |
| 0° | 0.2° | |
| 45° | 43.2° | |
| 90° | 86.4° | |
| 135° | 129.5° | |
| 180° | 179.8° | |
| 270° | 86.4° | 回绕：可表示范围 ≈ 64/D 个输出周期，D=125 时 ≈184° |

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

| 缺陷 | 后果 | 修法 |
| --- | --- | --- |
| IRQ 入口 `srsdb sp!, #0x12`（存 IRQ 栈）却用 `rfeia sp!`（取 SVC 栈） | 保存/恢复不是同一处，第一次中断就跳到垃圾地址 | 改成 `srsdb sp!, #0x13` |
| GIC 只 `enable` 没设 `ITARGETSR` | SPI 复位后目标 CPU 为 0，中断根本不送达 | 补 `set_priority/set_target` |
| 使能的是 UART0 的中断号（本工程用 UART1） | 串口中断永不触发 | 改 `DPM_IRQ_ID_UART1`(82) |

验证方式：上面的 28 点扫描与 PING/IDENTIFY/SET_SIG 全部在 `DPM_USE_IRQ=1`
（中断路径）构建上跑通，`ps/build.sh` 默认即该构建。

### 本轮另外修掉的三个缺陷

| # | 缺陷 | 证据 |
| --- | --- | --- |
| 9 | `__aeabi_uidivmod`/`__aeabi_uldivmod` 用 C 结构体返回实现 | AAPCS32 结构体返回是隐藏指针，与 RTABI 的「商/余数放寄存器」不符：GCC 生成的除法调用把被除数低字当指针，`str r0,[fp,#12]` 直接数据中止。改成 RTABI 汇编（自保 8 字节栈对齐） |
| 10 | `.data` 拷贝源用 `_etext`（在 `.rodata` 之前） | 初始化数据被只读数据覆盖（横幅 `cpu_variant` 打出 0x313D6F74）。改 `LOADADDR(.data)` |
| 11 | `SET_SIG` 改完片内源不复位测量 FSM | DRP 一改，等 sig2 边沿的窗口永远等不到 → `busy` 恒 1、`GET_RESULT` 永远 `ERR_BUSY`。改成写 `CTRL.clear` |

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
协议与错误路径（`ERR_PARAM`）→ 连续模式 + 流上报 → 32 点扫描（8 频点 × 4 相位）→ 写报告。
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

两个与判据无关但影响结论的修正：

* 流上报检查原先读 `StreamData.seq`（不存在），实际字段是 `data.meas.seq`，于是「有帧」
  被误报成「0 条」——上板与 C 验证台都会复现；改成分别统计帧数与 seq 后两项均通过；
* 脚本头部补了 `sys.path` 兜底，未装 `dpm-host` 包时也能直接跑（`.venv` 装了就仍用装好的）。

离线复跑（不接板）：`bash ps/tools/onboard_accept_sim.sh` —— 用 C 验证台
`ps/build/host_main`（与固件同一份 `dpm_cmd.c`）当服务端，经 socat 的一对 pty 对接验收脚本，
`--no-boot` 下 **44/44 项通过**，报告落在 `build/onboard_accept_sim/`。改脚本后先在这里过一遍
再上板。

## 5. 未完成 / 未验证（重要）

* **`SET_SIG` 的相位上界**：协议里 `phase < 180000`（180°）是有意为之——8MHz 这类
  D 较大的点只能表示 ≈184°，但 25MHz（D=40）可表示 ≈576°；是否放开上界待定。
  补充：MMCM 的延迟字是 6 位 VCO 周期，可表示范围实际是 `64/D` 个输出周期
  （D=125 时 184.32°），超过就按 `64/D` 回绕——纯 JTAG 实验里写 270° 读到 86.4° 即此故，
  固件把请求限制在 <180° 且 D ≤ 127，因此正常路径碰不到回绕。
* **`SET_SIG` 回包的 `phase_field` 仍为 0**（应用写了正确的 DRP 相位字，回包字段没跟上），
  属上报字段问题，不影响控制，待修。
* **相位字仍被固件吸附到 45° 的整数倍**：`dpm_sig.c` 的 `DPM_SIG_PHASE_UNIT_DEG_MILLI=45000`
  是「候选 B（字段 = 45° 步长）」的遗留；实测硬件网格是 `45/D`（≪1°），即固件把
  MMCM 的移相能力浪费了约 40~125 倍。验收脚本以 `SET_SIG` 回显的相位字为准，所以
  非 45° 整数倍的请求会以「吸附后的值」为期望值通过，不会暴露这个差异。
* **`IDENTIFY.flags.b4`（phaseControllable）仍是 0**：`dpm_regs.h` 的
  `DPM_DRP_PHASE_WRITABLE` 还是 0，而实测相位可控（`spec/drp_semantics.json` 已改 true）。
  上位机判可控性走的是 `spec/drp_semantics.json`，因此只是上报位没跟上。
* **长稳/精度**：本轮只做了单次点测量，没有做长时间连续模式统计（抖动、丢结果）。
  （流上报期间连续模式会沿用固件默认闸门，`seq` 增长很快，属正常。）
* **0° 的边界约定**：请求 0° 时上板实测读到**一个整周期**（CSV 里 `phase_meas_deg=360.000`，
  与 0° 同义），而 `ps/sim/fpga_model.c` 的模型给 0°。两沿重合时「等下一个 sig2 沿」
  落到下一周期，属相位计「测到下一个沿」的边界行为；验收判据按取模 360 比较，两种
  约定都通过，但模型没覆盖这个点。
* **上电自启**：本流程依赖 JTAG 加载；SD 卡里的 BOOT.bin（FSBL + bitstream + 程序）
  仍未在板上验证（本机没有 Vitis/ARM 工具链生成 FSBL）。
* 上电横幅里的 `DPM_BRINGUP_TRACE` 调试钩子（写 PL 的 GATE_TARGET 寄存器）默认关闭，
  需要时用 `-DDPM_BRINGUP_TRACE=1` 打开；它会占用闸门目标寄存器，测完务必关掉。
