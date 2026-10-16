package DigitalPhaseMeter

import chisel3._
import chisel3.util._
import chisel3.experimental.prefix

/** 相位计输入信号，sig1为参考信号，sig2为被测信号
  */
class PhaseMeterSignalPayload extends Bundle { val sig1, sig2 = Input(Bool()) }

/** 相位计响应负载
  *
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  */
class PhaseMeterResponsePayload(val edgeCntWidth: Int = 32, val phaseCntWidth: Int = 32) extends Bundle {
    // 检测到参考信号的总边沿数
    val edgeCnt = UInt(edgeCntWidth.W)
    // 相位差计数
    val phaseCnt = UInt(phaseCntWidth.W)
}

/** 相位计，工作在高速时钟域
  *
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  */
class PhaseMeter(val edgeCntWidth: Int = 32, val phaseCntWidth: Int = 32) extends Module {
    val io = IO(new Bundle {
        // 输入信号
        val sig = new PhaseMeterSignalPayload
        // 请求控制字
        val request = Input(Bool())
        // 响应控制字
        val response = DecoupledIO(new PhaseMeterResponsePayload(edgeCntWidth, phaseCntWidth))
    })

    private val pulseSig1 = EdgeDetector(io.sig.sig1)
    private val pulseSig2 = EdgeDetector(io.sig.sig2)

    private val request = CdcToggleHandShake.receive(io.request)
    private val responseReady = CdcToggleHandShake.receive(io.response.ready)
    private val responseValid = CdcToggleHandShake.send(io.response.valid)

    private object StateEnum extends ChiselEnum { val idle, waitStart, measuring, waitStop, keepingResult = Value }
    import StateEnum._
    private val stateReg = RegInit(idle)
    prefix("fsm") {
        switch(stateReg) {
            is(idle) { when(request) { stateReg := waitStart } }
            is(waitStart) {
                when(pulseSig1) {
                    stateReg := measuring
                    responseValid.send
                }
            }
            is(measuring) { when(request) { stateReg := waitStop } }
            is(waitStop) {
                when(pulseSig2) {
                    stateReg := keepingResult
                    responseValid.send
                }
            }
            is(keepingResult) { when(responseReady) { stateReg := idle } }
        }
    }
    when(!stateReg.isValid) { stateReg := idle }

    // 计数器控制脚全部寄存器化（400MHz 的关键路径）：
    // clearCnt / pulseSig1Valid / pulseSig2Valid 都是寄存器，DSP48 的 RSTP/CEP 因此由寄存器直接驱动。
    // 两个使能同时延迟一拍 → 它们之间的相对时序不变，使能窗口长度也不变。
    private val clearCnt = RegNext(stateReg === idle)
    private val pulseSig1Valid = RegNext(stateReg.isOneOf(waitStart, measuring) && pulseSig1)
    private val pulseSig2Valid = RegNext(stateReg.isOneOf(waitStart, measuring, waitStop) && pulseSig2)
    private val phaseCntEnable = Reg(Bool())
    when(pulseSig1Valid) { phaseCntEnable := true.B }.elsewhen(pulseSig2Valid) { phaseCntEnable := false.B }

    io.response.bits.edgeCnt := DspCounter(edgeCntWidth, clearCnt, pulseSig1Valid, false)
    io.response.bits.phaseCnt := DspCounter(phaseCntWidth, clearCnt, phaseCntEnable, false)
}

object EmitPhaseMeter extends Main {
    emitSystemVerilog(new PhaseMeter)
}
