<#
  Builds a standalone Windows folder with PyInstaller (no Python needed to run it).
  Run from the repository root inside a virtual environment:
      pip install . pyinstaller
      powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
  Output: dist\cogitator\cogitator.exe
#>
$ErrorActionPreference = "Stop"
# Use the active virtual environment's interpreter, not whichever `python` is first on PATH.
$py = if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" } else { "python" }
& $py -m PyInstaller --noconfirm --clean --onedir --name cogitator `
    --distpath dist --workpath build\pyi --specpath build\pyi `
    --collect-submodules textual --collect-data textual `
    --collect-submodules rich._unicode_data `
    --collect-submodules keyring `
    packaging\entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
# The update helper is a separate small program; it is copied to a temp folder before it replaces the app folder.
& $py -m PyInstaller --noconfirm --clean --onefile --name cogitator-updater `
    --distpath build\updater --workpath build\pyi-updater --specpath build\pyi-updater `
    packaging\updater_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (updater) failed" }
Copy-Item build\updater\cogitator-updater.exe dist\cogitator\cogitator-updater.exe -Force
& dist\cogitator\cogitator.exe --self-check
if ($LASTEXITCODE -ne 0) { throw "self-check failed: the build is missing something" }
Get-FileHash dist\cogitator\cogitator.exe -Algorithm SHA256 | Format-List
