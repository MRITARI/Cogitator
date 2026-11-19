"""Shared fakes for the GUI flow tests: a scripted Ui, a Ctx on a throw-away game folder and a fake Nexus client. No display."""
import threading
from collections import deque

from cogitator.actions import Ctx
from cogitator.config import Settings
from cogitator.nexus import NexusFile
from cogitator.registry import Registry


class FakeUi:
    """`actions.Ui` with scripted answers. An empty script answers: confirm=True, text=None, choose=None."""

    def __init__(self, confirms=(), texts=(), choices=()):
        self.logs: list[tuple[str, str]] = []
        self.asked: list[tuple] = []
        self.confirms, self.texts, self.choices = deque(confirms), deque(texts), deque(choices)

    def log(self, msg, style=""):
        self.logs.append((msg, style))

    def confirm(self, title, body="", default_yes=True):
        self.asked.append(("confirm", title, body))
        return self.confirms.popleft() if self.confirms else True

    def ask_text(self, title, hint="", body="", allow_blank=False):
        self.asked.append(("text", title))
        return self.texts.popleft() if self.texts else None

    def choose(self, question, options, selected=0):
        self.asked.append(("choose", question, list(options), selected))
        return self.choices.popleft() if self.choices else None

    @property
    def text(self) -> str:
        return "\n".join(m for m, _ in self.logs)


def make_ctx(tmp_path, **kw) -> Ctx:
    g = tmp_path / "Darktide"
    (g / "mods").mkdir(parents=True, exist_ok=True)
    (g / "mods" / "mod_load_order.txt").write_text("alpha\n")
    for m in ("alpha", "beta"):
        (g / "mods" / m).mkdir(exist_ok=True)
        (g / "mods" / m / f"{m}.mod").write_text("return {}")
    return Ctx(g, Registry.load(), Settings.load(), threading.RLock(), game_running=lambda: False, **kw)


def nfile(fid, version, uploaded=1_700_000_000, category="MAIN", name=None):
    return NexusFile(file_id=fid, version=version, category=category, uploaded=uploaded, name=name or f"file {fid}",
                     file_name=f"m-{fid}.zip", is_primary=category == "MAIN")


class FakeNexus:
    """Replaces NexusClient: `mods` maps mod id -> (info, files); `searches` is what search_mods returns."""
    mods: dict = {}
    searches: list = []
    changelog: dict = {}
    raise_on: set = set()
    calls: list = []

    def __init__(self, key=None, *a, **k):
        self.key = key

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def _maybe(self, what):
        FakeNexus.calls.append(what)
        if what in FakeNexus.raise_on:
            from cogitator.nexus import NexusError

            raise NexusError(f"{what} failed")

    def mod(self, mod_id):
        self._maybe("mod")
        return FakeNexus.mods[mod_id][0]

    def files(self, mod_id):
        self._maybe("files")
        return FakeNexus.mods[mod_id][1]

    def search_mods(self, text, count=10):
        self._maybe("search")
        return list(FakeNexus.searches)

    def changelogs(self, mod_id):
        self._maybe("changelogs")
        return FakeNexus.changelog

    @classmethod
    def reset(cls):
        cls.mods, cls.searches, cls.changelog, cls.raise_on, cls.calls = {}, [], {}, set(), []
