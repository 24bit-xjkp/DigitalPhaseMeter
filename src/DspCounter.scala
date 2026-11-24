package DigitalPhaseMeter

import chisel3.*
import chisel3.experimental.prefix
import chisel3.util.*

/** 使能型二进制计数器（FPGA 用 DSP48E1 累加器实现，ASIC 用分段流水线实现）
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
  * 实现由 `cfg.platform` 选择：FPGA 用单个 DSP48E1 累加器；ASIC 用 `cfg.asicDspCounterPipeline` 级分段流水线（段间进位寄存，输出比实际计数晚 `depth - 1`
  * 拍）。ASIC 的输出对齐延迟链不随 `clear` 复位，因此 `clear` 在输出上也要等 `depth - 1` 拍才完全排空；排空期间各段旧值 与已清零的高段可能混合，输出不应被读取。`PhaseMeter`
  * 的空闲清零与停止响应已按这个延迟安排，不会在排空窗口内读数。
  *
  * @param cntWidth
  *   计数器宽度，上限 48
  * @param cfg
  *   全局配置：平台与 ASIC 分段流水线的级数
  */
class DspCounter(val cntWidth: Int = 48)(using val cfg: Config) extends Module:
    require(cntWidth <= 48)
    val io = IO(new Bundle:
        val clear = Input(Bool())
        val enable = Input(Bool())
        val value = Output(UInt(cntWidth.W)))
    override protected def implicitReset: Reset = io.clear

    private val cnt = RegInit(0.U(cntWidth.W))
    private def genCounter(useDsp: Boolean) =
        if useDsp then addAttribute(cnt, """use_dsp = "yes"""")
        when(io.enable) { cnt := cnt + 1.U }
        io.value := cnt
    private val depth = cfg.asicDspCounterPipeline
    // 除最高块外每块的位数
    private val bitsPerBlock = (cntWidth + depth - 1) / depth
    private def genPipelineCounter =
        val cntBlocks = cnt.asTypeOf(Vec(depth, UInt(bitsPerBlock.W)))
        val nextBlocks, valueBlocks = Wire(chiselTypeOf(cntBlocks))
        cntBlocks
            .lazyZip(nextBlocks)
            .lazyZip(valueBlocks)
            .lazyZip(cntBlocks.indices)
            .foldLeft(1.U):
                case (carryIn, (cntBlock, nextBlock, valueBlock, i)) =>
                    prefix(s"block$i"):
                        val delay = ShiftRegister(cntBlock, depth - 1 - i)
                        valueBlock := delay
                        val next = cntBlock +& (carryIn & io.enable)
                        nextBlock := next.tail(1)
                        val carry = RegNext(next.head(1))
                        carry
        cnt := nextBlocks.asUInt.take(cntWidth)
        io.value := valueBlocks.asUInt.take(cntWidth)

    cfg.platform match
        case Platform.FPGA => genCounter(true)
        case Platform.ASIC =>
            require(depth >= 1 && bitsPerBlock >= 2)
            if depth == 1 then genCounter(false) else genPipelineCounter

object DspCounter:
    def apply(cntWidth: Int, clear: Bool, enable: Bool)(using cfg: Config): UInt =
        val dspCounter = Module(new DspCounter(cntWidth))
        dspCounter.io.clear := clear
        dspCounter.io.enable := enable
        dspCounter.io.value

// 独立产物按测量域实际使用的 32 位生成（PhaseMeter 的 edgeCnt/phaseCnt 宽度），
// 这样 test/DspCounter.cpp 的端口类型与真实用例一致。
object EmitDspCounter extends Main:
    protected def emit = emitSystemVerilog(new DspCounter(32))

object EmitDspCounterA extends Main(using Config.asic):
    protected def emit = emitSystemVerilog(new DspCounter(32))
