package DigitalPhaseMeter

import chisel3._
import java.nio.file._
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
        name: String =
            this.getClass.getSimpleName.stripSuffix("$").stripPrefix("Emit")
    ): Unit = {
        val outputDir = Paths.get("build", "chisel", name)
        val chiselArgs = args.concat(
            Array("--split-verilog", "--target-dir", outputDir.toString())
        )
        val firtoolOpts = Array(
            "-disable-all-randomization",
            "-default-layer-specialization=enable"
        )
        Files.createDirectories(outputDir)
        ChiselStage.emitSystemVerilogFile(genModule, chiselArgs, firtoolOpts)
    }
}
