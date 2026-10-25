/* The preconditioner's sparse factorisation with Apple Accelerate's sparse solvers, on a Mac (docs/SPEED.md, M3):
 * the twin of tools/dev/sparse_bench.py for Apple Silicon, where Intel MKL (PARDISO) does not run.
 *
 *   xcrun clang -O2 -o accelerate_bench accelerate_bench.c -framework Accelerate
 *   VECLIB_MAXIMUM_THREADS=1 ./accelerate_bench MATRIX.bin [LDLT|LDLTSBK|LDLTTPP|LDLTUnpivoted ...]
 *
 * MATRIX.bin (tools/dev/accelerate_pack.py): int64 n, int64 nnz, int64 columnStarts[n + 1], int32 rowIndices[nnz],
 * complex128 values[nnz]: the upper triangle of the complex symmetric (not Hermitian) Schur complement in CSC.
 * Per factorisation type one line: the factorisation (symbolic + numeric), a refactorisation on the same symbolic
 * one (the next frequency of a model), a 1- and an 8-column solve, the relative residual |S x - b| / |b|, the
 * process's peak memory, and whether two solves give the same bits. Accelerate runs multithreaded unless
 * VECLIB_MAXIMUM_THREADS=1, and Apple documents that only then are its results deterministic. Complex matrices
 * need macOS 15.5 or later. */
#include <Accelerate/Accelerate.h>
#include <complex.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + 1e-9 * t.tv_nsec;
}

static double peak_mb(void) {                    /* ru_maxrss is in bytes on macOS */
    struct rusage u;
    getrusage(RUSAGE_SELF, &u);
    return u.ru_maxrss / 1048576.0;
}

/* y = S x for S complex symmetric stored as its upper triangle (CSC): both triangles, no conjugation */
static void symv(long n, const long *cs, const int *ri, const double complex *v, const double complex *x,
                 double complex *y) {
    memset(y, 0, n * sizeof(double complex));
    for (long j = 0; j < n; j++)
        for (long p = cs[j]; p < cs[j + 1]; p++) {
            long i = ri[p];
            y[i] += v[p] * x[j];
            if (i != j) y[j] += v[p] * x[i];
        }
}

static double norm(long n, const double complex *x) {
    double s = 0;
    for (long i = 0; i < n; i++) s += creal(x[i] * conj(x[i]));
    return sqrt(s);
}

static SparseFactorization_t kind_of(const char *name) {
    if (!strcmp(name, "LDLTSBK")) return SparseFactorizationLDLTSBK;
    if (!strcmp(name, "LDLTTPP")) return SparseFactorizationLDLTTPP;
    if (!strcmp(name, "LDLTUnpivoted")) return SparseFactorizationLDLTUnpivoted;
    return SparseFactorizationLDLT;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s MATRIX.bin [LDLT LDLTSBK LDLTTPP LDLTUnpivoted]\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 2; }
    long long hdr[2];
    if (fread(hdr, sizeof hdr, 1, f) != 1) { fprintf(stderr, "short file\n"); return 2; }
    long n = (long)hdr[0], nnz = (long)hdr[1];
    long *cs = malloc((n + 1) * sizeof(long));
    int *ri = malloc(nnz * sizeof(int));
    double complex *v = malloc(nnz * sizeof(double complex));
    if (fread(cs, sizeof(long), n + 1, f) != (size_t)(n + 1) || fread(ri, sizeof(int), nnz, f) != (size_t)nnz ||
        fread(v, sizeof(double complex), nnz, f) != (size_t)nnz) {
        fprintf(stderr, "short file\n");
        return 2;
    }
    fclose(f);
    const char *threads = getenv("VECLIB_MAXIMUM_THREADS");
    SparseMatrix_Complex_Double A = {
        .structure = {.rowCount = (int)n, .columnCount = (int)n, .columnStarts = cs, .rowIndices = ri,
                      .attributes = {.transpose = false, .triangle = SparseUpperTriangle, .kind = SparseSymmetric},
                      .blockSize = 1},
        .data = (void *)v};
    double complex *b = malloc(n * 8 * sizeof(double complex)), *x = malloc(n * 8 * sizeof(double complex));
    double complex *x2 = malloc(n * sizeof(double complex)), *r = malloc(n * sizeof(double complex));
    srand(1);
    for (long i = 0; i < n * 8; i++) b[i] = (rand() / (double)RAND_MAX - 0.5) + I * (rand() / (double)RAND_MAX - 0.5);
    int nk = argc > 2 ? argc - 2 : 1;
    for (int k = 0; k < nk; k++) {
        const char *kname = argc > 2 ? argv[2 + k] : "LDLT";
        double t0 = now();
        SparseOpaqueFactorization_Complex_Double F = SparseFactor(kind_of(kname), A);
        double t1 = now();
        if (F.status != SparseStatusOK) {
            printf("{\"matrix\": \"%s\", \"type\": \"%s\", \"threads\": \"%s\", \"error\": %d}\n", argv[1], kname,
                   threads ? threads : "default", (int)F.status);
            continue;
        }
        SparseRefactor(A, &F);
        double t2 = now();
        DenseVector_Complex_Double bv = {.count = (int)n, .data = (void *)b}, xv = {.count = (int)n, .data = (void *)x};
        double s1 = 1e30, s8 = 1e30;
        for (int rep = 0; rep < 3; rep++) {
            double a = now();
            SparseSolve(F, bv, xv);
            double c = now();
            if (c - a < s1) s1 = c - a;
        }
        DenseVector_Complex_Double xv2 = {.count = (int)n, .data = (void *)x2};
        SparseSolve(F, bv, xv2);
        int same = !memcmp(x, x2, n * sizeof(double complex));
        symv(n, cs, ri, v, x, r);
        for (long i = 0; i < n; i++) r[i] -= b[i];
        double res = norm(n, r) / norm(n, b);
        DenseMatrix_Complex_Double B = {.rowCount = (int)n, .columnCount = 8, .columnStride = (int)n,
                                        .attributes = {0}, .data = (void *)b};
        DenseMatrix_Complex_Double X = {.rowCount = (int)n, .columnCount = 8, .columnStride = (int)n,
                                        .attributes = {0}, .data = (void *)x};
        for (int rep = 0; rep < 2; rep++) {
            double a = now();
            SparseSolve(F, B, X);
            double c = now();
            if (c - a < s8) s8 = c - a;
        }
        printf("{\"matrix\": \"%s\", \"type\": \"%s\", \"threads\": \"%s\", \"n\": %ld, \"nnz_upper\": %ld, "
               "\"factor_s\": %.4f, \"refactor_s\": %.4f, \"solve1_s\": %.5f, \"solve8_s\": %.5f, "
               "\"residual\": %.3e, \"peak_mb\": %.1f, \"repeatable\": %s}\n",
               argv[1], kname, threads ? threads : "default", n, nnz, t1 - t0, t2 - t1, s1, s8, res, peak_mb(),
               same ? "true" : "false");
        fflush(stdout);
        SparseCleanup(F);
    }
    return 0;
}
