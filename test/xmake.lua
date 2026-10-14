local verilator_options = {
    "-Wall", "--trace-fst", { "--x-assign", "unique" }, { "--x-initial", "unique" }, "--coverage"
}
local test_args = { "+verilator+rand+reset+2", "-fc" }
local chisel_dir = path.join(os.scriptdir(), "..", "build", "chisel")
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
        add_values("verilator.flags", verilator_options)

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
