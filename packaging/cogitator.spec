# One build, two programs in dist\cogitator\: cogitator.exe (console: CLI/TUI) and cogitator-web.exe (windowed web UI).
# Both share one Analysis, one PYZ and one _internal folder, so the self-updater's swap of the whole app folder carries both.
# Build (from the repository root):  pyinstaller --noconfirm --clean --distpath dist --workpath build\pyi packaging\cogitator.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "packaging" / "cogitator.ico")
USER_SCRIPTS = ("entry", "web_entry")

datas, binaries, hidden = [], [], ["sv_ttk", "tkinter", "tkinter.filedialog"]
for pkg in ("textual", "cogitator", "cogitator_gui", "cogitator_web", "keyring"):
    hidden += collect_submodules(pkg)
for pkg in ("textual", "cogitator", "cogitator_web", "sv_ttk"):
    datas += collect_data_files(pkg)
hidden += collect_submodules("rich._unicode_data")
for pkg in ("webview", "clr_loader", "pythonnet", "proxy_tools", "bottle"):  # the web window and its pythonnet bridge
    d, b, h = collect_all(pkg)
    datas, binaries, hidden = datas + d, binaries + b, hidden + h

a = Analysis([str(ROOT / "packaging" / "entry.py"), str(ROOT / "packaging" / "web_entry.py")],
             pathex=[str(ROOT)], binaries=binaries, datas=datas, hiddenimports=hidden)
pyz = PYZ(a.pure)


def scripts_for(name):
    return [s for s in a.scripts if s[0] not in USER_SCRIPTS or s[0] == name]


cli = EXE(pyz, scripts_for("entry"), [], exclude_binaries=True, name="cogitator", icon=ICON, console=True)
web = EXE(pyz, scripts_for("web_entry"), [], exclude_binaries=True, name="cogitator-web", icon=ICON, console=False)
COLLECT(cli, web, a.binaries, a.datas, name="cogitator")
