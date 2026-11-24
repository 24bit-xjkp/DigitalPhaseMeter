# DigitalPhaseMeterTop ASIC（sky130 HD）时序约束
#
# 时钟说明：RTL 不规定频率。FPGA 版本里 dspClock 由 PL 内 MMCM 从 50MHz 倍频到
# 450MHz；ASIC 基线把测量通路降到 300MHz（3.333ns），两个时钟分别为：
#   * clock    : 50MHz（20ns），对应 PS 的 FCLK_CLK0；
#   * dspClock : 300MHz（3.333ns），测量通路的 ASIC 基线频率。
# 如果要评估更高频率，只改下面的 period，其余流程不用动（synth.ys 里
# --clock dspClock=3333 需同步修改）。

set_units -time ns -capacitance pF -resistance kOhm

create_clock -name clock -period 20.0 [get_ports clock]
create_clock -name dspClock -period 3.333 [get_ports dspClock]

set_clock_uncertainty -setup 0.3 [get_clocks {clock dspClock}]
set_clock_uncertainty -hold 0.1 [get_clocks {clock dspClock}]
set_clock_transition 0.1 [get_clocks {clock dspClock}]

# AXI4-Lite 数据端口统一按主时钟约束，留 2ns 外部延迟（约一个周期的 10%）
set_input_delay 2.0 -clock clock [get_ports {
    s_axi_lite_awaddr[*] s_axi_lite_awprot[*] s_axi_lite_awvalid
    s_axi_lite_wdata[*] s_axi_lite_wstrb[*] s_axi_lite_wvalid
    s_axi_lite_bready
    s_axi_lite_araddr[*] s_axi_lite_arprot[*] s_axi_lite_arvalid
    s_axi_lite_rready
}]
set_output_delay 2.0 -clock clock [get_ports {
    s_axi_lite_awready s_axi_lite_wready
    s_axi_lite_bresp[*] s_axi_lite_bvalid
    s_axi_lite_arready s_axi_lite_rdata[*] s_axi_lite_rresp[*]
    s_axi_lite_rvalid interrupt
}]
set_load 0.05 [all_outputs]

# 两个时钟分别来自 PS 和 PL 内 MMCM，相位关系固定不了；控制器内部用
# CdcToggleHandShake 做跨域，因此按异步时钟组处理。
set_clock_groups -asynchronous -group [get_clocks clock] -group [get_clocks dspClock]

# 外部被测信号 / MMCM 源 / locked / 异步复位本来就没有相对采样时钟的固定关系，
# 在测量核内部先过同步链，因此不作为普通输入路径检查。
set_false_path -from [get_ports {
    extSig_sig1 extSig_sig2 intSig_sig1 intSig_sig2 srcLocked
}]
set_false_path -from [get_ports resetn]
