/*
 * aggregate_results.c
 *
 * Aggregates a merged network_results_<env>.csv (one row per (config, seed)
 * run, as produced by merge_results + Parallel_processes/network_parallel.py's
 * REPEATS/seed sweep) into one row per config, averaged over its seed
 * repeats: mean / sample std / 95%% CI half-width per metric.
 *
 * Unlike merge_results.c, this reads a single already-merged file rather
 * than thousands of individual per-run files, and the aggregation itself
 * is an in-memory reduction over at most tens of thousands of rows — that
 * finishes in milliseconds single-threaded, so there's no OpenMP
 * dependency here (keeps this buildable with a plain compiler, no libomp
 * required).
 *
 * Usage:
 *   ./aggregate_results <input_merged.csv> <output_agg.csv>
 *
 * Compile:
 *   gcc -O2 aggregate_results.c -o aggregate_results -lm
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#define MAX_LINE        8192
#define MAX_COLS        64
#define GROUP_VAL_LEN   64
#define KEY_LEN         1024

/* Fields that identify a distinct config (grid point) — everything except
 * "seed" and the per-run metrics being aggregated. Must match
 * simulation.py's PAPER_CSV_HEADER field names exactly. */
static const char *GROUP_FIELDS[] = {
    "currency", "nodes", "neighbors", "wallets", "miners", "transactions", "interval",
    "shards", "block size", "mode", "blocktime in configuration file",
    "sig_scheme", "broadcast_protocol", "shard_comm_protocol", "verify_mode",
    "conflict_check",
};
#define NUM_GROUP_FIELDS ((int)(sizeof(GROUP_FIELDS) / sizeof(GROUP_FIELDS[0])))

/* Per-run metrics to average over seed repeats. */
static const char *METRIC_FIELDS[] = {
    "tps", "average block time", "messages", "broadcast_cpu_seconds",
    "hop_p50", "hop_p90", "hop_p99", "hop_max",
};
#define NUM_METRIC_FIELDS ((int)(sizeof(METRIC_FIELDS) / sizeof(METRIC_FIELDS[0])))

/* Student's t 97.5th percentile by degrees of freedom (n_seeds - 1). Avoids
 * a stats-library dependency for a handful of small-sample CIs; falls back
 * to the nearest tabulated df at or below the true one (converges to the
 * n->inf normal approximation, 1.96, for large samples). */
typedef struct { int df; double t; } TEntry;
static const TEntry T_975[] = {
    {1,12.706},{2,4.303},{3,3.182},{4,2.776},{5,2.571},{6,2.447},{7,2.365},
    {8,2.306},{9,2.262},{10,2.228},{11,2.201},{12,2.179},{13,2.160},{14,2.145},
    {15,2.131},{16,2.120},{17,2.110},{18,2.101},{19,2.093},{20,2.086},
    {21,2.080},{22,2.074},{23,2.069},{24,2.064},{25,2.060},{26,2.056},
    {27,2.052},{28,2.048},{29,2.045},{30,2.042},{40,2.021},{60,2.000},
    {120,1.980},
};
#define T_TABLE_LEN ((int)(sizeof(T_975) / sizeof(T_975[0])))

static double t_critical(int df) {
    if (df <= 0) return NAN;
    double nearest_below = -1.0;
    for (int i = 0; i < T_TABLE_LEN; i++) {
        if (T_975[i].df == df) return T_975[i].t;
        if (T_975[i].df < df) nearest_below = T_975[i].t;
    }
    return nearest_below >= 0.0 ? nearest_below : 1.96;
}

/*
 * split_csv_line
 * Splits a NUL-terminated, already newline-stripped CSV line in place
 * (commas/quotes overwritten with '\0'), filling `fields` with pointers
 * into `line`. Handles doubled-quote escaping inside quoted fields.
 * None of this project's fields ever contain embedded commas, but this
 * keeps the parser correct if that ever changes.
 */
static int split_csv_line(char *line, char *fields[], int max_fields) {
    int n = 0;
    char *p = line;
    while (n < max_fields) {
        if (*p == '"') {
            p++;
            char *start = p;
            char *w = p;
            while (*p) {
                if (*p == '"') {
                    if (*(p + 1) == '"') { *w++ = '"'; p += 2; continue; }
                    break;
                }
                *w++ = *p++;
            }
            *w = '\0';
            if (*p == '"') p++;
            fields[n++] = start;
            if (*p == ',') { p++; continue; }
            break;
        } else {
            char *start = p;
            while (*p && *p != ',') p++;
            int at_end = (*p == '\0');
            if (!at_end) *p++ = '\0';
            fields[n++] = start;
            if (at_end) break;
        }
    }
    return n;
}

typedef struct {
    char   group_vals[NUM_GROUP_FIELDS][GROUP_VAL_LEN];
    double val[NUM_METRIC_FIELDS];
    int    present[NUM_METRIC_FIELDS];
} ParsedRow;

typedef struct GroupNode {
    char   key[KEY_LEN];
    char   group_vals[NUM_GROUP_FIELDS][GROUP_VAL_LEN];
    long   n_seeds;
    double sum[NUM_METRIC_FIELDS];
    double sumsq[NUM_METRIC_FIELDS];
    long   count[NUM_METRIC_FIELDS];
    struct GroupNode *next;
} GroupNode;

static unsigned long fnv1a(const char *s) {
    unsigned long h = 1469598103934665603UL;
    while (*s) {
        h ^= (unsigned char)(*s++);
        h *= 1099511628211UL;
    }
    return h;
}

int main(int argc, char *argv[]) {
    if (argc != 3) {
        fprintf(stderr, "Usage: %s <input_merged.csv> <output_agg.csv>\n", argv[0]);
        return 1;
    }
    const char *in_path  = argv[1];
    const char *out_path = argv[2];

    FILE *f = fopen(in_path, "rb");
    if (!f) { perror("[aggregate] Cannot open input"); return 1; }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc((size_t)size + 1);
    if (!buf) { fprintf(stderr, "[aggregate] Out of memory reading input\n"); fclose(f); return 1; }
    size_t read_n = fread(buf, 1, (size_t)size, f);
    fclose(f);
    buf[read_n] = '\0';

    /* Split buffer into lines in place, dropping blank lines. */
    size_t line_cap = 4096;
    char **lines = malloc(line_cap * sizeof(char *));
    size_t nlines = 0;
    char *p = buf;
    while (*p) {
        char *start = p;
        while (*p && *p != '\n') p++;
        int had_nl = (*p == '\n');
        char *end = p;
        if (end > start && *(end - 1) == '\r') end--;
        *end = '\0';
        if (had_nl) p++;
        if (start[0] != '\0') {
            if (nlines == line_cap) {
                line_cap *= 2;
                lines = realloc(lines, line_cap * sizeof(char *));
            }
            lines[nlines++] = start;
        }
    }

    if (nlines < 2) {
        fprintf(stderr, "[aggregate] No data rows found in '%s'\n", in_path);
        return 1;
    }

    /* Header -> column index lookup. */
    char *hdr_fields[MAX_COLS];
    int ncols = split_csv_line(lines[0], hdr_fields, MAX_COLS);

    int group_idx[NUM_GROUP_FIELDS];
    int active_group[NUM_GROUP_FIELDS];
    int n_active_group = 0;
    for (int g = 0; g < NUM_GROUP_FIELDS; g++) {
        group_idx[g] = -1;
        for (int c = 0; c < ncols; c++) {
            if (strcmp(hdr_fields[c], GROUP_FIELDS[g]) == 0) { group_idx[g] = c; break; }
        }
        active_group[g] = (group_idx[g] >= 0);
        if (active_group[g]) n_active_group++;
    }
    if (n_active_group == 0) {
        fprintf(stderr, "[aggregate] None of the expected group-by fields were found "
                        "in the input header.\n");
        return 1;
    }

    int metric_idx[NUM_METRIC_FIELDS];
    int active_metric[NUM_METRIC_FIELDS];
    for (int m = 0; m < NUM_METRIC_FIELDS; m++) {
        metric_idx[m] = -1;
        for (int c = 0; c < ncols; c++) {
            if (strcmp(hdr_fields[c], METRIC_FIELDS[m]) == 0) { metric_idx[m] = c; break; }
        }
        active_metric[m] = (metric_idx[m] >= 0);
    }

    long ndata = (long)nlines - 1;
    ParsedRow *rows = malloc((size_t)ndata * sizeof(ParsedRow));

    /* Parse phase: each data row split and its group/metric fields
     * extracted independently of every other row. */
    for (long i = 0; i < ndata; i++) {
        char *fields[MAX_COLS];
        int nf = split_csv_line(lines[1 + i], fields, MAX_COLS);

        for (int g = 0; g < NUM_GROUP_FIELDS; g++) {
            rows[i].group_vals[g][0] = '\0';
            if (active_group[g] && group_idx[g] < nf) {
                strncpy(rows[i].group_vals[g], fields[group_idx[g]], GROUP_VAL_LEN - 1);
                rows[i].group_vals[g][GROUP_VAL_LEN - 1] = '\0';
            }
        }
        for (int m = 0; m < NUM_METRIC_FIELDS; m++) {
            rows[i].present[m] = 0;
            if (active_metric[m] && metric_idx[m] < nf) {
                const char *s = fields[metric_idx[m]];
                if (s[0] != '\0') {
                    char *endp;
                    double v = strtod(s, &endp);
                    if (endp != s) { rows[i].val[m] = v; rows[i].present[m] = 1; }
                }
            }
        }
    }

    /* Grouping phase (serial reduction): hash each row's group key into a
     * chained hash table, accumulating sum/sumsq/count per metric. Group
     * insertion order is preserved via group_list for deterministic,
     * first-seen-order output. */
    size_t nbuckets = 64;
    while (nbuckets < (size_t)ndata * 2) nbuckets <<= 1;
    GroupNode **buckets = calloc(nbuckets, sizeof(GroupNode *));
    GroupNode **group_list = malloc((size_t)ndata * sizeof(GroupNode *));
    long ngroups = 0;

    for (long i = 0; i < ndata; i++) {
        char key[KEY_LEN];
        size_t klen = 0;
        key[0] = '\0';
        for (int g = 0; g < NUM_GROUP_FIELDS; g++) {
            if (!active_group[g]) continue;
            int n = snprintf(key + klen, KEY_LEN - klen, "%s\x1f", rows[i].group_vals[g]);
            if (n > 0) klen += (size_t)n;
            if (klen >= KEY_LEN) break;
        }

        unsigned long h = fnv1a(key) & (nbuckets - 1);
        GroupNode *node = buckets[h];
        while (node && strcmp(node->key, key) != 0) node = node->next;

        if (!node) {
            node = calloc(1, sizeof(GroupNode));
            strncpy(node->key, key, KEY_LEN - 1);
            for (int g = 0; g < NUM_GROUP_FIELDS; g++) {
                strncpy(node->group_vals[g], rows[i].group_vals[g], GROUP_VAL_LEN - 1);
            }
            node->next = buckets[h];
            buckets[h] = node;
            group_list[ngroups++] = node;
        }

        node->n_seeds++;
        for (int m = 0; m < NUM_METRIC_FIELDS; m++) {
            if (!rows[i].present[m]) continue;
            double v = rows[i].val[m];
            node->sum[m]   += v;
            node->sumsq[m] += v * v;
            node->count[m]++;
        }
    }

    /* Write output. */
    FILE *out = fopen(out_path, "w");
    if (!out) { perror("[aggregate] Cannot open output"); return 1; }

    for (int g = 0; g < NUM_GROUP_FIELDS; g++) {
        if (!active_group[g]) continue;
        fprintf(out, "%s,", GROUP_FIELDS[g]);
    }
    fprintf(out, "n_seeds");
    for (int m = 0; m < NUM_METRIC_FIELDS; m++) {
        if (!active_metric[m]) continue;
        fprintf(out, ",%s_mean,%s_std,%s_ci95_halfwidth,%s_n",
                METRIC_FIELDS[m], METRIC_FIELDS[m], METRIC_FIELDS[m], METRIC_FIELDS[m]);
    }
    fprintf(out, "\n");

    for (long i = 0; i < ngroups; i++) {
        GroupNode *node = group_list[i];
        for (int g = 0; g < NUM_GROUP_FIELDS; g++) {
            if (!active_group[g]) continue;
            fprintf(out, "%s,", node->group_vals[g]);
        }
        fprintf(out, "%ld", node->n_seeds);
        for (int m = 0; m < NUM_METRIC_FIELDS; m++) {
            if (!active_metric[m]) continue;
            long n = node->count[m];
            if (n == 0) {
                fprintf(out, ",,,,0");
                continue;
            }
            double mean = node->sum[m] / (double)n;
            double std  = 0.0, hw = 0.0;
            if (n > 1) {
                double var = (node->sumsq[m] - node->sum[m] * node->sum[m] / (double)n) / (double)(n - 1);
                std = var > 0.0 ? sqrt(var) : 0.0;
                hw  = t_critical((int)(n - 1)) * std / sqrt((double)n);
            }
            fprintf(out, ",%.6f,%.6f,%.6f,%ld", mean, std, hw, n);
        }
        fprintf(out, "\n");
    }

    fflush(out);
    fclose(out);

    printf("[aggregate] %ld per-run rows -> %ld config groups\n", ndata, ngroups);
    printf("[aggregate] Wrote -> %s\n", out_path);

    return 0;
}
