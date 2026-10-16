// ============================================================================
// 【已作废·仅作历史记录】片内信号源 clk_wiz_src 的 DRP 位域语义实测激励。
//
// 本文件针对的是**已被方案 A 取代**的 `clk_wiz_src` IP（它已从 `create_bd.tcl` 删除），
// 因此**现在跑不起来**（`run_drp_tb.sh` 找不到 IP 源目录）。它的产出是
// `docs/verify-drp.md` 与 `docs/verify-report.md` §3 的那批实测数据（相位字不生效的判决性证据），
// 保留下来是为了让那份结论可追溯，**不要再基于它做验证**。
// 当前方案的联调激励见 `sim/tb_src_handoff.sv` + `sim/run_handoff_tb.sh`。
//
// 片内信号源 clk_wiz_src（dpm_system_clk_wiz_src_0：MMCM + 动态重配置）的
// DRP 位域语义实测激励。
//
// 被测对象是 BD 生成的时钟向导 IP 本身（`dpm_system_clk_wiz_src_0`），不是它的替代模型：
// 参考时钟 50MHz、AXI4-Lite 时钟 50MHz，通过偏移 0x200 起的寄存器文件写 MMCM 配置，
// 写 0x25C（Load）触发重配置，然后用 1ps 精度的时间戳实测两路输出的频率与相位差。
//
// 要回答的问题（对应任务书与 Lead 的疑点）：
//   1. 0x200 起各字复位后到底是什么（寄存器文件布局的实测 dump）；
//   2. 写「相位字段」会不会改变 clk_out1 / clk_out2 的相位；
//   3. 分频字段是 D 本体还是 2D；
//   4. Load（0x25C）的触发位与 SADDR 位（静态配置区 / 寄存器文件动态配置区）。
//
// 方法：
//   - clk_in1 = s_axi_aclk = 50MHz（半周期 10000ps）；
//   - 复位释放 → 等 locked → 稳定后测量；之后每轮：写配置字 → 写 Load → 等 MMCM
//     重新锁定（重配置期间 MMCM 被复位，locked 会先落再起）→ 跳过放行瞬态 → 测量；
//   - 测量窗口内用 `$realtime`（1ps 分辨率）取两路输出上升沿：
//       周期   = 相邻同沿时间差的平均（多个周期取平均，抑制 MMCM 的分频抖动）；
//       相位差 = 某次 clk_out1 上升沿 → 其后第一个 clk_out2 上升沿的间隔，多周期平均；
//     delay_deg_milli = 360000 * delay_ps / t_out2_ps（按实测周期换算成毫度）。
//
// 机器可读输出（stdout 与日志同时有，格式固定）：
//   DRP_RD_RESET off=0x208 val=0x00000028           复位后寄存器文件 dump
//   DRP_RD_STATUS off=0x004 val=0x........ locked=. 状态寄存器（srdy/locked）读取
//   DRP_LOAD_PROBE val=0x00000007 ...               Load 值语义
//   DRP_MEAS div=<D> phase_field=<F> t_out1_ps=<整数> t_out2_ps=<整数> delay_ps=<整数> locked=<0/1>
//   DRP_PHASE_DEG div=.. phase_field=.. delay_ps=.. delay_deg_milli=..
//   DRP_PHASEW div=.. phase_field=.. ...            只写相位字（0x218）的结果
//   DRP_ASYM div=<CLKOUT0 字的字段值> phase_field=<CLKOUT1 字的字段值> ...（非对称探针，两字 D 都是 40）
//   DRP_TB_DONE lines=<DRP_MEAS 行数>
//
// 运行方式见 sim/run_drp_tb.sh；结论见 spec/drp_semantics.json 与 docs/verify-drp.md。
// ============================================================================

`timescale 1ps/1ps
`default_nettype none

module tb_clk_wiz_src_drp;

    // ---------------- 参数 ----------------
    // 50MHz：半周期 10ns = 10000ps
    localparam int unsigned CLK_HALF_PS = 10_000;
    // 每个测量点的窗口长度（clk_out1 上升沿数）与锁后稳定沿数
    localparam int unsigned WIN_EDGES = 32;
    localparam int unsigned SETTLE_EDGES = 16;
    // 看门狗：正常一轮约 8µs，全程 60 轮上下；3ms 足够宽（写成 64 位常量，避免 32 位溢出）
    localparam time WATCHDOG_PS = 64'd3_000_000_000;
    // 调试开关：置 1 后每笔 AXI 读写与每次等待都打时间戳（排查挂死用）
    localparam bit DEBUG_TRACE = 1'b0;

    // 寄存器文件偏移（字偏移 = 0x200 + 4*i）
    localparam logic [10:0] OFF_CLKFBOUT = 11'h200;  // CLKFBOUT 分频字
    localparam logic [10:0] OFF_CLKOUT0 = 11'h208;   // CLKOUT0（clk_out1）分频字
    localparam logic [10:0] OFF_CLKOUT1 = 11'h214;   // CLKOUT1（clk_out2）分频字
    localparam logic [10:0] OFF_PHASE1 = 11'h218;    // CLKOUT1 相位字（毫度，本 IP 未连接）
    localparam logic [10:0] OFF_LOAD = 11'h25C;      // Load：bit0 触发、bit1 SADDR
    localparam logic [10:0] OFF_DUMP_LO = 11'h200;
    localparam logic [10:0] OFF_DUMP_HI = 11'h228;

    // ---------------- 时钟与复位 ----------------
    logic clk_in1 = 1'b0;
    logic s_axi_aclk = 1'b0;
    logic s_axi_aresetn = 1'b0;

    always #(CLK_HALF_PS) clk_in1 = ~clk_in1;
    always #(CLK_HALF_PS) s_axi_aclk = ~s_axi_aclk;

    // ---------------- AXI4-Lite 主机 ----------------
    logic [10:0] s_axi_awaddr = '0;
    logic s_axi_awvalid = 1'b0;
    logic s_axi_awready;
    logic [31:0] s_axi_wdata = '0;
    logic [3:0] s_axi_wstrb = '0;
    logic s_axi_wvalid = 1'b0;
    logic s_axi_wready;
    logic [1:0] s_axi_bresp;
    logic s_axi_bvalid;
    logic s_axi_bready = 1'b0;
    logic [10:0] s_axi_araddr = '0;
    logic s_axi_arvalid = 1'b0;
    logic s_axi_arready;
    logic [31:0] s_axi_rdata;
    logic [1:0] s_axi_rresp;
    logic s_axi_rvalid;
    logic s_axi_rready = 1'b0;

    // ---------------- 被测对象 ----------------
    logic clk_out1;
    logic clk_out2;
    logic locked;

    dpm_system_clk_wiz_src_0 dut (
        .s_axi_aclk    (s_axi_aclk),
        .s_axi_aresetn (s_axi_aresetn),
        .s_axi_awaddr  (s_axi_awaddr),
        .s_axi_awvalid (s_axi_awvalid),
        .s_axi_awready (s_axi_awready),
        .s_axi_wdata   (s_axi_wdata),
        .s_axi_wstrb   (s_axi_wstrb),
        .s_axi_wvalid  (s_axi_wvalid),
        .s_axi_wready  (s_axi_wready),
        .s_axi_bresp   (s_axi_bresp),
        .s_axi_bvalid  (s_axi_bvalid),
        .s_axi_bready  (s_axi_bready),
        .s_axi_araddr  (s_axi_araddr),
        .s_axi_arvalid (s_axi_arvalid),
        .s_axi_arready (s_axi_arready),
        .s_axi_rdata   (s_axi_rdata),
        .s_axi_rresp   (s_axi_rresp),
        .s_axi_rvalid  (s_axi_rvalid),
        .s_axi_rready  (s_axi_rready),
        .clk_out1      (clk_out1),
        .clk_out2      (clk_out2),
        .locked        (locked),
        .clk_in1       (clk_in1)
    );

    // ---------------- 测量统计 ----------------
    bit meas_en = 1'b0;
    real t_out1_last = 0.0;
    real t_out2_last = 0.0;
    real sum_t_out1 = 0.0;
    real sum_t_out2 = 0.0;
    real sum_delay = 0.0;
    int n_t_out1 = 0;
    int n_t_out2 = 0;
    int n_delay = 0;
    bit delay_pending = 1'b0;
    real t_ref = 0.0;

    always @(posedge clk_out1) begin
        if (meas_en) begin
            if (n_t_out1 > 0) sum_t_out1 += $realtime - t_out1_last;
            t_out1_last = $realtime;
            n_t_out1     = n_t_out1 + 1;
            // 记录一个参考沿，等它的下一个 clk_out2 上升沿
            delay_pending = 1'b1;
            t_ref         = $realtime;
        end
    end

    always @(posedge clk_out2) begin
        if (meas_en) begin
            if (n_t_out2 > 0) sum_t_out2 += $realtime - t_out2_last;
            t_out2_last = $realtime;
            n_t_out2     = n_t_out2 + 1;
            if (delay_pending) begin
                sum_delay     = sum_delay + ($realtime - t_ref);
                n_delay       = n_delay + 1;
                delay_pending = 1'b0;
            end
        end
    end

    int unsigned meas_lines = 0;

    // ---------------- 通用小函数 ----------------
    /// 四舍五入成整数 ps
    function automatic int rnd_ps(input real value);
        return $rtoi(value + 0.5);
    endfunction

    /// 由实测周期与延迟换算毫度（中间量用 64 位，避免 delay*360000 溢出 32 位）
    function automatic int deg_milli(input int delay_ps, input int period_ps);
        longint num;
        if (period_ps <= 0) return 0;
        num = longint'(delay_ps) * 64'd360_000 + period_ps / 2;
        return int'(num / period_ps);
    endfunction

    /// CLKFBOUT 字：[7:0] = DIVCLK_DIVIDE，[15:8] = CLKFBOUT_MULT
    function automatic logic [31:0] word_clkfbout(input int mult, input int divclk);
        return ((mult & 32'hff) << 8) | (divclk & 32'hff);
    endfunction

    /// 输出分频字：[7:0] = D，[17:8] = FRAC（CLKOUT0 用），[18] = FRAC_EN 由硬件自动置
    /// phase_field 按「相位在 bit[15:8]」的假设放进高字节，用于检验该假设。
    function automatic logic [31:0] word_clkout_div(input int div, input int phase_field);
        return (div & 32'hff) | ((phase_field & 32'h3ff) << 8);
    endfunction

    // ---------------- AXI4-Lite 读写 ----------------
    // 激励在下降沿驱动、在上升沿采样握手，握手后回到下降沿撤销信号。
    //
    // 注意（本 IP 与 dpm_top 的从口不同）：clk_wiz 的 axi_lite_ipif v1.01a 在
    // slave_attachment.vhd 里要求 `S_AXI_AWVALID='1' and S_AXI_WVALID='1'` 同时成立才离开
    // SM_IDLE，且 `S_AXI_AWREADY <= wr_done`、`S_AXI_WREADY <= wr_done` 由同一信号驱动。
    // 因此写必须**同时**举起 AWVALID 与 WVALID（先 AW 后 W 的顺序会死锁），
    // 两通道在同一拍完成握手。

    task automatic axi_write(input logic [10:0] addr, input logic [31:0] data);
        if (DEBUG_TRACE) $display("[%0t] AXI 写 0x%03x = 0x%08x ...", $realtime, addr, data);
        @(negedge s_axi_aclk);
        s_axi_awaddr  = addr;
        s_axi_awvalid = 1'b1;
        s_axi_wdata   = data;
        s_axi_wstrb   = 4'hf;
        s_axi_wvalid  = 1'b1;
        do @(posedge s_axi_aclk); while (!(s_axi_awvalid && s_axi_awready));
        @(negedge s_axi_aclk);
        s_axi_awvalid = 1'b0;
        s_axi_wvalid  = 1'b0;
        s_axi_bready  = 1'b1;
        do @(posedge s_axi_aclk); while (!s_axi_bvalid);
        if (s_axi_bresp !== 2'b00)
            $display("ERROR axi_write 0x%03x 返回 bresp=%0b", addr, s_axi_bresp);
        @(negedge s_axi_aclk);
        s_axi_bready  = 1'b0;
        s_axi_awaddr  = '0;
        s_axi_wdata   = '0;
        s_axi_wstrb   = '0;
        if (DEBUG_TRACE) $display("[%0t] AXI 写 0x%03x 完成", $realtime, addr);
    endtask

    task automatic axi_read(input logic [10:0] addr, output logic [31:0] data);
        @(negedge s_axi_aclk);
        s_axi_araddr  = addr;
        s_axi_arvalid = 1'b1;
        s_axi_rready  = 1'b1;
        do @(posedge s_axi_aclk); while (!(s_axi_arvalid && s_axi_arready));
        @(negedge s_axi_aclk);
        s_axi_arvalid = 1'b0;
        do @(posedge s_axi_aclk); while (!s_axi_rvalid);
        data = s_axi_rdata;
        if (s_axi_rresp !== 2'b00)
            $display("ERROR axi_read 0x%03x 返回 rresp=%0b", addr, s_axi_rresp);
        @(negedge s_axi_aclk);
        s_axi_rready  = 1'b0;
        s_axi_araddr  = '0;
    endtask

    // ---------------- 重新锁定与测量 ----------------
    /// 等 MMCM 重新锁定：重配置期间 MMCM 被内部复位，locked 先落再起。
    task automatic wait_relock();
        int unsigned guard = 0;
        if (DEBUG_TRACE) $display("[%0t] 等 locked 落下（当前 locked=%0b）", $realtime, locked);
        // 先等 locked 落下（最多 400 个慢周期）
        while (locked && guard < 400) begin
            @(posedge s_axi_aclk);
            guard = guard + 1;
        end
        // 再等 locked 拉高（MMCM 锁定约 1.5µs，最多 4000 个慢周期 = 80µs）
        guard = 0;
        while (!locked && guard < 4000) begin
            @(posedge s_axi_aclk);
            guard = guard + 1;
        end
        if (locked !== 1'b1) $fatal(1, "等 locked 超时：重配置后 MMCM 未锁定");
        if (DEBUG_TRACE) $display("[%0t] locked 已拉高", $realtime);
    endtask

    /// 让输出稳定：Safe Clock Startup 在 locked 后还要 8 拍才放行
    task automatic settle_outputs();
        if (DEBUG_TRACE) $display("[%0t] 等输出稳定（%0d 个 clk_out1 沿）", $realtime, SETTLE_EDGES);
        repeat (SETTLE_EDGES) @(posedge clk_out1);
        if (DEBUG_TRACE) $display("[%0t] 输出已稳定", $realtime);
    endtask

    /// 开一个测量窗口并累加统计量
    task automatic measure_window();
        meas_en       = 1'b0;
        sum_t_out1    = 0.0;
        sum_t_out2    = 0.0;
        sum_delay     = 0.0;
        n_t_out1      = 0;
        n_t_out2      = 0;
        n_delay       = 0;
        delay_pending = 1'b0;
        t_out1_last   = 0.0;
        t_out2_last   = 0.0;
        @(negedge s_axi_aclk);
        meas_en = 1'b1;
        repeat (WIN_EDGES) @(posedge clk_out1);
        meas_en = 1'b0;
        if (n_t_out1 < 8 || n_t_out2 < 8 || n_delay < 8)
            $fatal(1, "测量窗口内输出沿太少：n_t_out1=%0d n_t_out2=%0d n_delay=%0d",
                   n_t_out1, n_t_out2, n_delay);
    endtask

    /// 测量并按固定格式打印（tag 用 ASCII，避免 xsim 破坏多字节字符）
    task automatic report_meas(input string tag, input int div, input int phase_field);
        int t1_ps;
        int t2_ps;
        int d_ps;
        begin
            measure_window();
            t1_ps = rnd_ps(sum_t_out1 / (n_t_out1 - 1));
            t2_ps = rnd_ps(sum_t_out2 / (n_t_out2 - 1));
            d_ps  = rnd_ps(sum_delay / n_delay);
            if (tag == "DRP_MEAS") begin
                $display("DRP_MEAS div=%0d phase_field=%0d t_out1_ps=%0d t_out2_ps=%0d delay_ps=%0d locked=%0b",
                         div, phase_field, t1_ps, t2_ps, d_ps, locked);
                $display("DRP_PHASE_DEG div=%0d phase_field=%0d delay_ps=%0d delay_deg_milli=%0d n_delay=%0d",
                         div, phase_field, d_ps, deg_milli(d_ps, t2_ps), n_delay);
                meas_lines = meas_lines + 1;
            end else begin
                $display("%s div=%0d phase_field=%0d t_out1_ps=%0d t_out2_ps=%0d delay_ps=%0d delay_deg_milli=%0d locked=%0b",
                         tag, div, phase_field, t1_ps, t2_ps, d_ps, deg_milli(d_ps, t2_ps), locked);
            end
        end
    endtask

    // ---------------- 看门狗 ----------------
    initial begin
        #(WATCHDOG_PS);
        $fatal(1, "看门狗超时：仿真超过 %0t ps 仍未结束", WATCHDOG_PS);
    end

    // ---------------- 主流程 ----------------
    int div_list[4];
    int phase_list[8];
    int pword_list[6];
    // 非对称探针：CLKOUT0 字与 CLKOUT1 字分别写入的 phase_field 值
    int asym_f0[5];
    int asym_f1[5];
    int i;
    int j;
    int div;
    int fld;
    int guard;
    logic [31:0] rd;

    initial begin
        div_list   = '{40, 80, 125, 96};
        phase_list = '{0, 1, 2, 4, 8, 22, 45, 90};
        pword_list = '{0, 22_500, 45_000, 90_000, 180_000, 270_000};
        asym_f0    = '{2, 4, 0, 0, 8};
        asym_f1    = '{0, 0, 4, 8, 0};

        $display("==== clk_wiz DRP 位域语义实测（clk_in1=50MHz, s_axi_aclk=50MHz）====");
        $display("DUT=dpm_system_clk_wiz_src_0（MMCM + 动态重配置）");

        // ---- 复位 ----
        s_axi_aresetn = 1'b0;
        repeat (20) @(negedge s_axi_aclk);
        s_axi_aresetn = 1'b1;
        wait_relock();
        settle_outputs();
        $display("---- 复位锁定后的初始状态 ----");
        report_meas("DRP_INIT", -1, -1);

        // ---- 状态寄存器：锁定位到底在哪个地址 ----
        // 生成代码里 program_status = {srdy, locked} 在 LOCAL_REG_READ_PROCESS 的
        // "10001000" 分支（即 CE1 → 偏移 0x004）；寄存器文件里的 word 31（0x27C）是空 RAM。
        axi_read(11'h004, rd);
        $display("DRP_RD_STATUS off=0x004 val=0x%08x locked=%0b（锁定后）", rd, locked);
        axi_read(11'h27c, rd);
        $display("DRP_RD_STATUS off=0x27c val=0x%08x（寄存器文件 word 31，非状态寄存器）", rd);

        // ---- 复位后寄存器文件 dump（寄存器文件布局的直接证据）----
        $display("---- 复位后 DRP 寄存器文件 dump（0x200 起，步长 4）----");
        for (i = 0; i <= ((OFF_DUMP_HI - OFF_DUMP_LO) >> 2); i = i + 1) begin
            axi_read(OFF_DUMP_LO + (i << 2), rd);
            $display("DRP_RD_RESET off=0x%03x val=0x%08x", OFF_DUMP_LO + (i << 2), rd);
        end

        // ---- Load（0x25C）语义探针 ----
        // 先把频率改成 D=96（10.4167MHz），再用不同的 Load 值观察是否真的重配置
        $display("---- Load（0x25C）触发位与 SADDR 探针 ----");
        axi_write(OFF_CLKFBOUT, word_clkfbout(20, 1));
        axi_write(OFF_CLKOUT0, word_clkout_div(96, 0));
        axi_write(OFF_CLKOUT1, word_clkout_div(96, 0));
        axi_write(OFF_PHASE1, 32'd0);
        // (1) 只写 0x02：bit0=0 → 不应触发任何重配置
        axi_write(OFF_LOAD, 32'h0000_0002);
        wait_relock();
        settle_outputs();
        report_meas("DRP_LOAD_PROBE val=0x00000002", 96, 0);
        // (2) 只写 0x00：不应触发
        axi_write(OFF_LOAD, 32'h0000_0000);
        wait_relock();
        settle_outputs();
        report_meas("DRP_LOAD_PROBE val=0x00000000", 96, 0);
        // (3) 写 0x01：bit0=1 触发、bit1=0 → SADDR=0，用状态 1（静态配置）重置
        axi_write(OFF_LOAD, 32'h0000_0001);
        wait_relock();
        settle_outputs();
        report_meas("DRP_LOAD_PROBE val=0x00000001", 96, 0);
        // (4) 写 0x03：bit0=1 触发、bit1=1 → SADDR=1，用寄存器文件（动态配置）
        axi_write(OFF_LOAD, 32'h0000_0003);
        // 重配置进行中读一次状态寄存器：locked 应为 0（两路输出此时停止）
        guard = 0;
        while (locked && guard < 200) begin
            @(posedge s_axi_aclk);
            guard = guard + 1;
        end
        axi_read(11'h004, rd);
        $display("DRP_RD_STATUS off=0x004 val=0x%08x locked=%0b（重配置中）", rd, locked);
        wait_relock();
        axi_read(11'h004, rd);
        $display("DRP_RD_STATUS off=0x004 val=0x%08x locked=%0b（重锁定后）", rd, locked);
        settle_outputs();
        report_meas("DRP_LOAD_PROBE val=0x00000003", 96, 0);
        // (5) 写 0x07：任务书给的值（bit2 无定义，行为应与 0x03 相同）
        axi_write(OFF_LOAD, 32'h0000_0007);
        wait_relock();
        settle_outputs();
        report_meas("DRP_LOAD_PROBE val=0x00000007", 96, 0);

        // ---- 主扫描矩阵：div × phase_field ----
        // 每轮把 CLKOUT0 字（clk_out1）与 CLKOUT1 字（clk_out2）都写成 D | (F<<8)，
        // 并按「相位字段」的假设把 F 也写进 0x218 的毫度相位字（本 IP 未连接，用于对照）。
        //
        // 注意矩阵本身**不能**区分「字段被忽略」与「字段把两路输出一起搬移」：
        // 两个字写同一个 F 时，即使字段真的是相位，两路之间的相位差也不变。
        // 非对称探针（下面一节）才是否定「相位字段」假设的判别实验。
        $display("---- 扫描矩阵：div × phase_field（Load=0x07）----");
        for (i = 0; i < 4; i = i + 1) begin
            for (j = 0; j < 8; j = j + 1) begin
                div = div_list[i];
                fld = phase_list[j];
                axi_write(OFF_CLKFBOUT, word_clkfbout(20, 1));
                axi_write(OFF_CLKOUT0, word_clkout_div(div, fld));
                axi_write(OFF_CLKOUT1, word_clkout_div(div, fld));
                axi_write(OFF_PHASE1, fld * 1000);  // 毫度相位字
                axi_write(OFF_LOAD, 32'h0000_0007);
                wait_relock();
                settle_outputs();
                report_meas("DRP_MEAS", div, fld);
                // 读回三个配置字，确认寄存器文件确实接受了写入（区分「没写进去」与「写进去但被忽略」）
                axi_read(OFF_CLKOUT0, rd);
                $display("DRP_REGCHK div=%0d phase_field=%0d w208=0x%08x", div, fld, rd);
                axi_read(OFF_CLKOUT1, rd);
                $display("DRP_REGCHK div=%0d phase_field=%0d w214=0x%08x", div, fld, rd);
                axi_read(OFF_PHASE1, rd);
                $display("DRP_REGCHK div=%0d phase_field=%0d w218=0x%08x", div, fld, rd);
            end
        end

        // ---- 非对称相位字段探针（判别实验）----
        // CLKOUT0 字与 CLKOUT1 字写**不同**的 phase_field，D 都取 40（周期 40ns，45° = 5ns）：
        //   候选 A（字段 = 度数/ps 延迟）：delay ≈ T − 2ps / T − 4ps / 4ps 等
        //   候选 B（字段 = 45° 的整数倍）：delay ≈ 30ns / 20ns / 20ns 等
        //   字段其实不被消费（本次实测预期）  ：delay 恒为 T/4 = 10ns（静态 90°）
        $display("---- 非对称相位字段探针：CLKOUT0 字与 CLKOUT1 字写不同的 phase_field ----");
        for (i = 0; i < 5; i = i + 1) begin
            axi_write(OFF_CLKFBOUT, word_clkfbout(20, 1));
            axi_write(OFF_CLKOUT0, word_clkout_div(40, asym_f0[i]));
            axi_write(OFF_CLKOUT1, word_clkout_div(40, asym_f1[i]));
            axi_write(OFF_PHASE1, asym_f1[i] * 1000);
            axi_write(OFF_LOAD, 32'h0000_0007);
            wait_relock();
            settle_outputs();
            report_meas("DRP_ASYM", asym_f0[i], asym_f1[i]);
        end

        // ---- 只写相位字（0x218，毫度）的探针 ----
        // 若相位字真的控制 clk_out2 的相位，delay_ps 应随毫度值变化。
        $display("---- 只写相位字 0x218（毫度）的探针，分频固定 D=40 ----");
        for (i = 0; i < 6; i = i + 1) begin
            axi_write(OFF_CLKFBOUT, word_clkfbout(20, 1));
            axi_write(OFF_CLKOUT0, word_clkout_div(40, 0));
            axi_write(OFF_CLKOUT1, word_clkout_div(40, 0));
            axi_write(OFF_PHASE1, pword_list[i]);
            axi_write(OFF_LOAD, 32'h0000_0007);
            wait_relock();
            settle_outputs();
            report_meas("DRP_PHASEW", 40, pword_list[i] / 1000);
        end

        // ---- 结束 ----
        $display("DRP_TB_DONE lines=%0d", meas_lines);
        if (meas_lines == 0) $fatal(1, "没有任何 DRP_MEAS 行，实验无效");
        $finish;
    end

endmodule

`default_nettype wire
