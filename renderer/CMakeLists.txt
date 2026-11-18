# ost_renderer.dll: OpenStacks' renderer plugin (gfxstream host renderer behind a C API).
# Configured by renderer\build_gfxstream.cmd (VS 2022, ClangCL). docs/renderer_plan.md.
cmake_minimum_required(VERSION 3.20)
project(ost_renderer C CXX)

set(GFXSTREAM_SRC "${CMAKE_CURRENT_SOURCE_DIR}/../deps/gfxstream" CACHE PATH "gfxstream checkout")
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# gfxstream itself (its own top-level CMakeLists handles being a subdirectory)
add_subdirectory("${GFXSTREAM_SRC}" gfxstream EXCLUDE_FROM_ALL)

add_library(ost_renderer SHARED
    ost_renderer.cpp
    "${GFXSTREAM_SRC}/host/render_api.cpp")   # initLibrary(), outside the static lib
target_compile_definitions(ost_renderer PRIVATE
    OST_RENDERER_BUILD=1 GFXSTREAM_ENABLE_HOST_GLES=1
    UNICODE _UNICODE NOMINMAX EMUGL_BUILD VK_USE_PLATFORM_WIN32_KHR)
target_compile_options(ost_renderer PRIVATE -Wno-unused-parameter -Wno-return-type-c-linkage)
target_link_libraries(ost_renderer PRIVATE
    gfxstream_common_build_info
    gfxstream_common_utils
    gfxstream_features
    gfxstream_host_common
    gfxstream_host_tracing
    gfxstream_backend_static)

# Smoke test: one channel, rcGetRendererVersion
add_executable(ost_renderer_smoke smoke.c)
target_link_libraries(ost_renderer_smoke PRIVATE ost_renderer)
