# ost_renderer: OpenStacks' renderer plugin (gfxstream host renderer behind a C API):
# ost_renderer.dll on Windows (renderer\build_gfxstream.cmd, VS 2022 ClangCL),
# libost_renderer.so on Linux (renderer/build_gfxstream.sh). docs/renderer_plan.md.
cmake_minimum_required(VERSION 3.20)
project(ost_renderer C CXX)

set(GFXSTREAM_SRC "${CMAKE_CURRENT_SOURCE_DIR}/../deps/gfxstream" CACHE PATH "gfxstream checkout")
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Desktop GL (OST_GL=wgl) needs gfxstream's ANGLE shader parser: GLSL ES -> desktop GLSL via
# libshadertranslator.dll (Android emulator), loaded at run time. Header from AOSP's ANGLE
# (platform/external/angle, emu-master-dev, src/libShaderTranslator).
set(USE_ANGLE_SHADER_PARSER ON CACHE BOOL "" FORCE)
include_directories("${CMAKE_CURRENT_SOURCE_DIR}/third_party/shadertranslator")

# gfxstream itself (its own top-level CMakeLists handles being a subdirectory)
add_subdirectory("${GFXSTREAM_SRC}" gfxstream EXCLUDE_FROM_ALL)

add_library(ost_renderer SHARED
    ost_renderer.cpp
    "${GFXSTREAM_SRC}/host/render_api.cpp")   # initLibrary(), outside the static lib
target_compile_definitions(ost_renderer PRIVATE
    OST_RENDERER_BUILD=1 GFXSTREAM_ENABLE_HOST_GLES=1 EMUGL_BUILD)
# common/imgd.h: image detection core, in gfxstream's GLcommon (gfxstream-patches/0009)
target_include_directories(ost_renderer PRIVATE
    "${GFXSTREAM_SRC}/host/gl/glestranslator/common/include")
if(WIN32)
    target_compile_definitions(ost_renderer PRIVATE
        UNICODE _UNICODE NOMINMAX VK_USE_PLATFORM_WIN32_KHR)
else()
    # QEMU dlopens it: only the ost_* C API is exported
    set_target_properties(ost_renderer PROPERTIES CXX_VISIBILITY_PRESET hidden
                          VISIBILITY_INLINES_HIDDEN ON)
    target_link_options(ost_renderer PRIVATE -Wl,--exclude-libs,ALL -Wl,-Bsymbolic)
endif()
target_compile_options(ost_renderer PRIVATE -Wno-unused-parameter -Wno-return-type-c-linkage)
target_link_libraries(ost_renderer PRIVATE
    gfxstream_common_build_info
    gfxstream_common_utils
    gfxstream_features
    gfxstream_host_common
    gfxstream_host_tracing
    gfxstream_backend_static)

# PDB (function names; gfxstream itself has no /Zi): resolves the crash offsets gr_render
# logs, e.g. llvm-symbolizer --obj=ost_renderer.dll 0x<offset> (VS Clang's llvm tools)
if(MSVC)
    target_compile_options(ost_renderer PRIVATE /Zi)
    target_link_options(ost_renderer PRIVATE /DEBUG:FULL)
endif()

# Smoke test: one channel, rcGetRendererVersion
add_executable(ost_renderer_smoke smoke.c)
target_link_libraries(ost_renderer_smoke PRIVATE ost_renderer)

# ost_imgd_test: the image detection core (gfxstream patch 0009), no GL needed
add_executable(ost_imgd_test imgd_test.cpp
    "${GFXSTREAM_SRC}/host/gl/glestranslator/common/imgd.cpp")
target_include_directories(ost_imgd_test PRIVATE
    "${GFXSTREAM_SRC}/host/gl/glestranslator/common/include")

# st_probe: checks the vendored ShaderTranslator.h against libshadertranslator.dll/.so
add_executable(st_probe st_probe.cpp)
if(NOT WIN32)
    target_link_libraries(st_probe PRIVATE ${CMAKE_DL_LIBS})
    set_target_properties(ost_renderer_smoke st_probe PROPERTIES BUILD_RPATH "$ORIGIN")
endif()
