package DigitalPhaseMeter

import java.nio.file._

import chisel3._
import circt.stage.ChiselStage

trait Main extends App {

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
    ): Unit = {
        val outputDir = Paths.get("build", "chisel", name)
        val chiselArgs = args.toArray.concat(Array("--target-dir", outputDir.toString()))
        val firtoolOpts = Array(
            "-disable-all-randomization",
            "-default-layer-specialization=enable",
            "--lowering-options=emittedLineLength=120",
            "--preserve-aggregate=1d-vec"
        )
        Files.createDirectories(outputDir)
        ChiselStage.emitSystemVerilogFile(genModule, chiselArgs, firtoolOpts)
    }
}

trait TopModule extends RawModule with ImplicitClock with ImplicitReset {
    val clock = IO(Input(Clock()))
    val resetn = IO(Input(Bool()))

    protected val reset = ResetSynchronizer(clock, resetn)
    override protected def implicitClock = clock
    override protected def implicitReset = reset
}
