"""The tkinter wizard, driven for real (Tk needs a display; skipped with a clear reason where none exists).

Real engine + fake game folder + FakeTool in place of dtkit-patch. No real network, Steam, game or keyring.
"""
import logging
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from cogitator import config as cfg
from cogitator.config import get_api_key
from cogitator.doctor import run_doctor
from cogitator.gamefinder import Resolution
from cogitator.patcher import ON, OFF, patch_state
from cogitator.registry import Registry
from cogitator.setup_run import KeyCheck, SetupDeps, SetupResult
from cogitator.sources import GITHUB_SNAPSHOT, SourceOption, choose_sources, local_option
from cogitator_setup import platform_gate
from cogitator_setup.app import Dialogs, SetupApp, main
from cogitator_setup.controller import Controller
from cogitator_setup.viewmodel import Page, Services, SetupModel

from test_frameworks import DMF_FILES, DML_FILES, mkzip
from test_patcher import FakeTool, ORIG

SECRET = "GuiSecretKey-0a1b2c3d4e5f6a7b8c9d"


@pytest.fixture
def root():
    r = None
    for attempt in range(4):  # Tk start-up is occasionally flaky from the Microsoft Store Python's WindowsApps folder
        try:
            r = tk.Tk()
            break
        except tk.TclError as e:
            err = e
            time.sleep(0.3)
    if r is None:  # headless CI without a display
        pytest.skip(f"Tk could not start: {err}")
    r.withdraw()
    yield r
    import gc

    gc.collect()  # drop Tk variables on the main thread, not from a worker's garbage collection
    try:
        r.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def world(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    (g / "mods" / "usermod").mkdir(parents=True)
    (g / "mods" / "usermod" / "usermod.mod").write_text("x")
    (g / "mods" / "mod_load_order.txt").write_bytes(b"usermod\r\n")
    z = tmp_path / "zips"
    z.mkdir()
    dml = mkzip(z / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z a.zip", DML_FILES)
    dmf = mkzip(z / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z b.zip", DMF_FILES)
    return g, dml, dmf


class FakeDialogs(Dialogs):
    def __init__(self, zips=None, answers=None):
        self.log, self.zips, self.answers = [], dict(zips or {}), list(answers or [])
        self.ask_yes_no = self._ask
        self.show_error = lambda t, b: self.log.append(("error", t, b))
        self.show_info = lambda t, b: self.log.append(("info", t, b))
        self.browse_dir = lambda: None
        self.browse_zip = lambda what: str(self.zips.get(what)) if self.zips.get(what) else None
        self.open_url = lambda u: self.log.append(("url", u))
        self.launch = lambda: self.log.append(("launch",))

    def _ask(self, title, body):
        self.log.append(("ask", title, body))
        return self.answers.pop(0) if self.answers else False


def services(world, *, running=False, check=None, snapshot=False):
    g, dml, dmf = world

    def resolve(kind, premium, key):
        if snapshot and kind == "dmf":
            return choose_sources("dmf", premium=premium, github=SourceOption("dmf", GITHUB_SNAPSHOT, "snap @ 0107475", "0107475" + "a" * 33,
                                                                          unreleased=True), nexus=None), ""
        return [local_option(kind, dml if kind == "dml" else dmf)], ""

    return Services(find_game=lambda: Resolution(g, "steam", [g]), is_running=lambda: running,
                    installed_state=lambda game, k: ("missing", None), resolve_options=resolve,
                    check_key=check or (lambda k: KeyCheck(True, False, "20000 daily / 2000 hourly", "Key ok (free account).")))


def real_deps(tool=None, **kw):
    return SetupDeps(patch_runner=tool or FakeTool(), is_running=lambda: False,
                     doctor_fn=lambda game, **k: run_doctor(game, **{**k, "offline": True, "log_dirs": [], "steam_roots_fn": lambda: []}), **kw)


def make_app(root, world, *, dialogs=None, deps=None, run_fn=None, first_run=False, **svc):
    model = SetupModel(services(world, **svc))
    ctl = Controller(model, deps or real_deps(), **({"run_fn": run_fn} if run_fn else {}))
    return SetupApp(model=model, controller=ctl, dialogs=dialogs or FakeDialogs(), root=root, first_run=first_run)


def pump(app, pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if pred():
            return True
        time.sleep(0.01)
    app.root.update()
    return bool(pred())


def widget_texts(w) -> list[str]:
    """Every string a user could see (or a screen reader could read) anywhere in the window."""
    out = []
    for c in [w, *w.winfo_children()]:
        if c is not w:
            out += widget_texts(c)
        for opt in ("text", "values"):
            try:
                v = c.cget(opt)
                out.append(" ".join(map(str, v)) if isinstance(v, (tuple, list)) else str(v))
            except tk.TclError:
                pass
        if isinstance(c, (tk.Entry, ttk.Entry, ttk.Combobox)):
            out.append(c.get())
        if isinstance(c, tk.Text):
            out.append(c.get("1.0", "end"))
        if isinstance(c, ttk.Treeview):
            out += [" ".join(map(str, c.item(i, "values"))) for i in c.get_children()]
    return out


def tree(g: Path) -> dict:
    return {p.relative_to(g).as_posix(): p.read_bytes() for p in sorted(g.rglob("*")) if p.is_file()}


# ------------------------------------------------------------ Windows 11 gate

def test_gate_build_numbers():
    assert not platform_gate.is_supported(None)
    assert not platform_gate.is_supported(19045)  # Windows 10 22H2
    assert not platform_gate.is_supported(21999)
    assert platform_gate.is_supported(22000)  # first Windows 11 build
    assert platform_gate.is_supported(26300)
    assert platform_gate.MESSAGE == "Cogitator Setup supports Windows 11 only"


@pytest.mark.parametrize("build", [None, 19045, 21999])
def test_unsupported_systems_get_the_message_and_nothing_else_happens(build):
    shown, built = [], []
    rc = main([], build=build, show_error=lambda t, b: shown.append((t, b)), app_factory=lambda: built.append(1))
    assert rc == 1 and built == []  # no window, no model, no engine call
    assert shown == [("Cogitator Setup", "Cogitator Setup supports Windows 11 only")]


def test_a_supported_build_starts_the_app():
    started = []

    class A:
        def mainloop(self):
            started.append(1)

    assert main([], build=22000, app_factory=lambda: A()) == 0 and started == [1]


@pytest.mark.skipif(not platform_gate.windows_build(), reason="Windows only")
def test_this_machine_reports_a_real_build_number():
    assert isinstance(platform_gate.windows_build(), int) and platform_gate.windows_build() > 0


def test_self_check_flag():
    assert main(["--self-check"]) == 0


# ------------------------------------------------------------ launching from the finish page

def test_terminal_command_prefers_windows_terminal_with_a_console_fallback():
    from cogitator_setup.launch import CREATE_NEW_CONSOLE, launch, terminal_command
    argv, flags = terminal_command(["cogitator.exe"], which=lambda n: r"C:\wt.exe" if n == "wt.exe" else None)
    assert argv == [r"C:\wt.exe", "--title", "Cogitator", "cogitator.exe"] and flags == 0
    argv, flags = terminal_command(["cogitator.exe"], which=lambda n: None)
    assert argv == ["cogitator.exe"] and flags == CREATE_NEW_CONSOLE
    calls = []
    assert launch(["x.exe"], which=lambda n: None, popen=lambda a, **k: calls.append((a, k))) == ["x.exe"]
    assert calls[0][1]["creationflags"] == CREATE_NEW_CONSOLE and "shell" not in calls[0][1]  # never through a shell


# ------------------------------------------------------------ Express: one click to a modded game

def test_express_install_end_to_end_with_the_real_engine(root, world):
    g, *_ = world
    d = FakeDialogs()
    app = make_app(root, world, dialogs=d)
    assert str(app.ex_install.cget("state")) == "disabled"  # nothing can be clicked before the sources are known
    assert pump(app, lambda: str(app.ex_install.cget("state")) == "normal")
    shown = app.ex_lines.get("1.0", "end")
    assert "Darktide Mod Loader" in shown and "Switch mods on" in shown and str(g) in app.ex_status.cget("text")
    app.ex_install.invoke()
    assert pump(app, lambda: app.model.page is Page.FINISH)
    res = app.result
    assert res.ok and patch_state(g) == ON and (g / "binaries" / "mod_loader").is_file() and (g / "mods" / "dmf" / "dmf.mod").is_file()
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == b"usermod\r\n" and (g / "mods" / "usermod" / "usermod.mod").is_file()
    texts = " ".join(t for t in widget_texts(app.root))
    assert "Installed:" in texts and "Mods are on" in texts and "press T" in texts and "desktop" in texts
    assert [r for r in app.steps.get_children()] and all(app.steps.item(i, "values")[0] in ("[x]", "[-]") for i in app.steps.get_children())
    assert app.bar["value"] == 100
    app.btn_finish.invoke()  # shows the check-up and launches
    assert ("launch",) in d.log and any(e[0] == "info" and "[OK]" in e[2] for e in d.log)


def test_express_blocked_while_the_game_runs_and_advanced_still_works(root, world):
    app = make_app(root, world, running=True)
    assert pump(app, lambda: "Darktide is running" in app.ex_status.cget("text"))
    assert str(app.ex_install.cget("state")) == "disabled"
    app.ex_adv.invoke()
    assert app.in_wizard and app.model.page is Page.WELCOME


def test_express_shows_the_unreleased_label_beside_the_button(root, world):
    app = make_app(root, world, snapshot=True)
    assert pump(app, lambda: str(app.ex_install.cget("state")) == "normal")
    assert "[UNRELEASED SNAPSHOT]" in app.ex_lines.get("1.0", "end")


# ------------------------------------------------------------ the full wizard

def walk_to_key(app):
    app.ex_adv.invoke()
    app.v_accept.set(True)
    app._accept_changed()
    app.btn_next.invoke()  # -> game
    assert pump(app, lambda: app.model.game_dir is not None)
    app.btn_next.invoke()  # -> key
    assert app.model.page is Page.KEY


def test_welcome_blocks_next_until_the_box_is_ticked_and_enter_advances(root, world):
    app = make_app(root, world)
    app.ex_adv.invoke()
    assert str(app.btn_next.cget("state")) == "disabled" and "understand" in app.nav_msg.cget("text")
    assert str(app.btn_back.cget("state")) == "disabled"
    app.btn_next.invoke()
    assert app.model.page is Page.WELCOME  # a disabled button does nothing
    app.v_accept.set(True)
    app._accept_changed()
    assert str(app.btn_next.cget("state")) == "normal"
    app._on_return()  # keyboard
    assert app.model.page is Page.GAME


def test_game_page_browse_and_typed_paths_and_errors(root, world, tmp_path):
    g, *_ = world
    svc = services(world)
    svc.find_game = lambda: Resolution(None, "none", message="Darktide not found via Steam.")
    model = SetupModel(svc)
    app = SetupApp(model=model, controller=Controller(model, real_deps()), dialogs=FakeDialogs(), root=root)
    app.ex_adv.invoke()
    app.v_accept.set(True)
    app._accept_changed()
    app.btn_next.invoke()
    assert pump(app, lambda: "Browse" in app.game_status.cget("text"))
    assert str(app.btn_next.cget("state")) == "disabled"
    app.dlg.browse_dir = lambda: str(tmp_path / "nope")
    app.on_browse_game()
    assert "does not look like Darktide" in app.game_status.cget("text") and str(app.btn_next.cget("state")) == "disabled"
    app.v_game.set(f'"{g}"')  # pasted with quotes
    app._typed_game()
    assert app.model.game_dir == g and str(app.btn_next.cget("state")) == "normal"


def test_key_page_is_masked_validates_and_never_echoes_the_key(root, world):
    app = make_app(root, world)
    walk_to_key(app)
    assert str(app.e_key.cget("show")) == "*"
    app.v_key.set(SECRET)
    app._key_typed()
    assert str(app.btn_next.cget("state")) == "disabled" and "Validate" in app.nav_msg.cget("text")
    app.v_show.set(True)
    app._toggle_show()
    assert str(app.e_key.cget("show")) == ""  # the Show toggle works
    app.btn_validate.invoke()
    assert pump(app, lambda: "Key ok" in app.key_status.cget("text"))
    assert str(app.e_key.cget("show")) == "*" and app.v_key.get() == ""  # cleared and re-masked
    assert not any(SECRET in t for t in widget_texts(app.root))  # nowhere in the window
    assert app.model.key_for_plan() == SECRET and str(app.btn_next.cget("state")) == "normal"


def test_invalid_key_message_and_skip(root, world):
    app = make_app(root, world, check=lambda k: KeyCheck(False, message="Nexus did not accept this key. Check that you copied all of it, or press Skip."))
    walk_to_key(app)
    app.v_key.set(SECRET)
    app._key_typed()
    app.btn_validate.invoke()
    assert pump(app, lambda: "did not accept" in app.key_status.cget("text"))
    assert app.model.key_for_plan() is None and SECRET not in app.key_status.cget("text")
    assert str(app.btn_next.cget("state")) == "disabled"
    app.on_skip_key()  # Skip moves on and forgets the key
    assert app.model.page is Page.COMPONENTS and not app.model.has_key() and app.v_key.get() == ""


def test_components_review_manual_zip_and_unreleased_warning(root, world, tmp_path):
    g, dml, dmf = world
    d = FakeDialogs(zips={"Darktide Mod Framework": dmf})
    app = make_app(root, world, dialogs=d, snapshot=True)
    walk_to_key(app)
    app.on_skip_key()
    assert app.model.page is Page.COMPONENTS and "not installed" in app.comp_status["dml"].cget("text")
    app.comp_vars["dml"].set(False)
    app._comp_changed("dml")
    assert app.model.selected_kinds() == ["dmf"]
    app.v_mods.set(False)  # mods cannot be switched on without the Mod Loader (see test_dmf_only_with_mods_on_is_stopped_up_front)
    app.model.enable_mods = False
    app.btn_next.invoke()  # -> review
    assert app.model.page is Page.REVIEW
    assert pump(app, lambda: "UNRELEASED" in app.review_text.get("1.0", "end"))  # drawn after the worker reports back
    review = app.review_text.get("1.0", "end")
    assert "UNRELEASED SNAPSHOT" in review and "Left switched off" in review
    assert "not installed - left as it is" in review  # the unticked Mod Loader is explicitly left alone
    assert str(app.btn_next.cget("text")) == "Install" and str(app.btn_next.cget("state")) == "normal"
    # switch to the manual route: Install is blocked until a zip is chosen
    opts = app.model.options["dmf"]
    manual = [i for i, o in enumerate(opts) if o.kind == "manual"][0]
    app._pick_source("dmf", manual)
    assert str(app.btn_next.cget("state")) == "disabled" and "zip you downloaded" in app.review_status.cget("text")
    app._choose_zip("dmf")
    assert str(app.btn_next.cget("state")) == "normal"
    app.btn_next.invoke()  # Install
    assert pump(app, lambda: app.model.page is Page.FINISH)
    assert app.result.ok and (g / "mods" / "dmf" / "dmf.mod").is_file() and not (g / "binaries" / "mod_loader").exists()
    assert patch_state(g) == OFF  # mods were left switched off, as chosen


def test_a_wrong_zip_is_explained_and_not_accepted(root, world):
    g, dml, dmf = world
    d = FakeDialogs(zips={"Darktide Mod Framework": dml})  # a DML zip offered for DMF
    app = make_app(root, world, dialogs=d, snapshot=True)
    walk_to_key(app)
    app.on_skip_key()
    app.btn_next.invoke()
    assert pump(app, lambda: "UNRELEASED" in app.review_text.get("1.0", "end"))
    manual = [i for i, o in enumerate(app.model.options["dmf"]) if o.kind == "manual"][0]
    app._pick_source("dmf", manual)
    app._choose_zip("dmf")
    assert any(e[0] == "error" and "DML archive, not DMF" in e[2] for e in d.log)
    assert str(app.btn_next.cget("state")) == "disabled"


# ------------------------------------------------------------ the key never leaks through a whole install

def test_the_key_typed_in_the_gui_appears_nowhere_after_a_full_install(root, world, tmp_path, caplog):
    g, *_ = world
    caplog.set_level(logging.DEBUG)
    cfg.setup_logging(logging.DEBUG)
    app = make_app(root, world)
    walk_to_key(app)
    app.v_key.set(SECRET)
    app._key_typed()
    app.btn_validate.invoke()
    assert pump(app, lambda: "Key ok" in app.key_status.cget("text"))
    for _ in range(3):  # Next x2 (-> components -> review), then Install
        app.btn_next.invoke()
        pump(app, lambda: True)
    assert pump(app, lambda: app.model.page is Page.REVIEW and str(app.btn_next.cget("state")) == "normal")
    app.btn_next.invoke()
    assert pump(app, lambda: app.model.page is Page.FINISH)
    assert app.result.ok and get_api_key() == SECRET  # stored, but only via the key store
    app.btn_copy.invoke()
    clip = app.root.clipboard_get()
    for h in logging.getLogger().handlers:
        h.flush()
    haystacks = widget_texts(app.root) + [clip, app.result.diagnostics(), caplog.text, str(Registry.load().data)]
    for rootdir in (tmp_path / "cfg", tmp_path / "cache", g, tmp_path / "zips"):
        for p in Path(rootdir).rglob("*"):
            if p.is_file() and p.stat().st_size < 30_000_000:
                haystacks.append(p.read_bytes().decode("utf-8", "replace"))
    assert not any(SECRET in h for h in haystacks)
    assert not (tmp_path / "cfg" / "nexus_key").exists()


# ------------------------------------------------------------ cancel, close, confirm, failures, responsiveness

class SlowRun:
    """A stand-in orchestrator that behaves like the engine: works in steps, honours cancel, can ask a question."""

    def __init__(self, ask=None):
        self.started, self.ask = False, ask
        self.answer = None

    def __call__(self, plan, progress, cancel, deps):
        self.started = True
        progress(__import__("cogitator.setup_run", fromlist=["x"]).ProgressEvent("dml", "start", 0, 2))
        if self.ask:
            self.answer = deps.confirm(*self.ask)
        for _ in range(500):
            if cancel.is_set():
                return SetupResult(cancelled=True, rolled_back=True, what="Setup was cancelled.")
            time.sleep(0.01)
        return SetupResult(ok=True, mods_on=True)


def start_run(app):
    assert pump(app, lambda: str(app.ex_install.cget("state")) == "normal")
    app.ex_install.invoke()


def test_the_ui_stays_responsive_while_the_engine_works(root, world):
    run = SlowRun()
    app = make_app(root, world, run_fn=run)
    start_run(app)
    assert pump(app, lambda: run.started)
    ticks = []
    for i in range(20):  # UI-thread timers keep firing while the worker is busy
        app.root.after(i * 5, lambda i=i: ticks.append(i))
    app.root.after(1, lambda: app.scale_text(1))
    assert pump(app, lambda: len(ticks) == 20, 3) and app.model.page is Page.PROGRESS
    app.ctl.cancel()
    assert pump(app, lambda: app.model.page is Page.FINISH)


def test_cancel_button_asks_the_engine_to_stop_and_shows_a_calm_message(root, world):
    app = make_app(root, world, run_fn=SlowRun())
    start_run(app)
    assert pump(app, lambda: app.model.page is Page.PROGRESS)
    app.btn_cancel.invoke()
    assert "Cancelling" in str(app.btn_cancel.cget("text")) and str(app.btn_cancel.cget("state")) == "disabled"
    assert pump(app, lambda: app.model.page is Page.FINISH)
    texts = " ".join(widget_texts(app.root))
    assert "cancelled" in texts.lower() and "nothing was changed" in texts.lower()
    assert str(app.fin_launch.cget("text")) and "disabled" in app.fin_launch.state()


def test_closing_the_window_mid_run_cancels_instead_of_killing_it(root, world):
    app = make_app(root, world, run_fn=SlowRun())
    start_run(app)
    assert pump(app, lambda: app.model.page is Page.PROGRESS)
    app.on_close()
    assert app.root.winfo_exists() and app.ctl.cancel_event.is_set()  # still open until the engine has unwound
    assert pump(app, lambda: app.model.page is Page.FINISH)


def test_the_engine_can_ask_a_question_and_the_answer_comes_back(root, world):
    run = SlowRun(ask=("This download does not match the official release", "Install it anyway?"))
    d = FakeDialogs(answers=[True])
    app = make_app(root, world, dialogs=d, run_fn=run)
    start_run(app)
    assert pump(app, lambda: run.answer is True)
    assert d.log[0][:2] == ("ask", "This download does not match the official release")
    app.ctl.cancel()
    assert pump(app, lambda: app.model.page is Page.FINISH)


def test_a_failed_run_explains_what_to_do_and_copies_redacted_details(root, world):
    g, *_ = world
    before = tree(g)
    app = make_app(root, world, deps=real_deps(FakeTool(fail_patch=True)))
    start_run(app)
    assert pump(app, lambda: app.model.page is Page.FINISH)
    res = app.result
    assert not res.ok and res.failed_step == "patch" and res.rolled_back and tree(g) == before and patch_state(g) == OFF
    texts = [t for t in widget_texts(app.root) if t]
    joined = " ".join(texts)
    assert "Next step:" in joined and "Setup could not finish" in joined
    assert "disabled" in app.fin_launch.state() and not app.v_launch.get()  # nothing to launch after a failure
    app.btn_copy.invoke()
    assert "failed_step=patch" in app.root.clipboard_get()


def test_a_worker_crash_becomes_a_dialog_not_a_frozen_window(root, world):
    def boom(plan, progress, cancel, deps):
        raise RuntimeError("kaboom " + SECRET)

    d = FakeDialogs()
    app = make_app(root, world, dialogs=d, run_fn=boom)
    start_run(app)
    assert pump(app, lambda: any(e[0] == "error" for e in d.log))
    err = [e for e in d.log if e[0] == "error"][0]
    assert "install" in err[2] and "kaboom" in err[2]


def test_text_can_be_made_larger_and_smaller_and_reset(root, world):
    app = make_app(root, world)
    base = abs(int(app.fonts[0].cget("size")))
    app.scale_text(1)
    assert abs(int(app.fonts[0].cget("size"))) == base + 1
    app.scale_text(-1)
    app.scale_text(-1)
    assert abs(int(app.fonts[0].cget("size"))) == max(8, base - 1)
    app.scale_text(0)
    assert abs(int(app.fonts[0].cget("size"))) == base


def test_back_goes_to_the_previous_page_but_never_into_a_run(root, world):
    app = make_app(root, world)
    walk_to_key(app)
    app.btn_back.invoke()
    assert app.model.page is Page.GAME
    app.model.page = Page.PROGRESS
    app._refresh_nav()
    assert str(app.btn_back.cget("state")) == "disabled" and str(app.btn_next.cget("state")) == "disabled"


def test_dmf_only_with_mods_on_is_stopped_up_front_with_a_plain_message(root, world):
    g, *_ = world
    before = tree(g)
    app = make_app(root, world, snapshot=True)
    walk_to_key(app)
    app.on_skip_key()
    app.comp_vars["dml"].set(False)
    app._comp_changed("dml")  # Mod Loader unticked while "Switch mods on" is still ticked
    app.btn_next.invoke()
    assert pump(app, lambda: "UNRELEASED" in app.review_text.get("1.0", "end"))
    assert str(app.btn_next.cget("state")) == "disabled"
    assert "Mods can only be switched on once the Mod Loader is installed" in app.review_status.cget("text")
    assert tree(g) == before  # and nothing was attempted, let alone rolled back


def test_the_checkup_report_opens_in_a_scrollable_window(root):
    from cogitator_setup.app import scroll_dialog
    long_text = "\n".join(f"[OK]   check {i}: fine" for i in range(200))
    seen = {}

    def inspect_and_close():
        top = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][0]
        text = [w for w in top.winfo_children() if isinstance(w, tk.Text)][0]
        seen["has_all_lines"] = "check 199" in text.get("1.0", "end")
        seen["read_only"] = str(text.cget("state")) == "disabled"
        seen["scrollbar"] = any(isinstance(w, ttk.Scrollbar) for w in top.winfo_children())
        seen["bar_range"] = text.yview()[1] < 1.0  # more text than fits -> it scrolls
        seen["escape_bound"] = any("Escape" in seq for seq in top.bind())  # keyboard focus can't be relied on in a test (Windows foreground lock)
        [w for w in top.winfo_children() if isinstance(w, ttk.Button)][0].invoke()

    root.after(250, inspect_and_close)
    root.deiconify()
    scroll_dialog("Check-up results", long_text)
    root.withdraw()
    assert seen == {"has_all_lines": True, "read_only": True, "scrollbar": True, "bar_range": True, "escape_bound": True}
    assert not [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


def test_the_finish_page_uses_the_scrollable_dialog_by_default():
    from cogitator_setup.app import Dialogs, scroll_dialog
    assert Dialogs().show_info is scroll_dialog


# ------------------------------------------------------------ first run: opened by the installer (--wizard)

def walk_first_run(app):
    """welcome -> game -> key (skipped) -> review; returns once Install is possible."""
    assert app.in_wizard and app.model.page is Page.WELCOME
    app.v_accept.set(True)
    app._accept_changed()
    app.btn_next.invoke()
    assert pump(app, lambda: app.model.page is Page.GAME and app.model.game_dir is not None and str(app.btn_next.cget("state")) == "normal")
    app.btn_next.invoke()
    assert app.model.page is Page.KEY and str(app.e_key.cget("show")) == "*"  # the key is typed hidden
    app.on_skip_key()
    assert app.model.page is Page.REVIEW  # no Components page: the installer already did the app and shortcuts
    assert pump(app, lambda: app.model.install_ready()[0] and bool(app.model.options) and str(app.btn_next.cget("state")) == "normal")


def test_first_run_goes_straight_to_the_wizard_comes_to_the_front_and_installs_everything(root, world):
    g, *_ = world
    d = FakeDialogs()
    app = make_app(root, world, dialogs=d, first_run=True)
    assert app.root.attributes("-topmost")  # raised above the installer window
    walk_first_run(app)
    rows = {r.label: r.lines for r in app.model.review_rows()}
    assert "installer" in rows["Cogitator"][0] and "installer" in rows["Shortcuts"][0]
    app.btn_next.invoke()
    assert pump(app, lambda: app.model.page is Page.FINISH)
    assert app.result.ok and patch_state(g) == ON and (g / "binaries" / "mod_loader").is_file() and (g / "mods" / "dmf" / "dmf.mod").is_file()
    assert "app" not in [app.steps.item(i, "values")[1] for i in app.steps.get_children()] and "shortcuts" not in app.result.__dict__.get("steps", "")
    assert app.v_launch.get() and str(app.btn_finish.cget("text")) == "Start Cogitator"
    assert "Installed:" in " ".join(widget_texts(app.root))
    app.btn_finish.invoke()
    assert ("launch",) in d.log


def test_first_run_that_fails_midway_leaves_the_game_folder_as_it_was(root, world):
    g, *_ = world
    before = tree(g)
    app = make_app(root, world, deps=real_deps(FakeTool(fail_patch=True)), first_run=True)
    walk_first_run(app)
    app.btn_next.invoke()
    assert pump(app, lambda: app.model.page is Page.FINISH)
    assert not app.result.ok and app.result.rolled_back and tree(g) == before and patch_state(g) == OFF
    assert "disabled" in app.fin_launch.state()  # nothing to start from a failed run; Cogitator itself is unaffected


def test_first_run_cancelled_mid_run_leaves_nothing_changed(root, world):
    g, *_ = world
    before = tree(g)
    app = make_app(root, world, run_fn=SlowRun(), first_run=True)
    walk_first_run(app)
    app.btn_next.invoke()
    assert pump(app, lambda: app.model.page is Page.PROGRESS)
    app.btn_cancel.invoke()
    assert pump(app, lambda: app.model.page is Page.FINISH) and app.result.cancelled and tree(g) == before


def test_wizard_flag_opens_the_app_in_first_run_mode_even_when_installed():
    seen = []

    class A:
        def mainloop(self):
            pass

    assert main(["--wizard"], build=22000, installed=True, app_factory=lambda **kw: seen.append(kw) or A()) == 0
    assert seen == [{"first_run": True}]


def test_start_cogitator_runs_the_installed_app_exe(tmp_path):
    from cogitator_setup.launch import cogitator_target

    (tmp_path / "setup").mkdir()
    (tmp_path / "app").mkdir()
    assert cogitator_target(True, str(tmp_path / "setup" / "Cogitator Setup.exe")) is None  # not installed there
    (tmp_path / "app" / "cogitator.exe").write_bytes(b"x")
    assert cogitator_target(True, str(tmp_path / "setup" / "Cogitator Setup.exe")) == [str((tmp_path / "app" / "cogitator.exe").resolve())]
    assert cogitator_target(False, "py.exe") == ["py.exe", "-m", "cogitator"]


def test_navigation_buttons_keep_their_space_when_the_review_page_is_tall(root, world):
    app = make_app(root, world, first_run=True)
    walk_first_run(app)
    assert app.root.pack_slaves()[0] is app.nav and str(app.nav.pack_info()["side"]) == "bottom"  # packed first: never squeezed out
    assert int(app.review_text.cget("height")) <= 10
