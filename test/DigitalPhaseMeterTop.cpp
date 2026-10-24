// DigitalPhaseMeterTop 的 Verilator 测试（顶层 IP：AXI4-Lite 寄存器通路 + 测量 + 输入源切换）
//
// 被测对象是 Chisel 生成的 DigitalPhaseMeterTop（build/chisel/DigitalPhaseMeterTop/，与综合、
// xsim 联调消费同一份产物）。顶层不含任何厂商原语，因此寄存器通路与测量通路的连接由本测试
// 覆盖；依赖真实 MMCME2_ADV 的联调仍在 sim/tb_src_handoff.sv 里用 xsim 跑。
//
// 数据通道是 GPIO 式的寄存器：一次测量的结果只有 4 个字，锁存在 ControlRegs 的只读寄存器里，
// PS 收到中断后读走即可，IP 内部没有 BRAM，对外也只有 s_axi_lite 一个 AXI4-Lite 从口。覆盖：
//   - 主时钟同步复位的异步置位/同步释放：复位期间无中断、AXI 从口不接受任何传输、寄存器为默认值；
//   - AXI4-Lite 寄存器读写（含写地址/写数据通道顺序颠倒、字节选通、只读寄存器写忽略），
//     且按 BD 里的真实基地址 + 偏移驱动（地址译码只消费页内偏移，见 dpm-chisel-rtl）；
//   - 测量启动与 busy/resultValid/lost/中断挂起位的时序，busy 期间重复写 start 被忽略；
//   - 测量完成中断（高电平）、中断使能门控，以及软件写 IRQ_STATUS 清中断/清 resultValid/清丢失计数；
//   - 测量期间改写 GATE_TARGET 不影响本次测量（targetGate 由 Controller 在启动的那一拍锁存）；
//   - CTRL.clear 写当拍生效：busy 期间写清只复位结果区与标志（不清 busy），在途测量继续跑完并
//     照常锁存结果，记录序号从 0 重新开始；
//   - 读 4 个结果寄存器并用理论模型校验 gateCnt / edgeCnt / phaseCnt
//     （模型见 .agents/skills/dpm-measurement-model）；
//   - 结果只保留最新一条：不读走就再次测量会覆盖旧结果并累加 LOST_COUNT；
//   - 片内信号源与输入选择：SIG_CTRL / SIG_STATUS 经 AXI 读写，选中片内源时测量跟随片内信号、
//     切回外部输入后跟随外部引脚信号（两条通路的理论值不同，因此这个用例能区分「切换生效」与
//     「切换没生效」）。片内源的两路信号真实来自 PL 内 MMCM（动态可重配置），测试里用两路固定
//     相位差的理想时钟代替（周期 640ns、相位差 160ns，由 `generate_clock` 的起始延时表达）；
//     MMCM 的寄存器语义由联调激励覆盖。

#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
#include <DigitalPhaseMeterTopVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using namespace verilator_utils;
    using dut_t = DigitalPhaseMeterTopVerilator;
    using dut_context_t = dut_context<dut_t, VerilatedFstC>;

    /// 主时钟 50MHz（低速域：寄存器堆、控制 FSM、闸门计数）
    constexpr auto clock_period{20_ns};
    /// 高速测量时钟。测试里取 250MHz；芯片实际是 450MHz，测量语义与两个时钟的周期比无关。
    /// 时间精度为 ns，周期必须取整数纳秒的偶数，半周期才不会被截断（5_ns 会变成 4ns）
    constexpr auto dsp_period{4_ns};

    /// 外部引脚上的被测信号：周期 400ns，sig2 上升沿滞后 sig1 120ns
    /// （两路都用 `generate_clock` 产生，`offset` 作为第三个参数即起始延时）
    constexpr auto external_period{400_ns};
    constexpr auto external_offset{120_ns};
    /// 片内信号源的两路输出（真实设计里是 MMCM 的两路同频、相位差可调时钟）：
    /// 周期 640ns、相位差 160ns（90°）。测试用理想时钟代替，IP 只需要它们的边沿
    constexpr auto internal_period{640_ns};
    constexpr auto internal_offset{160_ns};

    /// IP 在 BD 里的基地址（`vivado/create_bd.tcl` 的 M00_AXI → 0x43C0_0000）。
    ///
    /// 激励必须按**真实地址**（基地址 + 偏移）驱动：AXI 互连不剥离基地址，slave 看到的是完整
    /// 32 位地址。按裸偏移（`offset`）驱动就测不出「窗口判断误用高位地址」这类缺陷。
    constexpr std::uint32_t ip_base{0x43C0'0000u};

    [[nodiscard]] constexpr std::uint32_t ip_addr(std::uint32_t offset) { return ip_base + offset; }

    // 寄存器偏移（字对齐，字节地址）
    constexpr std::uint32_t reg_ctrl{0x00};
    constexpr std::uint32_t reg_status{0x04};
    constexpr std::uint32_t reg_gate_target{0x08};
    constexpr std::uint32_t reg_result_gate{0x0C};
    constexpr std::uint32_t reg_result_edge{0x10};
    constexpr std::uint32_t reg_result_phase{0x14};
    constexpr std::uint32_t reg_result_seq{0x18};
    constexpr std::uint32_t reg_lost_count{0x1C};
    constexpr std::uint32_t reg_irq_status{0x20};
    constexpr std::uint32_t reg_version{0x24};
    constexpr std::uint32_t reg_caps{0x28};
    constexpr std::uint32_t reg_sig_ctrl{0x2C};
    constexpr std::uint32_t reg_sig_status{0x30};

    // CTRL 位
    constexpr std::uint32_t ctrl_start{0};
    constexpr std::uint32_t ctrl_irq_enable{1};
    constexpr std::uint32_t ctrl_clear{2};
    // STATUS 位
    constexpr std::uint32_t status_busy{0};
    constexpr std::uint32_t status_result{1};
    constexpr std::uint32_t status_lost{2};
    constexpr std::uint32_t status_irq{3};
    // IRQ_STATUS 位（写 1 清除）
    constexpr std::uint32_t irq_pending{0};
    constexpr std::uint32_t irq_result_ack{1};
    constexpr std::uint32_t irq_lost_clear{2};
    // SIG_CTRL 位
    constexpr std::uint32_t sig_enable{0};
    constexpr std::uint32_t sig_select{1};
    // SIG_STATUS 位
    constexpr std::uint32_t sig_status_enable{0};
    constexpr std::uint32_t sig_status_select{1};
    constexpr std::uint32_t sig_status_locked{2};
    constexpr std::uint32_t sig_status_ready{3};

    // 只读寄存器里的常量
    /// 版本号 "DPM2"，末尾 02 表示数据经寄存器（GPIO 式）传递
    constexpr std::uint32_t version_value{0x4450'4D02u};
    /// b[7:0] 每条结果字数 = 4、b[15:8] 结果深度 = 1、b16 数据通道为寄存器、b17 带片内源
    constexpr std::uint32_t caps_value{0x0003'0104u};

    /// 目标闸门计数（低速时钟周期）
    constexpr std::uint32_t gate_slow{2000u};
    constexpr std::uint32_t gate_fast{500u};
    /// 覆盖「结果只保留最新一条」用的三个不同闸门时间
    constexpr std::uint32_t gate_a{600u};
    constexpr std::uint32_t gate_b{1000u};
    constexpr std::uint32_t gate_c{1400u};

    /// AXI 单笔事务的握手超时（低速时钟周期数）
    constexpr std::size_t axi_timeout{32zu};

    [[nodiscard]] constexpr std::uint32_t bit_mask(std::uint32_t index) { return 1u << index; }

    /// DUT 端口包装：两个时钟、两条输入通路、AXI4-Lite 从口与中断
    struct port_t
    {
        bit_slice<CData> clock;
        bit_slice<CData> resetn;
        bit_slice<CData> dsp_clock;
        bit_slice<CData> ext_sig1;
        bit_slice<CData> ext_sig2;
        bit_slice<CData> int_sig1;
        bit_slice<CData> int_sig2;
        bit_slice<CData> src_locked;
        // AXI4-Lite 从口
        vector_slice<IData> awaddr;
        vector_slice<CData> awprot;
        bit_slice<CData> awvalid;
        bit_slice<CData> awready;
        vector_slice<IData> wdata;
        vector_slice<CData> wstrb;
        bit_slice<CData> wvalid;
        bit_slice<CData> wready;
        vector_slice<CData> bresp;
        bit_slice<CData> bvalid;
        bit_slice<CData> bready;
        vector_slice<IData> araddr;
        vector_slice<CData> arprot;
        bit_slice<CData> arvalid;
        bit_slice<CData> arready;
        vector_slice<IData> rdata;
        vector_slice<CData> rresp;
        bit_slice<CData> rvalid;
        bit_slice<CData> rready;
        // 测量完成中断（端口名命中 Verilator 的 C++ 常用词，生成代码里是 __SYM__interrupt）
        bit_slice<CData> interrupt;

        explicit port_t(dut_t& dut) :
            clock{dut.clock}, resetn{dut.resetn}, dsp_clock{dut.dspClock, boolean}, ext_sig1{dut.extSig_sig1, boolean},
            ext_sig2{dut.extSig_sig2, boolean}, int_sig1{dut.intSig_sig1, boolean}, int_sig2{dut.intSig_sig2, boolean},
            src_locked{dut.srcLocked, boolean}, awaddr{dut.s_axi_lite_awaddr, 32zu, dec_unsigned},
            awprot{dut.s_axi_lite_awprot, 3zu, dec_unsigned}, awvalid{dut.s_axi_lite_awvalid, boolean},
            awready{dut.s_axi_lite_awready, boolean}, wdata{dut.s_axi_lite_wdata, 32zu, dec_unsigned},
            wstrb{dut.s_axi_lite_wstrb, 4zu, dec_unsigned}, wvalid{dut.s_axi_lite_wvalid, boolean},
            wready{dut.s_axi_lite_wready, boolean}, bresp{dut.s_axi_lite_bresp, 2zu, dec_unsigned},
            bvalid{dut.s_axi_lite_bvalid, boolean}, bready{dut.s_axi_lite_bready, boolean},
            araddr{dut.s_axi_lite_araddr, 32zu, dec_unsigned}, arprot{dut.s_axi_lite_arprot, 3zu, dec_unsigned},
            arvalid{dut.s_axi_lite_arvalid, boolean}, arready{dut.s_axi_lite_arready, boolean},
            rdata{dut.s_axi_lite_rdata, 32zu, dec_unsigned}, rresp{dut.s_axi_lite_rresp, 2zu, dec_unsigned},
            rvalid{dut.s_axi_lite_rvalid, boolean}, rready{dut.s_axi_lite_rready, boolean},
            interrupt{dut.__SYM__interrupt, boolean}
        {
        }

        /// 在下降沿之后、DUT 评估之前设置激励
        [[nodiscard]] auto stimulate(std::size_t cycles = 1zu) const { return wait_stimulate(clock, cycles); }
    };

    /// 复位并初始化全部 AXI 输入：复位保持 8 个低速周期，释放后再等 8 个周期
    /// （覆盖 ResetSynchronizer 的 4 级同步释放与 Controller 内部 DSP 域复位的释放）
    task<void> init_dut(port_t& port)
    {
        port.awaddr = 0;
        port.awprot = 0;
        port.awvalid = 0;
        port.wdata = 0;
        port.wstrb = 0;
        port.wvalid = 0;
        port.bready = 1;
        port.araddr = 0;
        port.arprot = 0;
        port.arvalid = 0;
        port.rready = 1;
        port.src_locked = 0;

        port.resetn = 0;
        co_await port.stimulate(8zu);
        port.resetn = 1;
        co_await port.stimulate(8zu);
    }

    /// 等写响应：bready 常为 1，bvalid 只保持一个低速周期，因此在上升沿之后评估时采样
    task<void> wait_write_response(const port_t& port)
    {
        bool responded{};
        for(std::size_t i{0zu}; i != axi_timeout && !responded; ++i)
        {
            co_await wait_verify(port.clock);
            if(port.bvalid != 0u)
            {
                responded = true;
                CHECK_EQ(static_cast<std::uint32_t>(port.bresp), 0u);
            }
        }
        CHECK_MESSAGE(responded, "AXI 写响应超时");
    }

    /// AXI4-Lite 写一笔：先写地址通道，再写数据通道
    task<void> axi_write(port_t& port, std::uint32_t offset, std::uint32_t data, std::uint32_t strb = 0xFu)
    {
        // 在下降沿等地址通道空闲，然后驱动；下一个上升沿握手（awready 被从口组合拉低）
        while(true)
        {
            co_await port.stimulate();
            if(port.awready != 0u) { break; }
        }
        port.awaddr = ip_addr(offset);
        port.awvalid = 1;
        co_await wait_verify(port.clock);
        co_await port.stimulate();
        port.awvalid = 0;

        // 写数据通道
        while(true)
        {
            co_await port.stimulate();
            if(port.wready != 0u) { break; }
        }
        port.wdata = data;
        port.wstrb = strb;
        port.wvalid = 1;
        co_await wait_verify(port.clock);
        co_await port.stimulate();
        port.wvalid = 0;

        co_await wait_write_response(port);
    }

    /// AXI4-Lite 写一笔：先写数据通道，再写地址通道（AXI 允许的另一种顺序）
    task<void> axi_write_data_first(port_t& port, std::uint32_t offset, std::uint32_t data)
    {
        while(true)
        {
            co_await port.stimulate();
            if(port.wready != 0u) { break; }
        }
        port.wdata = data;
        port.wstrb = 0xFu;
        port.wvalid = 1;
        co_await wait_verify(port.clock);
        co_await port.stimulate();
        port.wvalid = 0;

        while(true)
        {
            co_await port.stimulate();
            if(port.awready != 0u) { break; }
        }
        port.awaddr = ip_addr(offset);
        port.awvalid = 1;
        co_await wait_verify(port.clock);
        co_await port.stimulate();
        port.awvalid = 0;

        co_await wait_write_response(port);
    }

    /// AXI4-Lite 读一笔：rready 常为 1，读到 rvalid 的那一拍即完成握手，直接取 rdata
    task<std::uint32_t> axi_read(port_t& port, std::uint32_t offset)
    {
        while(true)
        {
            co_await port.stimulate();
            if(port.arready != 0u) { break; }
        }
        port.araddr = ip_addr(offset);
        port.arvalid = 1;

        std::uint32_t value{};
        bool received{};
        for(std::size_t i{0zu}; i != axi_timeout && !received; ++i)
        {
            co_await wait_verify(port.clock);
            if(port.rvalid != 0u)
            {
                received = true;
                value = static_cast<std::uint32_t>(port.rdata);
                CHECK_EQ(static_cast<std::uint32_t>(port.rresp), 0u);
            }
        }
        co_await port.stimulate();
        port.arvalid = 0;
        CHECK_MESSAGE(received, "AXI 读响应超时");
        co_return value;
    }

    /// 一条测量结果（GPIO 式数据通道的 4 个寄存器）
    struct measurement_t
    {
        std::uint32_t gate_cnt{};
        std::uint32_t edge_cnt{};
        std::uint32_t phase_cnt{};
        std::uint32_t seq{};
    };

    /// 一条结果的理论预期：目标闸门、记录序号、所用信号源的周期与相位差
    struct expectation_t
    {
        std::uint32_t gate_target{};
        std::uint32_t seq{};
        femtosecond_t signal_period{external_period};
        femtosecond_t phase_offset{external_offset};
    };

    [[nodiscard]] expectation_t expect(std::uint32_t gate_target,
                                       std::uint32_t seq,
                                       femtosecond_t signal_period = external_period,
                                       femtosecond_t phase_offset = external_offset)
    { return expectation_t{gate_target, seq, signal_period, phase_offset}; }

    /// 读走最新一条测量结果
    task<measurement_t> read_record(port_t& port)
    {
        measurement_t record{};
        record.gate_cnt = co_await axi_read(port, reg_result_gate);
        record.edge_cnt = co_await axi_read(port, reg_result_edge);
        record.phase_cnt = co_await axi_read(port, reg_result_phase);
        record.seq = co_await axi_read(port, reg_result_seq);
        co_return record;
    }

    /// 边沿计数容限：窗口边界上可能多算或少算一个沿
    constexpr approx<std::uint64_t> edge_cnt_tolerance{2zu, 0.0};

    /// 用测量模型校验一条结果（容限推导见 .agents/skills/dpm-measurement-model）
    void check_record(const measurement_t& record, const expectation_t& expectation)
    {
        CAPTURE(record.gate_cnt);
        CAPTURE(record.edge_cnt);
        CAPTURE(record.phase_cnt);
        CAPTURE(record.seq);

        // 闸门计数：窗口要等到被测信号的下一个上升沿才关闭，因此最多多一个信号周期；
        // 再加上停止握手与结果锁存需要的几个低速周期
        const auto gate_tolerance{static_cast<std::uint64_t>(std::ceil(static_cast<double>(expectation.signal_period.rep) /
                                                                       static_cast<double>(clock_period.rep))) +
                                  8u};
        const approx<std::uint64_t> gate_cnt_tolerance{gate_tolerance, 0.0};
        CHECK_EQ(static_cast<std::uint64_t>(record.gate_cnt), gate_cnt_tolerance(expectation.gate_target));

        // 边沿计数：窗口长度除以参考信号周期，窗口边界上允许 ±2
        const auto expected_edge_cnt{
            static_cast<std::uint64_t>(std::llround(static_cast<double>(record.gate_cnt) * static_cast<double>(clock_period.rep) /
                                                    static_cast<double>(expectation.signal_period.rep)))};
        CHECK_EQ(static_cast<std::uint64_t>(record.edge_cnt), edge_cnt_tolerance(expected_edge_cnt));

        // 相位计数：每个参考周期累计 offset / T_dsp 个高速周期，容限为每周期 1 个高速周期
        const auto expected_phase_cnt{static_cast<std::uint64_t>(
            std::llround(static_cast<double>(record.edge_cnt) * static_cast<double>(expectation.phase_offset.rep) /
                         static_cast<double>(dsp_period.rep)))};
        const approx<std::uint64_t> phase_cnt_tolerance{record.edge_cnt, 0.0};
        CHECK_EQ(static_cast<std::uint64_t>(record.phase_cnt), phase_cnt_tolerance(expected_phase_cnt));

        CHECK_EQ(record.seq, expectation.seq);
    }

    /// STATUS 四个标志位
    void check_status(std::uint32_t status, bool busy, bool result_valid, bool lost, bool irq_pending)
    {
        CAPTURE(status);
        CHECK_EQ((status >> status_busy) & 1u, busy ? 1u : 0u);
        CHECK_EQ((status >> status_result) & 1u, result_valid ? 1u : 0u);
        CHECK_EQ((status >> status_lost) & 1u, lost ? 1u : 0u);
        CHECK_EQ((status >> status_irq) & 1u, irq_pending ? 1u : 0u);
    }

    /// IRQ_STATUS 三个可清标志位（写 1 清除）
    void check_irq_status(std::uint32_t value, bool pending, bool result_valid, bool lost)
    {
        CAPTURE(value);
        CHECK_EQ((value >> irq_pending) & 1u, pending ? 1u : 0u);
        CHECK_EQ((value >> irq_result_ack) & 1u, result_valid ? 1u : 0u);
        CHECK_EQ((value >> irq_lost_clear) & 1u, lost ? 1u : 0u);
    }

    /// SIG_STATUS 四个状态位（enable / srcSelect / srcLocked / sourceReady）
    void check_sig_status(std::uint32_t value, bool enable, bool select_internal, bool locked, bool source_ready)
    {
        CAPTURE(value);
        CHECK_EQ((value >> sig_status_enable) & 1u, enable ? 1u : 0u);
        CHECK_EQ((value >> sig_status_select) & 1u, select_internal ? 1u : 0u);
        CHECK_EQ((value >> sig_status_locked) & 1u, locked ? 1u : 0u);
        CHECK_EQ((value >> sig_status_ready) & 1u, source_ready ? 1u : 0u);
    }

    /// 只清中断挂起位（不动 resultValid）：让下一次 wait_interrupt 等的是新测量的完成
    task<void> clear_irq_pending(port_t& port) { co_await axi_write(port, reg_irq_status, bit_mask(irq_pending)); }

    /// 表示「结果已被软件读走」：同时清中断挂起位与 resultValid
    task<void> acknowledge_result(port_t& port)
    { co_await axi_write(port, reg_irq_status, bit_mask(irq_pending) | bit_mask(irq_result_ack)); }

    /// 设置闸门时间并启动一次测量（保留中断使能位），不等待完成
    task<void> start_measurement(port_t& port, std::uint32_t gate_target, bool irq_enable)
    {
        const auto ctrl_value{(irq_enable ? bit_mask(ctrl_irq_enable) : 0u) | bit_mask(ctrl_start)};
        co_await axi_write(port, reg_gate_target, gate_target);
        co_await axi_write(port, reg_ctrl, ctrl_value);
    }

    /// 等到中断拉高；返回是否在给定的低速周期数内看到中断
    task<bool> wait_interrupt(const port_t& port, std::uint32_t max_cycles)
    {
        for(std::uint32_t cycle{}; cycle != max_cycles; ++cycle)
        {
            co_await wait_verify(port.clock);
            if(port.interrupt != 0u) { co_return true; }
        }
        co_return false;
    }

    /// 轮询 STATUS.busy 直到测量结束（不看中断）
    task<bool> wait_not_busy(port_t& port, std::uint32_t max_reads)
    {
        for(std::uint32_t read{}; read != max_reads; ++read)
        {
            const auto status{co_await axi_read(port, reg_status)};
            if(((status >> status_busy) & 1u) == 0u) { co_return true; }
        }
        co_return false;
    }

    /// 一次完整测量：启动，然后等本次测量的完成中断
    task<bool> run_measurement(port_t& port, std::uint32_t gate_target)
    {
        co_await clear_irq_pending(port);
        co_await start_measurement(port, gate_target, true);
        const auto interrupted{co_await wait_interrupt(port, 4u * gate_target + 200u)};
        co_return interrupted;
    }

    constexpr dut_context_option ctx_opt{.coverage = true, .time_precision = verilator_time_unit::ns};
}  // namespace

TEST_SUITE("DigitalPhaseMeterTop")
{
    TEST_CASE("reset keeps the AXI slave idle and releases registers to their defaults")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.reset");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            port.awaddr = 0;
            port.awprot = 0;
            port.awvalid = 0;
            port.wdata = 0;
            port.wstrb = 0;
            port.wvalid = 0;
            port.bready = 1;
            port.araddr = 0;
            port.arprot = 0;
            port.arvalid = 0;
            port.rready = 1;
            port.src_locked = 0;
            port.resetn = 0;

            // ---- 1. 复位期间：无中断，且 AXI 从口不接受任何传输 ----
            // 从设备在复位期间必须拉低所有 ready：若此时接受请求却无法给出响应，主机会永久挂起
            for(auto cycle{0zu}; cycle != 8zu; ++cycle) { co_await wait_verify(port.clock); }
            CHECK_EQ(port.interrupt, 0u);
            CHECK_EQ(port.awready, 0u);
            CHECK_EQ(port.wready, 0u);
            CHECK_EQ(port.arready, 0u);
            CHECK_EQ(port.bvalid, 0u);
            CHECK_EQ(port.rvalid, 0u);

            // 复位期间持续发请求，从口仍不得握手
            port.arvalid = 1;
            port.awvalid = 1;
            co_await port.stimulate(4zu);
            CHECK_EQ(port.arready, 0u);
            CHECK_EQ(port.awready, 0u);
            co_await port.stimulate();
            port.arvalid = 0;
            port.awvalid = 0;

            // ---- 2. 复位释放：寄存器为默认值 ----
            co_await port.stimulate();
            port.resetn = 1;
            co_await port.stimulate(8zu);
            CHECK_EQ(port.arready, 1u);

            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, false, false, false, false);
            value = co_await axi_read(port, reg_version);
            CHECK_EQ(value, version_value);
            value = co_await axi_read(port, reg_caps);
            CHECK_EQ(value, caps_value);
            value = co_await axi_read(port, reg_gate_target);
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_lost_count);
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_irq_status);
            check_irq_status(value, false, false, false);
            value = co_await axi_read(port, reg_ctrl);
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_sig_ctrl);
            CHECK_EQ(value, 0u);
            // 默认选外部输入，因此 sourceReady 为 1（外部通路在配置上总是可用的）；此时 MMCM 未锁定
            value = co_await axi_read(port, reg_sig_status);
            check_sig_status(value, false, false, false, true);

            const auto record{co_await read_record(port)};
            CHECK_EQ(record.gate_cnt, 0u);
            CHECK_EQ(record.edge_cnt, 0u);
            CHECK_EQ(record.phase_cnt, 0u);
            CHECK_EQ(record.seq, 0u);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("GATE_TARGET accepts write-data-first ordering and byte strobes")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.regs");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // 写数据通道先到（AXI 允许的另一种顺序）：从口必须各自锁存，两个通道都到齐后生效
            co_await axi_write_data_first(port, reg_gate_target, gate_slow);
            std::uint32_t value{co_await axi_read(port, reg_gate_target)};
            CHECK_EQ(value, gate_slow);

            // 字节选通只改写被 wstrb 选中的字节
            co_await axi_write(port, reg_gate_target, 0u);
            co_await axi_write(port, reg_gate_target, 0x1122'3344u, 0x1u);
            value = co_await axi_read(port, reg_gate_target);
            CHECK_EQ(value, 0x44u);
            co_await axi_write(port, reg_gate_target, 0x5566'7788u, 0x3u);
            value = co_await axi_read(port, reg_gate_target);
            CHECK_EQ(value, 0x7788u);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("a measurement raises busy, sets the interrupt and latches a valid record")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.start");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // CTRL 的 irqEnable 是普通读写位
            co_await axi_write(port, reg_ctrl, bit_mask(ctrl_irq_enable));
            std::uint32_t value{co_await axi_read(port, reg_ctrl)};
            CHECK_EQ(value, bit_mask(ctrl_irq_enable));

            // 启动一次长闸门测量：busy 置起，完成前 resultValid / 中断都不应出现
            co_await start_measurement(port, gate_slow, true);
            co_await port.stimulate(2zu);
            value = co_await axi_read(port, reg_status);
            check_status(value, true, false, false, false);
            CHECK_EQ(port.interrupt, 0u);

            CHECK_MESSAGE(co_await wait_interrupt(port, 4u * gate_slow + 200u), "测量完成中断超时");
            CHECK_EQ(port.interrupt, 1u);

            value = co_await axi_read(port, reg_status);
            check_status(value, false, true, false, true);
            value = co_await axi_read(port, reg_lost_count);
            CHECK_EQ(value, 0u);

            const auto record{co_await read_record(port)};
            check_record(record, expect(gate_slow, 0u));

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("clearing the interrupt keeps resultValid and writes to read-only registers are ignored")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.irq_clear");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);
            CHECK_MESSAGE(co_await run_measurement(port, gate_slow), "测量完成中断超时");
            const auto snapshot{co_await read_record(port)};

            // 只清中断挂起位：结果仍然有效（resultValid 保持），结果寄存器不被改动
            co_await clear_irq_pending(port);
            CHECK_EQ(port.interrupt, 0u);
            std::uint32_t value{co_await axi_read(port, reg_irq_status)};
            check_irq_status(value, false, true, false);
            value = co_await axi_read(port, reg_status);
            check_status(value, false, true, false, false);
            const auto kept{co_await read_record(port)};
            CHECK_EQ(kept.gate_cnt, snapshot.gate_cnt);
            CHECK_EQ(kept.edge_cnt, snapshot.edge_cnt);
            CHECK_EQ(kept.phase_cnt, snapshot.phase_cnt);
            CHECK_EQ(kept.seq, snapshot.seq);

            // 只读寄存器写忽略
            co_await axi_write(port, reg_result_gate, 0xDEAD'BEEFu);
            co_await axi_write(port, reg_version, 0xDEAD'BEEFu);
            value = co_await axi_read(port, reg_result_gate);
            CHECK_EQ(value, snapshot.gate_cnt);
            value = co_await axi_read(port, reg_version);
            CHECK_EQ(value, version_value);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("a second measurement with a shorter gate updates the result registers")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.second");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            CHECK_MESSAGE(co_await run_measurement(port, gate_slow), "第一次测量的中断超时");
            auto record{co_await read_record(port)};
            check_record(record, expect(gate_slow, 0u));

            // 读走结果（同时清中断挂起与 resultValid），STATUS 回到全 0
            co_await acknowledge_result(port);
            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, false, false, false, false);

            // 第二次测量：更短的闸门时间，结果寄存器被更新、序号自增
            CHECK_MESSAGE(co_await run_measurement(port, gate_fast), "第二次测量的中断超时");
            record = co_await read_record(port);
            check_record(record, expect(gate_fast, 1u));
            co_await acknowledge_result(port);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("only the latest result is kept and overwrites increment LOST_COUNT")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.lost");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // 连续 3 次测量都不读走结果：前两条被覆盖，LOST_COUNT 累加到 2
            CHECK_MESSAGE(co_await run_measurement(port, gate_a), "测量 A 的中断超时");
            CHECK_MESSAGE(co_await run_measurement(port, gate_b), "测量 B 的中断超时");
            CHECK_MESSAGE(co_await run_measurement(port, gate_c), "测量 C 的中断超时");

            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, false, true, true, true);
            value = co_await axi_read(port, reg_lost_count);
            CHECK_EQ(value, 2u);

            // 留下的只有最后一条（序号 2、闸门目标是 C 的 1400）
            const auto record{co_await read_record(port)};
            check_record(record, expect(gate_c, 2u));

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("CTRL.clear resets the result area and restarts the sequence")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.clear");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // 先留下一条未读结果 + 一次覆盖（LOST_COUNT=1），再写 CTRL.clear
            CHECK_MESSAGE(co_await run_measurement(port, gate_slow), "第一次测量的中断超时");
            CHECK_MESSAGE(co_await run_measurement(port, gate_slow), "第二次测量的中断超时");

            co_await axi_write(port, reg_ctrl, bit_mask(ctrl_irq_enable) | bit_mask(ctrl_clear));
            co_await port.stimulate(2zu);
            CHECK_EQ(port.interrupt, 0u);

            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, false, false, false, false);
            value = co_await axi_read(port, reg_lost_count);
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_irq_status);
            check_irq_status(value, false, false, false);

            const auto record{co_await read_record(port)};
            CHECK_EQ(record.gate_cnt, 0u);
            CHECK_EQ(record.edge_cnt, 0u);
            CHECK_EQ(record.phase_cnt, 0u);
            CHECK_EQ(record.seq, 0u);

            // 序号重新计数：clear 之后的第一条结果序号为 0
            CHECK_MESSAGE(co_await run_measurement(port, gate_fast), "clear 后测量的中断超时");
            const auto after_clear{co_await read_record(port)};
            check_record(after_clear, expect(gate_fast, 0u));
            co_await acknowledge_result(port);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("the interrupt pin is gated by irqEnable while the pending flag stays set")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.irq_gate");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // 不使能中断时测量完成：引脚不拉高，但挂起位仍然置位
            co_await start_measurement(port, gate_fast, false);
            co_await port.stimulate(2zu);
            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, true, false, false, false);
            CHECK_MESSAGE(co_await wait_not_busy(port, 400u), "测量结束超时");
            CHECK_EQ(port.interrupt, 0u);
            value = co_await axi_read(port, reg_status);
            check_status(value, false, true, false, true);
            value = co_await axi_read(port, reg_result_seq);
            CHECK_EQ(value, 0u);

            // 中断是电平：使能之后挂起的中断立刻出现在引脚上
            co_await axi_write(port, reg_ctrl, bit_mask(ctrl_irq_enable));
            CHECK_EQ(port.interrupt, 1u);
            co_await acknowledge_result(port);
            CHECK_EQ(port.interrupt, 0u);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("the gate target is latched when the measurement starts")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.gate_latch");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // Controller 在启动的那一拍把 GATE_TARGET 锁存进自己的 targetGate 寄存器，
            // 测量期间改写寄存器不影响本次测量
            co_await clear_irq_pending(port);
            co_await start_measurement(port, gate_slow, true);
            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, true, false, false, false);
            co_await axi_write(port, reg_gate_target, 8u);
            value = co_await axi_read(port, reg_gate_target);
            CHECK_EQ(value, 8u);

            CHECK_MESSAGE(co_await wait_interrupt(port, 4u * gate_slow + 200u), "测量完成中断超时");
            const auto record{co_await read_record(port)};
            check_record(record, expect(gate_slow, 0u));
            // 本次测量确实按启动时锁存的大目标值完成，而不是按测量中写入的小值
            CHECK_GT(record.gate_cnt, 8u);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("a start request while busy is ignored")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.double_start");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // busy 期间重复写 start 被忽略：只产生一条结果、不累加 LOST_COUNT
            co_await clear_irq_pending(port);
            co_await start_measurement(port, gate_slow, true);
            co_await axi_write(port, reg_gate_target, gate_fast);
            co_await start_measurement(port, gate_fast, true);

            CHECK_MESSAGE(co_await wait_interrupt(port, 4u * gate_slow + 200u), "测量完成中断超时");
            std::uint32_t value{co_await axi_read(port, reg_result_seq)};
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_lost_count);
            CHECK_EQ(value, 0u);

            const auto record{co_await read_record(port)};
            check_record(record, expect(gate_slow, 0u));

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("the internal signal source can be selected and switched back to the external input")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.signal_source");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // MMCM 锁定指示异步于主时钟，先拉高再等几个周期让 CdcLevel 同步
            port.src_locked = 1;
            co_await port.stimulate(8zu);
            std::uint32_t value{co_await axi_read(port, reg_sig_status)};
            check_sig_status(value, false, false, true, true);  // 已锁定、仍选外部输入

            // 使能片内源并切换到片内源
            co_await axi_write(port, reg_sig_ctrl, bit_mask(sig_enable) | bit_mask(sig_select));
            value = co_await axi_read(port, reg_sig_ctrl);
            CHECK_EQ(value, bit_mask(sig_enable) | bit_mask(sig_select));
            value = co_await axi_read(port, reg_sig_status);
            check_sig_status(value, true, true, true, true);  // 四个状态位全 1

            // 片内源（640ns / 160ns）与外部输入（400ns / 120ns）的理论值不同，
            // 因此两次测量能区分「切换生效」与「切换没生效」
            CHECK_MESSAGE(co_await run_measurement(port, gate_slow), "片内源测量的中断超时");
            auto record{co_await read_record(port)};
            check_record(record, expect(gate_slow, 0u, internal_period, internal_offset));
            co_await acknowledge_result(port);

            // 切回外部输入：同一寄存器里的选择位清 0，外部引脚上的信号重新成为被测对象
            co_await axi_write(port, reg_sig_ctrl, 0u);
            value = co_await axi_read(port, reg_sig_ctrl);
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_sig_status);
            check_sig_status(value, false, false, true, true);

            CHECK_MESSAGE(co_await run_measurement(port, gate_slow), "外部输入测量的中断超时");
            record = co_await read_record(port);
            check_record(record, expect(gate_slow, 1u));
            co_await acknowledge_result(port);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("CTRL.clear while busy resets the result area and the in-flight measurement completes")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.busy_clear");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // 先留下一条未读结果（resultValid=1、序号 0），再启动一次长闸门的新测量
            CHECK_MESSAGE(co_await run_measurement(port, gate_fast), "第一次测量的中断超时");
            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, false, true, false, true);
            value = co_await axi_read(port, reg_result_seq);
            CHECK_EQ(value, 0u);

            // 只清中断挂起位（不动 resultValid），保证后面的 wait_interrupt 等的是本次测量完成
            co_await clear_irq_pending(port);
            co_await start_measurement(port, gate_slow, true);
            co_await port.stimulate(2zu);
            value = co_await axi_read(port, reg_status);
            check_status(value, true, true, false, false);

            // busy 期间写 CTRL.clear：当拍生效，只清结果区与标志，busy 保持为 1
            co_await axi_write(port, reg_ctrl, bit_mask(ctrl_irq_enable) | bit_mask(ctrl_clear));
            value = co_await axi_read(port, reg_status);
            check_status(value, true, false, false, false);
            value = co_await axi_read(port, reg_result_seq);
            CHECK_EQ(value, 0u);
            const auto cleared{co_await read_record(port)};
            CHECK_EQ(cleared.gate_cnt, 0u);
            CHECK_EQ(cleared.edge_cnt, 0u);
            CHECK_EQ(cleared.phase_cnt, 0u);
            CHECK_EQ(cleared.seq, 0u);
            CHECK_EQ(port.interrupt, 0u);

            // 在途测量继续跑完：中断照常出现、结果照常锁存，序号从 0 重新开始
            CHECK_MESSAGE(co_await wait_interrupt(port, 4u * gate_slow + 200u), "在途测量的中断超时");
            CHECK_EQ(port.interrupt, 1u);
            value = co_await axi_read(port, reg_status);
            check_status(value, false, true, false, true);
            value = co_await axi_read(port, reg_lost_count);
            CHECK_EQ(value, 0u);
            const auto record{co_await read_record(port)};
            check_record(record, expect(gate_slow, 0u));
            co_await acknowledge_result(port);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }

    TEST_CASE("a second reset returns status and configuration to their defaults")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DigitalPhaseMeterTop.second_reset");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_clock(port.ext_sig1, external_period));
        ctx.add_task(generate_clock(port.ext_sig2, external_period, external_offset));
        ctx.add_task(generate_clock(port.int_sig1, internal_period));
        ctx.add_task(generate_clock(port.int_sig2, internal_period, internal_offset));

        ctx.add_task([&] -> task<void> {
            co_await init_dut(port);

            // 先让 DUT 处于「有结果、片内源已选中」的非默认状态
            CHECK_MESSAGE(co_await run_measurement(port, gate_fast), "测量的中断超时");
            co_await axi_write(port, reg_sig_ctrl, bit_mask(sig_enable) | bit_mask(sig_select));

            // 复位再次生效：中断清除、状态归零、AXI 从口重新不接受传输
            co_await port.stimulate();
            port.resetn = 0;
            port.src_locked = 0;
            co_await port.stimulate(4zu);
            CHECK_EQ(port.interrupt, 0u);
            CHECK_EQ(port.awready, 0u);
            CHECK_EQ(port.arready, 0u);

            co_await port.stimulate();
            port.resetn = 1;
            co_await port.stimulate(8zu);

            std::uint32_t value{co_await axi_read(port, reg_status)};
            check_status(value, false, false, false, false);
            value = co_await axi_read(port, reg_gate_target);
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_result_seq);
            CHECK_EQ(value, 0u);
            value = co_await axi_read(port, reg_version);
            CHECK_EQ(value, version_value);
            value = co_await axi_read(port, reg_sig_ctrl);
            CHECK_EQ(value, 0u);
            // 复位同样把 MMCM 的锁定指示拉低（测试模型：复位期间 locked = 0）
            value = co_await axi_read(port, reg_sig_status);
            check_sig_status(value, false, false, false, true);

            co_await port.stimulate(2zu);
            co_await eval_finish();
        }());

        ctx.loop_until_finish(20_ms);
        MESSAGE(ctx.stats());
    }
}
