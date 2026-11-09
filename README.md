# Cogitator

A terminal mod manager for **Warhammer 40,000: Darktide**, for Windows 11 (x64).

It finds your Darktide install, installs the Darktide Mod Loader and Mod Framework, switches mods on and off (the game
update that turns them off is detected), enables or disables individual mods, and checks your mods for updates on every
start. Mods are not supported by Fatshark and are used at your own risk.

## Install

Windows 11 (x64) only.

Download `Cogitator-<version>-win-x64-installer.exe` from the [Releases](https://github.com/MRITARI/Cogitator/releases) page and run it.
Follow the window that opens: it asks for your Nexus key (optional) and installs everything, including the Mod Loader and
Framework. It installs per user and needs no administrator rights.

The installer is not code-signed, so Windows SmartScreen may warn about it. Click **More info**, then **Run anyway**.

To run from source instead (Python 3.10 or newer):

```powershell
git clone https://github.com/MRITARI/Cogitator.git
cd Cogitator
pipx install .
```

## Use

Run `cogitator`. If the Mod Loader is missing it offers to install it from the Nexus zips you downloaded.

| Key | Action |
|---|---|
| `T` | Mods on or off (asks before switching off) |
| `Space` | Enable or disable the selected mod |
| `Shift+Up` / `Shift+Down` (or `[` / `]`) | Move the selected mod up or down in the load order |
| `U` / `Ctrl+U` | Update the selected mod / all mods |
| `F` | Update the Mod Loader or Framework |
| `R` | Re-check for updates |
| `L` | Link the selected mod to its Nexus page |
| `I` | Import a mod, Mod Loader or Framework zip (or just drop the file onto the window) |
| `K` | Set your Nexus API key (only needed for update checks) |
| `D` | Run the doctor |
| `Q` | Quit |

Free Nexus accounts see which mods have updates and are guided through downloading the file; Premium accounts can
update with one key.

The same things work without the interface: `cogitator status`, `toggle`, `mod enable|disable <folder>`,
`install-framework --zip <file>`, `check-updates`, `link`, `import`, `update`, `uninstall-framework`, `set-key` and
`doctor` (exit code 0 ok, 1 warnings, 2 failures) and `self-update [--check|--apply|--rollback]` (signed GitHub releases only).

`cogitator-setup` opens a step-by-step installer window (add `--dry-run` to look without changing anything, or
`--uninstall` to remove the Mod Loader and Framework). Uninstalling never deletes your own mods.

## Updates

Installed copies check GitHub Releases for a newer version. An update is downloaded, its SHA-256 and Ed25519 signature
are verified, and only then is the app folder replaced; the previous version is kept so `cogitator self-update --rollback`
can restore it. `cogitator self-update --check` looks without changing anything.
