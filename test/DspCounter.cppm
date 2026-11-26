module;
// 公共测试逻辑（FPGA / ASIC 两个 DspCounter 实现共用）。
//
// 本模块**不包含任何 Verilator 生成头**，只导出与具体 DUT 无关的部分：
//   - port_t：端口包装。两个实现的端口完全相同，因此结构体本身不是模板，只有构造函数
//     需要 DUT 类型（由实参推导）；
//   - 各用例的激励与断言：普通协程，只操作 port_t，与 DUT 类型无关，因此只编译一次；
//   - run_<用例>：唯一还需要 DUT 类型的地方（dut_context 是模板），只负责构造上下文、
//     注册时钟与激励任务、跑到结束并打印统计。
// 具体测试（test/DspCounter.cpp / test/DspCounterA.cpp）只提供 DUT 类型与平台差异参数。
//
// 两个实现的输出语义不同：
//   - FPGA：单级 DSP 累加器，io_value 就是当前计数值，使能/清零同拍可见；
//   - ASIC：`asicDspCounterPipeline` 段流水线，io_value 是各段的**原始**寄存器（已去掉输出
//     对齐延迟链）。段间进位是寄存的，计数刚跨过段边界时高段要晚一拍才收到进位，所以撤销
//     使能后要等 `asicDspCounterPipeline - 1` 拍让进位传播完，输出才是最终计数值；
//     PhaseMeter 也是按同样的延迟读取。ASIC 的 clear 走异步复位端，清零同拍生效。
//
// doctest 断言宏在这里展开（模块内部使用），因此公共模块的消费者仍需 import
// verilator_utils.full 以获得 doctest 实体。
#include <cstdint>
#include <doctest_macros.hpp>
export module DspCounterCommon;
import verilator_utils.full;

// 模块内部使用：下面不再写 verilator_utils:: 前缀（using 指令不导出，不影响消费者）。
using namespace verilator_utils;

export namespace dsp_counter_test
{
    /// DSP 域测试时钟周期。时间精度为 ns，周期必须取整数纳秒的偶数，
    /// 半周期才不会被整数除法截断（1_ns 的半周期会被截成 0，时钟根本不翻转）。
    constexpr inline auto period{4_ns};

    /// 上下文选项：时间精度取 ns，所有等待时长都是整数纳秒。
    constexpr inline dut_context_option ctx_opt{.time_precision = verilator_time_unit::ns};

    /// DUT 端口包装。两个实现只有 clock / io_clear / io_enable / io_value 四个功能端口；
    /// 生成 SV 里虽然声明了隐式 `reset`，但 DspCounter 的寄存器全部由 `io_clear` 复位，
    /// `reset` 不参与任何逻辑，因此这里不驱动它（Verilator 保持 0）。FPGA 版的 `io_clear`
    /// 是同步清零，ASIC 版接在计数寄存器的异步复位端（`dfrtp` 的 RESET_B）。
    ///
    /// 字段与 DUT 类型无关，所以这里不是模板；只有构造函数需要具体 DUT 类型。
    struct port_t
    {
        bit_slice<verilator::CData> clock;
        bit_slice<verilator::CData> io_clear;
        bit_slice<verilator::CData> io_enable;
        vector_slice<verilator::IData> io_value;

        template <typename dut_t>
        explicit port_t(dut_t& dut) :
            clock{dut.clock}, io_clear{dut.io_clear, boolean}, io_enable{dut.io_enable, boolean},
            io_value{dut.io_value, 32zu, dec_unsigned}
        {
        }

        [[nodiscard]] std::uint64_t value_of() const { return static_cast<std::uint64_t>(io_value); }
    };

    /// 使能计数、使能保持与 clear 优先。
    ///
    /// @param port        已绑定 DUT 的端口包装
    /// @param settle      撤销使能后，输出稳定到最终计数值所需的拍数：FPGA 为 0（输出即寄存器），
    ///                    ASIC 为 `asicDspCounterPipeline - 1`（等段间进位传播完）。
    /// @param clear_edges clear 置起后到输出归零经过的时钟边沿数：ASIC 为 0（异步复位，同拍清零），
    ///                    FPGA 为 1（同步清零，下一个上升沿生效）。
    /// @param block_mask  最低段的位掩码：单级累加器（FPGA）为全 1，ASIC 分段流水线为
    ///                    `(1 << ceil(cntWidth / depth)) - 1`。计数过程中只有最低段逐拍准确，
    ///                    更高段要等进位逐级传来，所以计数期间的逐拍断言只比较这一段。
    [[nodiscard]] task<void> enable_clear(port_t& port, std::uint64_t settle, std::uint64_t clear_edges, std::uint64_t block_mask)
    {
        port.io_enable = 0;
        // 用 io_clear 做复位：它是本模块唯一的复位/清零输入。
        co_await generate_reset(port.io_clear, port.clock, 3 + clear_edges + settle);

        // 逐拍采样：最低段每个使能拍都加 1，所以它始终等于已计拍数。分段流水线的高段要等
        // 进位逐级传来（最多 depth - 1 拍），计数期间拼出的整数值会偏低，等到撤销使能后的
        // settle 拍再检查完整值。
        port.io_enable = 1;
        for(std::uint64_t elapsed{1}; elapsed <= 8; ++elapsed)
        {
            co_await wait_verify(port.clock);
            CHECK_EQ(port.value_of() & block_mask, elapsed & block_mask);
        }

        // 使能拉低后计数保持；ASIC 要等段间进位传播完再读
        port.io_enable = 0;
        for(std::uint64_t i{0}; i < settle + 1; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), 8zu);

        // clear 与 enable 同时有效 → clear 优先。
        // 异步复位的实现在下降沿重新评估后就已经清零；同步清零的实现在下一个上升沿才清零。
        port.io_clear = 1;
        port.io_enable = 1;
        if(clear_edges == 0zu)
        {
            co_await wait_verify(port.clock, 1, edge_enum::falling);
            CHECK_EQ(port.value_of(), 0zu);
        }
        else
        {
            co_await wait_stimulate(port.clock);
            for(std::uint64_t i{0}; i < clear_edges; ++i)
            {
                CHECK_NE(port.value_of(), 0zu);
                co_await wait_verify(port.clock);
            }
            CHECK_EQ(port.value_of(), 0zu);
        }
        // clear 有效期间即使 enable=1 也保持 0
        for(auto i{0zu}; i < 2zu; ++i)
        {
            co_await wait_verify(port.clock);
            CHECK_EQ(port.value_of(), 0zu);
        }

        // 撤掉 clear 后从 0 重新计数：下一个使能拍就能看到 1（没有输出延迟链）
        port.io_clear = 0;
        co_await wait_verify(port.clock);
        CHECK_EQ(port.value_of(), 1zu);
        for(auto i{0zu}; i < 2zu; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), 3zu);

        co_await wait_stimulate(port.clock, 2);
        co_await eval_finish();
    }

    /// 跨 8 位 / 16 位边界的连续计数、32 位高位进位与跨边界后的 clear 优先。
    ///
    /// 分段流水线计数器按 `asicDspCounterPipeline` 段拆分（当前 16 段、每段 2 位），段间进位
    /// 是寄存的；本用例先让计数跨过 0xFF → 0x100，再连续跨过 0xFFFF → 0x10000，验证低段进位
    /// 与高位段都正确、输出没有在 8/16 位处截断。每次跨段后先撤销使能并等 `settle` 拍，
    /// 让进位传播完再读，
    /// 这是 ASIC 原始分段计数器的正确读法（PhaseMeter 也按同样的延迟读取）。
    /// 达到 bit31 需要 2^31 拍（约 8.6s 仿真时间），在回归里不可行；这里覆盖到 bit16 及其
    /// 以上的高段，进位结构对更高段是同一套逻辑（未覆盖风险见交付报告）。
    /// @param settle      撤销使能后输出稳定所需的拍数
    /// @param clear_edges clear 置起后输出归零经过的边沿数
    [[nodiscard]] task<void> cross_boundary(port_t& port, std::uint64_t settle, std::uint64_t clear_edges)
    {
        /// 目标计数值：65540 = 0x1_0004，跨过 16 位边界并让高段稳定进位。
        constexpr std::uint64_t target_count{65540zu};
        /// 第一段显式跨过 8 位边界的计数。
        constexpr std::uint64_t first_block{256zu};

        port.io_enable = 0;
        co_await generate_reset(port.io_clear, port.clock, 3 + clear_edges + settle);

        // 先显式跨过 8 位边界：0xFF → 0x100，验证低段进位到第二段
        port.io_enable = 1;
        for(std::uint64_t i{0}; i < first_block; ++i) { co_await wait_verify(port.clock); }
        port.io_enable = 0;
        for(std::uint64_t i{0}; i < settle; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), first_block);
        CHECK_EQ(port.value_of() >> 8zu, 1zu);

        // 再连续计数到 0x1_0004，跨过 16 位边界并让高段（bit16 起）稳定进位
        port.io_enable = 1;
        for(std::uint64_t i{0}; i < target_count - first_block; ++i) { co_await wait_verify(port.clock); }
        port.io_enable = 0;
        for(std::uint64_t i{0}; i < settle; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), target_count);
        // bit16 起的高位段已经进位，输出不是 16 位截断
        CHECK_EQ(port.value_of() >> 16zu, 1zu);

        // 继续累加 4 拍，输出仍是完整 32 位计数
        port.io_enable = 1;
        for(auto i{0zu}; i < 4zu; ++i) { co_await wait_verify(port.clock); }
        port.io_enable = 0;
        for(std::uint64_t i{0}; i < settle; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), target_count + 4zu);

        // 跨过 16 位之后 clear 仍然优先
        port.io_clear = 1;
        if(clear_edges == 0zu)
        {
            co_await wait_verify(port.clock, 1, edge_enum::falling);
            CHECK_EQ(port.value_of(), 0zu);
        }
        else
        {
            co_await wait_stimulate(port.clock);
            for(std::uint64_t i{0}; i < clear_edges; ++i) { co_await wait_verify(port.clock); }
            CHECK_EQ(port.value_of(), 0zu);
        }

        co_await wait_stimulate(port.clock, 2);
        co_await eval_finish();
    }

    /// enable 在进位拍之后立即撤销：进位必须继续传播到高段。
    ///
    /// 0x0F 再使能一拍得到 0x10，同时产生 block0→block1 的进位；如果每段都把
    /// `carryIn & io.enable` 当加数，下一拍 enable 已经为 0，这个进位会被丢掉，
    /// 输出最终停在 0 而不是 16。ASIC 分段流水线必须让进位寄存器跨段无条件传播，
    /// 撤销使能后等 `settle` 拍读取最终值。
    [[nodiscard]] task<void> carry_after_disable(port_t& port, std::uint64_t settle)
    {
        port.io_enable = 0;
        co_await generate_reset(port.io_clear, port.clock, 3 + settle);

        port.io_enable = 1;
        // 16 个使能拍：0 → 16，最后一拍产生 block0→block1 进位
        for(std::uint64_t i{0}; i < 16; ++i) { co_await wait_verify(port.clock); }

        // 进位还在流水线里时撤销使能，等它传播到高段后输出应是 16 而不是 0
        port.io_enable = 0;
        for(std::uint64_t i{0}; i < settle; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), 16zu);

        co_await wait_stimulate(port.clock, 2);
        co_await eval_finish();
    }

    /// 用例入口：使能计数、使能保持与 clear 优先。
    ///
    /// `dut_context` 是模板，因此这里是本模块唯一还需要 DUT 类型的地方；主体逻辑在上面的
    /// 非模板协程里。`settle` 是撤销使能后输出稳定所需的拍数，`clear_edges` 是 clear
    /// 置起后输出归零经过的边沿数，`block_mask` 是最低段的位掩码。
    template <typename dut_t>
    void run_enable_clear(std::uint64_t settle, std::uint64_t clear_edges, std::uint64_t block_mask)
    {
        dut_context<dut_t, verilator::VerilatedFstC, false> ctx{ctx_opt};
        ctx.set_base_name("DspCounter.enable_clear");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, period));
        ctx.add_task(enable_clear(port, settle, clear_edges, block_mask));
        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }

    /// 用例入口：进位传播期间撤销使能。
    template <typename dut_t>
    void run_carry_after_disable(std::uint64_t settle)
    {
        dut_context<dut_t, verilator::VerilatedFstC, false> ctx{ctx_opt};
        ctx.set_base_name("DspCounter.carry_after_disable");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, period));
        ctx.add_task(carry_after_disable(port, settle));
        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }

    /// 用例入口：跨 8 位 / 16 位边界的连续计数。
    template <typename dut_t>
    void run_cross_boundary(std::uint64_t settle, std::uint64_t clear_edges)
    {
        dut_context<dut_t, verilator::VerilatedFstC, false> ctx{ctx_opt};
        ctx.set_base_name("DspCounter.past_16bit");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, period));
        ctx.add_task(cross_boundary(port, settle, clear_edges));
        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }
}  // namespace dsp_counter_test
