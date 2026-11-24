# sky130 HD 的层 RC 估算参数（单位：pF/um、kOhm/um），
# 取自 OpenROAD sky130hd 平台的 correlateRC 结果，用于布局阶段的时序修复。

set_layer_rc -layer li1  -capacitance 1.499e-04   -resistance 7.176e-02
set_layer_rc -layer met1 -capacitance 1.72375e-04 -resistance 8.929e-04
set_layer_rc -layer met2 -capacitance 1.36233e-04 -resistance 8.929e-04
set_layer_rc -layer met3 -capacitance 2.14962e-04 -resistance 1.567e-04
set_layer_rc -layer met4 -capacitance 1.48128e-04 -resistance 1.567e-04
set_layer_rc -layer met5 -capacitance 1.54087e-04 -resistance 1.781e-05

set_layer_rc -via mcon  -resistance 9.249146e-3
set_layer_rc -via via   -resistance 4.5e-3
set_layer_rc -via via2  -resistance 3.368786e-3
set_layer_rc -via via3  -resistance 0.376635e-3
set_layer_rc -via via4  -resistance 0.00580e-3
