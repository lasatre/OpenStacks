@echo off
rem ==========================================================================
rem  OpenStacks QEMU build (Windows, MSYS2 ucrt64).
rem   1. fetch QEMU 11.1.0 into deps\ (once; reuses a local tarball if present)
rem   2. copy qemu\hw\misc\* into the tree + hook Kconfig/meson (apply_to_tree.py)
rem   3. configure (once) + ninja into build\
rem   4. copy pc-bios and the MSYS2 runtime DLLs next to the exe (self-contained)
rem  qemu\hw\misc is the ONLY canonical copy of the device sources.
rem  Kills running QEMU first (the linker cannot replace a running exe).
rem  Needs: C:\msys64 with mingw-w64-ucrt-x86_64-{gcc,ninja,meson,glib2,pixman,
rem         libslirp,pkgconf} + python + curl + tar. Override with QEMU_TREE=...
rem ==========================================================================
setlocal
set "QDIR=%~dp0"
for %%i in ("%QDIR%..") do set "ROOT=%%~fi\"
set "QVER=11.1.0"
if not defined QEMU_TREE set "QEMU_TREE=%ROOT%deps\qemu-%QVER%"
set "MSYSTEM=UCRT64"
set "PATH=C:\msys64\ucrt64\bin;C:\msys64\usr\bin;%PATH%"
set "BASH=C:\msys64\usr\bin\bash.exe"

taskkill /f /im qemu-system-x86_64.exe >nul 2>&1

if not exist "%ROOT%deps" mkdir "%ROOT%deps"
for /f "delims=" %%i in ('%BASH% -lc "cygpath -u '%ROOT%deps'"') do set "UDEPS=%%i"
if exist "%QEMU_TREE%\meson.build" goto :have_tree
echo [build] fetching QEMU %QVER% into %ROOT%deps
if exist "%ROOT%deps\qemu-%QVER%.tar.xz" goto :extract
if exist "%ROOT%..\qemu_bst\qemu-%QVER%.tar.xz" (
  copy /y "%ROOT%..\qemu_bst\qemu-%QVER%.tar.xz" "%ROOT%deps\" >nul
) else (
  curl -L -o "%ROOT%deps\qemu-%QVER%.tar.xz" https://download.qemu.org/qemu-%QVER%.tar.xz
)
if errorlevel 1 goto :fail
:extract
%BASH% -lc "cd '%UDEPS%' && tar xJf qemu-%QVER%.tar.xz 2>/dev/null; test -f qemu-%QVER%/meson.build"
if errorlevel 1 goto :fail
:have_tree

python "%QDIR%apply_to_tree.py" "%QEMU_TREE%"
if errorlevel 1 goto :fail

for /f "delims=" %%i in ('%BASH% -lc "cygpath -u '%ROOT%'"') do set "UROOT=%%i"
for /f "delims=" %%i in ('%BASH% -lc "cygpath -u '%QEMU_TREE%'"') do set "UTREE=%%i"
if not exist "%ROOT%build" mkdir "%ROOT%build"

rem reconfigure when build\ was configured against another source tree
if exist "%ROOT%build\build.ninja" (
  findstr /c:"%UTREE%" "%ROOT%build\build.ninja" >nul || (
    echo [build] build\ belongs to another QEMU tree: reconfiguring
    rmdir /s /q "%ROOT%build"
    mkdir "%ROOT%build"
  )
)

if not exist "%ROOT%build\build.ninja" (
  %BASH% -lc "cd '%UROOT%build' && '%UTREE%/configure' --target-list=x86_64-softmmu --enable-slirp --enable-whpx --disable-werror --disable-docs --disable-tools --disable-sdl --disable-gtk"
  if errorlevel 1 goto :fail
)

%BASH% -lc "cd '%UROOT%build' && ninja qemu-system-x86_64.exe -j %NUMBER_OF_PROCESSORS%"
if errorlevel 1 goto :fail

xcopy /E /I /Y /Q "%QEMU_TREE%\pc-bios\*" "%ROOT%build\pc-bios\" >nul
rem runtime DLLs from ucrt64 (so the exe runs without MSYS2 on PATH)
%BASH% -lc "cd '%UROOT%build' && ldd qemu-system-x86_64.exe | awk '/ucrt64/ {print $3}' | sort -u | xargs -r cp -u -t ."

echo.
echo Build OK: %ROOT%build\qemu-system-x86_64.exe
exit /b 0

:fail
echo.
echo Build FAILED - see the messages above.
exit /b 1
