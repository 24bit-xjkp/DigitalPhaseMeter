# DigitalPhaseMeterTop ASIC（sky130 HD）时序约束
#
# 时钟说明：RTL 不规定频率。FPGA 版本里 dspClock 由 PL 内 MMCM 从 50MHz 倍频到
# 450MHz；ASIC 默认与 FPGA 同频（450MHz / 2.222ns），两个时钟分别为：
#   * clock    : 50MHz（20ns），对应 PS 的 FCLK_CLK0，固定不变；
#   * dspClock : 测量通路的 ASIC 频率，由环境变量 DPM_DSP_FREQ_MHZ 决定
#                （默认 450MHz）。`dpm-asic-flow` 用同一个变量、同一套取整规则
#                算出 Yosys ABC 的 -D（整数 ps），所以综合与 OpenROAD 始终一致。
# 要评估其他频率只 export 这个变量即可，不需要改本文件。

set_units -time ns -capacitance pF -resistance kOhm

set DSP_FREQ_MHZ 450
if {[info exists ::env(DPM_DSP_FREQ_MHZ)] && [string trim $::env(DPM_DSP_FREQ_MHZ)] ne ""} {
    set DSP_FREQ_MHZ [string trim $::env(DPM_DSP_FREQ_MHZ)]
}
if {![string is double -strict $DSP_FREQ_MHZ] || $DSP_FREQ_MHZ <= 0.0} {
    error "DPM_DSP_FREQ_MHZ 必须是正数（单位 MHz），收到 $DSP_FREQ_MHZ"
}
# 周期按整数 ps 取整，与 `dpm-asic-flow` 生成给 ABC 的 -D 数值一致
set DSP_PERIOD_PS [expr {round(1000000.0 / $DSP_FREQ_MHZ)}]

create_clock -name clock -period 20.0 [get_ports clock]
create_clock -name dspClock -period [expr {$DSP_PERIOD_PS / 1000.0}] [get_ports dspClock]

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
