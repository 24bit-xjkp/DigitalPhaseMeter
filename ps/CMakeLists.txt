cmake_minimum_required(VERSION 3.16)
# ps/CMakeLists.txt —— 宿主机（x86-64）验证台与自测的构建定义
#
#   cmake -S ps -B ps/build/cmake && cmake --build ps/build/cmake && ctest --test-dir ps/build/cmake
#
# 覆盖三部分：
#   1. 共享核心（dpm_proto.c / dpm_calc.c / dpm_sig.c / dpm_cmd.c）在宿主机上的单元测试；
#   2. 仿真后端 + host_main（pty/stdio 服务端，供 spec/validate_sweep.py 使用）；
#   3. pty 端到端自测（Python 驱动 host_main，读 spec/golden/vectors.json）。
#
# PS 侧裸机程序不在这里构建（用 ps/build.sh 的交叉编译路径）。

project(dpm_ps_host C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

# 宿主机构建允许 libc；共享核心仍需保持可移植（不 include libc 头）
set(DPM_WARN -Wall -Wextra -Werror)

set(DPM_PS_DIR ${CMAKE_CURRENT_SOURCE_DIR})
set(DPM_REPO_DIR ${CMAKE_CURRENT_SOURCE_DIR}/..)

set(DPM_SHARED
    ${DPM_PS_DIR}/src/dpm_proto.c
    ${DPM_PS_DIR}/src/dpm_calc.c
    ${DPM_PS_DIR}/src/dpm_sig.c
    ${DPM_PS_DIR}/src/dpm_cmd.c)

# --------------------------------------------------------------- host_main

add_executable(host_main
    ${DPM_SHARED}
    ${DPM_PS_DIR}/sim/fpga_model.c
    ${DPM_PS_DIR}/sim/host_main.c)
target_include_directories(host_main PRIVATE ${DPM_PS_DIR}/include ${DPM_PS_DIR}/sim)
target_compile_definitions(host_main PRIVATE DPM_HOST_SELFTEST=1)
target_compile_options(host_main PRIVATE ${DPM_WARN})

# ------------------------------------------------------------- 单元测试

add_executable(test_calc ${DPM_PS_DIR}/tests/test_calc.c ${DPM_SHARED})
target_include_directories(test_calc PRIVATE ${DPM_PS_DIR}/include ${DPM_PS_DIR}/tests)
target_compile_options(test_calc PRIVATE ${DPM_WARN})

add_executable(test_proto ${DPM_PS_DIR}/tests/test_proto.c ${DPM_SHARED})
target_include_directories(test_proto PRIVATE ${DPM_PS_DIR}/include)
target_compile_options(test_proto PRIVATE ${DPM_WARN})

add_executable(test_sig ${DPM_PS_DIR}/tests/test_sig.c ${DPM_SHARED})
target_include_directories(test_sig PRIVATE ${DPM_PS_DIR}/include ${DPM_PS_DIR}/tests)
target_compile_options(test_sig PRIVATE ${DPM_WARN})

enable_testing()
add_test(NAME test_calc COMMAND test_calc)
add_test(NAME test_proto COMMAND test_proto)
add_test(NAME test_sig COMMAND test_sig)

# --------------------------------------------- pty 端到端自测（依赖 host_main）

find_program(PYTHON3 python3)
if(PYTHON3)
    add_test(NAME test_server_selftest
             COMMAND ${PYTHON3} ${DPM_PS_DIR}/tests/test_server_selftest.py
                     --host-main $<TARGET_FILE:host_main>)
    set_tests_properties(test_server_selftest PROPERTIES TIMEOUT 600)
endif()
