package DigitalPhaseMeter

import chisel3._
import java.nio.file._
import circt.stage.ChiselStage
trait Main extends App {

    /** 生成SystemVerilog源文件
      *
      * 产物目录为 `build/chisel/<name>/`，每个模块只生成**一份**：复位同步由纯 RTL 的 `ResetSynchronizer` 实现（异步置位、同步释放），仿真与综合共用同一份 产物，不再有按
      * `--type` 区分的 sim / syn 两套。
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
        val chiselArgs = args.toArray.concat(
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
