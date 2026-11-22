"""`cogitator-setup --uninstall --silent`: no window."""
from __future__ import annotations

from cogitator.uninstall_run import UninstallDeps, run_uninstall

from .uninstall_vm import UninstallModel, UServices


def run_silent(services: UServices | None = None, deps: UninstallDeps | None = None, out=print) -> int:
    """`--uninstall --silent`: the defaults only (framework, mods off, key, Cogitator). Never user mods, the mod list or data."""
    m = UninstallModel(services)
    m.load()
    if (m.remove_dml or m.remove_dmf) and m.game_running:
        out("Darktide is running. Close it and run the uninstaller again. Nothing was changed.")
        return 2
    res = run_uninstall(m.build_plan(), deps=deps or UninstallDeps())
    for line in m.finish_lines(res):
        if line:
            out(line)
    return 0 if res.ok else 2
