ThisBuild / scalaVersion := "2.13.18"
ThisBuild / version := "0.1.0"
val chiselVersion = "7.16.0"
lazy val DigitalPhaseMeter = (project in file("."))
    .settings(
        name := "DigitalPhaseMeter",
        libraryDependencies ++= Seq(
            "org.chipsalliance" %% "chisel" % chiselVersion
        ),
        scalacOptions ++= Seq(
            "-language:reflectiveCalls",
            "-deprecation",
            "-feature",
            "-Xcheckinit",
            "-Ymacro-annotations"
        ),
        addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full)
    )
    // 入口点缓存：入口点或其传递依赖未变化时跳过执行（实现见 project/RunMainCached.scala）
    .settings(RunMainCached.settings)
