package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 边沿检测器
  *
  * @param n
  *   延迟级数
  */
class EdgeDetector(val n: Int = 3) extends Module {
    assert(n >= 2)
    val io = IO(new Bundle {
        val in = Input(Bool())
        val trigger = Output(Bool())
    })
    val pipeline = ShiftRegister(io.in, n - 1, false.B, true.B)
    val currentValue = pipeline
    val previousValue = RegNext(currentValue, false.B)
    io.trigger := currentValue && !previousValue
}

object EdgeDetector {
    // 边沿检测器内部默认延迟级数
    val defaultN = 3
    // 默认延迟
    val defaultDelay = defaultN - 1

    def apply(sig: Bool): Bool = {
        val edgeDetector = Module(new EdgeDetector)
        edgeDetector.io.in := sig
        edgeDetector.io.trigger
    }
}

object EmitEdgeDetector extends Main {
    emitSystemVerilog(new EdgeDetector)
}
