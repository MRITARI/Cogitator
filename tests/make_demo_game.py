"""Build a fake Darktide folder for trying Cogitator Setup and the uninstaller.

    python tests/make_demo_game.py <folder to create>

It holds bundle/, binaries/, a few demo mods with a mod list, and a synthetic bundle_database.data that dtkit-patch
accepts. Nothing in it comes from the real game."""
from __future__ import annotations

import sys
from pathlib import Path

# What dtkit-patch 0.1.7 searches for (big-endian 0xA33A4AA4AF26A69B) and the number of bytes it replaces after it.
PATCH_ANCHOR = (0xA33A4AA4AF26A69B).to_bytes(8, "big")
OLD_SIZE = 84


def synthetic_database() -> bytes:
    return (b"COGITATOR-DEMO-DATABASE-NOT-A-REAL-GAME-FILE" + b"\x00" * 64 + PATCH_ANCHOR + bytes(range(OLD_SIZE)) + b"tail" * 64)


def build(target: Path) -> Path:
    game = Path(target) / "Warhammer 40,000 DARKTIDE"
    (game / "bundle").mkdir(parents=True, exist_ok=True)
    (game / "binaries").mkdir(exist_ok=True)
    (game / "bundle" / "bundle_database.data").write_bytes(synthetic_database())
    (game / "binaries" / "Darktide.exe").write_bytes(b"FAKE GAME EXECUTABLE (demo)")
    for name in ("demo_hud", "demo_chat", "demo_tweaks"):
        d = game / "mods" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.mod").write_text("return {}\n", encoding="utf-8")
        (d / "settings.lua").write_text("-- your saved settings\n", encoding="utf-8")
    (game / "mods" / "mod_load_order.txt").write_bytes(b"-- my demo mods\r\ndemo_hud\r\ndemo_chat\r\n--demo_tweaks\r\n")
    return game


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python tests/make_demo_game.py <empty folder to create>")
    out = build(Path(sys.argv[1]))
    print(f"Demo game folder created: {out}")
    print("It is fake: use it with  --game-dir  and never point Setup at your real Darktide folder while experimenting.")
