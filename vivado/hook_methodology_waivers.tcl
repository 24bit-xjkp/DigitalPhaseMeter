# 实现阶段（ROUTE_DESIGN 步骤的 pre-hook）创建 methodology waiver
#
# 背景：实现的 route_design 步骤末尾会自动跑 report_methodology，报告写在
#   build/vivado/bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper_methodology_drc_routed.rpt
# 并会被 report_timing_summary 抄到报告头部。下面两条是**设计上刻意保留**的告警，
# 无法在不破坏约束可靠性的前提下从工程代码里消除，因此按 AMD 的 waiver 机制显式屏蔽，
# 并在报告里保留「Checks waived」计数（不是删除检查，而是声明已知例外）。
#
#   [TIMING-28] Auto-derived clock referenced by a timing constraint（7 条）
#     vivado/constraints/dpm_top.xdc 里用「通配时钟名 + -quiet」引用 auto-derived 时钟
#     （clkout*_raw、*clk_out1_dpm_system_clk_wiz_0_0*）。这是刻意的：同一份 XDC 要
#     被评估两次（综合阶段 BD 还是黑盒、时钟不存在；实现阶段才有时钟），并且要兼容
#     IP 实例改名/回退方案。Vivado 建议改成 get_clocks -of_objects [get_pins ...]，
#     但那会把约束绑定到更深的层次名上，失效风险更大；本设计选择保留时钟名引用。
#   [TIMING-47] False path / max_delay datapath only 约束用于同步时钟对（6 条：
#     慢↔DSP 双向 4 条 + 片内源 MMCM 的 locked 2 条）
#     跨域握手（慢域 ↔ DSP 域）与片内源 MMCM 的 locked 准静态电平都用
#     set_max_delay -datapath_only 20ns 约束。片内源 MMCM 在运行期由 DRP 改相位/分频，
#     它与 FCLK_CLK0 之间没有固定的同步沿关系；真实需求是「一个慢时钟周期内到达」，
#     故用有界 max_delay（保留布线延迟检查），而不是 set_false_path。
#
# 注意：waiver 与 hook 一样会在 reset_run 后丢失，所以 build_bitstream / impl_stability /
# impl_multi 在 reset 之后都要重新挂本文件到 STEPS.ROUTE_DESIGN.TCL.PRE。
#
# 新增/删除 waiver 时同步更新 vivado/README.md 的「综合 / 实现告警清理」。
create_waiver -type METHODOLOGY -id {TIMING-28} -user "dpm_flow" \
    -description "dpm_top.xdc 刻意用通配时钟名（一份 XDC 需同时适配综合黑盒阶段与实现阶段，并兼容 IP 改名）；改为 get_pins 引用绑定层次更深、失效风险更大"
create_waiver -type METHODOLOGY -id {TIMING-47} -user "dpm_flow" \
    -description "片内源 MMCM 运行期由 DRP 改相位/分频，与 FCLK_CLK0 无固定同步沿关系；跨域握手用有界 set_max_delay -datapath_only 20ns 而非 false_path，属刻意的 CDC 约束"
puts "METHODOLOGY_WAIVERS_LOADED: 已创建 TIMING-28 / TIMING-47 的 methodology waiver（理由见 vivado/hook_methodology_waivers.tcl）"
