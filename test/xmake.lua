local verilator_options = {
    "--trace-fst", { "--x-assign", "unique" }, { "--x-initial", "unique" }, "-Wno-SYMRSVDWORD", "-DENABLE_INITIAL_REG_"
}
local test_args = { "+verilator+rand+reset+2", "-fc" }

target("mill.compile", function ()
    set_kind("phony")
    set_default(false)

    on_config(function (_)
        -- 保证串行执行一次编译
        local mill = path.join(os.projectdir(), "mill")
        os.vrunv(mill, { "compile" })
    end)
end)

local common_file_list = {}
for _, file in ipairs(os.files("*.cppm")) do
    local name = path.basename(file)
    table.append(common_file_list, name)
    target(name .. "Common", function ()
        set_kind("object")
        set_policy("build.c++.modules", true)
        add_packages("verilator_utils", { components = "main" })
        add_packages("doctest_module", { components = "core" })
        add_files(file, { public = true })
        -- cppm 会 import verilator_utils.full，而它的模块接口包含 <verilated.h>；
        -- verilator 的 include 目录由 verilator.shared 规则以 public 方式挂到 <Name>Verilator
        -- 目标上，因此公共模块目标要依赖同名 Verilator 目标才能拿到这些头文件。
        if os.isfile(path.join(os.scriptdir(), name .. ".cpp")) then
            add_deps(name .. "Verilator")
        end
    end)
end

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
        add_files(file)
        for _, common_file_name in ipairs(common_file_list) do
            if name:startswith(common_file_name) then
                add_deps(common_file_name .. "Common")
            end
        end

        on_load(function (target)
            target:set("targetdir", path.join(target:targetdir(), name))
        end)

        set_runargs(test_args)
        add_tests("rtl", { runargs = test_args })
    end)
end
