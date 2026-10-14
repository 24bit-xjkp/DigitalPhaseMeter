package DigitalPhaseMeter

import chisel3._
import chisel3.util._
import chisel3.experimental.prefix

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
    val dspClock = IO(Input(Clock()))
    private val dspReset = withClock(dspClock) { SyncReset() }
    val phaseMeter = withClockAndReset(dspClock, dspReset) { Module(new PhaseMeter(edgeCntWidth, phaseCntWidth)) }
    class ControllerInPayload extends Bundle {
        // 目标闸门计数，在测量期间保持不变
        val targetGate = UInt(gateCntWidth.W)
    }
    private type OutPayloadBase = phaseMeter.PhaseMeterResponsePayload
    class ControllerOutPayload extends OutPayloadBase {
        // 实际闸门计数
        val gateCnt = UInt(gateCntWidth.W)
    }
    val io = IO(new Bundle {
        val sig = new PhaseMeterSignalPayload
        val in = Flipped(ValidIO(new ControllerInPayload))
        val out = ValidIO(new ControllerOutPayload)
    })
    private val payload = Reg(new ControllerOutPayload)
    io.out.bits := payload
    private val outValid = RegInit(false.B)
    io.out.valid := outValid

    phaseMeter.io.sig := io.sig
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
                    payload.waiveAllAs[OutPayloadBase] :<>= phaseMeter.io.response.bits
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
