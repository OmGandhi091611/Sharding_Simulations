CC     = gcc
CFLAGS = -O2

# merge_results' static `rows` table is large enough (MAX_FILES * MAX_LINE)
# to exceed the +-2GB PC-relative addressing range of the default x86_64
# small code model, which fails to link on Ubuntu/Linux with a
# "relocation ... out of range" error. -mcmodel=medium lifts that limit;
# -no-pie is required alongside it since GCC doesn't support medium/large
# code models combined with position-independent executables. Neither
# flag is needed (or supported the same way) on macOS/ARM64, so this is
# scoped to Linux only.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Linux)
    MERGE_FLAGS = -mcmodel=medium -no-pie
else
    MERGE_FLAGS =
endif

all: merge_results aggregate_results conflict_check_benchmark

merge_results: merge_results.c
	$(CC) $(CFLAGS) -fopenmp $(MERGE_FLAGS) merge_results.c -o merge_results

aggregate_results: aggregate_results.c
	$(CC) $(CFLAGS) aggregate_results.c -o aggregate_results -lm

conflict_check_benchmark: conflict_check_benchmark.c
	$(CC) $(CFLAGS) -o conflict_check_benchmark conflict_check_benchmark.c -lm

# Rebuilds (only if conflict_check_benchmark.c changed) and runs a clean
# sweep. Removes the old raw CSV first - the benchmark writes a fresh
# copy every run (plain overwrite, no merge logic), but clearing it here
# keeps behavior obvious and avoids ever eyeballing a stale file.
bench-conflict-check: conflict_check_benchmark clean-csv
	./conflict_check_benchmark --repeats 5

clean: clean-csv
	rm -f merge_results aggregate_results conflict_check_benchmark

clean-csv:
	rm -f conflict_check_benchmark_raw_c.csv

.PHONY: all clean clean-csv bench-conflict-check
