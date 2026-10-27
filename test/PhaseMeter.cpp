#include <array>
#include <cstdint>

#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
#include <PhaseMeterVerilator.h>
#include <verilator_bwd.hpp>

// PhaseMeter 单元测试：直接驱动高速域端口，不经过 Controller 的慢域 FSM。
//
// 这里针对的是停止握手的一个具体缺陷（P1）：PhaseMeter 通过「电平翻转」把
// response.valid 送回慢域，慢域用同步链检测翻转沿。这个协议要求**每个事件只翻转
// 一次**——如果 sWaitStop 期间对每个 sig2 上升沿都翻转一次，当 sig2 频率接近/超过
// 慢时钟的一半时，慢域每拍采样到的电平可能保持不变（甚至完全错过翻转），停止响应
// 送不到慢域，控制器就永远停在等待停止响应的状态（STATUS.busy 卡死）。
//
// 测试直接数 response.valid 的翻转次数：
//   - 启动请求 → 恰好 1 次翻转（启动响应）；
//   - 停止请求 → 在**慢域不应答**的长窗口内恰好 1 次翻转（停止响应）。
// 修复前第二个断言会数到 20 次左右（每 400ns 的 sig2 上升沿一次）。
namespace
{
    using namespace verilator_utils;
    using dut_t = PhaseMeterVerilator;
    using dut_context_t = dut_context<dut_t, VerilatedFstC>;

    /// 高速测量时钟周期（测试时基取整数纳秒以便精确激励；实际芯片为 450MHz）
    constexpr auto dsp_period{4_ns};
    /// 被测信号周期与相位差：两路同频、相位差半个周期
    constexpr auto sig_period{400_ns};
    constexpr auto sig_phase{200_ns};
    /// 停止响应的观测窗口：2000 个高速周期（8us）。一次停止握手只有几个周期，
    /// 而一个 sig2 周期是 400ns；修复前 sWaitStop 会对窗口内每个 sig2 上升沿翻转，
    /// 因此窗口内会数到 20 次左右
    constexpr std::uint64_t observe_cycles{2000zu};
    /// 每轮完整握手的轮数：第二轮验证「每次启动都会重新武装发送逻辑」
    constexpr std::size_t rounds{2zu};

    /// DUT 端口包装
    struct port_t
    {
        bit_slice<CData> clock;
        bit_slice<CData> reset;
        bit_slice<CData> io_sig_sig1;
        bit_slice<CData> io_sig_sig2;
        bit_slice<CData> io_request;
        bit_slice<CData> io_response_ready;
        bit_slice<CData> io_response_valid;
        vector_slice<IData> io_response_bits_edge_cnt;
        vector_slice<IData> io_response_bits_phase_cnt;

        explicit port_t(dut_t& dut) :
            clock{dut.clock}, reset{dut.reset}, io_sig_sig1{dut.io_sig_sig1, boolean}, io_sig_sig2{dut.io_sig_sig2, boolean},
            io_request{dut.io_request, boolean}, io_response_ready{dut.io_response_ready, boolean},
            io_response_valid{dut.io_response_valid, boolean},
            io_response_bits_edge_cnt{dut.io_response_bits_edgeCnt, 32zu, dec_unsigned},
            io_response_bits_phase_cnt{dut.io_response_bits_phaseCnt, 32zu, dec_unsigned}
        {
        }
    };

    constexpr dut_context_option ctx_opt{.coverage = true, .time_precision = verilator_time_unit::ns};

    /// 在接下来 `cycles` 个高速周期内统计 response.valid 的翻转次数
    [[nodiscard]] task<std::uint64_t> count_response_toggles(const port_t& port, std::uint64_t cycles)
    {
        std::uint64_t count{0};
        auto previous{static_cast<std::uint64_t>(port.io_response_valid)};
        for(auto cycle{0zu}; cycle != static_cast<std::size_t>(cycles); ++cycle)
        {
            co_await wait_verify(port.clock);
            const auto current{static_cast<std::uint64_t>(port.io_response_valid)};
            if(current != previous) { ++count; }
            previous = current;
        }
        co_return count;
    }

    /// 空转若干高速周期
    [[nodiscard]] task<void> wait_cycles(const port_t& port, std::size_t cycles)
    {
        for(auto cycle{0zu}; cycle != cycles; ++cycle) { co_await wait_verify(port.clock); }
        co_return;
    }
}  // namespace

TEST_SUITE("PhaseMeter")
{
    TEST_CASE("a stop request toggles response.valid exactly once")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("PhaseMeter.stop_response_once");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, dsp_period));
        // 被测信号自由运行：两路同频、相位差半个周期
        ctx.add_task(generate_clock(port.io_sig_sig1, sig_period));
        ctx.add_task(generate_clock(port.io_sig_sig2, sig_period, sig_phase));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<std::uint64_t, rounds> start_toggles{};
        std::array<std::uint64_t, rounds> stop_toggles{};

        const auto stimulus{[&] -> task<void> {
            port.io_request = 0;
            port.io_response_ready = 0;
            co_await wait_reset_finish(port.reset);

            for(auto round{0zu}; round != rounds; ++round)
            {
                // 启动请求：慢域发送端翻转一次电平（PhaseMeter 在 sIdle 收到一个边沿）
                co_await wait_stimulate(port.clock);
                port.io_request = 1;

                // 启动响应只应翻转一次（启动握手本身不是本次缺陷所在，这里顺带守住）
                start_toggles[round] = co_await count_response_toggles(port, 400zu);
                CHECK_EQ(start_toggles[round], 1zu);

                // 慢域收到启动响应后的应答（PhaseMeter 在 sMeasuring 期间忽略它）
                co_await wait_stimulate(port.clock);
                port.io_response_ready = 1;
                co_await wait_cycles(port, 100zu);

                // 停止请求：再翻转一次电平
                co_await wait_stimulate(port.clock);
                port.io_request = 0;

                // 故意不应答停止响应，观察一个长窗口：每个停止请求只应产生一次翻转。
                // 修复前这里会对每个 sig2 上升沿再翻转一次（窗口内约 20 次）。
                stop_toggles[round] = co_await count_response_toggles(port, observe_cycles);
                CHECK_EQ(stop_toggles[round], 1zu);

                // 完成本轮停止握手（慢域应答）
                co_await wait_stimulate(port.clock);
                port.io_response_ready = 0;
                co_await wait_cycles(port, 100zu);
            }

            co_await eval_finish();
        }};
        ctx.add_task(stimulus());

        ctx.loop_until_finish(10_ms);

        // 协程内的断言失败不会终止用例，这里再汇总一次，避免只看第一处
        for(auto round{0zu}; round != rounds; ++round)
        {
            CAPTURE(round);
            CHECK_EQ(start_toggles[round], 1zu);
            CHECK_EQ(stop_toggles[round], 1zu);
        }
    }
}
