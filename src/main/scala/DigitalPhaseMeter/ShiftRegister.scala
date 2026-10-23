package DigitalPhaseMeter

import chisel3._
import chisel3.util._

object MyShiftRegisters {
    def apply(in: Bool, n: Int, init: Bool = false.B, async: Boolean = true): Seq[Bool] = {
        require(n >= 2)
        val pipeline = RegInit(Fill(n, init))
        if (async) { addAttribute(pipeline, """ASYNC_REG = "TRUE"""") }
        pipeline := Cat(pipeline.take(n - 1), in)
        pipeline.asBools
    }
}

object MyShiftRegister {
    def apply(in: Bool, n: Int, init: Bool = false.B, async: Boolean = true): Bool =
        MyShiftRegisters(in, n, init, async)(n - 1)
}
