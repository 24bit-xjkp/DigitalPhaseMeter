package DigitalPhaseMeter

import chisel3._

/** 测量输入选择：在外部引脚输入与片内信号源之间切换，并给片内源加使能门控
  *
  * **工作在低速控制域（主时钟域），是测量核心之前的一级纯组合门控与二选一**：
  *
  * {{{
  *   sig1External（引脚 W18）────────────┐
  *                                      ├─► Mux ─► sig1 ─► Controller（异步输入，内部再经采样链同步）
  *   sig1Internal && enable（信号源）────┘
  * }}}
  *
  * 门控与选择都取自 `ControlRegs` 里软件写的 `SIG_CTRL`，两者同处主时钟域，因此「写寄存器」与「切换」在同一拍生效，不需要任何跨时钟域传递：
  *
  *   - `enable`：片内源使能。**只门控片内源**（0 时两路内源恒低），不影响外部通路的可用性，因此 `enable = 0`、`selectInternal = 0` 时选的仍是外部输入（外部是否真有信号无法在 PL
  *     内判断）；
  *   - `selectInternal`：0 = 外部输入，1 = 片内信号源。
  *
  * 输出送到控制器后就是一条**异步被测信号**：外部引脚与片内 MMCM 输出本来就是异步的（XDC 里两者分别按 `set_false_path -from [get_ports {sig1 sig2}]` 与 MMCM
  * 输出时钟处理），由 `PhaseMeter` 的 `EdgeDetector` 采样链在测量时钟域同步，与这一级切换发生在哪个域无关。
  *
  * 切换是立即生效的：在测量进行中切换会让本次结果无效（甚至一直等不到边沿），因此软件应在 `STATUS.busy = 0` 时切换。
  *
  * 本模块用 `RawModule`：`clock` / `resetn`
  * 是模块的**域标记**（声明本模块属于哪个时钟域、复位取什么极性），本模块当前没有时序逻辑，因此两者不参与任何逻辑，只在综合实现阶段影响布局与复位分配。`resetn` 按仓库约定是**低有效**（顶层直接接 PS 的
  * `resetn`）。
  */
class SignalInputMux extends RawModule {
    val io = IO(new Bundle {
        // 外部输入（引脚送来，异步）
        val sig1External = Input(Bool())
        val sig2External = Input(Bool())
        // 片内信号源输出（MMCM 的两路同频、相位差可调的时钟）
        val sig1Internal = Input(Bool())
        val sig2Internal = Input(Bool())
        // 片内源使能：0 时两路片内源恒低
        val enable = Input(Bool())
        // 输入选择：0 = 外部输入，1 = 片内信号源
        val selectInternal = Input(Bool())
        // 送给控制器、再进测量核心的信号（对测量域是异步输入）
        val sig1 = Output(Bool())
        val sig2 = Output(Bool())
    })

    // 使能只门控片内源：外部通路始终可用，enable 无效时选的仍是外部输入
    private val src1 = io.sig1Internal && io.enable
    private val src2 = io.sig2Internal && io.enable
    io.sig1 := Mux(io.selectInternal, src1, io.sig1External)
    io.sig2 := Mux(io.selectInternal, src2, io.sig2External)
}

object EmitSignalInputMux extends Main { emitSystemVerilog(new SignalInputMux) }
