package DigitalPhaseMeter

import java.nio.file._

import scala.jdk.CollectionConverters._

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
            "--lowering-options=emittedLineLength=120",
            "--preserve-aggregate=1d-vec",
            "--verification-flavor=sva"
        )
        Files.createDirectories(outputDir)
        ChiselStage.emitSystemVerilogFile(genModule, chiselArgs, firtoolOpts)
        appendVerificationBinds(outputDir)
    }

    /** 把 firtool 提取到 `verification/` 下的 bind 文件补进 filelist.f
      *
      * `chisel3.ltl.AssertProperty` 默认使用 `Verification.Assert` 层（`LayerConfig.Extract`）：checker 会 被列进 filelist.f，但
      * `layers-*.sv` 的 bind 文件不会。仿真侧需要 checker + bind，综合/IP 打包则由 `vivado/package_*.tcl` 按 `verification/` 前缀过滤掉。
      */
    private def appendVerificationBinds(outputDir: Path): Unit = {
        val filelist = outputDir.resolve("filelist.f")
        val verificationDir = outputDir.resolve("verification")
        if (Files.isRegularFile(filelist) && Files.isDirectory(verificationDir)) {
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
            if (missing.nonEmpty) Files.write(filelist, (lines ++ missing).asJava)
        }
    }
}

trait TopModule extends RawModule with ImplicitClock with ImplicitReset {
    val clock = IO(Input(Clock()))
    val resetn = IO(Input(Bool()))

    protected val reset = ResetSynchronizer(clock, resetn)
    override protected def implicitClock = clock
    override protected def implicitReset = reset
}
