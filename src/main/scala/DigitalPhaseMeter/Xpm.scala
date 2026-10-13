package DigitalPhaseMeter

import chisel3._
import chisel3.util._
import chisel3.util.experimental.ForceNameAnnotation

class SyncResetBundle extends Bundle { val dstRst = Output(Bool()) }

class SyncResetSynthesize(n: Int) extends ExtModule(Map("DEST_SYNC_FF" -> n)) {
    override def desiredName = "xpm_cdc_sync_rst"
    val src_rst = IO(Input(Bool()))
    val dest_clk = IO(Input(Clock()))
    val dest_rst = IO(Output(Bool()))
}

class SyncResetSimulate(n: Int) extends Module {
    val io = IO(new SyncResetBundle)
    val pipeline = Reg(UInt(n.W))
    pipeline := pipeline << 1 | reset.asBool
    io.dstRst := pipeline(n - 1)
}

class SyncReset(val n: Int = 4) extends Module {
    assert(n >= 2 && n <= 10)
    val io = IO(new SyncResetBundle)
    if (Config.outType == Config.OutType.synthesize) {
        val syncResetSynthesize = Module(new SyncResetSynthesize(n))
        syncResetSynthesize.src_rst := reset
        syncResetSynthesize.dest_clk := clock
        io.dstRst := syncResetSynthesize.dest_rst
    } else {
        val syncResetSimulate = Module(new SyncResetSimulate(n))
        io := syncResetSimulate.io
    }
}

object SyncReset {
    def apply(n: Int = 4): Bool = {
        val syncReset = Module(new SyncReset(n))
        syncReset.io.dstRst
    }
}

object EmitSyncResetSynthesize extends Main {
    emitSystemVerilog(new SyncReset(4))
}
