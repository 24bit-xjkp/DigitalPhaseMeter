# 数字相位计 BD 工程的顶层约束
#
# 时钟拓扑（由 processing_system7 与 clk_wiz 自带约束建立，本文件不重复创建）：
#   clk_fpga_0 = FCLK_CLK0 = 50MHz —— AXI4-Lite 域、低速控制域（闸门计数/FSM）、MMCM 参考时钟
#   clk_out1_dpm_system_clk_wiz_0_0 —— MMCM 从 FCLK_CLK0 倍频出的 DSP 域时钟
#     （默认 400MHz，由 vivado/create_bd.tcl 的 dsp_clk_mhz 参数决定；
#      Zynq-7000 的 PS FCLK 上限 250MHz，300MHz 以上必须用 PL 内 MMCM）
#
# 本文件只补充三类约束：被测信号、慢→快时钟域的握手通路、板级引脚。

# ---- 被测信号 ----
# sig1/sig2 是外部送入的异步信号：在 PL 内先经 EdgeDetector 的 ASYNC_REG 同步链采样，
# 因此作为异步输入处理，不做建立/保持时间检查。
set_false_path -from [get_ports {sig1 sig2}]

# ---- 两个时钟域之间的握手通路（双向）----
# 跨域信号有两类，都是「电平翻转 + 同步器」结构：
#   - 慢 → 快：Controller 的 request / responseReady 在 DSP 域被 EdgeDetector(both) 的
#     3 级 ASYNC_REG 同步链采样；
#   - 快 → 慢：PhaseMeter 的 response.valid / response.ready 在慢域被同样的同步链采样。
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
# 引脚位置与 IO 标准由具体开发板的约束提供，本仓库不绑定开发板。
# 使用时把下面两行取消注释并改成实际引脚（示例为 Zynq-7000 常见 PL 引脚）：
# set_property -dict {PACKAGE_PIN H16 IOSTANDARD LVCMOS33} [get_ports sig1]
# set_property -dict {PACKAGE_PIN J16 IOSTANDARD LVCMOS33} [get_ports sig2]
