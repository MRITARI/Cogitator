"""Mouse-aware widgets: a clickable title bar and the mod table (right-click menu, drag to reorder)."""
from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import DataTable, Static
from textual.widgets._data_table import Coordinate


class TitleButton(Static):
    """A one-line clickable label ("Menu", "?", "X")."""

    class Pressed(Message):
        def __init__(self, button: "TitleButton") -> None:
            super().__init__()
            self.button = button

    def __init__(self, label, *, id: str, tip: str = "", classes: str = "tb") -> None:
        super().__init__(label, id=id, classes=classes, markup=False)
        self.tooltip = tip or None

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Pressed(self))


class ModTable(DataTable):
    """Mods list. Right-click asks for a context menu; dragging a row onto another asks to move it there."""

    class ContextRequested(Message):
        def __init__(self, folder: str, x: int, y: int) -> None:
            super().__init__()
            self.folder, self.x, self.y = folder, x, y

    class Dragging(Message):
        """The pointer is dragging `folder` over `over` (for a hint)."""

        def __init__(self, folder: str, over: str) -> None:
            super().__init__()
            self.folder, self.over = folder, over

    class Dropped(Message):
        def __init__(self, folder: str, target: str) -> None:
            super().__init__()
            self.folder, self.target = folder, target

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._drag_key: str | None = None
        self._drag_over: str | None = None
        self._dragged = False

    # ---- helpers
    def _key_at_row(self, row: int) -> str | None:
        if 0 <= row < self.row_count:
            return self.coordinate_to_cell_key(Coordinate(row, 0)).row_key.value
        return None

    def _row_at_y(self, y: int) -> int:
        """Row under table-relative `y`, clamped to the table (rows are one line high)."""
        top = 1 if self.show_header else 0
        return max(0, min(self.row_count - 1, y - top + int(self.scroll_offset.y)))

    # ---- mouse
    def on_mouse_down(self, event: events.MouseDown) -> None:
        meta = event.style.meta
        row = meta.get("row", -1) if meta else -1
        if row < 0 or row >= self.row_count:
            return
        key = self._key_at_row(row)
        if key is None:
            return
        if event.button == 3:
            event.stop()
            self.move_cursor(row=row)
            self.post_message(self.ContextRequested(key, event.screen_x, event.screen_y))
        elif event.button == 1:
            self._drag_key, self._drag_over, self._dragged = key, key, False
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_key is None or not self.row_count:
            return
        row = self._row_at_y(event.y)
        over = self._key_at_row(row)
        if over is None or over == self._drag_over:
            return
        self._drag_over = over
        if over != self._drag_key:
            self._dragged = True
        self.move_cursor(row=row)
        self.post_message(self.Dragging(self._drag_key, over))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        key, over, dragged = self._drag_key, self._drag_over, self._dragged
        self._drag_key = self._drag_over = None
        self._dragged = False
        if key is None:
            return
        self.release_mouse()
        if dragged and over and over != key:
            self.post_message(self.Dropped(key, over))
