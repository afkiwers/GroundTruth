/* Apple Accelerate's sparse LDLT for solver/sparse.py (ctypes cannot call its overloaded C API directly).
 * Compiled on first use on the Mac: xcrun clang -O2 -dynamiclib -o gt_accelerate.dylib accelerate_shim.c
 * -framework Accelerate. The matrix: the upper triangle of a complex symmetric (not Hermitian) matrix in CSC
 * (int64 column starts, int32 row indices, complex128 values); macOS 15.5+ for complex matrices. The calls are the
 * ones tools/dev/accelerate_bench.c measured on the M2. */
#include <Accelerate/Accelerate.h>
#include <complex.h>
#include <stdlib.h>

typedef struct {
    SparseOpaqueFactorization_Complex_Double F;
    long n;
} gt_handle;

/* Factorise; info[0] = status (0 ok, -2 out of memory, else Accelerate's), info[1] = the factor's size in
 * complex entries (its symbolic factorisation's double storage / 16). NULL on failure. */
void *gt_factor(long n, const long *columnStarts, const int *rowIndices, const double complex *values, long *info) {
    SparseMatrix_Complex_Double A = {
        .structure = {.rowCount = (int)n, .columnCount = (int)n, .columnStarts = (long *)columnStarts,
                      .rowIndices = (int *)rowIndices,
                      .attributes = {.transpose = false, .triangle = SparseUpperTriangle, .kind = SparseSymmetric},
                      .blockSize = 1},
        .data = (void *)values};
    gt_handle *h = malloc(sizeof(gt_handle));
    if (!h) { info[0] = -2; return NULL; }
    h->n = n;
    h->F = SparseFactor(SparseFactorizationLDLTSBK, A);
    info[0] = (long)h->F.status;
    info[1] = (long)(h->F.symbolicFactorization.factorSize_Double / 16);
    if (h->F.status != SparseStatusOK) {
        if (h->F.status == SparseStatusFailed && info[1] == 0) info[0] = -2;
        SparseCleanup(h->F);
        free(h);
        return NULL;
    }
    return h;
}

/* Solve in place: x holds k right-hand sides (column-major, n x k) and gets the solutions. 0 on success. */
int gt_solve(void *handle, double complex *x, int k) {
    gt_handle *h = handle;
    if (k == 1) {
        DenseVector_Complex_Double v = {.count = (int)h->n, .data = (void *)x};
        SparseSolve(h->F, v);
    } else {
        DenseMatrix_Complex_Double X = {.rowCount = (int)h->n, .columnCount = k, .columnStride = (int)h->n,
                                        .attributes = {0}, .data = (void *)x};
        SparseSolve(h->F, X);
    }
    return 0;
}

void gt_free(void *handle) {
    gt_handle *h = handle;
    if (h) { SparseCleanup(h->F); free(h); }
}
