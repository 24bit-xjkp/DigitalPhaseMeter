#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
#include <EdgeDetectorVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using namespace verilator_utils;
    using dut_t = EdgeDetectorVerilator;
    using dut_context_t = dut_context<dut_t, VerilatedFstC>;

    struct port_t
    {
        bit_slice<CData> clock;
        bit_slice<CData> reset;
        bit_slice<CData> io_in;
        bit_slice<CData> io_trigger;

        explicit port_t(dut_t& dut) : clock{dut.clock}, reset{dut.reset}, io_in{dut.io_in}, io_trigger{dut.io_trigger} {}
    };
}  // namespace

TEST_SUITE("EdgeDetector")
{
    TEST_CASE("EdgeDetector")
    {
        dut_context_t ctx{
            {.coverage = true, .time_precision = verilator_time_unit::ns}
        };
        port_t port{ctx.dut()};

        // 时间精度为 ns，且下面的异步激励在 period/4、period/2 处翻转，因此周期必须取
        // 4ns 的整数倍（20ns = 50MHz），才能保证这些等待时长都是整数纳秒、不被截断
        constexpr static auto period{20_ns};
        constexpr static auto pipeline{3zu};
        // 流水线级数为3，倒数第二级表示当前信号
        constexpr static auto delay{pipeline - 1};
        ctx.add_task(generate_clock(port.clock, period));

        const auto verify{
            [&](bool rising) -> task<void> {
                return verify_at(port.clock, [=, &port] { CHECK_EQ(port.io_trigger, rising); }, delay);
            },
        };
        const auto stimulate{
            [&] -> task<void> {
                port.io_in = 0;
                co_await generate_reset(port.reset, port.clock);
                auto verify_tasks{co_await spawn_pool()};
                const auto do_verify{[&](bool rising) { verify_tasks.add_task(verify(rising)); }};

                // 产生异步输入信号
                co_await wait_time(period / 4zu);
                port.io_in = 1;
                do_verify(true);
                co_await wait_time(period / 2zu);
                port.io_in = 0;
                do_verify(false);

                // 等待激励被采样
                co_await wait_posedge(port.clock);

                // 下降沿产生同步输入信号
                co_await wait_stimulate(port.clock);
                port.io_in = 1;
                do_verify(true);
                co_await wait_stimulate(port.clock);
                port.io_in = 0;
                do_verify(false);

                // 上升沿产生同步输入信号
                co_await wait_stimulate(port.clock, 1, edge_enum::rising);
                port.io_in = 1;
                do_verify(true);
                co_await wait_stimulate(port.clock, 1, edge_enum::rising);
                port.io_in = 0;
                do_verify(false);

                // 无输入信号
                co_await wait_stimulate(port.clock, 1, edge_enum::rising);
                do_verify(false);

                co_await verify_tasks.join_all();
                // 避免波形被截断
                co_await wait_stimulate(port.clock, 2);
                co_await eval_finish();
            },
        };
        ctx.add_task(stimulate());

        ctx.loop_until_finish();
        MESSAGE(ctx.stats());
    }
}
