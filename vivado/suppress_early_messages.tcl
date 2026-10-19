# 在 open_project 之前加载的早期消息过滤（由 build_bitstream / impl_stability /
# impl_multi / query_runs 在打开工程前 source 本文件）
#
# 【为什么必须放在 open_project 之前】下列告警是在 `open_project` 内部打印的：
#   - [Board 49-26]     × ~96：Vivado 扫描 XilinxBoardStore 时逐个报告「board 声明的器件未安装」。
#                      本工程只绑定器件、不绑定 board part（Vivado 没有 AX7010 的 board 文件），
#                      这些 board 与设计无关；它是环境/安装状态，不是工程缺陷。
#   - [Project 1-5713]  × 1：工程没有 board part，Vivado 把 BoardPart 属性置空。
#   - [Vivado 12-7122]  × 1：没有参考 checkpoint，自动增量编译不启用（本工程每轮全量跑）。
#   - [Constraints 18-550] × 1：clk_wiz 的 xdc 给「由 fabric 驱动、不是顶层引脚」的输入网络
#                      设 IBUF_LOW_PWR（旧 PRIM_SOURCE 时代的产物；现在的 No_buffer 配置预计
#                      不再触发，保留作兜底）。
# 这些消息早于脚本中任何 `set_msg_config` 生效的时机，所以过滤必须写在 `open_project` 之前；
# 之前文档里「open_project 内部的告警拦不到」的说法是错的——拦住的前提是过滤放得足够早。
#
# 注意：本文件只对「脚本流程」有效；在 GUI 里手工打开工程时仍会看到 Board 49-26 噪声。
set_msg_config -id "Board 49-26" -suppress
set_msg_config -id "Project 1-5713" -suppress
set_msg_config -id "Vivado 12-7122" -suppress
set_msg_config -id "Constraints 18-550" -suppress
# [IP_Flow 19-3664]：OOC 综合 run 被 reset 后、重新综合之前，.gen 下该 IP 的 dcp/stub/sim_netlist
# 暂时不存在，open_project 扫描时会逐文件报「generated file not found, please regenerate」。
# 这是 build_bitstream.tcl 主动 reset OOC run 后的**流程中间状态**，紧接着的 launch_runs 就会
# 重新生成这些文件；它不表示工程损坏（全新工程不会有这条）。
set_msg_config -id "IP_Flow 19-3664" -suppress
puts "EARLY_FILTER_LOADED: 已屏蔽 open_project 内部的工程/环境级告警（Board 49-26 等，理由见 vivado/suppress_early_messages.tcl）"
