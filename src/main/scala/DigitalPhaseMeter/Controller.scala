package DigitalPhaseMeter

import chisel3._
import chisel3.util._
import chisel3.experimental.prefix
import DigitalPhaseMeter.PhaseMeterStateEnum.start

/** 控制器，工作在低速时钟域
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
    class ControllerInPayload extends Bundle {
        // 目标闸门计数，在测量期间保持不变
        val targetGate = UInt(gateCntWidth.W)
    }
    class ControllerOutPayload extends Bundle {
        // 实际闸门计数
        val gateCnt = UInt(gateCntWidth.W)
        // 检测到参考信号的总边沿数
        val edgeCnt = UInt(edgeCntWidth.W)
        // 相位差计数
        val phaseCnt = UInt(phaseCntWidth.W)
    }
    val dspClock = IO(Input(Clock()))
    val io = IO(new Bundle {
        val sig = new PhaseMeterInputSignal
        val in = Flipped(ValidIO(new ControllerInPayload))
        val out = ValidIO(new ControllerOutPayload)
    })
    val payload = Reg(new ControllerOutPayload)
    io.out.bits := payload
    val outValid = RegInit(false.B)
    io.out.valid := outValid

    val dspReset = withClock(dspClock) { SyncReset() }
    val phaseMeter = withClockAndReset(dspClock, dspReset) { PhaseMeter(edgeCntWidth, phaseCntWidth) }
    phaseMeter.sig := io.sig
    val requestValid = RegInit(false.B)
    phaseMeter.request.valid := requestValid
    val requestState = Reg(PhaseMeterStateEnum())
    phaseMeter.request.bits.state := requestState
    val requestReady = EdgeDetector(phaseMeter.request.ready)
    when(requestReady) { requestValid := false.B }
    val responseReady = RegInit(false.B)
    phaseMeter.response.ready := responseReady
    val responseValid = EdgeDetector(phaseMeter.response.valid)
    val responseState = phaseMeter.response.bits.state

    val gateCntClear = RegInit(false.B)
    val gateCntEnable = RegInit(false.B)
    val (gateCnt, _) = Counter(0 to (1 << gateCntWidth) - 1, gateCntEnable, gateCntClear)

    object StateEnum extends ChiselEnum { val idle, waitStart, measuring, waitStop = Value }
    import StateEnum._
    val stateReg = RegInit(idle)
    val request = (state: PhaseMeterStateEnum.Type) => {
        requestState := state
        requestValid := true.B
    }
    val response = (state: PhaseMeterStateEnum.Type) => responseValid && responseState === state
    prefix("fsm") {
        import PhaseMeterStateEnum._
        switch(stateReg) {
            is(idle) {
                gateCntClear := true.B
                gateCntEnable := false.B
                responseReady := false.B
                outValid := false.B
                when(io.in.valid) {
                    stateReg := waitStart
                    request(start)
                }
            }
            is(waitStart) {
                when(response(start)) {
                    stateReg := measuring
                    gateCntEnable := true.B
                    gateCntClear := false.B
                    responseReady := true.B
                }
            }
            is(measuring) {
                when(gateCnt === io.in.bits.targetGate) {
                    stateReg := waitStop
                    request(stop)
                    responseReady := false.B
                }
            }
            is(waitStop) {
                when(response(stop)) {
                    stateReg := idle
                    payload.edgeCnt := phaseMeter.response.bits.edgeCnt
                    payload.phaseCnt := phaseMeter.response.bits.phaseCnt
                    payload.gateCnt := gateCnt
                    outValid := true.B
                    responseReady := true.B
                }
            }
        }
    }
}

object EmitController extends Main {
    emitSystemVerilog(new Controller)
}
