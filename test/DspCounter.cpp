#include <cstdint>

#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
#include <DspCounterVerilator.h>
#include <verilator_bwd.hpp>

// DspCounter 是 450MHz 测量域里的 32 位使能计数器（DSP48E1 累加器实现，见
// src/main/scala/DigitalPhaseMeter/DspCounter.scala）。它在 PhaseMeter 里分别累计
// edgeCnt 与 phaseCnt，因此要验证三件事：使能计数、清零优先、以及计数超过 16 位后仍然正确
// （实现上把 `cnt + enable` 交给 DSP，位宽行为靠断言与观测确认）。
// Controller 的测量用例已经覆盖「计数器在真实测量通路里的行为」，这里补的是模块级语义。
namespace
{
    using namespace verilator_utils;
    using dut_t = DspCounterVerilator;
    using dut_context_t = dut_context<dut_t, VerilatedFstC>;

    /// 时钟周期。测试取 DSP 域同款 4ns（250MHz）：时间精度为 ns，周期必须取整数纳秒的偶数，
    /// 半周期才不会被截断（1_ns 的半周期会被截成 0，时钟根本不翻转）。
    constexpr auto period{4_ns};

    struct port_t
    {
        bit_slice<CData> clock;
        bit_slice<CData> reset;
        bit_slice<CData> io_clear;
        bit_slice<CData> io_enable;
        vector_slice<IData> io_value;

        explicit port_t(dut_t& dut) :
            clock{dut.clock}, reset{dut.reset}, io_clear{dut.io_clear, boolean}, io_enable{dut.io_enable, boolean},
            io_value{dut.io_value, 32zu, dec_unsigned}
        {
        }

        [[nodiscard]] std::uint64_t value_of() const { return static_cast<std::uint64_t>(io_value); }
    };

    constexpr dut_context_option ctx_opt{.coverage = true, .time_precision = verilator_time_unit::ns};
}  // namespace

TEST_SUITE("DspCounter")
{
    TEST_CASE("enable counts, clear dominates enable")
    {
        dut_context_t ctx{ctx_opt};
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, period));

        const auto stimulate{
            [&] -> task<void> {
                port.io_clear = 0;
                port.io_enable = 0;
                co_await generate_reset(port.reset, port.clock);

                // enable 保持 4 拍 → 计到 4
                port.io_enable = 1;
                for(auto i{0zu}; i < 4zu; ++i) { co_await wait_verify(port.clock); }
                CHECK_EQ(port.value_of(), 4zu);

                // enable 拉低 → 保持
                port.io_enable = 0;
                co_await wait_verify(port.clock);
                CHECK_EQ(port.value_of(), 4zu);

                // clear 与 enable 同时有效 → clear 优先，计数归零
                port.io_clear = 1;
                port.io_enable = 1;
                co_await wait_verify(port.clock);
                CHECK_EQ(port.value_of(), 0zu);

                // 撤掉 clear 后从 0 重新开始计数
                port.io_clear = 0;
                co_await wait_verify(port.clock);
                CHECK_EQ(port.value_of(), 1zu);

                co_await wait_stimulate(port.clock, 2);
                co_await eval_finish();
            },
        };
        ctx.add_task(stimulate());

        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }

    TEST_CASE("counts past the 16-bit boundary")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("DspCounter.past_16bit");
        port_t port{ctx.dut()};
        ctx.add_task(generate_clock(port.clock, period));

        const auto stimulate{
            [&] -> task<void> {
                port.io_clear = 0;
                port.io_enable = 0;
                co_await generate_reset(port.reset, port.clock);

                // 连续 enable：计数超过 16 位后仍应为完整 32 位计数，不发生截断
                port.io_enable = 1;
                for(std::uint64_t i{0}; i < 65537; ++i) { co_await wait_verify(port.clock); }
                CHECK_EQ(port.value_of(), 65537zu);

                // 再跨几拍，确认高位持续累加
                for(auto i{0zu}; i < 3zu; ++i) { co_await wait_verify(port.clock); }
                CHECK_EQ(port.value_of(), 65540zu);

                // 跨 16 位之后 clear 仍然优先
                port.io_clear = 1;
                co_await wait_verify(port.clock);
                CHECK_EQ(port.value_of(), 0zu);

                co_await wait_stimulate(port.clock, 2);
                co_await eval_finish();
            },
        };
        ctx.add_task(stimulate());

        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }
}
