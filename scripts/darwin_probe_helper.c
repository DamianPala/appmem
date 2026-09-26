#include <libproc.h>
#include <mach/mach.h>
#include <mach/vm_statistics.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/proc_info.h>
#include <sys/resource.h>
#include <sys/mman.h>
#include <sys/sysctl.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "abi") == 0) {
        printf("%zu %zu %zu %zu %zu %zu %zu\n",
               sizeof(struct proc_bsdshortinfo), sizeof(struct rusage_info_v4),
               offsetof(struct rusage_info_v4, ri_phys_footprint),
               sizeof(struct vm_statistics64),
               offsetof(struct vm_statistics64, total_uncompressed_pages_in_compressor),
               offsetof(struct vm_statistics64, swapped_count), sizeof(struct xsw_usage));
        return 0;
    }
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
