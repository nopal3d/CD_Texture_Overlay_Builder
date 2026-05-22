/*
 * cd_hashlittle_native.c
 * Fast native C helper for Crimson Desert Texture Overlay Builder.
 *
 * Computes Bob Jenkins hashlittle over a file, matching the Python reference implementation.
 * Output:
 *   PROGRESS <doneBytes> <totalBytes>
 *   HASH <uint32>
 *
 * Build examples:
 *   cl /O2 /MT /nologo cd_hashlittle_native.c /Fe:cd_hashlittle_native.exe
 *   gcc -O3 -static -s cd_hashlittle_native.c -o cd_hashlittle_native.exe
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>

#if defined(_WIN32)
  #include <sys/stat.h>
  #define STAT_STRUCT struct _stat64
  #define STAT_FUNC _stat64
#else
  #include <sys/stat.h>
  #define STAT_STRUCT struct stat
  #define STAT_FUNC stat
#endif

#define BUFFER_SIZE (16u * 1024u * 1024u)
#define REPORT_STEP (256ull * 1024ull * 1024ull)
#define DEFAULT_SEED 0xC5EDEu

static uint32_t rot32(uint32_t x, unsigned k) {
    return (uint32_t)((x << k) | (x >> (32u - k)));
}

static uint32_t read_u32_le(const unsigned char *p) {
    return ((uint32_t)p[0]) | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void mix(uint32_t *a, uint32_t *b, uint32_t *c) {
    *a -= *c; *a ^= rot32(*c, 4);  *c += *b;
    *b -= *a; *b ^= rot32(*a, 6);  *a += *c;
    *c -= *b; *c ^= rot32(*b, 8);  *b += *a;
    *a -= *c; *a ^= rot32(*c, 16); *c += *b;
    *b -= *a; *b ^= rot32(*a, 19); *a += *c;
    *c -= *b; *c ^= rot32(*b, 4);  *b += *a;
}

static void final_mix(uint32_t *a, uint32_t *b, uint32_t *c) {
    *c ^= *b; *c -= rot32(*b, 14);
    *a ^= *c; *a -= rot32(*c, 11);
    *b ^= *a; *b -= rot32(*a, 25);
    *c ^= *b; *c -= rot32(*b, 16);
    *a ^= *c; *a -= rot32(*c, 4);
    *b ^= *a; *b -= rot32(*a, 14);
    *c ^= *b; *c -= rot32(*b, 24);
}

static int get_file_size_u64(const char *path, uint64_t *out_size) {
    STAT_STRUCT st;
    if (STAT_FUNC(path, &st) != 0) {
        return -1;
    }
    if (st.st_size < 0) {
        return -1;
    }
    *out_size = (uint64_t)st.st_size;
    return 0;
}

static int hashlittle_file(const char *path, uint32_t initval, uint32_t *out_hash) {
    uint64_t total_len = 0;
    if (get_file_size_u64(path, &total_len) != 0) {
        fprintf(stderr, "stat failed for '%s': %s\n", path, strerror(errno));
        return 1;
    }

    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "open failed for '%s': %s\n", path, strerror(errno));
        return 1;
    }

    unsigned char *buf = (unsigned char *)malloc(BUFFER_SIZE);
    if (!buf) {
        fclose(f);
        fprintf(stderr, "out of memory allocating buffer\n");
        return 1;
    }

    uint32_t a = (uint32_t)(0xDEADBEEFu + (uint32_t)total_len + initval);
    uint32_t b = a;
    uint32_t c = a;

    unsigned char tail[12];
    size_t tail_len = 0;
    uint64_t processed = 0;
    uint64_t last_report = 0;

    while (1) {
        size_t n = fread(buf, 1, BUFFER_SIZE, f);
        if (n == 0) {
            if (ferror(f)) {
                fprintf(stderr, "read failed for '%s'\n", path);
                free(buf);
                fclose(f);
                return 1;
            }
            break;
        }

        size_t off = 0;

        if (tail_len > 0) {
            size_t need = 12 - tail_len;
            size_t take = (n < need) ? n : need;
            memcpy(tail + tail_len, buf, take);
            tail_len += take;
            off += take;

            if (tail_len == 12 && processed + 12 < total_len) {
                a += read_u32_le(tail);
                b += read_u32_le(tail + 4);
                c += read_u32_le(tail + 8);
                mix(&a, &b, &c);
                processed += 12;
                tail_len = 0;
            }
        }

        while (off + 12 <= n && processed + 12 < total_len) {
            a += read_u32_le(buf + off);
            b += read_u32_le(buf + off + 4);
            c += read_u32_le(buf + off + 8);
            mix(&a, &b, &c);
            off += 12;
            processed += 12;

            if (processed - last_report >= REPORT_STEP) {
                last_report = processed;
                printf("PROGRESS %llu %llu\n", (unsigned long long)processed, (unsigned long long)total_len);
                fflush(stdout);
            }
        }

        if (off < n) {
            tail_len = n - off;
            if (tail_len > 12) {
                fprintf(stderr, "internal tail error: %zu\n", tail_len);
                free(buf);
                fclose(f);
                return 1;
            }
            memcpy(tail, buf + off, tail_len);
        }
    }

    free(buf);
    fclose(f);

    if (tail_len > 0) {
        if (tail_len >= 1) a += tail[0];
        if (tail_len >= 2) a += ((uint32_t)tail[1]) << 8;
        if (tail_len >= 3) a += ((uint32_t)tail[2]) << 16;
        if (tail_len >= 4) a += ((uint32_t)tail[3]) << 24;
        if (tail_len >= 5) b += tail[4];
        if (tail_len >= 6) b += ((uint32_t)tail[5]) << 8;
        if (tail_len >= 7) b += ((uint32_t)tail[6]) << 16;
        if (tail_len >= 8) b += ((uint32_t)tail[7]) << 24;
        if (tail_len >= 9) c += tail[8];
        if (tail_len >= 10) c += ((uint32_t)tail[9]) << 8;
        if (tail_len >= 11) c += ((uint32_t)tail[10]) << 16;
        if (tail_len >= 12) c += ((uint32_t)tail[11]) << 24;
        final_mix(&a, &b, &c);
    }

    printf("PROGRESS %llu %llu\n", (unsigned long long)total_len, (unsigned long long)total_len);
    fflush(stdout);
    *out_hash = c;
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: cd_hashlittle_native.exe <file> [seed]\n");
        return 2;
    }
    const char *path = argv[1];
    uint32_t seed = DEFAULT_SEED;
    if (argc >= 3) {
        seed = (uint32_t)strtoul(argv[2], NULL, 0);
    }

    uint32_t h = 0;
    int rc = hashlittle_file(path, seed, &h);
    if (rc != 0) return rc;
    printf("HASH %u\n", h);
    return 0;
}
