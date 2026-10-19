# 厂商 IP 的 OOC 综合 run 专用前置过滤（只挂在厂商 IP 的 `*_synth_1` run 上）
#
# 与 hook_filter_messages.tcl 的分工：
#   - hook_filter_messages.tcl → synth_1 / impl_1 主 run（BD 顶层与实现读网表阶段的厂商告警）；
#   - 本文件                  → 厂商 IP 自己的 OOC 综合 run（clk_wiz / PS7 / 互联等）。
#     IP 的 OOC run 是独立进程，不继承 synth_1 的 TCL.PRE，所以要单独挂。
#
# 只屏蔽厂商生成 RTL 固有的告警，**不挂到自研 IP（dpm_top / dpm_mmcm_src）的 run 上**，
# 这样自研 RTL 的问题仍然会出现在日志里。
#
# 目前屏蔽：
#   [Netlist 29-345] Vivado 2025.2 的 unisim 默认 BUFGCE.SIM_DEVICE = "ULTRASCALE"，
#     clk_wiz 生成的 RTL 例化 BUFGCE 时没有覆盖该参数，综合与翻译阶段各报一次；
#     工具会自动按当前器件改成 7SERIES，对硬件没有影响。厂商生成文件不应手改。
#     自研 RTL（vivado/rtl/dpm_mmcm_src.v）的同类问题已在源码里显式写 SIM_DEVICE("7SERIES")。
set_msg_config -id "Netlist 29-345" -suppress
puts "VENDOR_IP_FILTER_LOADED: 已屏蔽厂商 IP OOC 综合的 Netlist 29-345（BUFGCE SIM_DEVICE 默认值）"
