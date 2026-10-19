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

    /// 时钟周期。本模块只有 1 bit 控制与 32 bit 输出，不涉及时间精度截断陷阱；
    /// 10ps 精度与其它 RTL 测试保持一致。
    constexpr auto period{1_ns};

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
    };

    [[nodiscard]] std::uint64_t value_of(const port_t& port) { return static_cast<std::uint64_t>(port.io_value); }
}  // namespace

TEST_SUITE("DspCounter")
{
    TEST_CASE("enable counts, clear dominates enable")
    {
        dut_context_t ctx{
            {.coverage = true, .time_precision = verilator_time_unit::ps_10}
        };
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
                CHECK_EQ(value_of(port), 4zu);

                // enable 拉低 → 保持
                port.io_enable = 0;
                co_await wait_verify(port.clock);
                CHECK_EQ(value_of(port), 4zu);

                // clear 与 enable 同时有效 → clear 优先，计数归零
                port.io_clear = 1;
                port.io_enable = 1;
                co_await wait_verify(port.clock);
                CHECK_EQ(value_of(port), 0zu);

                // 撤掉 clear 后从 0 重新开始计数
                port.io_clear = 0;
                co_await wait_verify(port.clock);
                CHECK_EQ(value_of(port), 1zu);

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
        dut_context_t ctx{
            {.coverage = true, .time_precision = verilator_time_unit::ps_10}
        };
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
                CHECK_EQ(value_of(port), 65537zu);

                // 再跨几拍，确认高位持续累加
                for(auto i{0zu}; i < 3zu; ++i) { co_await wait_verify(port.clock); }
                CHECK_EQ(value_of(port), 65540zu);

                // 跨 16 位之后 clear 仍然优先
                port.io_clear = 1;
                co_await wait_verify(port.clock);
                CHECK_EQ(value_of(port), 0zu);

                co_await wait_stimulate(port.clock, 2);
                co_await eval_finish();
            },
        };
        ctx.add_task(stimulate());

        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }
}
