"""The GUI's queue and workers, with fake engine functions and no display."""
import threading
import time

from cogitator.config import set_api_key
from cogitator_web.controller import CHOICE, CONFIRM, TEXT, Controller, GuiUi

SECRET = "ControllerSecretKey-0123456789abcdef"


def test_worker_posts_log_and_ui_calls_in_order():
    c = Controller()
    seen = []
    c.run("job", lambda: (c.log("one", "green"), c.ui(seen.append, 2)))
    c.join()
    msgs = c.drain()
    assert msgs[0] == ("log", "one", "green")
    kind, fn, args, kwargs = msgs[1]
    assert kind == "ui" and (args, kwargs) == ((2,), {})
    fn(*args, **kwargs)
    assert seen == [2] and c.drain() == []


def test_worker_exception_is_reported_not_raised_and_is_redacted():
    set_api_key(SECRET)
    c = Controller()

    def boom():
        raise RuntimeError(f"Nexus said no to {SECRET}")

    c.run("update check", boom)
    c.join()
    ((kind, name, text),) = c.drain()
    assert (kind, name) == ("error", "update check") and "RuntimeError" in text and SECRET not in text


def test_log_lines_are_redacted():
    set_api_key(SECRET)
    c = Controller()
    c.log(f"key={SECRET}")
    assert SECRET not in c.drain()[0][1]


def test_one_job_per_group_and_the_refused_one_says_so():
    c = Controller()
    gate = threading.Event()
    assert c.run("first", gate.wait, group="net") is True
    assert c.busy("net")
    assert c.run("second", lambda: None, group="net") is False
    msg = c.drain()[0][1]
    assert "Cannot start 'second'" in msg and "'first' is still running" in msg and "net task" in msg
    assert c.run("other", lambda: None, group="patch") is True  # other groups are independent
    gate.set()
    c.join()
    assert not c.busy("net") and c.run("third", lambda: None, group="net") is True
    c.join()


def _answer_when_asked(c, value, results):
    for _ in range(200):
        for m in c.drain():
            if m[0] == "ask":
                results.append(m[1:3])
                Controller.answer(m[3], value)
                return
        time.sleep(0.02)


def test_confirm_round_trip_blocks_only_the_worker():
    c, out, asked = Controller(), [], []
    ui = GuiUi(c)
    t = threading.Thread(target=_answer_when_asked, args=(c, True, asked))
    t.start()
    c.run("job", lambda: out.append(ui.confirm("Install x?", "body", default_yes=False)))
    c.join()
    t.join()
    assert out == [True]
    assert asked[0] == (CONFIRM, {"title": "Install x?", "body": "body", "default_yes": False})


def test_text_and_choice_round_trips_and_none_means_cancel():
    c = Controller()
    ui = GuiUi(c)
    asked, got = [], []
    t = threading.Thread(target=_answer_when_asked, args=(c, "https://x/mods/5", asked))
    t.start()
    c.run("a", lambda: got.append(ui.ask_text("Link?", "hint", allow_blank=True)))
    c.join()
    t.join()
    assert got == ["https://x/mods/5"] and asked[0][0] == TEXT and asked[0][1]["allow_blank"] is True
    asked.clear()
    t = threading.Thread(target=_answer_when_asked, args=(c, None, asked))
    t.start()
    c.run("b", lambda: got.append(ui.choose("Which?", ["x", "y"], 1)))
    c.join()
    t.join()
    assert got[1] is None and asked[0] == (CHOICE, {"question": "Which?", "options": ["x", "y"], "selected": 1})


def test_closing_the_window_releases_a_blocked_worker():
    c = Controller()
    ui = GuiUi(c)
    out = []
    c.run("job", lambda: out.append(ui.confirm("never answered")))
    time.sleep(0.3)
    assert out == []  # still waiting
    c.close()
    c.join()
    assert out == [False]


def test_nothing_is_queued_after_close():
    c = Controller()
    c.close()
    c.log("late")
    assert c.drain() == []


def _serve(c, answer, timeout=5.0):
    """Answer the next question a worker asks, like the window would; returns the asked message."""
    end = time.time() + timeout
    while time.time() < end:
        for m in c.drain():
            if m[0] == "ask":
                c.answer(m[3], answer)
                return m
        time.sleep(0.01)
    raise AssertionError("the worker never asked")


def test_new_flows_confirm_round_trip_cancel_and_exception(tmp_path):
    from gui_fakes import make_ctx

    from cogitator import actions_setup

    ctx = make_ctx(tmp_path)
    actions_setup.create_snapshot(ctx, "one")
    from cogitator import snapshots

    name = snapshots.list_snapshots()[0]["name"]
    lo = ctx.game_dir / "mods" / "mod_load_order.txt"
    c, results = Controller(), []
    lo.write_text("beta\n")
    c.run("restore snapshot", lambda: results.append(actions_setup.restore_snapshot(ctx, GuiUi(c), name)), "install")
    asked = _serve(c, False)  # the person says no: nothing is restored
    c.join()
    assert asked[1] == "confirm" and asked[2]["title"] == "Restore this snapshot?" and results == [None] and lo.read_text() == "beta\n"
    c.run("restore snapshot", lambda: results.append(actions_setup.restore_snapshot(ctx, GuiUi(c), name)), "install")
    _serve(c, True)
    c.join()
    assert results[1].refresh and lo.read_text().startswith("alpha")

    def boom():
        raise OSError(f"disk gone for {SECRET}")

    set_api_key(SECRET)
    c.run("snapshot", boom, "install")
    c.join()
    errs = [m for m in c.drain() if m[0] == "error"]
    assert errs and errs[0][1] == "snapshot" and SECRET not in errs[0][2]
