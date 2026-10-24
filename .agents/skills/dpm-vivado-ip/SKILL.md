---
name: dpm-vivado-ip
description: 'Use when: 在 DigitalPhaseMeter 中打包 Vivado IP（vivado/package_ip.tcl）、创建或修改 PS↔PL 互联的 block design（vivado/create_bd.tcl）、跑 xsim 的 SystemVerilog 测试激励（sim/）、或做综合实现与导出 XSA（Python 入口 vivado/flow 的 dpm-vivado-flow）。覆盖 IP-XACT 接口推断与补全、地址块、BD 连接与地址分配、xsim 命令行流程、增量复用流程与其实测陷阱。'
argument-hint: '要做的 Vivado/xsim 工作（打包 IP、改 BD、写激励、跑实现）'
---

# DigitalPhaseMeter 的 Vivado IP、BD 与 xsim

当前设计（450MHz DSP 域、方案 A：裸 MMCM + 极简 DRP 控制器）已完成综合实现签核与上板验收。
本 skill 记录维护时需要的流程、约束与实测陷阱；上板步骤见 `docs/bringup.md`。

RTL 本身的写法见 `dpm-chisel-rtl`；测量结果的判据见 `dpm-measurement-model`；
交付前的格式化与回归见 `dpm-pre-delivery-check`。

## 交付物与执行顺序

```text
sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop"    # 顶层 IP RTL
sbtn -batch "runMain DigitalPhaseMeter.EmitMmcmDrpController"       # DRP 控制器 RTL
uv sync                                                 # 一次性：把 dpm-vivado-flow 装进根 .venv
uv run dpm-vivado-flow                                  # 官方增量流程（IP → BD → 综合/实现/XSA）
uv run dpm-vivado-flow --status                         # 只读查看 stamp/指纹/DCP/报告状态
uv run dpm-vivado-flow --force                          # 需要时全量重建
sim/run_handoff_tb.sh                                   # xsim 联调：真实 MMCM 行为模型（同一份生成产物）
vivado -mode batch -source vivado/impl_stability.tcl -tclargs 6  # 同策略 6 次独立实现（签核）
vivado -mode batch -source vivado/impl_multi.tcl -tclargs 6      # 换 place/route directive 扫策略（调优）
vivado -mode batch -source vivado/query_runs.tcl        # 查看 run 的策略/步骤/增量参数（排障）
```

**改了 RTL 之后**：先跑两个 `sbtn` 生成入口，再在仓库根跑 `uv run dpm-vivado-flow`。流程会按内容
哈希只重打包变化的 IP、在原工程内刷新 BD（接口不变时），并只重跑受影响的 OOC run / `synth_1` /
`impl_1`；需要从零重建时用 `--force`。手工只重跑底层 Tcl 打包/BD 脚本仍可，但不会自动复用，
且只重跑综合实现会用到旧 IP。

`build/` 下全部是产物，不入库；入库的是 `.scala`、`test/`、`sim/`、`vivado/` 里的脚本与文档。
**每个模块只有一份产物**：BD、综合与 xsim 都消费 `build/chisel/<Name>/`（顶层是
`build/chisel/DigitalPhaseMeterTop/`，`package_ip.tcl` 的 `rtl_dir` 就指这里）；复位同步是纯 RTL 的
`ResetSynchronizer`，产物里没有 XPM 原语，Verilator 与 xsim / Vivado 编译的是同一份文件。
IP 打包同样按 `filelist.f` 取文件，因此生成模块名/文件名变化（例如 `EdgeDetector` 按 `desiredName`
生成 `EdgeDetectorRising.sv`）不需要改 Tcl；但这类改动会让旧 XSA/bitstream 过期，需要重跑流程。

## 增量流程（dpm-vivado-flow）

入口是根 uv workspace 成员 `vivado/flow`（Python，运行时只用标准库）；Tcl worker 在
`vivado/flow/tcl/`。`uv sync` 把它装进**根 `.venv`**（不单独建环境），接口约定见
`vivado/flow/CONTRACT.md`。

- **阶段**：package（内容哈希决定是否重打包）→ BD（复用 / 原位 `upgrade_ip` 刷新 / 全量重建）→
  build（按 run 指纹选择性 reset + launch + 报告/XSA）。
- **状态**：`build/vivado/.flow/`（`*.json` stamp/指纹、`plans/`、`scripts/`、`results/`、`logs/`、
  `ref/`）；构建产物，删除或 `--force` 即回到全量路径。
- **复用判据**：输入指纹匹配 + 输出 DCP 存在，**不依赖 run 的 `STATUS`**。实测厂商 IP run 显示
  `Out-of-date` 但 DCP 完整时，只 reset `synth_1` 再 launch，它会被直接复用、不重综合。
- **warm path**：全部指纹匹配且输出存在时，Python 直接退出，**不启动 Vivado**（约 0.1–0.2 s）。
- **原位 BD 刷新**：仅 IP 内容变化时，`refresh_bd.tcl` 执行 `update_ip_catalog -rebuild` →
  `upgrade_ip -quiet` → `generate_target all [get_files <bd>]` → `validate_bd_design` → 地址段
  `0x43C00000/4K` / `0x43C10000/4K` 检查 → `save_bd_design`；失败由 Python 删除工程并全量重建
  （`BD_REFRESH_FALLBACK`）。`generate_target` 可能让 `auto_pc` 的 OOC run 被动重综合，属预期成本。
- **模式**：`--force` 全量；`--report-only` 只重出报告/XSA；`--dry-run` 只生成 Tcl 计划；
  `--status` 只读；`--strategy-only` 只写策略（若属性有变化会删除 `runs.json`，强制下次重跑）。
- **增量编译**：`--incremental-synth/impl off|on|auto`，默认 off。参考 DCP 由 Python 复制到
  `.flow/ref/`，worker 设 `AUTO_INCREMENTAL_CHECKPOINT 0` + `INCREMENTAL_CHECKPOINT` +
  `STEPS.SYNTH_DESIGN.ARGS.INCREMENTAL_MODE`，并以 `runme.log` 是否出现
  `read_checkpoint -incremental` 判定是否真的生效。只有墙钟收益 ≥20%、时序不退化、0 告警才
  设为默认；`impl_stability` / `impl_multi` 每轮显式关闭增量。
  **2025.2 实测**：增量实现（`RuntimeOptimized`，同网表）cell/net 100% 复用、WNS/WHS 不变，
  `impl_1 STATS.ELAPSED` 约 31→26 s（约 16%，低于 20% 门槛）；增量综合在 IP 重打包/原位刷新后
  往往没有同版本参考 DCP，只有 `synth_1` 真正走增量、耗时无收益。因此两者默认保持 off。

### 增量流程的实测坑

| 坑 | 现象/处理 |
| --- | --- |
| **`reset_run` 会清步骤属性** | 先 reset 再写策略/hook/增量属性；worker 只对本次实际 reset 的 run 写属性，未 reset 的 run 不碰 |
| **`STATUS=Out-of-date` 不等于 DCP 不能复用** | 复用看「指纹 + DCP 存在」；不要因为 STATUS 把未变化 IP 全 reset |
| **`generate_target` 不会自动把受影响 run 标成过期** | IP 内容变化必须由 Python 的 IP 哈希/run 指纹显式触发 reset，否则会静默复用旧网表 |
| **原位刷新后 `synth_1` 可能需要先 reset** | `launch_runs synth_1 -scripts_only` 报 `[Common 17-69]` 时，worker 先 `reset_run synth_1` 再重试一次 |
| **`upgrade_ip` 在 IP 已是最新时会打印 `[Coretcl 2-1042]`** | worker 用 `upgrade_ip -quiet`，真正问题由 `generate_target`/`validate_bd_design`/地址检查暴露 |
| **刷新后打开旧工程会看到 `[BD 41-1661]`** | IP 重打包后的预期中间态，已在 `suppress_early_messages.tcl` 带理由过滤；`upgrade_ip` 后消失 |
| **参考 DCP 不属于 utils_1 会报 `[Runs 36-537]`** | worker 用 `add_files -fileset utils_1 -norecurse` 登记 `.flow/ref/*.dcp` |
| **地址段 range 在 2025.2 返回 `0x00001000`** | 地址检查要把 `0x1000`/`4096`/`4K` 都规范成 4K；取 PS7 的 `Data` 地址空间（不是从口自己的 `s_axi_lite`） |
| **`reset_run` 不清 run 级增量属性** | `INCREMENTAL_CHECKPOINT`/`AUTO_INCREMENTAL_CHECKPOINT` 不是步骤属性，上次实验的设置会泄漏到普通 build；worker 对本次 reset 且非增量目标的 run 显式清空 |
| **增量实验的参考 DCP 会留在 `.flow/ref/`** | 它是构建产物；普通 build 不读取，但改回默认后建议 `--force` 或让 worker 清属性后再签核 |

## IP 打包（package_ip.tcl / package_src_ip.tcl）

- 用 `read_verilog -sv` 按 `filelist.f` 顺序读入生成的 SV（顺序即拓扑序），
  再 `ipx::package_project -root_dir <ip_root>/<ip_name>`。**不要**用 `-in_memory` 建工程：
  Vivado 会报 `Command ipx::package_project in non project mode is not supported`。
- `ipx::package_project` 的 `-vendor_display_name` / `-company_url` 选项在 2025.2 上不存在，
  公司信息用 `set_property company_url ... [ipx::current_core]`。
- **AXI 接口靠端口名推断**：顶层 RTL 的端口命名成 `s_axi_lite_awaddr` 这种形式，
  `ipx::infer_bus_interfaces "xilinx.com:interface:aximm_rtl:1.0" $core` 就能把 40 根 AXI4 信号
  （含 ID/REGION/QOS）一次性映射好，接口名取端口前缀。Chisel 侧用一个与端口同名的 bundle 即可。
- 时钟/复位/中断接口用 `ipx::infer_bus_interface <端口> "xilinx.com:signal:<kind>_rtl:1.0" $core`
  逐个建立，再补参数：时钟 `FREQ_HZ`（`value_resolve_type user`）、`ASSOCIATED_BUSIF`、
  `ASSOCIATED_RESET`；复位只写 `POLARITY`（`ACTIVE_LOW`）；中断写 `SENSITIVITY`（`LEVEL_HIGH`）。
- **复位接口不要写 `ASSOCIATED_BUSIF`**：否则 BD 报 `[BD 41-1732] Bus interface ... associated with
  multiple clock-pins`。总线与复位的关联只写在时钟接口的参数上（与 Xilinx 自带 IP 一致）。
- **多时钟域 IP 不要给从时钟写 `ASSOCIATED_RESET`**：打包器会自动加；高速时钟域若由内部同步器
  产生复位，BD 报 `[BD 41-1761] Reset pin ... associated with multiple clock pins`。用
  `ipx::remove_bus_parameter "ASSOCIATED_RESET" <时钟接口>` 删掉。
- **删掉打包器默认加的 `reg0` 地址块**（range 4GB），再添加真实的 4KB 地址块。
- 收尾：`ipx::create_xgui_files`、`ipx::update_checksums`、`ipx::check_integrity`、`ipx::save_core`，
  最后看输出里有没有 `ERROR` / `CRITICAL WARNING`。

## Block design（create_bd.tcl）

- Zynq-7000 的 `M_AXI_GP0` 是 **AXI3**（ID 宽 12、最大突发 16），自定义 IP 的从口是 AXI4-Lite，
  中间必须做协议转换：**只有一个从口**时用 `axi_protocol_converter`（`SI_PROTOCOL=AXI3`、
  `MI_PROTOCOL=AXI4LITE`、`DATA_WIDTH=32`、`ADDR_WIDTH=32`），省掉地址译码；多个从口时才用
  `axi_interconnect`（它会自动插入 `auto_pc`）。
- 实测坑：`axi_protocol_converter` 的 `ID_WIDTH` 默认 0，此时从口**没有 ID 端口**（`C_IGNORE_ID=1`），
  PS 的 `AWID/ARID/BID/RID` 会悬空；与 `M_AXI_GP0` 对齐要显式写 `ID_WIDTH=12` 与
  `TRANSLATION_MODE=2`（即 Vivado 自动插入的 `auto_pc` 的配置）。
- 结构：`processing_system7_0`（`PCW_USE_M_AXI_GP0=1`、`PCW_EN_CLK1_PORT=1`、
  `PCW_FPGA0_PERIPHERAL_FREQMHZ=50`、`PCW_FPGA1_PERIPHERAL_FREQMHZ=200`、
  `PCW_USE_FABRIC_INTERRUPT=1`、`PCW_IRQ_F2P_INTR=1`）+ `proc_sys_reset` + 协议转换器 +
  自定义 IP + `xlconcat`（`NUM_PORTS=16`，只接 `In0`）。
- `M_AXI_GP0_ACLK` 必须显式接到 `FCLK_CLK0`，脚本化建 BD 时没有自动化帮忙连。
- 中断是 `IRQ_F2P` 的 bit0：`xlconcat` 的 `NUM_PORTS` 与 16 位对齐，未连接的输入由 IP 当作 0。
- 地址分配用 `assign_bd_address -target_address_space /processing_system7_0/Data -offset ... -range 4K
  [get_bd_addr_segs <ip>/s_axi_lite/Reg]`，段的写法是 `<实例>/<接口>/<地址块名>`。
- 收尾必须 `validate_bd_design`：没有 ERROR 且没有 CRITICAL WARNING 才算干净，
  这两类信息不会让脚本退出码非零。
- `make_wrapper -files [get_files <bd>.bd] -top` 生成 BD 顶层，再加进工程、设成 top。
- PS 串口是参数：`create_bd.tcl` 用 `-uart uart0|uart1|none`（默认 uart1）与 `-baud` 配置并回读校验，
  详见 `docs/bringup.md`。

## xsim 仿真（sim/run_handoff_tb.sh）

xsim 的职责只有**真实 MMCM 联调**：`tb_src_handoff.sv` 把 BD 里真实用的三个单元接起来
（`dpm_mmcm_src` + `MmcmDrpController` + `DigitalPhaseMeterTop`），验证 MMCM 寄存器编码与整条
测量通路。命令行三段式，脚本已封装：

```bash
xvlog --sv --work work <filelist.f 补全成绝对路径> vivado/rtl/dpm_mmcm_src.v <Vivado>/data/verilog/src/glbl.v
xvlog --sv --work work sim/tb_src_handoff.sv
xelab --debug typical --timescale 1ns/1ps tb_src_handoff glbl -s <snapshot> -L unisims_ver
xsim <snapshot> -runall
```

- `xelab` **没有** `--work` 选项；工作库用 `-L work` 或默认 `work`。
- 被测 RTL 取生成产物目录 `build/chisel/DigitalPhaseMeterTop/` 与 `build/chisel/MmcmDrpController/`，
  另加手写的 `vivado/rtl/dpm_mmcm_src.v`。
- **仿真真实 MMCM 必须带厂商库**：编译 `glbl.v`（unisim 的全局复位/时序模型），`xelab` 时
  `-L unisims_ver` 并把 `glbl` 一起作顶层；**不要**加 `-L unisim`（VHDL 版 unisim 与 Verilog 版
  unisims_ver 冲突）。
- 判定成败不要只看 xsim 退出码（`$fatal` 也可能返回 0），要看日志里的
  `HANDOFF_SUMMARY ... fails=0`，且 `HANDOFF_MEAS` 行数 = 2×点数。
- 激励约定：
  - 在**下降沿驱动**激励、在**上升沿之后评估采样**握手，握手后回到下降沿再撤销信号；
  - AXI4-Lite BFM 按 AW → W → B 顺序（与 `test/DigitalPhaseMeterTop.cpp` 同一套）；
  - 每个用例结束前多等几个周期再 `$finish`，看门狗用「仿真时间内没有进展就 `$fatal`」；
  - **仿真真实 MMCM 的联调激励要把时基选成整数比**：`tb_src_handoff.sv` 取
    `T_MAIN=18900ps` / `T_DSP=2100ps`（严格 9:1，18900 能被频点表全部 MULT 整除），
    详见 `sim/README.md`；
  - 按**真实地址**（`0x43C0_0000` / `0x43C1_0000` + 偏移）驱动 AXI，不要给裸偏移；
  - `run_handoff_tb.sh` 默认每次用 sbtn 重新生成 RTL（与其它生成入口一致）；
    确认产物最新时用 `--no-gen`。
- **顶层 AXI 寄存器通路的守门人是 Verilator 顶层用例 `test/DigitalPhaseMeterTop.cpp`**：它用
  AXI4-Lite BFM 按真实基地址 `0x43C0_0000 + 偏移` 驱动，覆盖复位/寄存器默认值、写数据先到与
  字节选通、测量启动与中断/W1C 时序、结果锁存与 `CTRL.clear`、输入源切换等。本仓库实测过一次
  「寄存器堆往测量域的配置输出被 firtool 优化成常量 0」的缺陷——Verilator 单元测试全绿、
  只有经过寄存器堆的顶层用例能抓到。因此**动 AXI 寄存器通路必须跑 `xmake test -v`**（含
  `DigitalPhaseMeterTop`），并把新寄存器纳入 `test/DigitalPhaseMeterTop.cpp`（读回、字节选通、
  复位默认值、以及「配置真的改变了测量结果」这类有区分度的断言）。

## 片内信号源：裸 MMCM + 极简 DRP 控制器（当前实现）

```text
FCLK_CLK0(50MHz) ──► dpm_mmcm_src(clkIn) ──► clkOut1/clkOut2 ──► dpm_top_0/intSig_sig1,intSig_sig2
                                       └──► locked ──► dpm_src_drp/locked、dpm_top_0/srcLocked
dpm_src_drp（AXI4-Lite @0x43C1_0000）──► DRP 口 + mmcm_reset ──► dpm_mmcm_src（drpReset）
```

- `vivado/rtl/dpm_mmcm_src.v`：`MMCME2_ADV` 薄封装（BUFG 反馈 + 每个输出一路 `BUFGCE`/`BUFH`
  的 8 拍 Safe Clock Startup）。
- `src/main/scala/DigitalPhaseMeter/MmcmDrpController.scala` → `vivado/package_src_ip.tcl`
  打包成 `xjkp.org:dpm:dpm_src_drp:1.0`。
- 重配置时序由控制器内部完成（软件只写 8 个 16 位寄存器镜像 + `0x28` LOAD）：
  `RST=1`（≥8 个 DCLK）→ 8 笔 DRP 写（DEN/DWE 各一拍，等 DRDY）→ `RST=0` → MMCM 重新锁定。

### 实测陷阱（按重要性）

| 坑 | 现象与处理 |
| --- | --- |
| **`MMCME2_ADV` 不能直接做 BD cell** | `create_bd_cell -type primitive` 不支持；`-type module -reference MMCME2_ADV` 报 `[filemgmt 56-587] Failed to resolve reference`。必须写薄 HDL 封装、`add_files` 后用 module reference 例化 |
| **MMCM 输出时钟的网络名会变** | 时钟名是网表里的网络名 `clkout0_raw` / `clkout1_raw`。XDC 里的 `set_false_path`/`set_max_delay` 必须同步改，否则两路跨域路径会变成时序违例 |
| **`DPM_DRP_OFF_*` 是字节偏移，且 PL 侧寄存器 = MMCM 原始寄存器** | 寄存器直接镜像 MMCM 的 16 位值（镜像段 0x08/0x0C/0x10/0x14/0x18/0x1C/0x20/0x24/0x28） |
| **`ClkReg1` 的 bit12** | XAPP888 标为 RSVD，但 clk_wiz 生成器在该位恒置 1（div=20/40/80/100 实测全为 1）；`divide == 1` 的计数器字是 `0x0041`（bit12 为 0）。编码/解码按**实测生成值**，不要按手册推断 |
| **DIVCLK 寄存器布局与 ClkReg1 不同** | `DivReg = {2'b00, EDGE[13], NO_COUNT[12], HIGH_TIME[11:6], LOW_TIME[5:0]}`；`divide = 1` 时 HIGH/LOW_TIME 是占位值 1，不能相加，必须先判 NO_COUNT |
| **DRP 写必须让 MMCM 处于复位** | unisim 在 `RST` 为低时对 DRP 写报 `Error: RST need to be high when changing parameters through DRP`，并把 `@(negedge RST)` 当作新配置生效时刻；上板同样如此：不拉 `RST` 时「分频字立即生效、相位字不生效」。控制器时序见上 |
| **`DWE` 不能常置 1** | unisim 对「DWE 连续多个 DCLK 拉高」报 `DRC Error` 并结束仿真；DEN 与 DWE 都只在写那一拍拉高 |
| **AXI 地址译码要用页内偏移** | AXI 互连**不剥离基地址**。只译码 `addr[11:6] == 0 && addr[1:0] == 0`，高 20 位交给 BD 的地址段；按 `addr[31:6] == 0` 判窗口会让上板所有 DRP 访问被丢弃 |
| **unisim 的 DRP 写是支持的，但语义严格** | `sim/tb_src_handoff.sv` 用真实 `MMCME2_ADV` 行为模型跑通了整条链；踩到的是上面的时序约束，不是「不支持」 |
| 取权威寄存器值 | 用 clk_wiz 的 `CONFIG.AXI_DRP=true`（Write DRP 模式）生成配置，`S1_*` 参数就是权威 MMCM 寄存器值；相位拆分可查 `$XILINX_VIVADO/data/ip/xilinx/clk_wiz_v5_4/mmcm_pll_drp_func_7s_mmcm.vh` 的 `mmcm_pll_phase` / `mmcm_pll_count_calc` |

### MMCM 寄存器编码（软件侧，已对权威值验证）

```text
ClkReg1 = 0x1000 | (PHASE_MUX[2:0] << 13) | (HIGH_TIME[5:0] << 6) | LOW_TIME[5:0]
ClkReg2 = (EDGE << 7) | (NO_COUNT << 6) | DELAY_TIME[5:0]
DivReg  = (EDGE << 13) | (NO_COUNT << 12) | (HIGH_TIME[5:0] << 6) | LOW_TIME[5:0]

HIGH_TIME = divide // 2, LOW_TIME = divide - HIGH_TIME（50% 占空比）
EDGE = (divide == 1) ? 0 : (divide & 1), NO_COUNT = (divide == 1)
m          = round(phase_mdeg × divide / 45000)   # 9 位相位码，单位 1/8 VCO 周期
DELAY_TIME = m >> 3        # 相位码的整数部分（VCO 周期）
PHASE_MUX  = m & 0x7       # 相位码的小数部分（1/8 VCO 周期）
```

**相位网格 = 1/8 VCO 周期 = `45/divide` 度**（不是 45°）：22.5° 在 D=40 下 = 20 步
（`DELAY_TIME=2` + `PHASE_MUX=4`）精确可表示。

**可表示的相位上界与 D 有关（9 位相位码）**：`m <= 511` ⟺ `phase_mdeg × D <= 23017499`
（`DPM_MMCM_PHASE_MAX_MDEG_NUM`），即 `phase_max = min(23017499/D, 359999)` 毫度：

| D            | 频点             | 上界                      |
| ------------ | ---------------- | ------------------------- |
| 20 / 25 / 40 / 50 | 50/40/25/18MHz 等 | 359.999°（整个 0~360°） |
| 70 / 100     | 15 / 9 / 10MHz   | 328.821° / 230.174°       |
| 120 / 125    | 5 / 6MHz         | 191.812° / 184.139°       |

越界请求（`m > 511`）会让字段按位截断、相位**回绕**，因此协议层用 `dpm_mmcm_phase_code_fits`
显式回 `ERR_PARAM`。**旧规则「请求必须 < 180°」已作废**：相位计测的是无符号间隔，>180° 同样可测。

**`D ≤ 126`**：分频编码成 `HIGH_TIME + LOW_TIME`（各 6 位），上限 `63 + 63 = 126`；
三处实现（`DPM_SIG_DIV_MAX` / `host/dpm_host/sig.py::DIV_MAX` / `spec/gen_golden.py::DIV_HARD_MAX`）
都取 126，`spec/selftest_spec.py` 有自动校验。

参考实现：厂商 `.vh` 的 `mmcm_pll_phase` / `mmcm_pll_count_calc`（相位拆分）、
`host/dpm_host/sig.py::mmcm_encode_clkout` 与 `ps/include/dpm_regs.h::DPM_MMCM_ENCODE_CLKOUT`
（两者逐位一致，由 `ps/tests/test_sig.c` 与 `host/tests/test_sig.py` 对拍）。

## DSP 域高速时钟（450MHz）

目标频率由 `create_bd.tcl` 的第一个参数给出，**默认 450MHz**（450 = 50 × 9，clk_wiz 选小数倍频：
`MMCM_CLKFBOUT_MULT_F=20.250`、`MMCM_DIVCLK_DIVIDE=1`、`MMCM_CLKOUT0_DIVIDE_F=2.250` →
VCO 1012.5MHz、输出 450MHz）。当前签核结果（xc7z010clg400-1、Vivado 2025.2）：
**全设计 WNS +0.271ns / WHS +0.026ns / WPWS +0.067ns、0 失败端点**；同一份网表用
`impl_stability.tcl` 做 6 次独立实现，结果完全一致（6/6 PASS）。整体 WNS 仍由 **clk_wiz 的
Safe Clock Startup 内部路径**（`seq_reg1[7] → BUFGCE CE0`）决定；测量域自身最差 setup 路径
余量 +0.541ns，DSP 域 hold 最差 +0.121ns。
频率参数同时是 IP 侧 `dspClock` 的 `FREQ_HZ` 声明值来源（`package_ip.tcl` 里写死为 450MHz，改频率时同步改）。

**`CONFIG.PRIM_SOURCE` 必须是 `No_buffer`**（输入时钟来自 fabric 上的 FCLK_CLK0，不是器件引脚）。
用默认的 `Single_ended_clock_capable_pin` 会让 Wizard 生成的 XDC 在内部 pin
`dpm_system_i/clk_wiz_0/inst/clk_in1` 上再 `create_clock` 一次，而该网络已由 PS7 的 XDC 定义主时钟
`clk_fpga_0` → 实现方法学检查报 **`TIMING-4`/`TIMING-27` 两条 Critical Warning**（主时钟在层次 pin
上重复定义、覆盖 insertion delay），且 DSP 时钟在报告里会挂在 `clk_in1` 而不是 `clk_fpga_0` 下。
`No_buffer` 下 Wizard 的 xdc 模板会注释掉那条 create_clock。该属性写错是静默失效的，必须回读校验。

要真正收敛，需要「时钟路径 + 约束 + 运行策略 + RTL」四件事配套：

1. **PS 的 FCLK 不够用**：Zynq-7000 的 `FCLK_CLK1` 上限 250MHz；在 BD 里请求更高会被**静默降频**，
   不报错。因此 450MHz 必须在 PL 内用 MMCM/`clk_wiz` 从 `FCLK_CLK0` 倍频。
2. **「MMCM 锁定前不放时钟」用 `clk_wiz` 自带的 Safe Clock Startup**：加
   `CONFIG.USE_SAFE_CLOCK_STARTUP {true}` 即可，不要用「与门 + `util_vector_logic`」——后者让时钟
   走 fabric 布线（BUFG → LUT → 时钟脚），是 UG949 点名的反模式。开启后 Wizard 生成
   「MMCM 输出 → BUFGCE（`CE = seq_reg1[7]`）→ clk_out1」，`seq_reg1` 是同一 MMCM 输出经 BUFH
   打拍的 8 bit 移位寄存器，连续 8 拍锁定后才拉高 CE；IP 自带 XDC 已给 seq_reg 加 `set_false_path`。
   如果确实要自己例化原语：`create_bd_cell` 不接受 `-type primitive`，`BUFGCE` 也无法用
   `-type module -reference` 引用（实测 `[filemgmt 56-587]`）；那时才写薄封装 HDL 模块。
3. **跨时钟域握手通路要用有界的 max_delay**：clk_wiz 的相位补偿会让「慢域 → DSP 域」的逐沿
   跨时钟检查只剩约 2.1ns，450MHz 下必然违例。真实需求是「一个慢时钟周期内到达」（翻转电平 +
   3 级 ASYNC_REG 同步链），用 `set_max_delay -datapath_only` 表达，上限取一个慢时钟周期（20ns）。
   **两个方向都要写**：只写一个方向会让另一个方向变成新的最差路径。`PRIM_SOURCE=No_buffer` 之后
   DSP 时钟与 `clk_fpga_0` 在工具眼里是同步时钟对，这些 max_delay 会命中 `TIMING-47` 告警
   （设计意图，用 methodology waiver 声明，见「告警清理」）。
4. **运行策略（`dpm-vivado-flow` 的 build 阶段）必须同时写到 BD 里自定义 IP 的 OOC 综合 run 上**：
   - 综合（`synth_1` 与 `dpm_system_dpm_top_0_0_synth_1` 两处都写）：
     `DIRECTIVE=PerformanceOptimized`、`GLOBAL_RETIMING=off`、`MAX_DSP=2`。
     `GLOBAL_RETIMING` 保持 off：450MHz 计数器的正确写法是让综合器把
     `when(clear)/.elsewhen(enable)` 映射成 DSP48E1 的 RSTP + CEP（见 `dpm-chisel-rtl`）。
   - 实现：内置 **`Performance_Retiming`**（phys_opt = `AlternateFlowWithRetiming`），
     **不要覆盖它自带的步骤 directive**。
   - run 的 `STEPS.*.ARGS.*` 可以直接 `set_property` 改（内置 strategy 也允许），不需要
     `create_strategy`（2025.2 实测 `get_strategies` / `create_strategy` 不存在）；
     但**给 run 换 strategy 会把步骤参数重置回该 strategy 的预设**。
   - Python 的逐 run 指纹已覆盖策略/hook 内容；`run_build_stage.tcl` 在 reset 前再读当前值，
     发现漂移就把该 run 加入**实际 reset 列表**（结果里的 `STRATEGY_CHANGED` / `RESET_RUNS`）。
     未 reset 的 run 不写任何属性，避免把已完成 run 标成过期。
   - 顺序：materialize → 差异判定 → reset（`synth_1` → 其他 `*_synth_1` → `impl_1`）→ 写策略/
     增量属性 → 最后挂 `TCL.PRE` → launch/wait。`reset_run` 会清步骤属性，所以属性必须在 reset 后写。
   - **materialize**：新建 BD 工程刚 open 时 IP 的 OOC 综合 run 还不存在，先
     `launch_runs synth_1 -scripts_only`；BD 管理的 IP 不能用 `create_ip_run`（报 `[Vivado 12-3424]`）。
     原位刷新后 `synth_1` 可能需要先 reset，worker 会 `reset_run synth_1` 后重试一次。
     不要在挂完 hook 后再调 `scripts_only`（正式 launch 会报 `[Common 17-69]`）。

### 三个必须记住的实测坑

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| **place/route 有 run-to-run 差异** | 同一网表、同一策略，WNS 会明显摆动 | 「跑一次通过」不能算签核；用 `vivado/impl_stability.tcl N` 做同策略 N 次独立实现；报告里写清 N 与最小/最大余量 |
| **余量对 place directive 敏感** | 不同 directive 的余量可差 0.1~0.2ns | 余量薄时用 `impl_multi.tcl` 扫一遍 place directive，不要默认是随机波动；报告里写清用的哪条 |
| **过约束只能用来逼工具，不能用来签核** | `set_clock_uncertainty` 过约束下报出的余量是虚的 | 过约束跑完 place/route 后 `reset_clock_uncertainty -setup` 清掉、重新出报告，用**清掉后**的 WNS 作结论 |
| **同一工程不能并发跑两个 Vivado** | 后启动的进程会覆盖先生效的步骤参数 | 改策略/跑实现前确认没有其它 Vivado 在跑；跑完用 `runme.log` 的 `Command:` 行核对实际 directive |
| **BD 里自定义 IP 的 OOC 综合不继承 `synth_1` 的策略** | `synth_1` 只综合 BD 顶层 wrapper，几乎没有逻辑 | 把同一套策略写到自定义 IP 的 OOC run；用 `runme.log` 的 `Command:` 行核对每个 IP run 的参数 |
| **IP OOC run 要等 `launch_runs` 才会被建出来** | 新建工程 open 时 `get_runs` 只有 `synth_1`/`impl_1` | 先 `launch_runs synth_1 -scripts_only`，再写 `STEPS.SYNTH_DESIGN.ARGS.*`，然后 reset 并正式 launch |
| **Safe Clock Startup 的放行延迟是 8 拍** | `locked` 拉高后还要约 8 个时钟周期才出现第一个门控后时钟沿 | 这是该功能应有的行为，不是缺陷；DSP 域复位本来就要等时钟，不需要再叠一层外部 `locked` 门控 |
| **`-tclargs` 必须写在 Vivado 命令行最后** | 写成 `... -source x.tcl -tclargs 6 -log foo.log` 时，`-log`/`-journal` 会被当成 Tcl 参数吞掉，Vivado 改在**当前目录**写 `vivado.log` | 把 `-log`/`-journal` 放在 `-tclargs` 之前，或直接用默认日志文件名 |

### 签核方式

`uv run dpm-vivado-flow` 一次（增量或 `--force` 全量：策略 → 综合/实现/bitstream/XSA）+
`impl_stability.tcl 6` 同策略 6 次独立实现；`impl_multi.tcl` 只用于换 directive 调优，不用于签核
（它会覆盖官方策略）。报告里写清 N、最小/最大余量与失败端点数。build worker 在实现完成后会解析
run 的 methodology 报告：有未 waive 的 Critical Warning 直接 `error`；同时打印
`METHODOLOGY_CHECKS_FOUND / CHECKS_WAIVED` 与 `TIMING_BEGIN/END` 的 WNS/WHS。

## XDC 约束文件的写法（xdc 不是完整的 tcl）

**xdc 里不允许任何流程控制命令**。写 `foreach` / `if` / `puts` 会报
`[Designutils 20-1307] Command '...' is not supported in the xdc constraint file`（CRITICAL WARNING），
实测在综合阶段就会命中，且会产生 `[runtcl-6] Synthesis results are not added to the cache`。
因此「先判断时钟存不存在、再施加约束」这种写法在 xdc 里做不到。

本仓库顶层约束（`vivado/constraints/dpm_top.xdc`）用两个等价手段：

| 手法 | 作用 |
| --- | --- |
| `get_clocks -quiet <通配>` | 无匹配时安静返回空串，不报 `[Vivado 12-627] No clocks matched` |
| `set_false_path -quiet` / `set_max_delay -quiet` | 对象为空时安静地不生效，不报 `[Vivado 12-4739] No valid object(s) found` |

原因：**同一个 xdc 会被评估两次**——综合器启动时一次（此时 BD 还是黑盒，IP 生成的时钟一个都
不存在），实现读网表时一次。用具体时钟名硬取对象，综合阶段必然失败，还会带出整文件级的
`[Project 1-498] One or more constraints failed evaluation`。带 `-quiet` 后综合阶段「安静地不适用」、
实现阶段正常生效；把名字写成通配（如 `clkout*_raw`）后，IP 实例改名时约束也能自动跟上。

## 综合 / 实现日志的告警清理

目标：整条脚本流程（打包 → BD → 综合 → 实现）日志 **0 Warnings / 0 Critical Warnings / 0 Errors**。
分三类：

1. **根因在本仓库的，改设计**：
   - `clk_wiz` 的 `PRIM_SOURCE` 改 `No_buffer`（消 `TIMING-4`/`TIMING-27` 两条 Critical）；
   - `vivado/rtl/dpm_mmcm_src.v`：`BUFGCE` 显式 `SIM_DEVICE("7SERIES")`（消 `Netlist 29-345`）；
     `reset | drpReset` 在 DCLK 域同步两拍后再驱动 MMCM.RST 与启动移位寄存器 CLR（消 `LUTAR-1`）；
   - `Axi4LiteSlave` 只锁存实际使用的地址/数据位（`addrUsedWidth`/`dataUsedWidth`，高位在 payload
     零扩展），消**自研 IP** OOC 综合的 `Synth 8-3936`（awAddr 32→6 / 32→12、wData 32→16、
     wStrb 4→2；厂商 IP run 内部另有 INFO 级同名提示，不是告警）；
   - `clk_wiz` 的 `USE_LOCKED=false`（端口无消费者）。
   - IP/run 属性写错静默失效 → `create_bd.tcl` 回读校验 `part` / `PRIM_SOURCE` / `USE_LOCKED`，
     不匹配直接 `error`；`dpm-vivado-flow` 的 build 阶段再回读校验 `part`。
2. **厂商 IP 固有、改不动的，显式屏蔽**（每条在文件内写明理由，**只屏蔽厂商的、不屏蔽自研的**）：
   - `hook_filter_messages.tcl` → `synth_1/SYNTH_DESIGN`、`impl_1/INIT_DESIGN`：`Synth 8-7129`
     （xlconcat 的 In* 写死在 IP 里）、`8-7071`/`8-7023`、`8-4446`、`Opt 31-32`、
     `Netlist 29-160`(CRITICAL)（PS7 自带 xdc 给未使能 MIO/DDR 引脚设属性）；
   - `hook_filter_vendor_ip.tcl` → **厂商 IP 的 OOC 综合 run**：`Netlist 29-345`（厂商生成的 BUFGCE
     未覆盖 2025.2 unisim 默认 `SIM_DEVICE="ULTRASCALE"`）。IP run 是独立进程、不继承 `synth_1`
     的 TCL.PRE，必须单独挂；
   - `suppress_early_messages.tcl` → 各脚本 `open_project` **之前** source：`Board 49-26`（×96，
     未安装 board/不绑 board part）、`Project 1-5713`、`Vivado 12-7122`、`Constraints 18-550`、
     `IP_Flow 19-3664`（reset OOC run 后 .gen 产物暂时不存在的中间态）。
     **早期过滤必须先于 `open_project` 加载**（放到 `open_project` 之后，告警已经打印，过滤无效）。
3. **报告层的刻意例外 → methodology waiver**：`hook_methodology_waivers.tcl` 挂
   `impl_1/ROUTE_DESIGN.TCL.PRE`，用 `create_waiver -type METHODOLOGY -id {...}` 声明
   `TIMING-28`（按名字引用 auto-derived 时钟；为了让一份 xdc 兼容综合黑盒/实现两阶段并抗改名）与
   `TIMING-47`（DRP 动态相位下用有界 `max_delay -datapath_only` 表达跨域，属有意）。waiver 是
   **报告条目、不是 message**（`set_msg_config` 无效），且 `reset_run` 会清掉，稳定性/多策略脚本
   每轮 reset 后要重挂。报告应显示 `Checks found: 0` + `Checks waived: N`；`dpm-vivado-flow` 的
   build worker 以「存在任何未 waive 的 Critical Warning」为失败门禁。

### 三个必须记住的实测坑

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| **hook 必须挂在对的步骤上** | 综合告警在 `SYNTH_DESIGN`、实现读网表在 `INIT_DESIGN`、methodology waiver 在 `ROUTE_DESIGN` | 见 `vivado/flow/tcl/run_build_stage.tcl` 的 hook 计划 |
| **`reset_run` 会清掉步骤属性，不 reset 的 run 也不会重新生成脚本** | 先 `set_property STEPS.*.TCL.PRE` 再 `reset_run` 等于没设；**厂商 IP 的 OOC run 不 reset 就沿用物化时生成的旧脚本，后挂 hook 不生效**（实测 clk_wiz 过滤漏过） | 顺序「先比较/决策 → 全部 reset（含厂商 OOC run）→ 统一挂 hook → 正式 launch」；不要在挂完后调 `launch_runs -scripts_only`（正式 launch 会报 `[Common 17-69]`） |
| **hook 脚本要登记进工程** | 只写 `TCL.PRE` 不 `add_files` 报 `[Runs 36-537] File ... is not part of fileset utils_1` | `add_files -fileset utils_1 -norecurse <hook>` |
| **`open_project` 的早期告警要提前过滤** | `[Board 49-26]`×96 在 `open_project` 内部打印 | 在 `open_project` **之前** source `suppress_early_messages.tcl`；放晚了才「拦不到」 |

另有一条容易漏的：`package_ip.tcl` 的 `[IP_Flow 19-5661]`（`dspClock` 没有关联 bus interface）由
`ipx::check_integrity` 打印，其 `set_msg_config` **必须挂在 `check_integrity` 调用之前**——
`ipx::package_project` 之后 ipx 会重置消息规则（实测），挂在文件前部不生效。

## 交付前

- 报告里写清实际跑到哪一步：IP 打包通过、`validate_bd_design` 通过、xsim 联调点数与结果、
  综合实现是否 100%、时序 WNS/WHS（高速域单独给出域内 WNS）、bitstream/XSA 是否生成。
- 告警类改动要把**清理前后的条数**都给出，并说明哪些是改设计消掉的、哪些是屏蔽/waiver 的（附理由
  文件）；给出所有 run 的 `runme.log` 计数与 methodology 报告的 `Checks found / Checks waived`。
- 高速时钟类改动要给出**多次实现**的结果（`impl_stability.tcl`），单次通过不作为签核依据。
- **改了 RTL 之后**：先跑两个 `sbtn` 生成入口，再在仓库根跑 `uv run dpm-vivado-flow`；它会按内容
  哈希自动只重打包变化的 IP、刷新 BD 并重跑受影响的 run。需要从零重建时用 `--force`。
- **改了 BD 源文件（`vivado/rtl/dpm_mmcm_src.v` / `create_bd.tcl`）也必须重建工程**：BD 结构哈希
  变化会触发全量重建；如果只手工跑 build 而跳过 BD 阶段，会复用旧网表。最稳妥是
  `uv run dpm-vivado-flow --force`。
- 没有跑到的步骤（例如没上板）要明确写成「未验证」，不要用「已验证」概括。
