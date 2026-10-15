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

冻结频点（`docs/ps-protocol.md` 第 6 节，均由 VCO 与整数分频得到，分频字段 = D 本体且 ≤ 255）：

| 频点 | VCO | MULT | D | 相位量化步长（1 个 DSP 周期） |
| ---- | --- | ---- | - | ---------------------------- |
| 5.000 MHz | 600 MHz | 12 | 120 | 4.50° |
| 6.250 MHz | 750 MHz | 15 | 120 | 5.63° |
| 8.000 MHz | 1000 MHz | 20 | 125 | 7.20° |
| 10.000 MHz | 1000 MHz | 20 | 100 | 9.00° |
| 12.500 MHz | 1000 MHz | 20 | 80 | 11.25° |
| 20.000 MHz | 1000 MHz | 20 | 50 | 18.00° |
| 25.000 MHz | 1000 MHz | 20 | 40 | 22.50° |

相位点：`0° / 45° / 90° / 135°`（全部 < 180°，满足相位映射前提 `offset < T_sig/2`）。
组合数：7 × 4 = **28 个测量点**，每个点默认重复 3 次（`--repeat`）。

> **重要前提（实测结论）**：当前 bitstream 的 DRP **相位字不生效**，片内源两路输出的相位差
> 恒为 BD 初值 **90°**（xsim 实测 43 组，见 `docs/verify-drp.md`）。因此：
>
> | 阶段 | 可验证的维度 | 相位点 |
> | ---- | ------------ | ------ |
> | **无补丁（当前 bitstream）** | 7 个频点 × 频率读数 + 单一相位 90° | 只有 90° 有效，其余相位点用于验证「读回值与请求值无关」这一预期行为 |
> | **打 `patch_enable_src_phase.tcl` 补丁并重建 IP/BD 后** | 7 × 4 全矩阵 | 0/45/90/135（相位字单位毫度，分辨率 `45/D` = 1.125°@D=40） |
>
> 补丁的 Tcl 本身已在本机 **dry-run 验证**（`PHASE_DUTY_CONFIG: false → true`、退出码 0），
> 但**「打好补丁的 bitstream 里相位字真的生效」未验证**——那需要重跑 IP 打包 → BD → 综合实现
> （数十分钟到数小时），本仓库未执行。所以本轮报告的相位结论**只覆盖 90° 这一个相位点**，
> 其余相位点是「按预期不变」的行为验证，不是相位精度验证。

补充用例（写进报告）：

1. **量化行为（需要相位补丁）**：25 MHz 下测 22.5°，确认读数落在半个量化步长
   （±11.25° = ±1 个 DSP 周期）以内。无补丁时该用例退化为「读回 90°，与请求无关」；
2. **闸门时间**：25 MHz / 90° 下把闸门从 2000 改到 20000 个低速周期（400µs），
   `phaseCnt/edgeCnt` 应保持同一个整数（`phase_cnt/edge_cnt` 与闸门时间无关，这是测量模型的关键推论）；
3. **连续模式**：`SET_MODE 1` 跑 1 秒，检查 `lostCount` 不增长、`seq` 单调递增；
4. **错误路径**：非法频率（如 3 MHz，低于 `divide_field` 允许的最低频）、非法相位（>180°）、
   未锁定就选片内源，应分别返回 `ERR_PARAM` / `ERR_BUSY`；
5. **外部输入源**：切到外部输入（无信号）后启动测量，应停在 busy（不返回错误），
   上位机要能超时退出并提示——这条验证「源无信号」的容错路径；
6. **相位不可控的行为断言**：请求 0/45/90/135 四个相位，`SET_SIG` 都返回成功，
   但实测相位读数**全部等于 90°**（并允许 ±1 个 DSP 周期），用来固化「当前 bitstream 下
   相位字不生效」这一实测结论；补丁重建后这条断言应改为「读数等于请求值」。

## 4. 判定标准

对每个测量点：

- **频率**：`|f_meas - f_nominal| ≤ freq_tol`，`freq_tol` 由金标准给
  （量级 `2/edge_cnt + 2/gate_cnt`，来源是窗口两端相对被测信号的随机相位）；
- **相位**：`|φ_meas - φ_expected| ≤ phase_tol`，`φ_expected` 是**经过 DSP 量化后的期望值**
  （见下），`phase_tol = 半个量化步长 + 1 毫度`；同时 `phase_cnt % edge_cnt == 0`；
- **量化归因**：请求相位若不是量化步长的整数倍（例如 20 MHz 下的 45°），
  `φ_expected` 本身就偏离请求值最多半个步长，此时要报告
  「请求 45°，期望 35.99°（量化后），实测 35.99°」这样的三段式结论，
  不能把量化偏差记成相位计误差；
- **失败判据**：任何一点超出容限、或 `phase_cnt % edge_cnt != 0`，判定为失败并列出原始数据。

## 5. 期望值模型（与 `spec/gen_golden.py` 一致）

```text
gate_cnt  = target_gate + ceil(T_sig / T_slow)          # 闸门等到被测信号下一个沿
edge_cnt  = (gate_cnt * f_sig) // f_slow                # 整数运算取整
phase_cnt = round(phase_ns / T_dsp) * edge_cnt          # 相位先被 DSP 采样网格量化
freq_milli_hz  = edge_cnt * f_slow * 1000 // gate_cnt
phase_milli_deg = 360000 * phase_cnt * f_slow // (gate_cnt * f_dsp)
```

该模型与 `.agents/skills/dpm-measurement-model` 的公式一致，只是把两处取整显式化：
窗口长度取整（±1~2 个参考沿）与相位量化（1 个 DSP 周期）。

## 6. 上板补充验证清单（本轮未做，留给有板时）

1. 按 `docs/bringup.md` 打 BD 补丁（使能 PS UART），重建 bitstream 与 XSA；
2. 用 Vitis/xsct 或 bootgen 生成 BOOT.bin，串口 115200 下跑 `ps/build/dpm_ps.elf`；
3. 用上位机跑本文件第 3 节的 28 个点，比对**同一套**容限；
4. 额外确认 `docs/verify-drp.md` 的 DRP 结论与实测一致；**若要在线改相位**，
   先跑 `vivado/patch_enable_src_phase.tcl` 并按 README 的流水线重建，
   再用 `sim/run_drp_tb.sh` 复验「写 0x218 毫度字改变 clk_out2 相位」；
5. 关掉片内源（`SIG_CTRL = 0`）后切外部输入，用信号发生器给两路同频信号复测。
