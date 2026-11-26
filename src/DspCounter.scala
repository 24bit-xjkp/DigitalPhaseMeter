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
  * 实现由 `cfg.platform` 选择：FPGA 用单个 DSP48E1 累加器；ASIC 用 `cfg.asicDspCounterPipeline` 级分段流水线（段间进位寄存）。 ASIC 的 `io.value`
  * 直接取各段寄存器，**没有输出对齐延迟链**：段间进位是寄存的，计数刚跨过段边界时高段要晚一拍才收到进位（每段 `bitsPerBlock = ceil(cntWidth / depth)` 位），因此输出最多要
  * `depth - 1` 拍才稳定到最终计数值。调用方必须在撤销使能后等 `depth - 1` 拍再读（`PhaseMeter` 的停止响应按这个
  * 延迟安排，且实际读数发生在慢时钟域的握手里，远晚于进位传播完）；连续计数、没有跨段进位时输出逐拍就是当前计数值。 ASIC 的 `clear` 走计数寄存器的异步复位端（见
  * `implicitReset`），清零同拍生效；进位寄存器不清零，在 clear 保持期间（idle）随 段寄存器归零而自然清零。
  *
  * ASIC 分段流水线的段间进位是**无条件传播**的：只有 block0 的加数受 `enable` 门控，更高段直接加前一段的进位寄存器。 这样 enable
  * 在某个进位拍之后撤销时，已经在流水线里的进位仍会继续传到高段，`depth - 1` 拍后 `io.value` 上就是冻结后的完整计数值。不要把每段都写成 `carryIn & enable`：那会在 enable
  * 撤销时丢掉最后一拍产生的进位 （0x0F + 1 后立即停使能，输出会停在 0 而不是 16）。
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
    // clear 的实现方式随平台不同：
    //   * FPGA：DSP48E1 只有同步复位（RSTP），`io.clear` 保持同步 Bool；
    //   * ASIC：把 clear 接到计数寄存器的**异步复位端**（sky130 `dfrtp` 的 RESET_B），
    //     清零从「D 输入路径上的 mux」移到复位脚。450MHz 的 ss 关键路径原本是
    //     `clearCnt → 每级计数器的清零 mux → 计数器 D`；改成异步复位后 D 路径只剩
    //     段内加法器，清零变成复位脚上的 recovery/removal 检查。
    // 异步复位的撤销必须与 dspClock 同步：`clearCnt` 是 `PhaseMeter` 里由同一时钟
    // 驱动的寄存器，因此是「异步置位、同步撤销」，不需要再串一级复位同步器；STA 把清零
    // 检查从 setup 变成复位脚上的 recovery/removal（450MHz 实测各配置均为正）。
    override protected def implicitReset: Reset =
        if cfg.platform == Platform.ASIC then io.clear.asAsyncReset else io.clear

    private val cnt = RegInit(0.U(cntWidth.W))
    private def genCounter(useDsp: Boolean) =
        if useDsp then addAttribute(cnt, """use_dsp = "yes"""")
        when(io.enable) { cnt := cnt + 1.U }
    private val depth = cfg.asicDspCounterPipeline
    // 每段的位数（向上取整）：32 位计数器在 depth = 8 / 16 下分别是 4 位 / 2 位一段。
    // 段宽越小，段内加法器与进位逻辑越浅，代价是进位寄存器（FF）变多。
    private val bitsPerBlock = (cntWidth + depth - 1) / depth
    private def genPipelineCounter =
        // retime 实验：分段流水线（段寄存器 + 进位寄存器）都标 abc_retime = "false"；
        // synth_retime.ys 据此在 ABC 之前把这些 FF 先映射成库单元，避免 dretime 把流水线拆平。
        // 自定义属性对不跑 retime 的交付基线没有副作用。
        addAttribute(cnt, """abc_retime = "false"""")
        val cntBlocks = cnt.asTypeOf(Vec(depth, UInt(bitsPerBlock.W)))
        val nextBlocks = Wire(chiselTypeOf(cntBlocks))
        cntBlocks
            .lazyZip(nextBlocks)
            .lazyZip(cntBlocks.indices)
            .foldLeft(io.enable.asUInt):
                case (carryIn, (cntBlock, nextBlock, i)) =>
                    prefix(s"block$i"):
                        val next = cntBlock +& carryIn
                        nextBlock := next.tail(1)
                        val carry = RegNext(next.head(1))
                        addAttribute(carry, """abc_retime = "false"""")
                        carry
        cnt := nextBlocks.asUInt.take(cntWidth)

    cfg.platform match
        case Platform.FPGA => genCounter(true)
        case Platform.ASIC =>
            require(depth >= 1)
            val blocks = (cntWidth + bitsPerBlock - 1) / bitsPerBlock
            require(depth == blocks, s"流水线设定深度为$depth，实际为$blocks")
            if depth == 1 then genCounter(false) else genPipelineCounter
    io.value := cnt

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
