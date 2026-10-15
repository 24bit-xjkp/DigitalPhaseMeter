package DigitalPhaseMeter

import chisel3._
import java.nio.file._
import circt.stage.ChiselStage
import scala.collection.mutable.ArrayBuffer

object Config {
    object OutType extends Enumeration {
        type Type = Value
        val simulate, synthesize = Value
    }
    var _outType: Option[OutType.Type] = None

    def outType = _outType.get
}

trait Main extends App {

    /** 提取指定参数
      *
      * @param args
      *   参数列表
      * @param name
      *   参数名
      * @return
      *   参数值，为空表示不存在指定参数
      */
    def extractOption(args: ArrayBuffer[String], name: String): Option[String] = {
        val prefix = s"--$name="
        val idx = args.indexWhere(_.startsWith(prefix))
        if (idx < 0) { None }
        else {
            val value = args(idx).substring(prefix.length)
            args.remove(idx)
            Some(value)
        }
    }

    /** 生成SystemVerilog源文件
      *
      * 产物目录为 `build/chisel/<name>/<type>`，**两种产物必须分开存放**：`--type=sim` 与 `--type=syn` 生成的 SystemVerilog 内容不同（当前唯一差异是
      * `SyncReset`， 仿真用纯 RTL 移位链 `SyncResetSimulate`、综合用 `xpm_cdc_sync_rst` 原语），放同一个目录时后跑的那次会覆盖前一次，于是「跑完综合再跑 Verilator
      * 测试」会因为 Verilator 侧没有 XPM 模型而报 `Cannot find file containing module: 'xpm_cdc_sync_rst'`（本项目实测）。
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
        var commandArgs = args.to(ArrayBuffer)
        val typeStr = extractOption(commandArgs, "type").getOrElse("sim")
        typeStr match {
            case "sim" => Config._outType = Some(Config.OutType.simulate)
            case "syn" => Config._outType = Some(Config.OutType.synthesize)
            case other => sys.error(s"--type选项未知取值: $other")
        }
        val outputDir = Paths.get("build", "chisel", name, typeStr)
        val chiselArgs = commandArgs.toArray.concat(
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
