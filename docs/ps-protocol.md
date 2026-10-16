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
- **「还没有结果」用 `valid = 0` 表达，不要用 `ERR_BUSY`**：`GET_RESULT` 的 `valid` 字段就是为
  这个状态准备的。测量还没完成——**包括「选中的输入源没有边沿、测量永不完成」这一正常输入
  状态**（切到没接信号的外部引脚时必现，见 `docs/verify-plan.md` 第 3 节用例 5）——都必须回
  `valid = 0`（其余字段可为 0），**不得回 `ERR_BUSY`**。`ERR_BUSY` 的语义是「当前状态不允许该
  命令」（未锁定就选片内源、测量中再次 `START`）；把它用在「暂无结果」上，会让按本协议写的
  上位机把它当致命错误中止整段测量程序。实测踩到过：切到未接信号的外部输入源后 `START`，
  `GET_RESULT` 回 `0xFF`，上位机报
  「设备错误：PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）」。
  PS 侧可以为方便同步式调用方在命令**内部**做有界等待（当前实现 `50 + 4 × 闸门时间` ms），
  但等待超时后仍要回 `valid = 0`。
- **`GET_MEAS` 没有 `valid` 字段**，因此「暂无结果」只能用 `ERR_BUSY` 表达，这是与本条不冲突的
  例外；上位机必须把它当「继续轮询」，而不是致命错误（本仓库 `DpmClient.wait_meas` /
  `wait_result` 对 `GET_MEAS` 与 `GET_RESULT` 两条路径一视同仁：`ERR_BUSY` = 还没有结果，
  轮询到自己的超时再报「等待测量结果超时」并给出输入源诊断）。
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
| 0xFF         | `ERR_BUSY`：当前状态不允许该命令（例如 MMCM 未锁定就选片内源、测量进行中再次 `START`）；**不用来表达「暂无结果」**（见 §3.0） |

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

## 6. 片内信号源的参数换算（PS → MMCM 寄存器镜像）

MMCM 的结构：`VCO = f_in * MULT / DIVCLK`，`f_clkoutN = VCO / D_N`。

**方案 A（当前实现）**：PL 侧只有一块**极简 DRP 控制器**（`dpm_src_drp`，AXI 段
`0x43C1_0000`）——它把 8 个 16 位寄存器**镜像**按固定顺序搬进 MMCM 的 DRP 口，
**不做任何算术**。因此「毫度 / MHz → MMCM 计数器位域」的换算全部在 PS 软件里完成，
两侧（`ps/src/dpm_sig.c` 与 `host/dpm_host/sig.py`）必须逐位一致。

### 6.1 寄存器映射（`0x43C1_0000`，4KB 段）

| 偏移 | 名称        | 说明                                              |
| ---- | ----------- | ------------------------------------------------- |
| 0x00 | ID          | 只读，固定 `0x44 50 4D 03`（"DPM3"）               |
| 0x04 | STATUS      | b0 = MMCM `locked`、b1/b2 = 重配置进行中            |
| 0x08 | CLKFBOUT    | MMCM DADDR 0x14（ClkReg1）                        |
| 0x0C | CLKFBOUT2   | MMCM DADDR 0x15（ClkReg2）                        |
| 0x10 | CLKOUT0     | MMCM DADDR 0x08（clk_out1，参考输出）              |
| 0x14 | CLKOUT0_2   | MMCM DADDR 0x09（clk_out1 的小数字，不用 → 写 0）  |
| 0x18 | CLKOUT1     | MMCM DADDR 0x0A（clk_out2，移相输出）              |
| 0x1C | CLKOUT1_2   | MMCM DADDR 0x0B（clk_out2 的 EDGE/NO_COUNT/DELAY） |
| 0x20 | DIVCLK      | MMCM DADDR 0x16，`DIVCLK_DIVIDE = 1` → `0x1041`    |
| 0x24 | POWER       | MMCM DADDR 0x28，复位值 `0xFFFF`                   |
| 0x28 | LOAD        | 写 1 触发一次完整重配置（W1S）                      |

写寄存器镜像**不立即生效**，写 `LOAD = 1` 才整批生效。

### 6.2 MMCM 寄存器编码（软件侧）

位域取自 XAPP888 v1.8 的 Table 1/2，相位部分另按厂商实现
`mmcm_pll_drp_func_7s_mmcm.vh` 的 `mmcm_pll_phase` / `mmcm_pll_count_calc` 核对：

```text
ClkReg1 = {PHASE_MUX[15:13], 1'b1, HIGH_TIME[11:6], LOW_TIME[5:0]}
ClkReg2 = {EDGE[7], NO_COUNT[6], DELAY_TIME[5:0]}

HIGH_TIME  = D / 2，LOW_TIME = D - HIGH_TIME（50% 占空比，各 6 位 → D ≤ 126）
EDGE       = D & 1（奇数分频才置位；D == 1 时按厂商做法清 0）
NO_COUNT   = 1 当且仅当 D == 1
m          = round(phase_mdeg × D / 45000)   ← 9 位相位码，单位 **1/8 VCO 周期**
DELAY_TIME = m >> 3（相位码的**整数部分**，单位 VCO 周期）
PHASE_MUX  = m & 0x7（相位码的**小数部分**，单位 1/8 VCO 周期）
```

因此：

- **可实现的相位网格 = 1/8 VCO 周期 = `45°/D`**（D=40 → 1.125°，D=125 → 0.36°），
  不是 45°：22.5° 在 D 为偶数（D=40/50/80/100/120/126）时都能精确表示；
- **可表示的相位上界与 D 有关**：`m` 只有 9 位，`m ≤ 511` ⟺ `phase_mdeg × D ≤ 23017499`，
  即 `phase_max = 511 × 45°/D`，再被满量程 360° 截断：

  | D    | 对应频点        | 相位上界（毫度） | 弧度口径     |
  | ---- | --------------- | ---------------- | ------------ |
  | 32   | 31.25MHz        | 359999           | 整个 0~360°  |
  | 40   | 25MHz           | 359999           | 整个 0~360°  |
  | 64   | 15.625MHz       | 359648           | 359.648°     |
  | 100  | 10MHz           | 230174           | 230.174°     |
  | 120  | 5/6.25MHz       | 191812           | 191.812°     |
  | 125  | 6.4/8MHz        | 184139           | 184.139°     |

  请求**超过**这个上界时，`PHASE_MUX`/`DELAY_TIME` 两个字段会按位截断，输出相位
  **回绕**到另一个值（旧 bitstream 实测：8MHz 写 270° → 相位码 750 → 截成 238 步 →
  85.68°，见 `docs/onboard-ax7010.md` §5），因此固件用 `dpm_mmcm_phase_code_fits`
  显式返回 `ERR_PARAM`，不静默改相位。**旧规则「请求必须 < 180°」已作废**：它比硬件
  保守得多，会把 D ≤ 63 的频点的 180°~360° 白扔掉；相位计测的是无符号间隔
  （sig1 沿 → 下一个 sig2 沿，落在 `(0, 360°]`），因此 >180° 的相移是可测的；
- `clk_out1` 的相位恒 0，相位差只加在 `clk_out2`（CLKOUT1）上。

### 6.3 重配置时序（**必须先拉高 MMCM 的 RST**）

MMCM 的 DRP 写要求器件处于复位状态，且相位延迟单元只在复位/重新锁定时重新初始化。
PL 侧的 `dpm_src_drp` 因此在收到 `LOAD` 后按下面的顺序自己完成整条时序（软件只管写
镜像 + `LOAD`）：

1. 拉高 MMCM 的 `RST`（`dpm_mmcm_src.drpReset`，与系统复位相或）；
2. 按顺序写 8 笔 DRP：`CLKFBOUT → CLKFBOUT2 → CLKOUT0 → CLKOUT0_2 → CLKOUT1 →
   CLKOUT1_2 → DIVCLK → POWER`（每笔 DEN/DWE 各一拍，等 DRDY）；
3. 放开 `RST`，MMCM 重新锁定（`locked` 先掉后起，期间两路输出停止）；
4. 软件轮询 DRP 段 `STATUS`（偏移 `0x04`）的 **bit0 = locked**（或 DPM 的
   `SIG_STATUS.srcLocked`）确认完成。

> **上板实测（2026-10-16）**：不拉这个复位时，**分频字立即生效、相位字不生效**
> （写相位字段后输出相位纹丝不动）。见 `docs/onboard-ax7010.md`。

### 6.4 频率求解（冻结）

**冻结的验证频点表**（验证脚本按此表取点；PS 侧的 `SET_SIG` 必须精确产生这些频率）：

| 目标 f_sig | VCO (MHz) | MULT | DIVCLK | D   | 分频比 |
| ---------- | --------- | ---- | ------ | --- | ------ |
| 5 MHz      | 600       | 12   | 1      | 120 | 120    |
| 6.25 MHz   | 750       | 15   | 1      | 120 | 120    |
| 8 MHz      | 1000      | 20   | 1      | 125 | 125    |
| 10 MHz     | 1000      | 20   | 1      | 100 | 100    |
| 12.5 MHz   | 1000      | 20   | 1      | 80  | 80     |
| 16 MHz     | 800       | 16   | 1      | 50  | 50     |
| 20 MHz     | 1000      | 20   | 1      | 50  | 50     |
| 25 MHz     | 1000      | 20   | 1      | 40  | 40     |
| 40 MHz     | 1000      | 20   | 1      | 25  | 25     |
| 50 MHz     | 1000      | 20   | 1      | 20  | 20     |

**求解规则**（冻结，与 `spec/gen_golden.py::solve_sig`、`ps/src/dpm_sig.c::dpm_sig_solve`
同一实现，**不要硬编码频点表**）：

1. `DIVCLK = 1`、`MULT = 12..24` 整数、`VCO = 50MHz × MULT ∈ [600, 1200] MHz`、
   `D ∈ 1..126`（分频编码成 `HIGH_TIME + LOW_TIME` 各 6 位，最大值 63+63=126；
   127 的 `LOW_TIME = 64` 会被截断成 63，输出频率差一倍）、不使用小数分频；
2. 先最小化频率误差 `|VCO/D - f_target|`（整数交叉相乘比较，别用浮点）；
3. 同误差（含多个精确解）时取 `cost = |VCO - 1000MHz|/1MHz + |D - 40|` **最小**者。

上表就是该规则的输出（每个频点都是误差 0 的精确解，由 `spec/gen_golden.py` 断言）：
≥ 15MHz 的频点落在 1000MHz 的 `MULT=20`；16MHz 走 800MHz 的 `MULT=16`；
5MHz/6.25MHz 因 1000MHz 下需要 D=200/160（超硬上限）而分别落到 600MHz 与 750MHz。
选频点还有两条硬约束：

1. **信号周期是整数 ps**（`D×10^12/VCO`），金标准生成器因此全程整数运算——7.5/15/30MHz
   这类含因子 3 的频率周期不是整数 ps，不入表；
2. **信号周期是 DSP 周期（2.5ns）的整数倍**，即 `8D / MULT` 为整数：此时每个信号周期的
   相位计数窗口都是同一个整数，`phaseCnt % edgeCnt == 0` 严格成立。非整数时窗口在 k 与
   k+1 之间交替（xsim 联调实测：6.4MHz 用 VCO=800/D=125 → `phaseCnt % edgeCnt = 129`），
   金标准的 `phaseCnt = round(相位/T_dsp)×edgeCnt` 就只是近似值，这类频率（6.4/15.625/
   31.25MHz 等）一律不入冻结表。

注意 40MHz/50MHz 的 1 个 DSP 周期分别等于 36°/45°，相位判定只有这一档分辨率
（频率判定不受影响）；>180° 的用例在 50MHz 上恰好落在整数 DSP 周期（225°/270°/315° =
5/6/7 个周期），因此仍然精确。

**相位扫描点（验证用，度）**：45° 的整数倍 `0/45/90/135/180` 是每个频点的必测点；
`22.5°` 在 D 为偶数时精确落在 `45°/D` 网格上（另见 §6.2 的网格）。>180° 的测试点
**逐频点不同**（上界 = 9 位相位码，见 §6.2）：凡是相位码 ≤ 511 的 45° 倍数
（`225/270/315`）都可测；某个频点一个都测不了时（D ≥ 103），改用**相位码上界点**
（`m = 511`）兜底，保证每个频点都有 >180° 的用例。上界点与各频点相位点表由
`spec/gen_golden.py::phase_points_for` 生成、写入 `spec/golden/vectors.json`：

| 目标 f_sig | D   | 相位上界 | 相位扫描点（度）                     |
| ---------- | --- | -------- | ------------------------------------ |
| 5 MHz      | 120 | 191.812° | 0, 22.5, 45, 90, 135, 180, 191.625   |
| 6.25 MHz   | 120 | 191.812° | 0, 22.5, 45, 90, 135, 180, 191.625   |
| 8 MHz      | 125 | 184.139° | 0, 45, 90, 135, 180, 183.96          |
| 10 MHz     | 100 | 230.174° | 0, 22.5, 45, 90, 135, 180, 225       |
| 12.5 MHz   | 80  | 287.718° | 0, 22.5, 45, 90, 135, 180, 225, 270  |
| 16 MHz     | 50  | 359.999° | 0, 22.5, 45, 90, 135, 180, 225, 270, 315 |
| 20 MHz     | 50  | 359.999° | 0, 22.5, 45, 90, 135, 180, 225, 270, 315 |
| 25 MHz     | 40  | 359.999° | 0, 22.5, 45, 90, 135, 180, 225, 270, 315 |
| 40 MHz     | 25  | 359.999° | 0, 45, 90, 135, 180, 225, 270, 315   |
| 50 MHz     | 20  | 359.999° | 0, 22.5, 45, 90, 135, 180, 225, 270, 315 |

相位点并集（报告/文档用）：0, 22.5, 45, 90, 135, 180, 183.96, 191.625, 225, 270, 315。

### 6.5 `SET_SIG` 的响应字段

| 字段            | 含义                                                            |
| --------------- | --------------------------------------------------------------- |
| `freqHz`        | 实际输出频率 `VCO / D`（整数 Hz）                                 |
| `phaseMilliDeg` | **可实现**的相位差：请求值吸附到 `45°/D` 网格后的值（毫度）        |
| `vcoHz`         | VCO 频率                                                        |
| `div`           | 输出分频 `D`                                                     |
| `phaseField`    | MMCM 的 `DELAY_TIME` 字段（= 相位码 `m >> 3`，0..63）；细调部分在 `PHASE_MUX` 里，由 `phaseMilliDeg` 体现 |

请求相位不落在网格上时，`phaseMilliDeg` 回显吸附后的值（例如 8MHz 请求 22.5° →
回显 22.680°，网格 0.36°）。

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
