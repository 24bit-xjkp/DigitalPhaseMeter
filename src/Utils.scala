package DigitalPhaseMeter

import java.nio.file.*

import scala.jdk.CollectionConverters.*

import chisel3.*
import chisel3.choice.*
import circt.stage.ChiselStage

/** 全局配置
  *
  * 集中控制计数器位宽与复位风格。各模块通过 `(using cfg: Config)` 取得配置；同一个模块在两种 复位风格下会生成实现不同、名字不同的两份 RTL（见 `suffix`），因此 FPGA 与 ASIC 两个平台
  * 分支可以同时编进同一个仿真库而不互相覆盖。
  *
  * @param asyncReset
  *   true 时隐式复位与 `ResetSynchronizer` 的输出取 `AsyncReset`（ASIC 分支），false 时取 同步复位 `Bool`（FPGA 分支）
  * @param gateCntWidth
  *   闸门计数器宽度（`Controller`）
  * @param edgeCntWidth
  *   边沿计数器宽度（`PhaseMeter` / `Controller`）
  * @param phaseCntWidth
  *   相位计数器宽度（`PhaseMeter` / `Controller`）
  */
case class Config(
    val asyncReset: Boolean = false,
    val gateCntWidth: Int = 28,
    val edgeCntWidth: Int = 32,
    val phaseCntWidth: Int = 32
):
    /** 模块名后缀：同步复位分支为 `S`，异步复位分支为 `A` */
    def suffix = if asyncReset then "A" else "S"

trait Main:
    given cfg: Config = Config()
    protected var args = Array[String]() // scalafix:ok DisableSyntax.var

    final def main(args: Array[String]): Unit =
        this.args = args
        emit

    protected def emit: Unit

    /** 生成SystemVerilog源文件
      *
      * @param genModule
      *   返回RawModule的回调函数
      * @param name
      *   输出目录名称，默认为类名
      */
    def emitSystemVerilog(
        genModule: => RawModule,
        name: String = this.getClass.getSimpleName.stripSuffix("$").stripPrefix("Emit")
    ): Unit =
        val outputDir = Paths.get("build", "chisel", name)
        val chiselArgs = args ++ Array("--target-dir", outputDir.toString(), "--warnings-as-errors")
        val firtoolOpts = Array(
            "-disable-all-randomization",
            "--lowering-options=emittedLineLength=120",
            "--preserve-aggregate=1d-vec",
            "--verification-flavor=sva",
            "--warn-on-implicit-truncation",
            "--warn-on-unprocessed-annotations"
        )
        Files.createDirectories(outputDir)
        ChiselStage.emitSystemVerilogFile(genModule, chiselArgs, firtoolOpts)
        appendVerificationBinds(outputDir)

    /** 把 firtool 提取到 `verification/` 下的 bind 文件补进 filelist.f
      *
      * `chisel3.ltl.AssertProperty` 默认使用 `Verification.Assert` 层（`LayerConfig.Extract`）：checker 会 被列进 filelist.f，但
      * `layers-*.sv` 的 bind 文件不会。仿真侧需要 checker + bind，综合/IP 打包则由 `vivado/package_*.tcl` 按 `verification/` 前缀过滤掉。
      */
    private def appendVerificationBinds(outputDir: Path): Unit =
        val filelist = outputDir.resolve("filelist.f")
        val verificationDir = outputDir.resolve("verification")
        if Files.isRegularFile(filelist) && Files.isDirectory(verificationDir) then
            val lines = Files.readAllLines(filelist).asScala.toSeq
            val stream = Files.walk(verificationDir)
            val binds =
                try
                    stream.iterator.asScala
                        .filter(path => Files.isRegularFile(path) && path.getFileName.toString.startsWith("layers-"))
                        .map(path => outputDir.relativize(path).toString.replace(java.io.File.separatorChar, '/'))
                        .toSeq
                finally stream.close()
            val missing = binds.distinct.sorted.filterNot(lines.contains)
            if missing.nonEmpty then Files.write(filelist, (lines ++ missing).asJava)

/** 带显式时钟/复位的公共顶层
  *
  * 对外端口固定为 `clock` / `resetn`（低有效异步复位），内部由 `ResetSynchronizer` 生成 主时钟域的复位并同时作为 `ImplicitClock` /
  * `ImplicitReset`。`MmcmDrpController` 直接继承 本 trait；`DigitalPhaseMeterTop` 因为要按平台分支选择实现，改用 `RawModule` 包一层
  * `ModuleChoice`，实现分支再用 `FixedIORawModule` 复用同一份端口定义（见 `DigitalPhaseMeterTop.scala`）。
  */
trait TopModule(using cfg: Config) extends RawModule with ImplicitClock with ImplicitReset:
    val clock = IO(Input(Clock()))
    val resetn = IO(Input(AsyncReset()))

    protected val reset = ResetSynchronizer(clock, resetn)
    override protected def implicitClock = clock
    override protected def implicitReset = reset

/** `chisel3.choice` 的平台选择组
  *
  * `DigitalPhaseMeterTop` 用它把 FPGA（同步复位）与 ASIC（异步复位）两个实现分支编进同一份 生成的 SV；firtool 输出 `` `ifdef targets$Platform$FPGA `` /
  * `` `ifdef targets$Platform$ASIC `` 条件分支，消费方必须定义且只定义一个宏。
  */
object Platform extends Group:
    object FPGA extends Case
    object ASIC extends Case
