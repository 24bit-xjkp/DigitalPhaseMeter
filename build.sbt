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
            "-Ymacro-annotations",
            // Chisel 的匿名 Bundle 会「推断出结构类型」，访问其成员需要 reflective call，因此只关掉这一类。
            "-Xlint:_,-infer-structural",
            "-Wunused:imports,privates,locals,patvars,implicits,explicits",
            "-Wdead-code",
            "-Werror"
        ),
        addCompilerPlugin(("org.chipsalliance" % "chisel-plugin" % chiselVersion).cross(CrossVersion.full))
    )
    .settings(RunMainCached.settings)
    .settings(addCommandAlias("scalaLint", "scalafixAll --check; compile"))
