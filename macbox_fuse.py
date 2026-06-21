#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import os
import shutil
from pathlib import Path

try:
    from fuse import FUSE, FuseOSError, Operations
except Exception:  # pragma: no cover - exercised on hosts without fusepy.
    FUSE = None
    FuseOSError = None
    Operations = object


def fuse_error(code: int):
    if FuseOSError is not None:
        raise FuseOSError(code)
    raise OSError(code, os.strerror(code))


class ReadOnlyMirrorOperations(Operations):
    """Expose real paths through a virtual root, optionally backed by overlay writes."""

    def __init__(self, virtual_root: Path | None = None, overlay_root: Path | None = None, deletes_file: Path | None = None):
        self.virtual_root = virtual_root
        self.overlay_root = overlay_root
        self.deletes_file = deletes_file

    def _rel(self, path: str) -> str:
        return str(self._real(path)).lstrip("/")

    def _real(self, path: str) -> Path:
        rel = path.lstrip("/")
        if not rel:
            return Path("/")
        raw = Path("/") / rel
        return raw.parent.resolve(strict=False) / raw.name

    def _overlay(self, path: str) -> Path:
        if self.overlay_root is None:
            return self._real(path)
        return self.overlay_root / self._rel(path)

    def _deleted_paths(self) -> set[str]:
        if self.deletes_file is None or not self.deletes_file.exists():
            return set()
        return {line for line in self.deletes_file.read_text().splitlines() if line}

    def _is_deleted(self, path: str) -> bool:
        real = str(self._real(path))
        for deleted in self._deleted_paths():
            if real == deleted or real.startswith(deleted.rstrip("/") + "/"):
                return True
        return False

    def _note_delete(self, path: str) -> None:
        if self.deletes_file is None:
            return
        self.deletes_file.parent.mkdir(parents=True, exist_ok=True)
        real = str(self._real(path))
        if real not in self._deleted_paths():
            with self.deletes_file.open("a") as fh:
                fh.write(real + "\n")

    def _remove_overlay(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    def _read_path(self, path: str) -> Path:
        overlay = self._overlay(path)
        if self.overlay_root is not None and (overlay.exists() or overlay.is_symlink()):
            return overlay
        if self._is_deleted(path):
            fuse_error(errno.ENOENT)
        real = self._real(path)
        if not real.exists() and not real.is_symlink():
            fuse_error(errno.ENOENT)
        return real

    def _copy_symlink_target_as_file(self, source: Path, overlay: Path) -> None:
        try:
            target = source.resolve(strict=True)
        except FileNotFoundError:
            overlay.touch(exist_ok=True)
            return
        if target.is_dir():
            fuse_error(errno.EISDIR)
        shutil.copy2(target, overlay)

    def _copy_up(self, path: str, directory: bool = False, dereference_symlink: bool = False) -> Path:
        if self.overlay_root is None:
            fuse_error(errno.EROFS)
        overlay = self._overlay(path)
        if overlay.exists() or overlay.is_symlink():
            if dereference_symlink and overlay.is_symlink():
                overlay.unlink()
                self._copy_symlink_target_as_file(self._real(path), overlay)
            return overlay
        real = self._real(path)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        if self._is_deleted(path):
            if directory:
                overlay.mkdir(parents=True, exist_ok=True)
            return overlay
        if real.exists() or real.is_symlink():
            if real.is_dir() and not real.is_symlink():
                shutil.copytree(real, overlay, symlinks=True, dirs_exist_ok=True)
            elif real.is_symlink():
                if dereference_symlink:
                    self._copy_symlink_target_as_file(real, overlay)
                else:
                    os.symlink(os.readlink(real), overlay)
            else:
                shutil.copy2(real, overlay)
        elif directory:
            overlay.mkdir(parents=True, exist_ok=True)
        return overlay

    def _write_path(self, path: str, directory: bool = False) -> Path:
        if self.overlay_root is None:
            fuse_error(errno.EROFS)
        overlay = self._overlay(path)
        if directory:
            overlay.mkdir(parents=True, exist_ok=True)
        else:
            overlay.parent.mkdir(parents=True, exist_ok=True)
        return overlay

    def access(self, path: str, mode: int) -> int:
        try:
            resolved = self._read_path(path)
        except OSError:
            raise
        except Exception:
            fuse_error(errno.ENOENT)
        if mode & os.W_OK:
            if self.overlay_root is None:
                fuse_error(errno.EROFS)
            return 0
        if not os.access(resolved, mode):
            fuse_error(errno.EACCES)
        return 0

    def getattr(self, path: str, fh=None) -> dict:
        try:
            st = os.lstat(self._read_path(path))
        except FileNotFoundError:
            fuse_error(errno.ENOENT)
        return {key: getattr(st, key) for key in (
            "st_atime",
            "st_ctime",
            "st_gid",
            "st_mode",
            "st_mtime",
            "st_nlink",
            "st_size",
            "st_uid",
        )}

    def readdir(self, path: str, fh) -> list[str]:
        entries = {".", ".."}
        real = self._real(path)
        overlay = self._overlay(path)
        deleted = self._deleted_paths()
        if self._is_deleted(path) and not overlay.exists():
            fuse_error(errno.ENOENT)
        if real.exists():
            if not real.is_dir():
                fuse_error(errno.ENOTDIR)
            for name in os.listdir(real):
                child = str(real / name)
                if child not in deleted:
                    entries.add(name)
        if self.overlay_root is not None and overlay.exists():
            if not overlay.is_dir():
                fuse_error(errno.ENOTDIR)
            entries.update(os.listdir(overlay))
        if len(entries) == 2 and not real.exists() and not overlay.exists():
            fuse_error(errno.ENOENT)
        return sorted(entries)

    def readlink(self, path: str) -> str:
        try:
            target = os.readlink(self._read_path(path))
        except FileNotFoundError:
            fuse_error(errno.ENOENT)
        if self.virtual_root is not None and os.path.isabs(target):
            return str(self.virtual_root / target.lstrip("/"))
        return target

    def open(self, path: str, flags: int) -> int:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        try:
            if flags & write_flags:
                overlay = self._overlay(path)
                overlay_preexisting = overlay.exists() or overlay.is_symlink()
                target = self._copy_up(path, dereference_symlink=True)
                try:
                    return os.open(target, flags, 0o666)
                except OSError:
                    if not overlay_preexisting:
                        self._remove_overlay(target)
                    raise
            return os.open(self._read_path(path), flags)
        except FileNotFoundError:
            fuse_error(errno.ENOENT)

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, size)

    def release(self, path: str, fh: int) -> int:
        os.close(fh)
        return 0

    def statfs(self, path: str) -> dict:
        target = self.overlay_root if self.overlay_root is not None else self._read_path(path)
        st = os.statvfs(target)
        return {key: getattr(st, key) for key in (
            "f_bavail",
            "f_bfree",
            "f_blocks",
            "f_bsize",
            "f_favail",
            "f_ffree",
            "f_files",
            "f_flag",
            "f_frsize",
            "f_namemax",
        )}

    def create(self, path: str, mode: int, fi=None):
        target = self._write_path(path)
        return os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)

    def write(self, path: str, data: bytes, offset: int, fh: int):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, data)

    def mkdir(self, path: str, mode: int):
        overlay = self._overlay(path)
        real = self._real(path)
        if overlay.exists() or overlay.is_symlink() or (
            not self._is_deleted(path) and (real.exists() or real.is_symlink())
        ):
            fuse_error(errno.EEXIST)
        target = self._write_path(path, directory=True)
        os.chmod(target, mode)

    def unlink(self, path: str):
        if self.overlay_root is None:
            fuse_error(errno.EROFS)
        overlay = self._overlay(path)
        real = self._real(path)
        overlay_exists = overlay.exists() or overlay.is_symlink()
        if not overlay_exists and (
            not (real.exists() or real.is_symlink()) or self._is_deleted(path)
        ):
            fuse_error(errno.ENOENT)
        target = overlay if overlay_exists else real
        if target.is_dir() and not target.is_symlink():
            fuse_error(errno.EISDIR)
        self._remove_overlay(overlay)
        self._note_delete(path)

    def rmdir(self, path: str):
        if self.overlay_root is None:
            fuse_error(errno.EROFS)
        overlay = self._overlay(path)
        real = self._real(path)
        overlay_exists = overlay.exists()
        if not overlay_exists and (not real.exists() or self._is_deleted(path)):
            fuse_error(errno.ENOENT)
        target = overlay if overlay_exists else real
        if not target.is_dir() or target.is_symlink():
            fuse_error(errno.ENOTDIR)
        visible_children = [entry for entry in self.readdir(path, None) if entry not in (".", "..")]
        if visible_children:
            fuse_error(errno.ENOTEMPTY)
        self._remove_overlay(overlay)
        self._note_delete(path)

    def rename(self, old: str, new: str):
        old_overlay = self._copy_up(old)
        new_overlay = self._write_path(new)
        if new_overlay.exists() or new_overlay.is_symlink():
            self._remove_overlay(new_overlay)
        os.rename(old_overlay, new_overlay)
        self._note_delete(old)

    def chmod(self, path: str, mode: int):
        os.chmod(self._copy_up(path, dereference_symlink=True), mode)

    def chown(self, path: str, uid: int, gid: int):
        os.chown(self._copy_up(path, dereference_symlink=True), uid, gid)

    def truncate(self, path: str, length: int, fh=None):
        target = self._copy_up(path, dereference_symlink=True)
        with target.open("r+b") as fh2:
            fh2.truncate(length)

    def utimens(self, path: str, times=None):
        os.utime(self._copy_up(path, dereference_symlink=True), times)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MacBox FUSE overlay helper.")
    parser.add_argument("--session", required=True)
    parser.add_argument("--mount", required=True)
    parser.add_argument("--overlay", required=True)
    parser.add_argument("--deletes", required=True)
    parser.add_argument("--foreground", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if FUSE is None:
        raise SystemExit("Python FUSE binding is unavailable. Install fusepy for the MacBox interpreter.")
    mount = Path(args.mount)
    overlay = Path(args.overlay)
    deletes = Path(args.deletes)
    mount.mkdir(parents=True, exist_ok=True)
    overlay.mkdir(parents=True, exist_ok=True)
    deletes.parent.mkdir(parents=True, exist_ok=True)
    deletes.touch(exist_ok=True)
    FUSE(ReadOnlyMirrorOperations(mount, overlay, deletes), str(mount), foreground=args.foreground, nothreads=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
