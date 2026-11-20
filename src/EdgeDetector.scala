package DigitalPhaseMeter

import chisel3.*

/** 边沿类型 */
enum EdgeType { case Rising, Falling, Both }

/** 边沿检测器
  *
  * @param n
  *   延迟级数
  * @param edgeType
  *   边沿类型
  */
class EdgeDetector(val n: Int = 3, val edgeType: EdgeType = EdgeType.Rising) extends Module:
    require(n >= 2)
    val io = IO(new Bundle:
        val in = Input(Bool())
        val trigger = Output(Bool()))
    private val pipeline = MyShiftRegisters(io.in, n)
    io.trigger := (edgeType match
        case EdgeType.Rising  => !pipeline.last && pipeline.init.last
        case EdgeType.Falling => pipeline.last && !pipeline.init.last
        case EdgeType.Both    => pipeline.last =/= pipeline.init.last)
    override def desiredName: String = s"EdgeDetector$edgeType"

object EdgeDetector:
    def apply(sig: Bool, edgeType: EdgeType = EdgeType.Rising, n: Int = 3): Bool =
        val edgeDetector = Module(new EdgeDetector(n, edgeType))
        edgeDetector.io.in := sig
        edgeDetector.io.trigger

object EmitEdgeDetector extends Main { protected def emit = emitSystemVerilog(new EdgeDetector) }
