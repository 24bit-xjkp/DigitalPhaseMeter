# 数字相位计 BD 工程的顶层约束
#
# 时钟拓扑（由 processing_system7 与 clk_wiz 自带约束建立，本文件不重复创建）：
#   clk_fpga_0 = FCLK_CLK0 = 50MHz —— AXI4-Lite 域、低速控制域（闸门计数/FSM）、MMCM 参考时钟
#   clk_out1_dpm_system_clk_wiz_0_0 —— MMCM 从 FCLK_CLK0 倍频出的 DSP 域时钟
#     （默认 400MHz，由 vivado/create_bd.tcl 的 dsp_clk_mhz 参数决定；
#      Zynq-7000 的 PS FCLK 上限 250MHz，300MHz 以上必须用 PL 内 MMCM）
#
# 时钟拓扑补充（片内信号源，由 BD 里的 dpm_mmcm_src_0 提供）：
#   该模块是裸 MMCME2_ADV 的薄封装（vivado/rtl/dpm_mmcm_src.v），两个 BUFGCE 输出
#   同频（初始 25MHz）、相位差由 MMCM 的相位寄存器通过 DRP 设定（初始 90°）。
#   它们在 IP 内被测量时钟域采样成边沿脉冲（再用 SourceDivider 数边沿分频），
#   对目的域来说是异步输入，与外部引脚同样处理。
#
#   注意：这里的时钟名随实现方式变化。
#     旧方案（clk_wiz_src，IP 内部 BUFGCE）→ `clk_out1_dpm_system_clk_wiz_src_0_0` 之类；
#     新方案（裸 MMCM 薄封装，BUFGCE 在自定义模块里）→ 工具按**网表里的网络名**
#     命名，实测是 `clkout0_raw` / `clkout1_raw`（即 MMCM 的 CLKOUT0/CLKOUT1 网络）。
#   为兼容两种命名，下面同时列出两组名字。
#
# 本文件补充五类约束：被测信号、片内信号源时钟、慢↔快时钟域的握手通路、MMCM 锁定、板级引脚。

# ---- 被测信号（外部输入）----
# sig1/sig2 是外部送入的异步信号：在 PL 内先经 EdgeDetector 的 ASYNC_REG 同步链采样，
# 因此作为异步输入处理，不做建立/保持时间检查。片内信号源（SignalSource）由 DSP 域时钟
# 直接驱动、与外部输入共用同一条采样链，不需要额外约束。
set_false_path -from [get_ports {sig1 sig2}]

# ---- 片内信号源：两路 MMCM 输出 ----
# 频率与相位差都由 MMCM 的动态重配置设定，软件可以在线改动，因此源时钟与测量时钟之间
# 没有固定的相位关系：IP 只用边沿检测链采样它们，不做建立/保持检查。
# 新方案：MMCM 输出的网络名（实测）
set_false_path -from [get_clocks clkout0_raw] -to [get_clocks *clk_wiz_0*]
set_false_path -from [get_clocks clkout1_raw] -to [get_clocks *clk_wiz_0*]
# 旧方案（clk_wiz_src）的时钟名，保留以免回退时漏约束
set_false_path -from [get_clocks *clk_wiz_src*] -to [get_clocks *clk_wiz_0*]
# MMCM 的 locked 是准静态电平，经 3 级同步链进主时钟域（与其它跨域电平同样按「一个慢时钟
# 周期内到达」约束）
set_max_delay -datapath_only -from [get_clocks clkout0_raw] -to [get_clocks clk_fpga_0] 20.000
set_max_delay -datapath_only -from [get_clocks clkout1_raw] -to [get_clocks clk_fpga_0] 20.000
set_max_delay -datapath_only -from [get_clocks *clk_wiz_src*] -to [get_clocks clk_fpga_0] 20.000

# ---- 两个时钟域之间的握手通路（双向）----
# 跨域信号有三类，都是「电平翻转 + 同步器」结构：
#   - 慢 → 快：Controller 的 request / responseReady 在 DSP 域被 EdgeDetector(both) 的
#     3 级 ASYNC_REG 同步链采样；
#   - 快 → 慢：PhaseMeter 的 response.valid / response.ready 在慢域被同样的同步链采样；
#   - 慢 → 快：信号源与输入选择的配置字（频率字/相位字/使能/选择位，整字一起）经
#     CdcConfigBus 的翻转握手送到 DSP 域，数据在整笔传输期间保持稳定，同样只要求
#     「一个慢时钟周期内到达」。
# 源寄存器翻转一次后保持一整个源时钟周期（慢域 50MHz → 20ns），目的域（400MHz）在这一整个
# 周期内有 8 次采样机会，只需要可靠采到这一次跳变，具体落在哪一拍由同步器保证——
# 真实需求是「一个慢时钟周期内到达」，而不是逐沿的建立/保持关系。
#
# 为什么不能按默认的「逐沿跨时钟检查」约束：dspClock 由 MMCM 从 FCLK_CLK0 倍频而来，
# clk_wiz 为了让 MMCM 输出对齐到 FCLK_CLK0，把 CLKOUT0 的相位设成 -T_dsp（相位补偿），
# 于是工具把慢时钟的发射沿与「逻辑上同一时刻」的 dspClock 捕获沿放在一起比，只留给布线
# ~2.4ns——比这两条通路实际的 1.7ns 布线延迟还紧，400MHz 下必然报违例。
#
# 处理方式用有界的 max_delay（而不是 set_false_path），保留对布线延迟的检查：
#   -datapath_only 只量数据路径本身（去掉工具算错的跨时钟相位余量），上限取一个慢时钟
#   周期 20ns。两个方向都要写：只约束一个方向会让另一个方向的路径被挤成新的最差路径
#   （实测：只写慢→快时，快→慢反而从 +0.055ns 掉到 -0.258ns）。
set_max_delay -datapath_only -from [get_clocks clk_fpga_0] \
    -to [get_clocks clk_out1_dpm_system_clk_wiz_0_0] 20.000
set_max_delay -datapath_only -from [get_clocks clk_out1_dpm_system_clk_wiz_0_0] \
    -to [get_clocks clk_fpga_0] 20.000

# ---- 板级引脚 ----
# 外部被测信号输入：sig1 → W18、sig2 → W19，LVCMOS33。
# 这两个引脚在 xc7z010clg400-1 上属于 bank 34 的通用 IO，分别是差分对 L22 的 P/N 两半
# （IO_L22P_T3_34 / IO_L22N_T3_34），这里当两路独立的单端输入使用。
# bank 34 的 VCCO 需要 3.3V 才与 LVCMOS33 匹配：换开发板时按实际供电电压改 IOSTANDARD，
# 引脚号也一并改成实际使用的引脚。
set_property -dict {PACKAGE_PIN W18 IOSTANDARD LVCMOS33} [get_ports sig1]
set_property -dict {PACKAGE_PIN W19 IOSTANDARD LVCMOS33} [get_ports sig2]
