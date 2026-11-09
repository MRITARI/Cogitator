<#
  Builds the three programs with PyInstaller (no Python needed to run them) and self-checks each.
  Run from the repository root inside a virtual environment:
      pip install . pyinstaller
      powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
  Output: dist\cogitator\cogitator.exe (+ cogitator-updater.exe beside it)
          dist\Cogitator Setup\Cogitator Setup.exe
  Then packaging\cogitator.iss packs both into the installer (Inno Setup).
#>
$ErrorActionPreference = "Stop"
# Use the active virtual environment's interpreter, not whichever `python` is first on PATH.
$py = if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" } else { "python" }
$base = (& $py -c "import sys; print(sys.base_prefix)").Trim()
if ($base -match "conda") {
    throw "This Python is based on conda ($base). Build from a venv created with a python.org or Microsoft Store Python."
}
function Check($exe) {
    & $exe --self-check
    if ($LASTEXITCODE -ne 0) { throw "self-check failed: $exe" }
}
& $py -m PyInstaller --noconfirm --clean --onedir --name cogitator --icon "$PWD\packaging\cogitator.ico" `
    --distpath dist --workpath build\pyi --specpath build\pyi `
    --collect-submodules textual --collect-data textual `
    --collect-submodules rich._unicode_data `
    --collect-submodules keyring --collect-submodules cogitator `
    packaging\entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
# The update helper is a separate small program; it is copied to a temp folder before it replaces the app folder.
& $py -m PyInstaller --noconfirm --clean --onefile --name cogitator-updater `
    --distpath build\updater --workpath build\pyi-updater --specpath build\pyi-updater `
    packaging\updater_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (updater) failed" }
Copy-Item build\updater\cogitator-updater.exe dist\cogitator\cogitator-updater.exe -Force
# The setup wizard is a windowed tkinter program.
& $py -m PyInstaller --noconfirm --clean --onedir --windowed --name "Cogitator Setup" --icon "$PWD\packaging\cogitator.ico" `
    --distpath dist --workpath build\pyi-setup --specpath build\pyi-setup `
    --collect-submodules cogitator --collect-submodules cogitator_setup `
    packaging\setup_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (setup) failed" }
Check "dist\cogitator\cogitator.exe"
Check "dist\cogitator\cogitator-updater.exe"
Check "dist\Cogitator Setup\Cogitator Setup.exe"
Get-FileHash dist\cogitator\cogitator.exe -Algorithm SHA256 | Format-List
