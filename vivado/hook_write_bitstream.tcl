# write_bitstream 步骤的 pre-hook：把「引脚未约束」相关的 DRC 降级为警告
#
# 为什么需要单独的 hook：`launch_runs` 启动的 write_bitstream 在独立的 run 里执行，
# 主进程里 set_property SEVERITY 对 run 内部的 DRC 不生效，必须通过
# STEPS.WRITE_BITSTREAM.TCL.PRE 指定本文件（见 build_bitstream.tcl）。
#
# 本仓库不绑定具体开发板：sig1/sig2 没有 PACKAGE_PIN/IOSTANDARD 约束。
# 使用具体开发板时，请在 vivado/constraints/dpm_top.xdc 中填好引脚，
# 并去掉这里的降级（以及 build_bitstream.tcl 中的 hook 设置）。

foreach check {NSTD-1 UCIO-1} {
    set drc [get_drc_checks -quiet $check]
    if {$drc ne ""} {
        set_property SEVERITY {Warning} $drc
    }
}
