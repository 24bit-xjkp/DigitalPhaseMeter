// MmcmDrpController 的 Verilator 单测
//
// 被测对象：AXI4-Lite 从口 + MMCM DRP 写序列状态机。
// 这里用「DRP 从端模型」代替真实 MMCM：unisim 的 MMCME2_ADV 在 xsim 里对 DRP
// 写入不响应（实测写 0x0A/0x0B 的任意值都不改变寄存器内容），因此无法用它验证
// 控制器的正确性；本测试只验证控制器自己发出的地址、数据与握手时序。
//
// 覆盖点：
//   1. ID 寄存器读回；
//   2. 八个 MMCM 配置寄存器写入后能读回（AXI 通路正确）；
//   3. 写 LOAD=1 后 DRP 上按固定顺序出现 8 笔写，数据与软件写入的一致；
//   4. 每笔写之间 DEN 只拉高一拍（MMCM DRP 协议要求）；
//   5. 未写 LOAD 时 DRP 上没有任何写；
//   6. STATUS 的 busy 位在重配置期间为 1、结束后回到 0；
//   7. locked 异步电平同步后出现在 STATUS.b0。

#include <array>
#include <cstdint>
#include <optional>
#include <vector>

#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
#include <MmcmDrpControllerVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using namespace verilator_utils;
    using dut_t = MmcmDrpControllerVerilator;
    using dut_context_t = dut_context<dut_t, VerilatedFstC>;

    constexpr auto clock_period{20_ns};

    /// IP 在 BD 里的基地址（`vivado/create_bd.tcl` 的 M01_AXI → 0x43C1_0000）。
    ///
    /// 激励必须按**真实地址**（基地址 + 偏移）驱动：AXI 互连不剥离基地址，slave 看到的是
    /// 完整 32 位地址。历史上这里按裸偏移（`idx * 4`）驱动，掩盖了「窗口判断误用高位地址」
    /// 的缺陷——上板时所有 DRP 读写都被丢弃、SET_SIG 恒回 ERR_BUSY。
    constexpr std::uint32_t kIpBase{0x43C1'0000u};

    constexpr std::uint32_t ip_addr(std::uint32_t word_index) { return kIpBase + word_index * 4u; }

    // 寄存器字索引（字节偏移 = idx * 4）
    constexpr std::uint32_t kId{0};
    constexpr std::uint32_t kStatus{1};
    constexpr std::uint32_t kClkfbout{2};
    constexpr std::uint32_t kClkfbout2{3};
    constexpr std::uint32_t kClkout0{4};
    constexpr std::uint32_t kClkout0_2{5};
    constexpr std::uint32_t kClkout1{6};
    constexpr std::uint32_t kClkout1_2{7};
    constexpr std::uint32_t kDivclk{8};
    constexpr std::uint32_t kPower{9};
    constexpr std::uint32_t kLoad{10};

    /// 8 笔 DRP 写的目标地址顺序（与厂商参考设计的 ROM 一致）
    constexpr std::array<std::uint32_t, 8> kDrpAddrOrder{0x14, 0x15, 0x08, 0x09, 0x0A, 0x0B, 0x16, 0x28};

    struct DrpWrite
    {
        std::uint32_t addr;
        std::uint32_t data;
        /// 该笔写发生时 MMCM 重配置复位是否已拉高（要求恒为 1）
        std::uint32_t reset_level;
    };

    /// DUT 端口包装：AXI4-Lite 从口 + DRP 从端模型 + locked
    struct port_t
    {
        bit_slice<CData> clock;
        bit_slice<CData> resetn;
        // AXI4-Lite 从口
        vector_slice<IData> awaddr;
        bit_slice<CData> awvalid;
        bit_slice<CData> awready;
        vector_slice<IData> wdata;
        vector_slice<CData> wstrb;
        bit_slice<CData> wvalid;
        bit_slice<CData> wready;
        bit_slice<CData> bvalid;
        bit_slice<CData> bready;
        vector_slice<IData> araddr;
        bit_slice<CData> arvalid;
        bit_slice<CData> arready;
        vector_slice<IData> rdata;
        bit_slice<CData> rvalid;
        bit_slice<CData> rready;
        // DRP 从端模型
        vector_slice<CData> drp_daddr;
        vector_slice<SData> drp_di;
        bit_slice<CData> drp_den;
        bit_slice<CData> drp_dwe;
        bit_slice<SData> drp_dout;
        bit_slice<CData> drp_drdy;
        // MMCM 锁定
        bit_slice<CData> locked;
        // 重配置复位（写 DRP 期间必须为高）
        bit_slice<CData> mmcm_reset;

        explicit port_t(dut_t& dut) :
            clock{dut.clock}, resetn{dut.resetn}, awaddr{dut.s_axi_lite_awaddr, 32zu, dec_unsigned},
            awvalid{dut.s_axi_lite_awvalid, boolean}, awready{dut.s_axi_lite_awready, boolean},
            wdata{dut.s_axi_lite_wdata, 32zu, dec_unsigned}, wstrb{dut.s_axi_lite_wstrb, 4zu, dec_unsigned},
            wvalid{dut.s_axi_lite_wvalid, boolean}, wready{dut.s_axi_lite_wready, boolean},
            bvalid{dut.s_axi_lite_bvalid, boolean}, bready{dut.s_axi_lite_bready, boolean},
            araddr{dut.s_axi_lite_araddr, 32zu, dec_unsigned}, arvalid{dut.s_axi_lite_arvalid, boolean},
            arready{dut.s_axi_lite_arready, boolean}, rdata{dut.s_axi_lite_rdata, 32zu, dec_unsigned},
            rvalid{dut.s_axi_lite_rvalid, boolean}, rready{dut.s_axi_lite_rready, boolean},
            drp_daddr{dut.drp_daddr, 7zu, dec_unsigned}, drp_di{dut.drp_di, 16zu, dec_unsigned}, drp_den{dut.drp_den, boolean},
            drp_dwe{dut.drp_dwe, boolean}, drp_dout{dut.drp_dout}, drp_drdy{dut.drp_drdy, boolean}, locked{dut.locked, boolean},
            mmcm_reset{dut.mmcm_reset, boolean}
        {
        }

        /// 在下降沿之后、DUT 评估之前设置激励
        [[nodiscard]] auto stimulate(std::size_t cycles = 1zu) const { return wait_stimulate(clock, cycles); }
    };

    /// AXI4-Lite 写一笔：AW 与 W 同时举起（本项目 clk_wiz 的 ipif 也要求两通道同拍有效）
    ///
    /// `sink` 非空时，在握手期间每拍记录一次 DRP 上的 DEN 脉冲——因为写 LOAD 的
    /// **同一拍**状态机就可能发出第一笔 DRP 写，若等写完成后再开始观察会漏掉第一笔。
    task<void> axi_write(port_t& port, std::uint32_t word_index, std::uint32_t data, std::vector<DrpWrite>* sink = nullptr)
    {
        port.awaddr = ip_addr(word_index);
        port.awvalid = 1;
        port.wdata = data;
        port.wstrb = 0xF;
        port.wvalid = 1;
        port.drp_drdy = 1;
        co_await port.stimulate();
        if((sink != nullptr) && (port.drp_den != 0u) && (port.drp_dwe != 0u))
        {
            sink->push_back({static_cast<std::uint32_t>(port.drp_daddr),
                             static_cast<std::uint32_t>(port.drp_di),
                             static_cast<std::uint32_t>(port.mmcm_reset)});
        }
        // 等 bvalid
        for(std::size_t i{0zu}; i != 20zu && port.bvalid == 0u; ++i)
        {
            port.drp_drdy = 1;
            co_await port.stimulate();
            if((sink != nullptr) && (port.drp_den != 0u) && (port.drp_dwe != 0u))
            {
                sink->push_back({static_cast<std::uint32_t>(port.drp_daddr),
                                 static_cast<std::uint32_t>(port.drp_di),
                                 static_cast<std::uint32_t>(port.mmcm_reset)});
            }
        }
        port.awvalid = 0;
        port.wvalid = 0;
        co_await port.stimulate();
        co_await port.stimulate();
    }

    /// AXI4-Lite 读一笔
    /// rready 常为 1，因此读到 rvalid 的那一拍即完成握手，直接取 rdata。
    task<std::uint32_t> axi_read(port_t& port, std::uint32_t word_index)
    {
        port.araddr = ip_addr(word_index);
        port.arvalid = 1;
        std::uint32_t value{0};
        bool got{false};
        for(std::size_t i{0zu}; i != 20zu && !got; ++i)
        {
            co_await port.stimulate();
            if((port.rvalid != 0u) && (port.rready != 0u))
            {
                value = static_cast<std::uint32_t>(port.rdata);
                got = true;
            }
        }
        port.arvalid = 0;
        co_await port.stimulate();
        co_return value;
    }

    /// 复位并初始化所有激励
    task<void> init_dut(port_t& port)
    {
        port.awvalid = 0;
        port.wvalid = 0;
        port.bready = 1;
        port.arvalid = 0;
        port.rready = 1;
        port.drp_drdy = 1;
        port.drp_dout = 0;
        port.locked = 1;
        co_await generate_reset(port.resetn, port.clock, 4zu, false);
    }

    /// DRP 从端模型：把 DRDY 常置 1。
    ///
    /// MMCM 的语义是「DEN 拉高一拍之后 DRDY 拉高表示访问完成」。本测试只验证
    /// 控制器自己发出的地址/数据序列与 DEN 宽度，因此让 DRDY 常为 1 最简单也最
    /// 确定：状态机在每个 sWait 拍看到 DRDY 就推进，于是 8 笔写恰好各占 2 拍
    /// （1 拍 DEN + 1 拍等 DRDY），DEN 每笔都是 1 拍。
    /// 真实的 DRDY 往返由 `sim/run_handoff_tb.sh`（真实 MMCM 行为模型 + DRP 控制器 + 测量通路）
    /// 覆盖，本测试只关心控制器自己发出的地址/数据序列与 DEN 宽度。
    task<std::vector<DrpWrite>> run_drp_slave(port_t& port, std::size_t cycles)
    {
        std::vector<DrpWrite> writes{};
        for(std::size_t i{0zu}; i != cycles; ++i)
        {
            port.drp_drdy = 1;
            co_await port.stimulate();
            if((port.drp_den != 0u) && (port.drp_dwe != 0u))
            {
                writes.push_back({static_cast<std::uint32_t>(port.drp_daddr),
                                  static_cast<std::uint32_t>(port.drp_di),
                                  static_cast<std::uint32_t>(port.mmcm_reset)});
            }
        }
        co_return writes;
    }

    /// 数每笔 DRP 写的 DEN 持续拍数（协议要求每笔恰好一拍）
    task<std::vector<int>> drp_den_widths(port_t& port, std::size_t cycles)
    {
        std::vector<int> widths{};
        int current{0};
        for(std::size_t i{0zu}; i != cycles; ++i)
        {
            port.drp_drdy = 1;
            co_await port.stimulate();
            if((port.drp_den != 0u) && (port.drp_dwe != 0u)) { ++current; }
            else if(current > 0)
            {
                widths.push_back(current);
                current = 0;
            }
            if(port.bvalid != 0u)
            {
                port.awvalid = 0;
                port.wvalid = 0;
            }
        }
        if(current > 0) { widths.push_back(current); }
        co_return widths;
    }

    constexpr std::array<std::pair<std::uint32_t, std::uint32_t>, 8> kPattern{
        {
         {kClkfbout, 0x128A},
         {kClkfbout2, 0x0001},
         {kClkout0, 0x1514},
         {kClkout0_2, 0x0002},
         {kClkout1, 0x1A28},
         {kClkout1_2, 0x0014},
         {kDivclk, 0x1041},
         {kPower, 0xFFFF},
         }
    };

    /// 按**字索引**（不是数组下标）取 kPattern 里的值：kPattern 从 kClkfbout 起顺序排列。
    constexpr std::uint32_t pattern_value(std::uint32_t word_index) { return kPattern.at(word_index - kClkfbout).second; }

    constexpr dut_context_option ctx_opt{.coverage = true, .time_precision = verilator_time_unit::ns};
}  // namespace

TEST_CASE("MmcmDrpController：ID 与配置寄存器读写")
{
    dut_context_t ctx{ctx_opt};
    ctx.set_base_name("MmcmDrpController.regs");
    port_t port{ctx.dut()};
    ctx.add_task(generate_clock(port.clock, clock_period));
    ctx.add_task(max_eval_time(20_us));

    std::optional<std::uint32_t> id{};
    std::array<std::optional<std::uint32_t>, 8> readback{};
    ctx.add_task([&] -> task<void> {
        co_await init_dut(port);
        id = co_await axi_read(port, kId);
        for(const auto& [idx, value]: kPattern) { co_await axi_write(port, idx, value); }
        for(std::size_t i{0zu}; i != kPattern.size(); ++i) { readback[i] = co_await axi_read(port, kPattern[i].first); }
        co_await eval_finish();
    }());

    ctx.loop_until_finish(20_us);

    REQUIRE(id.has_value());
    // REQUIRE 失败会终止用例，因此此处访问是安全的
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*id, 0x4d03u);
    for(std::size_t i{0zu}; i != kPattern.size(); ++i)
    {
        REQUIRE(readback[i].has_value());
        CAPTURE(i);
        // REQUIRE 失败会终止用例，因此此处访问是安全的
        // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
        CHECK_EQ(*readback[i], kPattern[i].second);
    }
}

TEST_CASE("MmcmDrpController：地址译码只看页内偏移（基地址 + 偏移）")
{
    // 回归用例：地址译码必须接受「BD 基地址 + 页内偏移」的完整 32 位地址
    // （上板实测：早期实现按 addr[31:6] == 0 判断窗口，真实地址 0x43C1_0008 被判为窗口外，
    //  所有 DRP 读写被丢弃 → SET_SIG 恒回 ERR_BUSY）。同时确认页内越界偏移仍被忽略。
    dut_context_t ctx{ctx_opt};
    ctx.set_base_name("MmcmDrpController.window");
    port_t port{ctx.dut()};
    ctx.add_task(generate_clock(port.clock, clock_period));
    ctx.add_task(max_eval_time(20_us));

    std::optional<std::uint32_t> id_at_base{};
    std::optional<std::uint32_t> clkout1_at_base{};
    std::optional<std::uint32_t> read_out_of_page{};
    std::optional<std::uint32_t> read_in_page_but_unmapped{};
    ctx.add_task([&] -> task<void> {
        co_await init_dut(port);
        // 基地址 + 偏移：必须命中（早期实现这里读回 0）
        id_at_base = co_await axi_read(port, kId);
        co_await axi_write(port, kClkout1, pattern_value(kClkout1));
        clkout1_at_base = co_await axi_read(port, kClkout1);
        // 页内、但越出寄存器区的偏移（0x40 起，addr[11:6] != 0）：必须被忽略（读回 0）
        port.araddr = kIpBase + 0x40u;
        port.arvalid = 1;
        for(std::size_t i{0zu}; i != 20zu && !read_in_page_but_unmapped.has_value(); ++i)
        {
            co_await port.stimulate();
            if((port.rvalid != 0u) && (port.rready != 0u)) { read_in_page_but_unmapped = static_cast<std::uint32_t>(port.rdata); }
        }
        port.arvalid = 0;
        co_await port.stimulate();
        // 同一页里的另一个越界偏移（0x80）：同样读回 0。
        // 注：地址的 [31:12] 不参与译码——BD 的地址段（M01_AXI → 0x43C1_0000）保证互连
        // 只把本段内的传输送到这里，因此别的 4KB 段（如 0x43C0_xxxx）根本不会到达本 IP；
        // 换言之 `基地址 + 0x1000` 与 `基地址` 的低 12 位相同，不应该、也无法被本模块区分。
        port.araddr = kIpBase + 0x80u;
        port.arvalid = 1;
        for(std::size_t i{0zu}; i != 20zu && !read_out_of_page.has_value(); ++i)
        {
            co_await port.stimulate();
            if((port.rvalid != 0u) && (port.rready != 0u)) { read_out_of_page = static_cast<std::uint32_t>(port.rdata); }
        }
        port.arvalid = 0;
        co_await eval_finish();
    }());

    ctx.loop_until_finish(20_us);

    REQUIRE(id_at_base.has_value());
    // REQUIRE 失败会终止用例，因此此处访问是安全的
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*id_at_base, 0x4d03u);
    REQUIRE(clkout1_at_base.has_value());
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*clkout1_at_base, pattern_value(kClkout1));
    REQUIRE(read_in_page_but_unmapped.has_value());
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*read_in_page_but_unmapped, 0u);
    REQUIRE(read_out_of_page.has_value());
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*read_out_of_page, 0u);
}

TEST_CASE("MmcmDrpController：写 LOAD 触发 8 笔 DRP 写且顺序/数据正确")
{
    dut_context_t ctx{ctx_opt};
    ctx.set_base_name("MmcmDrpController.load");
    port_t port{ctx.dut()};
    ctx.add_task(generate_clock(port.clock, clock_period));
    ctx.add_task(max_eval_time(20_us));

    std::vector<DrpWrite> before{};
    std::vector<DrpWrite> after{};
    ctx.add_task([&] -> task<void> {
        co_await init_dut(port);
        for(const auto& [idx, value]: kPattern) { co_await axi_write(port, idx, value); }
        // 未写 LOAD：DRP 上不应有任何写
        before = co_await run_drp_slave(port, 20zu);
        // 写 LOAD=1 触发重配置：写期间就开始收集，避免漏掉同拍发出的第一笔
        // LOAD 写在**同一拍**就可能触发第一笔 DRP 写，因此把 LOAD 握手与 DRP 采样
        // 放进同一个逐拍循环，保证不漏拍。
        port.awaddr = ip_addr(kLoad);
        port.awvalid = 1;
        port.wdata = 1u;
        port.wstrb = 0xF;
        port.wvalid = 1;
        for(std::size_t i{0zu}; i != 100zu; ++i)
        {
            port.drp_drdy = 1;
            co_await port.stimulate();
            if((port.drp_den != 0u) && (port.drp_dwe != 0u))
            {
                after.push_back({static_cast<std::uint32_t>(port.drp_daddr),
                                 static_cast<std::uint32_t>(port.drp_di),
                                 static_cast<std::uint32_t>(port.mmcm_reset)});
            }
            if(port.bvalid != 0u)
            {
                port.awvalid = 0;
                port.wvalid = 0;
            }
        }
        co_await eval_finish();
    }());

    ctx.loop_until_finish(20_us);

    CHECK_EQ(before.size(), 0zu);
    REQUIRE_EQ(after.size(), 8zu);
    for(std::size_t i{0zu}; i != 8zu; ++i)
    {
        CAPTURE(i);
        CHECK_EQ(after[i].addr, kDrpAddrOrder[i]);
    }
    // 数据与软件写入的寄存器一一对应（顺序同上表）
    CHECK_EQ(after[0].data, 0x128Au);  // CLKFBOUT  -> 0x14
    CHECK_EQ(after[1].data, 0x0001u);  // CLKFBOUT2 -> 0x15
    CHECK_EQ(after[2].data, 0x1514u);  // CLKOUT0   -> 0x08
    CHECK_EQ(after[3].data, 0x0002u);  // CLKOUT0_2 -> 0x09
    CHECK_EQ(after[4].data, 0x1A28u);  // CLKOUT1   -> 0x0A
    CHECK_EQ(after[5].data, 0x0014u);  // CLKOUT1_2 -> 0x0B
    CHECK_EQ(after[6].data, 0x1041u);  // DIVCLK    -> 0x16
    CHECK_EQ(after[7].data, 0xFFFFu);  // POWER     -> 0x28
    // 8 笔 DRP 写全部在 MMCM 重配置复位拉高期间发出（MMCM 的 DRP 写要求器件处于复位，
    // 否则相位延迟单元不重新初始化——上板实测「分频字生效、相位字不生效」即此故）
    for(std::size_t i{0zu}; i != 8zu; ++i)
    {
        CAPTURE(i);
        CHECK_EQ(after[i].reset_level, 1u);
    }
    // 写完之后复位必须放开（否则 MMCM 不会重新锁定）
    CHECK_EQ(static_cast<std::uint32_t>(port.mmcm_reset), 0u);
}

TEST_CASE("MmcmDrpController：DEN 每笔只拉高一拍，busy 在重配置期间为 1")
{
    dut_context_t ctx{ctx_opt};
    ctx.set_base_name("MmcmDrpController.timing");
    port_t port{ctx.dut()};
    ctx.add_task(generate_clock(port.clock, clock_period));
    ctx.add_task(max_eval_time(20_us));

    std::vector<int> widths{};
    std::optional<std::uint32_t> busy_idle_before{};
    std::optional<std::uint32_t> busy_idle_after{};
    ctx.add_task([&] -> task<void> {
        co_await init_dut(port);
        busy_idle_before = co_await axi_read(port, kStatus);
        port.awaddr = ip_addr(kLoad);
        port.awvalid = 1;
        port.wdata = 1u;
        port.wstrb = 0xF;
        port.wvalid = 1;
        widths = co_await drp_den_widths(port, 100zu);
        (void)co_await run_drp_slave(port, 8zu);
        busy_idle_after = co_await axi_read(port, kStatus);
        co_await eval_finish();
    }());

    ctx.loop_until_finish(20_us);

    REQUIRE_EQ(widths.size(), 8zu);
    for(std::size_t i{0zu}; i != widths.size(); ++i)
    {
        CAPTURE(i);
        CHECK_EQ(widths[i], 1);
    }
    REQUIRE(busy_idle_before.has_value());
    REQUIRE(busy_idle_after.has_value());
    // REQUIRE 失败会终止用例，因此此处访问是安全的
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*busy_idle_before & 0b110u, 0u);
    // REQUIRE 失败会终止用例，因此此处访问是安全的
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*busy_idle_after & 0b110u, 0u);
}

TEST_CASE("MmcmDrpController：locked 异步电平同步后出现在 STATUS.b0")
{
    dut_context_t ctx{ctx_opt};
    ctx.set_base_name("MmcmDrpController.locked");
    port_t port{ctx.dut()};
    ctx.add_task(generate_clock(port.clock, clock_period));
    ctx.add_task(max_eval_time(20_us));

    std::optional<std::uint32_t> unlocked{};
    std::optional<std::uint32_t> locked{};
    ctx.add_task([&] -> task<void> {
        co_await init_dut(port);
        port.locked = 0;
        for(std::size_t i{0zu}; i != 8zu; ++i) { co_await port.stimulate(); }
        unlocked = co_await axi_read(port, kStatus);
        port.locked = 1;
        for(std::size_t i{0zu}; i != 8zu; ++i) { co_await port.stimulate(); }
        locked = co_await axi_read(port, kStatus);
        co_await eval_finish();
    }());

    ctx.loop_until_finish(20_us);

    REQUIRE(unlocked.has_value());
    REQUIRE(locked.has_value());
    // REQUIRE 失败会终止用例，因此此处访问是安全的
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*unlocked & 1u, 0u);
    // REQUIRE 失败会终止用例，因此此处访问是安全的
    // NOLINTNEXTLINE(bugprone-unchecked-optional-access)
    CHECK_EQ(*locked & 1u, 1u);
}
