"""Translations for the interface text. English lives in the code: every translatable string is a call

    tr("menu.import", "Import a mod or framework zip...   I")

so the code stays readable and a missing translation falls back to that English text. A translation is a JSON file
{"menu.import": "...", ...} named <language code>.json, in <config>/locales (yours) or cogitator/locales (shipped).

    python -m cogitator.i18n            prints every key with its English text, as a starting template
    python -m cogitator.i18n --check xx  checks a translation file against the code

A translation must keep the same {placeholders} as the English text; otherwise that one string falls back to English."""
from __future__ import annotations

import ast
import json
import locale
import os
import string
import sys
from pathlib import Path

_catalog: dict[str, str] = {}
_language = "en"
_extra_dirs: list[Path] = []  # tests and tools can add folders to search


def locale_dirs() -> list[Path]:
    from .config import config_dir

    return [*_extra_dirs, config_dir() / "locales", Path(__file__).parent / "locales"]


def available() -> list[str]:
    seen = {"en"}
    for d in locale_dirs():
        try:
            seen.update(p.stem for p in d.glob("*.json"))
        except OSError:
            pass
    return sorted(seen)


def _fields(text: str) -> set[str]:
    try:
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}
    except ValueError:
        return {"<invalid>"}


def _load(code: str) -> dict[str, str]:
    for d in locale_dirs():
        p = d / f"{code}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    return {}


def detect(setting: str = "auto") -> str:
    code = os.environ.get("COGITATOR_LANG") or setting
    if code and code != "auto":
        return code.lower()
    try:
        loc = locale.getlocale()[0] or ""
    except Exception:
        loc = ""
    return loc.split("_")[0].lower() or "en"


def set_language(code: str) -> bool:
    """Select a language; returns whether a translation was found. English (or an unknown code) uses the built-in text."""
    global _catalog, _language
    code = (code or "en").lower()
    cat = {} if code.split("-")[0] == "en" else (_load(code) or _load(code.split("-")[0].split("_")[0]))
    _catalog, _language = cat, code
    return bool(cat) or code.split("-")[0] == "en"


def language() -> str:
    return _language


def tr(key: str, default: str, **kw) -> str:
    """The text for `key` in the current language, or `default` (English). `default` must be a string literal."""
    text = _catalog.get(key)
    if text is None or _fields(text) != _fields(default):
        text = default
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return default.format(**kw)


# ------------------------------------------------------------------ tooling

def collect(root: Path | None = None) -> dict[str, str]:
    """Every tr("key", "English") call in the package: key -> English text. A default given as the name of a module-level
    string constant (the long notice and help texts) is resolved from the package's own constants."""
    root = root or Path(__file__).parent
    trees = []
    for p in sorted(root.rglob("*.py")):
        try:
            trees.append(ast.parse(p.read_text(encoding="utf-8")))
        except (OSError, SyntaxError):
            continue
    consts: dict[str, str] = {}
    for tree in trees:
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                try:
                    v = ast.literal_eval(n.value)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(v, str):
                    consts[n.targets[0].id] = v
    out: dict[str, str] = {}
    for tree in trees:
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and len(n.args) >= 2 and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str)):
                continue
            if not ((isinstance(n.func, ast.Name) and n.func.id == "tr") or (isinstance(n.func, ast.Attribute) and n.func.attr == "tr")):
                continue
            d = n.args[1]
            if isinstance(d, ast.Constant) and isinstance(d.value, str):
                out[n.args[0].value] = d.value
            elif isinstance(d, ast.Name) and d.id in consts:
                out[n.args[0].value] = consts[d.id]
    return out


def check(path: Path, keys: dict[str, str] | None = None) -> list[str]:
    """Problems in a translation file: unknown keys and changed placeholders. Empty list = fine."""
    keys = keys if keys is not None else collect()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"cannot read {path}: {e}"]
    if not isinstance(data, dict):
        return ["the file must be a JSON object"]
    out = []
    for k, v in data.items():
        if k not in keys:
            out.append(f"unknown key: {k}")
        elif not isinstance(v, str):
            out.append(f"{k}: the translation must be text")
        elif _fields(v) != _fields(keys[k]):
            out.append(f"{k}: placeholders differ from the English text ({sorted(_fields(keys[k]))})")
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["--check"] and len(argv) == 2:
        p = Path(argv[1])
        problems = check(p if p.suffix == ".json" else Path(f"{argv[1]}.json"))
        print("\n".join(problems) or "OK")
        return 1 if problems else 0
    print(json.dumps(collect(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
