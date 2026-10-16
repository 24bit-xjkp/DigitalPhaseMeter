# PS 侧裸机程序与宿主机验证台（`ps/`）

Zynq-7010（`xc7z010clg400-1`）PS 侧的 **freestanding 裸机 C 串口命令服务端**：
按 `docs/ps-protocol.md` 解析 PC 上位机的命令帧，读写 PL 的 AXI4-Lite 寄存器与片内
信号源 MMCM 的 DRP 口，完成单次/连续测量、闸门时间、输入源选择、片内源频率设置与
结果回传；**同一份命令处理核心**在宿主机（x86-64）上以「寄存器级 PL 仿真后端 + pty
服务端」的形式运行，用于无硬件时的端到端验证。

```text
ps/
├── include/   dpm_regs.h(冻结) dpm_calc.h(冻结) dpm_proto.h(冻结)
│              dpm_io.h        # 寄存器 IO vtable（真实 MMIO / 仿真后端共用）
│              dpm_cmd.h       # 命令核心上下文、PAYLOAD 结构
│              dpm_board.h     # 板级驱动接口（UART/GIC/时间源）
├── src/       dpm_main.c dpm_cmd.c dpm_proto.c dpm_calc.c dpm_sig.c dpm_div64.c
│              dpm_regs_io.c dpm_uart.c dpm_irq.c startup.c linker.ld
├── sim/       fpga_model.{c,h}（PL 仿真后端） host_main.c（pty/stdio 服务端）
├── tests/     test_calc.c test_proto.c test_sig.c test_server_selftest.py
│              e2e_session_record.py gen_golden_table.py
├── build.sh   CMakeLists.txt
└── build/     构建产物（不入库）
```

## 1. 构建

```bash
bash ps/build.sh              # 交叉编译 dpm_ps.elf + 宿主机 host_main
bash ps/build.sh --ps-only    # 只交叉编译
bash ps/build.sh --host-only  # 只构建 host_main

cmake -S ps -B ps/build/cmake && cmake --build ps/build/cmake -j4
ctest --test-dir ps/build/cmake --output-on-failure     # 4 个用例
```

**为什么必须 freestanding**：本机的 `arm-none-eabi-gcc` 只装了 M-profile 的 multilib，
链接 newlib/libgcc 会报 `conflicting architecture profiles A/M`。因此 PS 侧一律
`-mcpu=cortex-a9 -marm -ffreestanding -nostdlib -nostartfiles -ffunction-sections
-fdata-sections -Wl,--gc-sections`，自带 `startup.c`（栈/BSS/向量）与 `dpm_div64.c`
（`__aeabi_uidiv`/`__aeabi_uidivmod`/`__udivdi3`/`__umoddi3`/`__aeabi_uldivmod`），
不 include 任何 libc 头、不使用浮点与可变参数。构建脚本最后用 `nm -u` 断言
**没有未定义符号**，这正是"没有偷偷依赖 libgcc/newlib"的证据。

### 实测输出（`bash ps/build.sh`，原始日志见 `spec/ps_selftest/build.log`）

```text
== 交叉编译（arm-none-eabi-gcc, Cortex-A9, freestanding）
   CC  dpm_main.c / dpm_proto.c / dpm_calc.c / dpm_sig.c / dpm_cmd.c / dpm_div64.c
   CC  dpm_regs_io.c / dpm_uart.c / dpm_irq.c / startup.c
   LD  dpm_ps.elf
== file 校验
ps/build/dpm_ps.elf: ELF 32-bit LSB executable, ARM, EABI5 version 1 (SYSV),
  statically linked, with debug_info, not stripped
== arm-none-eabi-size
   text	   data	    bss	    dec	    hex	filename
  10780	      4	  16724	  27508	   6b74	ps/build/dpm_ps.elf
== 未定义符号检查（必须为空，否则说明还依赖 libgcc/newlib）
   （无未定义符号）
== 宿主机构建（clang, x86-64）
   构建完成：ps/build/host_main
```

> 链接器会提示 `has a LOAD segment with RWX permissions`：这是单段 OCM 镜像
> （代码+数据同段）的正常提示，可以用 `-Wl,-z,noexecstack` 之类消掉，但本工程
> 保持简单。全程序编译**零警告**（`-Wall -Wextra -Werror -Wshadow -Wundef
-Wpointer-arith -Wcast-align -Wstrict-prototypes -Wmissing-prototypes`）。

## 2. 宿主机验证台

```bash
ps/build/host_main --device /dev/pts/N        # 用已有 pty 从端（验证脚本用这个）
ps/build/host_main --pty                      # 自己建 pty，把从端路径打到 stdout
ps/build/host_main --stdio                    # 协议字节走 stdin/stdout

# 可选参数
--freq-hz N --phase-mdeg N --gate N           # 片内源初值 / 初始闸门
--slow-hz N --dsp-hz N --seed N --speed N     # 时钟、确定性种子、仿真加速
--ext-present --ext-freq-hz N --ext-phase-mdeg N  # 模拟外部引脚接信号
--unlock                                      # 测试钩子：MMCM 永不锁定
--verbose                                     # 日志走 stderr（不影响协议字节）
```

约定：协议字节只走数据通道，日志只走 stderr；数据通道 EOF、交互式 stdin 的 EOF 或
`SIGTERM`/`SIGINT` 都**干净退出且返回 0**。

### 仿真后端的建模范围与简化（不要当成硬件）

| 方面 | 建模 | 简化/假设 |
| --- | --- | --- |
| 寄存器语义 | 逐个对齐 `ControlRegs.scala`：start 脉冲（busy 期间忽略）、busy/resultValid/lost/irqPending、结果覆盖即 `lostCount++`、IRQ_STATUS W1C、`CTRL.clear` busy 期间挂起、SIG_CTRL 写后配置快照延迟若干慢周期（期间 DPM 写通道挂起） | — |
| 测量模型 | `gate = target + ceil(T_sig/T_slow)`；`edge = floor(gate·f_sig/f_slow)`；相位先量化到整数个 DSP 周期（整数 RNE）再乘沿数 → `phase_cnt = cycles·edge` | 与 `spec/gen_golden.py` 同口径，逐位一致 |
| 时间推进 | **每次 AXI 事务**推进 64 个慢周期；主循环每轮再用 `--speed`（默认 4096）额外推进，让虚拟时间与墙钟解耦（宿主机负载高时 `poll(2ms)` 实际节拍会变慢，否则 1ms 流间隔永远等不到） | 真实硬件是自由运行的时钟 |
| 源不可用 | 片内源未使能/未锁定，或外部引脚无信号（默认）→ 两路恒低：`busy` 恒为 1、不产生结果、`RESULT_SEQ` 不自增 | 与真实 PL 的「等参考沿」一致 |
| 片内源相位 | **固定 90°**（`DPM_MODEL_SRC_PHASE_MDEG`）：与上一版 clk_wiz 方案的实测结论一致（DRP 相位字没接到 MMCM，写它不改变输出相位） | 当前方案（裸 MMCM + 极简 DRP 控制器）下相位就是 MMCM 的相位寄存器，仿真后端可按需改为「跟随请求相位」 |
| DRP 映射 | `0x200` CLKFBOUT、`0x208`/`0x214` 分频（低字节 = D）、`0x20C`/`0x218` 相位（**毫度**）、`0x25C` Load=0x07、状态寄存器 `0x004`（**上述 clk_wiz 语义映射已作废**：当前方案写的是 MMCM 原始 16 位寄存器，`0x43C1_0000` 段的 `0x08..0x24` + `0x28` LOAD） | `spec/drp_semantics.json`、`dpm_regs.h` 记录的是上一版；当前映射见 `vivado/package_src_ip.tcl` 与 `build/analysis/golden/mmcm_encode.py` |

## 3. 测试与实测证据

下列输出都是本机实际运行结果，原始日志在 `spec/ps_selftest/`。

### 3.1 宿主机 C 用例（`spec/ps_selftest/host_tests.log`）

```text
$ ctest --test-dir ps/build/cmake --output-on-failure
    Start 1: test_calc            ... Passed
    Start 2: test_proto           ... Passed
    Start 3: test_sig             ... Passed
    Start 4: test_server_selftest ... Passed
100% tests passed out of 4      （exit=0）

$ ./ps/build/cmake/test_calc
== test_calc（dpm_calc.c 定点换算）
== 121 项检查，0 项失败        （exit=0）

$ ./ps/build/cmake/test_proto
== test_proto（帧层与 PAYLOAD）
== 120 项检查，0 项失败        （exit=0）

$ ./ps/build/cmake/test_sig
== test_sig（片内源求解与 DRP 字）
== 192 项检查，0 项失败        （exit=0）
```

> 上面的数字是 `spec/ps_selftest/host_tests.log`（2026-10-15 那一轮）的存档。随后的几轮扩充了金标准
> 向量与用例，**当前**同一组目标是 `test_calc` 325 / `test_proto` 128 / `test_sig` 579 项检查，
> 全部 0 失败（`test_proto` 新增的 8 项是命令层等待上限的量纲判据，见第 5 节与
> `docs/verify-report.md` §14）。

覆盖点：
- `test_calc`：28 条金标准的定点输出逐位一致、`gate/edge = 0` 不除零、32 位大数不溢出、
  相位归一化与周期换算；
- `test_proto`：CRC-16/MODBUS 标准向量 `"123456789" → 0x4B37`、组帧↔收帧逐字节往返、
  CRC 错/LEN=0/LEN=65/噪声后重新同步、全部请求 PAYLOAD 的长度校验与 IDENTIFY 字段布局；
- `test_sig`：7 个冻结频点的精确解与 VCO/MULT/D 约束、28 条金标准的求解结果、
  22.5° 吸附到 45° 网格、不可达频率的误差、DRP 字（`0x200 = (20<<8)|1`、分频字 = D 本体、
  相位字 = 毫度 90000、状态偏移 `0x004`、`DPM_DRP_PHASE_WRITABLE = 0`）。

### 3.2 pty 端到端自测（`spec/ps_selftest/pty_selftest.log`）

```text
$ python3 ps/tests/test_server_selftest.py
...
== 结果: 256/256 项通过 ==        （exit=0）
```

> 同样地，`256/256` 是 2026-10-15 存档的数字；**当前**是 **624/624**（新增的用例含
> 「长闸门 + 无信号时 `GET_RESULT` 的有界等待」，见第 5 节与 `docs/verify-report.md` §14）。

624 项覆盖：帧层负向（CRC 错/LEN 越界/噪声重同步/未知命令/方向位）、
全部命令码 `0x01..0x0E`、通知帧 `0xE1/0xE2`、错误路径（非法 mode/source/freq/相位/
PAYLOAD 长度 → `0xFE`；测量中 START、片内源未锁定、外部无信号 → `0xFF`）、
7 频点 × 4 相位点与金标准逐位对照、结果覆盖丢失（`lostCount`/`EVENT(1)`）、
单次/连续模式与流上报、外部源两条路径、`--unlock` 失锁实例、长闸门下的有界等待。

### 3.3 pty 会话记录（`spec/ps_selftest/pty_session.log`，节选）

```text
TX a5 01 01 c1 e0            (PING)          RX 81                → 空响应
TX a5 01 02 81 e1            (IDENTIFY)      RX 82 00 00 01 00 ...
      ver=0x00010000 caps=0x00000007 versionReg=0x44504D02 capsReg=0x00030104
      dsp=400000000 slow=50000000 flags=0x0007
TX a5 01 03 40 21            (GET_STATUS)    RX 83 ...
      status=0x00000000 sigStatus=0x0000000F gateTarget=2000 lostCount=0 seq=0
TX a5 09 0a 40 78 7d 01 90 5f 01 00 09 d0   (SET_SIG 25MHz/90°)
RX 8A 40 78 7d 01 90 5f 01 00 00 ca 9a 3b 28 00 00
      freq=25000000 vco=1000000000 div=40 phaseField=0（相位不可在线改 → 回 0）
TX a5 05 07 d0 07 00 00 3c 8f (SET_GATE 2000)   RX 87 d0 07 00 00 → gateTarget=2000
TX a5 01 04 01 e3            (START)         RX 84                → 空响应
TX a5 01 06 80 22            (GET_MEAS)      RX 86 ...
      seq=0 gate=2002 edge=1001 phase=4004 freq=25000000Hz phase=90000mdeg
      金标准（25MHz/90°）: gate=2002 edge=1001 phase=4004 freq=25000000 phase=90000 → 逐位一致
TX a5 01 0c 00 25            (CLEAR)         RX 8C                → 空响应
TX a5 02 08 02 ...           (SET_MODE 2)    RX FE                → ERR_PARAM
TX a5 09 0a 40 78 7d 01 00 ...(SET_SIG 3MHz) RX FE                → ERR_PARAM
TX a5 04 0d 01 14 00 89 ac   (STREAM on)     RX 8D 01 14 00       → enable=1 interval=20ms
RX E1 bc 00 00 00 d2 07 00 00 e9 03 00 00 a4 0f 00 00 40 78 7d 01 90 5f 01 00 00 00
      → STREAM_DATA seq=188 gate=2002 edge=1001 phase=4004 lostDelta=0
-- 服务端 SIGTERM 退出码: 0
```

### 3.4 Lead 的独立回归（`spec/ps_selftest/validate_sweep.log`）

用 `spec/validate_sweep.py`（独立参考实现 + 金标准）对我们的 host_main 做全量扫描：

```text
$ python3 spec/validate_sweep.py --server "./ps/build/host_main --device {device}" --repeat 1
  5.0000MHz 请求φ=  0.0°（相位固定） | gate= 2010 edge=  201 phaseCnt=    0 | φ=0.000° | PASS
  ...
  25.0000MHz 请求φ=135.0°（相位固定）| gate= 2002 edge= 1001 phaseCnt= 4004 | φ= 90.000° | PASS
通过 28 / 失败 0（共 28 点），串口统计 {'sent': 228, 'recv': 228, 'crc_err': 0,
'dropped': 0, 'timeouts': 0}，服务端退出码 0
```

## 4. 上板步骤（JTAG + UART）

前提：串口由 `vivado/create_bd.tcl` 直接给出（默认 `-uart uart1`，即 PS UART1 / MIO48-49），
重建 BD 与 bitstream 后即可用；详细步骤见 `docs/bringup.md` 第 0–1 节。

1. **生成 bitstream 并导出 XSA**（见 `vivado/build_bitstream.tcl`），启动 Vivado/Vitis 的
   硬件服务器（`hw_server`），或直接用 `xsct`：
   ```tcl
   connect
   targets -set -filter {name =~ "APU*"}
   rst -processor
   dow build/vivado/<...>/dpm_system_wrapper.bit   ;# 先下 PL
   fpga -f ...
   ```
2. **下载 PS 程序**（`ps/build/dpm_ps.elf`，链接在 OCM `0x00000000`）：
   ```tcl
   targets -set -filter {name =~ "ARM*#0"}
   dow ps/build/dpm_ps.elf
   con
   ```
   > 时钟/MIO/DDR 由 FSBL（`ps7_init`）负责：用 JTAG 直接 `dow` 时建议先跑 FSBL，
   > 或确认 `ps7_init.tcl` 已执行。程序本身只初始化 UART/全局定时器/GIC 与 PL 寄存器。
3. **接串口**：PC 侧 `/dev/ttyUSB*`，115200 8N1，用 `ps/tests/e2e_session_record.py`
   同款的帧格式手测，或直接跑上位机（`host/`）：
   ```bash
   python3 -m dpm_host.cli --port /dev/ttyUSB0        # 走 host/ 的 CLI
   python3 spec/validate_sweep.py --server "python3 -m dpm_host.cli --port /dev/ttyUSB0"
   ```
4. **复位**：`rst -processor` 后重新 `dow`/`con`；`CTRL.clear`（协议 `CLEAR`）只清结果与
   标志，不复位 PL 的测量状态机。

### 中断路径的状态

`DPM_USE_IRQ=1`（`build.sh` 默认）编译了 `dpm_irq.c` 的向量表 + GIC 使能 + 测量完成中断
（`DPM_IRQ_ID` 默认 61，UART0 = 59），中断里只置标志 + 清 `IRQ_STATUS`，主循环做读取。
**该路径未上板实测**：主循环始终同时轮询 `STATUS.busy`，所以即使中断号配错，
测量功能仍然可用（只是延迟由中断决定 vs 由轮询决定）。要退回纯轮询：
`bash ps/build.sh` 前修改 `DEFS_ARM` 里的 `-DDPM_USE_IRQ=0`。

## 5. 已知限制与实测结论（必须区分"已验证"与"未验证"）

| 项 | 状态 |
| --- | --- |
| 交叉编译 + 链接 + `nm -u` 无未定义符号 | **已验证**（本机，见 1 节输出） |
| 宿主机单元测试 1032 项（325+128+579）+ pty 端到端 624 项 | **已验证**（见 3.1/3.2；数字为 2026-10-16 当前值） |
| 与独立参考实现（`spec/validate_sweep.py`）79/79 一致 | **已验证**（见 3.4，本轮复跑 79 点全 PASS） |
| 片内源相位在线可改 | **未上板验证**：上一版 clk_wiz 方案的实测结论是「相位字没接到 MMCM，`clk_out2` 恒为静态 90.000°」（`spec/drp_semantics.json`、`docs/verify-drp.md`）；当前方案（裸 MMCM + 极简 DRP 控制器）改由 PS 直接写 MMCM 相位寄存器，但这条通路仍只有 DRP 从端模型与 `validate_bd_design` 的证据，上板前不要当成已生效 |
| 真实 PL 上的 DRP 重配置时序（等 `locked` 0→1、Safe Clock Startup） | **未上板验证**：旧方案的逻辑按当时的实测结论实现（`0x004` 状态位 + `0x25C` Load=0x07）；当前方案见 `vivado/package_src_ip.tcl` |
| UART 中断路径、GIC 中断号 61/59 | **未上板验证**（轮询路径已通过 pty 验证） |
| PS 时钟配置（`DPM_GTIMER_HZ` 默认 333.333MHz） | **未上板验证**：数值只影响超时判定的松紧，不影响协议与数值 |
| 外部引脚输入路径 | 宿主机侧用 `--ext-present` 模拟并对照金标准通过；真实引脚未测 |

## 6. 与冻结接口的关系

- `ps/include/dpm_regs.h`、`dpm_calc.h`、`dpm_proto.h` 由 Lead 冻结，本目录只读使用；
- DRP 映射与相位可写性以 `dpm_regs.h` + `spec/drp_semantics.json` 的记录为准——注意它们记录的是
  **上一版 clk_wiz_src 方案**（分频字低字节 = D 本体、相位是独立 32 位毫度字 `0x20C`/`0x218`、
  状态寄存器在 `0x004`）；当前方案写的是 MMCM 原始 16 位寄存器（`0x08..0x24` + `0x28` LOAD），
  映射见 `vivado/package_src_ip.tcl` 与 `build/analysis/golden/mmcm_encode.py`；
- 频率字段 `freqMilliHz` 的**单位是 Hz**（历史命名），公式用不溢出的等价形式
  （见 `dpm_calc.h` 与 `docs/ps-protocol.md` §5）；
- 通知帧是 `0xE1`（STREAM_DATA）/ `0xE2`（EVENT），响应是 `请求|0x80`，错误 `0xFE/0xFF`。
