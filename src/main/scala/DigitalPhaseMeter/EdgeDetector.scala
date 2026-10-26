package DigitalPhaseMeter

import chisel3._

/** 边沿类型 */
sealed trait EdgeType
object EdgeType {
    case object Rising extends EdgeType
    case object Falling extends EdgeType
    case object Both extends EdgeType
}

/** 边沿检测器
  *
  * @param n
  *   延迟级数
  * @param edgeType
  *   边沿类型
  */
class EdgeDetector(val n: Int = 3, val edgeType: EdgeType = EdgeType.Rising) extends Module {
    require(n >= 2)
    val io = IO(new Bundle {
        val in = Input(Bool())
        val trigger = Output(Bool())
    })
    private val pipeline = MyShiftRegisters(io.in, n)
    io.trigger := (edgeType match {
        case EdgeType.Rising  => !pipeline(n - 1) && pipeline(n - 2)
        case EdgeType.Falling => pipeline(n - 1) && !pipeline(n - 2)
        case EdgeType.Both    => pipeline(n - 1) =/= pipeline(n - 2)
    })
    override def desiredName: String = s"EdgeDetector${edgeType.toString}"
}

object EdgeDetector {
    def apply(sig: Bool, edgeType: EdgeType = EdgeType.Rising, n: Int = 3): Bool = {
        val edgeDetector = Module(new EdgeDetector(n, edgeType))
        edgeDetector.io.in := sig
        edgeDetector.io.trigger
    }
}

object EmitEdgeDetector extends Main { emitSystemVerilog(new EdgeDetector) }
