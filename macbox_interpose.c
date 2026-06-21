#define _DARWIN_C_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

static __thread int mb_in_hook = 0;

static const char *mb_root(void) {
    const char *root = getenv("MB_ROOT");
    return root && root[0] ? root : NULL;
}

static bool mb_starts_with_path(const char *path, const char *prefix) {
    if (!path || !prefix) return false;
    size_t n = strlen(prefix);
    if (strncmp(path, prefix, n) != 0) return false;
    return path[n] == '\0' || path[n] == '/';
}

static bool mb_absolute_path(const char *path, char *out, size_t out_len) {
    if (!path || !path[0]) return false;
    if (path[0] == '/') {
        snprintf(out, out_len, "%s", path);
        return true;
    }

    char cwd[PATH_MAX];
    if (!getcwd(cwd, sizeof(cwd))) return false;
    snprintf(out, out_len, "%s/%s", cwd, path);
    return true;
}

static bool mb_should_ignore(const char *abs_path) {
    const char *root = mb_root();
    if (!root) return true;
    if (mb_starts_with_path(abs_path, root)) return true;
    if (mb_starts_with_path(abs_path, "/dev")) return true;
    return false;
}

static bool mb_overlay_path(const char *path, char *out, size_t out_len) {
    const char *root = mb_root();
    if (!root) return false;

    char abs_path[PATH_MAX];
    if (!mb_absolute_path(path, abs_path, sizeof(abs_path))) return false;
    if (mb_should_ignore(abs_path)) return false;

    snprintf(out, out_len, "%s%s", root, abs_path);
    return true;
}

static void mb_mkdirs_for_file(const char *path) {
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof(tmp), "%s", path);

    char *last = strrchr(tmp, '/');
    if (!last) return;
    *last = '\0';

    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, 0777);
            *p = '/';
        }
    }
    mkdir(tmp, 0777);
}

static void mb_copy_up_if_needed(const char *real_path, const char *overlay_path) {
    struct stat overlay_st;
    if (lstat(overlay_path, &overlay_st) == 0) return;

    struct stat real_st;
    if (lstat(real_path, &real_st) != 0) return;
    if (!S_ISREG(real_st.st_mode)) return;

    int (*real_open)(const char *, int, ...) = dlsym(RTLD_NEXT, "open");
    if (!real_open) return;

    mb_mkdirs_for_file(overlay_path);
    int src = real_open(real_path, O_RDONLY);
    if (src < 0) return;
    int dst = real_open(overlay_path, O_WRONLY | O_CREAT | O_TRUNC, real_st.st_mode & 0777);
    if (dst < 0) {
        close(src);
        return;
    }

    char buffer[65536];
    ssize_t n;
    while ((n = read(src, buffer, sizeof(buffer))) > 0) {
        char *p = buffer;
        while (n > 0) {
            ssize_t written = write(dst, p, (size_t)n);
            if (written <= 0) break;
            p += written;
            n -= written;
        }
    }
    close(dst);
    close(src);
}

static bool mb_flags_write(int flags) {
    int accmode = flags & O_ACCMODE;
    return accmode == O_WRONLY || accmode == O_RDWR || (flags & O_CREAT) || (flags & O_TRUNC) || (flags & O_APPEND);
}

int mb_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }

    int (*real_open)(const char *, int, ...) = dlsym(RTLD_NEXT, "open");
    if (!real_open) {
        errno = ENOSYS;
        return -1;
    }

    if (mb_in_hook) {
        return (flags & O_CREAT) ? real_open(path, flags, mode) : real_open(path, flags);
    }

    mb_in_hook = 1;
    char overlay[PATH_MAX];
    char abs_path[PATH_MAX];
    const char *target = path;
    bool mapped = mb_overlay_path(path, overlay, sizeof(overlay));
    if (mapped && mb_absolute_path(path, abs_path, sizeof(abs_path))) {
        if (mb_flags_write(flags)) {
            mb_copy_up_if_needed(abs_path, overlay);
            mb_mkdirs_for_file(overlay);
            target = overlay;
        } else if (access(overlay, F_OK) == 0) {
            target = overlay;
        }
    }

    int result = (flags & O_CREAT) ? real_open(target, flags, mode) : real_open(target, flags);
    mb_in_hook = 0;
    return result;
}

int mb_openat(int dirfd, const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }

    int (*real_openat)(int, const char *, int, ...) = dlsym(RTLD_NEXT, "openat");
    if (!real_openat) {
        errno = ENOSYS;
        return -1;
    }

    if (path && path[0] == '/') {
        return (flags & O_CREAT) ? mb_open(path, flags, mode) : mb_open(path, flags);
    }
    return (flags & O_CREAT) ? real_openat(dirfd, path, flags, mode) : real_openat(dirfd, path, flags);
}

int mb_creat(const char *path, mode_t mode) {
    return mb_open(path, O_CREAT | O_WRONLY | O_TRUNC, mode);
}

FILE *mb_fopen(const char *path, const char *mode) {
    FILE *(*real_fopen)(const char *, const char *) = dlsym(RTLD_NEXT, "fopen");
    if (!real_fopen) {
        errno = ENOSYS;
        return NULL;
    }

    if (mb_in_hook) return real_fopen(path, mode);
    bool write_mode = mode && (strchr(mode, 'w') || strchr(mode, 'a') || strchr(mode, '+'));

    mb_in_hook = 1;
    char overlay[PATH_MAX];
    char abs_path[PATH_MAX];
    const char *target = path;
    bool mapped = mb_overlay_path(path, overlay, sizeof(overlay));
    if (mapped && mb_absolute_path(path, abs_path, sizeof(abs_path))) {
        if (write_mode) {
            mb_copy_up_if_needed(abs_path, overlay);
            mb_mkdirs_for_file(overlay);
            target = overlay;
        } else if (access(overlay, F_OK) == 0) {
            target = overlay;
        }
    }
    FILE *result = real_fopen(target, mode);
    mb_in_hook = 0;
    return result;
}

int mb_stat(const char *path, struct stat *buf) {
    int (*real_stat)(const char *, struct stat *) = dlsym(RTLD_NEXT, "stat");
    if (!real_stat) {
        errno = ENOSYS;
        return -1;
    }
    if (mb_in_hook) return real_stat(path, buf);

    mb_in_hook = 1;
    char overlay[PATH_MAX];
    int result;
    if (mb_overlay_path(path, overlay, sizeof(overlay)) && access(overlay, F_OK) == 0) {
        result = real_stat(overlay, buf);
    } else {
        result = real_stat(path, buf);
    }
    mb_in_hook = 0;
    return result;
}

int mb_lstat(const char *path, struct stat *buf) {
    int (*real_lstat)(const char *, struct stat *) = dlsym(RTLD_NEXT, "lstat");
    if (!real_lstat) {
        errno = ENOSYS;
        return -1;
    }
    if (mb_in_hook) return real_lstat(path, buf);

    mb_in_hook = 1;
    char overlay[PATH_MAX];
    int result;
    if (mb_overlay_path(path, overlay, sizeof(overlay)) && access(overlay, F_OK) == 0) {
        result = real_lstat(overlay, buf);
    } else {
        result = real_lstat(path, buf);
    }
    mb_in_hook = 0;
    return result;
}

int mb_access(const char *path, int amode) {
    int (*real_access)(const char *, int) = dlsym(RTLD_NEXT, "access");
    if (!real_access) {
        errno = ENOSYS;
        return -1;
    }
    if (mb_in_hook) return real_access(path, amode);

    mb_in_hook = 1;
    char overlay[PATH_MAX];
    int result;
    if (mb_overlay_path(path, overlay, sizeof(overlay)) && real_access(overlay, F_OK) == 0) {
        result = real_access(overlay, amode);
    } else {
        result = real_access(path, amode);
    }
    mb_in_hook = 0;
    return result;
}

int mb_unlink(const char *path) {
    int (*real_unlink)(const char *) = dlsym(RTLD_NEXT, "unlink");
    if (!real_unlink) {
        errno = ENOSYS;
        return -1;
    }

    mb_in_hook = 1;
    char overlay[PATH_MAX];
    int result = -1;
    if (mb_overlay_path(path, overlay, sizeof(overlay))) {
        result = real_unlink(overlay);
        if (result != 0 && errno == ENOENT) result = 0;
    } else {
        result = real_unlink(path);
    }
    mb_in_hook = 0;
    return result;
}

int mb_mkdir(const char *path, mode_t mode) {
    int (*real_mkdir)(const char *, mode_t) = dlsym(RTLD_NEXT, "mkdir");
    if (!real_mkdir) {
        errno = ENOSYS;
        return -1;
    }
    if (mb_in_hook) return real_mkdir(path, mode);

    mb_in_hook = 1;
    char overlay[PATH_MAX];
    const char *target = path;
    if (mb_overlay_path(path, overlay, sizeof(overlay))) {
        mb_mkdirs_for_file(overlay);
        target = overlay;
    }
    int result = real_mkdir(target, mode);
    mb_in_hook = 0;
    return result;
}

int mb_rename(const char *from, const char *to) {
    int (*real_rename)(const char *, const char *) = dlsym(RTLD_NEXT, "rename");
    if (!real_rename) {
        errno = ENOSYS;
        return -1;
    }

    mb_in_hook = 1;
    char from_overlay[PATH_MAX];
    char to_overlay[PATH_MAX];
    char from_abs[PATH_MAX];
    const char *from_target = from;
    const char *to_target = to;
    if (mb_overlay_path(from, from_overlay, sizeof(from_overlay)) && mb_absolute_path(from, from_abs, sizeof(from_abs))) {
        if (access(from_overlay, F_OK) != 0) mb_copy_up_if_needed(from_abs, from_overlay);
        from_target = from_overlay;
    }
    if (mb_overlay_path(to, to_overlay, sizeof(to_overlay))) {
        mb_mkdirs_for_file(to_overlay);
        to_target = to_overlay;
    }
    int result = real_rename(from_target, to_target);
    mb_in_hook = 0;
    return result;
}

#define DYLD_INTERPOSE(_replacement, _replacee) \
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } _interpose_##_replacee \
    __attribute__((section("__DATA,__interpose"))) = { (const void *)(unsigned long)&_replacement, (const void *)(unsigned long)&_replacee };

DYLD_INTERPOSE(mb_open, open)
DYLD_INTERPOSE(mb_openat, openat)
DYLD_INTERPOSE(mb_creat, creat)
DYLD_INTERPOSE(mb_fopen, fopen)
DYLD_INTERPOSE(mb_stat, stat)
DYLD_INTERPOSE(mb_lstat, lstat)
DYLD_INTERPOSE(mb_access, access)
DYLD_INTERPOSE(mb_unlink, unlink)
DYLD_INTERPOSE(mb_mkdir, mkdir)
DYLD_INTERPOSE(mb_rename, rename)
