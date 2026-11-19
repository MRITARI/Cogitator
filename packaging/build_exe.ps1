<#
  Builds the three programs with PyInstaller (no Python needed to run them) and self-checks each.
  Run from the repository root inside a virtual environment:
      pip install ".[web]" "pyinstaller==6.22.3"   (the version the releases are built and tested with)
      powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
  Output: dist\cogitator\cogitator.exe (console) + cogitator-web.exe (windowed) sharing one _internal, + cogitator-updater/-nxm.exe
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
$pin = "6.22.3"
$have = (& $py -m PyInstaller --version).Trim()
if ($have -ne $pin) { throw "PyInstaller $have is installed; releases are built with $pin. Run: pip install pyinstaller==$pin" }
function Check($exe) {
    # Start-Process -Wait: the windowed programs would otherwise not be waited for, and their exit code would be lost.
    $p = Start-Process -FilePath $exe -ArgumentList "--self-check" -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0) { throw "self-check failed ($($p.ExitCode)): $exe" }
    "self-check ok: $exe"
}
# One Analysis, two programs sharing one _internal folder: cogitator.exe (console) and cogitator-web.exe (windowed).
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyi packaging\cogitator.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
# The update helper is a separate small program; it is copied to a temp folder before it replaces the app folder.
& $py -m PyInstaller --noconfirm --clean --onefile --name cogitator-updater --icon "$PWD\packaging\cogitator.ico" `
    --distpath build\updater --workpath build\pyi-updater --specpath build\pyi-updater `
    packaging\updater_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (updater) failed" }
Copy-Item build\updater\cogitator-updater.exe dist\cogitator\cogitator-updater.exe -Force
# The nxm:// link handler is a windowless program, so a click on the Nexus website never flashes a console.
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name cogitator-nxm --icon "$PWD\packaging\cogitator.ico" `
    --distpath build\nxm --workpath build\pyi-nxm --specpath build\pyi-nxm `
    --collect-submodules cogitator `
    packaging\nxm_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (nxm) failed" }
Copy-Item build\nxm\cogitator-nxm.exe dist\cogitator\cogitator-nxm.exe -Force
# The setup wizard is a windowed program: the web-style wizard, with the tkinter one as fallback (--classic).
& $py -m PyInstaller --noconfirm --clean --onedir --windowed --name "Cogitator Setup" --icon "$PWD\packaging\cogitator.ico" `
    --distpath dist --workpath build\pyi-setup --specpath build\pyi-setup `
    --collect-submodules cogitator --collect-submodules cogitator_setup --collect-submodules cogitator_gui `
    --collect-submodules cogitator_web --collect-data cogitator_web --collect-data sv_ttk --hidden-import sv_ttk `
    --collect-all webview --collect-all clr_loader --collect-all pythonnet --collect-all proxy_tools --collect-all bottle `
    --hidden-import tkinter --hidden-import tkinter.filedialog `
    --add-data "$PWD\packaging\cogitator.ico;." --add-data "$PWD\packaging\cogitator.png;." `
    packaging\setup_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (setup) failed" }
Copy-Item LICENSE dist\cogitator\LICENSE -Force
# The licence texts of everything bundled (the generator fails, and so does this script, if one is missing).
& $py packaging\make_notices.py --out dist\cogitator\THIRD-PARTY-NOTICES.txt --pyz build\pyi\cogitator\PYZ-00.toc `
    --pyz "build\pyi-setup\Cogitator Setup\PYZ-00.toc" --app-dir dist\cogitator
if ($LASTEXITCODE -ne 0) { throw "THIRD-PARTY-NOTICES.txt could not be generated" }
Check "dist\cogitator\cogitator.exe"
Check "dist\cogitator\cogitator-web.exe"
Check "dist\cogitator\cogitator-updater.exe"
Check "dist\cogitator\cogitator-nxm.exe"
Check "dist\Cogitator Setup\Cogitator Setup.exe"
Get-FileHash dist\cogitator\cogitator.exe -Algorithm SHA256 | Format-List
