"""DigitalPhaseMeter 的 Vivado 增量综合实现流程。

Python 负责哈希、stamp、run 指纹与复用/重跑决策；Tcl worker 只执行线性 Vivado 命令。
"""

__version__ = "0.1.0"
