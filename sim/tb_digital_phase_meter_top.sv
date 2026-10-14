// 数字相位计顶层 IP 的 SystemVerilog 测试激励（Vivado xsim）
//
// 被测对象是 DigitalPhaseMeterTop（综合产物，XPM 原语版本）。数据通道是 GPIO 式的寄存器：
// 一次测量的结果只有 4 个字，锁存在 ControlRegs 的只读寄存器里，PS 收到中断后读走即可，
// IP 内部没有 BRAM，对外也只有 s_axi_lite 一个 AXI4-Lite 从口。覆盖：
//   - 主时钟同步复位的异步置位/同步释放：复位期间无中断、AXI 从口不接受任何传输、寄存器为默认值；
//   - AXI4-Lite 寄存器读写（含写地址/写数据通道顺序颠倒、字节选通、只读寄存器写忽略）；
//   - 测量启动与 busy/resultValid/lost/中断挂起位的时序，busy 期间重复写 start 被忽略；
//   - 测量完成中断（高电平）、中断使能门控，以及软件写 IRQ_STATUS 清中断/清 resultValid/清丢失计数；
//   - 读 4 个结果寄存器并用理论模型校验 gateCnt / edgeCnt / phaseCnt
//     （模型见 .agents/skills/dpm-measurement-model）；
//   - 结果只保留最新一条：不读走就再次测量会覆盖旧结果并累加 LOST_COUNT；
//   - 片内信号源与输入选择：SIG_CTRL / SIG_STATUS 经 AXI 读写，选中片内源时测量跟随片内信号、
//     切回外部输入后跟随外部引脚信号（两条通路的理论值不同，因此这个用例能区分「切换生效」与
//     「切换没生效」）。片内源的两路信号来自 PL 内 MMCM（动态可重配置），仿真里用两个固定相位差的
//     信号源代替（周期 640ns、相位差 160ns），MMCM 的频率/相位寄存器不在本激励的覆盖范围内。
//
// 运行方式见 sim/run_xsim.sh。

`timescale 1ns/1ps
`default_nettype none

module tb_digital_phase_meter_top;

    // ---------------- 参数 ----------------
    // 主时钟 50MHz，高速测量时钟 200MHz
    localparam real CLOCK_PERIOD = 20.0;
    localparam real DSP_CLOCK_PERIOD = 5.0;
    // 被测信号：400ns 周期，sig2 上升沿滞后 sig1 120ns
    localparam real SIGNAL_PERIOD = 400.0;
    localparam real PHASE_OFFSET = 120.0;
    // 目标闸门计数
    localparam int unsigned GATE_TARGET_SLOW = 2000;
    localparam int unsigned GATE_TARGET_FAST = 500;
    // 覆盖计数用例用的三个不同闸门时间
    localparam int unsigned GATE_TARGET_A = 600;
    localparam int unsigned GATE_TARGET_B = 1000;
    localparam int unsigned GATE_TARGET_C = 1400;
    // 极短闸门时间，用于测量期间改写 GATE_TARGET 的用例
    localparam int unsigned GATE_TARGET_TINY = 8;
    // 版本号："DPM2"，末尾 02 表示数据经寄存器（GPIO 式）传递
    localparam logic [31:0] VERSION_VALUE = 32'h4450_4d02;
    // CAPS：b[7:0] 每条结果字数 = 4、b[15:8] 结果深度 = 1、b16 数据通道为寄存器 = 1、
    // b17 带片内信号源与输入选择寄存器 = 1
    localparam logic [31:0] CAPS_VALUE = 32'h0003_0104;

    // 寄存器偏移（字对齐）
    localparam logic [31:0] REG_CTRL = 32'h00;
    localparam logic [31:0] REG_STATUS = 32'h04;
    localparam logic [31:0] REG_GATE_TARGET = 32'h08;
    localparam logic [31:0] REG_RESULT_GATE = 32'h0c;
    localparam logic [31:0] REG_RESULT_EDGE = 32'h10;
    localparam logic [31:0] REG_RESULT_PHASE = 32'h14;
    localparam logic [31:0] REG_RESULT_SEQ = 32'h18;
    localparam logic [31:0] REG_LOST_COUNT = 32'h1c;
    localparam logic [31:0] REG_IRQ_STATUS = 32'h20;
    localparam logic [31:0] REG_VERSION = 32'h24;
    localparam logic [31:0] REG_CAPS = 32'h28;
    localparam logic [31:0] REG_SIG_CTRL = 32'h2c;
    localparam logic [31:0] REG_SIG_STATUS = 32'h30;

    // CTRL 位
    localparam int unsigned CTRL_START = 0;
    localparam int unsigned CTRL_IRQ_ENABLE = 1;
    localparam int unsigned CTRL_CLEAR = 2;
    // STATUS 位
    localparam int unsigned STATUS_BUSY = 0;
    localparam int unsigned STATUS_RESULT = 1;
    localparam int unsigned STATUS_LOST = 2;
    localparam int unsigned STATUS_IRQ = 3;
    // IRQ_STATUS 位（写 1 清除）
    localparam int unsigned IRQ_PENDING = 0;
    localparam int unsigned IRQ_RESULT_ACK = 1;
    localparam int unsigned IRQ_LOST_CLEAR = 2;
    // SIG_CTRL 位
    localparam int unsigned SIG_ENABLE = 0;
    localparam int unsigned SIG_SELECT = 1;
    // SIG_STATUS 位
    localparam int unsigned SIG_STATUS_ENABLE = 0;
    localparam int unsigned SIG_STATUS_SELECT = 1;
    localparam int unsigned SIG_STATUS_LOCKED = 2;
    localparam int unsigned SIG_STATUS_READY = 3;

    // 片内信号源工作点：MMCM 两路输出的周期 640ns、相位差 160ns（90°），两路输出直接作为
    // 被测信号（测量核心的异步采样链只把它量化到 1 个高速时钟周期）
    localparam real SOURCE_PERIOD = 640.0;
    localparam real SOURCE_OFFSET = 160.0;
    localparam real INTERNAL_PERIOD = SOURCE_PERIOD;
    localparam real INTERNAL_OFFSET = SOURCE_OFFSET;

    // ---------------- 时钟与复位 ----------------
    logic clock = 1'b0;
    logic dsp_clock = 1'b0;
    logic resetn;

    always #(CLOCK_PERIOD / 2.0) clock = ~clock;
    always #(DSP_CLOCK_PERIOD / 2.0) dsp_clock = ~dsp_clock;

    // ---------------- 被测信号源（外部引脚） ----------------
    logic sig1 = 1'b0;
    logic sig2 = 1'b0;

    initial begin : sig1_source
        forever begin
            sig1 = 1'b1;
            #(SIGNAL_PERIOD / 2.0);
            sig1 = 1'b0;
            #(SIGNAL_PERIOD / 2.0);
        end
    end

    initial begin : sig2_source
        #(PHASE_OFFSET);
        forever begin
            sig2 = 1'b1;
            #(SIGNAL_PERIOD / 2.0);
            sig2 = 1'b0;
            #(SIGNAL_PERIOD / 2.0);
        end
    end

    // ---------------- 片内信号源（MMCM 的两路输出） ----------------
    // 实际电路中这两路来自 PL 内 MMCM（clk_wiz，动态可重配置）的两个输出：频率与相位差由
    // MMCM 的配置寄存器给出。仿真里用两个固定相位差的信号源代替，IP 只需要它们的边沿。
    logic src_sig1 = 1'b0;
    logic src_sig2 = 1'b0;
    logic src_locked = 1'b0;

    initial begin : src_sig1_source
        forever begin
            src_sig1 = 1'b1;
            #(SOURCE_PERIOD / 2.0);
            src_sig1 = 1'b0;
            #(SOURCE_PERIOD / 2.0);
        end
    end

    initial begin : src_sig2_source
        #(SOURCE_OFFSET);
        forever begin
            src_sig2 = 1'b1;
            #(SOURCE_PERIOD / 2.0);
            src_sig2 = 1'b0;
            #(SOURCE_PERIOD / 2.0);
        end
    end

    // MMCM 锁定指示：复位期间为低，复位释放 10us 后拉高（再次复位时重新拉低）
    initial begin : src_lock
        src_locked = 1'b0;
        forever begin
            @(posedge resetn);
            #10000.0;
            src_locked = 1'b1;
            @(negedge resetn);
            src_locked = 1'b0;
        end
    end

    // ---------------- AXI4-Lite 从口信号（控制/状态与结果寄存器） ----------------
    logic [31:0] s_axi_lite_awaddr;
    logic [2:0] s_axi_lite_awprot;
    logic s_axi_lite_awvalid;
    logic s_axi_lite_awready;
    logic [31:0] s_axi_lite_wdata;
    logic [3:0] s_axi_lite_wstrb;
    logic s_axi_lite_wvalid;
    logic s_axi_lite_wready;
    logic [1:0] s_axi_lite_bresp;
    logic s_axi_lite_bvalid;
    logic s_axi_lite_bready;
    logic [31:0] s_axi_lite_araddr;
    logic [2:0] s_axi_lite_arprot;
    logic s_axi_lite_arvalid;
    logic s_axi_lite_arready;
    logic [31:0] s_axi_lite_rdata;
    logic [1:0] s_axi_lite_rresp;
    logic s_axi_lite_rvalid;
    logic s_axi_lite_rready;

    logic interrupt;

    // ---------------- DUT ----------------
    DigitalPhaseMeterTop dut (
        .clock(clock),
        .resetn(resetn),
        .dspClock(dsp_clock),
        .sig1(sig1),
        .sig2(sig2),
        .srcSig1(src_sig1),
        .srcSig2(src_sig2),
        .srcLocked(src_locked),
        .s_axi_lite_awaddr(s_axi_lite_awaddr),
        .s_axi_lite_awprot(s_axi_lite_awprot),
        .s_axi_lite_awvalid(s_axi_lite_awvalid),
        .s_axi_lite_awready(s_axi_lite_awready),
        .s_axi_lite_wdata(s_axi_lite_wdata),
        .s_axi_lite_wstrb(s_axi_lite_wstrb),
        .s_axi_lite_wvalid(s_axi_lite_wvalid),
        .s_axi_lite_wready(s_axi_lite_wready),
        .s_axi_lite_bresp(s_axi_lite_bresp),
        .s_axi_lite_bvalid(s_axi_lite_bvalid),
        .s_axi_lite_bready(s_axi_lite_bready),
        .s_axi_lite_araddr(s_axi_lite_araddr),
        .s_axi_lite_arprot(s_axi_lite_arprot),
        .s_axi_lite_arvalid(s_axi_lite_arvalid),
        .s_axi_lite_arready(s_axi_lite_arready),
        .s_axi_lite_rdata(s_axi_lite_rdata),
        .s_axi_lite_rresp(s_axi_lite_rresp),
        .s_axi_lite_rvalid(s_axi_lite_rvalid),
        .s_axi_lite_rready(s_axi_lite_rready),
        .interrupt(interrupt)
    );

    // ---------------- 检查辅助 ----------------
    int unsigned error_count = 0;

    task automatic fail(input string message);
        error_count++;
        $display("[%0t] 错误：%s", $time, message);
    endtask

    task automatic check_equal_u32(input logic [31:0] actual, input logic [31:0] expected, input string name);
        if (actual !== expected) begin
            fail($sformatf("%s expected 0x%08x, got 0x%08x", name, expected, actual));
        end else begin
            $display("[%0t] 通过：%s = 0x%08x", $time, name, actual);
        end
    endtask

    task automatic check_range(input int unsigned actual, input int unsigned low, input int unsigned high,
                               input string name);
        if (actual < low || actual > high) begin
            fail($sformatf("%s expected in [%0d, %0d], got %0d", name, low, high, actual));
        end else begin
            $display("[%0t] 通过：%s = %0d（容限 [%0d, %0d]）", $time, name, actual, low, high);
        end
    endtask

    task automatic check_bit(input logic actual, input logic expected, input string name);
        if (actual !== expected) begin
            fail($sformatf("%s expected %0b, got %0b", name, expected, actual));
        end else begin
            $display("[%0t] 通过：%s = %0b", $time, name, actual);
        end
    endtask

    // ---------------- AXI4-Lite 主机 ----------------
    // 激励在时钟下降沿驱动、在上升沿采样握手，避免与 DUT 的时序寄存器竞争；
    // 每次握手完成后都回到下降沿再撤销信号。

    task automatic axi_lite_init();
        s_axi_lite_awaddr = '0;
        s_axi_lite_awprot = '0;
        s_axi_lite_awvalid = 1'b0;
        s_axi_lite_wdata = '0;
        s_axi_lite_wstrb = '0;
        s_axi_lite_wvalid = 1'b0;
        s_axi_lite_bready = 1'b0;
        s_axi_lite_araddr = '0;
        s_axi_lite_arprot = '0;
        s_axi_lite_arvalid = 1'b0;
        s_axi_lite_rready = 1'b0;
    endtask

    /// 写一笔寄存器：先写地址通道，再写数据通道
    task automatic axi_lite_write(input logic [31:0] addr, input logic [31:0] data,
                                  input logic [3:0] strb = 4'hf);
        @(negedge clock);
        s_axi_lite_awaddr = addr;
        s_axi_lite_awvalid = 1'b1;
        do @(posedge clock); while (!(s_axi_lite_awvalid && s_axi_lite_awready));
        @(negedge clock);
        s_axi_lite_awvalid = 1'b0;
        s_axi_lite_wdata = data;
        s_axi_lite_wstrb = strb;
        s_axi_lite_wvalid = 1'b1;
        do @(posedge clock); while (!(s_axi_lite_wvalid && s_axi_lite_wready));
        @(negedge clock);
        s_axi_lite_wvalid = 1'b0;
        s_axi_lite_bready = 1'b1;
        do @(posedge clock); while (!s_axi_lite_bvalid);
        if (s_axi_lite_bresp !== 2'b00) begin
            fail($sformatf("write 0x%08x response error: 0x%0x", addr, s_axi_lite_bresp));
        end
        @(negedge clock);
        s_axi_lite_bready = 1'b0;
        s_axi_lite_awaddr = '0;
        s_axi_lite_wdata = '0;
        s_axi_lite_wstrb = '0;
    endtask

    /// 写一笔寄存器：先写数据通道，再写地址通道（AXI 允许的另一种顺序）
    task automatic axi_lite_write_data_first(input logic [31:0] addr, input logic [31:0] data);
        @(negedge clock);
        s_axi_lite_wdata = data;
        s_axi_lite_wstrb = 4'hf;
        s_axi_lite_wvalid = 1'b1;
        do @(posedge clock); while (!(s_axi_lite_wvalid && s_axi_lite_wready));
        @(negedge clock);
        s_axi_lite_wvalid = 1'b0;
        s_axi_lite_awaddr = addr;
        s_axi_lite_awvalid = 1'b1;
        do @(posedge clock); while (!(s_axi_lite_awvalid && s_axi_lite_awready));
        @(negedge clock);
        s_axi_lite_awvalid = 1'b0;
        s_axi_lite_bready = 1'b1;
        do @(posedge clock); while (!s_axi_lite_bvalid);
        if (s_axi_lite_bresp !== 2'b00) begin
            fail($sformatf("write 0x%08x response error: 0x%0x", addr, s_axi_lite_bresp));
        end
        @(negedge clock);
        s_axi_lite_bready = 1'b0;
        s_axi_lite_awaddr = '0;
        s_axi_lite_wdata = '0;
        s_axi_lite_wstrb = '0;
    endtask

    /// 读一笔寄存器
    task automatic axi_lite_read(input logic [31:0] addr, output logic [31:0] data);
        @(negedge clock);
        s_axi_lite_araddr = addr;
        s_axi_lite_arvalid = 1'b1;
        s_axi_lite_rready = 1'b1;
        do @(posedge clock); while (!(s_axi_lite_arvalid && s_axi_lite_arready));
        @(negedge clock);
        s_axi_lite_arvalid = 1'b0;
        do @(posedge clock); while (!s_axi_lite_rvalid);
        data = s_axi_lite_rdata;
        if (s_axi_lite_rresp !== 2'b00) begin
            fail($sformatf("read 0x%08x response error: 0x%0x", addr, s_axi_lite_rresp));
        end
        @(negedge clock);
        s_axi_lite_rready = 1'b0;
        s_axi_lite_araddr = '0;
    endtask

    // ---------------- 测量辅助 ----------------
    /// 等待中断拉高
    task automatic wait_interrupt(input int unsigned max_cycles, input string name);
        int unsigned cycles = 0;
        while (interrupt !== 1'b1) begin
            @(posedge clock);
            cycles++;
            if (cycles > max_cycles) begin
                fail($sformatf("%s: interrupt timeout (%0d clock cycles)", name, max_cycles));
                return;
            end
        end
        $display("[%0t] %s：中断在 %0d 个主时钟周期后拉高", $time, name, cycles);
    endtask

    /// 轮询 STATUS.busy 直到测量结束（不看中断）
    task automatic wait_not_busy(input int unsigned max_reads, input string name);
        logic [31:0] value;
        int unsigned reads = 0;
        do begin
            axi_lite_read(REG_STATUS, value);
            reads++;
            if (reads > max_reads) begin
                fail($sformatf("%s: busy timeout (%0d reads)", name, max_reads));
                return;
            end
        end while (value[STATUS_BUSY] === 1'b1);
        $display("[%0t] %s：测量结束（轮询 STATUS %0d 次）", $time, name, reads);
    endtask

    /// 设置闸门时间、保留中断使能位并启动一次测量，然后等到本次测量的完成中断。
    /// 只清中断挂起位，不动 resultValid：是否「结果已被读走」由用例显式调用 ack_result 决定。
    task automatic run_measurement(input logic [31:0] gate_target, input string name);
        // 先清掉上一次的中断挂起：否则 wait_interrupt 会因为旧中断仍为高而立即返回，
        // 后续操作会与仍在进行中的测量撞车（启动被 busy 忽略）
        axi_lite_write(REG_IRQ_STATUS, 32'h1 << IRQ_PENDING);
        axi_lite_write(REG_GATE_TARGET, gate_target);
        // CTRL 的 bit1 为中断使能（普通读写位），bit0 为写 1 启动（自清脉冲）
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE) | (1 << CTRL_START));
        wait_interrupt(4 * gate_target + 200, name);
    endtask

    /// 表示「结果已被软件读走」：同时清中断挂起位与 resultValid
    task automatic ack_result();
        axi_lite_write(REG_IRQ_STATUS, (1 << IRQ_PENDING) | (1 << IRQ_RESULT_ACK));
    endtask

    /// 读走最新一条测量结果：数据通道就是这 4 个只读寄存器
    task automatic read_result(output logic [31:0] gate_cnt, output logic [31:0] edge_cnt,
                               output logic [31:0] phase_cnt, output logic [31:0] seq);
        axi_lite_read(REG_RESULT_GATE, gate_cnt);
        axi_lite_read(REG_RESULT_EDGE, edge_cnt);
        axi_lite_read(REG_RESULT_PHASE, phase_cnt);
        axi_lite_read(REG_RESULT_SEQ, seq);
    endtask

    /// 用测量模型校验一条结果（容限见 .agents/skills/dpm-measurement-model）
    task automatic check_record(input logic [31:0] gate_cnt, input logic [31:0] edge_cnt,
                                input logic [31:0] phase_cnt, input logic [31:0] seq,
                                input logic [31:0] gate_target, input logic [31:0] expect_seq,
                                input string name,
                                input real signal_period = SIGNAL_PERIOD,
                                input real phase_offset = PHASE_OFFSET);
        int unsigned expected_edge_cnt;
        int unsigned expected_phase_cnt;
        int unsigned edge_diff;
        int unsigned phase_diff;
        int unsigned gate_tolerance;
        $display("[%0t] %s：gateCnt=%0d edgeCnt=%0d phaseCnt=%0d seq=%0d", $time, name, gate_cnt, edge_cnt,
                 phase_cnt, seq);
        // 闸门计数：窗口要等到被测信号的下一个上升沿才关闭，因此最多多一个信号周期；
        // 再加上停止握手与结果锁存需要的几个低速周期（模型见 dpm-measurement-model）
        gate_tolerance = $rtoi($ceil(signal_period / CLOCK_PERIOD)) + 8;
        check_range(gate_cnt, gate_target, gate_target + gate_tolerance, {name, " gateCnt"});
        // 边沿计数：窗口长度除以参考信号周期，窗口边界上允许 ±2
        expected_edge_cnt = (gate_cnt * CLOCK_PERIOD) / signal_period;
        edge_diff = (edge_cnt > expected_edge_cnt) ? (edge_cnt - expected_edge_cnt)
                                                   : (expected_edge_cnt - edge_cnt);
        check_range(edge_diff, 0, 2, {name, " edgeCnt error"});
        // 相位计数：每个参考周期累计 offset / T_dsp 个高速周期，容限为每周期 1 个高速周期
        expected_phase_cnt = (edge_cnt * phase_offset) / DSP_CLOCK_PERIOD;
        phase_diff = (phase_cnt > expected_phase_cnt) ? (phase_cnt - expected_phase_cnt)
                                                      : (expected_phase_cnt - phase_cnt);
        check_range(phase_diff, 0, edge_cnt, {name, " phaseCnt error"});
        check_equal_u32(seq, expect_seq, {name, " seq"});
    endtask

    // ---------------- 用例主体 ----------------
    logic [31:0] read_value;
    logic [31:0] snapshot[4];

    initial begin : main
        // 初值：AXI 信号全部拉低，复位保持到第一次检查结束
        axi_lite_init();
        resetn = 1'b0;

        // ---- 1. 复位期间：无中断，且 AXI 从口不接受任何传输 ----
        // 从设备在复位期间必须拉低所有 ready：若此时接受请求却无法给出响应，主机会永久挂起
        repeat (8) @(negedge clock);
        check_bit(interrupt, 1'b0, "interrupt during reset");
        check_bit(s_axi_lite_awready, 1'b0, "s_axi_lite_awready during reset");
        check_bit(s_axi_lite_wready, 1'b0, "s_axi_lite_wready during reset");
        check_bit(s_axi_lite_arready, 1'b0, "s_axi_lite_arready during reset");
        check_bit(s_axi_lite_bvalid, 1'b0, "s_axi_lite_bvalid during reset");
        check_bit(s_axi_lite_rvalid, 1'b0, "s_axi_lite_rvalid during reset");
        // 复位期间持续发请求，从口仍不得握手
        s_axi_lite_arvalid = 1'b1;
        s_axi_lite_awvalid = 1'b1;
        repeat (4) @(negedge clock);
        check_bit(s_axi_lite_arready, 1'b0, "s_axi_lite_arready under request during reset");
        check_bit(s_axi_lite_awready, 1'b0, "s_axi_lite_awready under request during reset");
        @(negedge clock);
        s_axi_lite_arvalid = 1'b0;
        s_axi_lite_awvalid = 1'b0;

        // ---- 2. 复位释放：寄存器为默认值 ----
        @(negedge clock);
        resetn = 1'b1;
        repeat (8) @(negedge clock);
        check_bit(s_axi_lite_arready, 1'b1, "s_axi_lite_arready after reset release");
        axi_lite_read(REG_STATUS, read_value);
        check_equal_u32(read_value, 32'h0, "STATUS after reset release");
        axi_lite_read(REG_VERSION, read_value);
        check_equal_u32(read_value, VERSION_VALUE, "VERSION");
        axi_lite_read(REG_CAPS, read_value);
        check_equal_u32(read_value, CAPS_VALUE, "CAPS");
        axi_lite_read(REG_GATE_TARGET, read_value);
        check_equal_u32(read_value, 32'h0, "GATE_TARGET after reset");
        axi_lite_read(REG_LOST_COUNT, read_value);
        check_equal_u32(read_value, 32'h0, "LOST_COUNT after reset");
        axi_lite_read(REG_IRQ_STATUS, read_value);
        check_equal_u32(read_value, 32'h0, "IRQ_STATUS after reset");
        axi_lite_read(REG_CTRL, read_value);
        check_equal_u32(read_value, 32'h0, "CTRL after reset");
        axi_lite_read(REG_SIG_CTRL, read_value);
        check_equal_u32(read_value, 32'h0, "SIG_CTRL after reset");
        // 默认选外部输入，因此 sourceReady 为 1（外部通路在配置上总是可用的）；此时 MMCM 还没锁定
        axi_lite_read(REG_SIG_STATUS, read_value);
        check_equal_u32(read_value, 32'h8, "SIG_STATUS after reset");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_equal_u32(snapshot[0], 32'h0, "RESULT_GATE after reset");
        check_equal_u32(snapshot[1], 32'h0, "RESULT_EDGE after reset");
        check_equal_u32(snapshot[2], 32'h0, "RESULT_PHASE after reset");
        check_equal_u32(snapshot[3], 32'h0, "RESULT_SEQ after reset");

        // ---- 3. 写 GATE_TARGET（写数据通道先到）并读回 ----
        axi_lite_write_data_first(REG_GATE_TARGET, GATE_TARGET_SLOW);
        axi_lite_read(REG_GATE_TARGET, read_value);
        check_equal_u32(read_value, GATE_TARGET_SLOW, "GATE_TARGET readback");

        // ---- 4. 字节选通：只改写被 wstrb 选中的字节 ----
        axi_lite_write(REG_GATE_TARGET, 32'h0000_0000);
        axi_lite_write(REG_GATE_TARGET, 32'h1122_3344, 4'b0001);
        axi_lite_read(REG_GATE_TARGET, read_value);
        check_equal_u32(read_value, 32'h0000_0044, "GATE_TARGET strobe byte0");
        axi_lite_write(REG_GATE_TARGET, 32'h5566_7788, 4'b0011);
        axi_lite_read(REG_GATE_TARGET, read_value);
        check_equal_u32(read_value, 32'h0000_7788, "GATE_TARGET strobe byte0-1");
        axi_lite_write(REG_GATE_TARGET, GATE_TARGET_SLOW);

        // ---- 5. 启动一次测量：busy 与中断时序 ----
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE));
        axi_lite_read(REG_CTRL, read_value);
        check_equal_u32(read_value, (1 << CTRL_IRQ_ENABLE), "CTRL readback (irq enable)");
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE) | (1 << CTRL_START));
        repeat (2) @(negedge clock);
        axi_lite_read(REG_STATUS, read_value);
        check_bit(read_value[STATUS_BUSY], 1'b1, "STATUS.busy after start");
        check_bit(read_value[STATUS_RESULT], 1'b0, "STATUS.resultValid after start");
        check_bit(read_value[STATUS_IRQ], 1'b0, "STATUS.irq after start");
        check_bit(interrupt, 1'b0, "interrupt while busy");
        wait_interrupt(4 * GATE_TARGET_SLOW + 200, "measurement 1");
        check_bit(interrupt, 1'b1, "interrupt after measurement");
        axi_lite_read(REG_STATUS, read_value);
        check_bit(read_value[STATUS_BUSY], 1'b0, "STATUS.busy after measurement");
        check_bit(read_value[STATUS_RESULT], 1'b1, "STATUS.resultValid after measurement");
        check_bit(read_value[STATUS_IRQ], 1'b1, "STATUS.irq after measurement");
        check_bit(read_value[STATUS_LOST], 1'b0, "STATUS.lost after first measurement");
        axi_lite_read(REG_LOST_COUNT, read_value);
        check_equal_u32(read_value, 32'h0, "LOST_COUNT after first measurement");

        // ---- 6. 读 4 个结果寄存器并校验（GPIO 式数据通道） ----
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_record(snapshot[0], snapshot[1], snapshot[2], snapshot[3], GATE_TARGET_SLOW, 0,
                     "measurement 1");

        // ---- 7. 清中断：resultValid 保持，结果寄存器不被改动；只读寄存器写忽略 ----
        axi_lite_write(REG_IRQ_STATUS, 32'h1 << IRQ_PENDING);
        check_bit(interrupt, 1'b0, "interrupt after clear");
        axi_lite_read(REG_IRQ_STATUS, read_value);
        check_equal_u32(read_value, 32'h2, "IRQ_STATUS readback (resultValid pending)");
        axi_lite_read(REG_STATUS, read_value);
        check_bit(read_value[STATUS_IRQ], 1'b0, "STATUS.irq after clear");
        check_bit(read_value[STATUS_RESULT], 1'b1, "STATUS.resultValid kept after irq clear");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_equal_u32(snapshot[3], 32'h0, "RESULT_SEQ kept after irq clear");
        axi_lite_write(REG_RESULT_GATE, 32'hdead_beef);
        axi_lite_write(REG_VERSION, 32'hdead_beef);
        axi_lite_read(REG_RESULT_GATE, read_value);
        check_equal_u32(read_value, snapshot[0], "RESULT_GATE ignores write");
        axi_lite_read(REG_VERSION, read_value);
        check_equal_u32(read_value, VERSION_VALUE, "VERSION ignores write");

        // ---- 8. 第二次测量：更短的闸门时间，结果寄存器被更新 ----
        ack_result();
        axi_lite_read(REG_STATUS, read_value);
        check_equal_u32(read_value, 32'h0, "STATUS after ack");
        run_measurement(GATE_TARGET_FAST, "measurement 2");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_record(snapshot[0], snapshot[1], snapshot[2], snapshot[3], GATE_TARGET_FAST, 1,
                     "measurement 2");
        ack_result();

        // ---- 9. 结果只保留最新一条：连续 3 次测量不读走，旧结果被覆盖并计入丢失 ----
        run_measurement(GATE_TARGET_A, "measurement A");
        run_measurement(GATE_TARGET_B, "measurement B");
        run_measurement(GATE_TARGET_C, "measurement C");
        axi_lite_read(REG_STATUS, read_value);
        check_bit(read_value[STATUS_RESULT], 1'b1, "STATUS.resultValid after overwrites");
        check_bit(read_value[STATUS_LOST], 1'b1, "STATUS.lost after overwrites");
        axi_lite_read(REG_LOST_COUNT, read_value);
        check_equal_u32(read_value, 32'h2, "LOST_COUNT after two overwrites");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_record(snapshot[0], snapshot[1], snapshot[2], snapshot[3], GATE_TARGET_C, 4,
                     "latest result after overwrites");

        // ---- 10. 清零（CTRL.clear）：结果寄存器与标志归零，序号重新计数 ----
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE) | (1 << CTRL_CLEAR));
        repeat (2) @(negedge clock);
        check_bit(interrupt, 1'b0, "interrupt after clear");
        axi_lite_read(REG_STATUS, read_value);
        check_equal_u32(read_value, 32'h0, "STATUS after clear");
        axi_lite_read(REG_LOST_COUNT, read_value);
        check_equal_u32(read_value, 32'h0, "LOST_COUNT after clear");
        axi_lite_read(REG_IRQ_STATUS, read_value);
        check_equal_u32(read_value, 32'h0, "IRQ_STATUS after clear");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_equal_u32(snapshot[0], 32'h0, "RESULT_GATE after clear");
        check_equal_u32(snapshot[3], 32'h0, "RESULT_SEQ after clear");

        // ---- 11. 中断使能门控：不使能中断时测量完成不拉高中断，但挂起位仍置位 ----
        axi_lite_write(REG_GATE_TARGET, GATE_TARGET_FAST);
        axi_lite_write(REG_CTRL, (1 << CTRL_START));
        repeat (2) @(negedge clock);
        axi_lite_read(REG_STATUS, read_value);
        check_bit(read_value[STATUS_BUSY], 1'b1, "STATUS.busy with irq disabled");
        wait_not_busy(400, "measurement with irq disabled");
        check_bit(interrupt, 1'b0, "interrupt with irq disabled");
        axi_lite_read(REG_STATUS, read_value);
        check_bit(read_value[STATUS_RESULT], 1'b1, "STATUS.resultValid with irq disabled");
        check_bit(read_value[STATUS_IRQ], 1'b1, "STATUS.irq pending with irq disabled");
        axi_lite_read(REG_RESULT_SEQ, read_value);
        check_equal_u32(read_value, 32'h0, "RESULT_SEQ after clear");
        // 中断是电平：使能之后挂起的中断立刻出现在引脚上
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE));
        check_bit(interrupt, 1'b1, "interrupt after irq enable");
        ack_result();
        check_bit(interrupt, 1'b0, "interrupt after ack");

        // ---- 12. 测量期间改写 GATE_TARGET 不影响本次测量 ----
        // 启动时把目标闸门计数锁存进 activeTargetGate，测量期间用的是锁存值
        axi_lite_write(REG_GATE_TARGET, GATE_TARGET_SLOW);
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE) | (1 << CTRL_START));
        axi_lite_read(REG_STATUS, read_value);
        check_bit(read_value[STATUS_BUSY], 1'b1, "STATUS.busy before gate change");
        axi_lite_write(REG_GATE_TARGET, GATE_TARGET_TINY);
        wait_interrupt(4 * GATE_TARGET_SLOW + 200, "measurement with gate change");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_record(snapshot[0], snapshot[1], snapshot[2], snapshot[3], GATE_TARGET_SLOW, 1,
                     "gate kept during measurement");
        ack_result();

        // ---- 13. busy 期间重复写 start 被忽略：只产生一条结果 ----
        axi_lite_write(REG_IRQ_STATUS, 32'h1 << IRQ_PENDING);
        axi_lite_write(REG_GATE_TARGET, GATE_TARGET_SLOW);
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE) | (1 << CTRL_START));
        axi_lite_write(REG_GATE_TARGET, GATE_TARGET_FAST);
        axi_lite_write(REG_CTRL, (1 << CTRL_IRQ_ENABLE) | (1 << CTRL_START));
        wait_interrupt(4 * GATE_TARGET_SLOW + 200, "double start");
        axi_lite_read(REG_RESULT_SEQ, read_value);
        check_equal_u32(read_value, 32'h2, "RESULT_SEQ after double start");
        axi_lite_read(REG_LOST_COUNT, read_value);
        check_equal_u32(read_value, 32'h0, "LOST_COUNT after double start");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_record(snapshot[0], snapshot[1], snapshot[2], snapshot[3], GATE_TARGET_SLOW, 2,
                     "only one result from double start");


        // ---- 14. 片内信号源：配置寄存器 → 跨时钟域 → 测量，以及外部/内部输入切换 ----
        // 源信号（MMCM 的两路输出）周期 640ns、相位差 160ns（90°），直接作为被测信号。
        // 外部引脚上仍然驱动 400ns / 120ns 的信号：选中片内源时测量必须跟随片内源，切回外部
        // 输入后必须跟随外部信号——两条通路的理论值不同，结论才有区分度。
        // 使能片内源并切换到片内源
        axi_lite_write(REG_SIG_CTRL, (1 << SIG_ENABLE) | (1 << SIG_SELECT));
        axi_lite_read(REG_SIG_CTRL, read_value);
        check_equal_u32(read_value, 32'h3, "SIG_CTRL readback (internal)");
        axi_lite_read(REG_SIG_STATUS, read_value);
        check_bit(read_value[SIG_STATUS_ENABLE], 1'b1, "SIG_STATUS.enable (internal)");
        check_bit(read_value[SIG_STATUS_SELECT], 1'b1, "SIG_STATUS.srcSelect (internal)");
        check_bit(read_value[SIG_STATUS_LOCKED], 1'b1, "SIG_STATUS.srcLocked (internal)");
        check_bit(read_value[SIG_STATUS_READY], 1'b1, "SIG_STATUS.sourceReady (internal)");

        run_measurement(GATE_TARGET_SLOW, "measurement with internal source");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_record(snapshot[0], snapshot[1], snapshot[2], snapshot[3], GATE_TARGET_SLOW, 3,
                     "internal source measurement", INTERNAL_PERIOD, INTERNAL_OFFSET);
        ack_result();

        // 切回外部输入：同一寄存器里的选择位清 0，外部引脚上的信号重新成为被测对象
        axi_lite_write(REG_SIG_CTRL, 32'h0);
        axi_lite_read(REG_SIG_CTRL, read_value);
        check_equal_u32(read_value, 32'h0, "SIG_CTRL readback (external)");
        // 切回外部输入：选择位清 0，MMCM 仍锁定（b2 = 1），sourceReady 因选外部输入恒为 1
        axi_lite_read(REG_SIG_STATUS, read_value);
        check_equal_u32(read_value, 32'hC, "SIG_STATUS after switching to external");
        run_measurement(GATE_TARGET_SLOW, "measurement with external input");
        read_result(snapshot[0], snapshot[1], snapshot[2], snapshot[3]);
        check_record(snapshot[0], snapshot[1], snapshot[2], snapshot[3], GATE_TARGET_SLOW, 4,
                     "external input measurement after switching back");
        ack_result();

        // ---- 15. 复位再次生效：中断清除、状态归零、结果与信号源配置回默认值 ----

        @(negedge clock);
        resetn = 1'b0;
        repeat (4) @(negedge clock);
        check_bit(interrupt, 1'b0, "interrupt after second reset");
        check_bit(s_axi_lite_awready, 1'b0, "s_axi_lite_awready after second reset");
        check_bit(s_axi_lite_arready, 1'b0, "s_axi_lite_arready after second reset");
        @(negedge clock);
        resetn = 1'b1;
        repeat (8) @(negedge clock);
        axi_lite_read(REG_STATUS, read_value);
        check_equal_u32(read_value, 32'h0, "STATUS after second reset");
        axi_lite_read(REG_GATE_TARGET, read_value);
        check_equal_u32(read_value, 32'h0, "GATE_TARGET after second reset");
        axi_lite_read(REG_RESULT_SEQ, read_value);
        check_equal_u32(read_value, 32'h0, "RESULT_SEQ after second reset");
        axi_lite_read(REG_VERSION, read_value);
        check_equal_u32(read_value, VERSION_VALUE, "VERSION after second reset");
        axi_lite_read(REG_SIG_CTRL, read_value);
        check_equal_u32(read_value, 32'h0, "SIG_CTRL after second reset");
        // 复位同样把 MMCM 的锁定指示拉低（仿真模型：复位期间 locked = 0）
        axi_lite_read(REG_SIG_STATUS, read_value);
        check_equal_u32(read_value, 32'h8, "SIG_STATUS after second reset");

        // ---- 汇总 ----
        if (error_count == 0) begin
            $display("==== 测试通过：DigitalPhaseMeterTop 寄存器数据通道 ====");
        end else begin
            $fatal(1, "==== 测试失败：共 %0d 处错误 ====", error_count);
        end
        $finish;
    end

    // 看门狗：正常用例在数百微秒内结束
    initial begin : watchdog
        #20ms;
        $fatal(1, "仿真超时");
    end

endmodule

`default_nettype wire
