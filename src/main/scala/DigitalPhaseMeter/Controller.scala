package DigitalPhaseMeter

import chisel3._
import chisel3.experimental.BundleLiterals._
import chisel3.experimental.dataview._
import chisel3.util._

/** 控制器输入负载
  *
  * @param gateCntWidth
  *   闸门计数器宽度
  */
class ControllerInPayload(val gateCntWidth: Int = 28) extends Bundle {
    // 目标闸门计数，在启动的那一拍被锁存；之后改写不影响本次测量（输入不必在测量期间保持稳定）
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
  *   io.sig ─► PhaseMeter（高速域）
  * }}}
  *
  * 片内信号源与外部输入的使能门控、二选一在**上级的本域**里完成（见 `SignalInputMux`），到本模块时 `io.sig` 已经是一条被测信号：
  *
  *   - 外部引脚输入（W18/W19）本来就是异步的；
  *   - intSig 由 PL 内 MMCM 产生（可动态重配置，**频率与相位差都由 MMCM 的配置寄存器决定**，PS 通过 AXI4-Lite 写它的 DRP 寄存器），对测量时钟域同样没有固定相位关系。
  *
  * 两者共用 `PhaseMeter` 的异步采样链，因此切换不改变测量通路的延迟，相位差只是被量化到 1 个测量时钟周期（即相位计本身的分辨率）。使能无效时片内源在上级被门成恒低。
  *
  * 时钟与复位：闸门计数 FSM 与结果锁存工作在隐式 `clock` / `reset`（低速域，主复位是高有效），`PhaseMeter` 例化在 `withClockAndReset(dspClock, dspReset)`
  * 里。高速域的 `dspReset` 由本模块内部的 `ResetSynchronizer` 从主复位同步得到（异步置位、与 `dspClock` 同步释放，同步链带
  * `ASYNC_REG`），因此复位释放与消费它的时钟同域。注意该模块的复位输入是**低有效**的（顶层接 PS 的 `resetn`），主复位是高有效，接入前必须取反。
  *
  * @param gateCntWidth
  *   闸门计数器宽度
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  */
class Controller(val gateCntWidth: Int = 28, val edgeCntWidth: Int = 32, val phaseCntWidth: Int = 32) extends Module {
    require(gateCntWidth <= 30)
    val dspClock = IO(Input(Clock()))
    val io = IO(new Bundle {
        // 被测信号：由上级在本域完成外部输入/片内源的使能门控与二选一后送来（对外部与 MMCM 源都是异步信号）
        val sig = Input(new PhaseMeterSignalPayload)
        val in = Flipped(ValidIO(new ControllerInPayload(gateCntWidth)))
        val out = ValidIO(new ControllerOutPayload(gateCntWidth, edgeCntWidth, phaseCntWidth))
    })
    private val out = RegInit(chiselTypeOf(io.out).Lit(_.valid -> false.B))
    io.out := out

    // 高速域复位：由本模块自己的复位同步链产生（异步置位、与 dspClock 同步释放）。
    // ResetSynchronizer 的输入是**低有效**复位（顶层接 PS 的 resetn），而隐式 reset 是高有效主复位，
    // 必须取反后接入；直接传 reset.asBool 会让 dsp 域在系统复位撤销后被永久按在复位里。
    private val dspReset = ResetSynchronizer(dspClock, !reset.asBool)
    private val phaseMeter = withClockAndReset(dspClock, dspReset) { PhaseMeter(edgeCntWidth, phaseCntWidth) }

    // io.sig 由上级在本域做完使能门控与二选一后送来，这里直接进测量核心的异步采样链
    phaseMeter.sig := io.sig

    private val request = CdcToggleHandShake.send(phaseMeter.request)
    private val responseReady = CdcToggleHandShake.send(phaseMeter.response.ready)
    private val responseValid = CdcToggleHandShake.receive(phaseMeter.response.valid)

    private val gateCntClear = Reg(Bool())
    private val (gateCnt, _) = Counter(0 to (1 << gateCntWidth) - 1, true.B, gateCntClear)
    private val targetGate = Reg(UInt(gateCntWidth.W))
    private val targetGateMet = gateCnt === targetGate

    import PhaseMeterStateEnum._
    private val state = RegInit(sIdle)
    switch(state) {
        is(sIdle) { when(io.in.fire) { state := sWaitStart } }
        is(sWaitStart) { when(responseValid) { state := sMeasuring } }
        is(sMeasuring) { when(targetGateMet) { state := sWaitStop } }
        is(sWaitStop) { when(responseValid) { state := sIdle } }
    }

    private val isIdle = dontTouch(state === sIdle)
    when(isIdle) { gateCntClear := true.B; out.valid := false.B }
    private val startRequestReceived = isIdle && io.in.fire
    when(startRequestReceived) { targetGate := io.in.bits.targetGate; request.send() }
    when(state === sWaitStart && responseValid) { gateCntClear := false.B }
    when(state === sMeasuring && targetGateMet) { request.send() }
    private val finishMeasure = state === sWaitStop && responseValid
    when(finishMeasure) {
        out.bits.viewAsSupertype(new PhaseMeterResponsePayload) := phaseMeter.response.bits
        out.bits.gateCnt := gateCnt
        out.valid := true.B
        responseReady.send()
    }
}

object EmitController extends Main { emitSystemVerilog(new Controller) }
