package DigitalPhaseMeter

import chisel3.*
import chisel3.util.*

object MyShiftRegisters:
    def apply(in: Bool, n: Int, init: Bool = false.B): Seq[Bool] =
        require(n >= 2)
        val pipeline = RegInit(Fill(n, init))
        addAttribute(pipeline, """ASYNC_REG = "TRUE"""")
        pipeline := Cat(pipeline.take(n - 1), in)
        pipeline.asBools

object MyShiftRegister:
    def apply(in: Bool, n: Int, init: Bool = false.B): Bool = MyShiftRegisters(in, n, init).last
