---
name: dpm-vivado-ip
description: 'Use when: 在 DigitalPhaseMeter 中打包 Vivado IP（vivado/package_ip.tcl）、创建或修改 PS↔PL 互联的 block design（vivado/create_bd.tcl）、跑 xsim 的 SystemVerilog 测试激励（sim/）、或做综合实现与导出 XSA（vivado/build_bitstream.tcl）。覆盖 IP-XACT 接口推断与补全、地址块、BD 连接与地址分配、xsim 命令行流程与其实测陷阱。'
argument-hint: '要做的 Vivado/xsim 工作（打包 IP、改 BD、写激励、跑实现）'
---

# DigitalPhaseMeter 的 Vivado IP、BD 与 xsim

## 适用场景

- 打包/更新 `DigitalPhaseMeterTop` 的 Vivado IP。
- 修改 PS↔PL 互联（PS7、AXI Interconnect、地址映射、中断）。
- 用 xsim 跑 `sim/tb_*.sv` 测试激励，或新增激励。
- 综合、实现、导出 XSA。

RTL 本身的写法见 `dpm-chisel-rtl`；测量结果的判据见 `dpm-measurement-model`；
交付前的格式化与回归见 `dpm-pre-delivery-check`。

## 交付物与执行顺序

```text
sbt -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop --type=syn"   # 生成综合产物 RTL
vivado -mode batch -source vivado/package_ip.tcl        # 打包 IP → build/vivado/ip/dpm_top
vivado -mode batch -source vivado/create_bd.tcl         # BD → build/vivado/bd_proj（DSP 域默认 400MHz）
vivado -mode batch -source vivado/apply_timing_strategy.tcl  # 写入高速时钟需要的综合/实现策略
sim/run_xsim.sh                                         # xsim 仿真（综合产物 + XPM）
vivado -mode batch -source vivado/build_bitstream.tcl   # 综合/实现/bitstream/XSA
vivado -mode batch -source vivado/impl_multi.tcl 6      # 高速时钟：多次实现取余量最好的一次
```

**改了 RTL 必须从 `package_ip.tcl` 重新开始**：IP 内容变了，`create_bd.tcl` 里的 BD 也要重新
生成；只重跑 `build_bitstream.tcl` 会用旧的 IP。`apply_timing_strategy.tcl` 改的是 run 参数，
`reset_run` 之后依然有效，但换新工程时要重跑。

`build/` 下全部是产物，不入库；入库的是 `.scala`、`test/`、`sim/`、`vivado/` 里的脚本与文档。

**必须用 `--type=syn` 的产物**：BD、综合与 xsim 都消费 `build/chisel/DigitalPhaseMeterTop/`
（其中复位同步链实例化 `xpm_cdc_sync_rst`）；`--type=sim` 的行为模型只用于 Verilator，
Verilator 侧没有 XPM 模型。

## IP 打包（package_ip.tcl）

- 用 `read_verilog -sv` 按 `filelist.f` 顺序读入生成的 SV（顺序即拓扑序），
  再 `ipx::package_project -root_dir <ip_root>/<ip_name>`。**不要**用 `-in_memory` 建工程：
  Vivado 会报 `Command ipx::package_project in non project mode is not supported`。
- `ipx::package_project` 的 `-vendor_display_name` / `-company_url` 选项在 2025.2 上不存在，
  公司信息用 `set_property company_url ... [ipx::current_core]`。
- **AXI 接口靠端口名推断**：顶层 RTL 的端口命名成 `s_axi_lite_awaddr` 这种形式，
  `ipx::infer_bus_interfaces "xilinx.com:interface:aximm_rtl:1.0" $core` 就能把
  40 根 AXI4 信号（含 ID/REGION/QOS）一次性映射好，接口名取端口前缀（`s_axi_lite`）。
  在 Chisel 侧用一个与端口同名的 bundle（`RawModule` 里 `val s_axi_lite = IO(new Axi4LiteSlavePorts)`）
  即可得到这套名字。
- 时钟/复位/中断接口用 `ipx::infer_bus_interface <端口> "xilinx.com:signal:clock_rtl:1.0" $core`
  逐个建立，再补参数：
  - 时钟：`FREQ_HZ`（配 `value_resolve_type user`）、`ASSOCIATED_BUSIF`、`ASSOCIATED_RESET`；
  - 复位：只写 `POLARITY`（`ACTIVE_LOW`）；
  - 中断：`SENSITIVITY`（`LEVEL_HIGH`）。
- **复位接口不要写 `ASSOCIATED_BUSIF`**：写了以后 BD 会把 `resetn` 当成第二个时钟关联，
  报 `[BD 41-1732] Bus interface ... associated with multiple clock-pins`（CRITICAL WARNING）。
  总线与复位的关联只写在时钟接口的 `ASSOCIATED_BUSIF` / `ASSOCIATED_RESET` 上（与 Xilinx 自带 IP 一致）。
- **多时钟域 IP 不要给从时钟写 `ASSOCIATED_RESET`**：打包器会给每个时钟接口自动加
  `ASSOCIATED_RESET`，若高速时钟域实际上由内部同步器产生复位，BD 会报
  `[BD 41-1761] Reset pin ... associated with multiple clock pins`。用
  `ipx::remove_bus_parameter "ASSOCIATED_RESET" <时钟接口>` 删掉。
- **删掉打包器默认加的 `reg0` 地址块**（range 4GB），再添加真实的地址块，
  否则地址编辑器里会出现一个 4GB 的空洞。
- 收尾：`ipx::create_xgui_files`、`ipx::update_checksums`、`ipx::check_integrity`、`ipx::save_core`，
  最后看输出里有没有 `ERROR` / `CRITICAL WARNING`。

## Block design（create_bd.tcl）

- Zynq-7000 的 `M_AXI_GP0` 是 **AXI3**（ID 宽 12、最大突发 16），自定义 IP 的从口通常是 AXI4/AXI4-Lite，
  中间必须做协议转换：**只有一个从口**时用 `axi_protocol_converter`（`SI_PROTOCOL=AXI3`、
  `MI_PROTOCOL=AXI4LITE`、`DATA_WIDTH=32`、`ADDR_WIDTH=32`），省掉 `axi_interconnect` 的地址译码；
  **多个从口**时才用 `axi_interconnect`（它会自动插入 `auto_pc`）。
  地址分配会穿过协议转换器落在从口的地址块上（`<IP>/s_axi_lite/Reg`）。
  实测坑：`axi_protocol_converter` 的 `ID_WIDTH` 默认 0，此时从口**没有 ID 端口**（`C_IGNORE_ID=1`），
  PS 的 `AWID/ARID/BID/RID` 会悬空；与 `M_AXI_GP0` 对齐要显式写 `ID_WIDTH=12` 与 `TRANSLATION_MODE=2`
  （即 Vivado 自动插入的 `auto_pc` 的配置）。
- 结构：`processing_system7_0`（`PCW_USE_M_AXI_GP0=1`、`PCW_EN_CLK1_PORT=1`、
  `PCW_FPGA0_PERIPHERAL_FREQMHZ=50`、`PCW_FPGA1_PERIPHERAL_FREQMHZ=200`、
  `PCW_USE_FABRIC_INTERRUPT=1`、`PCW_IRQ_F2P_INTR=1`）+ `proc_sys_reset` + 协议转换器
  （或从口多于一个时的 `axi_interconnect`）+ 自定义 IP + `xlconcat`（`NUM_PORTS=16`，只接 `In0`）。
- `M_AXI_GP0_ACLK` 必须显式接到 `FCLK_CLK0`，脚本化建 BD 时不会有自动化帮你连。
- 中断是 `IRQ_F2P` 的 bit0：`xlconcat` 的 `NUM_PORTS` 要与 `IRQ_F2P` 的 16 位对齐，
  未连接的输入由 IP 当作 0。
- 地址分配用 `assign_bd_address -target_address_space /processing_system7_0/Data -offset ... -range 4K
  [get_bd_addr_segs <ip>/s_axi_lite/Reg]`，段的写法是 `<实例>/<接口>/<地址块名>`。
- 收尾必须 `validate_bd_design`：没有 ERROR 且没有 CRITICAL WARNING 才算干净，
  这两类信息不会让脚本退出码非零。
- `make_wrapper -files [get_files <bd>.bd] -top` 生成 BD 顶层，再加进工程、设成 top。

## xsim 仿真（sim/run_xsim.sh）

命令行三段式，`run_xsim.sh` 已经封装：

```bash
xvlog --sv --work work "$XILINX_VIVADO/data/ip/xpm/xpm_cdc/hdl/xpm_cdc.sv" \
                       "$XILINX_VIVADO/data/verilog/src/glbl.v"
xvlog --sv --work work -f <补全成绝对路径的 filelist.f>
xvlog --sv --work work sim/tb_*.sv
xelab --debug typical --timescale 1ns/1ps <tb_top> glbl -s <snapshot>
xsim <snapshot> -runall
```

- `xelab` **没有** `--work` 选项；工作库用 `-L work` 或默认 `work`。
- 判定成败不要只看 xsim 退出码（`$fatal` 也可能返回 0），要在日志里找激励打印的通过标记，
  失败时再 grep 错误行。
- 测试激励的写法约定：
  - 在**下降沿驱动**激励、在**上升沿采样**握手（`do @(posedge clk); while (!(valid && ready));`），
    握手后回到下降沿再撤销信号，避免与 DUT 的时序寄存器竞争；
  - BFM 任务按通道顺序实现即可（AW 与 W 互相独立，分开握手是合法的），
    但要专门用「写数据先到」的顺序写一笔，覆盖 AW/W 偏斜；
  - 每个用例结束前多等几个周期再 `$finish`，看门狗用 `initial #<超时>; $fatal`；
  - **「闸门计数」类的容限要按被测信号周期给**：XDC/激励里的引脚与时钟固定下来以后，
    `gateCnt` 的余量是「等到被测信号的下一个上升沿」，最多一个信号周期。激励里写死
    `gate_target + 8` 这类常数，在外部 400ns 信号（= 20 个低速周期）上够用，换到片内信号源
    640ns（= 32 个低速周期）就会误判。`check_record` 现在按
    `$ceil(signal_period / CLOCK_PERIOD) + 8` 计算，与 `dpm-measurement-model` 的公式一致。
- **顶层 xsim 用例是寄存器堆通路的唯一守门人**：`test/` 下的 Verilator 单测直接驱动
  `Controller` / `SignalSource` 的端口，**不经过 AXI 从口与 `ControlRegs`**。本仓库实测过一次
  「寄存器堆往测量域的配置输出被 firtool 优化成常量 0」的缺陷（见 `dpm-chisel-rtl`），
  Verilator 全绿、只有顶层 xsim 用例（写寄存器 → 启动测量 → 校验结果跟随片内源）能抓到。
  因此**动 AXI 寄存器通路必须跑 `sim/run_xsim.sh`**，并把新寄存器纳入激励：读回、字节选通、
  复位默认值、以及「配置真的改变了测量结果」这类有区分度的断言。

## 片内信号源：MMCM 的动态重配置（DRP）

`create_bd.tcl` 里的 `clk_wiz_src` 打开 `CONFIG.USE_DYN_RECONFIG {true}`，把 MMCM 的**频率与相位差**
交给 PS 通过 AXI4-Lite 在线设定（PG065 的 Dynamic Reconfiguration 寄存器映射，寄存器文件从偏移
0x200 起）。要点：

- **从口多于一个时改用 `axi_interconnect`**（`NUM_SI=1`、`NUM_MI=2`），它会自动插入 `auto_pc`
  完成 AXI3 → AXI4-Lite 的转换；只有一个从口时用 `axi_protocol_converter` 更省。地址段分别落在
  `dpm_top_0/s_axi_lite/Reg`（0x43C0_0000）与 `clk_wiz_src/s_axi_lite/Reg`（0x43C1_0000）。
- **开了 DRP 的 clk_wiz 没有 `reset` 端口**（复位由 DRP 状态机内部驱动），但仍有 `s_axi_aclk` /
  `s_axi_aresetn`；它的 AXI 接口名是 **`s_axi_lite`**（不是 `s_axi`）。`connect_bd_net` 之前先用
  `get_bd_pins <cell>/*` 确认端口名，别照抄其它 AXI IP 的命名。
- **输出选点要考虑相位范围**：MMCM 每个输出的相位寄存器覆盖 64 个 VCO 周期，换成输出相位是
  `64 / D` 个输出周期（D = CLKOUTx 分频）。本仓库初始工作点 25MHz（VCO 800MHz、D = 32）→ 2 个
  输出周期，0～360° 全覆盖；D = 64（12.5MHz）刚好 1 个周期；D = 128（6.25MHz）只剩 0～180°。
  要更低频率时优先把 VCO 调到下限（600MHz），不要在 MMCM 上把 D 拉到 128 再想要大相位。
- **不要在 IP 里对 MMCM 输出再做数字分频**：本仓库试过「测量时钟域里数边沿后分频（÷N）把频率
  做到 1kHz」，除了低频端相位差会带上 0～1 个源周期的固定偏移，分频器本身在 400MHz 域里的
  「取配置字 → 计数终值减法 → 比较器 → 计数器」路径也一直违约（实测 −1.7ns，即使在模块内先打
  一拍、并把减法挪到 50MHz 域）。最终方案是**两路 MMCM 输出直接作为被测信号**（与外部引脚共用
  同一条异步采样链），频率下限就取 MMCM 的输出下限（约 4.7MHz），400MHz 域里不再新增任何逻辑，
  时序一次通过。要更低频率就应该换用别的手段（例如换更低的参考时钟或外部分频），不要在测量域里做。
- **重配置期间输出时钟会停止**（MMCM 被复位、重新锁定），因此软件要在测量空闲时改。
- **MMCM 输出的两路时钟在 IP 里当数据用**（被测量时钟域的边沿检测链采样），因此 XDC 上按异步输入
  处理：`set_false_path -from [get_clocks *clk_wiz_src*] -to [get_clocks *clk_wiz_0*]`；
  `locked` 是准静态电平，进慢域前经 3 级同步链，用 `set_max_delay -datapath_only 20` 约束。

## 实测陷阱

| 现象 | 原因与处理 |
| --- | --- |
| `Module ... doesn't have a timescale but at least one module in design has a timescale` | 生成的 RTL 无 `` `timescale``，激励有；`xelab` 加 `--timescale 1ns/1ps` |
| `'glbl' is not declared` | 用到 XPM 原语时必须编译并细化 `glbl`（见上） |
| `xvlog -f filelist.f` 报找不到文件 | filelist 里的路径相对于生成目录，先补成绝对路径 |
| 日志里中文变成 `0xff` 字节 | xsim 会破坏经 `string` 参数/拼接传递的非 ASCII；静态提示用中文、任务参数用 ASCII |
| `[BD 41-1732]` / `[BD 41-1761]` | IP-XACT 里复位与时钟的关联写多了，见上节 |
| `write_bitstream` 报 `NSTD-1` / `UCIO-1` | 没有板级约束时 PS 的 MIO/DDR 引脚未指定；`build_bitstream.tcl` 把这两条 DRC 降级为警告，用具体开发板时应补板级约束并去掉降级 |
| `xvlog` 编译激励时报「中文变成 `0xff`」 | 见上表；作为任务参数传递的字符串用 ASCII |
| 引脚约束写法 | 外部被测信号 `sig1 → W18`、`sig2 → W19`（`xc7z010clg400-1` 的 bank 34，差分对 `L22` 的 P/N 两半，当两路独立单端输入用，LVCMOS33 需要该 bank VCCO = 3.3V）。只给 PL 引脚约束并不能消掉 `NSTD-1`/`UCIO-1`——没有 board part 时 PS 的 MIO/DDR 仍无法解析，所以 `hook_write_bitstream.tcl` 的降级要保留 |

## DSP 域高速时钟（默认 400MHz）

DSP 域（测量核心）的目标时钟频率由 `create_bd.tcl` 的第一个参数给出，**默认 400MHz**
（实测在 xc7z010clg400-1 上收敛：6 次独立实现全部为正、最差 +0.191ns；place/route 有
run-to-run 波动，单次通过不算签核，见下文）；频率参数同时也是 IP 侧
`dspClock` 的 `FREQ_HZ` 声明值来源（`package_ip.tcl` 里写死为 400MHz，改频率时同步改）。
要真正收敛，需要「时钟路径 + 约束 + 运行策略 + RTL」四件事配套：

1. **PS 的 FCLK 不够用**：Zynq-7000 的 `FCLK_CLK1` 由 PS 的 IO PLL 分频，**上限 250MHz**；
   在 BD 里写 `PCW_FPGA1_PERIPHERAL_FREQMHZ=300` 会被**静默降到 250MHz**（不报错，只在报告里
   看到实际频率）。因此 300MHz 以上必须在 PL 内用 MMCM/`clk_wiz` 倍频。参考时钟取 `FCLK_CLK0`。
2. **「MMCM 锁定前不放时钟」用 `clk_wiz` 自带的 Safe Clock Startup**：给 clk_wiz 加
   `CONFIG.USE_SAFE_CLOCK_STARTUP {true}` 即可，不要用「与门 + `util_vector_logic`」——后者让时钟
   走 fabric 布线（BUFG → LUT → 时钟脚），是 UG949 点名的反模式。开启后 Wizard 会把
   `CLKOUTx_DRIVES` 自动限定成 `BUFGCE`，生成「MMCM 输出 → BUFGCE（`CE = seq_reg1[7]`）→ clk_out1」，
   其中 `seq_reg1` 是同一 MMCM 输出经 `BUFH` 打拍的 8 bit 移位寄存器，逐拍移入 MMCM 的 `locked`，
   **连续 8 拍锁定**后才拉高 CE；IP 自带 XDC 已给 seq_reg 加 `set_false_path`，不需要外部约束。
   代价是多 1 个 BUFH + 8 个 FF，但**省掉外部那一级 BUFG**：时钟链路由「MMCM → BUFG → BUFGCE」
   变成「MMCM → BUFGCE」（实测 BUFGCTRL 由 4 个降到 3 个）。`locked` 端口可以留着不接，
   门控逻辑内部用的是 MMCM 原语的 `locked`（与 `USE_LOCKED` 无关）。
   若确实要自己例化原语：`create_bd_cell` 只接受 `IP/Hier/BD/MODULE`，**不接受 `-type primitive`**；
   `BUFGCE` 也不在工程里、无法用 `-type module -reference BUFGCE` 引用（实测
   `[filemgmt 56-587] Failed to resolve reference`）。那时才写一个薄封装 HDL 模块，`add_files` 后
   用 module reference 例化。
3. **跨时钟域握手通路要用有界的 max_delay**：`clk_wiz` 为了让 MMCM 输出对齐到输入时钟，会把
   输出相位设成 `-T_out`（相位补偿）。于是工具对「慢域 → DSP 域」的逐沿跨时钟检查只留约 2.4ns，
   比真实布线延迟还紧，400MHz 下必然报违例。真实需求是「一个慢时钟周期内到达」（翻转电平 +
   3 级 ASYNC_REG 同步链），用 `set_max_delay -datapath_only` 表达，上限取一个慢时钟周期。
   **两个方向都要写**：只写一个方向会让另一个方向变成新的最差路径（实测 +0.055 → −0.258ns）。
4. **运行策略**（`apply_timing_strategy.tcl`）：综合 `PerformanceOptimized` +
   `GLOBAL_RETIMING=on` + `FLATTEN_HIERARCHY=full` + `MAX_DSP=2`；实现用内置
   **`Performance_Retiming`**（phys_opt = `AlternateFlowWithRetiming`：DSP/BRAM 优化 + 重定时），
   **不要覆盖它自带的步骤 directive**，否则掉回 +0.077ns 一档。逐档实测（同一综合网表只换
   impl strategy）：`Performance_Retiming` +0.270ns > `Performance_RefinePlacement` +0.109ns >
   `Performance_ExplorePostRoutePhysOpt` +0.077ns > `Performance_Auto_1/2/3` +0.061ns。
   run 的 `STEPS.*.ARGS.*` 可以直接 `set_property` 改（内置 strategy 也允许），不需要
   `create_strategy`（2025.2 实测 `get_strategies` / `create_strategy` 不存在）；
   但注意**给 run 换 strategy 会把步骤参数重置回该 strategy 的预设**。

### 三个必须记住的实测坑

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| **place/route 有 run-to-run 差异** | 同一网表、同一策略，WNS 在 +0.006 与 −0.202ns 之间摇摆 | 「跑一次通过」不能算签核；用 `vivado/impl_multi.tcl` 多次独立实现取余量最好的一次，报告里给出多次结果 |
| **过约束只能用来逼工具，不能用来签核** | `set_clock_uncertainty` 0.15ns 过约束 → 清掉后 WNS −0.070ns；0.20ns → −0.083ns；0.25/0.30ns 反而退化到 −0.330ns（目标过紧，工具选到更差的解） | 过约束下跑完 place/route 后，用 `reset_clock_uncertainty -setup` 清掉、重新出报告，用**清掉后**的 WNS 作为结论；本设计最终不用过约束 |
| **控制脚寄存器化要成对做** | 只把边沿脉冲延迟一拍送 FSM：FSM 输出（握手翻转信号）也跟着晚一拍，改变与慢速域的握手时序 → Verilator 相位读数超容限（phaseCnt/edgeCnt 由 10.0 变成 14.24） | 时序优化只动不影响协议时序的通路（如计数器控制脚 `clear`/`enable` 的状态译码寄存）；两个使能要**同时**延迟，保持相对时序 |
| **同一工程不能并发跑两个 Vivado** | 每次 `launch_runs` 按当前工程步骤参数重写 `*.runs/<run>/<top>.tcl`，后启动的进程覆盖先生效的配置。实测先设 `Performance_Retiming`、另一仍在跑的脚本改回 `ExplorePostRoutePhysOpt`，同一次改动报出 +0.077ns（与 +0.270ns 差 0.19ns） | 改策略/跑实现前确认没有其它 Vivado 在跑；跑完用 `dpm_bd.runs/<run>/runme.log` 的 `Command:` 行核对实际 directive |
| **手工 one-hot FSM 反而更慢** | 把 ChiselEnum（3 bit）手工改成 `RegInit(1.U(5.W))` one-hot：带 `PopCount` 兜底 WNS −0.444ns、不带兜底 −0.020ns，都差于 ChiselEnum 的 +0.109ns | 让 Vivado 自己做 FSM 重编码；要动编码先量测 |
| **慢域参考频率会影响 DSP 域余量** | 同一 RTL/策略，慢域 50MHz 参考时 MMCM 离散抖动 127ps（100MHz 参考时 101ps），400MHz 域内 WNS 差约 0.2ns | 选参考频率时把 MMCM 抖动算进去；两者都可用 400MHz，但 50MHz 参考要额外留余量 |
| **Safe Clock Startup 的放行延迟是 8 拍** | 在 xsim 里单独跑 clk_wiz（`-L unisims_ver` 才有 MMCME2_ADV/BUFGCE 模型）：MMCM 锁定前未门控输出已有 322 个上升沿、门控后的 `clk_out1` 有 0 个；`locked` 拉高后再过 23ns（≈9 个 400MHz 周期：8 拍移位 + 缓冲器延迟）才出现第一个时钟沿 | 这是该功能应有的行为，不是缺陷；DSP 域复位本来就要等时钟，晚 8 拍放行不影响功能，不需要再叠一层外部 `locked` 门控 |
| **`-tclargs` 必须写在 Vivado 命令行最后** | 写成 `vivado -mode batch -source x.tcl -tclargs 6 -log foo.log` 时，`-log`/`-journal` 会被当成 Tcl 参数吞掉：Vivado 改在**当前目录**写 `vivado.log`/`vivado.jou`（在仓库根跑就会在根目录留下这两个文件） | 把 `-log`/`-journal` 放在 `-tclargs` 之前，或直接用默认日志文件名；跑完顺手确认仓库根没有多出 `vivado.log` |

### 400MHz 的关键路径与实测结果（xc7z010clg400-1，−1）

- 关键路径：`stateReg → LUT → DSP48 的 RSTP/CEP`（data path 2.006ns，其中布线 1.426ns、71% 是布线），
  即「组合逻辑跨 clock region 驱动 DSP 控制脚」。把计数器控制脚（`clear`/`enable` 的状态译码）
  寄存器化后路径消失。
- 结果：默认策略 WNS −0.391ns（失败）→ 上面策略组合 +0.006ns（通过但余量太薄）→
  策略 + RTL 控制脚寄存器化：**6 次独立实现全部为正，最差 +0.236ns、最好 +0.328ns**。
- 该器件 DSP48E1 的 FMAX（全寄存器）为 464.25MHz（DS187 Table 66），400MHz 用掉约 86%；
  `place` 前给 DSP 路径留 ≥500ps 余量（UG949）。

## 交付前

- 报告里写清实际跑到哪一步：IP 打包通过、`validate_bd_design` 通过、xsim 用例数与结果、
  综合实现是否 100%、时序 WNS/WHS（高速域单独给出域内 WNS）、bitstream/XSA 是否生成。
- 高速时钟类改动要给出**多次实现**的结果（见 `impl_multi.tcl`），单次通过不作为签核依据。
- **改了 RTL 之后**：`package_ip.tcl` → `create_bd.tcl` → `apply_timing_strategy.tcl` →
  `build_bitstream.tcl` 必须按顺序重跑；只重跑后面几步会用到旧的 IP 内容。
- 没有跑到的步骤（例如没上板）要明确写成「未验证」，不要用「已验证」概括。
