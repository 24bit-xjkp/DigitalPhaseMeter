# 在 open_project 之前加载的早期消息过滤（由 dpm-vivado-flow 的 build / refresh_bd
# worker 以及 impl_stability / impl_multi / query_runs 在打开工程前 source 本文件）
#
# 【为什么必须放在 open_project 之前】下列告警是在 `open_project` 内部或紧随其后的
# 工程/BD 扫描阶段打印的：
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
# 这是 build stage 主动 reset OOC run 后的**流程中间状态**，紧接着的 launch_runs 就会
# 重新生成这些文件；它不表示工程损坏（全新工程不会有这条）。
set_msg_config -id "IP_Flow 19-3664" -suppress

# ---------------------------------------------------------------------------
# 原位 BD 刷新（refresh_bd.tcl）的预期消息
#
# 场景：package_ip.tcl / package_src_ip.tcl 重新打包了自研 IP，随后 refresh_bd.tcl
# 在**既有工程**上 update_ip_catalog -rebuild / upgrade_ip / generate_target all。
# 下面这些消息描述的是刷新过程中的预期中间态，不是工程/设计缺陷；它们大多在
# open_project 扫描 BD/IP 目录时就会出现，所以过滤同样必须先于 open_project。
# ---------------------------------------------------------------------------
# [BD 41-1661] × N：打开旧工程时，BD 里的 IP 实例因与 catalog 中刚重新打包的版本不一致
#   而被标记为 locked（Vivado 原文：One or more IPs have been locked in the design ...）。
#   这是 IP 重打包后的预期中间态——紧接着的 upgrade_ip 会按新版本升级/解锁这些实例；
#   若刷新失败，upgrade_ip / validate_bd_design 会以更明确的 ERROR 报出来，不会被这条掩盖。
set_msg_config -id "BD 41-1661" -suppress
# [IP_Flow 19-4994]：generate_target all 重新生成 BD 产物时覆盖 auto_pc 的 OOC 约束文件——
#   与 create_bd.tcl 里同一条理由：每次重建/再生成 BD 都必然发生，属预期覆盖。
set_msg_config -id "IP_Flow 19-4994" -suppress
# [IP_Flow 19-5661] / [IP_Flow 19-11770]：重新打包 dpm_top 后重新推断接口时，dspClock
#   时钟接口没有关联总线、也没有 FREQ_HZ——dspClock 只驱动测量逻辑、不驱动 AXI 总线，
#   频率由 BD 里的 MMCM 决定，都是设计如此（理由同 create_bd.tcl / package_ip.tcl）。
set_msg_config -id "IP_Flow 19-5661" -suppress
set_msg_config -id "IP_Flow 19-11770" -suppress
# 注：增量综合/实现的参考 DCP 本身不产生新的预期告警；「没有参考 checkpoint、自动增量
# 编译不启用」这条（Vivado 12-7122）已在上面的通用过滤里覆盖。若集成时发现增量 run
# 还有其它预期消息，按本文件的约定逐条写理由后再加，不要成片屏蔽。

puts "EARLY_FILTER_LOADED: 已屏蔽 open_project / 原位 BD 刷新内部的工程/环境级告警（理由见 vivado/suppress_early_messages.tcl）"
