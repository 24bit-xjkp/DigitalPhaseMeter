# 给已存在的 block design 使能 PS UART（不重建 BD）。
#
# 用法（在仓库根目录）：
#   vivado -mode batch -source vivado/patch_enable_ps_uart.tcl -tclargs uart1
#   vivado -mode batch -source vivado/patch_enable_ps_uart.tcl -tclargs uart0
#   vivado -mode batch -source vivado/patch_enable_ps_uart.tcl -tclargs uart1 115200 <bd_proj 路径>
#
# 参数：
#   uart   : uart0 → MIO14/15（bank0，常见 3.3V）；uart1 → MIO48/49（bank1，常见 1.8V）
#   波特率 : 默认 115200
#   工程   : 默认 build/vivado/bd_proj/dpm_bd.xpr
#
# 作用：设置 PS7 的 UART 使能、MIO 分配（含 54 项 MIO 树）、bank 电压无关的属性，
#       保存 BD 并 generate_target，使新的 .hwh / wrapper 生效。
#       之后必须重跑综合/实现（build_bitstream.tcl）才能得到带串口的 bitstream。
#
# 注意：`create_bd.tcl` 会重新生成 BD，会把这里的修改覆盖掉——补丁要写进 create_bd.tcl，
#       或者在 create_bd.tcl 之后跑本脚本（推荐后者，脚本保持单一职责）。

set uart_sel [expr {$argc > 0 ? [lindex $argv 0] : "uart1"}]
set baud     [expr {$argc > 1 ? [lindex $argv 1] : "115200"}]
set proj     [expr {$argc > 2 ? [lindex $argv 2] : "build/vivado/bd_proj/dpm_bd.xpr"}]
# 第四个参数 dry=1 时只做「设置属性 + validate」，不保存、不 generate_target（用于验证属性名）
set dry      [expr {$argc > 3 ? [lindex $argv 3] : "0"}]

if {![file exists $proj]} {
    puts "ERROR: 找不到 BD 工程 $proj（先跑 vivado/create_bd.tcl）"
    exit 1
}

# MIO 号与方向：Zynq UART0 = MIO14(tx)/15(rx)，UART1 = MIO48(tx)/49(rx)
switch -- $uart_sel {
    uart0   { set uart "UART0" ; set mio_tx 14 ; set mio_rx 15 ; set periph "UART 0" ; set sig_tx "uart0_tx" ; set sig_rx "uart0_rx" }
    uart1   { set uart "UART1" ; set mio_tx 48 ; set mio_rx 49 ; set periph "UART 1" ; set sig_tx "uart1_tx" ; set sig_rx "uart1_rx" }
    default { puts "ERROR: 第一个参数必须是 uart0 或 uart1（收到 '$uart_sel'）" ; exit 1 }
}

open_project $proj
set bd_file [get_files -quiet *.bd]
if {$bd_file eq ""} { puts "ERROR: 工程里没有 .bd" ; exit 1 }
open_bd_design $bd_file
set ps [get_bd_cells -quiet processing_system7_0]
if {$ps eq ""} { puts "ERROR: 找不到 processing_system7_0" ; exit 1 }

puts "PATCH: 使能 $uart（$periph），MIO$mio_tx=tx MIO$mio_rx=rx，波特率 $baud"

# ---- 1) MIO 树：54 项，'#' 分隔；把对应位置改成 UART ----
set mio_periph [split [get_property CONFIG.PCW_MIO_TREE_PERIPHERALS $ps] "#"]
set mio_sig    [split [get_property CONFIG.PCW_MIO_TREE_SIGNALS $ps] "#"]
if {[llength $mio_periph] != 54} {
    puts "WARNING: MIO_TREE_PERIPHERALS 长度 [llength $mio_periph] != 54，将重建为全 unassigned"
    set mio_periph [lrepeat 54 "unassigned"]
    set mio_sig    [lrepeat 54 "unassigned"]
}
lset mio_periph $mio_tx $periph
lset mio_periph $mio_rx $periph
lset mio_sig    $mio_tx $sig_tx
lset mio_sig    $mio_rx $sig_rx

# ---- 2) 一次性设置属性 ----
set_property -dict [list \
    CONFIG.PCW_EN_$uart {1} \
    CONFIG.PCW_${uart}_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_${uart}_${uart}_IO "MIO $mio_tx .. $mio_rx" \
    CONFIG.PCW_${uart}_BAUD_RATE $baud \
    CONFIG.PCW_MIO_${mio_tx}_PULLUP {enabled} \
    CONFIG.PCW_MIO_${mio_tx}_DIRECTION {out} \
    CONFIG.PCW_MIO_${mio_tx}_SLEW {slow} \
    CONFIG.PCW_MIO_${mio_rx}_PULLUP {enabled} \
    CONFIG.PCW_MIO_${mio_rx}_DIRECTION {in} \
    CONFIG.PCW_MIO_${mio_rx}_SLEW {slow} \
    CONFIG.PCW_MIO_TREE_PERIPHERALS [join $mio_periph "#"] \
    CONFIG.PCW_MIO_TREE_SIGNALS [join $mio_sig "#"] \
] $ps

# ---- 3) 校验并落盘 ----
set en [get_property CONFIG.PCW_EN_$uart $ps]
set io [get_property CONFIG.PCW_${uart}_${uart}_IO $ps]
puts "PATCH_CHECK: PCW_EN_$uart=$en PCW_${uart}_${uart}_IO=$io"
if {$en != 1} { puts "ERROR: 属性没有生效" ; exit 1 }

if {$dry == 1} {
    puts "PATCH_DRY_RUN: 属性设置成功、未保存（dry=1）"
    close_project
    exit 0
}

validate_bd_design
save_bd_design
generate_target all $bd_file
puts "PATCH_DONE: $uart 已使能（io=$io baud=$baud）。"
puts "PATCH_NEXT: 重跑 vivado/build_bitstream.tcl 得到带串口的 bitstream，再用"
puts "            python3 ps/tools/check_bd_uart.py <xci> 复验。"
close_project
