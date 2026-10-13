package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 基于DSP的计数器
  *
  * @param width
  *   宽度
  */
class DspCounter(val width: Int = 48) extends Module {
    assert(width <= 48)
    val io = IO(new Bundle {
        val clear = Input(Bool())
        val enable = Input(Bool())
        val value = Output(UInt(width.W))
    })

    val cnt = RegInit(0.U(width.W))
    addAttribute(cnt, """use_dsp = "yes"""")
    when(io.clear) {
        cnt := 0.U
    }.elsewhen(io.enable) {
        cnt := cnt + 1.U
    }
    io.value := cnt
}

object DspCounter {
    def apply(width: Int, clear: Bool, enable: Bool): UInt = {
        val dspCounter = Module(new DspCounter(width))
        dspCounter.io.clear := clear
        dspCounter.io.enable := enable
        dspCounter.io.value
    }
}

object EmitDspCounter extends Main {
    emitSystemVerilog(new DspCounter)
}
