"""Modal dialogs. Text is rendered with markup disabled (paths contain brackets); results come back via dismiss()."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option


class _Base(ModalScreen):
    DEFAULT_CSS = """
    _Base { align: center middle; }
    _Base > Vertical { width: 80%; max-width: 100; height: auto; max-height: 90%;
                       background: $panel; border: round $primary; padding: 1 2; }
    _Base .title { text-style: bold; margin-bottom: 1; }
    _Base .body { margin-bottom: 1; }
    _Base Horizontal { height: auto; align-horizontal: right; }
    _Base Button { margin-left: 2; }
    """

    def focus_later(self, selector: str) -> None:
        """Focus after the first refresh. Dialogs pushed back-to-back (one dismissed, next pushed from a worker) can
        run on_mount before their children exist; a failed focus must never take the app down."""
        def go() -> None:
            try:
                self.query_one(selector).focus()
            except NoMatches:
                pass

        self.call_after_refresh(go)


class ConfirmScreen(_Base):
    """Yes/no. `default_yes=False` focuses No, so a stray Enter never confirms something destructive."""

    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, title: str, body: str = "", default_yes: bool = True):
        super().__init__()
        self._title, self._body, self._default_yes = title, body, default_yes

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="title", markup=False)
            if self._body:
                yield Static(self._body, classes="body", markup=False)
            with Horizontal():
                yield Button("Yes (y)", id="yes", variant="primary")
                yield Button("No (n)", id="no")

    def on_mount(self) -> None:
        self.focus_later("#yes" if self._default_yes else "#no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class InputScreen(_Base):
    """Single-line prompt. With password=True the field is masked; the value is only ever returned, never logged."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", password: bool = False, body: str = "", allow_blank: bool = False):
        super().__init__()
        self._title, self._ph, self._pw, self._body, self._blank = title, placeholder, password, body, allow_blank

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="title", markup=False)
            if self._body:
                yield Static(self._body, classes="body", markup=False)
            yield Input(placeholder=self._ph, password=self._pw, id="value")
            yield Static("Enter = OK, Esc = cancel", markup=False)

    def on_mount(self) -> None:
        self.focus_later("Input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        v = event.value.strip()
        self.dismiss(v if v else ("" if self._blank else None))

    def on_paste(self, event) -> None:
        """A dropped file lands here even if the box was not focused yet (focus is set after the first refresh)."""
        inp = self.query_one("#value", Input)
        if not inp.has_focus and not self._pw:
            line = (event.text.splitlines() or [""])[0]
            inp.value = line.strip()
            inp.cursor_position = len(inp.value)
            inp.focus()
            event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceScreen(_Base):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, options: list[str], selected: int = 0):
        super().__init__()
        self._q, self._opts, self._sel = question, options, selected

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._q, classes="title", markup=False)
            yield OptionList(*[Option(o, id=str(i)) for i, o in enumerate(self._opts)], id="choices")
            yield Static("Enter = choose, Esc = cancel", markup=False)

    def on_mount(self) -> None:
        self.focus_later("OptionList")
        if 0 < self._sel < len(self._opts):
            self.query_one(OptionList).highlighted = self._sel

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(int(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NoticeScreen(_Base):
    BINDINGS = [Binding("enter,escape", "ok", "OK")]
    DEFAULT_CSS = """
    NoticeScreen VerticalScroll { height: auto; max-height: 55vh; }
    """

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="title", markup=False)
            with VerticalScroll():  # a long text scrolls; the OK button always stays on screen
                yield Static(self._body, classes="body", markup=False)
            with Horizontal():
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.focus_later("Button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_ok(self) -> None:
        self.dismiss(None)


class DoctorScreen(_Base):
    """Scrollable doctor report. Esc / Enter / D close it."""

    BINDINGS = [Binding("escape,enter,d,D", "close", "Close")]
    DEFAULT_CSS = """
    DoctorScreen > Vertical { width: 95%; max-width: 160; height: 90%; }
    DoctorScreen VerticalScroll { height: 1fr; border: round $primary-darken-2; padding: 0 1; }
    """

    def __init__(self, report, exit_code: int):
        super().__init__()
        self._report, self._code = report, exit_code

    def compose(self) -> ComposeResult:
        verdict = {0: "all good", 1: "warnings", 2: "failures"}[self._code]
        with Vertical():
            yield Static(f"Doctor - {verdict} (exit code {self._code})", classes="title", markup=False)
            with VerticalScroll():
                yield Static(self._report, id="report", markup=False)
            with Horizontal():
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self.focus_later("#close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class MenuScreen(ModalScreen):
    """A small pop-up menu anchored at a screen position (right-click menu, top-left Menu button).

    `items` is a list of (label, action_id, enabled); an item of None draws a separator. Dismisses with the chosen
    action_id, or None (Esc, or a click anywhere outside the menu)."""

    BINDINGS = [Binding("escape", "cancel", "Close")]
    DEFAULT_CSS = """
    MenuScreen { background: transparent; align: left top; }
    MenuScreen > OptionList { height: auto; background: $panel; border: round $primary; padding: 0; }
    MenuScreen > OptionList > .option-list--option-disabled { color: $text-muted; text-style: italic; }
    """

    def __init__(self, items: list, x: int = 0, y: int = 0, title: str = ""):
        super().__init__()
        self._items, self._x, self._y, self._title = items, x, y, title

    def compose(self) -> ComposeResult:
        opts = []
        for it in self._items:
            if it is None:
                opts.append(None)
            else:
                label, aid, enabled = it
                opts.append(Option(label, id=aid, disabled=not enabled))
        ol = OptionList(*opts, id="menu")
        if self._title:
            ol.border_title = self._title
        yield ol

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        labels = [it[0] for it in self._items if it]
        width = min(max([len(s) for s in labels] + [len(self._title) + 2, 16]) + 6, max(20, self.app.size.width - 2))
        height = len(self._items) + 2
        ol.styles.width = width
        ol.styles.offset = (max(0, min(self._x, self.app.size.width - width)),
                            max(0, min(self._y, self.app.size.height - height)))
        first = next((i for i, it in enumerate(self._items) if it and it[2]), None)
        if first is not None:
            ol.highlighted = first
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_mouse_down(self, event) -> None:
        if self.get_widget_at(event.screen_x, event.screen_y)[0] is self:  # a click outside the menu closes it
            event.stop()
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
