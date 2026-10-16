#!/usr/bin/bash
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
vivado -mode batch -source "$script_dir/package_ip.tcl"
vivado -mode batch -source "$script_dir/package_src_ip.tcl"
vivado -mode batch -source "$script_dir/create_bd.tcl"
vivado -mode batch -source "$script_dir/build_bitstream.tcl"
