# JTAG 上板启动流程（AX7010 / xc7z010，2025.2 实测可用）
#
#   xsdb -quiet ps/tools/jtag_boot.tcl
#
# 做四件事：① 复位 PS 并**同时停住核**（`rst -system -stop`）——不能让它跑起来等
# SD 卡的 FSBL/U-Boot，否则 MMU 被打开，PC=0 走虚拟地址、程序根本不会执行（实测踩过）；
# ② 配置 PL（bitstream）；③ 跑本 BD 导出的 ps7_init（PS 时钟 / MIO48-49 UART1 / DDR）；
# ④ 经 **APU 目标的物理通路**把 ps/build/dpm_ps.bin 写到 OCM，PC 指向 0（向量表），运行。
#
# 两条硬约束（实测）：
#   * 运行中**不要**在 Cortex-A9 目标上读内存/寄存器（mrd/rrd/state）——APB-AP 会进入
#     粘滞错误态，之后连 rst -dap/-system/-cores 都复位不了，只能物理复位板子；
#     判断程序状态请读 APU 一侧的 UART/PL 寄存器。
#   * 串口波特率依赖 UART 参考时钟（SLCR UART_CLK_CTRL）。本板实测 86.4MHz
#     （见 ps/src/dpm_main.c 的 DPM_UART_REF_HZ）；换板用 ps/tests/find_uart_baud.py 重测。
set root /home/luo/DigitalPhaseMeter
set bit  $root/build/vivado/bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit
set bin  $root/ps/build/dpm_ps.bin
set a9   "*Cortex-A9 MPCore #0*"
connect
catch {targets 1} e
catch {rst -dap} e0
targets -set -filter {name =~ $a9}
catch {rst -system -stop} er
catch {stop} es0
catch {stop} es
puts "T1 rst=$er state=[state] pc=[rrd pc]"
targets -set -filter {name =~ "xc7z010"}
fpga -f $bit
targets -set -filter {name =~ "APU"}
source $root/build/vivado/bd_proj/dpm_bd.gen/sources_1/bd/dpm_system/ip/dpm_system_processing_system7_0_0/ps7_init.tcl
ps7_init
ps7_post_config
dow -data $bin 0x0
puts "T2 mem0=[mrd -force 0x0]"
targets -set -filter {name =~ $a9}
catch {stop} es2
rwr pc 0x0
puts "T3 pc=[rrd pc]"
con
# 只经 APU 轮询观察（绝不读 A9 目标）
for {set i 0} {$i < 12} {incr i} {
    after 250
    targets -set -filter {name =~ "APU"}
    puts "P$i TRACE=[lindex [mrd -force 0x43C00008] end] BG=[lindex [mrd -force 0xE0001018] end] SR=[lindex [mrd -force 0xE000102C] end]"
}
puts "BOOT7_DONE"
