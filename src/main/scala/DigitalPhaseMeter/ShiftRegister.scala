package DigitalPhaseMeter

import chisel3._
import chisel3.util._

class MyShiftRegisters(val n: Int, val init: Bool = false.B, val async: Boolean = true) extends Module {
    assert(n >= 2)
    val io = IO(new Bundle {
        val in = Input(Bool())
        val out = Output(Vec(n, Bool()))
    })

    private val pipeline = RegInit(VecInit.fill(n)(init))
    if (async) { addAttribute(pipeline, """ASYNC_REG = "TRUE"""") }
    for (i <- 0 until n) { pipeline(i) := (if (i == 0) io.in else pipeline(i - 1)) }
    io.out := pipeline
}

object MyShiftRegisters {
    def apply(in: Bool, n: Int, init: Bool = false.B, async: Boolean = true): Seq[Bool] = {
        val myShiftRegisters = Module(new MyShiftRegisters(n, init, async))
        myShiftRegisters.io.in := in
        myShiftRegisters.io.out.toSeq
    }
}

object MyShiftRegister {
    def apply(in: Bool, n: Int, init: Bool = false.B, async: Boolean = true): Bool =
        MyShiftRegisters(in, n, init, async)(n - 1)
}
