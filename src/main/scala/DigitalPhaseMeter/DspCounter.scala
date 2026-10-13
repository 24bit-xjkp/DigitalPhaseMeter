package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/**
  * 基于DSP的计数器
  *
  * @param width 宽度
  */
class DspCounter(width: Int = 48) extends Module {
    assert(width <= 48)
    val io = IO(new Bundle {
        val clear = Input(Bool())
        val enable = Input(Bool())
        val value = Output(UInt(width.W))
    })

    val cnt = RegInit(0.U(width.W))
    addAttribute(cnt, """use_dsp = "true"""")
    when(io.clear) {
        cnt := 0.U
    }.elsewhen(io.enable) {
        cnt := cnt + 1.U
    }
    io.value := cnt
}

object DspCounter extends Main {
    emitSystemVerilog(new DspCounter)
}
