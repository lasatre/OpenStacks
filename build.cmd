@echo off
rem ==========================================================================
rem  OpenStacks dev build: copies dev\src\hw\misc into the shared QEMU 11.1.0
rem  tree (..\qemu_bst\qemu-11.1.0) and builds into dev\build.
rem  dev\src is the ONLY canonical copy of the device sources.
rem  Kill QEMU first (link.exe Permission denied otherwise).
rem ==========================================================================
setlocal
set "ROOT=%~dp0"
set "TREE=%ROOT%..\qemu_bst\qemu-11.1.0"
set "MSYSTEM=UCRT64"
set "PATH=C:\msys64\ucrt64\bin;C:\msys64\usr\bin;%PATH%"

taskkill /f /im qemu-system-x86_64.exe >nul 2>&1

python "%ROOT%src\apply_to_tree.py" "%TREE%"
if errorlevel 1 goto :fail

for /f "delims=" %%i in ('C:\msys64\usr\bin\bash.exe -lc "cygpath -u '%ROOT%'"') do set "UROOT=%%i"
if not exist "%ROOT%build" mkdir "%ROOT%build"

if not exist "%ROOT%build\build.ninja" (
  C:\msys64\usr\bin\bash.exe -lc "cd '%UROOT%build' && ../../qemu_bst/qemu-11.1.0/configure --target-list=x86_64-softmmu --enable-slirp --enable-whpx --disable-werror --disable-docs --disable-tools --disable-vnc --disable-sdl --disable-gtk"
  if errorlevel 1 goto :fail
)

C:\msys64\usr\bin\bash.exe -lc "cd '%UROOT%build' && ninja qemu-system-x86_64.exe -j %NUMBER_OF_PROCESSORS%"
if errorlevel 1 goto :fail

xcopy /E /I /Y /Q "%TREE%\pc-bios\*" "%ROOT%build\pc-bios\" >nul

echo.
echo Build OK: %ROOT%build\qemu-system-x86_64.exe
exit /b 0

:fail
echo.
echo Build FAILED - see the messages above.
exit /b 1
