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
        addCompilerPlugin(
            "org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full
        )
    )
