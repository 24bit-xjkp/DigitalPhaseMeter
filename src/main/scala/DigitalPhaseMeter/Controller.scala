package DigitalPhaseMeter

import chisel3._
import chisel3.util._
import chisel3.experimental.prefix

/** 控制器输入负载
  *
  * @param gateCntWidth
  *   闸门计数器宽度
  */
class ControllerInPayload(val gateCntWidth: Int = 28) extends Bundle {
    // 目标闸门计数，在测量期间保持不变
    val targetGate = UInt(gateCntWidth.W)
}

/** 控制器输出负载
  *
  * @param gateCntWidth
  *   闸门计数器宽度
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  */
class ControllerOutPayload(
    val gateCntWidth: Int = 28,
    override val edgeCntWidth: Int = 32,
    override val phaseCntWidth: Int = 32
) extends PhaseMeterResponsePayload(edgeCntWidth, phaseCntWidth) {
    // 实际闸门计数
    val gateCnt = UInt(gateCntWidth.W)
}

/** 控制器，工作在低速时钟域
  *
  * 除了低速域的闸门计数 FSM 与高速域的测量核心，这里还接入了信号输入通路：
  *
  * {{{
  *   io.sig（外部引脚 W18/W19，异步）────────┐
  *                                          ├─► SignalInputMux ─► PhaseMeter（高速域）
  *   io.sourceSig1/2（MMCM 的两路输出）──────┘        ▲
  *                                                   │ srcSelect / enable
  *                          io.config（低速域）─► CdcConfigBus（整字握手）
  * }}}
  *
  * `io.sourceSig1` / `io.sourceSig2` 是 PL 内 MMCM（clk_wiz，动态可重配置）产生的两路同频时钟， **频率与相位差都由 MMCM 的配置寄存器决定**（PS 通过 AXI4-Lite
  * 写它的 DRP 寄存器）。 它们与外部输入一样由 `PhaseMeter` 的异步采样链在测量时钟域采样，因此切换不改变测量通路的 延迟，相位差只是被量化到 1 个测量时钟周期（即相位计本身的分辨率）。使能无效时片内源输出 恒低（用
  * `enable` 把它门掉）。
  *
  * @param gateCntWidth
  *   闸门计数器宽度
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  */
class Controller(val gateCntWidth: Int = 28, val edgeCntWidth: Int = 32, val phaseCntWidth: Int = 32) extends Module {
    assert(gateCntWidth <= 30)
    val dspClock = IO(Input(Clock()))
    val io = IO(new Bundle {
        // 外部输入信号（引脚送来，异步）
        val sig = new PhaseMeterSignalPayload
        // 片内信号源：MMCM 产生的两路同频、相位差可调的时钟（异步于测量时钟域）
        val sourceSig1 = Input(Bool())
        val sourceSig2 = Input(Bool())
        // 片内信号源使能与输入选择配置（低速域），整字跨到高速域
        val config = Flipped(DecoupledIO(new SignalConfigPayload))
        val in = Flipped(ValidIO(new ControllerInPayload(gateCntWidth)))
        val out = ValidIO(new ControllerOutPayload(gateCntWidth, edgeCntWidth, phaseCntWidth))
    })
    private val payload = Reg(new ControllerOutPayload(gateCntWidth, edgeCntWidth, phaseCntWidth))
    io.out.bits := payload
    private val outValid = RegInit(false.B)
    io.out.valid := outValid

    // 配置跨时钟域：目的域在收到新配置前一直保持旧值（复位后为全 0，即选外部输入）
    private val cdcConfig = Module(new CdcConfigBus(new SignalConfigPayload))
    cdcConfig.dstClock := dspClock
    cdcConfig.io.in.valid := io.config.valid
    cdcConfig.io.in.bits := io.config.bits
    io.config.ready := cdcConfig.io.in.ready

    private val dspReset = withClock(dspClock) { SyncReset() }
    val phaseMeter = withClockAndReset(dspClock, dspReset) { Module(new PhaseMeter(edgeCntWidth, phaseCntWidth)) }
    private val signalInputMux = withClockAndReset(dspClock, dspReset) { Module(new SignalInputMux) }

    signalInputMux.io.sig1External := io.sig.sig1
    signalInputMux.io.sig2External := io.sig.sig2
    // 片内源：MMCM 的两路输出直接作为被测信号（与外部输入共用 PhaseMeter 的异步采样链），
    // 使能无效时把它门成恒低
    signalInputMux.io.sig1Internal := io.sourceSig1 && cdcConfig.io.out.enable
    signalInputMux.io.sig2Internal := io.sourceSig2 && cdcConfig.io.out.enable
    signalInputMux.io.selectInternal := cdcConfig.io.out.srcSelect
    phaseMeter.io.sig.sig1 := signalInputMux.io.sig1
    phaseMeter.io.sig.sig2 := signalInputMux.io.sig2

    private val request = CdcToggleHandShake.send(phaseMeter.io.request)
    private val responseReady = CdcToggleHandShake.send(phaseMeter.io.response.ready)
    private val responseValid = CdcToggleHandShake.receive(phaseMeter.io.response.valid)

    private val gateCntClear = RegInit(false.B)
    private val (gateCnt, _) = Counter(0 to (1 << gateCntWidth) - 1, true.B, gateCntClear)

    private object StateEnum extends ChiselEnum { val idle, waitStart, measuring, waitStop = Value }
    import StateEnum._
    private val stateReg = RegInit(idle)
    prefix("fsm") {
        switch(stateReg) {
            is(idle) {
                gateCntClear := true.B
                outValid := false.B
                when(io.in.valid) {
                    stateReg := waitStart
                    request.send
                }
            }
            is(waitStart) {
                when(responseValid) {
                    stateReg := measuring
                    gateCntClear := false.B
                    responseReady.send
                }
            }
            is(measuring) {
                when(gateCnt === io.in.bits.targetGate) {
                    stateReg := waitStop
                    request.send
                }
            }
            is(waitStop) {
                when(responseValid) {
                    stateReg := idle
                    payload.waiveAllAs[PhaseMeterResponsePayload] :<= phaseMeter.io.response.bits
                    payload.gateCnt := gateCnt
                    outValid := true.B
                    responseReady.send
                }
            }
        }
    }
}

object EmitController extends Main {
    emitSystemVerilog(new Controller)
}
