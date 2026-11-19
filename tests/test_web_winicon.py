import inspect
from types import SimpleNamespace

from cogitator_web import app, setup_app, winicon


class FakeUser32:
    def __init__(self, hwnd=42):
        self.hwnd, self.sent, self.loaded = hwnd, [], []

    def GetSystemMetrics(self, i):
        return {winicon.SM_CXSMICON: 16, winicon.SM_CXICON: 32}[i]

    def FindWindowW(self, cls, title):
        return self.hwnd

    def LoadImageW(self, inst, path, kind, cx, cy, flags):
        self.loaded.append((cx, cy))
        return 1000 + cx

    def SendMessageW(self, hwnd, msg, kind, handle):
        self.sent.append((hwnd, msg, kind, handle))


def _win():
    return SimpleNamespace(title="t")  # no .native: falls back to FindWindowW


def test_sets_small_and_big():
    u = FakeUser32()
    assert winicon.set_window_icon(_win(), user32=u, platform="win32")
    assert u.loaded == [(16, 16), (32, 32)]
    assert u.sent == [(42, winicon.WM_SETICON, winicon.ICON_SMALL, 1016), (42, winicon.WM_SETICON, winicon.ICON_BIG, 1032)]


def test_quiet_failures(tmp_path):
    assert not winicon.set_window_icon(_win(), user32=FakeUser32(hwnd=0), platform="win32")
    u = FakeUser32()
    assert not winicon.set_window_icon(_win(), tmp_path / "none.ico", user32=u, platform="win32") and not u.sent
    assert not winicon.set_window_icon(_win(), user32=u, platform="linux") and not u.sent
    assert not winicon.set_window_icon(None, user32=object(), platform="win32")


def test_ico_is_packaged_and_valid():
    assert winicon.ICON_PATH.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_entries_call_it():
    assert "winicon.set_window_icon(w)" in inspect.getsource(app)
    assert inspect.getsource(setup_app).count("winicon.set_window_icon(w)") == 2
