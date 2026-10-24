local verilator_options = {
    "-Wall", "--trace-fst", { "--x-assign", "unique" }, { "--x-initial", "unique" }, "--coverage"
}
local test_args = { "+verilator+rand+reset+2", "-fc" }
local chisel_dir = path.join(os.scriptdir(), "..", "build", "chisel")

-- 逐目标的 Verilator 豁免：
-- MmcmDrpController 是一个 AXI4-Lite 从口 + MMCM DRP 控制器。AXI4-Lite 接口固定
-- 带有 awprot / arprot / wstrb 与 32 位写数据，但 MMCM 的配置寄存器只有 16 位、
-- 且本设计不做保护级检查，因此这些位天然不被消费。这些是**接口固有的未使用位**，
-- 不是 RTL 缺陷，所以对该目标关掉 UNUSEDSIGNAL（其余 -Wall 检查全部保留）。
--
-- Controller 的高速域复位由模块内部的 ResetSynchronizer 从隐式 reset（高有效主复位）
-- 异步置位得到：同一根 reset 网线既做本模块寄存器的同步复位，又做同步链触发器的异步复位，
-- Verilator 的 -Wall 因此报 SYNCASYNCNET。这是复位同步器的固有结构——异步端按定义与本地
-- 时钟无关（链的 ASYNC_REG 打包负责把释放沿的亚稳态风险压到可接受范围），不是缺陷，所以
-- 只对该目标关掉这一条检查（其余 -Wall 检查全部保留）。
--
-- DigitalPhaseMeterTop 是上面两种结构的组合（AXI4-Lite 从口 + ResetSynchronizer），因此同样
-- 关掉 UNUSEDSIGNAL 与 SYNCASYNCNET；另外它的中断端口名是 IP 接口规定的 `interrupt`，命中
-- Verilator 的「C++ 常用词」检查（SYMRSVDWORD，生成代码里改名为 `__SYM__interrupt`），
-- 端口名由 IP-XACT/BD 接口约定决定、不能为测试改 RTL，所以也关掉这一条。
local extra_verilator_options = {
    ["MmcmDrpController"] = { "-Wno-UNUSEDSIGNAL" },
    ["Controller"] = { "-Wno-SYNCASYNCNET" },
    ["DigitalPhaseMeterTop"] = { "-Wno-UNUSEDSIGNAL", "-Wno-SYNCASYNCNET", "-Wno-SYMRSVDWORD" }
}

for _, file in ipairs(os.files("*.cpp")) do
    local name = path.basename(file)
    target(name .. "Verilator", function ()
        add_toolchains("verilator")
        add_rules("verilator.shared")
        set_warnings("none")
        set_policy("build.fence", true)
        if is_mode("debug") then
            set_optimize("fast")
        end
        local flags = table.join(verilator_options, extra_verilator_options[name] or {})
        add_values("verilator.flags", flags)

        on_load(function (target)
            -- 每个模块只生成**一份**产物（build/chisel/<Name>，见 Utils.scala 的 emitSystemVerilog）：
            -- 复位同步改成纯 RTL 的 ResetSynchronizer 后，仿真与综合（xsim / Vivado）共用同一份 RTL，
            -- 不再有 sim / syn 两种产物，Verilator 侧也就不会再遇到 XPM 原语缺失的问题。
            local sv_dir = path.join(chisel_dir, name)
            for file in io.lines(path.join(sv_dir, "filelist.f")) do
                target:add("files", path.join(sv_dir, file), {})
            end
            target:set("targetdir", path.join(target:targetdir(), name))
        end)
    end)

    target(name, function ()
        set_policy("build.c++.modules", true)
        add_packages("verilator_utils", { components = "main" })
        add_packages("doctest_module", { components = "core" })
        add_deps(name .. "Verilator")
        add_files(name .. ".cpp")

        on_load(function (target)
            target:set("targetdir", path.join(target:targetdir(), name))
        end)

        set_runargs(test_args)
        add_tests("rtl", { runargs = test_args })
    end)
end
