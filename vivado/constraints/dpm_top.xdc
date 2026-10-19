# 数字相位计 BD 工程的顶层约束
#
# 时钟拓扑（由 processing_system7 与 clk_wiz 自带约束建立，本文件不重复创建）：
#   clk_fpga_0 = FCLK_CLK0 = 50MHz —— AXI4-Lite 域、低速控制域（闸门计数/FSM）、MMCM 参考时钟
#   clk_out1_dpm_system_clk_wiz_0_0 —— MMCM 从 FCLK_CLK0 倍频出的 DSP 域时钟
#     （默认 450MHz，由 vivado/create_bd.tcl 的 dsp_clk_mhz 参数决定；
#      Zynq-7000 的 PS FCLK 上限 250MHz，300MHz 以上必须用 PL 内 MMCM）
#
# 时钟拓扑补充（片内信号源，由 BD 里的 dpm_mmcm_src_0 提供）：
#   该模块是裸 MMCME2_ADV 的薄封装（vivado/rtl/dpm_mmcm_src.v），两个 BUFGCE 输出
#   同频（初始 25MHz）、相位差由 MMCM 的相位寄存器通过 DRP 设定（初始 90°）。
#   它们在 IP 内被测量时钟域直接采样成边沿脉冲（不做数字分频），
#   对目的域来说是异步输入，与外部引脚同样处理。
#
#   注意：这里的时钟名随实现方式变化。
#     旧方案（clk_wiz_src，IP 内部 BUFGCE）→ `clk_out1_dpm_system_clk_wiz_src_0_0` 之类；
#     新方案（裸 MMCM 薄封装，BUFGCE 在自定义模块里）→ 工具按**网表里的网络名**
#     命名，实测是 `clkout0_raw` / `clkout1_raw`（即 MMCM 的 CLKOUT0/CLKOUT1 网络）。
#   为兼容两种命名，下面不写死具体时钟名，改用通配匹配（见「片内信号源」一段）。
#
# 本文件补充五类约束：被测信号、片内信号源时钟、慢↔快时钟域的握手通路、MMCM 锁定、板级引脚。

# ---- 被测信号（外部输入）----
# extSig_sig1/extSig_sig2 是外部送入的异步信号：在 PL 内先经 EdgeDetector 的 ASYNC_REG 同步链采样，
# 因此作为异步输入处理，不做建立/保持时间检查。片内信号源（dpm_mmcm_src_0 的两路 MMCM
# 输出）从 BD 里直接进 IP，与外部输入共用同一条采样链，同样按异步输入处理。
set_false_path -quiet -from [get_ports -quiet {extSig_sig1 extSig_sig2}]

# ---- 片内信号源：两路 MMCM 输出 ----
# 频率与相位差都由 MMCM 的动态重配置设定，软件可以在线改动，因此源时钟与测量时钟之间
# 没有固定的相位关系：IP 只用边沿检测链采样它们，不做建立/保持检查。
#
# 【为什么必须用「通配符 + -quiet」这种写法】本文件被评估两次：综合器启动时一次，实现读
# 网表时一次。综合阶段 BD 还是黑盒（Black Box），MMCM/clk_wiz 都还没展开，本文件用到的
# 时钟一个都不存在；此时若直接写 `get_clocks <具体名字>`，约束评估会失败并告警：
#   综合    [Vivado 12-627] No clocks matched '...'（逐条）
#           [Project 1-498] One or more constraints failed evaluation ...（整文件级，提示
#                           改用实现阶段再读）
#   实现    [Vivado 12-4739] CRITICAL WARNING: set_false_path: No valid object(s) found ...
# 而这些时钟名本来也只有实现期才有（片内源是裸 MMCM 薄封装的网表网络名 clkout0_raw /
# clkout1_raw，见上方「时钟拓扑补充」；DSP 域时钟名带 IP 实例路径前缀
# clk_out1_dpm_system_clk_wiz_0_0）。
#
# xdc 不是完整的 tcl——**不支持任何流程控制命令**，写 `foreach` / `if` 会报
# [Designutils 20-1307] Command '...' is not supported in the xdc constraint file
# （CRITICAL WARNING，实测）。所以不能用「先判断再施加」，只能靠两个开关等效实现：
#   1) 取时钟对象用「通配符 + -quiet」：通配符一次匹配一组时钟（如 clkout*_raw 同时命中
#      clkout0_raw 与 clkout1_raw），-quiet 让「一个都没匹配上」时安静地返回空串（无 12-627）；
#   2) 约束命令本身也带 -quiet：对象为空时安静地不生效（无 12-4739）。
# 于是综合阶段这些约束是「安静地不适用」，实现阶段（时钟已由 IP 生成）正常生效；空对象上的
# 约束本来就是空操作，两种写法效果等同。
# 顺带一点：匹配改用通配后，回退到旧的 clk_wiz 方案时（时钟名变成
# clk_out1_dpm_system_clk_wiz_src_0_0 之类）这些约束会自动跟上，不必再改本文件。

# MMCM 输出 → DSP 域测量时钟：源时钟异步，不做建立/保持检查
set_false_path -quiet -from [get_clocks -quiet clkout*_raw] \
    -to [get_clocks -quiet *clk_out1_dpm_system_clk_wiz_0_0*]

# MMCM 的 locked 是准静态电平，经 3 级同步链进主时钟域（与其它跨域电平同样按「一个慢时钟
# 周期内到达」约束）。-datapath_only 只量数据路径，上限取一个慢时钟周期 20ns。
set_max_delay -quiet -datapath_only -from [get_clocks -quiet clkout*_raw] \
    -to [get_clocks -quiet clk_fpga_0] 20.000

# ---- 两个时钟域之间的握手通路（双向）----
# 跨域信号有两类，都是「电平翻转 + 同步器」结构：
#   - 慢 → 快：Controller 的 request / responseReady 在 DSP 域被 EdgeDetector(both) 的
#     3 级 ASYNC_REG 同步链采样；
#   - 快 → 慢：PhaseMeter 的 response.valid / response.ready 在慢域被同样的同步链采样。
#
# 配置通路已经不再是跨域通路：片内源使能与输入选择（SIG_CTRL）同寄存器堆一样在主时钟域，
# 门控与二选一也在主时钟域完成（SignalInputMux），只有它的**输出**作为异步被测信号进
# 测量域的采样链——与外部引脚 extSig_sig1/extSig_sig2 同一待遇（见上方「被测信号」一段）。
#
# 源寄存器翻转一次后保持一整个源时钟周期（慢域 50MHz → 20ns），目的域（450MHz）在这一整个
# 周期内有 9 次采样机会，只需要可靠采到这一次跳变，具体落在哪一拍由同步器保证——
# 真实需求是「一个慢时钟周期内到达」，而不是逐沿的建立/保持关系。
#
# 为什么不能按默认的「逐沿跨时钟检查」约束：dspClock 由 MMCM 从 FCLK_CLK0 倍频而来，
# clk_wiz 为了让 MMCM 输出对齐到 FCLK_CLK0，把 CLKOUT0 的相位设成 -T_dsp（相位补偿），
# 于是工具把慢时钟的发射沿与「逻辑上同一时刻」的 dspClock 捕获沿放在一起比，只留给数据路径
# ~2.1ns（≈1 个 DSP 周期 2.222ns 减去时钟不确定性）——比这两条通路在 400MHz 轮次实测的
# 约 1.7ns 布线延迟还紧，450MHz 下按逐沿跨时钟检查必然报违例。
#
# 处理方式用有界的 max_delay（而不是 set_false_path），保留对布线延迟的检查：
#   -datapath_only 只量数据路径本身（去掉工具算错的跨时钟相位余量），上限取一个慢时钟
#   周期 20ns。两个方向都要写：只约束一个方向会让另一个方向的路径被挤成新的最差路径
#   （实测：只写慢→快时，快→慢反而从 +0.055ns 掉到 -0.258ns）。
# 这两条同样按上面的写法带上通配与 -quiet：慢域时钟用 clk_fpga_0，DSP 域时钟名带 IP
# 实例路径前缀，写成通配以便 IP 实例改名或回退方案后仍然生效。
set_max_delay -quiet -datapath_only -from [get_clocks -quiet clk_fpga_0] \
    -to [get_clocks -quiet *clk_out1_dpm_system_clk_wiz_0_0*] 20.000
set_max_delay -quiet -datapath_only -from [get_clocks -quiet *clk_out1_dpm_system_clk_wiz_0_0*] \
    -to [get_clocks -quiet clk_fpga_0] 20.000

# ---- 板级引脚 ----
# 外部被测信号输入：sig1 → W18、sig2 → W19，LVCMOS33。
# 这两个引脚在 xc7z010clg400-1 上属于 bank 34 的通用 IO，分别是差分对 L22 的 P/N 两半
# （IO_L22P_T3_34 / IO_L22N_T3_34），这里当两路独立的单端输入使用。
# bank 34 的 VCCO 需要 3.3V 才与 LVCMOS33 匹配：换开发板时按实际供电电压改 IOSTANDARD，
# 引脚号也一并改成实际使用的引脚。
set_property -dict {PACKAGE_PIN W18 IOSTANDARD LVCMOS33} [get_ports extSig_sig1]
set_property -dict {PACKAGE_PIN W19 IOSTANDARD LVCMOS33} [get_ports extSig_sig2]
