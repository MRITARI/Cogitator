# Cogitator

A mod manager for **Warhammer 40,000: Darktide** (Steam version), for Windows 11 (x64).

It finds your Darktide install, installs the Darktide Mod Loader and Mod Framework, switches mods on and off (the game
update that turns them off is detected), enables or disables individual mods, and checks your mods for updates on every
start. Mods are not supported by Fatshark and are used at your own risk.

Cogitator is an independent fan project. It is not affiliated with, endorsed by or connected to Fatshark, Games Workshop or
Nexus Mods.

Cogitator is released under the MIT licence (see `LICENSE`).

---
---
---

## Install

Windows 11 (x64) only. The Steam version of Darktide is supported; other stores (Game Pass / Microsoft Store) are not.

Download `Cogitator-<version>-win-x64-installer.exe` from the [Releases](https://github.com/MRITARI/Cogitator/releases) page and run it.
Follow the window that opens: it asks for your Nexus key (optional) and installs everything, including the Mod Loader and
Framework. It installs per user and needs no administrator rights. A ticked option adds `cogitator` to your user PATH, so the
commands below work in any new terminal window (open a new one after installing).

## Uninstall

Use **Settings > Apps > Installed apps > Cogitator > Uninstall**. It asks whether to remove the Mod Loader and Framework from
Darktide too (your own mods are never deleted). It also removes the PATH entry. Cogitator's settings, logs and saved Nexus key are kept unless you also remove
them: delete `%LOCALAPPDATA%\cogitator` and the Windows Credential Manager entry named `cogitator`.

---
---
---
The installer is not code-signed, so Windows SmartScreen may warn about it. Click **More info**, then **Run anyway**.

To check that your download is genuine, also download `SHA256SUMS` and `SHA256SUMS.sig` from the same release, then run
(from an installed Cogitator): `cogitator verify-download SHA256SUMS Cogitator-<version>-win-x64-installer.exe`. It checks
the signature of the list against the release key built into Cogitator and the file against the list.

**Before your first install (no Cogitator yet)**, download the installer, `SHA256SUMS` and `SHA256SUMS.sig` into one folder and
check them by hand. The release public key (Ed25519) is
`e2d621da43aa9b6cae3d0dde71e65b69bc77cccbf12fb93ba8cbdabf7bb664ce`.

```powershell
# 1. the installer's hash must appear in SHA256SUMS (a line is printed)
$h = (Get-FileHash .\Cogitator-<version>-win-x64-installer.exe -Algorithm SHA256).Hash.ToLower()
Select-String $h .\SHA256SUMS

# 2. SHA256SUMS must be signed by the release key (needs: pip install cryptography)
python -c "import base64; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey as K; K.from_public_bytes(bytes.fromhex('e2d621da43aa9b6cae3d0dde71e65b69bc77cccbf12fb93ba8cbdabf7bb664ce')).verify(base64.b64decode(open('SHA256SUMS.sig','rb').read()), open('SHA256SUMS','rb').read()); print('signature OK')"
```

If step 1 prints nothing, or step 2 raises `InvalidSignature`, do not run the installer.

To run from source instead (Python 3.10 or newer):

```powershell
git clone https://github.com/MRITARI/Cogitator.git
cd Cogitator
pipx install .
```

## Use

Open **Cogitator** from the Start menu (or the desktop icon); running `cogitator` with no command opens the same window. It needs
the Microsoft Edge WebView2 runtime, which Windows 11 includes. If the Mod Loader is missing it offers to install it from the
Nexus zips you downloaded. The window has the mod list (switch mods on and off, drag to change the load order, link to Nexus,
update), the Mod Loader and Framework, snapshots and profiles, settings and the doctor.

Free Nexus accounts see which mods have updates and are guided through downloading the file; Premium accounts can
update with one key.

The same things work from a terminal with the headless command line, `cogitator ...` (`cogitator --help` lists everything): `cogitator status`, `toggle`, `mod enable|disable <folder>`,
`install-framework --zip <file>`, `check-updates`, `link`, `import`, `update`, `uninstall-framework`, `set-key`,
`restore-database`, `verify-download` and `doctor` (exit code 0 ok, 1 warnings, 2 failures) and
`self-update [--check|--apply|--rollback]` (signed GitHub releases only). `path add|remove|status` adds or removes the folder of
`cogitator.exe` in your user PATH (the installer's PATH option runs it). Commands that change things refuse to run while
the window is open.

`cogitator-setup` opens a step-by-step installer window (add `--dry-run` to look without changing anything, or
`--uninstall` to remove the Mod Loader and Framework). Uninstalling never deletes your own mods.

## Updates

Cogitator looks for a newer version on GitHub Releases and tells you; **nothing is downloaded until you press `A` and
confirm.** An update is downloaded, its SHA-256 and Ed25519 signature are verified, and only then is the app folder replaced;
the previous version is kept so `cogitator self-update --rollback` can restore it. `cogitator self-update --check` looks
without changing anything. To download updates automatically, set `update_mode = "download"` (or `"auto"` to also install them)
and `update_consent = true` in `config.toml`; `update_mode = "off"` turns the check off.

## What Cogitator sends and stores

- **Sent:** a request to the GitHub API for Cogitator's own releases (at most once per 24 hours; GitHub sees your IP address and
  `User-Agent: Cogitator/<version>`). If you set a Nexus key: requests to `api.nexusmods.com` carrying that key, the
  application name and version, and `User-Agent: Cogitator/<version> (Windows; Python x.y)`. Downloading the Mod Loader/Framework from GitHub, if you choose it, contacts GitHub only.
  No telemetry, no analytics, no crash reports.
- **Stored on your PC:** your Nexus key (Windows Credential Manager; if that is unavailable, a plain-text file in the settings
  folder), `config.toml`, `registry.json`, the log and caches in `%LOCALAPPDATA%\cogitator`, and the files Cogitator puts in
  the Darktide folder.
- **Trust:** the Mod Loader, the Framework and the `dtkit-patch.exe` tool inside the Mod Loader come from third parties
  (the Darktide Mod Framework GitHub organisation / Nexus Mods) and are not signed by Cogitator. `dtkit-patch.exe` is run
  to switch mods on and off. Mods are Lua code that runs inside the game; install only mods you trust.

## Troubleshooting

- Open the doctor in the Cogitator window (or run `cogitator doctor`) and read the `[WARN]`/`[FAIL]` lines; each comes with a fix.
- Mods stopped working after a game update: switch them on again in the window (or run `cogitator toggle`).
- Cogitator will not start after an update was cut off (power loss): open **Cogitator Setup** from the Start menu (it puts the
  previous version back), or run the installer again.
- A mod patch was interrupted and the game will not start: `cogitator restore-database`, or in Steam use
  *Properties > Installed Files > Verify integrity of game files*.
- Darktide is not found: Cogitator reads your Steam libraries. If your install is somewhere else, type or paste the folder
  that contains `bundle` and `binaries` when asked.
- A settings file that cannot be read is kept as `config.toml.corrupt` / `registry.json.corrupt` next to it.

**Reporting a bug:** open an issue at <https://github.com/MRITARI/Cogitator/issues> with your Cogitator version, what you did,
what you expected and what happened, and the log file `%LOCALAPPDATA%\cogitator\Cache\cogitator.log` (secrets are removed
from it, but check it before posting).



## Windows packaging

`packaging\build_exe.ps1` (in a venv with `pip install . pyinstaller==6.22.3`) builds, then self-checks every program:
`dist\cogitator\` holds `cogitator.exe` (console: the command line) and `cogitator-web.exe` (the windowed Cogitator window) from one
spec (`packaging\cogitator.spec`) and one shared `_internal` folder, plus `cogitator-updater.exe` and `cogitator-nxm.exe`;
`dist\Cogitator Setup\Cogitator Setup.exe` is the setup/uninstall program. `packaging\cogitator.iss` (Inno Setup) packs both
into the installer: shortcuts "Cogitator" and "Cogitator Setup", then the setup wizard opens. The windows need the Microsoft Edge
WebView2 runtime (included in Windows 11); the installer says so if it is missing and does not block, and `cogitator` on the command
line keeps working. `--self-check [--expect-version V]` works on every exe and, for the windowed ones, writes its result to the log file.

## Third-party software

The installer bundles Python and these libraries, each under its own licence (copyright their authors):
pywebview (BSD-3-Clause), keyring, platformdirs, vdf and pythonnet (MIT-style), httpx and its dependencies (BSD-3-Clause), cryptography
(Apache-2.0 or BSD-3-Clause), certifi (MPL-2.0), Python (PSF-2.0). Their full licence texts are in each project's repository.

