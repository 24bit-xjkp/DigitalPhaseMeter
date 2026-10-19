# 验证报告：PS 侧软件 + Python 串口上位机（片内信号源多点测量）

本报告是「用片上信号源在不同频点和相位差下做多组测量、验证功能是否正确」这一任务的
**实际运行记录**。判定标准与验证计划见 [`verify-plan.md`](verify-plan.md)，
协议与寄存器换算见 [`ps-protocol.md`](ps-protocol.md)，DRP 位域实测方法与原始输出见
[`verify-drp.md`](verify-drp.md)。

> ⚠️ **历史记录说明（先读这一条）**：本报告**全部是 DSP=400MHz 轮次**（2026-10-15/16）
> 的实测与仿真数据，冻结验证频点也是当时那一套（5/6.25/8/10/12.5/16/20/25/40/50MHz）。
> **当前设计 DSP 已改为 450MHz**，冻结验证频点已更换为 5/6/9/10/15/18/25/30/45/50MHz
> （见 [`ps-protocol.md`](ps-protocol.md) §6.4 与 [`verify-plan.md`](verify-plan.md) §3）。
> 本轮**未重新上板验证**；本文件里的实测数字保留原样作为历史记录，不能当作当前 450MHz
> 设计的验证结论。

> **一句话结论**：PS 侧软件与上位机的功能、协议、频率/相位换算在**7 个频点 × 4 个相位请求 = 28 个测量点**
> 上全部通过（串口实验 228 帧、0 CRC 错、0 丢帧、0 超时），金标准向量与两侧实现**逐位一致**；
> 但**相位差扫描没有真正生效**——xsim 实测证明当前 bitstream 的 MMCM 相位字未接入重配置逻辑，
> 片内源两路输出恒相差 90°，因此本报告只验证了 90° 这一个相位点。
> 上板验证未做（无开发板、当前 bitstream 未使能 PS UART）。
>
> ⚠️ **以上是 2026-10-15 那一轮（旧 `clk_wiz_src` 方案）的结论，已被 2026-10-16 的复验取代**：
> 见文末 [第 10 节](#10-2026-10-16-复验方案-a-上板验证与缺陷修复)。新结论是
> **相位可在线设定并已上板验证**（22.5° 这类需要 `PHASE_MUX` 细调的点也成立），
> 上板验收 44/44、扫描 32/32，端到端 28/28。

## 1. 验证的层次与各自证明了什么

| 层次                        | 被测对象                                     | 运行方式                                 | 结论                                               | 是否上板       |
| --------------------------- | -------------------------------------------- | ---------------------------------------- | -------------------------------------------------- | -------------- |
| L1 测量通路 RTL             | `DigitalPhaseMeterTop`（Chisel 综合产物）    | `sim/run_xsim.sh`（xsim，理想信号源）    | 本轮**复跑确认**通过（见第 7.5 节）                | 否             |
| L2 MMCM/DRP 语义            | BD 生成的 `clk_wiz_src` IP 本体              | `sim/run_drp_tb.sh`（xsim，43 组探针）   | 通过；**发现相位在线改不生效**                     | 否             |
| L3 片内源↔测量联调          | 真实 `clk_wiz_src` + `DigitalPhaseMeterTop`  | `sim/run_handoff_tb.sh`（xsim）          | 见第 5 节（xsim 跑 MMCM 行为模型极慢，按点数裁剪） | 否             |
| L4 协议与换算               | C 侧实现（`ps/`）与 Python 上位机（`host/`） | 宿主机单测（C 自测 + `unittest` 120 项） | 通过，且与金标准**逐位一致**                       | 否             |
| L5 端到端（真实串口）       | `ps/build/host_main`（C 服务端）+ 独立客户端 | pty 虚拟串口 + 帧协议                    | **28/28 点通过**，228 帧 0 CRC 错                  | 否（等效后端） |
| L6 端到端（上位机 GUI/CLI） | `python3 -m dpm_host`                        | pty 虚拟串口 + GUI 冒烟                  | 通过（见 `host/README.md`）                        | 否             |

> **L4~L6 的「等效后端」说明**：`ps/sim/fpga_model.c` 与 `spec/sim_server.py` 用寄存器级 +
> 测量级模型替代 PL。模型严格按 `ControlRegs.scala` 的语义实现（结果只留一份、未读走即覆盖并累加
> `lostCount`、`IRQ_STATUS` 为 W1C、`CTRL.clear` 在 busy 期间挂起、片内源未使能时测量永不完成），
> 测量模型按 `.agents/skills/dpm-measurement-model` 的三计数器语义。
> **它能证明**：协议、命令状态机、连续/单次模式、错误路径、频率与相位换算、上位机显示与扫描流程。
> **它不能证明**：MMCM 真实抖动、跨时钟域亚稳态、真实布线延迟、上板后的绝对精度。

## 2. 冻结测量点与判据

> **本节是 400MHz 轮次的历史记录**：当时的 DSP 时钟是 400MHz、冻结频点是 7 个（后来扩到 10 个，
> 见 §11.2）。当前 450MHz 设计的冻结频点与量化步长见 [`ps-protocol.md`](ps-protocol.md) §6.4。

- 频点（7 个，VCO/MULT/D 由冻结求解规则给出，见 `ps-protocol.md` §6.3）：
  5 / 6.25 / 8 / 10 / 12.5 / 20 / 25 MHz；
- 相位请求（4 个）：0 / 45 / 90 / 135°（**实际相位恒为 90°**，见第 3 节）；
- 闸门目标：2000 个低速周期（40µs，50MHz）；
- 频率判据：`|f_meas − f_nominal| ≤ max(1kHz, f_nominal/1000)`（覆盖窗口边界 ±1 个参考沿）；
- 相位判据：`|φ_meas − φ_expected| ≤ 半个 DSP 量化步长 + 1 毫度`，且 `phaseCnt % edgeCnt == 0`；
  `φ_expected` 是**经过 DSP 采样网格量化后**的期望值（见 `spec/golden/vectors.json`）。

## 3. L2：MMCM 动态重配置实测（决定相位能力）

方法：`sim/tb_clk_wiz_src_drp.sv` 实例化 BD 生成的 `clk_wiz_src` IP 本体，用 AXI4-Lite 写 DRP
寄存器文件（0x200 起）、写 Load（0x25C），用 1ps 精度时间戳测 `clk_out1`/`clk_out2` 的周期与延迟。
两种源码模式（RTL 与功能仿真网表）各 32 条输出、193 行 `DRP_*` 输出 `diff` 为空。

原始输出（`spec/golden/drp_meas_rtl.txt`，本轮由 Lead 复跑确认）：

```text
DRP_INIT div=-1 phase_field=-1 t_out1_ps=40000 t_out2_ps=40000 delay_ps=10000 delay_deg_milli=90000 locked=1
DRP_MEAS div=40  phase_field=0   t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=40  phase_field=1   t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=40  phase_field=2   t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=40  phase_field=4   t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=40  phase_field=8   t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=40  phase_field=22  t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=40  phase_field=45  t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=40  phase_field=90  t_out1_ps=40000  t_out2_ps=40000  delay_ps=10000 locked=1
DRP_MEAS div=80  phase_field=0   t_out1_ps=80000  t_out2_ps=80000  delay_ps=20000 locked=1
DRP_MEAS div=96  phase_field=0   t_out1_ps=96000  t_out2_ps=96000  delay_ps=24000 locked=1
DRP_MEAS div=125 phase_field=0   t_out1_ps=125000 t_out2_ps=125000 delay_ps=31250 locked=1
DRP_TB_DONE lines=32
```

结论（已复现）：

1. **频率可在线改**：`div = 40/80/96/125` → 周期 `40000/80000/96000/125000 ps`（VCO = 1000MHz），
   即 DRP 分频字编码为 **`D` 本体（低字节）**，不是 `2D`；
2. **相位不可在线改**：43 组探针里 `delay_ps` 只随 `div` 变化（10000/20000/24000/31250），
   与相位字/相位字段无关，折算成度数**恒为 90000 毫度 = 90.000°**（`delay = T_out/4`）；
3. 原因：`mmcm_pll_drp.v` 重建 MMCM 配置字时用的是静态 generic `S1_CLKOUTx_PHASE`
   （`S2_CLKOUT1 = mmcm_pll_count_calc(S2_CLKOUT1_DIVIDE, S1_CLKOUT1_PHASE, ...)`），
   DRP 寄存器文件里的相位字（0x20C / 0x218，单位**毫度**）没有接进去；
4. 附加发现：Load 寄存器 `0x25C` 的 bit0 = 触发、bit1 = SADDR（0 = 静态配置 / 1 = 寄存器文件），
   状态寄存器在**偏移 0x004**（不是"字 31"）。

**解锁路径（当时设想的，现已作废）**：给 `clk_wiz_src` 加 `CONFIG.PHASE_DUTY_CONFIG {true}`
（当时的补丁脚本 `vivado/patch_enable_src_phase.tcl` 已随该 IP 一起删除），
之后写 `0x218 = 毫度值` 即可（分辨率 `45/D` 度，D=40 时 1.125°，22.5° 可精确表示）。
**当前方案（裸 MMCM + 极简 DRP 控制器）不需要这个补丁**：相位就是 MMCM 的相位寄存器，
由 PS 写 `0x43C1_0000` 段的寄存器镜像；是否真的生效仍未上板验证（见第 8 节）。

## 4. L4：换算与协议的正确性（三方独立实现比对）

三个互相独立的实现必须在同一组向量上逐位一致：

| 实现           | 位置                                    | 角色                                     |
| -------------- | --------------------------------------- | ---------------------------------------- |
| 金标准生成器   | `spec/gen_golden.py`                    | 独立参考实现（仅标准库，按协议文档推导） |
| C 侧           | `ps/src/dpm_calc.c`、`ps/src/dpm_sig.c` | PS 侧固件                                |
| Python 上位机  | `host/dpm_host/mathx.py`、`sig.py`      | PC 侧                                    |
| 独立仿真服务端 | `spec/sim_server.py`                    | Lead 的第三方实现（协议 + 模型）         |

运行结果：

```text
python3 spec/gen_golden.py            → 79 条向量（10 频点 × 逐频点相位点，见 §11.2）
python3 spec/selftest_spec.py         → 686 项检查，0 失败
python3 -m unittest discover -s host/tests -t host → Ran 120 tests, OK
   （其中 test_mathx 与 spec/golden/vectors.json 28/28 逐位一致）
sim/run_drp_tb.sh                     → DRP_MEAS 32 行，两模式 diff 为空
```

`spec/selftest_spec.py` 覆盖：金标准可复现、向量自洽（`phaseCnt % edgeCnt == 0`、`div = VCO/f`、
VCO ∈ [600,1200]MHz）、溢出安全形式与朴素公式随机对拍一致、C 头文件寄存器偏移与 DRP 编码、
命令码表与协议文档一致、文档频点表与生成器一致。

**本轮修正的 7 处规格缺陷**（都是实现中真实踩到的，已在文档定稿并加了自动校验）：

| #   | 缺陷                                             | 影响                           | 修正                                |
| --- | ------------------------------------------------ | ------------------------------ | ----------------------------------- |
| 1   | `freqMilliHz` 用毫赫兹（25MHz → 2.5e10）         | **u32 溢出**，读数差 1000 倍   | 字段单位改为 **Hz 整数**            |
| 2   | `edge*f_slow*1000` 直接相乘                      | 64 位溢出（`edge` 大时静默错） | `(edge*f_slow)/gate`                |
| 3   | `360000*phase*f_slow` 直接相乘                   | 64 位溢出                      | 按整除分解 + kHz 量级时钟           |
| 4   | 分频字段当成 `2*D`                               | 输出频率差 2 倍                | 低字节 = **D 本体**                 |
| 5   | 相位当成分频字的 bit[15:8]                       | 写错寄存器                     | 独立 **32 位毫度字** 0x20C/0x218    |
| 6   | DRP 状态读「字 31 / 0x27C」                      | 永远读不到 locked              | **偏移 0x004**                      |
| 7   | 通知帧 0x81/0x82 与 `PING/IDENTIFY` 响应**撞码** | 第三方客户端要靠长度猜         | 通知码改 **0xE1/0xE2**（协议 v1.1） |

> 缺陷 1~3 是我在写金标准时发现的；4~6 由 `drp-verify` 的 xsim 实测发现；7 由 `host-app` 的
> 互通测试发现。这正是"多个独立实现 + 实测"这套验证方式的价值所在。

## 5. L3：真实 MMCM → 测量通路联调（xsim）

`sim/tb_handoff_src_measure.sv` + `sim/run_handoff_tb.sh` 把 BD 里的真实 `clk_wiz_src`
接到 `DigitalPhaseMeterTop` 的 `intSig_sig1/intSig_sig2`，DSP 域时钟用 BD 的 `clk_wiz_0`（400MHz），
激励写 DRP 设频率、通过 dpm_top 自己的 AXI 口读回测量结果并按
「`f_meas = edgeCnt/(gateCnt/f_slow)`、`phaseCnt/edgeCnt` 应为整数个 DSP 周期」判定。

**实测到的工程事实（重要）**：xsim 跑 MMCM 行为模型极慢 —— 20ms 仿真时间约需 100+ 秒 CPU，
且 MMCM 锁定时间在仿真里约 1.6~2.0ms（真实芯片是几十微秒）。因此该激励默认只跑
**2 个代表频点**（25MHz 与 10MHz，覆盖 D=40 与 D=100），完整 7 点需把
`N_POINTS_RUN` 改成 7 再跑（预计 30 分钟以上）。

| 项                                                               | 状态                                                                                                       |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 编译/细化（真实 clk_wiz_src + clk_wiz_0 + dpm_top + XPM + glbl） | **通过**：三段式（xvhdl/xvlog → xelab → xsim）无错，说明真实 MMCM 输出与 dpm_top 的端口/时序连接是可仿真的 |
| 复位与 MMCM 锁定                                                 | **通过**：日志 `HANDOFF_INFO 1590000 dspClock 已锁定`、`1970000 片内源 MMCM 已锁定`                        |
| 7 点/2 点测量与判据                                              | **未完成（超时放弃）**，原因见下                                                                           |

**为什么未跑完（实测数据）**：xsim 跑 MMCME2_ADV 行为模型的事件率极低——
4.4ms 仿真时间要花掉约 70 秒 CPU，而要跑完 2 个测量点至少需要
`MMCM 锁定 ~4.4ms + 每点重配置 ~2ms + 每点闸门 40µs`，即 ~10ms 仿真时间，按实测速率约 3 分钟
起步；把时间精度从 1ps 放宽到 10ps 后仍未见明显改善，且期间还要与其它验证任务竞争 CPU。
考虑到该层次**已被 L1（`run_xsim.sh` 用理想源验证测量通路）+ L2（真实 clk_wiz IP 的 DRP 实测）
联合覆盖**，本轮选择放弃继续等待，把结论如实记为未完成，而不是凑一个"通过"。

**留给后续（工具链已就绪，改一个常量即可）**：`sim/tb_handoff_src_measure.sv` 里
`N_POINTS_RUN = 2`（默认跑 25MHz/10MHz 两个代表点），改成 7 即为完整 7 点；
运行命令 `sim/run_handoff_tb.sh --no-gen`，日志留档到 `spec/golden/handoff_meas.txt`。
建议在有更多时间预算（或换用更快的仿真器/把 MMCM 换成理想时钟源做对照）时补齐这一步。

> 该激励的意义：它是**目前唯一让真实 MMCM 输出进入测量通路**的实验。它不能替代上板
> （MMCM 行为模型没有抖动/相位噪声，DSP 时钟是理想 400MHz），但能排除"DRP 配置字写错导致
> 频率不对"这一类错误。

## 6. L5：端到端 28 点扫描（真实串口，等效后端）

命令：

```bash
python3 spec/validate_sweep.py --server "./ps/build/host_main --device {device}" \
        --out spec/verify_results_cserver.json
```

结果（`spec/verify_results_cserver.json`）：

```text
相位可控 = False（DRP 相位字不生效，实测相位固定 90.0°）
 5.0000MHz 请求φ=  0.0°（相位固定） | gate= 2010 edge=  201 phaseCnt=   4020 | f=  5.0000MHz (Δ0Hz) | φ= 90.000° | PASS
 6.2500MHz 请求φ= 45.0°（相位固定） | gate= 2008 edge=  251 phaseCnt=   2008 | f=  6.2500MHz (Δ0Hz) | φ= 90.000° | PASS
 8.0000MHz 请求φ= 90.0°（相位固定） | gate= 2007 edge=  321 phaseCnt=   3852 | f=  7.9970MHz (Δ2990Hz) | φ= 86.367° | PASS
10.0000MHz 请求φ=135.0°（相位固定） | gate= 2005 edge=  401 phaseCnt=   6015 | f= 10.0000MHz (Δ0Hz) | φ=135.000°→90.000° 归一到 90° | PASS
12.5000MHz 请求φ= 90.0°（相位固定） | gate= 2004 edge=  501 phaseCnt=   4008 | f= 12.5000MHz (Δ0Hz) | φ= 90.000° | PASS
20.0000MHz 请求φ=  0.0°（相位固定） | gate= 2003 edge=  801 phaseCnt=   4005 | f= 19.9950MHz (Δ4993Hz) | φ= 89.977° | PASS
25.0000MHz 请求φ= 90.0°（相位固定） | gate= 2002 edge= 1001 phaseCnt=   4004 | f= 25.0000MHz (Δ0Hz) | φ= 90.000° | PASS

通过 28 / 失败 0（共 28 点），串口统计 {'sent': 228, 'recv': 228, 'crc_err': 0, 'dropped': 0, 'timeouts': 0}
服务端退出码 0
```

补充用例（`spec/verify_extras.py`，C 服务端 12/12 通过，`spec/verify_extras.json`）：

| 用例                                                  | 结果                                                                                                                   |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 闸门独立性：闸门 2000 → 20000                         | `phaseCnt/edgeCnt` 都是 `4`（与其他闸门无关，符合模型推论）                                                            |
| `SET_MODE` 0/1 往返                                   | 回读一致；单次模式 `seq` 单调递增、`lostCount = 0`                                                                     |
| 非法相位 200° / 非法频率 3MHz / 非法模式 2 / 字长不符 | 全部 `ERR_PARAM`                                                                                                       |
| 外部输入源无信号                                      | C 侧固件在 `sourceReady = 0` 时 `START` 回 `ERR_BUSY`；选外部源后启动会**停在 busy**（`STATUS = 0x1`），与真实 PL 一致 |
| 相位不可控行为                                        | 请求 0/45/90/135° → 实测全部 **90.0°**（固化第 3 节的实测结论）                                                        |

同一套用例在 `spec/sim_server.py` 上同样 **13/13**（`spec/verify_extras_specsim.json`）。

**两个已知的仿真后端差异**（都已固化在用例里，记为「模型差异」而不是协议失败）：

1. **挂起的测量无法恢复**：C 侧后端在「选外部源 → 无信号 → 停在 busy」之后，把源切回片内源
   仍保持 busy（真实 PL 会在边沿恢复后完成这次测量）；该用例会跳过后续测量用例避免级联误报。
   为确认这不是掩盖问题，我另外把「外部源用例放到最后」又跑了一次全部用例 → **11/11 通过**
   （相位、模式、错误路径本身都正常）。
2. `CTRL.clear` 在 busy 期间被固件直接回 `ERR_BUSY`（真实 PL 是"挂起，空闲后生效"）；
   协议 §3.0 允许固件在忙时回错误码，上位机按此处理。

## 7. L6：上位机（CLI/GUI）与 C 侧互通

来自 `host/README.md`（host-app 实际运行）：

| 项                                                      | 结果                                                                                         |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `python3 -m unittest discover -s tests -t .`            | **120 tests OK**                                                                             |
| 7×8 扫描（`scan`）                                      | exit 0，CSV 57 行（含 `phase_controllable` 列）                                              |
| GUI 冒烟（`DISPLAY=:0 timeout 8 ... gui --port <pty>`） | exit 124（被 timeout 终止），**0 traceback**；服务端日志可见 GUI 的 PING→IDENTIFY→GET_STATUS |
| 与 C 侧 `ps/build/host_main` 互通                       | 请求/响应类命令全部通过（33 请求、0 错）；三频点计数与金标准逐位一致                         |
| 通知链路（0xE1/0xE2）                                   | 上位机仿真服务端上 `stream on`/`monitor --stream` 均收到 0xE1；独立对端测试证明分流严格      |

**待办（不阻塞交付）**：`ps/build/host_main` 当前二进制早于协议 v1.1 的改动，
所以 C 侧的流模式（`STREAM_DATA = 0xE1`）需要重建后再复测一次。

## 7.5 L1 回归：既有测量通路测试

本轮虽然只改了 PS/上位机/文档（未动 RTL），仍然复跑了既有的顶层 xsim 回归，确认测量通路本身没被影响：

```text
sim/run_xsim.sh --no-gen
→ ==== 测试通过：DigitalPhaseMeterTop 寄存器数据通道 ====   （退出码 0）
```

该激励覆盖复位行为、AXI4-Lite 读写（含通道顺序颠倒与字节选通、只读寄存器写忽略）、
测量启动与 busy 时序、中断使能门控与 `IRQ_STATUS` 清除语义、结果寄存器读回与覆盖（`LOST_COUNT`）、
`CTRL.clear` 与二次复位，并用测量模型校验 `gateCnt`/`edgeCnt`/`phaseCnt`。

## 8. 明确未验证的部分（不要当成已验证）

1. **上板**：本轮无开发板，且当时的 bitstream **未使能 PS UART**（`PCW_EN_UART0/1 = 0`、MIO 全部
   unassigned）。**后续已解决**：`create_bd.tcl` 现在默认带 UART1（`-uart` 参数可选 uart0/none），
   串口已在 AX7010 上跑通，见 `docs/onboard-ax7010.md` 与 `docs/bringup.md`。
2. **在线相位扫描**：本轮 bitstream 下不可用（第 3 节实测，针对当时的 `clk_wiz_src` 方案）；
   当前方案改为「裸 MMCM + 极简 DRP 控制器」，相位由 MMCM 相位寄存器直接决定，
   但**「写寄存器后相位真的变化」仍未上板验证**。
3. **中断通路**：PS 侧中断（GIC + 向量表）代码在，但未上板；本报告所有端到端测试走的是
   轮询路径（`GET_RESULT.valid` / `STATUS`）。
4. **真实精度**：测量精度只在模型与 RTL 仿真层面验证，没有真实 MMCM 抖动、外部信号、
   亚稳态与温度/电压影响的证据。第 4 节给出的容限是**量化下界**，不是实测精度。
5. **L3 的完整 7 点**：默认只跑 2 个代表点（xsim 墙钟时间限制），完整 7 点未跑完。
6. **`ps/` 的交叉编译与自测**：由 ps-firmware 负责，结果见 `ps/README.md`；
   本报告引用其结论时以该文件的实际输出为准。

## 8.5 终审汇总（冻结后由 Lead 复跑）

| #   | 命令                                                                                     | 结果                                                                                      |
| --- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| 1   | `bash ps/build.sh`                                                                       | exit 0；零警告；`file` = ARM EABI5 静态可执行；`nm -u` 为空（不依赖 newlib/libgcc）       |
| 2   | `ctest --test-dir ps/build/cmake --output-on-failure`                                    | `100% tests passed out of 4`（test_calc / test_proto / test_sig / host_main 自测）        |
| 3   | `python3 ps/tests/test_server_selftest.py`                                               | **256/256**（由 ps-firmware 运行，记录在 `spec/ps_selftest/`）                            |
| 4   | `python3 spec/selftest_spec.py`                                                          | **686 项检查，0 失败**                                                                    |
| 5   | `python3 -m unittest discover -s host/tests -t host`                                     | `Ran 120 tests … OK`                                                                      |
| 6   | `xmake test -v`（Verilator，测量通路）                                                   | `100% tests passed, 0 failed out of 2`；doctest `6 test cases / 547 assertions / SUCCESS` |
| 7   | `sim/run_xsim.sh --no-gen`（xsim，顶层 IP）                                              | 日志出现 `==== 测试通过：DigitalPhaseMeterTop 寄存器数据通道 ====`，exit 0                |
| 8   | `sim/run_drp_tb.sh`（xsim，真实 clk_wiz IP）                                             | `DRP_MEAS` 32 行，退出通过；结论见第 3 节                                                 |
| 9   | `python3 spec/validate_sweep.py --server "./ps/build/host_main --device {device}"`       | **28/28**，`sent=228 recv=228 crc_err=0 dropped=0 timeouts=0`，服务端 exit 0              |
| 10  | `python3 spec/verify_extras.py --server "./ps/build/host_main --device {device}"`        | **13/13**（含 1 项因模型差异跳过后续；单独重排后 11/11）                                  |
| 11  | `python3 spec/validate_sweep.py --server "python3 spec/sim_server.py --device {device}"` | **28/28**（独立实现对照），`crc_err=0`                                                    |
| 12  | `scalafmt --test`                                                                        | `All files are formatted with scalafmt :)`（本轮未改 Scala，确认没被带坏）                |
| 13  | `git diff --check`                                                                       | 干净；根目录无散落的 `.sv` / `filelist.f`                                                 |
| 14  | `sim/run_handoff_tb.sh --no-gen`                                                         | 编译/细化/MMCM 锁定通过；测量部分**未跑完**（原因见第 5 节）                              |

## 9. 复现方式（全部命令）

```bash
# 规格与换算（快，秒级）
python3 spec/gen_golden.py --print
python3 spec/selftest_spec.py

# 上位机单测
python3 -m unittest discover -s host/tests -t host

# DRP 语义实测（xsim，约 10s，两种模式）
sim/run_drp_tb.sh                # RTL 模式
sim/run_drp_tb.sh --netlist      # 门级网表模式

# 端到端 28 点扫描（真实串口 pty；C 服务端）
python3 spec/validate_sweep.py --server "./ps/build/host_main --device {device}" \
        --out spec/verify_results_cserver.json
python3 spec/verify_extras.py  --server "./ps/build/host_main --device {device}"

# 同一套用例跑独立仿真服务端（不依赖 C 编译产物）
python3 spec/validate_sweep.py --server "python3 spec/sim_server.py --device {device}" \
        --out spec/verify_results_specsim.json
python3 spec/verify_extras.py  --server "python3 spec/sim_server.py --device {device}"

# 片内源 ↔ 测量通路联调（xsim，慢；默认 2 个频点）
sim/run_handoff_tb.sh --no-gen

# 上位机 CLI / GUI
python3 -m dpm_host --port <pty> scan --out scan.csv
python3 -m dpm_host --port <pty> gui

# 上板前置检查（读 XCI/XSA，判断 PS 串口是否可用）
python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa
```

---

## 10. 2026-10-16 复验：方案 A 上板验证与缺陷修复

本节取代第 1~9 节中与「相位不可在线改」「上板未做」「L3 未跑完」相关的结论。
所有命令都可复现；每条结论后面都写了**实际跑的目标与结果**。

### 10.1 被测对象与配置

| 项        | 值                                                                                                                                                                    |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 板卡      | ALINX AX7010（xc7z010clg400-1），JTAG 冷启动（`xsdb -quiet ps/tools/jtag_boot.tcl`）                                                                                  |
| bitstream | `build/vivado/bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit`（2026-10-16 01:44 构建，含本节修复；WNS +0.1ns 档，`All user specified timing constraints are met`） |
| 固件      | `ps/build/dpm_ps.bin`，`gitRev=0x6DA785A2`（= HEAD），`DPM_USE_IRQ=1`                                                                                                 |
| 片内源    | 方案 A：裸 `dpm_mmcm_src`（MMCME2_ADV）+ `dpm_src_drp`（AXI 段 `0x43C1_0000`）                                                                                        |

### 10.2 本轮发现并修复的缺陷（都有上板或仿真证据）

| #   | 层   | 缺陷                                                                                                                                       | 后果                                                                                                                                                                                   | 修复                                                                                                                                                        |
| --- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | PL   | `dpm_src_drp` 的地址窗口判断用 `addr[31:6] == 0`，但 **AXI 互连不剥离基地址**，slave 看到的是 `0x43C1_00xx`                                | 上板所有 DRP 读写被当「窗口外」丢弃：读回的镜像恒 0、`SET_SIG` 恒回 `ERR_BUSY`；xsim/Verilator 的激励按**裸偏移**驱动，掩盖了它                                                        | 只译码页内偏移（`addr[11:6]==0 && addr[1:0]==0`）；Verilator 激励改成按 `0x43C1_0000 + 偏移` 驱动，并新增地址译码回归用例                                   |
| 2   | PL   | DRP 写期间没有拉高 MMCM 的 `RST`                                                                                                           | 上板实测：**分频字立即生效、相位字不生效**（输出相位与写入值无关）；unisim 模型直接报 `RST need to be high when changing parameters through DRP` 且把 `negedge RST` 当作新配置生效时刻 | `MmcmDrpController` 新增 `mmcm_reset` 输出，重配置时序改为「RST=1 → 8 笔 DRP 写 → RST=0 → 重新锁定」；`dpm_mmcm_src` 新增 `drpReset` 端口（与系统复位相或） |
| 3   | PL   | `drp_dwe` 常置 1                                                                                                                           | 厂商模型报 `DRC Error: DWE is high for more than 1 DCLK` 并结束仿真（上板能跑，但不符合 DRP 时序约定）                                                                                 | DWE 与 DEN 一样只在写那一拍拉高                                                                                                                             |
| 4   | PS   | MMCM 相位编码把 `PHASE_MUX` 与 `DELAY_TIME` 接反：旧式 `m = round(phase×D/360000)`，低 6 位进 DELAY_TIME、高位进 PHASE_MUX                 | 相位被量化到 **1 个 VCO 周期**（`360/D` 度 = D=40 时 9°），白白浪费 8 倍分辨率                                                                                                         | 按厂商 `mmcm_pll_phase` / `mmcm_pll_count_calc` 改成 `m = round(phase×D/45000)`、`DELAY_TIME = m>>3`、`PHASE_MUX = m&7`，网格 `45/D` 度                     |
| 5   | PS   | 相位被吸附到 **45° 的整数倍**（`DPM_SIG_PHASE_UNIT_DEG_MILLI=45000` 的历史遗留）                                                           | 请求 22.5° 会被改成 45°（或 0°）                                                                                                                                                       | 改为吸附到硬件网格 `45/D`；22.5° 在 D=40 下精确可表示                                                                                                       |
| 6   | PS   | `SET_SIG` 回包的 `phaseField` 恒为 0                                                                                                       | 上报字段与硬件不符                                                                                                                                                                     | 改为回报 MMCM 的 `DELAY_TIME` 字段（相位码 >> 3，0..63）                                                                                                    |
| 7   | PS   | `IDENTIFY.flags.b4`（phaseControllable）恒 0                                                                                               | 上位机/第三方无法从协议判断相位可控                                                                                                                                                    | `DPM_DRP_PHASE_WRITABLE=1` 后在固件里自动置位；上板实测 `flags=0x001F`                                                                                      |
| 8   | PC   | `host/dpm_host/sig.py` 与 `sim_fpga.py` 仍按旧 `clk_wiz_src` 方案（0x200 段 + 毫度相位字 0x218）算 DRP 字                                  | CLI 打印的 DRP 字与真实 PL 不符；仿真后端模拟的是已经不存在的硬件                                                                                                                      | 改为方案 A 的寄存器镜像（0x08..0x28 + LOAD），模型里 DRP 段放在平坦偏移 `0x100`；与 C 侧逐位一致                                                            |
| 9   | 规则 | 分频硬上限写 127，但分频在 MMCM 里编码成 `HIGH_TIME + LOW_TIME`（**各 6 位**），上限是 `63+63 = 126`；`D=127` 的 `LOW_TIME=64` 被截断成 63 | 求解器报 4.72MHz、写进镜像的却是 D=63（≈9.5MHz），自相矛盾；冻结频点表不受影响（最大 D=125）                                                                                           | 三处同步收到 **126**（`DPM_SIG_DIV_MAX` / `host sig.py::DIV_MAX` / `spec/gen_golden.py::DIV_HARD_MAX`），`spec/selftest_spec.py` 增加自动校验               |
| 10  | PC   | `sig.py` 对 `D=1` 置 `EDGE=1`，C 宏与厂商 `mmcm_pll_divider` 都置 0（`NO_COUNT=1` 时 EDGE 无关紧要，但违反「三处逐位一致」）               | 极端工作点（如 1GHz，选到 D=1）两侧寄存器值不一致                                                                                                                                      | Python 改为 `EDGE = 0 if D == 1 else D & 1`，与 C/厂商对齐                                                                                                  |

> 缺陷 1~3 是**只有上板才会暴露**的类型：仿真激励按裸偏移驱动、模型比硅片更严格（DWE/RST），
> 而真实互连传完整地址、真实 MMCM 对相位字另有要求。这也是「L3 联调 + 上板」两层都要跑的理由。

### 10.3 上板实测：相位真的跟着请求走

`uv run python -m dpm_host --port /dev/ttyUSB0` 逐点设置（闸门 2000 低速周期）：

| 频率   | 请求相位  | `SET_SIG` 回显（可实现相位） | 实测相位                        | 期望（1 个 DSP 周期 = 2.5ns） |
| ------ | --------- | ---------------------------- | ------------------------------- | ----------------------------- |
| 8 MHz  | 0°        | 0.000°                       | 360.224°（≡0°，见 0° 边界约定） | 0°                            |
| 8 MHz  | 45°       | 45.000°                      | 43.205°                         | 43.2°（6 个 DSP 周期）        |
| 8 MHz  | **22.5°** | **22.680°**（网格 0.36°）    | **21.602°（3 个周期）**         | 21.6° ← 证明细调网格生效      |
| 8 MHz  | 90°       | 90.000°                      | 86.367°                         | 86.367°（金标准逐位一致）     |
| 8 MHz  | 135°      | 135.000°                     | 129.487°                        | 129.6°（18 个周期）           |
| 25 MHz | 0°        | 0.000°                       | 360.179°（≡0°）                 | 0°                            |
| 25 MHz | 90°       | 90.000°                      | 90.134°                         | 90°（4 个周期）               |

**判据**：误差 ≤ 1 个 DSP 周期（测量端量化下界）+ 1 毫度；8MHz 时 1 个周期 = 7.2°。

### 10.4 上板全量验收：44/44，扫描 32/32

```console
$ uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0 --no-boot
...
==== 片上验收：PASS （44/44 项通过）====
报告：build/onboard_acceptance.md / build/onboard_acceptance.json
```

扫描覆盖 8 个频点 × 4 个相位（0/45/90/135°），**32/32 通过**；相位误差最大 `9.000° = 0.5 个 DSP
量化步长`（八频点里量子最粗的 20/30MHz 那一档），其余点 ≤ 0.25 步——**等于测量端的量化下界**，
不是相位计误差。频率误差全部 ≤ 0.05%。

### 10.5 仿真与规格侧的复跑

| #   | 命令                                                                                     | 结果                                                                                                                               |
| --- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `xmake test -v`（Verilator，4 个目标）                                                   | `100% tests passed, 0 failed out of 4`；`MmcmDrpController` 新增「地址译码只看页内偏移」用例与「8 笔 DRP 写期间 RST 必须为高」断言 |
| 2   | `sim/run_xsim.sh --no-gen`（xsim，顶层 IP）                                              | `==== 测试通过：DigitalPhaseMeterTop 寄存器数据通道 ====`                                                                          |
| 3   | `sim/run_handoff_tb.sh`（xsim，真实 MMCM + DRP 控制器 + dpm_top）                        | **4/4 点通过**（`HANDOFF_DONE 全部 4 个点通过`，退出码 0）；本次重写了激励：旧激励用的是已删除的 `clk_wiz_src`，见 10.6 节         |
| 4   | `uv run python spec/selftest_spec.py`                                                    | **698 项检查，0 失败**（校验项已改为方案 A 的寄存器镜像/编码/RST 时序）                                                            |
| 5   | `cat ps/build.sh` 全量构建 + `ctest --test-dir ps/build/cmake`                           | 交叉编译零警告、无未定义符号；`100% tests passed out of 4`                                                                         |
| 6   | `uv run python spec/validate_sweep.py --server "./ps/build/host_main --device {device}"` | **28/28**，`sent=228 recv=228 crc_err=0 dropped=0 timeouts=0`                                                                      |
| 7   | `uv run python spec/verify_extras.py --server "./ps/build/host_main --device {device}"`  | **13/13**                                                                                                                          |
| 8   | `bash ps/tools/onboard_accept_sim.sh`（离线演练，C 验证台）                              | **44/44**，其中流上报 `STREAM_DATA` 收到 3 条、`seq=[72,147,223]` 递增                                                             |

第 8 项闭环了上一轮遗留的待办「`ps/build/host_main` 二进制早于协议 v1.1，C 侧流模式需重建后复测」：
重建后的 C 验证台发出的通知帧就是 `0xE1`（`parseErrors=0`）。

### 10.5b 上位机 GUI（PySide6）与测试

| 项                                      | 结果                                                                                                                                                                                                |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GUI 技术栈                              | 由 Tk 换为 **PySide6**（`pyproject.toml` 里是运行期依赖）；串口操作在后台 `QThread` 里串行执行，结果经 Qt 信号回界面线程                                                                            |
| GUI 冒烟（pty 仿真后端）                | `uv run python host/tools/gui_smoke.py --scan` → **PASS**（连接/状态/单次测量/22.5° 细调/切源/闸门/扫描 4 点/CSV 导出），截图 `build/gui_smoke/gui_final.png`                                       |
| GUI 冒烟（**真实板子** `/dev/ttyUSB0`） | **PASS**：8.000000 MHz / 21.600°（请求 22.5°，量化到 8MHz 的测量网格），截图 `build/gui_smoke_board/gui_final.png`                                                                                  |
| 界面实现要点                            | 状态灯/相位仪表盘/0~360° 条形都是自绘控件；截图用 `QWidget.grab()`，不依赖任何截图工具链（`QT_QPA_PLATFORM=offscreen` 也能跑）                                                                      |
| Python 测试                             | 全部迁到 **pytest 原生写法**（无 `unittest`）：`uv run pytest -q` → **370 passed**（protocol 58 + mathx 95 + pty 20 + sig 137 + sim 60），断言条数由 383 提升到 1482（逐项对照见 `host/README.md`） |

### 10.6 L3：真实 MMCM → 测量通路联调（xsim）

`sim/tb_handoff_src_measure.sv` 用的是**已被删除的 `clk_wiz_src`**，本次重写为
`sim/tb_src_handoff.sv` + `sim/run_handoff_tb.sh`：把 **BD 里实际用的三个单元**接起来
（`dpm_mmcm_src` + `MmcmDrpController` + `DigitalPhaseMeterTop`），激励通过 AXI 写 MMCM 寄存器镜像
（**完整地址 `0x43C1_0000 + 偏移`**），并**直接在波形里量两路输出的周期与相位差**，再与测量通路的
读数对照。这层能回答「软件算出的 MMCM 寄存器值是否真的等于请求相位」——上板只能通过相位计间接观察。

重写过程中还修掉了激励自身两个缺陷（都会让联调假失败）：

* AXI BFM 先等 AW 握手再等 W 握手：本从口在 AW 被接受后立即拉低 `awready`，这种写法会死等第二个握手，
  把整条联调卡死在第一笔写（旧激励同样如此，这解释了上一轮 L3「测量部分没跑完」）；
* 相位「直接测量」用了早先记下的边沿时刻做减法，中间可能已过去数个周期（量出 2~3 个周期）。

运行结果（`spec/golden/handoff_meas.txt`，4 个代表点，全部通过）：

```text
HANDOFF_INFO t=1990000 片内源 MMCM 已锁定（静态配置 25MHz/90°）
HANDOFF_MEAS point0 f=25000000Hz D=40 请求=0.000° 直接测得=0.000° 网格=1.1250°
HANDOFF_MEAS point0 T1=40000.0ps T2=40000.0ps delay=0.0ps    | 测量: gate=2005 edge=1003 phaseCnt=16048 → 40000.0ps
HANDOFF_MEAS point1 f=25000000Hz D=40 请求=22.500° 直接测得=22.500° 网格=1.1250°
HANDOFF_MEAS point1 T1=40000.0ps T2=40000.0ps delay=2500.0ps | 测量: gate=2003 edge=1003 phaseCnt=1003 → 2500.0ps
HANDOFF_MEAS point2 f=8000000Hz D=125 请求=0.000° 直接测得=0.000° 网格=0.3600°
HANDOFF_MEAS point2 T1=125000.0ps T2=125000.0ps delay=0.0ps  | 测量: gate=2005 edge=321 phaseCnt=16050 → 125000.0ps
HANDOFF_MEAS point3 f=8000000Hz D=125 请求=90.000° 直接测得=90.000° 网格=0.3600°
HANDOFF_MEAS point3 T1=125000.0ps T2=125000.0ps delay=31250.0ps | 测量: gate=2006 edge=321 phaseCnt=4173 → 32500.0ps
HANDOFF_DONE 全部 4 个点通过
```

怎么读这张表：

* **直接测得**：在波形里量 `clkOut1` 上升沿 → 下一个 `clkOut2` 上升沿。输出相位由 unisim 的
  `MMCME2_ADV` 行为模型独立决定，因此「直接测得 = 请求值」反过来证明了软件侧的寄存器编码
  （`DELAY_TIME = 相位码 >> 3`、`PHASE_MUX = 相位码 & 7`）是对的——包括 22.5° 这种需要细调的点
  （25MHz/D=40：相位码 20 → DELAY_TIME 2 + PHASE_MUX 4 → 2.5 个 VCO 周期 = 2500ps ✓）；
* **测量**：`phaseCnt/edgeCnt × 2.5ns`。point1 与直接测得值完全相同；point3 差 1250ps = 0.5 个 DSP
  周期（13 个周期 vs 12.5 个），在判据「≤ 1 个 DSP 周期 + 1 毫度」内；
* point0/point2 请求 0° 时读数是一个整周期（40000ps / 125000ps）——两沿几乎重合时相位计「等下一个
  `extSig_sig2` 沿」落到下一周期，与上板观察到的一致（0° 边界约定，比较按取模 360° 处理，见 10.3 与
  `docs/onboard-ax7010.md`）。

**覆盖范围与限制**：默认跑 4 个代表点（2 个频点 × 2 个相位），不是完整 7 频点 × 4 相位；xsim 跑
MMCM 行为模型一次锁定约 1~2ms 仿真时间，加点数墙钟时间线性增长（本次 4 点约 6 分钟）。改点表只需改
`sim/tb_src_handoff.sv` 里的 `N_POINTS` 与三张点表。

### 10.7 原始输出（留档）

* 上板验收报告：`build/onboard_acceptance.md`、`build/onboard_acceptance.json`、
  `build/onboard_acceptance.csv`
* 离线演练（C 验证台）：`build/onboard_accept_sim/onboard_acceptance.md`
* L3 联调日志：`spec/golden/handoff_meas.txt`
* 端到端结果：`spec/verify_results_cserver.json`、`spec/verify_extras.json`

## 11. 2026-10-16 第二轮：扩大频点与 >180° 相移覆盖

本节取代第 10 节里「相位上界 = 180°」「4 个代表点 / 7 频点 × 4 相位」等说法。
被测对象与第 10 节**同一块板、同一份 bitstream**（PL RTL 未改），只有固件（`dpm_cmd.c`
的相位校验）与验证脚本/金标准/测试更新，固件在 02:38 重新交叉编译后经 JTAG 重新加载。

> **SD 卡**：全程只走 JTAG（`xsdb -quiet ps/tools/jtag_boot.tcl`：`rst -system -stop`
> 先停核再配 PL、下载到 OCM 后 `con`），没有向 SD 卡写任何数据，卡上原有镜像未被改动
> ——该脚本里也没有任何 mmc/文件写入操作。

### 11.1 规则变更：相位上界从「写死 180°」改为硬件依据

MMCM 的相位码 `m = round(相位 × D / 45000)` 只有 **9 位**（`DELAY_TIME` 6 位 +
`PHASE_MUX` 3 位），因此：

* 可请求上界 = `min(23017499 / D, 359999)` 毫度（`m ≤ 511` ⟺ `相位 × D ≤ 23017499`）；
* 越界请求不是「吸附」而是**回绕**：两个字段按位截断（旧 bitstream 实测 8MHz 写 270°
  → 相位码 750 → 截成 238 步 → 读到 86.4°，见 §5 与 `docs/onboard-ax7010.md`）；
* 旧的「`phase < 180000`」比硬件保守得多（D=40 时硬件能到 574.875°），把 D ≤ 63 频点的
  180°~360° 白扔掉了。相位计测的是**无符号**间隔（`extSig_sig1` 沿 → 下一个 `extSig_sig2` 沿，
  落在 `(0, 360°]`），因此 >180° 的相移本来就可测。

新规则在三处实现里一致，并各有单测对拍：

| 处               | 判据                                                                                                                                   | 边界单测                                                                                                                     |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| C（固件）        | `ps/include/dpm_regs.h::dpm_mmcm_phase_code_fits` / `dpm_mmcm_phase_max_mdeg`，`dpm_cmd.c::handle_set_sig` 在求解出 D 之后判 `m ≤ 511` | `ps/tests/test_sig.c::test_phase_bounds`（上界点恰好 511 步、上界+1 被拒、180° 恒合法、D=125 写 270° 的截断回绕=85680 毫度） |
| Python（上位机） | `host/dpm_host/sig.py::phase_code_fits` / `phase_max_milli_deg` / `phase_code_top_milli_deg`                                           | `host/tests/test_sig.py`（1..126 全 D 扫描、上界±1、越界回绕并给出提示）                                                     |
| 独立参考（spec） | `spec/gen_golden.py::phase_points_for` / `spec/sim_server.py::phase_code_fits`                                                         | `spec/selftest_spec.py` 逐向量断言 `相位码 ≤ 511` 且 `相位 ≤ phase_max(D)`                                                   |

### 11.2 频点与相位点：10 个频点、79 个点（31 个 ≥180°）

> **本节是 400MHz 轮次的历史记录**：下表频点（5/6.25/8/10/12.5/16/20/25/40/50MHz）与 79 个点
> 是当时 DSP=400MHz 的冻结表；**该表已作废**，当前 450MHz 的冻结表与相位点见
> [`ps-protocol.md`](ps-protocol.md) §6.4（80 个点、32 个 ≥180°），验证数据尚未重跑。

频点从 7 个扩到 10 个（**全部是求解规则的精确解**）：5 / 6.25 / 8 / 10 / 12.5 / 16 / 20 /
25 / 40 / 50 MHz，覆盖 VCO 600/750/800/1000MHz、MULT 12/15/16/20 与
D = 20/25/40/50/80/100/120/125。相位点**逐频点**按上界裁剪（`0/45/90/135/180` 必测，
D 偶数加 `22.5°`，`225/270/315°` 按相位码裁剪，一个都放不下时用相位码上界点兜底）：

| 频点     | D   | 相位上界 | 相位点（度）                                 | 点数 |
| -------- | --- | -------- | -------------------------------------------- | ---- |
| 5 MHz    | 120 | 191.812° | 0, 22.5, 45, 90, 135, 180, **191.625**       | 7    |
| 6.25 MHz | 120 | 191.812° | 0, 22.5, 45, 90, 135, 180, **191.625**       | 7    |
| 8 MHz    | 125 | 184.139° | 0, 45, 90, 135, 180, **183.96**              | 6    |
| 10 MHz   | 100 | 230.174° | 0, 22.5, 45, 90, 135, 180, **225**           | 7    |
| 12.5 MHz | 80  | 287.718° | 0, 22.5, 45, 90, 135, 180, **225, 270**      | 8    |
| 16 MHz   | 50  | 359.999° | 0, 22.5, 45, 90, 135, 180, **225, 270, 315** | 9    |
| 20 MHz   | 50  | 359.999° | 0, 22.5, 45, 90, 135, 180, **225, 270, 315** | 9    |
| 25 MHz   | 40  | 359.999° | 0, 22.5, 45, 90, 135, 180, **225, 270, 315** | 9    |
| 40 MHz   | 25  | 359.999° | 0, 45, 90, 135, 180, **225, 270, 315**       | 8    |
| 50 MHz   | 20  | 359.999° | 0, 22.5, 45, 90, 135, 180, **225, 270, 315** | 9    |

合计 **79 个点，其中 ≥180° 的 31 个**。加粗的是 >180° 的点。

选频点还发现了两条硬约束（写进 `spec/gen_golden.py` 的断言与 `docs/ps-protocol.md` §6.4）：

1. 信号周期必须是整数 ps（`D×10^12/VCO`），否则金标准生成器无法用整数运算——7.5/15/30MHz
   这类含因子 3 的频率被排除；
2. 信号周期必须是 **DSP 周期（2.5ns）的整数倍**（`8D / MULT` 为整数），此时
   `phaseCnt % edgeCnt == 0` 严格成立。**这条是 xsim 联调发现的**：点表里一度放进
   6.4MHz（VCO=800/D=125），79 点里 8 个点报 `phaseCnt % edgeCnt = 129`——
   非整数比时每个信号周期的相位计数窗口在 `k` 与 `k+1` 之间交替。

### 11.3 结果：xsim 联调 79/79、上板验收 79/79（93/93 项）

**L3 联调（真实 MMCM 行为模型 + DRP 控制器 + 测量通路，xsim）**：

```text
HANDOFF_INFO 测试点表：10 个频点、79 个点（其中 ≥180° 的 31 个）
HANDOFF_SUMMARY points=79 freqs=10 over180=31 fails=0
HANDOFF_DONE 全部 79 个点通过（10 个频点，含 31 个 ≥180° 的点）
```

每个点判 4 条：直接测得的输出相位 = MMCM 网格值、测量通路 = 直接测得值、
测量通路 = 网格值（≤1 个 DSP 周期）、计数模型（`gate ∈ [target, target+ceil(T_sig/T_slow)+5]`、
`edge` 落在闸门区间折算的范围内、`phaseCnt % edgeCnt == 0`）。
整轮墙钟 **17s**（10 个频点、79 个点；每点含一次 MMCM 重配置 + 40µs 闸门）。
`sim/run_handoff_tb.sh` 的验收已收紧为「`HANDOFF_SUMMARY` 的 `fails=0` 且
`HANDOFF_MEAS` 行数 = 2×points」——只数「有没有输出」会把中途 `$fatal` 当成成功。

>180° 的代表性点（`spec/golden/handoff_meas.txt`）：

| 点                            | 请求     | MMCM 网格值 | 直接测得 | 测量通路                                     |
| ----------------------------- | -------- | ----------- | -------- | -------------------------------------------- |
| 8MHz（D=125，相位码上界 511） | 183.96°  | 183.960°    | 183.960° | 65000ps（约 180°，1 个 DSP 周期 7.2° 内）    |
| 5MHz（D=120，相位码上界 511） | 191.625° | 191.625°    | 191.623° | 107500ps vs 直接 106457ps（0.4 个 DSP 周期） |
| 16MHz（D=50）                 | 315°     | 315.000°    | 314.997° | 55000ps vs 54687ps                           |
| 12.5MHz（D=80）               | 270°     | 270.000°    | 270.000° | 40000ps（与直接测得完全一致）                |

**上板验收（ALINX AX7010，JTAG 冷启动加载 02:38 的固件）**：

```text
==== 片上验收：PASS （93/93 项通过）====
[PASS] 扫描 79 点（10 个频点，其中 ≥180° 的 31 点）全部通过  (通过 79/79；…)
```

>180° 的实测（`build/onboard_acceptance.csv`，判据 = `|实测 − MMCM 网格期望| ≤ 1 个 DSP
量化步长`，另有频率与计数自检）：

| 频点/D        | 请求                      | MMCM 期望 | 实测                                      | 误差   | 容限（1 个 DSP 周期） |
| ------------- | ------------------------- | --------- | ----------------------------------------- | ------ | --------------------- |
| 5MHz / 120    | 191.625°                  | 191.625°  | 189.000°                                  | 2.625° | 4.500°                |
| 6.25MHz / 120 | 191.625°                  | 191.625°  | 191.250°                                  | 0.375° | 5.625°                |
| 8MHz / 125    | 183.96°                   | 183.960°  | 179.999°                                  | 3.961° | 7.200°                |
| 10MHz / 100   | 225°                      | 225.000°  | 224.999°                                  | 0.001° | 9.000°                |
| 12.5MHz / 80  | 180° / 225° / 270°        | 同请求    | 180.000° / 225.000° / 270.000°            | 0.000° | 11.250°               |
| 16MHz / 50    | 180° / 225° / 270° / 315° | 同请求    | 172.800° / 216.000° / 259.200° / 316.800° | ≤10.8° | 14.400°               |
| 20MHz / 50    | 180° / 225° / 270° / 315° | 同请求    | 180.000° / 216.000° / 270.000° / 306.000° | ≤9.0°  | 18.000°               |
| 25MHz / 40    | 180° / 225° / 270° / 315° | 同请求    | 180.000° / 225.000° / 270.000° / 315.000° | 0.000° | 22.500°               |
| 40MHz / 25    | 180° / 225° / 270° / 315° | 同请求    | 180.000° / 216.000° / 252.000° / 287.999° | ≤27.0° | 36.000°               |
| 50MHz / 20    | 180° / 225° / 270° / 315° | 同请求    | 180.000° / 225.000° / 270.000° / 315.000° | 0.000° | 45.000°               |

读法：MMCM 的移相网格（`45/D`）与测量端的 DSP 采样网格（`360·f_sig/f_dsp`）是**两重**
量化，后者在高频点很粗（40MHz → 36°、50MHz → 45°）。因此 40/50MHz 上「相位」这一维只有
一档分辨率，主要验证的是**频率与相移量级**（例如 225° 没有被回绕成 45°）；20MHz 及以下
（1 个 DSP 周期 ≤ 18°）才谈得上相位精度。50MHz 上 45° 的整数倍恰好落在整数个 DSP 周期，
所以 180/225/270/315° 依旧逐位吻合。

### 11.4 修正：三条判据/实现问题（前两条是判据过紧，第三条是实现缺陷）

| #   | 位置                                        | 现象                                                                                                                                               | 结论与修正                                                                                                                                                                                                                                                                                                                             |
| --- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `onboard_accept.py` 计数自检                | 40MHz/90°、135°、225° 与 50MHz/45°~180° 共 7 点 FAIL，但相位误差远小于容限                                                                         | 原判据 `abs(edge − round(gate×f/f_slow)) ≤ 1` 按**实测 gate** 反推，忽略了相位计的 DSP 窗口比闸门计数窗口多几拍 CDC/启动对齐延迟（≈2~3 个慢时钟）：高频点 1 个沿 = 1.0~1.25 个慢时钟，偏差因此到 2 个沿。改为与 xsim 激励同一口径——按闸门**区间** `[GATE, GATE+ceil(T_sig/T_slow)+5]` 折算 edge 范围再放宽 1 个沿。相位判据不变        |
| 2   | `onboard_accept.py` 流上报                  | 冷启动后 `seq=[0,0,1]`，严格递增判据 FAIL                                                                                                          | 固件 tick 的语义是「周期性推送**最新**结果」（`dpm_cmd.c` 的 stream 分支）：闸门与上报周期都是 100ms 时，两次 tick 之间可能还没出新结果，于是同一条结果被推两次。判据改为「单调不减 + 至少 3 个不同 seq」（实跑 `[0,0,1,2]`）。这不是协议缺陷，`docs/ps-protocol.md` 的 STREAM_DATA 语义即「最新结果 + lostDelta」                     |
| 3   | **`host/dpm_host/sim_server.py`（真缺陷）** | Python 仿真后端仍按老规则接受相位：5MHz/225° 被接受、读数回绕成 31.5°，而真机/固件会回 `ERR_PARAM`；CLI 的 `scan` 又会在第一个被拒点上中断整轮扫描 | 仿真后端补上 `phase_code_fits` 判据（与 `dpm_cmd.c` 同一规则，消除「仿真接受、真机拒绝」的假象）；`DpmClient.scan` 把 `ERR_PARAM` 视为「该点不可实现」跳过继续（其它错误码仍抛出）；`cmd_scan` 的扫描计划预先列出越界组合与实际行数。新增用例钉住：>180° 合法、越界被拒、扫描跳过越界点（`host/tests/test_client_over_pty.py`，+9 项） |

### 11.5 全量门禁（本轮复跑）

| 门禁                      | 命令                                                                                     | 结果                                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Chisel/Verilator          | `xmake test -v`                                                                          | 4 个目标、100% 通过（13+17+110+545 断言）                                                                                |
| xsim 顶层激励             | `sim/run_xsim.sh --no-gen`                                                               | 通过                                                                                                                     |
| **xsim 联调（本轮扩表）** | `sim/run_handoff_tb.sh --no-gen`                                                         | **79/79（10 频点，31 个 ≥180°），17s**                                                                                   |
| C 单测 + 服务端自测       | `ctest --test-dir ps/build/cmake`                                                        | 4/4（`test_sig` 579 项、`test_calc` 325 项、`test_proto` 120 项、`test_server_selftest` 619/619——79 条黄金向量逐位一致） |
| Python 上位机             | `uv run pytest -q`                                                                       | **667 项通过**（protocol 58 + mathx 248 + pty 29 + sig 221 + sim 111）                                                   |
| 规格自检                  | `uv run python spec/selftest_spec.py`                                                    | **1284 项检查，0 失败**（含逐向量相位码/上界断言、协议文档表格与相位点表一致性）                                         |
| 端到端扫描（C 服务端）    | `uv run python spec/validate_sweep.py --server "./ps/build/host_main --device {device}"` | **79 点全部 PASS**（sent=636=recv、crc_err=0）                                                                           |
| 补充用例（C 服务端）      | `uv run python spec/verify_extras.py --server …`                                         | 15/15（新增「相位码越界 → ERR_PARAM」「270° 合法」两条）                                                                 |
| 离线演练（上板前置）      | `bash ps/tools/onboard_accept_sim.sh`                                                    | 93/93                                                                                                                    |
| **上板验收**              | `uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0`                           | **93/93（扫描 79/79）**                                                                                                  |
| GUI 冒烟（PySide6）       | `uv run python host/tools/gui_smoke.py`                                                  | PASS（`build/gui_smoke/gui_final.png`）                                                                                  |

### 11.6 未验证 / 未做的部分

* 40MHz/50MHz 的**相位精度**只有 36°/45° 一档分辨率（1 个 DSP 周期），本轮的判据只能证明
  「相位跟着请求走、没有被回绕或拒绝」，不能证明优于这一档的精度；
* 相位上界附近的**极端点**（`m = 511` 之上一点、359.999° 这类贴满量程的点）没有上板测：
  上界内的最大点（8MHz/183.96°、5MHz/191.625°）已测；
* SD 卡 `BOOT.bin` 自启仍未验证（本机没有 Vitis/FSBL 工具链），本轮也没有改动 SD 卡内容；
* 长稳/温漂统计仍未做（与第 10 节相同）。

## 12. 2026-10-16 第三轮：切到外部输入源后报 `ERR_BUSY`（`GET_RESULT`）的根因与修复

**这一轮没有新的上板证据**：板上现象来自用户报告，根因定位与修复验证在 **C 验证台
（`ps/build/host_main`，与固件同一份 `dpm_cmd.c`）经真 pty 驱动上位机**上完成；上板复验需要
先重烧固件（见 12.5）。

### 12.1 现象与复现

用户报告：上位机切到外部输入源后执行测量，程序直接退出并打印

```text
设备错误：PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）
```

按同一条链路复现（`SET_SOURCE(0)` → `SET_MODE(single)` → `START` → 等结果，C 验证台默认
外部引脚无信号），三个观察量一致：

```text
SET_SOURCE ext → sigStatus=0x0000000C [srcLocked|sourceReady]
START 后：busy=True resultValid=False sourceReady=True
[ERROR] DpmError: PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）
```

### 12.2 根因（三层，缺一层都不会中止程序）

1. **PL（不是缺陷）**：选中的输入源没有边沿时，测量 FSM 停在「等参考沿」——`STATUS.busy = 1`、
   结果永不锁存。这是设计内的**正常输入状态**：选外部输入时 `SIG_STATUS.sourceReady` 恒为 1
   （引脚上是否真有信号，PL 判断不了，见 `SignalInputMux`），`docs/verify-plan.md` 第 3 节
   用例 5 描述的就是这条路径。
2. **PS 固件（缺陷）**：`handle_result` 把「等不到结果」表达成 `GET_RESULT = ERR_BUSY`。
   而协议 §3.0 规定 `GET_RESULT.valid` 是「结果是否就绪」的权威探针，`ERR_BUSY` 的语义是
   「当前状态不允许该命令」；两个参照实现（`host/dpm_host/sim_server.py`、`spec/sim_server.py`）
   在无结果时都回 `valid = 0`，只有 C 侧固件回 `0xFF`。
3. **上位机（缺陷）**：`DpmClient.wait_meas` 把 `GET_MEAS` 的 `ERR_BUSY` 当「暂无结果」继续
   轮询，却把 `GET_RESULT` 上**同一条** `ERR_BUSY` 当致命错误抛出（`DpmError`），于是整段测量
   程序中止——连 `docs/verify-plan.md` 要求的「上位机要能超时退出并提示」都没走到。
4. **上位机 CLI/GUI（缺陷，独立验证发现后补修）**：`cli.py` 的 `status`/`meas` 与 `gui.py` 的
   连续测量循环用的是**裸 `get_meas()`**，`GET_MEAS` 上的 `ERR_BUSY` 同样被当致命错误。触发
   条件更窄但很现实：结果槽里还留着上一次未读的结果（`STATUS.resultValid = 1`），此时再
   `START` 一次却卡在没边的源上——固件按旧结果保护拒绝把上一次的结果当成本次结果，`GET_MEAS`
   回 `ERR_BUSY`，CLI 又打出「设备错误…请求 0x06（GET_MEAS）」。最小复现（纯 CLI）：
   `source int` → `start --no-wait` → `source ext` → `start --no-wait` → `meas`；
   同一状态下 `start`/`monitor` 已经不报设备错误，说明修复漏了这两个调用点。

### 12.3 修复

| 侧             | 文件                                                                                                                                   | 改动                                                                                                                                                                                                                                            |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 固件           | `ps/src/dpm_cmd.c`、`ps/include/dpm_cmd.h`                                                                                             | `GET_RESULT` 等不到结果时回 `valid = 0`（计数全 0），不再回 `ERR_BUSY`；保留命令**内部**的有界等待（`50 + 4 × 闸门时间` ms），`START` 后紧接一次 `GET_RESULT` 仍能一次读走。`GET_MEAS` 没有 `valid` 字段，保持 `ERR_BUSY`（在协议里写明为例外） |
| 上位机         | `host/dpm_host/client.py`                                                                                                              | `wait_meas` / `wait_result` 把两条路径上的 `ERR_BUSY` 统一当「还没有结果」，轮询到自己的超时再抛 `DpmTimeoutError`；新增输入源诊断（当前源、`busy`/`resultValid`/`sourceReady`、查线建议、如何恢复）                                            |
| 上位机 CLI/GUI | `host/dpm_host/cli.py`、`host/dpm_host/gui.py`                                                                                         | `status`/`meas` 改用 `try_get_meas()`：读不到就**说明原因**（`status`，仍算成功）/ 退回「等一次新结果」（`meas`）；连续测量循环同样容错，读不到 `GET_MEAS` 时用刚读到的 `GET_RESULT` 计数本地换算，不再因一条 `ERR_BUSY` 中断                   |
| 协议           | `docs/ps-protocol.md` §3.0、§3.2                                                                                                       | 写明「『还没有结果』用 `valid = 0` 表达，不要用 `ERR_BUSY`」与 `GET_MEAS` 的例外                                                                                                                                                                |
| 回归           | `host/tests/test_client_over_pty.py`、`ps/tests/test_server_selftest.py`、`host/tools/interop_host_main.py`、`host/tools/gui_smoke.py` | 新增 `BusyResultPeer`（模拟仍回 `ERR_BUSY` 的固件）4 项单测（含 CLI `status`/`meas` 的残留路径）；固件侧 `GET_RESULT → valid=0` 断言；互通脚本新增「外部输入源（无信号容错路径）」段；GUI 冒烟新增「外部无信号时单次测量」步                    |

修复后同一条链路的输出（`host/tools/interop_host_main.py --spawn ./ps/build/host_main`）：

```text
SET_SOURCE(ext) → sigStatus=0x0000000C sourceReady=1
START → status=0x0000000B（busy=1 resultValid=1）
[OK] 等不到结果 → 超时（不是设备错误）：等待响应超时，timeout=1.5s：等待测量结果超时（源没有边沿时测量永不完成）；
     当前输入源 = 外部引脚 extSig_sig1/sig2，status=0x0000000B（busy=1 resultValid=1）、sigStatus=0x0000000C（sourceReady=1）；
     外部引脚没有边沿：确认 extSig_sig1/extSig_sig2 已接两路同频信号、与开发板共地，电平满足引脚 IO 标准（默认 LVCMOS33），
     且频率落在闸门窗口内；结果槽里还留着上一次未读的结果（resultValid=1），本次测量还没产出新结果——
     对端按「不把旧结果当新结果」处理，所以这里继续等的是本次的结果；
     挂起的这次测量会一直等边沿：接好信号或切回片内信号源后它才会结束
[OK] GET_RESULT → valid=0（等不到结果时必须用 valid=0，不是 ERR_BUSY）
```

`START → status=0x…0B`（`busy=1` 且 `resultValid=1`）是上一轮测量留下的**未读结果**：
固件按 `valid_before`/`seq_before` 保护不把它当本次结果交出去，上位机因此继续等——这条旧结果
保护逻辑在修复前后都成立（`ps/tests/test_server_selftest.py::test_result_clear` 覆盖）。

**兼容性**：只更新上位机、暂不重烧固件也能消除用户看到的「设备错误」——轮询到自己的超时后
报超时 + 诊断。拿旧固件跑互通脚本会打印 `[FAIL] GET_RESULT 回了错误响应 0xFF` 并以退出码 1
结束：那一条是固件侧的一致性断言，要重烧固件才变绿（已用旧固件二进制实测确认）。

### 12.4 全量门禁（本轮复跑）

| 门禁                             | 命令                                                                                     | 结果                                                                                     |
| -------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Verilator（RTL 未改，顺带复跑）  | `xmake test -v`                                                                          | 4 个目标 100% 通过（13 + 17 + 110 + 545 断言）                                           |
| Python 上位机                    | `uv run pytest -q`                                                                       | **700 项通过**（新增 4 项 ERR_BUSY 容错用例）                                            |
| 规格自检                         | `uv run python spec/selftest_spec.py`                                                    | **1284 项检查，0 失败**                                                                  |
| C 单测 + 服务端自测              | `ctest --test-dir ps/build/cmake --output-on-failure`                                    | 4/4（`test_server_selftest` **620/620**，含新增的「外部无信号：GET_RESULT 回 valid=0」） |
| 固件交叉编译                     | `bash ps/build.sh`                                                                       | 零警告（`-Wall -Wextra -Werror`）、`nm -u` 为空                                          |
| 端到端扫描（C 服务端）           | `uv run python spec/validate_sweep.py --server "./ps/build/host_main --device {device}"` | **79 点全部 PASS**（sent=636=recv、crc_err=0）                                           |
| C 验台互通（含新的外部源容错段） | `uv run python host/tools/interop_host_main.py --spawn ./ps/build/host_main`             | 退出码 0                                                                                 |
| 离线演练（上板前置）             | `bash ps/tools/onboard_accept_sim.sh`                                                    | 93/93                                                                                    |
| GUI 冒烟（PySide6，无头）        | `QT_QPA_PLATFORM=offscreen uv run python host/tools/gui_smoke.py --scan`                 | PASS（含「外部无信号时单次测量」步）                                                     |

### 12.5 未验证 / 未做的部分

* **上板**：本轮没有板上的 bitstream/固件改动，也没有重新烧写。板上要拿到固件侧的
  `valid = 0`，需 `bash ps/build.sh --ps-only` 后按 `docs/bringup.md` 重新加载/更新 `BOOT.bin`；
* **外部输入源真机路径**（`docs/verify-plan.md` 第 6.5 条）仍**没有上板验证**：两路同频信号
  接到 `extSig_sig1`(W18)/`extSig_sig2`(W19) 后能否测到正确频率/相位，要有信号源才能下结论。本轮只证明了
  「**没有**信号时不再报设备错误」这条容错路径；
* 「挂起的这次测量会自行结束」只有模型/RTL 层面的依据：真实 PL 上源恢复边沿后，测量 FSM 要
  先等到闸门计数器回到 `targetGate`（28 位 @50MHz，最长回绕一次 ≈5.4s）再等一个 `extSig_sig2` 沿，
  因此**不是立刻**结束，这一点没有上板计时确认；C 侧模型则完全不恢复（第 6 节已知差异 1）；
* 真实串口的重试/超时窗口（上位机 `--timeout` 默认 0.5s、`--result-timeout` 默认 2s）在板上
  未复跑；修复后的诊断依赖一次额外的 `GET_STATUS`（上限 0.2s），与真机时延的配合未实测。

### 12.6 独立验证（另一名验证者，只读 + /tmp 重建）

修复后由**独立验证者**用自己写的脚本重做了一遍，结论与上面的记录一致，并补出两条本次修复
**没覆盖到**的路径（第 4 层根因、见 12.2）：

| 项                           | 独立验证结果                                                                                                                                                                                                                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 复现修复前故障               | `git worktree add /tmp/dpm_prefix HEAD` + 重建验证台：裸帧 `GET_RESULT → cmd=0xFF`；客户端 `DpmError code=255`，原文与用户报告**逐字相同**；CLI 打印 `设备错误：…请求 0x05（GET_RESULT）`                                                                                                   |
| 修复后                       | 裸帧 `cmd=0x85 payload=0000…`（valid=0）；客户端 `DpmTimeoutError code=0`（2.00s）含输入源诊断；CLI 无「设备错误」                                                                                                                                                                          |
| 反向验证（成功路径未被掩盖） | `--ext-present` 10MHz/0°、`--unlock` 下 `SET_SOURCE(int)`/`START` 仍回 `ERR_BUSY`、测量中再次 `START` 仍回 `ERR_BUSY`（需 `--speed 0 --gate 200000` 构造真 busy）、`START` 后**一次** `GET_RESULT` 一次读走（payload 与修复前逐字节相同）                                                   |
| 门禁                         | `ps/tests/test_server_selftest.py` 620/620、`interop_host_main.py` 退出码 0、`gui_smoke.py` PASS（均用仓库外副本 / 只读方式跑）                                                                                                                                                             |
| 发现的残留路径               | CLI `meas`/`status` 的裸 `get_meas()`（12.2 第 4 层）→ 已补修，并加 `test_cli_status_and_meas_survive_busy_get_meas` 钉住（未补修前该用例以 `ERR_BUSY … 请求 0x06（GET_MEAS）` 失败）                                                                                                       |
| 记录在案但**未**修的既有问题 | ① 流上报在源没有边沿时会持续重发缓存旧结果（`dpm_cmd_tick` 只判 `have_result`，帧里没有 valid 位），`monitor --stream` 会把它当样本；② 先 `start --no-wait`、再切到无信号源后 `meas` 会**直接打印上一次的片内源读数**（exit 0）——两者在本轮改动前后表现相同，属既有语义问题，未纳入本次修复 |
| 未能验证的                   | 真实板子/引脚/MMCM/真实串口；RTL 级「无信号」行为（`test/Controller.cpp` 恒驱周期信号，本轮未新增该用例）                                                                                                                                                                                   |

## 13. 2026-10-16 第四轮：长闸门时间下的测量被误报「超时」

**这一轮没有上板证据，也不需要**：问题在 PC 侧上位机的超时口径上，改动只涉及 `host/`。
板上暂无长闸门实测（见 13.5），验证在三个后端上完成：`sim_server`（墙钟模型，真实等待 2.4s）、
自建的 pty 对端（复现 C 固件「命令内部有界等待」的语义）、C 验证台（`ps/build/host_main`，
同一份 `dpm_cmd.c`，虚拟时间与墙钟解耦）。

### 13.1 现象

把闸门时间调长（`SET_GATE` / GUI 的「闸门时间」输入框）后，测量**明明在正常进行**，上位机却报

```text
测量未完成：等待响应超时，timeout=2s：等待测量结果超时（源没有边沿时测量永不完成）；……
```

### 13.2 根因（两处，都在上位机的固定超时上）

一次测量的时长就是闸门窗口 `T_gate = gateCnt / f_slow ≈ gateTarget / f_slow`
（`docs/ps-protocol.md` 第 5 节）；RTL 的闸门计数器是 28 位，50MHz 下最长 **5.37s**，
而旧实现的超时是写死的：

1. **等结果的总预算固定 2.0s**（`--result-timeout` 默认值，GUI 单次测量是
   `max(2.0, timeout×4)`）：闸门 > 2.0s 时结果还没锁存，预算就先到期——必然误报；
2. **单次请求超时固定 0.5s**（`--timeout` 默认值）：真机固件在 `GET_RESULT` / `GET_MEAS`
   命令**内部**等结果（`ps/src/dpm_cmd.c` 的 `ensure_result` → `dpm_cmd_wait_result`，
   `50 + 4 × 闸门时间` ms 的有界等待，协议 §3.0），`SET_SIG` / `CLEAR` 也会在命令内部
   `wait_idle`；因此闸门 > 约 0.11s 时单次请求也会被拖爆。

另外 GUI 把闸门时间上界压在 **2.0s**（`GATE_MAX_SECONDS`，理由是「闸门长了用户等不到结果」），
正是同一处固定超时的产物——长闸门想设也设不进去。

### 13.3 修复

| 文件                       | 改动                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `host/dpm_host/client.py`  | 新增 `GATE_TIMEOUT_FACTOR = 2.0` / `GATE_TIMEOUT_SLACK_S = 0.25` 与 `gate_wait_budget_seconds()`：等待预算 = `max(调用方给的值, 2×闸门窗口 + 0.25s)`。`DpmClient.gate_target` 由 `GET_STATUS.gateTarget` / `SET_GATE` 回显**自动缓存**；`request()`、`wait_result` / `wait_meas` / `wait_idle` / `wait_source_ready` 一律按它放大；闸门未知时先补读一次 `GET_STATUS`（`_ensure_gate_target`，`start --wait` / `meas` 这类不先读状态的路径靠它）；等待过程中单次请求超时只当「这一拍没问出结果」，继续轮询到总预算；`_status_or_none` 显式关掉放大（诊断不该再等一个闸门窗口）；超时诊断里加上「闸门多长、等了多久」 |
| `host/dpm_host/cli.py`     | `--timeout` / `--result-timeout` 的帮助写明「只是下限」；`gate` 子命令打印生效闸门与等待预算（`--json` 含 `waitBudgetSeconds`）；`start`/`meas`/`monitor`/`scan` 的超时提示补上闸门预算一行                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `host/dpm_host/gui.py`     | 「闸门时间」实时提示加上等待预算；闸门上限从 2.0s 放开到 28 位计数器的真实上界（`GATE_MAX_CYCLES = GATE_TARGET_MAX`，50MHz 下 ≈5.37s）；设置闸门后日志里报出预算                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `docs/ps-protocol.md` §3.0 | 新增「上位机的等待预算随闸门时间放大」一条（PC 侧约定，不改 PS 行为）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 回归                       | `host/tests/test_gate_timeout.py`（14 项纯单元：预算换算、缓存、诊断文本、CLI 接线、`GET_STATUS` 失败容错）；`host/tests/test_client_over_pty.py` 新增 5 项：`LongGatePeer`（命令内部阻塞 0.6s / 结果按窗口锁存）四项 + `sim_server --gate 120000000`（2.4s）的端到端一项；`host/tests/test_gui.py` 新增闸门上限/预算提示 1 项、改写夹取范围 1 项；`host/tools/gui_smoke.py` 新增「长闸门 2.4s 单次测量」步                                                                                                                                                                                                         |

### 13.4 验证（本轮实测）

| 证据                                | 命令 / 后端                                                                              | 结果                                                                                                      |
| ----------------------------------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| 反向验证（修复前确实会失败）        | 把系数临时置 0 后跑长闸门用例（等价于旧口径）                                            | 5 项里 **4 项失败**（请求级、结果级、闸门未知路径、2.4s 端到端），只有「默认闸门不放大」的回归项照常通过  |
| 结果级预算（真实墙钟 2.4s 闸门）    | `sim_server --gate 120000000` + `DpmClient(timeout=0.5).wait_meas(timeout=2.0)`          | 成功，`gate_cnt=120000001`，耗时 ≈2.4s（旧口径在此必然报超时）                                            |
| 请求级预算（对端在命令内部阻塞）    | `LongGatePeer`（pty，闸门 0.6s / 基准 0.1s）                                             | 成功，`stats.timeouts == 0`（旧口径在 0.1s 就超时）                                                       |
| 闸门未知路径（`start --wait` 语义） | `LongGatePeer`，不先读状态                                                               | 成功，等待过程中补读到 `gateTarget`，预算随之放大                                                         |
| 默认闸门不被拖慢                    | `LongGatePeer`（2000 周期，0.2s 基线）                                                   | 仍在 ≈0.25s 报超时（而不是白等到几秒）                                                                    |
| C 固件内核互通（长闸门）            | `ps/build/host_main --pty --gate 120000000` + `SET_GATE`→`wait_idle`→`START`→`wait_meas` | `gateCnt=120000002`、`timeouts=0`，但墙钟只花 12.1ms——该验证台虚拟时间与墙钟解耦，**证明不了**第 2 条根因 |
| CLI 端到端（长闸门扫描）            | `python -m dpm_host --port <pty> scan --gate 120000000 --freqs 25e6 --phases 90`         | 1 行结果 `gate=120000002`、`err=+0.0000°`，耗时 2.54s                                                     |
| GUI 冒烟（含长闸门步）              | `QT_QPA_PLATFORM=offscreen uv run python host/tools/gui_smoke.py --scan`                 | PASS（长闸门步：`gateCnt=120000002`，无「等待测量结果超时」）                                             |
| 全量 Python                         | `uv run pytest -q`                                                                       | **720 项通过**（新增 20 项：14 纯单元 + 5 pty + GUI 改写）                                                |
| 规格自检                            | `uv run python spec/selftest_spec.py`                                                    | 1284 项检查，0 失败（协议文档新增段落未破坏文档↔头文件一致性检查）                                        |

### 13.5 未验证 / 未做的部分

* **真实板子上的长闸门**没有实测：本轮没有 bitstream/固件改动，也没有重新烧写。真机上要确认的是
  「`GET_RESULT` 在命令内部等满一个 2~5s 的闸门窗口后回结果」这条时序，以及 28 位闸门计数器在
  `gateTarget` 接近 `0x0FFFFFFF` 时 `gateCnt = target + ceil(T_sig/T_slow)` 是否会溢出（本轮未查）；
* **C 验证台的墙钟与虚拟时间解耦**：它按事务推进模型时间，长闸门只花十几毫秒墙钟，因此它只能证明
  「长闸门下协议互通、结果正确」，不能证明「请求级超时会被闸门拖爆」——后者目前只有 pty 对端
  （按协议语义建模）与 `sim_server`（真实墙钟）两条仿真证据；
* **预算系数（2× 闸门 + 0.25s）是工程余量**，不是从硬件时延推导出来的：真机上若存在测量重启、
  MMCM 重锁、串口大缓冲等额外延迟，仍需按实测调整；系数与余量都集中在
  `client.py` 的两个常量里，便于改；
* **`--timeout` 的语义变化**：它从「硬上限」变成「下限」。想让某次请求**更早**失败（例如探活）
  得用 `gate_aware=False`，CLI/GUI 目前没有暴露这个开关（诊断路径内部使用）；
* **流模式（`monitor --stream`）的等待**没有跟着改：`STREAM` 的上报周期仍是固定的
  `intervalMs`，长闸门下会重复上报同一条旧结果（12.6 已记录的既有问题，本轮未动）；
* **PS 侧的等待上限当时仍是旧口径**（本轮只改上位机）：`DPM_WAIT_TIMEOUT_MS(gate, slow)`
  = `50 + 4 × 闸门时间`，其中「闸门时间」取的是**微秒数值当毫秒用**，所以 2.4s 闸门下
  `GET_RESULT` 在命令内部最多可等 ≈9600s（2.7 小时）。**已在 §14 修复**：改成
  `50ms + 4 × 闸门时间（秒）`。

## 14. 2026-10-16 第五轮：PS 侧等待上限的量纲错误（长闸门下等 2.7 小时）

这一轮补的是 §13 记下的 PS 侧问题（**没有上板证据**：改动是纯软件的时间上限换算），外加一次
仓库内 Python 脚本的统一格式化。

### 14.1 现象与根因（PS 侧，量纲）

`ps/include/dpm_cmd.h` 的 `DPM_WAIT_TIMEOUT_MS` 写成「`50 + 4 × 闸门时间的微秒数值`」，
把 µs 当 ms 用：

```c
/* 旧实现 */
(50u + 4u * (uint32_t)(((uint64_t)(gate_target) * 1000u) / (((slow_hz) / 1000u) ? ((slow_hz) / 1000u) : 1u)))
```

- 默认闸门（2000 周期 = 40µs）算出 **210ms**，看起来完全正常——这就是它一直没被发现的原因；
- 闸门一长就放大 1000 倍：2.4s 闸门 → **9600s（2.7 小时）**，28 位上界闸门（5.37s）→ **5.96 小时**。

后果：`GET_RESULT` / `GET_MEAS`（`ensure_result`）与 `SET_SIG` / `CLEAR`（`wait_idle`）会在
命令**内部**按这个上限等；「长闸门 + 源没有边沿」时固件长时间占着命令处理，上位机只能按自己的
预算超时退出（§13 的修复），连诊断用的 `GET_STATUS` 都读不到。

### 14.2 修复

| 文件                               | 改动                                                                                                                                                                                                                                                                                            |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ps/include/dpm_cmd.h`             | `DPM_WAIT_TIMEOUT_MS(gate_target, slow_hz)` = `50ms + 4 × (gate_target / slow_hz)` 秒，中间量用 u64（`4000 × gateTarget ≤ 1.07e12`），`slow_hz == 0` 时按 1 处理避免除零。默认闸门仍是 50ms 量级、上界闸门 ≈21.5s——与闸门同量级、有界                                                           |
| `ps/include/dpm_cmd.h`             | `DPM_WAIT_POLL_LIMIT` **不改数值**，只补注释说明它是**慢时钟周期**意义上的界（每次轮询至少一次寄存器读 → `4 × gateTarget` 个慢周期 ≈ 4 个闸门窗口），与上面的**时间**界量纲不同；它只在 `io->now_ms == 0` 的集成里生效，且提前返回（回 `valid = 0`）是良性的：上位机会继续轮询，PL 里的测量照跑 |
| `docs/ps-protocol.md` §3.0         | 「当前实现 `50 + 4 × 闸门时间` ms」→「`50ms + 4 × 闸门时间（秒）`，上界闸门 5.37s → 最多 ≈21.5s」                                                                                                                                                                                               |
| `ps/tests/test_proto.c`            | 新增 `test_wait_budget()`：钉住 4 个闸门点的数值（默认 2000 周期 → 50ms、100ms 闸门 → 450ms、2.4s 闸门 → 9650ms、28 位上界 → 21524ms）、`slow_hz = 0` 不崩、兜底轮询上限随闸门线性增长（共 8 项检查，与机器速度无关）                                                                           |
| `ps/tests/test_server_selftest.py` | 新增 `test_long_gate_bounded_wait()`：28 位上界闸门 + 外部无信号时，`GET_RESULT` 必须在有界时间内回 `valid = 0`（阈值 3s，见 14.3 的实测），且之后服务端仍能正常处理命令                                                                                                                        |

### 14.3 验证（本轮实测）

| 证据                                            | 命令 / 后端                                                                              | 结果                                                                                                                   |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 反向验证（修复前确实会卡住）                    | 用旧宏在 `/tmp` 里重建 `host_main`（不动工作树），跑新的 selftest                        | **FAIL**：`GET_RESULT` 墙钟 **10.52s**（模型里跑完 2.15e9 次 AXI 事务 = 兜底轮询上限；真实硬件上 ms 上限是 5.96 小时） |
| 修复后同一条用例                                | `uv run python ps/tests/test_server_selftest.py --host-main ./ps/build/host_main`        | PASS：墙钟 **0.083s**（同一位置虚拟时间 21.5s，模型按事务推进，比墙钟快 ≈258 倍），624/624 项通过                      |
| C 单测（精确数值）                              | `ctest -R test_proto`                                                                    | `test_proto` 128 项检查 0 失败（含新增 8 项）                                                                          |
| 全部 C 单测 + pty 端到端                        | `ctest --test-dir ps/build/cmake --output-on-failure`                                    | **4/4 通过**（`test_server_selftest` 4.29s）                                                                           |
| 交叉编译（目标工具链，`-Wall -Wextra -Werror`） | `bash ps/build.sh`                                                                       | 零警告、`nm -u` 为空、镜像 28880B                                                                                      |
| 79 点扫描（C 服务端）                           | `uv run python spec/validate_sweep.py --server "./ps/build/host_main --device {device}"` | **79/79 PASS**，`crc_err=0`、`timeouts=0`                                                                              |
| 补充用例                                        | `uv run python spec/verify_extras.py --server …`                                         | 15/15 通过                                                                                                             |
| 离线上板演练                                    | `bash ps/tools/onboard_accept_sim.sh`                                                    | **93/93 通过**（退出码 0）                                                                                             |
| 上位机互通脚本                                  | `uv run python host/tools/interop_host_main.py --spawn ./ps/build/host_main`             | 退出码 0                                                                                                               |
| 上位机全量                                      | `uv run pytest -q`                                                                       | 720 项通过                                                                                                             |
| 规格自检（文档 ↔ 头文件）                       | `uv run python spec/selftest_spec.py`                                                    | 1284 项检查，0 失败                                                                                                    |
| GUI 冒烟                                        | `QT_QPA_PLATFORM=offscreen uv run python host/tools/gui_smoke.py --scan`                 | PASS（含长闸门步）                                                                                                     |

### 14.4 附带：仓库内 Python 脚本统一格式化

`pyproject.toml` 里本来就有 `[tool.black] line-length = 140` 与 `[tool.isort] profile = "black"`，
但仓库里从没跑过格式化：33 个脚本里 18 个不符合 black、5 个不符合 isort。本轮统一跑了一遍
（只改排版，不改语义）：

```console
$ uv run isort host spec ps      # 修 5 个文件的导入顺序/格式（跳过后端产物 1 个）
$ uv run black host spec ps      # 重排 18 个文件、15 个不变
$ uv run black --check host spec ps   # 33 files would be left unchanged
$ uv run isort --check-only host spec ps   # 干净（Skipped 1 file）
```

覆盖 `host/`（上位机 + 测试 + 工具）、`spec/`（金标准与验证脚本）、`ps/tests` 与 `ps/tools`
（C 验证台的自测与上板脚本）。格式化后上面所有门禁都复跑通过（`pytest` 720、`ctest` 4/4、
`selftest_spec` 1284、79 点扫描、`onboard_accept_sim.sh` 93/93、GUI 冒烟 PASS）。
C 源码（`ps/**/*.c|h`）本轮只动了 §14.2 的两处宏/注释与测试，**没有**整体套用 clang-format：
`ps/` 的 C 风格与仓库根的 `.clang-format` 并不完全一致，整体重排会淹没本次的功能改动。

### 14.5 未验证 / 未做的部分

* **上板**：`DPM_WAIT_TIMEOUT_MS` 只在宿主机验证台（同一份 `dpm_cmd.c`）与交叉编译上验证，
  真实 Zynq 上「长闸门 + 无信号」时固件的解卡时延没有实测（依赖 `dpm_regs_io.c` 的
  Cortex-A9 全局定时器 `now_ms`，代码路径相同）；
* **兜底轮询上限的数值**仍是 `4 × gateTarget + 200000`：它只在 `io->now_ms == 0` 的集成里生效，
  本轮只补了注释说明量纲，没有按真实 AXI 读延迟重新标定；
* **`wait_idle` 路径**（`SET_SIG` / `CLEAR`）用的是同一个宏，本轮只有 `GET_RESULT` 的端到端用例；
  `SET_SIG` 在长闸门 + busy 下的解卡时延没有单独用例；
* **clang-format**：如 §14.4 所述，`ps/` 的 C 代码没有整体格式化。
