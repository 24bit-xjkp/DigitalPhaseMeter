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
local extra_verilator_options = {
    ["MmcmDrpController"] = { "-Wno-UNUSEDSIGNAL" }
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
