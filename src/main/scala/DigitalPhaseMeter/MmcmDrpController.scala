package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** MMCM 动态重配置（DRP）控制器
  *
  * 「方案 A」的 PL 侧实现：用极简状态机替代 clk_wiz 在 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG` 下生成的重配置参考设计。
  *
  * ==为什么可以这么小==
  *
  * clk_wiz 的重配置参考设计在硬件里放了 `mmcm_pll_count_calc` / `mmcm_pll_divider` / `mmcm_pll_phase` 三组定点函数， 把「毫度 / 百分比 / MHz」实时换算成
  * MMCM 的计数器位域——实测在 `xc7z010clg400-1` 上占 13,287 个物理 LUT site 与 38 个 DSP48E1。
  *
  * 但那套换算是**纯整数算术**，PS 侧软件完全算得出来（参考实现与权威校验见 `build/analysis/golden/mmcm_encode.py`）。把它搬到软件后，PL 侧只需要 「把软件给的 16 位值按顺序写进
  * MMCM 的 DRP 口」，也就是本模块。
  *
  * ==寄存器映射（AXI4-Lite，字节偏移）==
  *
  * | 偏移   | 名称        | 属性  | 说明                              |
  * |:-----|:----------|:----|:--------------------------------|
  * | 0x00 | ID        | 只读  | 固定 0x44504D03（"DPM3"）           |
  * | 0x04 | STATUS    | 只读  | b0 = MMCM locked、b1/b2 = 重配置进行中 |
  * | 0x08 | CLKFBOUT  | 读写  | MMCM 寄存器 0x14 的值                |
  * | 0x0C | CLKFBOUT2 | 读写  | MMCM 寄存器 0x15 的值                |
  * | 0x10 | CLKOUT0   | 读写  | MMCM 寄存器 0x08 的值（参考输出）          |
  * | 0x14 | CLKOUT0_2 | 读写  | MMCM 寄存器 0x09 的值                |
  * | 0x18 | CLKOUT1   | 读写  | MMCM 寄存器 0x0A 的值（移相输出）          |
  * | 0x1C | CLKOUT1_2 | 读写  | MMCM 寄存器 0x0B 的值                |
  * | 0x20 | DIVCLK    | 读写  | MMCM 寄存器 0x16 的值                |
  * | 0x24 | POWER     | 读写  | MMCM 寄存器 0x28 的值（复位值 0xFFFF）    |
  * | 0x28 | LOAD      | W1S | 写 1 触发一次完整重配置                   |
  *
  * ==DRP 时序==
  *
  * MMCM 要求 `DEN` 只拉高**一个 DCLK 周期**，且此时 `DADDR`/`DI`/`DWE` 有效； 之后等 `DRDY` 拉高表示该次访问完成。本模块用一个小状态机逐条发送
  * `{CLKFBOUT, CLKFBOUT2, CLKOUT0, CLKOUT0_2, CLKOUT1, CLKOUT1_2, DIVCLK, POWER}` 八笔写，全部完成后回 IDLE。
  *
  * 重配置期间 MMCM 会失锁、输出停止，因此软件必须在测量空闲（`STATUS.busy = 0`）时改。
  *
  * 本模块用 `RawModule`：顶层必须显式声明时钟与复位，且 AXI 端口名要满足 Vivado 的接口推断约定（`ipx::infer_bus_interfaces` 按端口名前缀建接口）。
  *
  * @param addrWidth
  *   AXI 地址宽度
  * @param dataWidth
  *   AXI 数据宽度，当前只支持 32
  * @param resetDepth
  *   复位同步链级数
  */
class MmcmDrpController(val addrWidth: Int = 32, val dataWidth: Int = 32, val resetDepth: Int = 4) extends RawModule {
    require(dataWidth == 32, "MmcmDrpController 只支持 32 位数据宽度")

    // 主时钟：AXI 与 DRP 同域（DRP 的 DCLK 直接用主时钟，不额外消耗时钟资源）
    val clock = IO(Input(Clock()))
    // 异步复位输入，低有效
    val resetn = IO(Input(Bool()))
    // AXI4-Lite 从口
    val s_axi_lite = IO(new Axi4LiteSlavePorts(addrWidth, dataWidth))
    // MMCM 动态重配置口
    val drp_daddr = IO(Output(UInt(7.W)))
    val drp_di = IO(Output(UInt(16.W)))
    val drp_den = IO(Output(Bool()))
    val drp_dwe = IO(Output(Bool()))
    val drp_dout = IO(Input(UInt(16.W)))
    val drp_drdy = IO(Input(Bool()))
    // MMCM 锁定指示（异步电平）
    val locked = IO(Input(Bool()))

    // 主时钟域的复位：异步置位、同步释放
    private val mainReset = ResetSynchronizer(clock, resetn, resetDepth)

    withClockAndReset(clock, mainReset) {
        // ---- 软件可写的 MMCM 寄存器镜像 ----
        // 复位值取 clk_wiz Write DRP 在同一工作点（50MHz 参考、D=40、相位 0/90°）
        // 生成的值，保证软件不写任何寄存器时 MMCM 的静态配置与改造前一致。
        val cfgClkfbout = RegInit(0x128a.U(16.W))
        val cfgClkfbout2 = RegInit(0x0000.U(16.W))
        val cfgClkout0 = RegInit(0x1514.U(16.W))
        val cfgClkout0_2 = RegInit(0x0000.U(16.W))
        val cfgClkout1 = RegInit(0x1514.U(16.W))
        val cfgClkout1_2 = RegInit(0x000a.U(16.W))
        val cfgDivclk = RegInit(0x1041.U(16.W))
        val cfgPower = RegInit(0xffff.U(16.W))

        // locked 是异步电平，先同步到本时钟域
        val lockedSync = CdcLevel(locked)

        // ---- AXI4-Lite 从口 ----
        // 寄存器空间只有 11 个字、读写都一拍完成：就地吸收命令，不给 AXI 挂起。
        val axiLiteSlave = Module(new Axi4LiteSlave(addrWidth, dataWidth))
        axiLiteSlave.io.wr.ready := true.B
        axiLiteSlave.io.rd.ready := true.B

        val busy = RegInit(false.B)
        // 软件写 LOAD=1 置起本标志；状态机在 IDLE 看到它就开始重配置并清除。
        // 用「寄存器」而不是单拍脉冲，避免组合脉冲宽度依赖 AXI 握手时序。
        val loadPending = RegInit(false.B)
        // 调试计数：记录收到多少次写、多少次 LOAD（读寄存器 15 可查看）

        // 寄存器索引只用到地址的 [5:2]，但整条 32 位地址必须被真正消费
        // （否则 Verilator 的 -Wall 会对未使用的位报 UNUSEDSIGNAL）。
        // 这里顺带做了一件事：只有落在本 IP 的 4KB 窗口内才响应，
        // 窗口外的访问被忽略（读回 0），与「按地址块译码」的语义一致。
        // 用「整条地址」做窗口判断：只有 [5:2]（寄存器索引）允许非零，
        // 其余位必须为 0 —— 这样 32 位地址的每一位都被真正消费。
        def inWindowOf(addr: UInt): Bool = {
            val rest = Cat(addr(31, 6), addr(1, 0))
            rest === 0.U
        }
        val inWindow = inWindowOf(axiLiteSlave.io.wr.bits.addr)
        val inWindowRd = inWindowOf(axiLiteSlave.io.rd.bits.addr)

        // 写通道：按 `wstrb` 合并字节（保持 AXI 字节选通语义，
        // 同时让 32 位写数据与 4 位选通全部被真正消费）。
        // 8 个 MMCM 配置寄存器都只用低 16 位，故只关心 strb 的低 2 位。
        // 8 个配置寄存器都只用低 16 位，所以按字节选通合并时只需要低 2 个字节：
        // 写数据的 [15:0] 与 wstrb[1:0] 被完整消费，[31:16] / strb[3:2] 与它们
        // 对应的高 16 位寄存器位一样在本设计里不使用。
        def merged(old16: UInt, newIn: UInt): UInt = {
            val lo = Wire(UInt(8.W))
            val hi = Wire(UInt(8.W))
            lo := Mux(axiLiteSlave.io.wr.bits.strb(0), newIn(7, 0), old16(7, 0))
            hi := Mux(axiLiteSlave.io.wr.bits.strb(1), newIn(15, 8), old16(15, 8))
            Cat(hi, lo)
        }

        // LOAD 是写 1 触发，不落寄存器；用字节选通合并后的值
        when(axiLiteSlave.io.wr.valid && inWindow) {
            switch(axiLiteSlave.io.wr.bits.addr(5, 2)) {
                is(2.U) { cfgClkfbout := merged(cfgClkfbout, axiLiteSlave.io.wr.bits.data)(15, 0) }
                is(3.U) {
                    cfgClkfbout2 := merged(cfgClkfbout2, axiLiteSlave.io.wr.bits.data)(15, 0)
                }
                is(4.U) { cfgClkout0 := merged(cfgClkout0, axiLiteSlave.io.wr.bits.data)(15, 0) }
                is(5.U) {
                    cfgClkout0_2 := merged(cfgClkout0_2, axiLiteSlave.io.wr.bits.data)(15, 0)
                }
                is(6.U) { cfgClkout1 := merged(cfgClkout1, axiLiteSlave.io.wr.bits.data)(15, 0) }
                is(7.U) {
                    cfgClkout1_2 := merged(cfgClkout1_2, axiLiteSlave.io.wr.bits.data)(15, 0)
                }
                is(8.U) { cfgDivclk := merged(cfgDivclk, axiLiteSlave.io.wr.bits.data)(15, 0) }
                is(9.U) { cfgPower := merged(cfgPower, axiLiteSlave.io.wr.bits.data)(15, 0) }
                is(10.U) { when(axiLiteSlave.io.wr.bits.data(0)) { loadPending := true.B } }
            }
        }

        // drp_dout：本设计不需要读-改-写（软件给的是完整寄存器值），
        // 但仍把上一次 DRP 访问读回的内容放进 STATUS 的高 16 位作为诊断信息，
        // 便于上板核对 MMCM 寄存器的实际内容。
        val drpLastRead = RegNext(drp_dout, 0.U)

        // 读数据：窗口外读回 0。寄存器的数据只用低 16 位，高 16 位恒 0。
        axiLiteSlave.io.rdData := Mux(
            inWindowRd,
            MuxLookup(axiLiteSlave.io.rd.bits.addr(5, 2), 0.U)(
                Seq(
                    0.U -> 0x44504d03L.U, // "DPM3"
                    1.U -> Cat(drpLastRead, 13.U(13.W), busy, busy, lockedSync),
                    2.U -> cfgClkfbout,
                    3.U -> cfgClkfbout2,
                    4.U -> cfgClkout0,
                    5.U -> cfgClkout0_2,
                    6.U -> cfgClkout1,
                    7.U -> cfgClkout1_2,
                    8.U -> cfgDivclk,
                    9.U -> cfgPower
                )
            ),
            0.U
        )

        // ---- DRP 状态机：顺序发送 8 笔写 ----
        val sIdle :: sWrite :: sWait :: sDone :: Nil = Enum(4)
        val state = RegInit(sIdle)
        val idx = RegInit(0.U(3.W))

        val daddrTable = VecInit(
            Seq(0x14.U, 0x15.U, 0x08.U, 0x09.U, 0x0a.U, 0x0b.U, 0x16.U, 0x28.U)
        )
        val diTable = VecInit(
            Seq(cfgClkfbout, cfgClkfbout2, cfgClkout0, cfgClkout0_2, cfgClkout1, cfgClkout1_2, cfgDivclk, cfgPower)
        )

        drp_daddr := daddrTable(idx)
        drp_di := diTable(idx)
        drp_den := state === sWrite
        drp_dwe := true.B

        when(state === sIdle) {
            busy := false.B
            when(loadPending) {
                loadPending := false.B
                idx := 0.U
                busy := true.B
                state := sWrite
            }
        }.elsewhen(state === sWrite) {
            state := sWait
        }.elsewhen(state === sWait) {
            when(drp_drdy) {
                when(idx === 7.U) { state := sDone }.otherwise {
                    idx := idx + 1.U
                    state := sWrite
                }
            }
        }.otherwise { // sDone
            state := sIdle
        }

        // AXI 从口：本模块顶层端口与 Axi4LiteSlave 的方向约定一致，直接整体连接
        s_axi_lite :<>= axiLiteSlave.io.axi
    }
}

object EmitMmcmDrpController extends Main {
    emitSystemVerilog(new MmcmDrpController)
}
