# DPM 串口协议（PS ↔ PC 上位机）

本文件是 **PS 侧软件**（`ps/`，Zynq PS 裸机 C）与 **PC 上位机**（`host/`，Python）之间的接口契约。
两侧必须严格实现本文件；改动协议前先改本文件，再同步两侧实现。
本文件中的**所有数值参数都是冻结值**（频率表、相位表、默认闸门时间都在这里写死），
验证脚本按这些数值生成期望值。

## 1. 物理层

| 项目         | 值                                                                 |
| ------------ | ------------------------------------------------------------------ |
| 接口         | Zynq PS 的 UART（默认 UART0，基地址 `0xE0001000`，MIO48/49）         |
| 波特率       | `115200`                                                           |
| 帧格式       | 8 数据位、无校验、1 停止位（8N1）                                   |
| 流控         | 无                                                                 |
| 字节序       | 帧内多字节字段一律**小端**（与 AXI 寄存器一致）                     |

> 当前 BD 尚未使能 PS UART（`PCW_EN_UART0/1 = 0`、MIO 全部 unassigned），
> 上板前需要按 `docs/bringup.md` 打补丁并重建 bitstream。

## 2. 帧格式

帧 = `SOF` + `LEN` + `CMD` + `PAYLOAD` + `CRC16`，字段之间无填充、无转义：

```text
偏移  长度  字段      说明
0     1     SOF       固定 0xA5
1     1     LEN       CMD + PAYLOAD 的字节数（1..64）
2     1     CMD       命令/响应码
3     LEN-1 PAYLOAD   命令参数或响应数据
3+LEN 2     CRC16     CRC-16/MODBUS（poly 0x8005 反射、init 0xFFFF），
                      计算范围 = LEN、CMD、PAYLOAD（不含 SOF），低字节在前
```

- 帧总长 = `4 + LEN`，最大 68 字节。
- 接收方按状态机同步：等待 `0xA5` → 读 `LEN`（校验范围）→ 收 `LEN+2` 字节 → 校验 CRC。
  CRC 错或 `LEN` 越界时丢弃本帧、回到「等待 SOF」状态，**不回响应**（上行另有计数器可查）。
- 字符化辅助形式（只用于人工调试与日志，不作为两侧实现依据）：
  `A5 06 01 00 02 34 12 CRC_L CRC_H` 表示 `SET_MODE(2)`。

## 3. 命令表

`CMD` 的编码空间划分：

| 范围        | 用途                                            |
| ----------- | ----------------------------------------------- |
| `0x01..0x0E`| 请求（PC → PS）                                 |
| `0x81..0x8E`| 响应（PS → PC），值 = `请求码 \| 0x80`           |
| `0xE1..0xE2`| 通知（PS → PC 主动上报，无需请求）               |
| `0xFE/0xFF` | 错误响应 `ERR_PARAM` / `ERR_BUSY`                |

| CMD  | 名称             | 方向 | 请求 PAYLOAD                | 响应 PAYLOAD（按顺序）                                              |
| ---- | ---------------- | ---- | --------------------------- | ------------------------------------------------------------------- |
| 0x01 | `PING`           | R/R  | 空                          | 无                                                                  |
| 0x02 | `IDENTIFY`       | R/R  | 空                          | `ver(u32)` `gitRev(u32)` `caps(u32)` `versionReg(u32)` `capsReg(u32)` `dspClkHz(u32)` `slowClkHz(u32)` `flags(u16)` |
| 0x03 | `GET_STATUS`     | R/R  | 空                          | `status(u32)` `sigStatus(u32)` `gateTarget(u32)` `lostCount(u32)` `seq(u32)` |
| 0x04 | `START`          | R/R  | 空                          | 无                                                                  |
| 0x05 | `GET_RESULT`     | R/R  | 空                          | `valid(u8)` `seq(u32)` `gateCnt(u32)` `edgeCnt(u32)` `phaseCnt(u32)` |
| 0x06 | `GET_MEAS`       | R/R  | 空                          | `seq(u32)` `gateCnt(u32)` `edgeCnt(u32)` `phaseCnt(u32)` `freqMilliHz(u32)` `phaseMilliDeg(u32)` |
| 0x07 | `SET_GATE`       | R/R  | `gateTarget(u32)`           | `gateTarget(u32)`                                                   |
| 0x08 | `SET_MODE`       | R/R  | `mode(u8)`                  | `mode(u8)`                                                          |
| 0x09 | `SET_SOURCE`     | R/R  | `source(u8)`                | `sigStatus(u32)`                                                    |
| 0x0A | `SET_SIG`        | R/R  | `freqHz(u32)` `phaseMilliDeg(u32)` | `freqHz(u32)` `phaseMilliDeg(u32)` `vcoHz(u32)` `div(u16)` `phaseField(u8)` |
| 0x0B | `GET_SIG`        | R/R  | 空                          | `freqHz(u32)` `phaseMilliDeg(u32)` `vcoHz(u32)` `div(u16)` `phaseField(u8)` |
| 0x0C | `CLEAR`          | R/R  | 空                          | 无                                                                  |
| 0x0D | `STREAM`         | R/R  | `enable(u8)` `intervalMs(u16)` | `enable(u8)` `intervalMs(u16)`                                   |
| 0x0E | `RESET_COUNTERS` | R/R  | 空                          | 无                                                                  |

### 3.0 语义约定（避免两侧各写一套）

- **`SET_SOURCE` 同时负责使能**：`source = 1` ⇒ 写 `SIG_CTRL = enable|srcSelect`（使能并选中片内源）；
  `source = 0` ⇒ 写 `SIG_CTRL = 0`（关掉片内源并使能失效、走外部引脚）。
  这样上位机不需要额外的 enable 命令；PL 的 `SIG_CTRL` 位域不变。
- **`START` 在不能启动时回 `ERR_BUSY`**（0xFF）：已经在测量中、或选中的片内源不可用
  （未使能/未锁定）时都算 `ERR_BUSY`。PL 内部对 `busy` 期间的 `CTRL.start` 是**忽略**，
  固件多回一个错误码对调用方等价且更有信息量；上位机应把 `ERR_BUSY` 当作
  「已在测量中」而不是致命错误。
- **结果就绪的权威探针是 `GET_RESULT.valid`（或 `GET_MEAS` 的序号变化）**，
  不是 `STATUS.resultValid`：仿真后端的事件驱动模型里，结果锁存与状态位更新可能落在
  同一条命令内部的两次 AXI 读之间，只轮询 `STATUS` 可能看不到 valid 恰好为 1 的那一拍。
  `STATUS.resultValid` 仍然按 PL 语义实现（有未读走的结果时为 1），只是不适合当唯一判据。
- **`IDENTIFY` 的字段口径**：`capsReg` / `versionReg` 是**从 PL 读回的原始寄存器值**
  （`CAPS = 0x00030104`、`VERSION = 0x44504D02`）；`caps` 是固件自己的能力字，
  按 `DPM_FLAG_*` 组合；`flags` 的 b3（中断驱动）只有在固件真的实现了中断路径时才置位，
  轮询实现应清 0。`SET_SIG` 回读的 `phaseField` 在相位不可控时应回 `0`
  （它表示"写入的相位字段/步数"，当前 bitstream 下没有意义）。

### 3.1 枚举值

`mode`：

| 值 | 含义                                                                 |
| -- | -------------------------------------------------------------------- |
| 0  | `single`：收到 `START` 才启动一次测量                                 |
| 1  | `continuous`：结果读走后**立即**重新启动下一次测量（PS 侧软件轮转）   |

`source`：

| 值 | 含义                       |
| -- | -------------------------- |
| 0  | 外部引脚输入（`sig1`/`sig2`） |
| 1  | 片内信号源（MMCM 两路输出）    |

`flags`（`IDENTIFY` 响应）：b0 片内信号源可用、b1 支持连续模式、b2 支持流模式、b3 中断驱动。

### 3.2 响应码

| 响应码        | 含义                                                     |
| ------------- | -------------------------------------------------------- |
| `cmd \| 0x80` | 成功，PAYLOAD 按上表                                      |
| 0xFE         | `ERR_PARAM`：参数非法（频率/相位/模式/源超范围、字长不符）  |
| 0xFF         | `ERR_BUSY`：当前状态不允许该命令（例如 MMCM 未锁定就选片内源） |

错误响应 PAYLOAD 为空。`ERR_PARAM` 情况下 PS 侧不改变任何硬件状态。

### 3.3 通知帧（PS → PC，主动上报）

| CMD  | 名称            | PAYLOAD                                                                 |
| ---- | --------------- | ----------------------------------------------------------------------- |
| 0xE1 | `STREAM_DATA`   | 与 `GET_MEAS` 相同，另加 `lostDelta(u16)`（自上次上报以来新增的丢失计数） |
| 0xE2 | `EVENT`         | `code(u8)` `detail(u32)`：`code` 1 = 结果丢失，2 = MMCM 失锁并恢复，3 = 上位机未及时读取 |

> **为什么通知码取 0xE1/0xE2（v1.1 修正）**：响应码规则是「请求码 | 0x80」，而请求码占
> 0x01..0x0E，于是 `PING` 的响应恰好是 0x81、`IDENTIFY` 的响应是 0x82 ——
> 与旧版通知码 0x81/0x82 **完全撞码**，只能靠 PAYLOAD 长度猜（STREAM_DATA 恒 26B、
> EVENT 恒 5B、PING 响应 0B、IDENTIFY 响应 30B）。这在第三方实现里极易出错，
> 因此把通知码整体挪到 **0xE1/0xE2**：`0xE0..0xEF` 保留给 PS 主动上报，
> `0x81..0x8E` 只用于响应，两者不再有交集。
> 帧的其它字段与校验方式不变。

## 4. 通道/字节选通语义（PS 侧实现要求）

- 所有寄存器写都是 32 位整字写；`GATE_TARGET` 与 `SIG_CTRL` 在 PL 内按字节选通合并，
  PS 侧一律整字写（`strb = 0xF`），不要依赖部分字节写。
- `CTRL` 的 b1（irqEnable）是普通读写位：**每次写 CTRL 都要把当前 irqEnable 一起写回**，
  启动测量写 `DPM_CTRL_IRQ_ENABLE | DPM_CTRL_START`。
- `IRQ_STATUS` 是 W1C：结果读走后写 `DPM_IRQ_PENDING | DPM_IRQ_RESULT_READ`
  （可选加 `DPM_IRQ_LOST_CLEAR`）。**不写 b1 会导致下一次结果被记为丢失**。
- PL 的写通道在配置快照未送到测量域前会拉低 `ready`，AXI 写会被挂起；
  PS 侧应轮询 `SIG_STATUS` 的 `sourceReady` 后再发 `START`，或在 `SET_SOURCE`/`SET_SIG`
  返回前确认 `SIG_STATUS` 已更新。

## 5. 频率与相位的计算（两侧必须一致）

设低速（AXI）时钟 `f_slow`、高速（DSP）时钟 `f_dsp`、一次结果的 `gateCnt`/`edgeCnt`/`phaseCnt`。

```text
测量窗口时长      T_win = gateCnt / f_slow                 [s]
被测信号频率      f_sig = edgeCnt / T_win = edgeCnt * f_slow / gateCnt
绝对相位差        t_phase = (phaseCnt / edgeCnt) / f_dsp
相位差（度）      phase_deg = 360 * t_phase * f_sig
                            = 360 * phaseCnt * f_sig / (edgeCnt * f_dsp)
                        （代入 f_sig = edgeCnt * f_slow / gateCnt，约去 edgeCnt：）
                            = 360 * phaseCnt * f_slow / (gateCnt * f_dsp)
```

定点输出（整数，避免浮点；**整除截断**，即向下取整，两侧必须一致）：

```text
freqHz        = edgeCnt * f_slow / gateCnt                        （整除截断，单位 Hz）
phaseMilliDeg = phaseCnt * f_slow * 360000 / (gateCnt * f_dsp)     （整除截断，单位毫度）
gateCnt == 0 或 edgeCnt == 0 → 两者都为 0
```

**字段单位（冻结）**：

| 字段 | 单位 | 25MHz / 90° 时的值 | 说明 |
| ---- | ---- | ------------------ | ---- |
| `freqMilliHz` | **Hz 整数** | `25000000` | 名字是历史遗留；真正的毫赫兹在 25MHz 时是 `2.5e10`，会溢出 u32。1Hz 分辨率已远优于测量精度（40µs 闸门下 `edgeCnt` 的 ±1 计数约对应 1~10kHz 不确定度）。 |
| `phaseMilliDeg` | 毫度（1e-3°） | `90000` | 32 位足够（上限 4.29e6 度） |

> **实现必须用下面的等价形式**（数学上逐位相同，但 64 位中间量不会溢出；已用随机用例
> 与直接公式逐位比对验证）：
>
> ```text
> # 频率：直接整数乘除（f_slow ≤ 100MHz 时 edge*f_slow 不会越过 2^64）
> freqHz = (edgeCnt * f_slow) / gateCnt      # 整除
>
> # 相位：按整除分解后再乘，且用 kHz 量级的时钟频率（比值不变）
> c = phaseCnt / gateCnt ; d = phaseCnt % gateCnt
> v = 360000 * c + (360000 * d) / gateCnt
> phaseMilliDeg = v * (f_slow / 1000) / (f_dsp / 1000)
> ```
>
> 为什么必须这样写：`edgeCnt * f_slow * 1000` 在 `edgeCnt ≈ 3.7e8` 时就越过 2^64
> （`f_slow = 50MHz`），`360000 * phaseCnt * f_slow` 的上限更高（phaseCnt 是 32 位累加值）。
> 上面的形式把中间量限制在 `edgeCnt*f_slow ≤ 2.1e17` 与 `v*(f_slow/1000) ≤ 4.3e17` 的规模内
> （`edgeCnt` 是 32 位、`f_slow = 50MHz` 时 `edgeCnt*f_slow ≤ 2.1e17 < 2^64`）。
> 要求 `f_slow` 与 `f_dsp` 都能被 1000 整除（默认 50MHz/400MHz 满足；换频点时要检查）。
>
> 本仓库实测到过一次「公式差 1000 倍」的实现错误（把 `f_slow*1000` 写成先乘 1000 再除），
> 因此两侧单测与 `spec/selftest_spec.py` 都必须用 `spec/golden/vectors.json` 的定点输出
> **逐位**校验，而不是只看量级。

`gateCnt == 0` 或 `edgeCnt == 0` 时，`freqMilliHz = 0`、`phaseMilliDeg = 0`，
并且 `GET_MEAS` 仍返回 `gateCnt/edgeCnt/phaseCnt` 原值（便于诊断「源没边沿」）。

精度说明（写进验证报告时必须区分）：

- 相位量化下界 = 1 个 DSP 周期，`f_dsp = 400MHz` 时为 2.5ns；
  折算成度数 = `360 * f_sig / f_dsp`（25MHz 时 22.5°，10MHz 时 9°）。
  `phaseCnt/edgeCnt` 的小数部分是量化误差在多个参考沿上的平均，不属于缺陷。
- 频率由 `edgeCnt/gateCnt` 得到，窗口边界不整除时会多算/少算 1 个沿；
  相对误差量级 `1/edgeCnt`（`edgeCnt = 1000` 时约 0.1%）。
- 判据见 `.agents/skills/dpm-measurement-model`：相位判据优先写在 `phaseCnt/edgeCnt` 上，
  容限 `±1` 个 DSP 周期；频率容限按 `±(2/edgeCnt)` 量级给。

## 6. 片内信号源的参数换算（PS → MMCM DRP）

MMCM 的结构：`VCO = f_in * MULT / DIVCLK`，`f_clkoutN = VCO / D_N`。

### 6.1 DRP 寄存器文件（实测，2025-10 由 `sim/tb_clk_wiz_src_drp.sv` 在 xsim 上测得）

每个输出占 **3 个字**：{分频, 相位（毫度）, 占空比}。字索引 = `2 + 3i`（i = 0 → clk_out1，i = 1 → clk_out2）。

| 字索引 | 字节偏移 | 复位值     | 含义                                      |
| ------ | -------- | ---------- | ----------------------------------------- |
| 0      | 0x200    | 0x00001401 | CLKFBOUT：`MULT = 0x14 = 20`、`DIVCLK = 1` |
| 2      | 0x208    | 0x00000028 | clk_out1 分频 = 0x28 = **40 = D 本体**     |
| 3      | 0x20C    | 0x00000000 | clk_out1 相位（**整数毫度**）              |
| 4      | 0x210    | 0x0000C350 | clk_out1 占空比（50000 = 50%）             |
| 5      | 0x214    | 0x00000028 | clk_out2 分频 = 40 = D                     |
| 6      | 0x218    | 0x00015F90 | clk_out2 相位 = 90000 毫度 = 90.000°       |
| 7      | 0x21C    | 0x0000C350 | clk_out2 占空比 = 50%                      |

- **Load 寄存器在 0x25C**：bit0 = 触发、bit1 = SADDR（0 = 用 BD 静态配置 S1，1 = 用寄存器文件 S2）。
  实测 `0x03`（S1）与 `0x07`（S2）在频率上结果一致；写 `0x01` 会回到静态配置的 D=40。
- **状态寄存器在偏移 0x004**（不是「字 31 / 0x27C」，后者恒为 0）：bit0 = locked。
- 重配置约 **9µs**，期间 `locked = 0`、两路输出停止；`SIG_STATUS.srcLocked` 会跟着变化。
- **分频字段 = D 本体**（低字节），不是 `2D`：`0x28 = 40 = D`。实测 D = 40/80/96/125
  分别得到周期 40000/80000/96000/125000 ps（VCO = 1000MHz）。

### 6.2 上一版 bitstream 的限制：**相位不可在线改**（方案 A 之后已作废）

> **本节的结论针对上一版 `clk_wiz_src` 方案，现在不适用**：当前片内源是「裸 MMCM +
> 极简 DRP 控制器」，PS 写的是 **MMCM 的原始 16 位寄存器**（`0x43C1_0000` 段的
> `0x08..0x24` + `0x28` LOAD），不再经过下面这个 `mmcm_pll_drp.v` 的换算，
> 因此「寄存器文件里的相位字没接到重建逻辑」这条限制不存在。
> 是否真的生效仍需上板确认（见 `docs/bringup.md` 第 4 节）。

`mmcm_pll_drp.v` 重建 MMCM 配置字时，各输出的相位取自**静态 generic**（`S1_CLKOUTx_PHASE`），
寄存器文件里的相位字（0x20C / 0x218）**没有接到重建逻辑**：
`S2_CLKOUT1 = mmcm_pll_count_calc(S2_CLKOUT1_DIVIDE, S1_CLKOUT1_PHASE, ...)`。
xsim 实测 **43 组**（CLKOUT0/CLKOUT1 分频字、相位字、非对称组合、毫度相位字）：
`clk_out2` 相对 `clk_out1` 的延迟**恒为 `T_out/4` = 90.000°**，与写入值无关。
因此：

- **频率可以在线改**（分频字有效），`SET_SIG` 的频率部分按 6.1 实现；
- **相位不可在线改**：那一版 bitstream 上被测信号的相位差恒为 **90°**（BD 初值）。
  PS 侧仍然照写相位字（无害），但**不要**声称它能生效；`spec/drp_semantics.json` 的
  `phase_controllable = false` 是那一版的结论。
- 当时设想的解锁办法是给 `clk_wiz_src` 加 `CONFIG.PHASE_DUTY_CONFIG {true}` 后重建 IP/BD
  （那个补丁脚本 `patch_enable_src_phase.tcl` 已随 `clk_wiz_src` 一起删除）；
  现在换成方案 A 后，相位由 MMCM 相位寄存器直接给出，分辨率仍是 `45/D` 度（D=40 时 1.125°）。

### 6.3 频率求解（冻结）

**冻结的验证频点表**（验证脚本按此表取点；PS 侧的 `SET_SIG` 必须精确产生这些频率）：

| 目标 f_sig | VCO (MHz) | MULT | DIVCLK | D   | 分频字（低字节） |
| ---------- | --------- | ---- | ------ | --- | ---------------- |
| 5 MHz      | 600       | 12   | 1      | 120 | 120              |
| 6.25 MHz   | 750       | 15   | 1      | 120 | 120              |
| 8 MHz      | 1000      | 20   | 1      | 125 | 125              |
| 10 MHz     | 1000      | 20   | 1      | 100 | 100              |
| 12.5 MHz   | 1000      | 20   | 1      | 80  | 80               |
| 20 MHz     | 1000      | 20   | 1      | 50  | 50               |
| 25 MHz     | 1000      | 20   | 1      | 40  | 40               |

**求解规则**（冻结，与 `spec/gen_golden.py::solve_sig`、`ps/src/dpm_sig.c::dpm_sig_solve`
同一实现，**不要硬编码频点表**）：

1. `DIVCLK = 1`、`MULT = 12..24` 整数、`VCO = 50MHz × MULT ∈ [600, 1200] MHz`、
   `D ∈ 1..127`（分频字段 8 位可到 255，但硬上限取 127）、不使用小数分频；
2. 先最小化频率误差 `|VCO/D - f_target|`（整数交叉相乘比较，别用浮点）；
3. 同误差（含多个精确解）时取 `cost = |VCO - 1000MHz|/1MHz + |D - 40|` **最小**者。

上表就是该规则的输出：≥ 7.9MHz 的频点都落在 1000MHz 的 `MULT=20`；
5MHz 与 6.25MHz 因 1000MHz 下需要 D=200/160（超硬上限）而分别落到 600MHz 与 750MHz。

**相位扫描点（验证用，度）**：`0, 45, 90, 135`（全部 < 180°，满足相位映射前提
`offset < T_sig/2`）。注意在**当前 bitstream** 下相位差恒为 90°（见 6.2），
这四个点只有在打好 `PHASE_DUTY_CONFIG` 补丁后才是真正可设定的相位。

### 6.4 写入流程（PS 侧 `SET_SIG`）

1. 写 CLKFBOUT 字（`DPM_DRP_WORD(0)`）= `(MULT << 8) | 1`；
2. 写 clk_out1 分频字（0x208）= `D`；
3. 写 clk_out2 分频字（0x214）= `D`；
4. 写 clk_out1 相位字（0x20C）= `0`、clk_out2 相位字（0x218）= `phase_mdeg`
   （**当前 bitstream 下不生效**，见 6.2；补丁后生效）；
5. 写 Load（0x25C）= `0x07`；
6. 轮询 DRP 状态（偏移 0x004）bit0 与 DPM 的 `SIG_STATUS.srcLocked`；
   重配置期间两路输出停止，因此**必须等 `STATUS.busy = 0`** 时才写 DRP。

## 7. 默认参数

| 参数             | 默认值                                    |
| ---------------- | ----------------------------------------- |
| 波特率           | 115200                                    |
| 闸门目标         | 2000 个低速周期（50MHz → 40µs）            |
| 模式             | `single`                                  |
| 输入源           | 片内信号源（`source = 1`）                 |
| 片内源频率/相位  | 25.000 MHz / 90.000°（与 BD 初值一致）      |
| 流模式           | 关闭，`intervalMs = 100`                   |
| 中断使能         | 打开（`CTRL.irqEnable = 1`）               |
