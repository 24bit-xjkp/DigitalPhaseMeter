# 按模块边界给顶层和 PhaseMeter 分别设置 ABC 目标延迟。
#
# 由 synth.ys 通过 Yosys 内置的 `tcl` 命令调用。Yosys 原生的 .ys 语法没有环境
# 变量读取/替换，这里用内置 Tcl 直接读 DPM_DSP_FREQ_MHZ，避免 Python 生成脚本。
# 周期按整数 ps 取整，与 asic/synth/dpm_asic.sdc 的 round(1e6 / f) 使用同一规则。
#
# 顶层只包含 50MHz 的 clock 域和 PhaseMeter 边界之外的逻辑，目标固定 20000ps；
# PhaseMeter 整块在 dspClock 域，目标由 DPM_DSP_FREQ_MHZ（默认 450MHz）决定。

set DSP_FREQ_MHZ 450
if {[info exists ::env(DPM_DSP_FREQ_MHZ)] && [string trim $::env(DPM_DSP_FREQ_MHZ)] ne ""} {
    set DSP_FREQ_MHZ [string trim $::env(DPM_DSP_FREQ_MHZ)]
}
if {![string is double -strict $DSP_FREQ_MHZ] || $DSP_FREQ_MHZ <= 0.0} {
    error "DPM_DSP_FREQ_MHZ 必须是正数（单位 MHz），收到 $DSP_FREQ_MHZ"
}
set DSP_PERIOD_PS [expr {round(1000000.0 / $DSP_FREQ_MHZ)}]
puts "==> dspClock ${DSP_FREQ_MHZ}MHz -> ABC -D ${DSP_PERIOD_PS}ps"

set LIB    build/asic/sky130_fd_sc_hd__ss_100C_1v60_synth.lib
set CONSTR asic/synth/abc.constr

# 不要加 -dress：ABC 的 dress 按等价性转移名字，但在 Yosys 0.71+128 / ABC 1.01
# 上会把 -liberty 映射后的 .gate 网络改写成 LUT/SOP 逻辑网络（Yosys 读回 $lut），
# 最终网表只剩 FF 单元和 assign 表达式，无法交给 OpenROAD。可读性靠 synth.ys 里的
# flatten -separator / rename -wire / rename -unescape 解决。

# select 不带 -add/-del 时会替换当前选择，两个模块各跑一次 abc。
yosys "select DigitalPhaseMeterTop"
yosys "abc -liberty $LIB -constr $CONSTR -D 20000 -dff"
yosys {select PhaseMeter$*}
yosys "abc -liberty $LIB -constr $CONSTR -D $DSP_PERIOD_PS -dff"
