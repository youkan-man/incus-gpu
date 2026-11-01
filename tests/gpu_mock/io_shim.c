/* Test-only syscall boundary for a temporary sysfs tree.
 * No production function is replaced. All non-mock file I/O is forwarded.
 * The independent mock daemon owns driver links, override values, faults and
 * delayed completion, including completion after the writing client is killed.
 */
#define _GNU_SOURCE
#include <sys/syscall.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/types.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <limits.h>

static int eligible(const char *p) {
    const char *root = getenv("GPU_MOCK_SYSFS");
    return root && p && strncmp(p, root, strlen(root)) == 0 && p[strlen(root)] == '/';
}
static int fdpath(int fd, char *out) {
    char link[64];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = syscall(SYS_readlinkat, AT_FDCWD, link, out, PATH_MAX - 1);
    if (n < 0) return 0;
    out[n] = 0;
    return eligible(out);
}
static int sendall(int fd, const void *buf, size_t size) {
    size_t pos = 0;
    while (pos < size) {
        ssize_t n = syscall(SYS_sendto, fd, (const char *)buf + pos, size - pos, MSG_NOSIGNAL, NULL, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        pos += (size_t)n;
    }
    return 0;
}
static int rpc(const char *op, const char *path, const void *buf, size_t size) {
    const char *socket_path = getenv("GPU_MOCK_SOCKET");
    if (!socket_path) { errno = ECONNREFUSED; return -1; }
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    if (strlen(socket_path) >= sizeof(addr.sun_path)) { errno = ENAMETOOLONG; return -1; }
    strcpy(addr.sun_path, socket_path);
    int s = (int)syscall(SYS_socket, AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (s < 0) return -1;
    char header[PATH_MAX + 128];
    int n = snprintf(header, sizeof(header), "%s\t%s\t%zu\t%d\n", op, path, size, getpid());
    if (syscall(SYS_connect, s, &addr, sizeof(addr)) < 0 ||
        sendall(s, header, (size_t)n) < 0 || (size && sendall(s, buf, size) < 0)) {
        int saved = errno; syscall(SYS_close, s); errno = saved; return -1;
    }
    char response[80]; size_t pos = 0;
    while (pos < sizeof(response) - 1) {
        ssize_t got = syscall(SYS_read, s, response + pos, 1);
        if (got < 0 && errno == EINTR) continue;
        if (got <= 0) break;
        if (response[pos++] == '\n') break;
    }
    response[pos] = 0; syscall(SYS_close, s);
    if (!pos) { errno = EIO; return -1; }
    int error = atoi(response);
    if (error) { errno = error; return -1; }
    return 0;
}
static int open_impl(int d, const char *p, int flags, mode_t mode) {
    /* sysfs attributes are not truncated when opened. Ordinary fixture files
     * must not lose the old value before an injected failure is returned. */
    if (eligible(p)) flags &= ~O_TRUNC;
    return (int)syscall(SYS_openat, d, p, flags, mode);
}
#define OPEN_VARIANT(name) \
int name(const char *p, int flags, ...) { \
    mode_t mode = 0; \
    if ((flags & O_CREAT) || (flags & O_TMPFILE) == O_TMPFILE) { \
        va_list a; va_start(a, flags); mode = (mode_t)va_arg(a, int); va_end(a); \
    } \
    return open_impl(AT_FDCWD, p, flags, mode); \
}
OPEN_VARIANT(open)
OPEN_VARIANT(open64)
int __open_2(const char *p, int flags) { return open_impl(AT_FDCWD, p, flags, 0); }
int __open64_2(const char *p, int flags) { return open_impl(AT_FDCWD, p, flags, 0); }
#define OPENAT_VARIANT(name) \
int name(int d, const char *p, int flags, ...) { \
    mode_t mode = 0; \
    if ((flags & O_CREAT) || (flags & O_TMPFILE) == O_TMPFILE) { \
        va_list a; va_start(a, flags); mode = (mode_t)va_arg(a, int); va_end(a); \
    } \
    return open_impl(d, p, flags, mode); \
}
OPENAT_VARIANT(openat)
OPENAT_VARIANT(openat64)
ssize_t read(int fd, void *buf, size_t count) {
    char p[PATH_MAX];
    if (count && fdpath(fd, p) && syscall(SYS_lseek, fd, 0, SEEK_CUR) == 0) {
        if (rpc("read", p, NULL, 0) < 0) return -1;
    }
    return syscall(SYS_read, fd, buf, count);
}
ssize_t write(int fd, const void *buf, size_t count) {
    char p[PATH_MAX];
    if (fdpath(fd, p)) {
        if (count > 8192) { errno = E2BIG; return -1; }
        if (rpc("write", p, buf, count) < 0) return -1;
        syscall(SYS_lseek, fd, (off_t)count, SEEK_CUR);
        return (ssize_t)count;
    }
    return syscall(SYS_write, fd, buf, count);
}
