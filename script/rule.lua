-- 在 Verilator 编译之前自动执行 Mill 生成 SystemVerilog 的共享规则。
--
-- 用法（见 test/xmake.lua）：
--   add_rules("mill.verilator.shared")
--   add_values("mill.target", name)                 -- Chisel 模块名，也是 build/chisel/<Name>/ 的目录名
--   add_values("verilator.flags", verilator_options)
--
-- 规则自己克隆一份 verilator.shared 并加上顺序依赖，保证本规则的 before_preparecmd_files 先执行：
-- 在仓库根执行 `./mill emit [--force] <Name>`，再把 build/chisel/<Name>/filelist.f 列出的 SV 交给
-- verilator.buildcmd_vfiles 编译。`emit` 自身按内容缓存，未变化时直接跳过；只有目标被重建
-- （如 `xmake -r`）或 build.mill 比 filelist.f 新时才加 `--force`（依赖信息记在 filelist.f.d）。
-- 目标把 build.mill 登记为源文件，改构建定义会触发本目标重建。
rule("mill.verilator.shared", function ()
    add_deps("verilator.shared")
    set_extensions(".mill")

    on_load(function (target)
        local rule = target:rule("verilator.shared"):clone()
        rule:add("deps", "mill.verilator.shared", { order = true })
        target:add("rules", rule)
        target:add("files", path.join(os.projectdir(), "build.mill"))
    end)

    before_preparecmd_files(function (target, batchcmds, sourcebatch, opt)
        import("utils.progress")
        import("core.project.depend")
        import("rules.verilator.verilator", { rootdir = os.programdir() })

        local mill = path.join(os.projectdir(), "mill")
        local mill_target = target:values("mill.target")
        local sourcefiles = sourcebatch.sourcefiles
        local sv_dir = path.join(os.projectdir(), get_config("builddir"), "chisel", mill_target)
        local makefile = path.join(sv_dir, "filelist.f")
        local dependfile = makefile .. ".d"
        local dependinfo = depend.load(dependfile) or {}
        local is_changed_opt = { lastmtime = os.mtime(makefile), files = sourcefiles }
        progress.set_target(opt.progress, target)
        progress.show(opt.progress, "${color.build.object}mill.emit %s", mill_target)
        local args = { "emit" }
        if target:is_rebuilt() or depend.is_changed(dependinfo, is_changed_opt) then
            table.append(args, "--force")
            depend.save({ files = sourcefiles }, dependfile)
        end
        table.append(args, mill_target)
        os.vrunv(mill, args)
        for sv_file in io.lines(makefile) do
            target:add("files", path.join(sv_dir, sv_file))
        end
        verilator.buildcmd_vfiles(target, batchcmds, target:sourcebatches()["verilator.shared"], opt)
    end)
end)
