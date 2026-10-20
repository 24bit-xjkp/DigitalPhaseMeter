# 综合/实现期屏蔽「厂商 IP 固有」告警的过滤器
#
# 用法：不需要手工调用。dpm-vivado-flow 的 build worker 把它挂到 run 步骤的 TCL.PRE 上，
# 在综合与实现各步骤执行前生效：
#   STEPS.SYNTH_DESIGN.TCL.PRE        → 综合
#   STEPS.INIT_DESIGN.TCL.PRE         → 实现（读网表时出现的告警都归这一步）
#
# 为什么用过滤器而不是改设计：下面这些告警的来源是**厂商 IP 自己声明的端口或随 IP 生成的
# xdc**，既不在本仓库源码里，也不该由本仓库修改：
#   - xlconcat 的 128 个 In* 端口是 IP 内部 HDL 里写死的（不是 BD 里 NUM_PORTS 的结果，
#     NUM_PORTS 只决定 dout 位宽与哪些 In* 被连接）；
#   - Zynq PS7 的 DDR_*/MIO/PS_CLK 等端口，只要不使能 DDR 与对应 MIO，Vivado 一律不连接；
#     PS7 自带的 dpm_system_processing_system7_0_0.xdc 又会给这些不存在的引脚设属性；
#   - proc_sys_reset 的 mb_reset / bus_struct_reset / interconnect_aresetn 三个输出，本设计
#     （无 MicroBlaze、无 AXI4 从口带 SysReset 接口）确实没有消费者；该 IP 不提供关闭选项。
# 它们对功能与时序都没有影响（时序报告见报告结论），因此这里显式屏蔽并在下方逐条写明原因，
# 而不是让日志长期带着上百条噪声——噪声会掩盖真正需要注意的告警。
#
# 每条都写明「为什么可以安全屏蔽」。若以后真的关注其中某项，删掉对应行即可让它重新出现。
#
# 注意：本文件必须留在 vivado/ 下并被 dpm-vivado-flow 的 build worker 加进工程的 utils_1 fileset，
# 否则 Vivado 会报 [Runs 36-537]「hook 脚本不属于工程文件集」。

# ---------------------------------------------------------------------------
# 1) xlconcat_0：128 个输入端口里只有 In0 被连（PL 侧只有一个中断源），其余 127 个恒为
#    unconnected。IP 的 In* 端口是写死的，改 BD 的 NUM_PORTS 也不会减少。
# ---------------------------------------------------------------------------
set_msg_config -id "Synth 8-7129" -suppress

# ---------------------------------------------------------------------------
# 2) 厂商 IP（PS7 / proc_sys_reset / clk_wiz_0）上未连接的端口，以及 BD 例化它们时
#    「声明 N 个连接、实际给 M 个」的条目式告警。前者是 8-7071，后者是 8-7023，
#    两者一一对应，一起屏蔽。
#    本仓库自己的 IP（dpm_top_0）与 DRP 控制器（dpm_src_drp_0）没有未连接端口——
#    IP 打包脚本按端口名推断接口，BD 里逐个 connect_bd_net 连接；这条屏蔽不会掩盖
#    自研 IP 的接口漏连（真要漏了，validate_bd_design 与 xsim 用例都会先报出来）。
# ---------------------------------------------------------------------------
set_msg_config -id "Synth 8-7071" -suppress
set_msg_config -id "Synth 8-7023" -suppress

# ---------------------------------------------------------------------------
# 3) 顶层 wrapper 里唯一的实例没有任何输出端口。本设计的全部对外通路都在 PS 侧
#    （AXI4-Lite 寄存器 + PS7 的 MIO/DDR 硬核引脚），PL 侧本来就没有输出引脚，
#    因此 dpm_system_wrapper 只有 extSig_sig1/extSig_sig2 两个输入端口。要消掉这条告警必须刻意
#    加一个板级调试输出引脚（会同时引入未约束引脚相关的告警），收益不足，故屏蔽。
# ---------------------------------------------------------------------------
set_msg_config -id "Synth 8-4446" -suppress

# ---------------------------------------------------------------------------
# 4) clk_wiz_0 的输入时钟由 fabric 上的 FCLK_CLK0 驱动（不是从顶层引脚进来）。
#    早期配置（PRIM_SOURCE=Single_ended_clock_capable_pin）会让 Wizard 例化一个多余的
#    IBUFG 并被优化掉（报 [Opt 31-32]）；现在改为 PRIM_SOURCE=No_buffer 后 IBUFG 已不存在，
#    这条预计不再触发，保留作换配置时的兜底。
#
#    注意：`Constraints 18-550`（IBUF_LOW_PWR 落不到顶层引脚网络）不在这里屏蔽——它在
#    open_project 之后的约束处理阶段出现，统一由 vivado/suppress_early_messages.tcl 在
#    open_project **之前**屏蔽（本文件在 run 子进程里加载，重复加载等价规则会报
#    [Common 17-1361]，因此两边不要重复写同一条）。
# ---------------------------------------------------------------------------
set_msg_config -id "Opt 31-32" -suppress

# ---------------------------------------------------------------------------
# 5) PS7 自带 xdc 给「未使能因而在网表里不存在」的引脚设 PACKAGE_PIN / IOSTANDARD /
#    drive / slew / pullup / PIO_DIRECTION。实现阶段读网表时，这些 xdc 语句落在
#    type=pin 的对象上，于是每一条都报一次 CRITICAL WARNING（实测 100 条）。
#    真正需要约束的 extSig_sig1/extSig_sig2 在 vivado/constraints/dpm_top.xdc 里单独约束，
#    与本条无关；UART 用到的 MIO48/49 由 PS7 的 PCW 配置决定，走的是硬核，
#    不依赖这些 xdc 语句。
# ---------------------------------------------------------------------------
set_msg_config -id "Netlist 29-160" -suppress

puts "MESSAGE_FILTER_LOADED: 已按 vivado/hook_filter_messages.tcl 屏蔽厂商 IP 固有告警"
