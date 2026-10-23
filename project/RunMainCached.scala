/*
 * sbt 2 缓存驱动的入口点运行器，供 build.sbt 的 `runMainCached` 使用。
 *
 * 语义：入口点（主类）本身或其传递依赖发生变化时执行一次，否则跳过；需要无条件重跑时
 * 直接用 `runMain`（本任务不提供 --force）。
 * 「依赖」直接取字节码依赖闭包：从入口主类的 .class 文件出发，解析常量池里的类引用，
 * 沿项目自己的 class 目录递归，把可达的 .class 文件内容做 SHA-256。这比读源码可靠：
 *   - 不改变编译产物的编辑（例如在文件末尾追加注释）不会触发重跑；
 *   - 改某个入口点依赖的模块，只有字节码可达它的入口点会重跑；
 *   - 与入口点无关的模块改动不会触发它重跑；
 *   - 命中缓存后还会确认约定产物存在（`Emit<Name>` → `build/chisel/<Name>/filelist.f`），
 *     产物被删掉时会重新生成，避免“跳过执行但测试看不到模块”。
 * 项目之外的类（Chisel、Scala 库、JDK）不在项目 class 目录里，靠缓存键里的
 * dependencyClasspath 摘要统一兜底：依赖 jar 版本一变，所有入口点都会重跑。
 *
 * 缓存本身用 sbt 2 的 ActionCache（磁盘缓存，可被远程缓存后端替换），与
 * Def.cachedTask 背后是同一套机制；判定成功后写入“已成功执行”的事实，下次
 * 同一键直接跳过。为什么不用 Def.cachedTask 包住运行本身：InputTask 的命令
 * 行参数（主类名/程序参数）是解析期才拿到的运行期值，进不了 Def.cachedTask
 * 编译期生成的输入元组；这里显式构造缓存键来补齐这部分。
 *
 * 放在 project/ 且声明 `package sbt` 是为了复用 sbt 的 RunUtil 等内部实现。
 */
package sbt

import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Path}

import scala.collection.mutable
import scala.jdk.CollectionConverters.*

import sbt.Def.Initialize
import sbt.Keys.*
import sbt.internal.RunUtil
import sbt.internal.util.Attributed
import sbt.util.{ActionCache, CacheLevelTag, Digest}
import sbt.util.CacheImplicits.given
import xsbti.FileConverter

object RunMainCached {

    /** 缓存键格式版本：缓存键的构造方式变化时递增，使旧缓存条目失效。 */
    private val CacheFormatVersion = "runMainCached-v1"

    /** 缓存成功执行的事实（与 sbt 自身缓存测试成功结果的做法一致）。 */
    private val SuccessfulRun = 0

    val entryPointDigests = taskKey[Map[String, Digest]](
        "每个入口点（已发现主类）的字节码依赖摘要，供 runMainCached 判定是否需要执行"
    )

    val runMainCached = inputKey[Unit](
        "仅当入口点或其传递依赖发生变化时执行主类，否则跳过；用法: runMainCached <主类全名> [参数...]"
    )

    val settings: Seq[Setting[?]] = Seq(
        // 摘要要读编译产物（.class 内容），这些文件不是任务图的显式输入，不能放进
        // 自动缓存的任务里，否则会漏失效；开销只是遍历几十个 class 文件并做 SHA-256。
        entryPointDigests := Def.uncached {
            (Compile / compile).value
            val classesDir = (Compile / classDirectory).value.toPath
            val mainClasses = (Compile / discoveredMainClasses).value
            val classIndex = indexClasses(classesDir)
            val references = mutable.Map.empty[String, Seq[String]]
            def referencesOf(className: String): Seq[String] =
                references.getOrElseUpdate(
                    className,
                    classIndex.get(className).map(classReferences).getOrElse(Nil)
                )
            Map.from(mainClasses.map(name => name -> entryDigest(name, classIndex, referencesOf)))
        },
        runMainCached := inputTask.evaluated
    )

    /** 项目 class 目录里所有 class 文件：内部类名（`pkg/Foo$`） -> 文件路径。 */
    private def indexClasses(classDirectory: Path): Map[String, Path] = {
        val stream = Files.walk(classDirectory)
        try {
            stream.iterator.asScala
                .filter(_.toString.endsWith(".class"))
                .map { path =>
                    val name = classDirectory.relativize(path).toString.stripSuffix(".class")
                    name.replace(java.io.File.separatorChar, '/') -> path
                }
                .toMap
        } finally stream.close()
    }

    /** 读一个 .class 文件的常量池，返回它引用的所有类（JVM 内部名）。 只做常量池的浅解析，不需要 ASM；数组类型归一化为元素类型。
      */
    private def classReferences(path: Path): Seq[String] = {
        val buffer = ByteBuffer.wrap(Files.readAllBytes(path))
        if (buffer.getInt() != 0xcafebabe) return Nil
        buffer.getShort() // minor
        buffer.getShort() // major
        val constantPoolCount = buffer.getShort() & 0xffff
        val utf8 = new Array[String](constantPoolCount)
        val classNames = mutable.ArrayBuffer.empty[Int]
        var index = 1
        while (index < constantPoolCount) {
            (buffer.get() & 0xff) match {
                case 1 =>
                    val length = buffer.getShort() & 0xffff
                    val bytes = new Array[Byte](length)
                    buffer.get(bytes)
                    utf8(index) = new String(bytes, StandardCharsets.UTF_8)
                case 7 =>
                    classNames += (buffer.getShort() & 0xffff)
                case 8 | 16 | 19 | 20 =>
                    buffer.getShort()
                case 3 | 4 | 9 | 10 | 11 | 12 | 17 | 18 =>
                    buffer.getInt()
                case 5 | 6 =>
                    buffer.getLong()
                    index += 1 // long/double 占两个常量池槽位
                case 15 =>
                    buffer.get()
                    buffer.getShort()
                case _ =>
                    return Nil
            }
            index += 1
        }
        classNames.toSeq.flatMap { nameIndex =>
            if (nameIndex <= 0 || nameIndex >= constantPoolCount || utf8(nameIndex) == null) Nil
            else normalizeClassName(utf8(nameIndex)).toSeq
        }
    }

    /** `[Lpkg/Foo;` -> `pkg/Foo`；基本类型数组与普通类名原样返回。 */
    private def normalizeClassName(raw: String): Option[String] = {
        val name = raw.dropWhile(_ == '[')
        if (name.startsWith("L") && name.endsWith(";")) Some(name.substring(1, name.length - 1))
        else if (name == raw || name.isEmpty) Some(name)
        else None
    }

    /** 入口点的字节码依赖摘要：从主类 class 文件出发，沿项目 class 目录里的类引用做 BFS， 对可达的每个 .class 内容求 SHA-256。项目外的类由 dependencyClasspath 摘要兜底。
      */
    private def entryDigest(
        mainClass: String,
        classIndex: Map[String, Path],
        references: String => Seq[String]
    ): Digest = {
        val visited = mutable.Set.empty[String]
        val reached = mutable.ArrayBuffer.empty[String]
        val queue = mutable.Queue(mainClass.replace('.', '/'))
        while (queue.nonEmpty) {
            val className = queue.dequeue()
            if (visited.add(className)) {
                classIndex.get(className) match {
                    case Some(path) =>
                        val bytes = Files.readAllBytes(path)
                        reached += className + "\n" + Digest.sha256Hash(bytes).toString
                        references(className).foreach { referenced =>
                            if (classIndex.contains(referenced) && !visited.contains(referenced)) {
                                queue.enqueue(referenced)
                            }
                        }
                    case None => () // 项目外（依赖库/JDK）的类：交给 dependencyClasspath 摘要
                }
            }
        }
        if (reached.isEmpty) Digest.sha256Hash(mainClass.getBytes(StandardCharsets.UTF_8))
        else Digest.sha256Hash(reached.sorted.mkString("\n").getBytes(StandardCharsets.UTF_8))
    }

    /** 依赖 classpath 的摘要：依赖 jar 版本变化时所有入口点都应重跑，由该摘要兜底。 */
    private def dependencyDigest(classpath: Def.Classpath): Digest =
        Digest.sha256Hash(
            classpath
                .map(entry => s"${entry.data.id}>${entry.data.contentHashStr}/${entry.data.sizeBytes}")
                .sorted
                .mkString("\n")
                .getBytes(StandardCharsets.UTF_8)
        )

    /** `Emit<Name>` 生成主类的约定输出目录（`Main.emitSystemVerilog` 的默认命名）。 缓存只记录“执行过”，不记录产物本身；命中后若约定产物不在，仍然要重新生成。 非 `Emit*`
      * 主类没有约定输出，返回 None。
      */
    private def expectedOutputDirectory(baseDirectory: java.io.File, mainClass: String): Option[Path] = {
        val simpleName = mainClass.split('.').last
        if (simpleName.startsWith("Emit")) {
            val name = simpleName.stripPrefix("Emit")
            Some(baseDirectory.toPath.resolve("build").resolve("chisel").resolve(name))
        } else None
    }

    /** 执行主类。调用点在 ActionCache 的缓存未命中分支里，因此这里必须是普通方法 （不能是任务）——任务在任务图中总是会被求值，而缓存命中时不能运行主类。
      */
    private def runMainClass(
        mainClass: String,
        classpath: Seq[Path],
        appArgs: Seq[String],
        jvmArgs: Seq[String],
        runner: ScalaRun,
        forkOptions: ForkOptions,
        log: Logger
    ): Unit = {
        val (modifiedRun, _) = RunUtil.applyJvmArgs(runner, jvmArgs, forkOptions, log)
        modifiedRun.run(mainClass, classpath, appArgs, log).get
    }

    private def inputTask: Initialize[InputTask[Unit]] = Def.inputTask {
        val args = Def.spaceDelimited("<主类全名> [参数...]").parsed
        val (mainClass, allArgs) = args match {
            case main :: rest => (main, rest)
            case Nil          => sys.error("用法: runMainCached <主类全名> [参数...]")
        }
        val (jvmArgs, appArgs) = RunUtil.splitArgs(allArgs)

        val classpath = (Compile / fullClasspath).value
        val depClasspath = (Compile / dependencyClasspath).value
        val digests = entryPointDigests.value
        val scalaRunner = (Compile / run / runner).value
        val forkOpts = (Compile / run / forkOptions).value
        val config = Def.cacheConfiguration.value
        val converter = fileConverter.value
        val log = streams.value.log
        val classpathFiles = Attributed.data(classpath).map(converter.toPath)

        // 主类不在 discoveredMainClasses 里（例如来自依赖 jar）时退化为整个 classpath
        // 的摘要，保证缓存不会漏掉依赖变化。
        val stamp = digests.get(mainClass) match {
            case Some(digest) => digest
            case None         =>
                log.warn(
                    s"runMainCached: $mainClass 不在本项目的 discoveredMainClasses 中，" +
                        "改用整个 classpath 的摘要作为缓存键"
                )
                Digest.sha256Hash(
                    classpath
                        .map(entry => s"${entry.data.id}>${entry.data.contentHashStr}/${entry.data.sizeBytes}")
                        .mkString("\n")
                        .getBytes(StandardCharsets.UTF_8)
                )
        }

        // 会改变主类行为、但不在编译产物里的运行参数也必须进入缓存键：程序参数、JVM 参数，
        // 以及可能影响运行结果的 fork 配置（javaHome / JVM 选项 / 环境变量）。
        val runnerFingerprint = Seq(
            forkOpts.javaHome.fold("")(_.getAbsolutePath),
            forkOpts.runJVMOptions.mkString("\u0000"),
            forkOpts.envVars.toSeq.sortBy(_._1).map((k, v) => s"$k=$v").mkString("\u0000")
        ).mkString("\n")
        val cacheKey = Seq(
            CacheFormatVersion,
            // 缓存是机器级共享的；把工程根目录也纳入键，避免不同工程/不同检出互相命中。
            baseDirectory.value.getAbsolutePath,
            mainClass,
            jvmArgs.mkString("\u0000"),
            appArgs.mkString("\u0000"),
            stamp.toString,
            dependencyDigest(depClasspath).toString,
            runnerFingerprint
        ).mkString("\n")

        val outputDirectory = expectedOutputDirectory(baseDirectory.value, mainClass)
        val outputMissing = outputDirectory.exists(dir => !Files.isRegularFile(dir.resolve("filelist.f")))

        val codeContentHash = Digest.sha256Hash(CacheFormatVersion.getBytes(StandardCharsets.UTF_8))
        val extraHash = Digest.zero
        val tags = CacheLevelTag.all.toList

        def execute(): Unit = {
            log.info(s"runMainCached: 入口点 $mainClass 或其依赖发生变化，执行一次")
            runMainClass(mainClass, classpathFiles, appArgs, jvmArgs, scalaRunner, forkOpts, log)
        }

        // get 命中时会记录 sbt 的 cache hit 事件（与 Def.cachedTask 的统计口径一致）
        ActionCache.get[String, Int](cacheKey, codeContentHash, extraHash, tags, config) match {
            case Some(_) if outputMissing =>
                // 缓存说“执行过”，但约定产物被删了（例如手工清掉 build/chisel）：重新生成。
                log.warn("runMainCached: 缓存命中但约定生成物缺失，重新生成")
                execute()
            case Some(_) =>
                log.info(s"runMainCached: 入口点 $mainClass 及其依赖未变化，跳过执行（强制重跑请用 runMain）")
            case None =>
                ActionCache.cache[String, Int](cacheKey, codeContentHash, extraHash, tags, config) { _ =>
                    execute()
                    ActionCache.actionResult(SuccessfulRun)
                }
        }
    }
}
