# 片内信号源 clk_wiz 的 DRP 位域语义实测（task-3）

本文记录**用厂商 IP 自己的仿真模型实测**得到的结论：`build/vivado/bd_proj/.../dpm_system_clk_wiz_src_0`
（clk_wiz v6.0，MMCM + 动态重配置，参考 50MHz）的 DRP 寄存器文件到底怎么解释，
以及「通过 DRP 在线改相位」在当前 PL 方案下是否成立。

一句话结论：**当前 IP 的 DRP 寄存器文件里没有相位字段可写**。
寄存器文件里确实有一个相位载体——每个输出一个独立的 **32 位毫度字**（`clk_out2` → 偏移 `0x218`）——
但当前 BD 配置（`CONFIG.PHASE_DUTY_CONFIG=false`）下它**没有接到任何逻辑**，
DRP 重建 MMCM 配置字时相位取自静态 generic `S1_CLKOUTx_PHASE`。
所以 `SET_SIG --phase 22.5` 在当前方案下**做不到**（写进去不会有任何效果），
而**频率**（divide 字段）可以正常在线改。

同一份结论的机器可读版本：`spec/drp_semantics.json`。

---

## 1. 复现方式

```bash
sim/run_drp_tb.sh              # 默认 --rtl：BD 生成目录里的 VHDL + Verilog 混合语言编译
sim/run_drp_tb.sh --netlist    # 同一 IP 的功能仿真网表 *_sim_netlist.v（纯 Verilog）
```

- 激励：`sim/tb_clk_wiz_src_drp.sv`（实例化 `dpm_system_clk_wiz_src_0` 本体，不用替代模型）。
- 时钟：`clk_in1` = `s_axi_aclk` = 50MHz；复位后等 `locked`，再逐点写 DRP 配置 + `Load` 重配置后测量。
- 产物：`build/xsim_drp/<mode>/xsim_drp.log`（完整日志）；
  `spec/golden/drp_meas_rtl.txt`、`spec/golden/drp_meas_netlist.txt`（随交付物留档的原始输出；
  用 `.txt` 是因为仓库 `.gitignore` 有 `*.log`，`.log` 版本留在 `build/` 下）。
- 两种源码模式的 **193 行 `DRP_*` 机器可读输出逐字节一致**（`diff` 为空），
  因此结论不依赖某一种编译方式：既不是 RTL/网表不一致，也不是编译选项造成的假象。
- 运行时间约 10s（`xsim` CPU 1.5s～5s）。

测量方法（1ps 精度时间戳，窗口 32 个输出周期取平均）：

- 周期：相邻同沿时间差的平均；
- `delay_ps`：某次 `clk_out1` 上升沿 → 其后**第一个** `clk_out2` 上升沿的间隔，多周期平均；
- `delay_deg_milli = 360000 × delay_ps / t_out2_ps`（按实测周期换算）。

写配置的流程（每点）：写 `0x200`(CLKFBOUT) → `0x208`(CLKOUT0) → `0x214`(CLKOUT1) → `0x218`(相位字)
→ 写 `0x25C = 0x07` 触发重配置 → 等 `locked` 重新拉高 → 再等 16 个输出沿（Safe Clock Startup
在 `locked` 后 8 拍才放行）→ 测量。

> 实测陷阱（写激励时踩到的）：`clk_wiz` 的 `axi_lite_ipif v1.01a` 在
> `slave_attachment.vhd` 里要求 `AWVALID` 与 `WVALID` **同时**为 1 才离开 `SM_IDLE`，
> 且 `AWREADY`/`WREADY` 由同一个 `wr_done` 驱动（`:587-588`）。
> 「先 AW 后 W」的顺序握手会死锁；激励里必须同时举起两通道（两通道在同一拍完成握手）。

---

## 2. 复位后的寄存器文件 dump（布局的直接证据）

复位释放、MMCM 锁定后逐字读回 `0x200` 起 11 个字（原始输出 `DRP_RD_RESET` 行）：

| 偏移 | 复位值 | 含义（实测 + 代码对照） |
| --- | --- | --- |
| `0x200` | `0x00001401` | CLKFBOUT 分频字：`[7:0]`=DIVCLK=1、`[15:8]`=MULT=20 |
| `0x204` | `0x00000000` | CLKFBOUT 相位字（未连接） |
| `0x208` | `0x00000028` | CLKOUT0（clk_out1）分频字：D=40 |
| `0x20C` | `0x00000000` | CLKOUT0 相位字 = 0 毫度 = 0.000° |
| `0x210` | `0x0000c350` | CLKOUT0 占空比字 = 50000 = 50.000% |
| `0x214` | `0x00000028` | CLKOUT1（clk_out2）分频字：D=40 |
| `0x218` | `0x00015f90` | CLKOUT1 相位字 = **90000 毫度 = 90.000°** |
| `0x21C` | `0x0000c350` | CLKOUT1 占空比字 = 50.000% |
| `0x220` | `0x00000001` | CLKOUT2（未用）分频字 = 1 |
| `0x224` | `0x00000000` | CLKOUT2 相位字 |
| `0x228` | `0x0000c350` | CLKOUT2 占空比字 |

结论：寄存器文件是**每个输出 3 个连续字 `{divide, phase(毫度), duty(×10^5)}`**，
字索引 = `2 + 3×i`（CLKOUT0→2/3/4，CLKOUT1→5/6/7，…），CLKFBOUT 占字 0/1。
这与生成代码的复位值表和厂商模板完全一致：

- `dpm_system_clk_wiz_src_0_clk_wiz_drp.vhd:391-422` 的复位表就是
  `(0)=0x1401, (2)=0x28, (5)=0x28, (6)=0x15F90, (7)=0xC350, (8)=0x1 …`；
- `$XILINX_VIVADO/data/ip/xilinx/clk_wiz_v6_0/ttcl/clk_core_drp_vhd.ttcl:1341-1380`
  写明 `S2_CLKOUT0_PHASE => ram_clk_config(3)(31 downto 0)`、`S2_CLKOUT1_PHASE => ram_clk_config(6)(31 downto 0)`；
- 相位字的单位由 `mmcm_pll_drp_func_7s_mmcm.vh` 的 `mmcm_pll_phase` 入参约定：
  `_PHASE: This is the phase multiplied by 1000`（毫度，范围 ±360000）。

> 因此「CLKOUT1 字 = `0x00005A28`（低字节 40=D、高字节 0x5A=90=相位）」这个早期推断**不成立**：
> 实测 `0x214 = 0x00000028`，相位在相邻的 `0x218 = 0x00015f90`（毫度）。
> `0x5A28` 在任何生成产物里都搜不到；把 90 当成「bit[15:8] 的相位字段」是把
> 「毫度整数字」和「8 位字段」两种编码混在了一起。

---

## 3. 主扫描矩阵：`div × phase_field`

每点写 `0x214 = D | (F << 8)`（按「相位在 bit[15:8]」的假设）、`0x208` 同样写，
并把 `F` 以毫度写进 `0x218`（相位字假设），然后 `Load=0x07` 重配置并测量。
表中是 `delay_ps`（`clk_out2` 相对 `clk_out1`），全部 32 点：

| div | F=0 | F=1 | F=2 | F=4 | F=8 | F=22 | F=45 | F=90 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 40 | 10000 | 10000 | 10000 | 10000 | 10000 | 10000 | 10000 | 10000 |
| 80 | 20000 | 20000 | 20000 | 20000 | 20000 | 20000 | 20000 | 20000 |
| 125 | 31250 | 31250 | 31250 | 31250 | 31250 | 31250 | 31250 | 31250 |
| 96 | 24000 | 24000 | 24000 | 24000 | 24000 | 24000 | 24000 | 24000 |

每个 div 的周期与角度（`DRP_MEAS` + `DRP_PHASE_DEG` 原始行）：

```text
DRP_MEAS div=40  phase_field=2  t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000  locked=1
DRP_PHASE_DEG div=40  phase_field=2  delay_ps=10000  delay_deg_milli=90000  n_delay=31
DRP_MEAS div=80  phase_field=2  t_out1_ps=80000  t_out2_ps=80000  delay_ps=20000  locked=1
DRP_PHASE_DEG div=80  phase_field=2  delay_ps=20000  delay_deg_milli=90000  n_delay=31
DRP_MEAS div=125 phase_field=2  t_out1_ps=125000 t_out2_ps=125000 delay_ps=31250  locked=1
DRP_PHASE_DEG div=125 phase_field=2  delay_ps=31250  delay_deg_milli=90000  n_delay=31
DRP_MEAS div=96  phase_field=2  t_out1_ps=96000  t_out2_ps=96000  delay_ps=24000  locked=1
DRP_PHASE_DEG div=96  phase_field=2  delay_ps=24000  delay_deg_milli=90000  n_delay=31
```

- **周期只跟低字节的 D 有关，且字段就是 D 本体**：D=40/80/125/96 → 40000/80000/125000/96000 ps
  （VCO = 50MHz × 20 / 1 = 1000MHz，`T_out = D × 1ns`）。**不是 `2D`**（若为 2D，D=40 应是 80ns）。
- **相位字段（bit[15:8]）对相位没有任何影响**：每个 div 下 8 个 F 值的 `delay_deg_milli` 恒为 90000。

寄存器读回也证明写进去了（区分「没写进去」和「写进去但被忽略」）：

```text
DRP_REGCHK div=40 phase_field=0 w208=0x00000028
DRP_REGCHK div=40 phase_field=0 w214=0x00000028
DRP_REGCHK div=40 phase_field=0 w218=0x00000000
DRP_REGCHK div=40 phase_field=1 w208=0x00040128
DRP_REGCHK div=40 phase_field=1 w214=0x00000128
DRP_REGCHK div=40 phase_field=1 w218=0x000003e8
DRP_REGCHK div=40 phase_field=90 w208=0x00045a28
DRP_REGCHK div=40 phase_field=90 w214=0x00005a28
DRP_REGCHK div=40 phase_field=90 w218=0x00015f90
```

- `0x214`（CLKOUT1 分频字）**原样存下** `D | (F<<8)`，说明寄存器文件接受了写入；
  但生成代码里只有 `ram_clk_config(5)(7 downto 0)` 被消费（`S2_CLKOUT1_DIVIDE`），高字节无人读。
- `0x208`（CLKOUT0 分频字）bit[15:8] **是小数分频 `FRAC[7:0]` 的一部分**
  （`FRAC` 占 bit[17:8]，`FRAC_EN` 是 bit[18]，写 `0x208` 时硬件自动把 `FRAC_EN` 置成
  `or_reduce(bit[17:8])`），读回里 `0x00040128` 的 bit18=1 就是硬件自动置起的证据。
  并且 `mmcm_frac_count_calc` 里 `clkout0_divide_frac = frac / 125`，
  所以 `F < 125` 时小数分频被截断为 0 —— 这正是实测周期与 F 无关的原因
  （F=125..1000 的区间未扫，属于未验证范围）。

### 3.1 非对称探针（把「字段被忽略」与「字段把两路一起搬移」分开）

矩阵里两个字写同一个 F 时，即使字段真是相位、两路一起搬移，相位差也不变。
所以补了 5 组「两个字写不同 F」的判别实验（D 都是 40，45° 对应 5ns）：

| `0x208` 的 F0 | `0x214` 的 F1 | 候选 A 预期（度/ps 延迟） | 候选 B 预期（45° 整数倍） | **实测 delay** |
| --- | --- | --- | --- | --- |
| 2 | 0 | 39998 ps | 30000 ps | **10000 ps** |
| 4 | 0 | 39996 ps | 20000 ps | **10000 ps** |
| 0 | 4 | 4 ps | 20000 ps | **10000 ps** |
| 0 | 8 | 8 ps | 40000 ps | **10000 ps** |
| 8 | 0 | 39992 ps | 40000 ps | **10000 ps** |

```text
DRP_ASYM div=2 phase_field=0 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_ASYM div=4 phase_field=0 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_ASYM div=0 phase_field=4 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_ASYM div=0 phase_field=8 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_ASYM div=8 phase_field=0 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
```

（`DRP_ASYM` 行里的 `div=` 是写入 `0x208` 的字段值、`phase_field=` 是写入 `0x214` 的字段值，
两字的真实分频都是 40。）

**候选 A 与候选 B 同时被否定**：字段根本没被任何逻辑消费。

### 3.2 只写相位字 `0x218`（毫度）

按「相位字」假设写 `0x218 ∈ {0, 22500, 45000, 90000, 180000, 270000}`（0°…270°）并 `Load`：

```text
DRP_PHASEW div=40 phase_field=0   t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_PHASEW div=40 phase_field=22  t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_PHASEW div=40 phase_field=45  t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_PHASEW div=40 phase_field=90  t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_PHASEW div=40 phase_field=180 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_PHASEW div=40 phase_field=270 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
```

**没有变化**。这与生成代码一致（下面第 5 节）：`S2_CLKOUT1_PHASE` 端口在当前 IP 里根本不存在。

---

## 4. `Load`（`0x25C`）与状态寄存器

### 4.1 Load 语义（实测）

先把 `0x208`/`0x214` 写成 D=96，再用不同 Load 值触发：

```text
DRP_LOAD_PROBE val=0x00000002 div=96 phase_field=0 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_LOAD_PROBE val=0x00000000 div=96 phase_field=0 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_LOAD_PROBE val=0x00000001 div=96 phase_field=0 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_LOAD_PROBE val=0x00000003 div=96 phase_field=0 t_out1_ps=96000 t_out2_ps=96000 delay_ps=24000 delay_deg_milli=90000 locked=1
DRP_LOAD_PROBE val=0x00000007 div=96 phase_field=0 t_out1_ps=96000 t_out2_ps=96000 delay_ps=24000 delay_deg_milli=90000 locked=1
```

- `0x00`、`0x02`（bit0=0）：不触发任何重配置 → 时钟保持上一次的配置（D=96 没生效，仍是 40ns）。
- `0x01`（bit0=1、bit1=0 → SADDR=0）：触发，但用**状态 1（静态 S1 配置）**重写 MMCM
  → 回到 IP 出厂配置 D=40（40ns），寄存器文件里的 D=96 被忽略。
- `0x03` / `0x07`（bit0=1、bit1=1 → SADDR=1）：触发并用**状态 2（寄存器文件动态配置）**
  → D=96 生效（96ns）。两次结果完全一致，说明 **bit2 未使用**，任务书取 `0x07` 是对的。

对应生成代码：`clk_wiz_drp.vhd:553` 的触发条件是写 `0x25C` 且数据 **bit0=1**
（VHDL 里 `Bus2IP_Data(31)='1'`，而 `Bus2IP_Data(0)` 才是 AXI 的 MSB，故对应 AXI 的 bit0），
`SADDR => config_reg(30)` 对应 AXI 写数据的 **bit1**。

### 4.2 状态寄存器在 `0x004`，不在「字 31」

```text
DRP_RD_STATUS off=0x004 val=0x00000001 locked=1（锁定后）
DRP_RD_STATUS off=0x004 val=0x00000000 locked=0（重配置中）
DRP_RD_STATUS off=0x004 val=0x00000001 locked=1（重锁定后）
DRP_RD_STATUS off=0x27c val=0x00000000（寄存器文件 word 31，非状态寄存器）
```

- 硬件状态（`program_status = {srdy, locked}`）在**本地寄存器 `0x004`**：bit0 = `locked`，
  bit1 = `srdy`（只在 `WAIT_LOCK→WAIT_SEN` 那一拍为 1，所以稳定态读到 `0x1`）。
- 文档里写的「DRP 状态字 `DPM_DRP_WORD(31)`」（偏移 `0x27C`）实测**恒为 0**，它是空 RAM。
- 重配置期间 `locked` 会落下：实测 Load 写入后 `locked` 在几百 ns 内变 0，
  约 **8.6µs** 后重新拉高（Safe Clock Startup 再等 8 拍放行输出）。
  因此 PS 必须在测量空闲时改配置，且要等 `0x004` 的 bit0 从 0 回到 1。

---

## 5. 交叉证据：为什么相位写不进去（生成代码层面）

| 证据 | 位置 | 说明 |
| --- | --- | --- |
| DRP 重建用静态相位 | `dpm_system_clk_wiz_src_0_mmcm_pll_drp.v:336-352` | `S2_CLKOUTx = mmcm_pll_count_calc(S2_CLKOUTx_DIVIDE, S1_CLKOUTx_PHASE, S1_CLKOUTx_DUTY)`；`S1_CLKOUT1_PHASE => 90000` 是 `*_clk_wiz_drp.vhd:601` 的**静态 generic** |
| 只有 divide 被接出 | `dpm_system_clk_wiz_src_0_clk_wiz_drp.vhd` 端口映射 | `S2_CLKOUT0_DIVIDE => ram_clk_config(2)(7 downto 0)`、`S2_CLKOUT1_DIVIDE => ram_clk_config(5)(7 downto 0)`，没有任何 `S2_CLKOUTx_PHASE`（`grep -c` = 0） |
| 相位字是独立 32 位字 | `ttcl/clk_core_drp_vhd.ttcl:1341-1380` | `S2_CLKOUT1_PHASE => ram_clk_config(6)(31 downto 0)`——只在 `reconfig_adv=true` 时才生成 |
| `reconfig_adv` 的来源 | `ttcl/variables.ttcl:959` | `set reconfig_adv [get_property PARAM_VALUE.PHASE_DUTY_CONFIG]`；本 IP 的 `PHASE_DUTY_CONFIG=false` |
| 相位单位是毫度 | `mmcm_pll_drp_func_7s_mmcm.vh`（`mmcm_pll_phase`） | 入参是「phase × 1000」，内部 `phase_in_cycles = phase_fixed*divide/360`，拆成 `DELAY_TIME`（整数 VCO 周期）+ `PHASE_MUX`（1/8 VCO 周期） |
| 小数分频的量化 | 同上（`mmcm_frac_count_calc`） | `clkout0_divide_frac = frac / 125`，F<125 被截断为 0 |

---

## 6. 结论与置信度

| 结论 | 置信度 | 依据 |
| --- | --- | --- |
| 写 CLKOUT1 字 bit[15:8] 的「相位字段」不改变 `clk_out2` 相位（恒 90.000°） | **高**（实测 43 组，两种源码模式一致） | 第 3 节 |
| 写 CLKOUT0 字 bit[15:8] 不是相位，而是 FRAC[7:0] 的一部分 | **高**（实测 + 读回 bit18 自动置位 + 代码） | 第 3、5 节 |
| `divide` 字段 = D 本体（不是 2D） | **高**（4 个 D 值实测周期） | 第 3 节 |
| Load：bit0 触发、bit1 = SADDR；`0x07` 正确 | **高**（5 种 Load 值实测） | 第 4.1 节 |
| 状态寄存器在 `0x004`（bit0=locked），字 31 是空 RAM | **高**（实测两种状态） | 第 4.2 节 |
| 寄存器文件布局 = 每输出 3 字 `{divide, phase(毫度), duty}` | **高**（复位 dump + 模板 + 复位值表三方一致） | 第 2 节 |
| 相位字在 `PHASE_DUTY_CONFIG=false` 下未连接 | **高**（代码 + 实测无效果） | 第 3.2、5 节 |
| 置 `PHASE_DUTY_CONFIG=true` 后相位字能生效 | **中**（厂商模板与单位约定明确，但**未实测**，需重建 IP） | 第 5 节 + 第 7 节 |

### 6.1 未确定项

1. **置 `CONFIG.PHASE_DUTY_CONFIG {true}` 重新生成 IP 后，`0x218 = 毫度` 是否真能改变
   `clk_out2` 相位**——本任务不重建 BD/IP，因此未实测，只有厂商模板与 `mmcm_pll_phase`
   的单位约定作为依据。要确认必须重跑 `vivado/create_bd.tcl` 后用同一套激励复测。
2. 启用相位字后硬件对 22.5° 的量化行为未实测。按 `mmcm_pll_phase` 的算法，
   相位分辨率是 **1/8 VCO 周期 = 125ps**（VCO=1000MHz），换算到输出相位是
   **`45/D` 度**：D=40 → 1.125°，22.5° = 2.5ns = **20 步，可精确表示**；
   D=125 → 0.36°，22.5° = 62.5 步，只能取 62/63 步（22.32°/22.68°）。
3. `F = 125..1000`（小数分频真正生效的区间）未扫。
4. `FINE_PSHIFT`（`CLKOUTx_USE_FINE_PS`，1/56 VCO 周期 ≈ 17.9ps）完全未评估。
5. 相位字取负值或 > 360° 的行为未实测（`mmcm_pll_phase` 支持 ±360000，负值按 +360° 折算）。

> **对 `docs/ps-protocol.md` §6 的三处修正**：
> ① 「分频字段 = 2D」→ **D 本体**（已在文档 2025-10 修正中改过，本实测确认）；
> ② 「CLKOUT1 字 `0x00005A28`，相位字段在 bit[15:8]」→ **不存在**，相位是独立 32 位毫度字
>    `0x218`（复位值 `0x00015F90` = 90000）；
> ③ 「相位字段 = 0..7，即 45° 的整数倍，22.5° 无法精确产生」→ 45° 只是 1/8 个 **VCO** 周期
>    在 VCO 自身相位上的角度；对输出相位，网格是 `45/D` 度，D=40 时 22.5° 可以精确表示。

---

## 7. 对 PL/PS 方案的建议（供 Lead 决策）

**当前 IP：`SET_SIG --phase` 必须拒绝。** 频率参数（`MULT`/`DIVCLK`/`D`）可以用下面这条序列在线改：

```text
写 0x200 = (MULT << 8) | DIVCLK               // 例如 0x00001401 = MULT 20 / DIVCLK 1
写 0x208 = D                                  // clk_out1 分频（CLKOUT0）
写 0x214 = D                                  // clk_out2 分频（CLKOUT1）
写 0x25C = 0x07                               // bit0 触发 + bit1=SADDR=1（寄存器文件动态配置）
轮询 0x004 的 bit0（locked）等它 0→1，再等 ≥8 个输出周期（Safe Clock Startup）
```

重配置期间两路输出停止约 9µs，只能在测量空闲时改。

**若要恢复在线相位能力**，最小改动是给 `vivado/create_bd.tcl` 的 `clk_wiz_src` 加
`CONFIG.PHASE_DUTY_CONFIG {true}` 后按 `dpm-vivado-ip` 的顺序重新生成 IP/BD
（`package_ip.tcl` → `create_bd.tcl` → `apply_timing_strategy.tcl` → `build_bitstream.tcl`）。
届时：

```text
写 0x218 = 22500                              // clk_out2（CLKOUT1）相位 = 22.5°（毫度整数）
```

这条路径**未实测**（需要重建 IP，属 `vivado/` 的写入范围）。
**重建之后，验收入口就是同一份激励**：直接复跑 `sim/run_drp_tb.sh`，
此时 `DRP_PHASEW` 行的 `delay_ps` 应随写入的毫度值变化（0/22.5°/45°/90°/180°/270° →
0/2500/5000/10000/20000/30000 ps @ D=40），同时 `phase_controllable` 才能从 `false` 改成 `true`。
如果不想动 IP，另一条路是在 PL 内做相位可编程的信号源（`SignalSource` 的相位寄存器通路已存在），
代价是相位差要自己在测量域里保证。

---

## 8. 附：机器可读输出的行数与格式

| 标签 | 行数 | 含义 |
| --- | --- | --- |
| `DRP_INIT` | 1 | 复位锁定后的初始状态 |
| `DRP_RD_RESET` | 11 | 复位后寄存器文件 dump |
| `DRP_RD_STATUS` | 4 | `0x004` / `0x27C` 状态读取 |
| `DRP_LOAD_PROBE` | 5 | Load 值语义（0x00/0x01/0x02/0x03/0x07） |
| `DRP_MEAS` | **32** | 主扫描矩阵（`div × phase_field`），格式固定 |
| `DRP_PHASE_DEG` | 32 | 上表每点的角度换算 |
| `DRP_REGCHK` | 96 | 每点的 `0x208`/`0x214`/`0x218` 读回 |
| `DRP_ASYM` | 5 | 非对称相位字段探针（判别实验） |
| `DRP_PHASEW` | 6 | 只写相位字 `0x218`（毫度） |
| `DRP_TB_DONE` | 1 | 结束标记，`lines=<DRP_MEAS 行数>` |

`DRP_MEAS` 固定格式：

```text
DRP_MEAS div=<D> phase_field=<F> t_out1_ps=<整数> t_out2_ps=<整数> delay_ps=<整数> locked=<0/1>
```
