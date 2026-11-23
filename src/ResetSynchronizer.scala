package DigitalPhaseMeter

import chisel3.*

/** 复位同步器：异步置位、同步释放
  *
  * @param n
  *   同步释放的延迟级数
  */
class ResetSynchronizer(val n: Int = 4) extends RawModule:
    require(n >= 2)
    val clock = IO(Input(Clock()))
    // 异步复位输入，低有效
    val resetn = IO(Input(AsyncReset()))
    // 与 clock 同步的复位输出，高有效
    val reset = IO(Output(Bool()))

    // 复位链的寄存器用异步复位：resetn 拉低时立即清零，输出随之置位
    reset := withClockAndReset(clock, (!resetn.asBool).asAsyncReset) { MyShiftRegister(false.B, n, true.B) }

object ResetSynchronizer:
    /** 例化复位同步器，并按 `cfg.asyncReset` 选择输出类型
      *
      * 同步链本身只产生与 `clock` 对齐的高有效 `Bool`；`cfg.asyncReset = true`（ASIC 分支）时 把它转成 `AsyncReset`，否则保持同步复位 `Bool`（FPGA 分支）。
      *
      * @return
      *   `AsyncReset` 或 `Bool` 类型的高有效复位
      */
    def apply(clock: Clock, resetn: Reset, n: Int = 4)(using cfg: Config): Reset =
        val synchronizer = Module(new ResetSynchronizer(n))
        synchronizer.clock := clock
        synchronizer.resetn := resetn.asAsyncReset
        if cfg.asyncReset then synchronizer.reset.asAsyncReset else synchronizer.reset

object EmitResetSynchronizer extends Main:
    protected def emit = emitSystemVerilog(new ResetSynchronizer)
