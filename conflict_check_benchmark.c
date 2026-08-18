/*
 * conflict_check_benchmark.c
 *
 * Benchmarks three ways of detecting cross-shard duplicate transactions
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
 *
 * Also benchmarks two ways of detecting same-shard nonce replay (the same
 * source address reusing the same nonce - the leader-independent, per-
 * shard-local half of double-spend detection):
 *
 *   4. nonce_check             - O(n) open-addressing hash table keyed on
 *                                 (address, nonce) instead of address alone.
 *   5. nonce_sort_then_scan_check - O(n log n) qsort ordered by (address,
 *                                 nonce) + linear scan.
 *
 * hash_set_check and sort_then_scan_check (and their nonce counterparts)
 * each additionally report an alloc/process or sort/scan sub-phase
 * breakdown alongside their total time - see NUM_ALGOS below.
 *
 * For each n in BLOCK_SIZES (matching Parallel_processes/network_parallel.py)
 * a synthetic transaction set is generated once, then every algorithm is
 * run REPEATS times and timed with clock_gettime(CLOCK_MONOTONIC, ...), all
 * in one command/run and written as raw per-measurement rows to one CSV so
 * the address-check and nonce-check numbers are directly comparable.
 *
 * Usage:
 *   ./conflict_check_benchmark [--repeats N] [--dup-rate F]
 *
 * Compile:
 *   gcc -O2 -o conflict_check_benchmark conflict_check_benchmark.c -lm
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

#define ADDR_LEN 41  /* 40 hex chars + NUL, matches a 32-byte hash in hex */
#define NUM_SHARDS 256
#define DEFAULT_REPEATS 10
#define DEFAULT_DUP_RATE 0.05
#define RADIX_BASE 256

typedef struct {
    char source_address[ADDR_LEN];
    int  shard;
    int  nonce;
} Transaction;

/* Extended down to 2 (not just 1024) so the nonce check - benchmarked in
 * the same sweep as the address check below - covers actual_per_shard,
 * which can be as small as 2 with shard counts up to 512 crossed against
 * the smallest block size of 1024. */
static const int BLOCK_SIZES[] = {
    2, 4, 8, 16, 32, 64, 128, 256, 512,
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
 * Fills `out` (caller-allocated, at least n entries) with exactly n
 * transactions total: num_unique distinct synthetic addresses plus
 * num_dups injected cross-shard duplicates of some of them, where
 * num_dups = floor(n * dup_rate) (capped at n/2, see below), and
 * num_unique = n - num_dups - so n is the TRUE total transaction count
 * (matching a real block/shard size), not a floor that duplicates get
 * added on top of. Then Fisher-Yates shuffles the whole array so
 * duplicates aren't trivially adjacent. Returns n (the total written).
 */
static int generate_transactions(int n, double dup_rate, unsigned int seed,
                                  Transaction *out)
{
    Xorshift32 rng;
    xorshift32_seed(&rng, seed);

    /* Pick duplicate sources WITHOUT replacement (shuffle the index range
     * and take a prefix) so no original is duplicated more than once.
     * Otherwise a birthday collision could pick the same original twice,
     * producing an address that appears 3+ times across only 2 distinct
     * shards - the resulting "conflict count" for that address becomes
     * dependent on processing order (hash table insertion order vs.
     * qsort's order for equal keys), so hash_set_check and
     * sort_then_scan_check could legitimately disagree even with both
     * implementations correct. Capping each original at one duplicate
     * keeps every address group at size <= 2, where order can't matter -
     * and since each duplicate pairs with one unique original out of the
     * n total slots, num_dups can be at most n/2 (100% dup_rate means
     * every address appears exactly twice, using all n slots as n/2
     * pairs). */
    int num_dups = (int) (n * dup_rate);
    int max_dups = n / 2;
    if (num_dups > max_dups) num_dups = max_dups;
    int num_unique = n - num_dups;

    for (int i = 0; i < num_unique; i++) {
        snprintf(out[i].source_address, ADDR_LEN, "addr_%08x", i);
        /* pad remaining hex chars deterministically so the address is a
         * full 40 hex chars, not just the 13-char "addr_%08x" prefix */
        for (int j = 13; j < ADDR_LEN - 1; j++) {
            static const char hex[] = "0123456789abcdef";
            out[i].source_address[j] = hex[xorshift32_bounded(&rng, 16)];
        }
        out[i].source_address[ADDR_LEN - 1] = '\0';
        out[i].shard = (int) xorshift32_bounded(&rng, NUM_SHARDS);
        out[i].nonce = i;
    }

    int *indices = malloc((size_t) num_unique * sizeof(int));
    if (!indices) {
        fprintf(stderr, "[bench] generate_transactions: out of memory\n");
        exit(1);
    }
    for (int i = 0; i < num_unique; i++) indices[i] = i;
    for (int i = num_unique - 1; i > 0; i--) {
        int j = (int) xorshift32_bounded(&rng, (unsigned int) (i + 1));
        int tmp = indices[i];
        indices[i] = indices[j];
        indices[j] = tmp;
    }

    for (int i = 0; i < num_dups; i++) {
        int src = indices[i];
        Transaction *dup = &out[num_unique + i];
        strcpy(dup->source_address, out[src].source_address);
        dup->shard = (out[src].shard + 1) % NUM_SHARDS;
        /* Same nonce as its source - this is what nonce_check (a same-
         * address-same-nonce replay check) is meant to catch, independent
         * of the shard field it doesn't look at. */
        dup->nonce = out[src].nonce;
    }
    free(indices);

    int total = num_unique + num_dups;   /* always equals n */

    /* Fisher-Yates shuffle */
    for (int i = total - 1; i > 0; i--) {
        int j = (int) xorshift32_bounded(&rng, (unsigned int) (i + 1));
        Transaction tmp = out[i];
        out[i] = out[j];
        out[j] = tmp;
    }

    return total;
}

/* Moved up here (from the "Timing helpers" section further down) so the
 * check functions below can time their own sub-phases internally. */
static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double) ts.tv_sec * 1000.0 + (double) ts.tv_nsec / 1e6;
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

/*
 * hash_set_check
 * out_alloc_ms/out_process_ms (nullable): split of the total time into the
 * table calloc() vs. the main hash+probe+insert loop. Insert and lookup
 * aren't separable within the loop itself - each iteration hashes the key,
 * probes for an existing match (the "lookup"), and either counts a
 * conflict or inserts, all as one fused operation - so this is the closest
 * real two-phase split available for a hash table (setup vs. the work),
 * unlike sort_then_scan_check below where sort and scan genuinely are two
 * separate passes.
 */
static int hash_set_check(Transaction *txs, int n, double *out_alloc_ms, double *out_process_ms) {
    int table_size = next_pow2((int) (n * 1.3) + 1);

    double t0 = now_ms();
    HashEntry *table = calloc((size_t) table_size, sizeof(HashEntry));
    double t1 = now_ms();
    if (out_alloc_ms) *out_alloc_ms = t1 - t0;

    if (!table) {
        fprintf(stderr, "[bench] hash_set_check: out of memory\n");
        exit(1);
    }
    unsigned int mask = (unsigned int) (table_size - 1);

    int conflicts = 0;
    double t2 = now_ms();
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
    double t3 = now_ms();
    if (out_process_ms) *out_process_ms = t3 - t2;

    free(table);
    return conflicts;
}

/* ----------------------------------------------------------------------
 * Nonce-replay check - O(n), single-threaded
 *
 * Same open-addressing shape as hash_set_check, but keyed on
 * (source_address, nonce) instead of source_address alone: a repeat of
 * the exact same (address, nonce) pair is a same-shard replay/double-
 * spend, independent of whichever shard field the transaction carries -
 * this check never looks at shard at all. Meant to run per-shard, on
 * each shard's own local transaction slice (actual_per_shard), not the
 * full aggregated block.
 * ---------------------------------------------------------------------- */
typedef struct NonceHashEntry {
    char address[ADDR_LEN];
    int  nonce;
    int  occupied;
} NonceHashEntry;

static unsigned int fnv1a_hash_nonce(const char *s, int nonce) {
    unsigned int h = 2166136261u;
    while (*s) {
        h ^= (unsigned char) (*s++);
        h *= 16777619u;
    }
    const unsigned char *nb = (const unsigned char *) &nonce;
    for (size_t k = 0; k < sizeof(nonce); k++) {
        h ^= nb[k];
        h *= 16777619u;
    }
    return h;
}

static int nonce_check(Transaction *txs, int n, double *out_alloc_ms, double *out_process_ms) {
    int table_size = next_pow2((int) (n * 1.3) + 1);

    double t0 = now_ms();
    NonceHashEntry *table = calloc((size_t) table_size, sizeof(NonceHashEntry));
    double t1 = now_ms();
    if (out_alloc_ms) *out_alloc_ms = t1 - t0;

    if (!table) {
        fprintf(stderr, "[bench] nonce_check: out of memory\n");
        exit(1);
    }
    unsigned int mask = (unsigned int) (table_size - 1);

    int conflicts = 0;
    double t2 = now_ms();
    for (int i = 0; i < n; i++) {
        unsigned int idx = fnv1a_hash_nonce(txs[i].source_address, txs[i].nonce) & mask;
        while (table[idx].occupied) {
            if (table[idx].nonce == txs[i].nonce &&
                strcmp(table[idx].address, txs[i].source_address) == 0) {
                conflicts++;
                break;
            }
            idx = (idx + 1) & mask;
        }
        if (!table[idx].occupied) {
            strcpy(table[idx].address, txs[i].source_address);
            table[idx].nonce = txs[i].nonce;
            table[idx].occupied = 1;
        }
    }
    double t3 = now_ms();
    if (out_process_ms) *out_process_ms = t3 - t2;

    free(table);
    return conflicts;
}

/* ----------------------------------------------------------------------
 * Sort-then-scan nonce-replay check - O(n log n)
 * ---------------------------------------------------------------------- */
static int nonce_cmp(const void *a, const void *b) {
    const Transaction *ta = (const Transaction *) a;
    const Transaction *tb = (const Transaction *) b;
    int c = strcmp(ta->source_address, tb->source_address);
    if (c != 0) return c;
    return (ta->nonce > tb->nonce) - (ta->nonce < tb->nonce);
}

static int nonce_sort_then_scan_check(Transaction *txs, int n, double *out_sort_ms, double *out_scan_ms) {
    Transaction *copy = malloc((size_t) n * sizeof(Transaction));
    if (!copy) {
        fprintf(stderr, "[bench] nonce_sort_then_scan_check: out of memory\n");
        exit(1);
    }
    memcpy(copy, txs, (size_t) n * sizeof(Transaction));

    double t0 = now_ms();
    qsort(copy, (size_t) n, sizeof(Transaction), nonce_cmp);
    double t1 = now_ms();
    if (out_sort_ms) *out_sort_ms = t1 - t0;

    int conflicts = 0;
    double t2 = now_ms();
    for (int i = 1; i < n; i++) {
        if (copy[i].nonce == copy[i - 1].nonce &&
            strcmp(copy[i].source_address, copy[i - 1].source_address) == 0)
            conflicts++;
    }
    double t3 = now_ms();
    if (out_scan_ms) *out_scan_ms = t3 - t2;

    free(copy);
    return conflicts;
}

/* ----------------------------------------------------------------------
 * Sort-then-scan duplicate check - O(n log n)
 * ---------------------------------------------------------------------- */
static int addr_cmp(const void *a, const void *b) {
    return strcmp(((const Transaction *) a)->source_address,
                   ((const Transaction *) b)->source_address);
}

/* out_sort_ms/out_scan_ms (nullable): the qsort call and the adjacent-pair
 * comparison loop are genuinely two separate passes here, unlike the hash
 * checks, so this is a clean split rather than an approximation. */
static int sort_then_scan_check(Transaction *txs, int n, double *out_sort_ms, double *out_scan_ms) {
    Transaction *copy = malloc((size_t) n * sizeof(Transaction));
    if (!copy) {
        fprintf(stderr, "[bench] sort_then_scan_check: out of memory\n");
        exit(1);
    }
    memcpy(copy, txs, (size_t) n * sizeof(Transaction));

    double t0 = now_ms();
    qsort(copy, (size_t) n, sizeof(Transaction), addr_cmp);
    double t1 = now_ms();
    if (out_sort_ms) *out_sort_ms = t1 - t0;

    int conflicts = 0;
    double t2 = now_ms();
    for (int i = 1; i < n; i++) {
        if (strcmp(copy[i].source_address, copy[i - 1].source_address) == 0 &&
            copy[i].shard != copy[i - 1].shard)
            conflicts++;
    }
    double t3 = now_ms();
    if (out_scan_ms) *out_scan_ms = t3 - t2;

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

/* CSV_LINE_LEN: generous fixed width for a raw-CSV row
 * ("algorithm,run,n,time_ms\n") - far more than needed, but cheap and
 * avoids any risk of truncation. */
#define CSV_LINE_LEN 8192

/* Raw (long-format) CSV: one row per (algorithm, run, n), with every
 * timed sub-phase as its own column alongside the total. alloc_ms/
 * process_ms only apply to hash_set/nonce_hash_set; sort_ms/scan_ms only
 * apply to sort_scan/nonce_sort_scan; radix_sort has neither pair. The
 * non-applicable columns are 0 for a given algorithm's rows - total_ms
 * is the only column populated for every algorithm. */
#define RAW_CSV_HEADER "algorithm,run,n,alloc_ms,process_ms,sort_ms,scan_ms,total_ms\n"
#define NUM_ALGOS 5

/* Plain overwrite, no thread-merge logic - the raw CSV doesn't carry a
 * threads column to key off of (kept minimal on purpose), so each run
 * just replaces the whole file. */
static void write_csv_rows(const char *path, const char *header,
                            char rows[][CSV_LINE_LEN], int num_rows)
{
    FILE *out = fopen(path, "w");
    if (!out) {
        perror("[bench] Cannot open output CSV");
        exit(1);
    }
    fprintf(out, "%s", header);
    for (int i = 0; i < num_rows; i++)
        fprintf(out, "%s", rows[i]);
    fclose(out);
}

int main(int argc, char *argv[]) {
    int    repeats  = DEFAULT_REPEATS;
    double dup_rate = DEFAULT_DUP_RATE;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--repeats") == 0 && i + 1 < argc) {
            repeats = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--dup-rate") == 0 && i + 1 < argc) {
            dup_rate = atof(argv[++i]);
        } else {
            fprintf(stderr, "Usage: %s [--repeats N] [--dup-rate F]\n", argv[0]);
            return 1;
        }
    }

    if (repeats < 1) {
        fprintf(stderr, "[bench] --repeats must be >= 1\n");
        return 1;
    }

    printf("[bench] repeats=%d dup_rate=%.4f\n", repeats, dup_rate);

    /* Raw (long-format) CSV: one row per single measurement (algorithm x
     * run x n) - heap-allocated since its row count depends on the
     * runtime --repeats value. */
    const char *raw_csv_path = "conflict_check_benchmark_raw_c.csv";
    int num_raw_rows = NUM_BLOCK_SIZES * NUM_ALGOS * repeats;
    char (*raw_rows)[CSV_LINE_LEN] = malloc((size_t) num_raw_rows * CSV_LINE_LEN);
    if (!raw_rows) {
        fprintf(stderr, "[bench] Out of memory for raw CSV rows\n");
        return 1;
    }

    /* Results are buffered here during the sweep and printed as three
     * clean, separated tables afterward, instead of one wide packed line
     * per n - much easier to read across 19 rows. */
    int    ns[NUM_BLOCK_SIZES];
    double t_hash[NUM_BLOCK_SIZES], t_sort[NUM_BLOCK_SIZES], t_radix[NUM_BLOCK_SIZES];
    double t_speedup[NUM_BLOCK_SIZES], t_radix_speedup[NUM_BLOCK_SIZES];
    int    t_conflicts[NUM_BLOCK_SIZES];
    double t_nonce_hash[NUM_BLOCK_SIZES], t_nonce_sort[NUM_BLOCK_SIZES];
    double t_nonce_speedup[NUM_BLOCK_SIZES];
    int    t_nonce_conflicts[NUM_BLOCK_SIZES];

    for (int b = 0; b < NUM_BLOCK_SIZES; b++) {
        int n = BLOCK_SIZES[b];
        /* generate_transactions now always writes exactly n entries - no
         * extra room needed for duplicates on top, since dup_rate is a
         * fraction of n, not an addition to it. */
        Transaction *txs = malloc((size_t) n * sizeof(Transaction));
        if (!txs) {
            fprintf(stderr, "[bench] Out of memory for n=%d\n", n);
            return 1;
        }

        unsigned int seed = (unsigned int) (1000003u + (unsigned int) n);
        int total = generate_transactions(n, dup_rate, seed, txs);

        double *hash_samples      = malloc((size_t) repeats * sizeof(double));
        double *sort_samples      = malloc((size_t) repeats * sizeof(double));
        double *radix_samples     = malloc((size_t) repeats * sizeof(double));
        double *nonce_hash_samples = malloc((size_t) repeats * sizeof(double));
        double *nonce_sort_samples = malloc((size_t) repeats * sizeof(double));
        /* Sub-phase breakdowns: alloc vs. process for the two hash checks,
         * sort vs. scan for the two sort-then-scan checks. */
        double *hash_alloc_samples        = malloc((size_t) repeats * sizeof(double));
        double *hash_process_samples      = malloc((size_t) repeats * sizeof(double));
        double *sort_sortonly_samples     = malloc((size_t) repeats * sizeof(double));
        double *sort_scanonly_samples     = malloc((size_t) repeats * sizeof(double));
        double *nonce_hash_alloc_samples   = malloc((size_t) repeats * sizeof(double));
        double *nonce_hash_process_samples = malloc((size_t) repeats * sizeof(double));
        double *nonce_sort_sortonly_samples = malloc((size_t) repeats * sizeof(double));
        double *nonce_sort_scanonly_samples = malloc((size_t) repeats * sizeof(double));
        if (!hash_samples || !sort_samples || !radix_samples ||
            !nonce_hash_samples || !nonce_sort_samples ||
            !hash_alloc_samples || !hash_process_samples ||
            !sort_sortonly_samples || !sort_scanonly_samples ||
            !nonce_hash_alloc_samples || !nonce_hash_process_samples ||
            !nonce_sort_sortonly_samples || !nonce_sort_scanonly_samples) {
            fprintf(stderr, "[bench] Out of memory for timing samples\n");
            free(txs);
            return 1;
        }

        int hash_conflicts = -1, sort_conflicts = -1, radix_conflicts = -1,
            nonce_hash_conflicts = -1, nonce_sort_conflicts = -1;

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            hash_conflicts = hash_set_check(txs, total, &hash_alloc_samples[r], &hash_process_samples[r]);
            double t1 = now_ms();
            hash_samples[r] = t1 - t0;
        }

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            sort_conflicts = sort_then_scan_check(txs, total, &sort_sortonly_samples[r], &sort_scanonly_samples[r]);
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
            nonce_hash_conflicts = nonce_check(txs, total, &nonce_hash_alloc_samples[r], &nonce_hash_process_samples[r]);
            double t1 = now_ms();
            nonce_hash_samples[r] = t1 - t0;
        }

        for (int r = 0; r < repeats; r++) {
            double t0 = now_ms();
            nonce_sort_conflicts = nonce_sort_then_scan_check(txs, total, &nonce_sort_sortonly_samples[r], &nonce_sort_scanonly_samples[r]);
            double t1 = now_ms();
            nonce_sort_samples[r] = t1 - t0;
        }

        if (hash_conflicts != sort_conflicts || hash_conflicts != radix_conflicts) {
            fprintf(stderr,
                    "[bench] WARNING: conflict count mismatch at n=%d "
                    "(hash_set=%d, sort_scan=%d, radix_sort=%d) - algorithm bug\n",
                    n, hash_conflicts, sort_conflicts, radix_conflicts);
        }

        /* Separate cross-validation: the nonce check counts a different
         * thing (address+nonce replays) than the address+shard check
         * above, so its conflict count is only compared against its own
         * other method, not against hash_conflicts. */
        if (nonce_hash_conflicts != nonce_sort_conflicts) {
            fprintf(stderr,
                    "[bench] WARNING: nonce conflict count mismatch at n=%d "
                    "(nonce_hash_set=%d, nonce_sort_scan=%d) - algorithm bug\n",
                    n, nonce_hash_conflicts, nonce_sort_conflicts);
        }

        double hash_mean, hash_sd, sort_mean, sort_sd, radix_mean, radix_sd,
               nonce_hash_mean, nonce_hash_sd, nonce_sort_mean, nonce_sort_sd;
        mean_stddev(hash_samples, repeats, &hash_mean, &hash_sd);
        mean_stddev(sort_samples, repeats, &sort_mean, &sort_sd);
        mean_stddev(radix_samples, repeats, &radix_mean, &radix_sd);
        mean_stddev(nonce_hash_samples, repeats, &nonce_hash_mean, &nonce_hash_sd);
        mean_stddev(nonce_sort_samples, repeats, &nonce_sort_mean, &nonce_sort_sd);

        double hash_alloc_mean, hash_alloc_sd, hash_process_mean, hash_process_sd,
               sort_sortonly_mean, sort_sortonly_sd, sort_scanonly_mean, sort_scanonly_sd,
               nonce_hash_alloc_mean, nonce_hash_alloc_sd,
               nonce_hash_process_mean, nonce_hash_process_sd,
               nonce_sort_sortonly_mean, nonce_sort_sortonly_sd,
               nonce_sort_scanonly_mean, nonce_sort_scanonly_sd;
        mean_stddev(hash_alloc_samples, repeats, &hash_alloc_mean, &hash_alloc_sd);
        mean_stddev(hash_process_samples, repeats, &hash_process_mean, &hash_process_sd);
        mean_stddev(sort_sortonly_samples, repeats, &sort_sortonly_mean, &sort_sortonly_sd);
        mean_stddev(sort_scanonly_samples, repeats, &sort_scanonly_mean, &sort_scanonly_sd);
        mean_stddev(nonce_hash_alloc_samples, repeats, &nonce_hash_alloc_mean, &nonce_hash_alloc_sd);
        mean_stddev(nonce_hash_process_samples, repeats, &nonce_hash_process_mean, &nonce_hash_process_sd);
        mean_stddev(nonce_sort_sortonly_samples, repeats, &nonce_sort_sortonly_mean, &nonce_sort_sortonly_sd);
        mean_stddev(nonce_sort_scanonly_samples, repeats, &nonce_sort_scanonly_mean, &nonce_sort_scanonly_sd);

        double speedup = (hash_mean > 0.0) ? sort_mean / hash_mean : 0.0;
        double radix_speedup = (radix_mean > 0.0) ? sort_mean / radix_mean : 0.0;
        double nonce_speedup = (nonce_hash_mean > 0.0) ? nonce_sort_mean / nonce_hash_mean : 0.0;

        ns[b] = n;
        t_hash[b] = hash_mean; t_sort[b] = sort_mean; t_radix[b] = radix_mean;
        t_speedup[b] = speedup; t_radix_speedup[b] = radix_speedup;
        t_conflicts[b] = hash_conflicts;
        t_nonce_hash[b] = nonce_hash_mean; t_nonce_sort[b] = nonce_sort_mean;
        t_nonce_speedup[b] = nonce_speedup;
        t_nonce_conflicts[b] = nonce_hash_conflicts;

        /* Raw (long-format) rows: one row per (algorithm, run, n), with
         * every timed sub-phase as its own column - NULL sample arrays
         * mean "not applicable for this algorithm", written out as 0. */
        {
            struct {
                const char *name;
                double *alloc_samples, *process_samples;
                double *sort_samples_, *scan_samples_;
                double *total_samples;
            } algos[NUM_ALGOS] = {
                { "hash_set",        hash_alloc_samples,      hash_process_samples,      NULL, NULL, hash_samples },
                { "sort_scan",       NULL, NULL,               sort_sortonly_samples,     sort_scanonly_samples, sort_samples },
                { "radix_sort",      NULL, NULL,               NULL, NULL,                radix_samples },
                { "nonce_hash_set",  nonce_hash_alloc_samples, nonce_hash_process_samples, NULL, NULL, nonce_hash_samples },
                { "nonce_sort_scan", NULL, NULL,               nonce_sort_sortonly_samples, nonce_sort_scanonly_samples, nonce_sort_samples },
            };
            for (int a = 0; a < NUM_ALGOS; a++) {
                for (int r = 0; r < repeats; r++) {
                    int row_idx = (b * NUM_ALGOS + a) * repeats + r;
                    double alloc_v   = algos[a].alloc_samples   ? algos[a].alloc_samples[r]   : 0.0;
                    double process_v = algos[a].process_samples ? algos[a].process_samples[r] : 0.0;
                    double sort_v    = algos[a].sort_samples_   ? algos[a].sort_samples_[r]    : 0.0;
                    double scan_v    = algos[a].scan_samples_   ? algos[a].scan_samples_[r]    : 0.0;
                    double total_v   = algos[a].total_samples[r];
                    snprintf(raw_rows[row_idx], CSV_LINE_LEN, "%s,%d,%d,%.6f,%.6f,%.6f,%.6f,%.6f\n",
                             algos[a].name, r + 1, n, alloc_v, process_v, sort_v, scan_v, total_v);
                }
            }
        }

        free(hash_samples);
        free(sort_samples);
        free(radix_samples);
        free(nonce_hash_samples);
        free(nonce_sort_samples);
        free(hash_alloc_samples);
        free(hash_process_samples);
        free(sort_sortonly_samples);
        free(sort_scanonly_samples);
        free(nonce_hash_alloc_samples);
        free(nonce_hash_process_samples);
        free(nonce_sort_sortonly_samples);
        free(nonce_sort_scanonly_samples);
        free(txs);
    }

    write_csv_rows(raw_csv_path, RAW_CSV_HEADER, raw_rows, num_raw_rows);
    free(raw_rows);

    printf("\n================================================================================\n");
    printf("  Address+Shard Duplicate Check (single-threaded)\n");
    printf("================================================================================\n");
    printf("%10s  %14s  %14s  %14s  %9s  %9s\n",
           "n", "hash_set(ms)", "sort_scan(ms)", "radix_sort(ms)", "speedup", "radix_spd");
    printf("--------------------------------------------------------------------------------\n");
    for (int b = 0; b < NUM_BLOCK_SIZES; b++) {
        printf("%10d  %14.4f  %14.4f  %14.4f  %8.2fx  %8.2fx\n",
               ns[b], t_hash[b], t_sort[b], t_radix[b], t_speedup[b], t_radix_speedup[b]);
    }

    printf("\n================================================================================\n");
    printf("  Nonce Replay Check (single-threaded)\n");
    printf("================================================================================\n");
    printf("%10s  %16s  %16s  %9s  %11s  %11s\n",
           "n", "nonce_hash(ms)", "nonce_sort(ms)", "speedup", "conflicts", "shard_conf");
    printf("--------------------------------------------------------------------------------\n");
    for (int b = 0; b < NUM_BLOCK_SIZES; b++) {
        printf("%10d  %16.4f  %16.4f  %8.2fx  %11d  %11d\n",
               ns[b], t_nonce_hash[b], t_nonce_sort[b], t_nonce_speedup[b],
               t_nonce_conflicts[b], t_conflicts[b]);
    }
    printf("================================================================================\n");

    printf("\n[bench] Done -> %s (%d rows written)\n", raw_csv_path, num_raw_rows);

    return 0;
}
