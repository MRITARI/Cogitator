"""Uninstaller / maintenance page rules. Fake folders only."""
from pathlib import Path

import pytest

from cogitator.registry import Registry
from cogitator.uninstall_run import FullUninstallResult
from cogitator_setup.uninstall_vm import (Detected, MaintenanceModel, SAFE_NOTE, UPage, UServices, UninstallModel, detect)

from test_uninstall import NR, USER_ORDER, installed  # noqa: F401  (fixture re-used)
from test_uninstall_run import FakeApp


def svc(game, reg, *, running=False, app=None):
    return UServices(game_dir=lambda: game, registry=lambda: reg, is_running=lambda: running, app_remover=app,
                     data_dirs=lambda: [(Path("C:/cfg"), 2048), (Path("C:/cache"), 5 * 1024 * 1024)])


def model(installed, **kw):
    g, reg, tool = installed
    m = UninstallModel(svc(g, reg, **kw))
    m.load()
    return m


# ------------------------------------------------------------ defaults

def test_default_choices_are_safe(installed):
    m = model(installed, app=FakeApp())
    assert (m.remove_app, m.remove_dmf, m.remove_dml, m.remove_key) == (True, True, True, True)
    assert (m.remove_data, m.remove_user_mods, m.remove_load_order) == (False, False, False)
    plan = m.build_plan()
    assert plan.remove_dml and plan.remove_dmf and plan.remove_key and plan.app_remover and not plan.remove_data
    assert plan.user_mods == [] and not plan.remove_load_order  # user mods and the mod list are kept by default


def test_what_is_not_installed_is_not_preselected(tmp_path):
    g = tmp_path / "G"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    m = UninstallModel(UServices(game_dir=lambda: g, registry=Registry.load, is_running=lambda: False))
    d = m.load()
    assert (d.dml, d.dmf) == ("missing", "missing") and not m.remove_dml and not m.remove_dmf and not m.remove_app
    assert m.can_next() == (True, "") or m.can_next()[1] == "Choose what to remove."  # only the key is preselected


def test_detection_reports_the_real_state(installed):
    g, reg, tool = installed
    d = detect(svc(g, reg, app=FakeApp()))
    assert (d.dml, d.dmf, d.patched, d.any_installed, d.app_installed) == ("installed", "installed", True, True, True)
    assert set(d.user_mods) == {"usermod", "othermod"}


# ------------------------------------------------------------ user mods need a second confirmation

def test_removing_user_mods_needs_a_second_confirmation_that_lists_every_folder(installed):
    m = model(installed)
    m.set_remove_user_mods(True)
    assert set(m.user_mods_selected) == {"usermod", "othermod"}
    ok, why = m.can_next()
    assert not ok and "Confirm which of your mods" in why and m.needs_user_mod_confirmation
    text = m.user_mod_confirmation_text()
    assert "mods/usermod/" in text and "mods/othermod/" in text and "2 mod folder(s)" in text and "permanently deleted" in text
    m.confirm_user_mods()
    assert m.can_next() == (True, "") and not m.needs_user_mod_confirmation
    m.toggle_user_mod("othermod", False)  # any change withdraws the confirmation
    assert not m.user_mods_confirmed and "Confirm" in m.can_next()[1]
    assert m.user_mods_selected == ["usermod"] and "othermod" not in m.user_mod_confirmation_text()


def test_unticking_every_mod_is_explained(installed):
    m = model(installed)
    m.set_remove_user_mods(True)
    for name in list(m.user_mods_selected):
        m.toggle_user_mod(name, False)
    assert "Tick at least one" in m.can_next()[1]
    m.set_remove_user_mods(False)
    assert m.can_next()[0] and m.build_plan().user_mods == []


def test_unconfirmed_selection_never_reaches_the_plan_as_a_deletion_without_next(installed):
    m = model(installed)
    m.set_remove_user_mods(True)
    assert not m.next() and m.page is UPage.CHOOSE  # blocked until confirmed
    m.confirm_user_mods()
    assert m.next() and m.page is UPage.REVIEW
    assert sorted(m.build_plan().user_mods) == ["othermod", "usermod"]


# ------------------------------------------------------------ the mod list

def test_load_order_deletion_is_only_offered_when_it_is_safe(installed):
    g, reg, tool = installed
    m = model(installed)
    assert not m.load_order_offered()  # it lists mods and they are kept
    m.set_remove_load_order(True)
    assert m.remove_load_order is False
    m.set_remove_user_mods(True)  # all user mods selected -> offered
    assert m.load_order_offered()
    m.set_remove_load_order(True)
    assert m.remove_load_order
    m.toggle_user_mod("usermod", False)  # one mod is kept again -> no longer offered, and the choice is dropped
    assert not m.load_order_offered() and m.remove_load_order is False


def test_header_only_load_order_can_be_removed(installed):
    g, reg, tool = installed
    (g / "mods" / "mod_load_order.txt").write_bytes(b"-- ####\r\n-- header only\r\n")
    m = model(installed)
    assert m.load_order_offered()


# ------------------------------------------------------------ gating, review, finish

def test_game_running_blocks_with_a_retry_message(installed):
    m = model(installed, running=True)
    ok, why = m.can_next()
    assert not ok and "Darktide is running" in why and "Retry" in why
    m.remove_dml = m.remove_dmf = False  # only Cogitator's own key: the game does not matter
    assert m.can_next()[0]


def test_nothing_selected(installed):
    m = model(installed)
    m.remove_dml = m.remove_dmf = m.remove_key = False
    assert m.can_next() == (False, "Choose what to remove.")


def test_review_lists_groups_and_the_kept_note(installed):
    m = model(installed, app=FakeApp())
    m.remove_data = True
    groups = dict(m.review_text())
    assert {"Mods", "Mod Loader and Mod Framework files", "Nexus key", "Cogitator", "Cogitator's settings, caches and rollback copies"} <= set(groups)
    assert any("binaries/mod_loader" in l for l in groups["Mod Loader and Mod Framework files"])
    assert any("KB" in l or "MB" in l for l in groups["Cogitator's settings, caches and rollback copies"])
    assert groups["Kept"] == [SAFE_NOTE] and "Your mods" not in groups
    m.set_remove_user_mods(True)
    assert "Kept" not in dict(m.review_text()) and any("mods/usermod/" in l for l in dict(m.review_text())["Your mods"])


def test_back_only_from_review(installed):
    m = model(installed)
    m.next()
    assert m.page is UPage.REVIEW and m.back() and m.page is UPage.CHOOSE and not m.back()
    m.page = UPage.PROGRESS
    assert not m.back() and not m.next()


def test_finish_lines_are_at_most_three_plain_lines(installed):
    m = model(installed, app=FakeApp())
    ok = m.finish_lines(FullUninstallResult(ok=True))
    assert len(ok) == 3 and "mods are switched off" in ok[0] and "were not touched" in ok[1] and "Verify integrity" in ok[2]
    bad = m.finish_lines(FullUninstallResult(ok=False, what="Mods could not be switched off.", why="It could stop the game starting.", fix="Use Verify integrity."))
    assert bad == ["Mods could not be switched off.", "It could stop the game starting.", "Next step: Use Verify integrity."]
    assert "already removed stays removed" in m.finish_lines(FullUninstallResult(cancelled=True, removed=["x"]))[0]
    assert "nothing was removed" in m.finish_lines(FullUninstallResult(ok=True, dry_run=True))[0].lower()
    for line in ok + bad:
        for jargon in ("unpatch", "patch_999", "bundle_database", "dtkit", "manifest"):
            assert jargon not in line.lower()


# ------------------------------------------------------------ maintenance mode

def test_maintenance_choices_and_summary(installed):
    g, reg, tool = installed
    mm = MaintenanceModel(detect(svc(g, reg, app=FakeApp())))
    assert mm.enabled("repair") and mm.enabled("uninstall") and not mm.enabled("update")  # nothing newer announced
    s = mm.summary()
    assert "already installed" in s and "Mod Loader: installed" in s and "mods are on" in s


def test_the_demo_game_tool_builds_a_valid_fake_folder(tmp_path):
    import make_demo_game as mod
    from cogitator.gamefinder import validate_game_dir
    from cogitator.patcher import OFF, patch_state
    g = mod.build(tmp_path / "demo")
    assert validate_game_dir(g) == (True, "ok") and patch_state(g) == OFF
    db = (g / "bundle" / "bundle_database.data").read_bytes()
    assert mod.PATCH_ANCHOR in db and db.index(mod.PATCH_ANCHOR) + 8 + mod.OLD_SIZE <= len(db)  # what dtkit-patch needs
    assert b"patch_999" not in db and b"patch_001" not in db
    assert (g / "mods" / "mod_load_order.txt").read_bytes().startswith(b"-- my demo mods") and (g / "mods" / "demo_hud" / "demo_hud.mod").is_file()


class FakeUpdater:
    def __init__(self, in_place=True, shown=True):
        from cogitator.selfupdate import Layout, ReleaseInfo, Version
        self.layout = Layout("installer" if in_place else "pipx")
        self.rel = ReleaseInfo(Version.parse("2.0.0"), "v2.0.0", False, "<b>Fixes</b> & more", "u", 1, "s", "c")
        self.shown, self.log, self._ready = shown, [], False

    def announcement(self): return self.rel if self.shown else None
    def ready(self):
        from types import SimpleNamespace
        return SimpleNamespace(version="2.0.0") if self._ready else None
    def stage(self, info): self.log.append("stage"); self._ready = True
    def apply(self): self.log.append("apply")
    def skip_version(self, v): self.log.append(("skip", v)); self.shown = False
    def remind_later(self): self.log.append("remind"); self.shown = False


def test_update_is_offered_only_when_a_newer_release_is_announced(installed):
    g, reg, tool = installed
    det = detect(svc(g, reg, app=FakeApp()))
    assert not MaintenanceModel(det).enabled("update")
    assert not MaintenanceModel(det, FakeUpdater(shown=False)).enabled("update")
    mm = MaintenanceModel(det, FakeUpdater())
    assert mm.enabled("update") and "v2.0.0" in mm.notes() and "<b>Fixes</b> & more" in mm.notes()  # shown verbatim, never interpreted


def test_install_downloads_then_applies_and_skip_remind_are_recorded(installed):
    g, reg, tool = installed
    det = detect(svc(g, reg, app=FakeApp()))
    u = FakeUpdater()
    mm = MaintenanceModel(det, u)
    assert "Installing v2.0.0" in mm.install() and u.log == ["stage", "apply"]
    u = FakeUpdater(); mm = MaintenanceModel(det, u); mm.skip()
    assert u.log == [("skip", "2.0.0")] and not mm.enabled("update")
    u = FakeUpdater(); mm = MaintenanceModel(det, u); mm.remind_later()
    assert u.log == ["remind"] and not mm.enabled("update")


def test_non_installer_installs_get_guidance_and_nothing_is_applied(installed):
    g, reg, tool = installed
    u = FakeUpdater(in_place=False)
    out = MaintenanceModel(detect(svc(g, reg, app=FakeApp())), u).install()
    assert "pipx upgrade cogitator" in out and u.log == []
