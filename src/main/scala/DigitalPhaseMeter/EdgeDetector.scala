package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 边沿类型
  */
object EdgeType extends Enumeration {
    type Type = Value
    val rising, falling, both = Value
}

/** 边沿检测器
  *
  * @param n
  *   延迟级数
  * @param edgeType
  *   边沿类型
  * @param async
  *   是否为异步边沿
  */
class EdgeDetector(val n: Int = 3, val edgeType: EdgeType.Type = EdgeType.rising, val async: Boolean = true)
    extends Module {
    assert(n >= 2)
    val io = IO(new Bundle {
        val in = Input(Bool())
        val trigger = Output(Bool())
    })
    val pipeline = RegInit(VecInit.fill(n)(false.B))
    if (async) { addAttribute(pipeline, """async_reg = "yes"""") }
    for (i <- 0 until n) { pipeline(i) := (if (i == 0) io.in else pipeline(i - 1)) }
    edgeType match {
        case EdgeType.rising  => io.trigger := !pipeline(n - 1) && pipeline(n - 2)
        case EdgeType.falling => io.trigger := pipeline(n - 1) && !pipeline(n - 2)
        case EdgeType.both    => io.trigger := pipeline(n - 1) =/= pipeline(n - 2)
    }
}

object EdgeDetector {
    // 边沿检测器内部默认延迟级数
    val defaultN = 3
    // 默认延迟
    val defaultDelay = defaultN - 1

    def apply(sig: Bool, edgeType: EdgeType.Type = EdgeType.rising, async: Boolean = true): Bool = {
        val edgeDetector = Module(new EdgeDetector(edgeType = edgeType, async = async))
        edgeDetector.io.in := sig
        edgeDetector.io.trigger
    }
}

object EmitEdgeDetector extends Main {
    emitSystemVerilog(new EdgeDetector)
}
