package DigitalPhaseMeter

import chisel3._
import chisel3.experimental.prefix
import chisel3.util._
import chisel3.util.experimental.InlineInstance

class MmcmDrpControllerBundle extends Bundle {
    val addrWidth = 32
    val dataWidth = 32
    // AXI4-Lite 从口
    val s_axi_lite = new Axi4LiteSlavePorts(addrWidth, dataWidth)
    // MMCM 动态重配置口
    val drp_daddr = Output(UInt(7.W))
    val drp_di = Output(UInt(16.W))
    val drp_den = Output(Bool())
    val drp_dwe = Output(Bool())
    val drp_dout = Input(UInt(16.W))
    val drp_drdy = Input(Bool())
    // MMCM 锁定指示（异步电平）
    val locked = Input(Bool())
    // 重配置复位（高有效），接到 MMCM 的 RST：重配置期间必须先把它拉高
    val mmcm_reset = Output(Bool())
}

/** MMCM 动态重配置（DRP）控制器
  *
  * 「方案 A」的 PL 侧实现：用极简状态机替代 clk_wiz 在 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG` 下生成的重配置参考设计。
  *
  * ==为什么可以这么小==
  *
  * clk_wiz 的重配置参考设计在硬件里放了 `mmcm_pll_count_calc` / `mmcm_pll_divider` / `mmcm_pll_phase` 三组定点函数， 把「毫度 / 百分比 / MHz」实时换算成
  * MMCM 的计数器位域——实测在 `xc7z010clg400-1` 上占 13,287 个物理 LUT site 与 38 个 DSP48E1。
  *
  * 但那套换算是**纯整数算术**，PS 侧软件完全算得出来（参考实现与权威校验见 `host/dpm_host/sig.py` 与 `ps/include/dpm_regs.h`，两侧逐位一致）。把它搬到软件后，PL 侧只需要
  * 「把软件给的 16 位值按顺序写进 MMCM 的 DRP 口」，也就是本模块。
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
  * 地址译码只看**页内偏移** `addr[11:0]`（BD 把本 IP 映射在 `0x43C1_0000` 的 4KB 段上， 而 AXI 互连不剥离基地址）：`addr[11:6] == 0` 且
  * `addr[1:0] == 0` 才响应，高位不参与译码。 仿真激励必须按真实地址驱动（基地址 + 偏移），否则测不到这条路径——上板时曾因此 让 DRP 读写全部被丢弃。
  *
  * ==DRP 时序==
  *
  * MMCM 要求 `DEN` 只拉高**一个 DCLK 周期**，且此时 `DADDR`/`DI`/`DWE` 有效； 之后等 `DRDY` 拉高表示该次访问完成。本模块用一个小状态机逐条发送
  * `{CLKFBOUT, CLKFBOUT2, CLKOUT0, CLKOUT0_2, CLKOUT1, CLKOUT1_2, DIVCLK, POWER}` 八笔写，全部完成后回 IDLE。
  *
  * 重配置期间 MMCM 会失锁、输出停止，因此软件必须在测量空闲（`STATUS.busy = 0`）时改。
  */
class MmcmDrpControllerImpl extends Module with InlineInstance {
    val io = IO(new MmcmDrpControllerBundle)
    private val addrWidth = io.addrWidth
    private val dataWidth = io.dataWidth

    // ---- 软件可写的 MMCM 寄存器镜像 ----
    // 复位值取 clk_wiz Write DRP 在同一工作点（50MHz 参考、D=40、相位 0/90°）
    // 生成的值，保证软件不写任何寄存器时 MMCM 的静态配置与改造前一致。
    private val clkFbOut = RegInit(0x128a.U(16.W))
    private val clkFbOut2 = RegInit(0x0000.U(16.W))
    private val clkOut0 = RegInit(0x1514.U(16.W))
    private val clkOut0_2 = RegInit(0x0000.U(16.W))
    private val clkOut1 = RegInit(0x1514.U(16.W))
    private val clkOut1_2 = RegInit(0x000a.U(16.W))
    private val divClk = RegInit(0x1041.U(16.W))
    private val power = RegInit(0xffff.U(16.W))

    // locked 是异步电平，先同步到本时钟域
    private val lockedSync = CdcLevel(io.locked)

    // ---- AXI4-Lite 从口 ----
    // 寄存器空间只有 11 个字、读写都一拍完成：就地吸收命令，不给 AXI 挂起。
    // 地址译码只看页内偏移 addr[11:0]（[11:6] 窗口、[5:2] 寄存器索引、[1:0] 对齐），
    // 写数据与字节选通只用低 16 位 / 低 2 字节，因此 AXI 从口内部只锁存这些低位，
    // 其余位零扩展回 32 位 payload。行为与全宽锁存完全一致，只是不再产生
    // 「未读寄存器位被 trim」的 Synth 8-3936 告警。
    private val axiLiteSlave = Axi4LiteSlave(addrWidth, dataWidth, addrUsedWidth = 12, dataUsedWidth = 16)
    axiLiteSlave.wr.ready := true.B
    axiLiteSlave.rd.ready := true.B

    private val busy = RegInit(false.B)
    // 软件写 LOAD=1 置起本标志；状态机在 IDLE 看到它就开始重配置并清除。
    // 用「寄存器」而不是单拍脉冲，避免组合脉冲宽度依赖 AXI 握手时序。
    private val loadPending = RegInit(false.B)
    // 调试计数：记录收到多少次写、多少次 LOAD（读寄存器 15 可查看）

    // 地址译码：BD 把本 IP 映射到 4KB 段（0x43C1_0000）
    // `addr[11:6]` 必须为 0（寄存器区只有 0x00..0x28）、`addr[1:0]` 必须为 0（32 位对齐）。
    // 高 20 位由 BD 的地址段（M01_AXI → 0x43C1_0000）保证。
    private object RegIndex extends ChiselEnum {
        val iVersion, iStatus, iClkFbOut, iClkFbOut2, iClkOut0, iClkOut0_2, iClkOut1, iClkOut1_2, iDivClk, iPower,
            iLoadPending = Value
    }
    private val indexWidth = RegIndex.getWidth
    import RegIndex._
    def inWindowOf(addr: UInt): Bool = {
        val rest = Cat(addr.tail(20) >> indexWidth + 2, addr.take(2))
        rest === 0.U
    }
    private val inWindow = inWindowOf(axiLiteSlave.wr.bits.addr)
    private val inWindowRd = inWindowOf(axiLiteSlave.rd.bits.addr)

    // LOAD 是写 1 触发，不落寄存器；用字节选通合并后的值
    prefix("wrImpl") {
        when(axiLiteSlave.wr.fire && inWindow) {
            import axiLiteSlave.wr.bits._
            switch(addr(indexWidth + 1, 2)) {
                is(iClkFbOut.asUInt) { clkFbOut := ByteStrobe.merge(clkFbOut, data, strb) }
                is(iClkFbOut2.asUInt) { clkFbOut2 := ByteStrobe.merge(clkFbOut2, data, strb) }
                is(iClkOut0.asUInt) { clkOut0 := ByteStrobe.merge(clkOut0, data, strb) }
                is(iClkOut0_2.asUInt) { clkOut0_2 := ByteStrobe.merge(clkOut0_2, data, strb) }
                is(iClkOut1.asUInt) { clkOut1 := ByteStrobe.merge(clkOut1, data, strb) }
                is(iClkOut1_2.asUInt) { clkOut1_2 := ByteStrobe.merge(clkOut1_2, data, strb) }
                is(iDivClk.asUInt) { divClk := ByteStrobe.merge(divClk, data, strb) }
                is(iPower.asUInt) { power := ByteStrobe.merge(power, data, strb) }
                is(iLoadPending.asUInt) { when(data(0)) { loadPending := true.B } }
            }
        }
    }

    // drp_dout：本设计不需要读-改-写（软件给的是完整寄存器值），
    // 但仍把上一次 DRP 访问读回的内容放进 STATUS 的高 16 位作为诊断信息，
    // 便于上板核对 MMCM 寄存器的实际内容。
    private val drpLastRead = RegNext(io.drp_dout, 0.U)

    // 读数据：窗口外读回 0。寄存器的数据只用低 16 位，高 16 位恒 0。
    private val versionValue = Cat("DPM".map(_.U(8.W)) :+ 3.U(8.W))
    private val statusValue = Cat(drpLastRead, 13.U(13.W), busy, busy, lockedSync)
    private val indexMap = Seq(
        iVersion -> versionValue,
        iStatus -> statusValue,
        iClkFbOut -> clkFbOut,
        iClkFbOut2 -> clkFbOut2,
        iClkOut0 -> clkOut0,
        iClkOut0_2 -> clkOut0_2,
        iClkOut1 -> clkOut1,
        iClkOut1_2 -> clkOut1_2,
        iDivClk -> divClk,
        iPower -> power
    )
    axiLiteSlave.rdData := Mux(
        inWindowRd,
        MuxLookup(axiLiteSlave.rd.bits.addr(indexWidth + 1, 2), 0.U)(indexMap.map { case (i, v) => (i.asUInt, v) }),
        0.U
    )

    // ---- DRP 状态机：先按住 MMCM 复位，再顺序发送 8 笔写，最后放开复位 ----
    //
    // 为什么必须先拉高 MMCM 的 RST（上板实测 + unisim 行为模型的双重依据）：
    //   * MMCM 的 DRP 写要求器件处于复位状态（unisim 模型在 RST 为低时直接报
    //     `Error: RST need to be high when changing parameters through DRP`，
    //     且把 @(negedge RST) 当作「新配置生效」的时刻）；
    //   * 上板实测：不拉 RST 时**分频字立即生效、相位字不生效**（写相位字段后输出
    //     相位纹丝不动），因为相位延迟单元只在复位/重新锁定时重新初始化。
    // 因此重配置的完整时序是：RST=1 → 8 笔 DRP 写 → RST=0 → MMCM 重新锁定。
    private object StateEnum extends ChiselEnum { val sIdle, sResetHold, sWrite, sWait, sRelease = Value }
    import StateEnum._
    private val state = RegInit(sIdle)
    private val isIdle = state === sIdle
    private val idxCntEnable = state === sWait && io.drp_drdy
    private val holdCntEnable = state.isOneOf(sResetHold, sRelease)
    private val (idx, idxTrigger) = Counter(0 until 8, idxCntEnable, isIdle)
    private val (holdCnt, holdCntTrigger) = Counter(0 until 8, holdCntEnable, isIdle)

    private val daddrTable = VecInit(0x14.U, 0x15.U, 0x08.U, 0x09.U, 0x0a.U, 0x0b.U, 0x16.U, 0x28.U)
    private val diTable = VecInit(indexMap.drop(2).map { case (_, v) => v })

    io.drp_daddr := daddrTable(idx)
    io.drp_di := diTable(idx)
    // DEN 与 DWE 都只在 sWrite 那一拍拉高：
    //  * MMCM 要求 DEN 只拉高**一个** DCLK 周期（DADDR/DI/DWE 此时有效）；
    //  * 厂商 unisim 模型对「DWE 连续多个 DCLK 拉高」直接报 DRC Error 并结束仿真。
    private val isWrite = state === sWrite
    io.drp_den := isWrite
    io.drp_dwe := isWrite
    // 复位在「按住 + 8 笔写 + 等 DRDY」期间保持拉高，写完放开
    io.mmcm_reset := state.isOneOf(sResetHold, sWrite, sWait)

    prefix("fsm") {
        switch(state) {
            is(sIdle) {
                busy := false.B
                holdCnt := 0.U
                when(loadPending) {
                    loadPending := false.B
                    busy := true.B
                    state := sResetHold
                }
            }
            // 复位至少要按住几拍（MMCM 数据手册要求 RST 有效宽度；这里取 8 个 CLK）
            is(sResetHold) { when(holdCntTrigger) { state := sWrite } }
            is(sWrite) { state := sWait }
            is(sWait) { when(io.drp_drdy) { state := Mux(idxTrigger, sRelease, sWrite) } }
            is(sRelease) { when(holdCntTrigger) { state := sIdle } }
        }
    }

    // AXI 从口：本模块顶层端口与 Axi4LiteSlave 的方向约定一致，直接整体连接
    io.s_axi_lite :<>= axiLiteSlave.axi
}

class MmcmDrpController extends RawModule {
    val clock = IO(Input(Clock()))
    val resetn = IO(Input(Bool()))
    val io = FlatIO(new MmcmDrpControllerBundle)
    private val mainReset = ResetSynchronizer(clock, resetn)
    private val impl = withClockAndReset(clock, mainReset) { Module(new MmcmDrpControllerImpl) }
    io :<>= impl.io
}

object EmitMmcmDrpController extends Main { emitSystemVerilog(new MmcmDrpController) }
