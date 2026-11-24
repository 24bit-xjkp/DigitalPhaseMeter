"""DigitalPhaseMeter 的 Yosys + OpenROAD sky130 HD ASIC 流程。

Python 负责阶段编排、PDK 派生文件准备与 filelist 改写；`asic/openroad/` 与
`asic/synth/` 下的 Tcl/Python 脚本只执行单个阶段。
"""

__version__ = "0.1.0"
