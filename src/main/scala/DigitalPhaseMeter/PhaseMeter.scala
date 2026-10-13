package DigitalPhaseMeter

import chisel3._
import chisel3.util._
import chisel3.experimental.prefix

/** 相位计状态控制字
  */
object PhaseMeterStateEnum extends ChiselEnum { val start, stop = Value }

/** 相位计输入信号，sig1为参考信号，sig2为被测信号
  */
class PhaseMeterInputSignal extends Bundle { val sig1, sig2 = Input(Bool()) }

/** 相位计，工作在高速时钟域
  *
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  * @param edgeDetectorDelay
  *   边沿检测器延迟
  */
class PhaseMeter(val edgeCntWidth: Int = 32, val phaseCntWidth: Int = 32) extends Module {
    val io = IO(new Bundle {
        // 输入信号
        val sig = new PhaseMeterInputSignal
        // 请求控制字
        val request = Flipped(DecoupledIO(new Bundle { val state = PhaseMeterStateEnum() }))
        // 响应控制字
        val response = DecoupledIO(new Bundle {
            val state = PhaseMeterStateEnum()
            // 检测到参考信号的总边沿数
            val edgeCnt = UInt(edgeCntWidth.W)
            // 相位差计数
            val phaseCnt = UInt(phaseCntWidth.W)
        })
    })

    val pulseSig1 = EdgeDetector(io.sig.sig1)
    val pulseSig2 = EdgeDetector(io.sig.sig2)

    val requestValid = EdgeDetector(io.request.valid)
    val requestState = io.request.bits.state
    val requestReady = RegInit(false.B)
    io.request.ready := requestReady
    when(!io.request.valid) { requestReady := false.B }
    val responseReady = EdgeDetector(io.response.ready)
    val responseValid = RegInit(false.B)
    val responseState = Reg(PhaseMeterStateEnum())
    io.response.bits.state := responseState
    io.response.valid := responseValid
    when(responseReady) { responseValid := false.B }

    object StateEnum extends ChiselEnum { val idle, waitStart, measuring, waitStop, keepingResult = Value }
    import StateEnum._
    val stateReg = RegInit(idle)
    val request = (state: PhaseMeterStateEnum.Type) => requestValid && requestState === state
    val response = (state: PhaseMeterStateEnum.Type) => {
        responseState := state
        responseValid := true.B
    }
    prefix("fsm") {
        import PhaseMeterStateEnum._
        switch(stateReg) {
            is(idle) {
                when(request(start)) {
                    stateReg := waitStart
                    requestReady := true.B
                }
            }
            is(waitStart) {
                when(pulseSig1) {
                    stateReg := measuring
                    response(start)
                }
            }
            is(measuring) {
                when(request(stop)) {
                    stateReg := waitStop
                    requestReady := true.B
                }
            }
            is(waitStop) {
                when(pulseSig2) {
                    stateReg := keepingResult
                    response(stop)
                }
            }
            is(keepingResult) { when(responseReady) { stateReg := idle } }
        }
    }
    when(!stateReg.isValid) { stateReg := idle }

    val clearCnt = stateReg === idle
    val pulseSig1Valid = stateReg.isOneOf(waitStart, measuring) && pulseSig1
    val pulseSig2Valid = stateReg.isOneOf(waitStart, measuring, waitStop) && pulseSig2
    val phaseCntEnable = RegInit(false.B)
    when(pulseSig1Valid) { phaseCntEnable := true.B }.elsewhen(pulseSig2Valid) { phaseCntEnable := false.B }
    io.response.bits.edgeCnt := DspCounter(edgeCntWidth, clearCnt, pulseSig1Valid)
    io.response.bits.phaseCnt := DspCounter(phaseCntWidth, clearCnt, phaseCntEnable)
}

object PhaseMeter {
    def apply(edgeCntWidth: Int = 32, phaseCntWidth: Int = 32) = {
        val phaseMeter = Module(new PhaseMeter(edgeCntWidth, phaseCntWidth))
        phaseMeter.io
    }
}

object EmitPhaseMeter extends Main {
    emitSystemVerilog(new PhaseMeter)
}
