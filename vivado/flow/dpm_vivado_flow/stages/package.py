"""IP 打包阶段：内容未变则跳过，变化则重新打包。"""

from __future__ import annotations

from pathlib import Path

from ..hashing import compose, read_filelist, sha256_file, sha256_files
from ..paths import CUSTOM_IPS, PART, FlowError, RepoPaths
from ..runner import VivadoRunner
from ..stamps import SCHEMA, load_json, save_json
from ..tclgen import write_source_wrapper


def package_inputs(paths: RepoPaths, ip: str, vivado: str) -> str:
    spec = CUSTOM_IPS[ip]
    rtl_dir = paths.chisel_dir / spec["rtl"]
    files = read_filelist(rtl_dir)
    filelist = rtl_dir / "filelist.f"
    script = paths.vivado_dir / spec["script"]
    content = sha256_files([script, filelist, *files])
    return compose("package", ip, content, SCHEMA, PART, vivado)


def package_stage(
    paths: RepoPaths,
    runner: VivadoRunner,
    vivado: str,
    *,
    force: bool,
    dry_run: bool,
) -> dict[str, dict[str, object]]:
    info: dict[str, dict[str, object]] = {}
    for ip, spec in CUSTOM_IPS.items():
        inputs = package_inputs(paths, ip, vivado)
        stamp_path = paths.package_stamp_file(ip)
        stamp = load_json(stamp_path)
        component = paths.ip_dir / spec["ip_dir"] / "component.xml"
        expected = {
            "schema": SCHEMA,
            "ip": ip,
            "inputs": inputs,
            "vivado": vivado,
            "part": PART,
        }
        reusable = (
            not force and stamp is not None and all(stamp.get(key) == value for key, value in expected.items()) and component.is_file()
        )
        if reusable:
            print(f"IP_PACKAGE_REUSED: {ip} inputs={inputs[:12]}")
            info[ip] = {"inputs": inputs, "reused": True}
            continue
        if dry_run:
            print(f"IP_PACKAGE_RAN: {ip} (dry-run) inputs={inputs[:12]}")
            info[ip] = {"inputs": inputs, "reused": False}
            continue
        wrapper = write_source_wrapper(
            paths.wrapper_file(f"package_{ip}"),
            paths.vivado_dir / spec["script"],
            argv=[],
        )
        result = runner.run_script(wrapper, paths.log_file(f"package_{ip}"))
        runner.check_script(result, f"package_{ip}")
        if not component.is_file():
            raise FlowError(f"打包后仍找不到 {component}")
        stamp = dict(expected)
        stamp["component"] = sha256_file(component)
        save_json(stamp_path, stamp)
        print(f"IP_PACKAGE_REBUILT: {ip} inputs={inputs[:12]}")
        info[ip] = {"inputs": inputs, "reused": False}
    return info
