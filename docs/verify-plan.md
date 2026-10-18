# 验证计划：片内信号源多点测量（无硬件路径）

本文件说明「用片上信号源进行不同频点和相位差的多组测量，验证功能是否正确」这件事
在**没有开发板**的情况下如何落地、判定标准是什么、哪些结论是模型验证、哪些必须上板确认。

## 1. 为什么不能直接跑真实硬件

| 事实 | 来源 |
| ---- | ---- |
| 工作区不绑定开发板，只有器件 `xc7z010clg400-1`，无板级约束 | `vivado/README.md`「注意事项与已知限制」 |
| 当前 bitstream **没有使能 PS UART**：`PCW_EN_UART0/1 = 0`、54 个 MIO 全部 unassigned | `build/vivado/bd_proj/**/dpm_system_processing_system7_0_0.xci` |
| 本机没有 A-profile 裸机 libc，PS 侧程序必须 freestanding | `arm-none-eabi-gcc -print-multi-lib` 只有 M-profile；链接 newlib 报 `conflicting architecture profiles A/M` |
| 无 JTAG 目标（`xsdb` 的 `jtag targets` 为空） | 环境侦察记录 |

因此本轮验证采用**等效后端**：把 PL 的行为（寄存器语义 + 三计数器测量模型 + MMCM 的
频率/相位换算）做成与真实寄存器接口一致的仿真后端，让**真正的 PS 侧协议服务端**与
**真正的 Python 上位机**通过**真正的串口（pty）**跑完整流程。这样能验证：

- 串口协议、命令处理、单次/连续模式状态机、中断/标志语义、丢失计数处理；
- 频率与相位的换算（与 `spec/golden/vectors.json` 的独立参考实现对照）；
- 片内信号源参数换算（DRP 配置字、频率/相位求解）；
- 上位机的显示与扫描流程。

**不能验证**（必须在报告里写清）：MMCM 实际相位/频率是否与 DRP 设定一致、跨时钟域真实时序、
亚稳态、MMCM 抖动、真实布线延迟、上板后的测量精度。这些只有上板才能确认。

## 2. 验证链路

```text
python3 spec/validate_sweep.py
  ├── 建 pty 对（pty.openpty()）
  ├── 启动 ps/build/host_main --device /dev/pts/N   ← 真实的 PS 侧协议服务端代码
  │     └── 内部接 ps/sim/fpga_model.c             ← 寄存器级 + 测量级 PL 模型（DRP 换算同 dpm_sig.c）
  └── 用 host/dpm_host 的上位机客户端在另一个 pty 端跑扫描
        ├── SET_SIG(f, φ) → START → GET_MEAS
        └── 与 spec/golden/vectors.json 比对
  → spec/verify_results.json + docs/verify-report.md
```

三个**互相独立**的实现必须一致：C 侧（`ps/src/dpm_calc.c`、`ps/src/dpm_sig.c`）、
Python 上位机（`host/dpm_host/mathx.py`、`sig.py`）、金标准生成器（`spec/gen_golden.py`）
与 `spec/sim_server.py`（Lead 的独立仿真服务端）。任意两者一致也可能是同一个误解，
因此**三方以上对齐**才算证据；`spec/selftest_spec.py` 负责其中「文档 ↔ 头文件 ↔ 金标准」的自动校验。

已知的规格修正（都曾在实现中踩到，已在 `docs/ps-protocol.md` 定稿）：

| 项 | 错误写法 | 正确写法 |
| -- | -------- | -------- |
| 频率字段单位 | 毫赫兹（25MHz → 2.5e10，溢出 u32） | **Hz 整数**（25MHz → 25000000） |
| 频率公式 | `edge*f_slow*1000/gate`（2^64 溢出） | `(edge*f_slow)/gate/1000` |
| 相位公式 | `360000*phase*f_slow/(gate*f_dsp)`（溢出） | 先按整除分解再乘 `(f_slow/1000)/(f_dsp/1000)` |
| 分频字段 | `2*D` | **低字节 = D 本体** |
| 相位字段位置 | 分频字的 bit[15:8] | **独立 32 位毫度字**（0x20C / 0x218） |
| DRP 状态寄存器 | 字 31（0x27C，恒 0） | **偏移 0x004** |
| 相位在线可改 | 假设可改 | **当前 bitstream 不可改**（实测恒 90°），需 PHASE_DUTY_CONFIG 补丁 |

## 3. 测量点

冻结频点（`docs/ps-protocol.md` §6.4，均由 VCO 与整数分频得到，分频字段 = D 本体且 ≤ 126）：

| 频点 | VCO | MULT | D | 相位量化步长（1 个 DSP 周期） |
| ---- | --- | ---- | - | ---------------------------- |
| 5.000 MHz | 600 MHz | 12 | 120 | 4.00° |
| 6.000 MHz | 750 MHz | 15 | 125 | 4.80° |
| 9.000 MHz | 900 MHz | 18 | 100 | 7.20° |
| 10.000 MHz | 1000 MHz | 20 | 100 | 8.00° |
| 15.000 MHz | 1050 MHz | 21 | 70 | 12.00° |
| 18.000 MHz | 900 MHz | 18 | 50 | 14.40° |
| 25.000 MHz | 1000 MHz | 20 | 40 | 20.00° |
| 30.000 MHz | 1050 MHz | 21 | 35 | 24.00° |
| 45.000 MHz | 900 MHz | 18 | 20 | 36.00° |
| 50.000 MHz | 1000 MHz | 20 | 20 | 40.00° |

相位点**逐频点**生成（上界 = 9 位相位码 `m ≤ 511`，见 `docs/ps-protocol.md` §6.2/§6.4）：
`0/45/90/135/180` 是每个频点的必测点，D 为偶数时加 `22.5°`，`225/270/315°` 只在相位码
装得下时可测，一个 >180° 的 45° 倍数都放不下时用相位码上界点兜底（本轮为 D=120 的 5MHz
`191.625°`、D=125 的 6MHz `183.96°`）。
组合数：**80 个测量点，其中 ≥180° 的 32 个**（每个点默认重复 3 次，`--repeat`）。

> 频点选取依据（`spec/gen_golden.py` 会断言）：`450MHz = 9 × 50MHz`，冻结频点取 5~50MHz 内
> **能整除 450MHz 的整数 MHz 频点**，且每个都是冻结求解规则的**精确解**；`f_dsp / f_sig`
> 全为整数 ⇒ 每个信号周期的相位计数窗口都是同一个整数，`phaseCnt % edgeCnt == 0` 严格成立。
> 旧的两条硬约束（「信号周期是整数 ps」与「`8D / MULT` 为整数」）在 450MHz 下**作废**：
> 6/9/15/18/30/45MHz 的周期不是整数 ps，金标准生成器改用有理数精确运算；
> 400MHz 轮次里 `phaseCnt % edgeCnt != 0` 的实测反例（xsim 联调 6.4MHz 时余数 129）是历史数据。

> **重要前提（实测结论，针对上一版 clk_wiz_src 方案）**：那一版 bitstream 的 DRP **相位字不生效**，
> 片内源两路输出的相位差恒为 BD 初值 **90°**（xsim 实测 43 组，见 `docs/verify-drp.md`）。
> **该前提已随方案 A（裸 MMCM + 极简 DRP 控制器）作废**：现在相位由 PS 直接编码进 MMCM 的
> `PHASE_MUX`/`DELAY_TIME`（写 `0x43C1_0000` 段的 `0x08..0x24` + `0x28` LOAD），
> **已于 2026-10-16 上板验证生效**（8MHz 请求 22.5° → 实测 21.602°；上板验收 44/44、
> 扫描 32/32，见 `docs/verify-report.md` §10）。
> **注意：以上是 DSP=400MHz 轮次的历史数据**，当前设计 DSP 已改为 450MHz、冻结验证频点也已更换
> （见 §3 与 `docs/ps-protocol.md` §6.4），本轮**未重新上板验证**。
> 下表是当时的分阶段结论，保留作对照：
>
> | 阶段 | 可验证的维度 | 相位点 |
> | ---- | ------------ | ------ |
> | **当时无补丁的 bitstream** | 7 个频点 × 频率读数 + 单一相位 90° | 只有 90° 有效，其余相位点用于验证「读回值与请求值无关」这一预期行为 |
> | **当时设想的补丁（`patch_enable_src_phase.tcl`，已删除）** | 7 × 4 全矩阵 | 0/45/90/135（相位字单位毫度，分辨率 `45/D` = 1.125°@D=40） |
>
> 所以当时报告的相位结论**只覆盖 90° 这一个相位点**，其余相位点是「按预期不变」的行为验证，
> 不是相位精度验证。

补充用例（写进报告）：

1. **量化行为**：25 MHz 下测 22.5°，确认读数落在半个量化步长（±10° = 半个 DSP 周期）以内
   ——450MHz 下 25MHz 的量化步长是 20°，22.5° 量化后期望 20°。当时那版 bitstream 上该用例
   退化为「读回 90°，与请求无关」（400MHz 轮次历史）；
2. **闸门时间**：25 MHz / 90° 下把闸门从 2000 改到 20000 个低速周期（400µs），
   `phaseCnt/edgeCnt` 应保持同一个整数（`phase_cnt/edge_cnt` 与闸门时间无关，这是测量模型的关键推论；
   450MHz 下 25MHz/90° 量化到 4 个 DSP 周期，仍为整数）；
3. **连续模式**：`SET_MODE 1` 跑 1 秒，检查 `lostCount` 不增长、`seq` 单调递增；
4. **错误路径**：非法频率（如 3 MHz，低于 `divide_field` 允许的最低频）、相位码越界
   （如 8MHz/200°：相位码 556 > 511，字段截断会让相位回绕）、相位满量程（360° = 0°）、
   未锁定就选片内源，应分别返回 `ERR_PARAM` / `ERR_BUSY`；
5. **外部输入源**：切到外部输入（无信号）后启动测量，应停在 busy（不返回错误），
   上位机要能超时退出并提示——这条验证「源无信号」的容错路径；
6. ~~**相位不可控的行为断言**~~（方案 A 下已作废，保留原记录）：请求 0/45/90/135 四个相位，`SET_SIG` 都返回成功，
   但实测相位读数**全部等于 90°**（并允许 ±1 个 DSP 周期），用来固化「当前 bitstream 下
   「相位字不生效」这一实测结论。**方案 A 下这条已改为「读数 = 请求值 ± 1 个 DSP 周期」**，
   并且额外加了 22.5° 这种需要细调的点（网格 `45/D`）——见 `docs/verify-report.md` §10.3。

## 4. 判定标准

对每个测量点：

- **频率**：`|f_meas - f_nominal| ≤ freq_tol`，`freq_tol` 由金标准给
  （量级 `2/edge_cnt + 2/gate_cnt`，来源是窗口两端相对被测信号的随机相位）；
- **相位**：`|φ_meas - φ_expected| ≤ phase_tol`，`φ_expected` 是**经过 DSP 量化后的期望值**
  （见下），`phase_tol = 半个量化步长 + 1 毫度`；同时 `phase_cnt % edge_cnt == 0`；
- **量化归因**：请求相位若不是量化步长的整数倍（例如 25 MHz 下的 90°：步长 20°，
  90° = 4.5 个 DSP 周期，RNE 取偶后为 4 个周期 = 80°），`φ_expected` 本身就偏离请求值
  最多半个步长，此时要报告「请求 90°，期望 80.0°（量化后），实测 80.0°」这样的三段式结论，
  不能把量化偏差记成相位计误差；
- **失败判据**：任何一点超出容限、或 `phase_cnt % edge_cnt != 0`，判定为失败并列出原始数据
  （新冻结表满足 `f_dsp / f_sig` 为整数，`phase_cnt % edge_cnt == 0` 应严格成立）。

## 5. 期望值模型（与 `spec/gen_golden.py` 一致）

```text
# 450MHz 下的时钟：T_slow = 20ns（50MHz）、T_dsp = 2.222ns（450MHz）
gate_cnt  = target_gate + ceil(T_sig / T_slow)          # 闸门等到被测信号下一个沿
edge_cnt  = (gate_cnt * f_sig) // f_slow                # 整数运算取整
phase_cnt = round(phase_ns / T_dsp) * edge_cnt          # 相位先被 DSP 采样网格量化
freq_hz   = edge_cnt * f_slow // gate_cnt               # 单位 Hz（字段名 freqMilliHz 是历史遗留）
phase_milli_deg = 360000 * phase_cnt * f_slow // (gate_cnt * f_dsp)
```

该模型与 `.agents/skills/dpm-measurement-model` 的公式一致，只是把两处取整显式化：
窗口长度取整（±1~2 个参考沿）与相位量化（1 个 DSP 周期 = 2.222ns）。

## 6. 上板补充验证清单（本轮未做，留给有板时）

1. 按 `docs/bringup.md` 重建 bitstream 与 XSA（串口 `create_bd.tcl -uart uart1` 已是默认值）；
2. 用 Vitis/xsct 或 bootgen 生成 BOOT.bin，串口 115200 下跑 `ps/build/dpm_ps.elf`；
3. 用上位机跑本文件第 3 节的 80 个测量点（默认重复 3 次），比对**同一套**容限；
4. ~~**确认在线改相位真的生效**~~ **已完成（2026-10-16）**：写 `0x43C1_0000` 段的 MMCM 寄存器
   （`0x08..0x24`）+ `0x28` LOAD 后两路输出的相位差随写入值变化，上板逐点实测通过；
   xsim 侧另有 `sim/run_handoff_tb.sh` 用真实 MMCM 行为模型独立复核；
   注：DRP 写期间必须拉高 MMCM 的 `RST`（PL 的 `dpm_src_drp` 自动完成），否则相位字段不生效。
5. 关掉片内源（`SIG_CTRL = 0`）后切外部输入，用信号发生器给两路同频信号复测。
