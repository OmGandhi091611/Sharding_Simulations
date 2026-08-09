/*
 * conflict_check_benchmark.c
 *
 * Benchmarks five ways of detecting cross-shard duplicate transactions
 * (the same source address submitted to two different shards):
 *
 *   1. hash_set_check          - O(n) open-addressing hash table
 *   2. sort_then_scan_check    - O(n log n) qsort (strcmp) + linear scan
 *   3. radix_sort_check        - O(n * L) LSD byte-wise radix sort (L =
 *                                 fixed 40-char address length) + linear
 *                                 scan; exploits the fixed-width keys that
 *                                 qsort's generic comparator can't, so
 *                                 it's effectively linear in n rather than
 *                                 n log n
 *   4. hash_set_check_parallel - OpenMP version of (1). Uses chaining
 *                                 instead of open addressing, splits the
 *                                 bucket range into one disjoint contiguous
 *                                 chunk per thread, and has every thread
 *                                 scan the full input but only act on
 *                                 transactions that hash into its own
 *                                 chunk - no bucket is ever touched by two
 *                                 threads, so no locks are needed.
 *   5. sort_then_scan_check_parallel - OpenMP version of (2). Splits the
 *                                 array into one chunk per thread, sorts
 *                                 each chunk independently in parallel
 *                                 (disjoint memory, no synchronization),
 *                                 then does a serial k-way merge of the
 *                                 sorted chunks before the same scan.
 *
 * For each n in BLOCK_SIZES (matching Parallel_processes/network_parallel.py)
 * a synthetic transaction set is generated once, then each algorithm is run
 * REPEATS times and timed with clock_gettime(CLOCK_MONOTONIC, ...) (wall
 * clock, immune to the CPU-time oddities of clock() under threading, which
 * matters now that two of these five algorithms actually thread).
 *
 * Usage:
 *   ./conflict_check_benchmark [--repeats N] [--dup-rate F] [--threads N]
 *
 * Compile:
 *   gcc -O2 -fopenmp -o conflict_check_benchmark conflict_check_benchmark.c -lm
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <omp.h>

#define ADDR_LEN 41  /* 40 hex chars + NUL, matches a 32-byte hash in hex */
#define NUM_SHARDS 256
#define DEFAULT_REPEATS 10
#define DEFAULT_DUP_RATE 0.05

typedef struct {
    char source_address[ADDR_LEN];
    int  shard;
} Transaction;

static const int BLOCK_SIZES[] = {
    1024, 2048, 4096, 8192, 16384, 32768,
    65536, 131072, 262144, 524288,
};
#define NUM_BLOCK_SIZES ((int)(sizeof(BLOCK_SIZES) / sizeof(BLOCK_SIZES[0])))

/* ----------------------------------------------------------------------
 * Self-contained xorshift32 PRNG - srand/rand's output isn't guaranteed
 * identical across libc implementations, and reproducibility across
 * platforms matters for the paper's results.
 * ---------------------------------------------------------------------- */
typedef struct { unsigned int state; } Xorshift32;

static void xorshift32_seed(Xorshift32 *rng, unsigned int seed) {
    /* xorshift32 is undefined at state 0 */
    rng->state = seed ? seed : 0xa5a5a5a5u;
}

static unsigned int xorshift32_next(Xorshift32 *rng) {
    unsigned int x = rng->state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    rng->state = x;
    return x;
}

/* Uniform integer in [0, bound) */
static unsigned int xorshift32_bounded(Xorshift32 *rng, unsigned int bound) {
    return xorshift32_next(rng) % bound;
}

/*
 * generate_transactions
 * Fills `out` (caller-allocated, at least n + floor(n*dup_rate) entries)
 * with n unique synthetic transactions plus floor(n*dup_rate) injected
 * cross-shard duplicates, then Fisher-Yates shuffles the whole array so
 * duplicates aren't trivially adjacent. Returns the total transaction
 * count written (n + num_dups).
 */
static int generate_transactions(int n, double dup_rate, unsigned int seed,
                                  Transaction *out)
{
    Xorshift32 rng;
    xorshift32_seed(&rng, seed);

    for (int i = 0; i < n; i++) {
        snprintf(out[i].source_address, ADDR_LEN, "addr_%08x", i);
        /* pad remaining hex chars deterministically so the address is a
         * full 40 hex chars, not just the 13-char "addr_%08x" prefix */
        for (int j = 13; j < ADDR_LEN - 1; j++) {
            static const char hex[] = "0123456789abcdef";
            out[i].source_address[j] = hex[xorshift32_bounded(&rng, 16)];
        }
        out[i].source_address[ADDR_LEN - 1] = '\0';
        out[i].shard = (int) xorshift32_bounded(&rng, NUM_SHARDS);
    }

    /* Pick duplicate sources WITHOUT replacement (shuffle the index range
     * and take a prefix) so no original is duplicated more than once.
     * Otherwise a birthday collision could pick the same original twice,
     * producing an address that appears 3+ times across only 2 distinct
     * shards - the resulting "conflict count" for that address becomes
     * dependent on processing order (hash table insertion order vs.
     * qsort's order for equal keys), so hash_set_check and
     * sort_then_scan_check could legitimately disagree even with both
     * implementations correct. Capping each original at one duplicate
     * keeps every address group at size <= 2, where order can't matter. */
    int num_dups = (int) (n * dup_rate);
    if (num_dups > n) num_dups = n;

    int *indices = malloc((size_t) n * sizeof(int));
    if (!indices) {
        fprintf(stderr, "[bench] generate_transactions: out of memory\n");
        exit(1);
    }
    for (int i = 0; i < n; i++) indices[i] = i;
    for (int i = n - 1; i > 0; i--) {
        int j = (int) xorshift32_bounded(&rng, (unsigned int) (i + 1));
        int tmp = indices[i];
        indices[i] = indices[j];
        indices[j] = tmp;
    }

    for (int i = 0; i < num_dups; i++) {
        int src = indices[i];
        Transaction *dup = &out[n + i];
        strcpy(dup->source_address, out[src].source_address);
        dup->shard = (out[src].shard + 1) % NUM_SHARDS;
    }
    free(indices);

    int total = n + num_dups;

    /* Fisher-Yates shuffle */
    for (int i = total - 1; i > 0; i--) {
        int j = (int) xorshift32_bounded(&rng, (unsigned int) (i + 1));
        Transaction tmp = out[i];
        out[i] = out[j];
        out[j] = tmp;
    }

    return total;
}

/* ----------------------------------------------------------------------
 * Hash-set duplicate check - O(n)
 * ---------------------------------------------------------------------- */
typedef struct HashEntry {
    char address[ADDR_LEN];
    int  shard;
    int  occupied;
} HashEntry;

static unsigned int fnv1a_hash(const char *s) {
    unsigned int h = 2166136261u;
    while (*s) {
        h ^= (unsigned char) (*s++);
        h *= 16777619u;
    }
    return h;
}

static int next_pow2(int x) {
    int p = 1;
    while (p < x) p <<= 1;
    return p;
}

static int hash_set_check(Transaction *txs, int n) {
    int table_size = next_pow2((int) (n * 1.3) + 1);
    HashEntry *table = calloc((size_t) table_size, sizeof(HashEntry));
    if (!table) {
        fprintf(stderr, "[bench] hash_set_check: out of memory\n");
        exit(1);
    }
    unsigned int mask = (unsigned int) (table_size - 1);

    int conflicts = 0;
    for (int i = 0; i < n; i++) {
        unsigned int idx = fnv1a_hash(txs[i].source_address) & mask;
        while (table[idx].occupied) {
            if (strcmp(table[idx].address, txs[i].source_address) == 0) {
                if (table[idx].shard != txs[i].shard)
                    conflicts++;
                table[idx].shard = txs[i].shard;
                break;
            }
            idx = (idx + 1) & mask;
        }
        if (!table[idx].occupied) {
            strcpy(table[idx].address, txs[i].source_address);
            table[idx].shard = txs[i].shard;
            table[idx].occupied = 1;
        }
    }

    free(table);
    return conflicts;
}

/* ----------------------------------------------------------------------
 * Parallel hash-set duplicate check - OpenMP, chaining + bucket ownership
 *
 * Open addressing (used above) can't be parallelized safely - a probe
 * sequence can spill past a bucket "owned" by another thread. Chaining
 * fixes that: each bucket is an independent linked list, so partitioning
 * the bucket range into one disjoint contiguous chunk per thread means no
 * two threads ever touch the same bucket. Every thread scans the full
 * input array but skips any transaction whose bucket isn't in its own
 * chunk, so there's no locking and no merge step - just a final sum of
 * each thread's private conflict count via reduction(+:conflicts).
 * ---------------------------------------------------------------------- */
typedef struct ChainNode {
    char address[ADDR_LEN];
    int  shard;
    struct ChainNode *next;
} ChainNode;

static int hash_set_check_parallel(Transaction *txs, int n, int num_threads) {
    int table_size = next_pow2((int) (n * 1.3) + 1);
    unsigned int mask = (unsigned int) (table_size - 1);

    ChainNode **buckets = calloc((size_t) table_size, sizeof(ChainNode *));
    if (!buckets) {
        fprintf(stderr, "[bench] hash_set_check_parallel: out of memory\n");
        exit(1);
    }

    int conflicts = 0;

    #pragma omp parallel num_threads(num_threads) reduction(+:conflicts)
    {
        int tid  = omp_get_thread_num();
        int nth  = omp_get_num_threads();
        int base = table_size / nth;
        int rem  = table_size % nth;
        int lo   = tid * base + (tid < rem ? tid : rem);
        int hi   = lo + base + (tid < rem ? 1 : 0);

        for (int i = 0; i < n; i++) {
            unsigned int b = fnv1a_hash(txs[i].source_address) & mask;
            if ((int) b < lo || (int) b >= hi)
                continue;   /* not this thread's bucket - skip */

            ChainNode *found = NULL;
            for (ChainNode *node = buckets[b]; node; node = node->next) {
                if (strcmp(node->address, txs[i].source_address) == 0) {
                    found = node;
                    break;
                }
            }
            if (found) {
                if (found->shard != txs[i].shard)
                    conflicts++;
                found->shard = txs[i].shard;
            } else {
                ChainNode *nn = malloc(sizeof(ChainNode));
                if (!nn) {
                    fprintf(stderr,
                            "[bench] hash_set_check_parallel: out of memory\n");
                    exit(1);
                }
                strcpy(nn->address, txs[i].source_address);
                nn->shard = txs[i].shard;
                nn->next  = buckets[b];
                buckets[b] = nn;
            }
        }
    }

    for (int b = 0; b < table_size; b++) {
        ChainNode *node = buckets[b];
        while (node) {
            ChainNode *next = node->next;
            free(node);
            node = next;
        }
    }
    free(buckets);

    return conflicts;
}

/* ----------------------------------------------------------------------
 * Sort-then-scan duplicate check - O(n log n)
 * ---------------------------------------------------------------------- */
static int addr_cmp(const void *a, const void *b) {
    return strcmp(((const Transaction *) a)->source_address,
                   ((const Transaction *) b)->source_address);
}

static int sort_then_scan_check(Transaction *txs, int n) {
    Transaction *copy = malloc((size_t) n * sizeof(Transaction));
    if (!copy) {
        fprintf(stderr, "[bench] sort_then_scan_check: out of memory\n");
        exit(1);
    }
    memcpy(copy, txs, (size_t) n * sizeof(Transaction));

    qsort(copy, (size_t) n, sizeof(Transaction), addr_cmp);

    int conflicts = 0;
    for (int i = 1; i < n; i++) {
        if (strcmp(copy[i].source_address, copy[i - 1].source_address) == 0 &&
            copy[i].shard != copy[i - 1].shard)
            conflicts++;
    }

    free(copy);
    return conflicts;
}

/* ----------------------------------------------------------------------
 * Parallel sort-then-scan duplicate check - OpenMP
 *
 * Splits the array into one contiguous chunk per thread, sorts each chunk
 * independently and in parallel (disjoint memory regions, no
 * synchronization needed), then does a serial k-way merge of the sorted
 * chunks before running the same linear conflict scan.
 * ---------------------------------------------------------------------- */
static int sort_then_scan_check_parallel(Transaction *txs, int n, int num_threads) {
    if (num_threads < 1) num_threads = 1;
    if (num_threads > n) num_threads = n;

    Transaction *copy = malloc((size_t) n * sizeof(Transaction));
    int *starts = malloc((size_t) num_threads * sizeof(int));
    int *lens   = malloc((size_t) num_threads * sizeof(int));
    if (!copy || !starts || !lens) {
        fprintf(stderr, "[bench] sort_then_scan_check_parallel: out of memory\n");
        exit(1);
    }
    memcpy(copy, txs, (size_t) n * sizeof(Transaction));

    int base = n / num_threads, rem = n % num_threads, off = 0;
    for (int t = 0; t < num_threads; t++) {
        lens[t]   = base + (t < rem ? 1 : 0);
        starts[t] = off;
        off += lens[t];
    }

    #pragma omp parallel for num_threads(num_threads) schedule(static)
    for (int t = 0; t < num_threads; t++) {
        qsort(copy + starts[t], (size_t) lens[t], sizeof(Transaction), addr_cmp);
    }

    /* Serial k-way merge of the num_threads sorted chunks */
    Transaction *merged = malloc((size_t) n * sizeof(Transaction));
    int *pos = calloc((size_t) num_threads, sizeof(int));
    if (!merged || !pos) {
        fprintf(stderr, "[bench] sort_then_scan_check_parallel: out of memory\n");
        exit(1);
    }

    for (int i = 0; i < n; i++) {
        int best = -1;
        for (int t = 0; t < num_threads; t++) {
            if (pos[t] < lens[t] &&
                (best == -1 ||
                 strcmp(copy[starts[t] + pos[t]].source_address,
                        copy[starts[best] + pos[best]].source_address) < 0))
                best = t;
        }
        merged[i] = copy[starts[best] + pos[best]];
        pos[best]++;
    }

    int conflicts = 0;
    for (int i = 1; i < n; i++) {
        if (strcmp(merged[i].source_address, merged[i - 1].source_address) == 0 &&
            merged[i].shard != merged[i - 1].shard)
            conflicts++;
    }

    free(pos);
    free(merged);
    free(lens);
    free(starts);
    free(copy);

    return conflicts;
}

/* ----------------------------------------------------------------------
 * Radix-sort-then-scan duplicate check - O(n * L)
 *
 * LSD (least-significant-digit-first) byte-wise radix sort over the fixed
 * ADDR_LEN-1 = 40 character address. Each pass is a stable counting sort
 * on one character position (radix 256), starting from the rightmost
 * character. After L passes the array is fully sorted, in O(n * L) total
 * rather than qsort's O(n log n) comparisons - since L is fixed (not a
 * function of n), this is effectively linear in n.
 * ---------------------------------------------------------------------- */
#define RADIX_BASE 256

static int radix_sort_check(Transaction *txs, int n) {
    Transaction *buf_a = malloc((size_t) n * sizeof(Transaction));
    Transaction *buf_b = malloc((size_t) n * sizeof(Transaction));
    if (!buf_a || !buf_b) {
        fprintf(stderr, "[bench] radix_sort_check: out of memory\n");
        exit(1);
    }
    memcpy(buf_a, txs, (size_t) n * sizeof(Transaction));

    Transaction *src = buf_a, *dst = buf_b;
    int count[RADIX_BASE + 1];

    for (int pos = ADDR_LEN - 2; pos >= 0; pos--) {
        memset(count, 0, sizeof(count));
        for (int i = 0; i < n; i++)
            count[(unsigned char) src[i].source_address[pos] + 1]++;
        for (int c = 0; c < RADIX_BASE; c++)
            count[c + 1] += count[c];
        for (int i = 0; i < n; i++) {
            unsigned char c = (unsigned char) src[i].source_address[pos];
            dst[count[c]++] = src[i];
        }
        Transaction *tmp = src; src = dst; dst = tmp;
    }

    int conflicts = 0;
    for (int i = 1; i < n; i++) {
        if (strcmp(src[i].source_address, src[i - 1].source_address) == 0 &&
            src[i].shard != src[i - 1].shard)
            conflicts++;
    }

    free(buf_a);
    free(buf_b);
    return conflicts;
}

/* ----------------------------------------------------------------------
 * Timing helpers
 * ---------------------------------------------------------------------- */
static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double) ts.tv_sec * 1000.0 + (double) ts.tv_nsec / 1e6;
}

static void mean_stddev(const double *samples, int n, double *mean, double *sd) {
    double sum = 0.0;
    for (int i = 0; i < n; i++) sum += samples[i];
    *mean = sum / n;

    double sq = 0.0;
    for (int i = 0; i < n; i++) {
        double d = samples[i] - *mean;
        sq += d * d;
    }
    *sd = (n > 1) ? sqrt(sq / (n - 1)) : 0.0;
}

#define CSV_HEADER \
    "n,hash_set_ms,hash_set_sd_ms,sort_scan_ms,sort_scan_sd_ms," \
    "radix_sort_ms,radix_sort_sd_ms,speedup,radix_speedup," \
    "hash_set_parallel_ms,hash_set_parallel_sd_ms," \
    "hash_parallel_speedup,sort_scan_parallel_ms," \
    "sort_scan_parallel_sd_ms,sort_parallel_speedup," \
    "conflicts_found,threads\n"

#define CSV_LINE_LEN 320

/*
 * upsert_csv_rows
 * Rewrites `path` so it ends up holding every previously-saved row EXCEPT
 * ones whose `threads` column (the last CSV field) matches this run's
 * thread count, plus all of `new_rows` appended at the end. That gives
 * append-across-different-thread-counts, overwrite-within-the-same-
 * thread-count semantics: rerunning with --threads 4 replaces the old
 * --threads 4 rows in place instead of duplicating them, while a prior
 * --threads 8 run's rows are left untouched.
 */
static void upsert_csv_rows(const char *path,
                             char new_rows[][CSV_LINE_LEN], int num_new,
                             int threads)
{
    char **kept = NULL;
    int kept_count = 0, kept_cap = 0;

    FILE *in = fopen(path, "r");
    if (in) {
        char line[CSV_LINE_LEN];
        int first = 1;
        while (fgets(line, sizeof(line), in)) {
            if (first) {
                first = 0;
                continue;   /* skip the old header - we write our own below */
            }
            size_t len = strlen(line);
            while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r'))
                line[--len] = '\0';
            if (len == 0)
                continue;

            const char *last_comma = strrchr(line, ',');
            int row_threads = last_comma ? atoi(last_comma + 1) : -1;
            if (row_threads == threads)
                continue;   /* superseded by this run */

            if (kept_count == kept_cap) {
                kept_cap = kept_cap ? kept_cap * 2 : 64;
                kept = realloc(kept, (size_t) kept_cap * sizeof(char *));
                if (!kept) {
                    fprintf(stderr, "[bench] upsert_csv_rows: out of memory\n");
                    exit(1);
                }
            }
            kept[kept_count] = malloc(len + 1);
            if (!kept[kept_count]) {
                fprintf(stderr, "[bench] upsert_csv_rows: out of memory\n");
                exit(1);
            }
            memcpy(kept[kept_count], line, len + 1);
            kept_count++;
        }
        fclose(in);
    }

    FILE *out = fopen(path, "w");
    if (!out) {
        perror("[bench] Cannot open output CSV");
        exit(1);
    }
    fprintf(out, "%s", CSV_HEADER);
    for (int i = 0; i < kept_count; i++) {
        fprintf(out, "%s\n", kept[i]);
        free(kept[i]);
    }
    free(kept);
    for (int i = 0; i < num_new; i++)
        fprintf(out, "%s", new_rows[i]);   /* already newline-terminated */
    fclose(out);
}

int main(int argc, char *argv[]) {
    int    repeats  = DEFAULT_REPEATS;
    double dup_rate = DEFAULT_DUP_RATE;
    int    threads  = omp_get_max_threads();

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--repeats") == 0 && i + 1 < argc) {
            repeats = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--dup-rate") == 0 && i + 1 < argc) {
            dup_rate = atof(argv[++i]);
        } else if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
            threads = atoi(argv[++i]);
        } else {
            fprintf(stderr,
                    "Usage: %s [--repeats N] [--dup-rate F] [--threads N]\n",
                    argv[0]);
            return 1;
        }
    }

    if (repeats < 1) {
        fprintf(stderr, "[bench] --repeats must be >= 1\n");
        return 1;
    }
    if (threads < 1) {
        fprintf(stderr, "[bench] --threads must be >= 1\n");
        return 1;
    }

    printf("[bench] repeats=%d dup_rate=%.4f threads=%d\n",
           repeats, dup_rate, threads);
    printf("%10s %14s %14s %14s %10s %14s | %14s %10s | %14s %10s\n",
           "n", "hash_set_ms", "sort_scan_ms", "radix_sort_ms", "speedup",
           "radix_speedup", "hash||_ms", "||speedup", "sort||_ms", "||speedup");

    /* Rows are buffered in memory and only written at the end via
     * upsert_csv_rows() - which keeps rows from prior runs at other
     * --threads values (append) while replacing rows from a prior run at
     * this same --threads value (overwrite), rather than duplicating them. */
    const char *csv_path = "conflict_check_benchmark_c.csv";
    char new_rows[NUM_BLOCK_SIZES][CSV_LINE_LEN];

    double prev_n = -1.0, prev_hash_ms = -1.0, prev_sort_ms = -1.0,
           prev_radix_ms = -1.0;

    for (int b = 0; b < NUM_BLOCK_SIZES; b++) {
        int n = BLOCK_SIZES[b];
        int max_total = n + (int) (n * dup_rate) + 1;

        Transaction *txs = malloc((size_t) max_total * sizeof(Transaction));
        if (!txs) {
            fprintf(stderr, "[bench] Out of memory for n=%d\n", n);
            return 1;
        }

        unsigned int seed = (unsigned int) (1000003u + (unsigned int) n);
        int total = generate_transactions(n, dup_rate, seed, txs);

        double *hash_samples      = malloc((size_t) repeats * sizeof(double));
        double *sort_samples      = malloc((size_t) repeats * sizeof(double));
        double *radix_samples     = malloc((size_t) repeats * sizeof(double));
        double *hash_par_samples  = malloc((size_t) repeats * sizeof(double));
        double *sort_par_samples  = malloc((size_t) repeats * sizeof(double));
        if (!hash_samples || !sort_samples || !radix_samples ||
            !hash_par_samples || !sort_par_samples) {
            fprintf(stderr, "[bench] Out of memory for timing samples\n");
            free(txs);
            return 1;
        }

        int hash_conflicts = -1, sort_conflicts = -1, radix_conflicts = -1,
            hash_par_conflicts = -1, sort_par_conflicts = -1;

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            hash_conflicts = hash_set_check(txs, total);
            double t1 = now_ms();
            hash_samples[r] = t1 - t0;
        }

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            sort_conflicts = sort_then_scan_check(txs, total);
            double t1 = now_ms();
            sort_samples[r] = t1 - t0;
        }

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            radix_conflicts = radix_sort_check(txs, total);
            double t1 = now_ms();
            radix_samples[r] = t1 - t0;
        }

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            hash_par_conflicts = hash_set_check_parallel(txs, total, threads);
            double t1 = now_ms();
            hash_par_samples[r] = t1 - t0;
        }

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            sort_par_conflicts = sort_then_scan_check_parallel(txs, total, threads);
            double t1 = now_ms();
            sort_par_samples[r] = t1 - t0;
        }

        if (hash_conflicts != sort_conflicts || hash_conflicts != radix_conflicts ||
            hash_conflicts != hash_par_conflicts || hash_conflicts != sort_par_conflicts) {
            fprintf(stderr,
                    "[bench] WARNING: conflict count mismatch at n=%d "
                    "(hash_set=%d, sort_scan=%d, radix_sort=%d, "
                    "hash_set||=%d, sort_scan||=%d) - algorithm bug\n",
                    n, hash_conflicts, sort_conflicts, radix_conflicts,
                    hash_par_conflicts, sort_par_conflicts);
        }

        double hash_mean, hash_sd, sort_mean, sort_sd, radix_mean, radix_sd,
               hash_par_mean, hash_par_sd, sort_par_mean, sort_par_sd;
        mean_stddev(hash_samples, repeats, &hash_mean, &hash_sd);
        mean_stddev(sort_samples, repeats, &sort_mean, &sort_sd);
        mean_stddev(radix_samples, repeats, &radix_mean, &radix_sd);
        mean_stddev(hash_par_samples, repeats, &hash_par_mean, &hash_par_sd);
        mean_stddev(sort_par_samples, repeats, &sort_par_mean, &sort_par_sd);
        double speedup = (hash_mean > 0.0) ? sort_mean / hash_mean : 0.0;
        double radix_speedup = (radix_mean > 0.0) ? sort_mean / radix_mean : 0.0;
        double hash_par_speedup = (hash_par_mean > 0.0) ? hash_mean / hash_par_mean : 0.0;
        double sort_par_speedup = (sort_par_mean > 0.0) ? sort_mean / sort_par_mean : 0.0;

        printf("%10d %14.4f %14.4f %14.4f %10.2fx %13.2fx | %14.4f %9.2fx | %14.4f %9.2fx\n",
               n, hash_mean, sort_mean, radix_mean, speedup, radix_speedup,
               hash_par_mean, hash_par_speedup, sort_par_mean, sort_par_speedup);

        snprintf(new_rows[b], CSV_LINE_LEN,
                 "%d,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,"
                 "%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%d,%d\n",
                 n, hash_mean, hash_sd, sort_mean, sort_sd, radix_mean,
                 radix_sd, speedup, radix_speedup,
                 hash_par_mean, hash_par_sd, hash_par_speedup,
                 sort_par_mean, sort_par_sd, sort_par_speedup,
                 hash_conflicts, threads);

        if (prev_n > 0.0) {
            double n_ratio = (double) n / prev_n;
            double hash_ratio = hash_mean / prev_hash_ms;
            double sort_ratio = sort_mean / prev_sort_ms;
            double radix_ratio = radix_mean / prev_radix_ms;
            printf("           scaling: n x%.2f | hash_set x%.2f "
                   "(want ~%.2f, linear) | sort_scan x%.2f "
                   "(want > %.2f, n log n) | radix_sort x%.2f "
                   "(want ~%.2f, linear)\n",
                   n_ratio, hash_ratio, n_ratio, sort_ratio, n_ratio,
                   radix_ratio, n_ratio);
        }

        prev_n = (double) n;
        prev_hash_ms = hash_mean;
        prev_sort_ms = sort_mean;
        prev_radix_ms = radix_mean;

        free(hash_samples);
        free(sort_samples);
        free(radix_samples);
        free(hash_par_samples);
        free(sort_par_samples);
        free(txs);
    }

    upsert_csv_rows(csv_path, new_rows, NUM_BLOCK_SIZES, threads);
    printf("[bench] Done -> %s (threads=%d rows upserted)\n", csv_path, threads);

    return 0;
}
