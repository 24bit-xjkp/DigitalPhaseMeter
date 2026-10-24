#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
#include <ResetSynchronizerVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using namespace verilator_utils;
    using dut_t = ResetSynchronizerVerilator;
    using dut_context_t = dut_context<dut_t, VerilatedFstC>;

    /// 复位同步链级数，与 ResetSynchronizer 的 n 默认值一致
    constexpr std::size_t reset_depth{4zu};

    struct port_t
    {
        bit_slice<CData> clock;
        bit_slice<CData> resetn;
        bit_slice<CData> reset;

        explicit port_t(dut_t& dut) : clock{dut.clock}, resetn{dut.resetn}, reset{dut.reset} {}
    };
}  // namespace

TEST_SUITE("ResetSynchronizer")
{
    TEST_CASE("reset asserts asynchronously and releases after the chain depth")
    {
        dut_context_t ctx{
            {.coverage = true, .time_precision = verilator_time_unit::ns}
        };
        ctx.set_base_name("ResetSynchronizer.async_assert_sync_release");
        port_t port{ctx.dut()};

        constexpr static auto period{20_ns};
        ctx.add_task(generate_clock(port.clock, period));

        /// 释放复位：resetn 抬高后输出再保持 reset_depth 个上升沿，第 reset_depth 个上升沿之后失效
        const auto verify_release{[&] -> task<void> {
            for(auto cycle{0zu}; cycle != reset_depth - 1zu; ++cycle)
            {
                co_await wait_verify(port.clock);
                CHECK_EQ(port.reset, 1u);
            }
            co_await wait_verify(port.clock);
            CHECK_EQ(port.reset, 0u);
        }};

        const auto stimulate{[&] -> task<void> {
            // 异步置位：resetn 拉低后，输出不等任何时钟沿立刻有效
            port.resetn = 0;
            co_await wait_time(period / 4zu);
            CHECK_EQ(port.reset, 1u);

            // 复位保持期间输出一直有效
            for(auto cycle{0zu}; cycle != 3zu; ++cycle)
            {
                co_await wait_verify(port.clock);
                CHECK_EQ(port.reset, 1u);
            }

            // 同步释放：resetn 抬高后，输出正好再保持 reset_depth 个上升沿才失效
            co_await wait_stimulate(port.clock);
            port.resetn = 1;
            co_await verify_release();

            // 运行中再次异步置位：同样不等时钟沿立刻有效（此时距上一个上升沿不足一个周期）
            co_await wait_stimulate(port.clock, 2zu);
            port.resetn = 0;
            co_await wait_time(period / 4zu);
            CHECK_EQ(port.reset, 1u);

            // 第二次释放同样经过 reset_depth 个上升沿
            co_await wait_stimulate(port.clock);
            port.resetn = 1;
            co_await verify_release();

            // 避免波形被截断
            co_await wait_stimulate(port.clock, 2zu);
            co_await eval_finish();
        }};
        ctx.add_task(stimulate());

        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }
}
