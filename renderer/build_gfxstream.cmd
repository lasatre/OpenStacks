@echo off
rem ==========================================================================
rem  ost_renderer.dll: gfxstream host renderer for OpenStacks (Windows, VS ClangCL).
rem   1. clone gfxstream at the pinned commit into deps\gfxstream (once)
rem   2. apply renderer\gfxstream-patches\*.patch (once per patch)
rem   3. configure deps\ost-renderer-build (renderer\CMakeLists.txt + gfxstream, VS 2022,
rem      ClangCL) and build ost_renderer.dll + ost_renderer_smoke.exe
rem   4. fetch libshadertranslator.dll (renderer\fetch_shadertranslator.py: pinned,
rem      hash-checked) next to it, and install both into build\ when the QEMU build
rem      is there (assemble.py ships every DLL in build\)
rem  Needs: Visual Studio 2022 (Build Tools) with the C++ and "C++ Clang" components,
rem         CMake 3.20+, git, python. Output: deps\ost-renderer-build\Release\ost_renderer.dll
rem ==========================================================================
setlocal
set "RDIR=%~dp0"
for %%i in ("%RDIR%..") do set "ROOT=%%~fi\"
set "GFX_COMMIT=cf47bd2660e7217f5729c91664d36d81c1b3282a"
set "SRC=%ROOT%deps\gfxstream"
set "BLD=%ROOT%deps\ost-renderer-build"
set "CMAKE=cmake"
where cmake >nul 2>&1 || set "CMAKE=%ProgramFiles%\CMake\bin\cmake.exe"

if not exist "%SRC%\CMakeLists.txt" (
  echo [gfxstream] cloning %GFX_COMMIT%
  git clone -q https://github.com/google/gfxstream "%SRC%" || goto :fail
)
git -C "%SRC%" cat-file -e %GFX_COMMIT% 2>nul || git -C "%SRC%" fetch -q --depth 1 origin %GFX_COMMIT% || goto :fail
git -C "%SRC%" diff --quiet HEAD || echo [gfxstream] note: source tree has local changes
for %%p in ("%RDIR%gfxstream-patches\*.patch") do (
  git -C "%SRC%" apply --check "%%~fp" >nul 2>&1 && (
    echo [gfxstream] applying %%~nxp
    git -C "%SRC%" apply "%%~fp" || goto :fail
  )
)

rem _WINSOCKAPI_: keep windows.h from pulling in winsock.h (clashes with WinSock2.h)
if not exist "%BLD%\CMakeCache.txt" (
  "%CMAKE%" -S "%RDIR%." -B "%BLD%" -G "Visual Studio 17 2022" -A x64 -T ClangCL ^
    "-DCMAKE_C_FLAGS=/DWIN32 /D_WINDOWS /D_WINSOCKAPI_" ^
    "-DCMAKE_CXX_FLAGS=/DWIN32 /D_WINDOWS /EHsc /GR /D_WINSOCKAPI_" || goto :fail
)
"%CMAKE%" --build "%BLD%" --config Release --target ost_renderer ost_renderer_smoke --parallel || goto :fail
python "%RDIR%fetch_shadertranslator.py" --dest "%BLD%\Release" || goto :fail
if exist "%ROOT%build\qemu-system-x86_64.exe" (
  copy /y "%BLD%\Release\ost_renderer.dll" "%ROOT%build\" >nul || goto :fail
  copy /y "%BLD%\Release\libshadertranslator.dll" "%ROOT%build\" >nul || goto :fail
  echo [gfxstream] installed ost_renderer.dll + libshadertranslator.dll into %ROOT%build
)
echo.
echo Build OK: %BLD%\Release\ost_renderer.dll (smoke test: ost_renderer_smoke.exe)
exit /b 0

:fail
echo.
echo ost_renderer build FAILED - see the messages above.
exit /b 1
