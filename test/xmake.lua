local verilator_options = {
    "--trace-fst", { "--x-assign", "unique" }, { "--x-initial", "unique" }, "-Wno-SYMRSVDWORD",
    "-Dtargets$Platform$FPGA",  "-DENABLE_INITIAL_REG_"
}
local test_args = { "+verilator+rand+reset+2", "-fc" }
-- 顶层产物目录同时含平台选择层与 ImplS/ImplA 两个实现分支，Verilator 无法自动选顶层；
-- 需要显式 --top 的目录在这里登记（其它模块目录只有一个顶层模块）。
local top_module_map = { DigitalPhaseMeterTop = "DigitalPhaseMeterTop" }

target("mill.compile", function ()
    set_kind("phony")
    set_default(false)

    on_config(function (_)
        -- 保证串行执行一次编译
        local mill = path.join(os.projectdir(), "mill")
        os.vrunv(mill, { "compile" })
    end)
end)

for _, file in ipairs(os.files("*.cpp")) do
    local name = path.basename(file)

    target(name .. "Verilator", function ()
        add_deps("mill.compile")
        add_rules("mill.verilator.shared")
        add_toolchains("@verilator")
        set_warnings("none")
        set_policy("build.fence", true)
        if is_mode("debug") then
            set_optimize("fast")
        end
        add_values("mill.target", name)
        -- chisel3.ltl.AssertProperty 的 checker/bind 在 build/chisel/<Name>/verification 下；
        -- bind 文件用 `include "layers-*.sv"`，需要把 verification 目录加入 include 搜索路径
        local target_options = table.clone(verilator_options)
        local verification_dir = path.join(get_config("builddir"), "chisel", name, "verification")
        table.insert(target_options, "-I" .. verification_dir)
        table.insert(target_options, "-I" .. path.join(verification_dir, "assert"))
        local top_module = top_module_map[name]
        if top_module then
            table.join2(target_options, { "--top", top_module })
        end
        add_values("verilator.flags", target_options)

        on_load(function (target)
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
