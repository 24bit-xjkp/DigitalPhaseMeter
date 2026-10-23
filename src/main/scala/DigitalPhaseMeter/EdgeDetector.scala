package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 边沿类型 */
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
    require(n >= 2)
    val io = IO(new Bundle {
        val in = Input(Bool())
        val trigger = Output(Bool())
    })
    private val pipeline = MyShiftRegisters(io.in, n, async = async)
    io.trigger := (edgeType match {
        case EdgeType.rising  => !pipeline(n - 1) && pipeline(n - 2)
        case EdgeType.falling => pipeline(n - 1) && !pipeline(n - 2)
        case EdgeType.both    => pipeline(n - 1) =/= pipeline(n - 2)
    })
    override def desiredName: String = s"EdgeDetector${edgeType.toString.capitalize}${if (async) "A" else "S"}"
}

object EdgeDetector {
    def apply(sig: Bool, edgeType: EdgeType.Type = EdgeType.rising, async: Boolean = true): Bool = {
        val edgeDetector = Module(new EdgeDetector(edgeType = edgeType, async = async))
        edgeDetector.io.in := sig
        edgeDetector.io.trigger
    }
}

object EmitEdgeDetector extends Main { emitSystemVerilog(new EdgeDetector) }
