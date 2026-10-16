package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 基于DSP的计数器
  *
  * @param width
  *   宽度
  * @param withReset
  *   是否进行复位
  */
class DspCounter(val width: Int = 48, val withReset: Boolean = true) extends Module {
    assert(width <= 48)
    val io = IO(new Bundle {
        val clear = Input(Bool())
        val enable = Input(Bool())
        val value = Output(UInt(width.W))
    })

    val cnt = if (withReset) RegInit(0.U(width.W)) else Reg(UInt(width.W))
    addAttribute(cnt, """use_dsp = "yes"""")
    cnt := Mux(io.clear, 0.U, cnt + io.enable)
    io.value := cnt
}

object DspCounter {
    def apply(width: Int, clear: Bool, enable: Bool, withReset: Boolean = true): UInt = {
        val dspCounter = Module(new DspCounter(width, withReset))
        dspCounter.io.clear := clear
        dspCounter.io.enable := enable
        dspCounter.io.value
    }
}

object EmitDspCounter extends Main { emitSystemVerilog(new DspCounter) }
