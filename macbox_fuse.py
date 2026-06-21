#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import os
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
    """Expose the real filesystem under the FUSE mount as read-only paths."""

    def __init__(self, virtual_root: Path | None = None):
        self.virtual_root = virtual_root

    def _real(self, path: str) -> Path:
        rel = path.lstrip("/")
        if not rel:
            return Path("/")
        return Path("/") / rel

    def access(self, path: str, mode: int) -> int:
        real = self._real(path)
        if not real.exists() and not real.is_symlink():
            fuse_error(errno.ENOENT)
        if mode & os.W_OK:
            fuse_error(errno.EROFS)
        if not os.access(real, mode):
            fuse_error(errno.EACCES)
        return 0

    def getattr(self, path: str, fh=None) -> dict:
        try:
            st = os.lstat(self._real(path))
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
        real = self._real(path)
        try:
            return [".", "..", *os.listdir(real)]
        except FileNotFoundError:
            fuse_error(errno.ENOENT)
        except NotADirectoryError:
            fuse_error(errno.ENOTDIR)

    def readlink(self, path: str) -> str:
        try:
            target = os.readlink(self._real(path))
        except FileNotFoundError:
            fuse_error(errno.ENOENT)
        if self.virtual_root is not None and os.path.isabs(target):
            return str(self.virtual_root / target.lstrip("/"))
        return target

    def open(self, path: str, flags: int) -> int:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
            fuse_error(errno.EROFS)
        try:
            return os.open(self._real(path), flags)
        except FileNotFoundError:
            fuse_error(errno.ENOENT)

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, size)

    def release(self, path: str, fh: int) -> int:
        os.close(fh)
        return 0

    def statfs(self, path: str) -> dict:
        st = os.statvfs(self._real(path))
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
        fuse_error(errno.EROFS)

    def write(self, path: str, data: bytes, offset: int, fh: int):
        fuse_error(errno.EROFS)

    def mkdir(self, path: str, mode: int):
        fuse_error(errno.EROFS)

    def unlink(self, path: str):
        fuse_error(errno.EROFS)

    def rmdir(self, path: str):
        fuse_error(errno.EROFS)

    def rename(self, old: str, new: str):
        fuse_error(errno.EROFS)

    def chmod(self, path: str, mode: int):
        fuse_error(errno.EROFS)

    def chown(self, path: str, uid: int, gid: int):
        fuse_error(errno.EROFS)

    def truncate(self, path: str, length: int, fh=None):
        fuse_error(errno.EROFS)

    def utimens(self, path: str, times=None):
        fuse_error(errno.EROFS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MacBox read-only FUSE mirror helper.")
    parser.add_argument("--session", required=True)
    parser.add_argument("--mount", required=True)
    parser.add_argument("--foreground", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if FUSE is None:
        raise SystemExit("Python FUSE binding is unavailable. Install fusepy for the MacBox interpreter.")
    mount = Path(args.mount)
    mount.mkdir(parents=True, exist_ok=True)
    FUSE(ReadOnlyMirrorOperations(mount), str(mount), foreground=args.foreground, nothreads=True, ro=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
