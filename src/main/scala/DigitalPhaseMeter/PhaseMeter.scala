package DigitalPhaseMeter

import chisel3._
import chisel3.experimental.prefix
import chisel3.util._

/** 相位计输入信号，sig1为参考信号，sig2为被测信号
  */
class PhaseMeterSignalPayload extends Bundle { val sig1, sig2 = Bool() }

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

/** 相位计状态机枚举
  */
object PhaseMeterStateEnum extends ChiselEnum { val sIdle, sWaitStart, sMeasuring, sWaitStop = Value }

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
        val sig = Input(new PhaseMeterSignalPayload)
        // 请求控制字
        val request = Input(Bool())
        // 响应控制字
        val response = EnqIO(new PhaseMeterResponsePayload(edgeCntWidth, phaseCntWidth))
    })

    // 四个跨域/异步输入都先经检测器的组合输出，再统一打一拍送进 FSM 与计数器控制逻辑。
    // 这一拍是 450MHz 时序的一部分：不寄存时「检测器最后一级 → 组合脉冲 → FSM/使能译码」
    // 是两级 LUT + 长布线（实测单条 0.188~0.212ns 余量）；寄存后检测器输出只驱动本地
    // 触发器，FSM 的输入来自寄存器，路径大幅缩短。
    //
    // 四个信号都延迟同样的 1 拍（包括 request/responseReady），因此：
    //   - 握手只是整体晚 1 个高速周期，协议（电平翻转 + 同步链）不受影响；
    //   - pulseSig1Valid/pulseSig2Valid/phaseCntEnable 之间的相对时序不变，
    //     相位计数窗口长度不变（测量语义不变）。
    private val pulseSig1 = RegNext(EdgeDetector(io.sig.sig1))
    private val pulseSig2 = RegNext(EdgeDetector(io.sig.sig2))

    private val request = RegNext(CdcToggleHandShake.receive(io.request))
    private val responseReady = RegNext(CdcToggleHandShake.receive(io.response.ready))
    private val responseValid = CdcToggleHandShake.send(io.response.valid)

    import PhaseMeterStateEnum._
    private val state = RegInit(sIdle)
    private val needSendResponse = Reg(Bool())
    prefix("fsm") {
        switch(state) {
            is(sIdle) { when(request) { state := sWaitStart; needSendResponse := true.B } }
            is(sWaitStart) { when(pulseSig1) { state := sMeasuring; responseValid.send() } }
            is(sMeasuring) { when(request) { state := sWaitStop } }
            is(sWaitStop) {
                when(pulseSig2 && needSendResponse) { responseValid.send(); needSendResponse := false.B }
                when(responseReady) { state := sIdle }
            }
        }
    }

    // 计数器控制脚全部寄存器化（450MHz 的关键路径）：clearCnt / pulseSig1Valid /
    // pulseSig2Valid / phaseCntEnable 都是寄存器，直接驱动 DSP48E1 的 RSTP/CEP 控制脚。
    //
    // 置位与清零必须**用同一级延迟**（pulseSig1Valid / pulseSig2Valid）：相位计数的是
    // 「sig1 检测沿 → sig2 检测沿」之间的高速时钟周期数，两个边沿脉冲经同样的边沿检测
    // 流水线后相隔 K 拍，那么使能窗口也必须是 K 拍
    private val clearCnt = RegNext(state === sIdle)
    private val pulseSig1Valid = RegNext(state.isOneOf(sWaitStart, sMeasuring) && pulseSig1)
    private val pulseSig2Valid = RegNext(pulseSig2)
    // 无复位值在 xsim 里是 X，在第一次测量时经 `cnt + enable` 污染计数器
    private val phaseCntEnable = RegInit(false.B)
    when(pulseSig2Valid) { phaseCntEnable := false.B }.elsewhen(pulseSig1Valid) { phaseCntEnable := true.B }

    io.response.bits.edgeCnt := DspCounter(edgeCntWidth, clearCnt, pulseSig1Valid, false)
    io.response.bits.phaseCnt := DspCounter(phaseCntWidth, clearCnt, phaseCntEnable, false)
}

object PhaseMeter {
    def apply(edgeCntWidth: Int = 32, phaseCntWidth: Int = 32) = {
        val phaseMeter = Module(new PhaseMeter(edgeCntWidth, phaseCntWidth)).suggestName("")
        phaseMeter.io
    }
}

object EmitPhaseMeter extends Main { emitSystemVerilog(new PhaseMeter) }
