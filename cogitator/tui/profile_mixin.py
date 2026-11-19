"""Profile picker for the interface: switch, save, share and import named mod setups."""
from __future__ import annotations

from .. import actions_setup, profiles
from ..mods import ModError
from .screens import ChoiceScreen, ConfirmScreen, InputScreen


class ProfileMixin:
    """Methods of CogitatorApp; they use its attributes, so it is only meant to be mixed into it."""

    def action_profiles(self) -> None:
        if not self._need_game():
            return
        try:
            rows = profiles.list_profiles()
        except OSError as e:
            self.log_line(f"Could not read profiles: {e}", "bold red")
            return
        opts = [f"Switch to: {r['name']}  ({r['enabled']}/{r['mods']} on)" for r in rows]
        opts += ["Save the current setup as a profile...", "Show a share code for the current setup",
                 "Import a share code..."]
        self.push_screen(ChoiceScreen("Profiles", opts), lambda i, rows=rows: self._profile_choice(i, rows))

    def _profile_choice(self, i: int | None, rows: list[dict]) -> None:
        if i is None:
            return
        if i < len(rows):
            name = rows[i]["name"]
            try:
                prof = profiles.load(name)
                plan = profiles.plan_switch(self.game_dir, prof)
            except (ModError, OSError) as e:
                self.log_line(str(e), "bold red")
                return
            body = actions_setup.switch_body(plan)
            self.push_screen(ConfirmScreen(f"Switch to profile {name}?", body, default_yes=False),
                             lambda ok, p=prof: ok and self._bg(self._job_profile_switch, p, name="switch profile",
                                                               group="install", exclusive=True))
        elif i == len(rows):
            self.push_screen(InputScreen("Profile name", "e.g. hordes"), self._profile_name_entered)
        elif i == len(rows) + 1:
            try:
                code = profiles.encode(profiles.capture(self.game_dir, self.registry, "shared"))
            except (ModError, OSError) as e:
                self.log_line(str(e), "bold red")
                return
            try:
                self.copy_to_clipboard(code)
                self.log_line("Setup code copied to the clipboard:", "green")
            except Exception:
                self.log_line("Setup code (select and copy it):", "green")
            self.log_line(code)
        else:
            self.push_screen(InputScreen("Paste a setup code", "cog1:..."), self._profile_code_entered)

    def _profile_name_entered(self, name: str | None) -> None:
        if not name:
            return
        for text, style in actions_setup.save_profile(self._actx(), name):
            self.log_line(text, style)

    def _profile_code_entered(self, code: str | None) -> None:
        if not code:
            return
        for text, style in actions_setup.import_code(self._actx(), code):
            self.log_line(text, style)

    def _job_profile_switch(self, prof: dict) -> None:
        actions_setup.switch_profile(self._actx(), self._tui_ui(), prof)
        self._ui(self.refresh_all)
