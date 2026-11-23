package DigitalPhaseMeter

import chisel3.*
import chisel3.util.*

/** 计数器的复位类型
  *
  * 决定是否声明复位端口以及端口的类型；模块名后缀与之一一对应。
  */
enum DspCounterResetType:
    /** 同步复位：`reset: Bool`，模块名后缀 `S` */
    case Sync

    /** 异步复位：`reset: AsyncReset`，模块名后缀 `A` */
    case Async

    /** 不复位：不声明复位端口，模块名不带后缀（`PhaseMeter` 的两个计数器靠 `clear` 清零） */
    case None

/** 使能型二进制计数器（DSP48E1 累加器实现）
  *
  * 语义：`clear` 优先；`clear` 无效且 `enable` 有效时 +1；否则保持（模 `2^cntWidth`）。
  *
  * 为什么写成 `when/elsewhen` 而不是 `Mux(clear, 0, cnt + enable)`： 后者的「+enable」会被综合器推断成 DSP48E1 的 CARRYIN 输入，使能要经一级 LUT 才到
  * CARRYIN（2026-10-19 实测这条路径只剩 +0.088ns，另一次实现低到 -0.049ns）。 写成两级条件后映射成 RSTP（同步复位）+ CEP（时钟使能），使能/清零由寄存器直接进 DSP 控制脚，不再有
  * LUT 夹在中间。
  *
  * DSP48E1 在 xc7z010clg400-1（-1 速度等级）上的全寄存器累加器 FMAX 是 464.25MHz （DS187 Table 66），450MHz 用掉约 97%；是否满足以 Vivado
  * 的时序分析为准——实现后 DSP 内部路径与 CEP/RSTP 控制路径都在 2.222ns 约束下检查过，余量见 vivado/README.md。
  *
  * @param cntWidth
  *   计数器宽度，上限 48
  * @param resetType
  *   复位类型（见 `DspCounterResetType`）：`Sync` / `Async` 分别声明同步 / 异步复位端口， `None` 不声明复位端口。`DspCounter.apply` 按
  *   `Config.asyncReset` 与 `withReset` 选择； `PhaseMeter` 里的两个计数器都靠 `clear` 清零，因此传 `withReset = false`（即 `None`）
  */
class DspCounter(val cntWidth: Int = 48, val resetType: DspCounterResetType = DspCounterResetType.Sync)
    extends RawModule
    with ImplicitClock:
    require(cntWidth <= 48)
    // RawModule 不提供隐式时钟，显式声明并由 `DspCounter.apply` 接到父模块的时钟
    val clock = IO(Input(Clock()))
    // 复位端口的类型与是否存在由 resetType 决定；`suffix` 只用于 desiredName，不是硬件
    val reset = FlatIO(new Bundle {
        val (reset, suffix) = resetType match
            case DspCounterResetType.Sync  => Some(Input(Bool())) -> "S"
            case DspCounterResetType.Async => Some(Input(AsyncReset())) -> "A"
            case DspCounterResetType.None  => None -> ""
    })

    override protected def implicitClock: Clock = clock
    override def desiredName: String = s"DspCounter${reset.suffix}"
    val io = IO(new Bundle:
        val clear = Input(Bool())
        val enable = Input(Bool())
        val value = Output(UInt(cntWidth.W)))

    private val cnt = reset.reset match
        case Some(r) => chisel3.withReset(r) { RegInit(0.U(cntWidth.W)) }
        case None    => Reg(UInt(cntWidth.W))
    addAttribute(cnt, """use_dsp = "yes"""")
    when(io.clear) { cnt := 0.U }.elsewhen(io.enable) { cnt := cnt + 1.U }
    io.value := cnt

object DspCounter:
    /** 例化计数器并接到当前隐式时钟/复位
      *
      * @param withReset
      *   false 时不声明复位端口（`PhaseMeter` 的计数器靠 `clear` 清零）；true 时按 `cfg.asyncReset` 选择同步（`Bool`）或异步（`AsyncReset`）复位
      */
    def apply(cntWidth: Int, clear: Bool, enable: Bool, withReset: Boolean = true)(using cfg: Config): UInt =
        def getReset = (withReset, cfg.asyncReset) match
            case (false, _)    => DspCounterResetType.None
            case (true, true)  => DspCounterResetType.Async
            case (true, false) => DspCounterResetType.Sync
        val dspCounter = Module(new DspCounter(cntWidth, getReset))
        dspCounter.clock := Module.clock
        dspCounter.reset.reset match
            case Some(reset) => reset := Module.reset
            case None        =>
        dspCounter.io.clear := clear
        dspCounter.io.enable := enable
        dspCounter.io.value

// 独立产物按测量域实际使用的 32 位生成（PhaseMeter 的 edgeCnt/phaseCnt 宽度），
// 这样 test/DspCounter.cpp 的端口类型与真实用例一致。
object EmitDspCounter extends Main:
    protected def emit = emitSystemVerilog(new DspCounter(32))
