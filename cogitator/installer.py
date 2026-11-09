"""File placement with rollback: everything overwritten or removed is backed up first. The undo log is written to
`<backup_root>/journal.json`, so a finished or interrupted run can be rolled back later."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def _lp(p: Path) -> Path:
    from .loader import long_path

    return long_path(p)


class Transaction:
    def __init__(self, backup_root: Path):
        self.backup_root = Path(backup_root)
        self._undo: list[tuple[str, Path, Path | None]] = []  # (kind, dest, backup)
        self._n = 0

    # ---- journal
    @property
    def journal_path(self) -> Path:
        return self.backup_root / "journal.json"

    def _save_journal(self) -> None:
        from .fsutil import atomic_write_text

        data = [{"kind": k, "dest": str(d), "backup": str(b) if b else None} for k, d, b in self._undo]
        atomic_write_text(self.journal_path, json.dumps(data, indent=1))

    @classmethod
    def from_journal(cls, backup_root: Path) -> "Transaction":
        tx = cls(backup_root)
        try:
            data = json.loads(tx.journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        for e in data:
            tx._undo.append((e["kind"], Path(e["dest"]), Path(e["backup"]) if e.get("backup") else None))
        return tx

    def _slot(self) -> Path:
        """Next free backup slot. Never reuses a path, even if another run shares this directory (same-second stamps)."""
        while True:
            self._n += 1
            p = self.backup_root / str(self._n)
            if not _lp(p).exists():
                return p

    def _log(self, kind: str, dest: Path, backup: Path | None) -> None:
        self._undo.append((kind, dest, backup))
        self._save_journal()  # recorded BEFORE the change, so a crash mid-change is still recoverable

    # ---- operations
    def put_file(self, src: Path, dest: Path) -> None:
        dest = Path(dest)
        _lp(dest).parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if _lp(dest).exists():
            backup = self._slot()
            _lp(backup).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_lp(dest), _lp(backup))
        self._log("file", dest, backup)
        tmp = dest.with_name(dest.name + ".cogitator.tmp")
        try:
            shutil.copyfile(_lp(src), _lp(tmp))
            os.replace(_lp(tmp), _lp(dest))
        finally:
            if _lp(tmp).exists():
                _lp(tmp).unlink()

    def replace_dir(self, src: Path, dest: Path) -> None:
        dest = Path(dest)
        backup = None
        if _lp(dest).exists():
            backup = self._slot()
            _lp(backup).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(_lp(dest)), str(_lp(backup)))
        self._log("dir", dest, backup)
        shutil.copytree(_lp(src), _lp(dest))

    # ---- removals (reversible: the removed thing is moved into the rollback dir, never destroyed)
    def remove_file(self, dest: Path) -> bool:
        dest = Path(dest)
        if not _lp(dest).is_file():
            return False
        backup = self._slot()
        _lp(backup).parent.mkdir(parents=True, exist_ok=True)
        self._log("rmfile", dest, backup)
        shutil.move(str(_lp(dest)), str(_lp(backup)))
        return True

    def remove_dir(self, dest: Path) -> bool:
        dest = Path(dest)
        if not _lp(dest).is_dir():
            return False
        backup = self._slot()
        _lp(backup).parent.mkdir(parents=True, exist_ok=True)
        self._log("rmdir", dest, backup)
        shutil.move(str(_lp(dest)), str(_lp(backup)))
        return True

    def remove_empty_dir(self, dest: Path) -> bool:
        dest = Path(dest)
        if not _lp(dest).is_dir() or any(_lp(dest).iterdir()):
            return False
        self._log("emptydir", dest, None)
        _lp(dest).rmdir()
        return True

    def snapshot_file(self, dest: Path) -> None:
        """Keep a copy of a file that is about to be edited in place; rollback restores it."""
        dest = Path(dest)
        if _lp(dest).is_file():
            backup = self._slot()
            _lp(backup).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_lp(dest), _lp(backup))
            self._log("restorefile", dest, backup)

    def rollback(self) -> None:
        for kind, dest, backup in reversed(self._undo):
            try:
                if kind in ("rmfile", "rmdir"):
                    if backup and _lp(backup).exists():
                        _lp(dest).parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(_lp(backup)), str(_lp(dest)))
                elif kind == "emptydir":
                    _lp(dest).mkdir(parents=True, exist_ok=True)
                elif kind == "restorefile":
                    if backup and _lp(backup).exists():
                        shutil.copy2(_lp(backup), _lp(dest))
                elif kind == "dir":
                    if _lp(dest).exists():
                        shutil.rmtree(_lp(dest))
                    if backup and _lp(backup).exists():
                        shutil.move(str(_lp(backup)), str(_lp(dest)))
                else:
                    if backup and _lp(backup).exists():
                        os.replace(_lp(backup), _lp(dest))
                    elif _lp(dest).exists():
                        _lp(dest).unlink()
            except OSError:
                pass  # best effort; keep going so as much as possible is restored
        self._undo.clear()
        try:
            self._save_journal()
        except OSError:
            pass
