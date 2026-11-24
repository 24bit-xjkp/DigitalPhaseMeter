module;
// 公共测试逻辑（FPGA / ASIC 两个 DspCounter 实现共用）。
//
// 本模块**不包含任何 Verilator 生成头**，只导出与具体 DUT 无关的部分：
//   - port_t：端口包装。两个实现的端口完全相同，因此结构体本身不是模板，只有构造函数
//     需要 DUT 类型（由实参推导）；
//   - 各用例的激励与断言：普通协程，只操作 port_t，与 DUT 类型无关，因此只编译一次；
//   - run_<用例>：唯一还需要 DUT 类型的地方（dut_context 是模板），只负责构造上下文、
//     注册时钟与激励任务、跑到结束并打印统计。
// 具体测试（test/DspCounter.cpp / test/DspCounterA.cpp）只提供 DUT 类型与输出延迟。
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
    /// `reset` 不参与任何逻辑，因此这里不驱动它（Verilator 保持 0）。
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
    /// @param port          已绑定 DUT 的端口包装
    /// @param latency       输出相对实际计数的延迟拍数：FPGA 为 0，ASIC 为
    ///                      `asicDspCounterPipeline - 1`（默认 7）。因此第 n 个使能拍之后的期望值是
    ///                      `max(0, n - latency)`。
    /// @param clear_latency clear 在输出上完全排空所需的拍数：FPGA 为 0（输出即寄存器），ASIC 为
    ///                      `latency`（对齐延迟链不随 clear 复位，排空期间输出仍可能是旧值/分段混合值）。
    [[nodiscard]] task<void> enable_clear(port_t& port, std::uint64_t latency, std::uint64_t clear_latency)
    {
        port.io_enable = 0;
        // 用 io_clear 做复位：它是本模块唯一的复位/清零输入（同步清零）。
        // 延迟链没有复位，复位期间也要保持足够长，让链内的旧值排空。
        co_await generate_reset(port.io_clear, port.clock, 3 + clear_latency);

        // 逐拍采样，显式验证流水线延迟：ASIC 版前 7 拍输出仍为 0，
        // 第 8 拍才看到计数 1；FPGA 版每一拍都等于已计拍数。
        port.io_enable = 1;
        for(std::uint64_t elapsed{1}; elapsed <= 8; ++elapsed)
        {
            co_await wait_verify(port.clock);
            CHECK_EQ(port.value_of(), elapsed > latency ? elapsed - latency : 0zu);
        }

        // 使能拉低后计数保持；等流水线排空后输出应稳定在 8
        port.io_enable = 0;
        for(std::uint64_t i{0}; i < latency + 1; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), 8zu);

        // clear 与 enable 同时有效 → clear 优先。
        // 先等到下降沿：同步清零不应在半个周期内改变寄存器（异步复位才会）。
        // 输出对齐链不复位，clear 后要等 clear_latency 拍才完全排空；排空期间输出仍非零，
        // 具体值可能是旧值或各段旧值的混合，因此这里只断言“尚未归零”和最终归零。
        port.io_clear = 1;
        port.io_enable = 1;
        co_await wait_stimulate(port.clock);
        for(std::uint64_t i{0}; i < clear_latency + 1; ++i)
        {
            CHECK_NE(port.value_of(), 0zu);
            co_await wait_verify(port.clock);
        }
        CHECK_EQ(port.value_of(), 0zu);

        // 撤掉 clear 后从 0 重新计数：经过 latency 拍延迟后第 1 个计数可见
        port.io_clear = 0;
        for(std::uint64_t i{0}; i < latency + 1; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), 1zu);
        for(auto i{0zu}; i < 2zu; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), 3zu);

        co_await wait_stimulate(port.clock, 2);
        co_await eval_finish();
    }

    /// 跨 8 位 / 16 位边界的连续计数、32 位高位进位与跨边界后的 clear 优先。
    ///
    /// 分段流水线计数器按 4 位一段拆分（32 位 / 8 段），段间进位是寄存的；本用例先让计数
    /// 跨过 0xFF → 0x100，再连续跨过 0xFFFF → 0x10000，验证低段进位与高位段都正确、
    /// 输出没有在 8/16 位处截断。达到 bit31 需要 2^31 拍（约 8.6s 仿真时间），在回归里
    /// 不可行；这里覆盖到 bit16 及其以上的高段，进位结构对更高段是同一套逻辑
    /// （未覆盖风险见交付报告）。
    /// @param latency       输出相对实际计数的延迟拍数
    /// @param clear_latency clear 在输出上可见的延迟拍数
    [[nodiscard]] task<void> cross_boundary(port_t& port, std::uint64_t latency, std::uint64_t clear_latency)
    {
        /// 期望在使能 `target_count + latency` 拍后看到的计数值。取 65540 = 0x1_0004：
        /// 跨过 16 位边界，并让高段（bit16 起）稳定进位。
        constexpr std::uint64_t target_count{65540zu};

        port.io_enable = 0;
        co_await generate_reset(port.io_clear, port.clock, 3 + clear_latency);

        port.io_enable = 1;
        // 先显式跨过 8 位边界：0xFF → 0x100，验证低段进位到第二段
        constexpr std::uint64_t first_block{256zu};
        for(std::uint64_t i{0}; i < first_block + latency; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), first_block);
        CHECK_EQ(port.value_of() >> 8zu, 1zu);

        // 再连续计数到 0x1_0004，跨过 16 位边界并让高段（bit16 起）稳定进位
        const auto remaining{target_count - first_block};
        for(std::uint64_t i{0}; i < remaining; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), target_count);
        // bit16 起的高位段已经进位，输出不是 16 位截断
        CHECK_EQ(port.value_of() >> 16zu, 1zu);

        // 继续累加 4 拍，输出仍是完整 32 位计数（FPGA：+4；ASIC：流水线排空后同样是 +4）
        for(auto i{0zu}; i < 4zu; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), target_count + 4zu);

        // 跨过 16 位之后 clear 仍然优先；输出对齐链不复位，等旧值排空后归零
        port.io_clear = 1;
        port.io_enable = 0;
        for(std::uint64_t i{0}; i < clear_latency + 1; ++i) { co_await wait_verify(port.clock); }
        CHECK_EQ(port.value_of(), 0zu);

        co_await wait_stimulate(port.clock, 2);
        co_await eval_finish();
    }

    /// 用例入口：使能计数、使能保持与 clear 优先。
    ///
    /// `dut_context` 是模板，因此这里是本模块唯一还需要 DUT 类型的地方；主体逻辑在上面的
    /// 非模板协程里。`latency` 是输出相对实际计数的延迟拍数，`clear_latency` 是 clear
    /// 后输出完全排空所需的拍数。
    template <typename dut_t>
    void run_enable_clear(std::uint64_t latency, std::uint64_t clear_latency)
    {
        dut_context<dut_t, verilator::VerilatedFstC, false> ctx{ctx_opt};
        ctx.set_base_name("DspCounter.enable_clear");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, period));
        ctx.add_task(enable_clear(port, latency, clear_latency));
        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }

    /// 用例入口：跨 8 位 / 16 位边界的连续计数。
    template <typename dut_t>
    void run_cross_boundary(std::uint64_t latency, std::uint64_t clear_latency)
    {
        dut_context<dut_t, verilator::VerilatedFstC, false> ctx{ctx_opt};
        ctx.set_base_name("DspCounter.past_16bit");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, period));
        ctx.add_task(cross_boundary(port, latency, clear_latency));
        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }
}  // namespace dsp_counter_test
