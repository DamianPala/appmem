#include <libproc.h>
#include <mach/mach.h>
#include <mach/vm_statistics.h>
#include <stdint.h>
#include <stddef.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/proc_info.h>
#include <sys/resource.h>
#include <sys/mman.h>
#include <sys/sysctl.h>
#include <unistd.h>
#include <sys/wait.h>

_Static_assert(sizeof(vm_size_t) == sizeof(size_t), "host_page_size output must be pointer-width");
_Static_assert(sizeof(vm_size_t) == 8, "native probe requires 64-bit vm_size_t");

static int read_line(int fd, char *line, size_t capacity) {
    size_t length = 0;
    while (length + 1 < capacity) {
        char byte;
        if (read(fd, &byte, 1) != 1) return -1;
        if (byte == '\n') {
            line[length] = '\0';
            return 0;
        }
        line[length++] = byte;
    }
    return -1;
}

static int proxy_child(const char *path) {
    int input[2], output[2];
    if (pipe(input)) return 10;
    if (pipe(output)) {
        close(input[0]); close(input[1]);
        return 10;
    }
    pid_t child = fork();
    if (child < 0) {
        close(input[0]); close(input[1]); close(output[0]); close(output[1]);
        return 11;
    }
    if (child == 0) {
        if (dup2(input[0], STDIN_FILENO) < 0 || dup2(output[1], STDOUT_FILENO) < 0)
            _exit(12);
        close(input[0]); close(input[1]); close(output[0]); close(output[1]);
        char *const args[] = {(char *)path, "workload", NULL};
        execv(path, args);
        _exit(13);
    }
    close(input[0]);
    close(output[1]);
    setvbuf(stdout, NULL, _IONBF, 0);
    char line[32];
    int result = 14;
    if (read_line(output[0], line, sizeof(line)) || strcmp(line, "ready")) goto done;
    printf("ready %d\n", child);
    const char commands[] = {'a', 'f', 'q'};
    const char *responses[] = {"allocated", "freed"};
    for (size_t index = 0; index < 3; index++) {
        char command;
        if (read(STDIN_FILENO, &command, 1) != 1 || command != commands[index]) goto done;
        if (write(input[1], &command, 1) != 1) goto done;
        if (index < 2) {
            if (read_line(output[0], line, sizeof(line)) || strcmp(line, responses[index]))
                goto done;
            puts(line);
        }
    }
    result = 0;
done:
    close(input[1]);
    close(output[0]);
    if (result) kill(child, SIGKILL);
    int status;
    if (waitpid(child, &status, 0) != child) return 15;
    return result ? result : (!WIFEXITED(status) || WEXITSTATUS(status) ? 16 : 0);
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "abi") == 0) {
        printf("%zu %zu %zu %zu %zu %zu %zu %zu\n",
               sizeof(struct proc_bsdshortinfo), sizeof(struct rusage_info_v4),
               offsetof(struct rusage_info_v4, ri_phys_footprint),
               sizeof(struct vm_statistics64),
               offsetof(struct vm_statistics64, total_uncompressed_pages_in_compressor),
               sizeof(struct xsw_usage),
               offsetof(struct vm_statistics64, swapins),
               offsetof(struct vm_statistics64, swapouts));
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "parent") == 0) return proxy_child(argv[2]);
    if (argc != 2 || strcmp(argv[1], "workload") != 0) return 2;
    const size_t length = 128 * 1024 * 1024;
    unsigned char *bytes = mmap(NULL, length, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANON, -1, 0);
    if (bytes == MAP_FAILED) return 3;
    char command;
    setvbuf(stdout, NULL, _IONBF, 0);
    puts("ready");
    if (read(STDIN_FILENO, &command, 1) != 1 || command != 'a') return 4;
    volatile unsigned char *touched = bytes;
    for (size_t i = 0; i < length; i += 4096) touched[i] = 1;
    puts("allocated");
    if (read(STDIN_FILENO, &command, 1) != 1 || command != 'f') return 5;
    if (munmap(bytes, length)) return 7;
    puts("freed");
    if (read(STDIN_FILENO, &command, 1) != 1 || command != 'q') return 6;
    return 0;
}
