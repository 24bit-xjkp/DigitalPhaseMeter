---@diagnostic disable-next-line
package("clean_std_heads", function ()
    set_homepage("https://github.com/YexuanXiao/convert-cpp-std-headers-to-std-module")
    set_description("A library for helping to convert #include standard library headers to import std;")
    set_urls("https://github.com/YexuanXiao/convert-cpp-std-headers-to-std-module.git")
    add_versions("1.1", "00ae6cc3184d9e55cae67fc9ff305a235b968e6e")
    add_versions("1.0", "73b5212319bc9dab6a0bdc12afed5036b153bb7e")
    set_kind("library", { headeronly = true })

    on_install(function (package)
        os.cp("clear_all_cpp_std_headers.h", package:installdir("include"))
    end)

    on_test(function (package)
        assert(package:has_cxxincludes("clear_all_cpp_std_headers.h"))
    end)
end)

---@diagnostic disable-next-line
package("doctest_module", function ()
    set_homepage("https://github.com/24bit-xjkp/doctest_module")
    set_description("Use doctest framework as C++20 module.")
    add_urls(
        "https://github.com/24bit-xjkp/doctest_module/archive/refs/tags/$(version).tar.gz",
        "https://github.com/24bit-xjkp/doctest_module.git"
    )
    add_versions("v1.2.0", "2f2005bf5f28acc853a75a4bc81860543bf5da6ef78ab52f89dc89fc4a3e867f")
    add_versions("v1.1.0", "427b0977e30844956e3fd7bfc9e159e12df5df5c3e0dc5922a104a90c1e8f599")
    add_versions("v1.0.0", "4a864de74790fdc70f876a20c5bad7589b4c10058f2a6e0a7041c0c351702ee6")
    add_deps("clean_std_heads >=1.1", "doctest")
    add_configs("main", { description = "启用main函数支持", default = true, type = "boolean" })
    add_configs("std_harden", { description = "启用C++标准库硬化支持", default = true, type = "boolean" })

    on_component("core", function (_, component)
        component:add("links", "doctest")
    end)

    on_component("main", function (_, component)
        component:add("deps", "core")
        component:add("links", "doctest_main")
    end)

    on_load(function (package)
        package:add("components", "core")
        if package:config("main") then
            package:add("components", "main")
        end
    end)

    on_install(function (package)
        local config = { with_main = package:config("main"), use_std_harden = package:config("std_harden") }
        import("package.tools.xmake").install(package, config)
    end)

    on_test(function (package)
        assert(package:has_cxxincludes("doctest_macros.hpp"))
    end)
end)

std_harden_defines = {
    "_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_" .. (is_mode("debug") and "DEBUG" or "FAST"), "_GLIBCXX_ASSERTIONS"
}

---@diagnostic disable-next-line
package("verilator_utils", function ()
    set_homepage("https://github.com/24bit-xjkp/verilator_utils")
    set_description("基于Verilator和Doctest的RTL测试框架")
    add_urls("https://github.com/24bit-xjkp/verilator_utils.git")
    add_deps("doctest_module[main=n]", "verilator >=5.050", "cpptrace")
    add_configs("main", { description = "启用main函数支持", default = true, type = "boolean" })
    add_configs("fst", { description = "启用FST波形记录支持", default = true, type = "boolean" })
    add_configs("std_harden", { description = "启用C++标准库硬化支持", default = true, type = "boolean" })

    on_component("core", function (_, component)
        component:add("links", "verilator_utils")
    end)

    on_component("main", function (_, component)
        component:add("deps", "core")
        component:add("links", "verilator_utils_main")
    end)

    on_load(function (package)
        package:add("components", "core")
        if package:config("main") then
            package:add("components", "main")
        end
        if package:config("fst") then
            package:add("deps", "zlib", "lz4")
        end
    end)

    on_install(function (package)
        local config = {
            with_main = package:config("main"),
            trace_support_fst = package:config("fst"),
            enable_test = false,
            use_lto = package:config("lto"),
            use_std_harden = package:config("std_harden")
        }
        import("package.tools.xmake").install(package, config)
    end)
end)
