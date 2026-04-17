/*
 * merge_results.c  (OpenMP version)
 *
 * Multi-threaded CSV merger for simulation results.
 *
 * Usage:
 *   ./merge_results <output.csv> <input1.csv> [input2.csv ...]
 *
 * Design:
 *   - OpenMP parallel for reads all input files concurrently
 *   - Each file's data row lands in rows[i] (one row per sim run)
 *   - Single sequential pass writes header + all rows to output
 *   - No ring buffer, no manual mutexes — OpenMP handles it
 *
 * Compile:
 *   gcc -O2 -fopenmp merge_results.c -o merge_results
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <omp.h>

#define MAX_LINE  4096
#define MAX_FILES 4096

/* One data row per input file */
static char rows[MAX_FILES][MAX_LINE];
static int  row_ok[MAX_FILES];          /* 1 if file was read successfully */
static char header[MAX_LINE] = {0};     /* shared header, claimed once     */

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr,
            "Usage: %s <output.csv> <input1.csv> [input2.csv ...]\n",
            argv[0]);
        return 1;
    }

    const char  *out_path = argv[1];
    int          n        = argc - 2;
    char       **in_paths = argv + 2;

    if (n > MAX_FILES) {
        fprintf(stderr, "[merge] Too many files (max %d)\n", MAX_FILES);
        return 1;
    }

    printf("[merge] Output : %s\n", out_path);
    printf("[merge] Inputs : %d file(s)\n", n);

    memset(row_ok, 0, sizeof(int) * n);

    /* ----------------------------------------------------------------
     * Parallel read — each thread handles one file
     * ---------------------------------------------------------------- */
    #pragma omp parallel for schedule(dynamic)
    for (int i = 0; i < n; i++) {
        FILE *f = fopen(in_paths[i], "r");
        if (!f) {
            fprintf(stderr, "[merge] Cannot open '%s'\n", in_paths[i]);
            continue;
        }

        char  line[MAX_LINE];
        int   first = 1;

        while (fgets(line, sizeof(line), f)) {
            /* Strip trailing newline / carriage-return */
            size_t len = strlen(line);
            while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r'))
                line[--len] = '\0';
            if (len == 0)
                continue;

            if (first) {
                first = 0;
                /* Claim header — only the first thread to arrive writes it */
                #pragma omp critical
                {
                    if (!header[0])
                        strncpy(header, line, MAX_LINE - 1);
                }
                continue;
            }

            /* Data row — store at index i (no contention, each i is unique) */
            strncpy(rows[i], line, MAX_LINE - 1);
            rows[i][MAX_LINE - 1] = '\0';
            row_ok[i] = 1;
            break;   /* one data row per sim-run CSV */
        }

        fclose(f);
    }

    /* ----------------------------------------------------------------
     * Sequential write
     * ---------------------------------------------------------------- */
    FILE *out = fopen(out_path, "w");
    if (!out) {
        perror("[merge] Cannot open output");
        return 1;
    }

    if (!header[0]) {
        fprintf(stderr, "[merge] No header found — are the input CSVs empty?\n");
        fclose(out);
        return 1;
    }

    fprintf(out, "%s\n", header);

    int written = 0, errors = 0;
    for (int i = 0; i < n; i++) {
        if (row_ok[i]) {
            fprintf(out, "%s\n", rows[i]);
            written++;
        } else {
            errors++;
        }
    }

    fflush(out);
    fclose(out);

    printf("[merge] Rows written : %d\n", written);
    if (errors)
        printf("[merge] WARNING: %d file(s) had no data row\n", errors);
    printf("[merge] Done -> %s\n", out_path);

    return errors ? 1 : 0;
}