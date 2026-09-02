"""M46 item 4(a) — linear-CCA sanity check on the (section, facts) view pair.

Gates the JEPA/MemoryJEPA track. The MemoryJEPA proposal trains an encoder to
predict a section's *structured implications* (extracted facts) from its raw
text. That only pays off if the two views actually share a correlated subspace
under a linear map — if they don't, no amount of nonlinear machinery will find
one, and the GPU-days are wasted.

CCA finds paired directions (a, b) maximising corr(Xa, Yb). Reported as
canonical correlations, descending.

**The null control is the whole point.** CCA on finite samples finds spurious
correlation from dimensionality alone, so we re-run against *shuffled* pairs
(section i's embedding vs another section's facts). Real signal = the true
curve sits materially above the shuffled curve. Without this control a
"0.9 correlation" is uninterpretable.

CPU-only, no API calls.
"""
from __future__ import annotations

import argparse
import asyncio
import os

import numpy as np


async def load_pairs(dsn: str, limit: int, view_x: str = "summary") -> tuple[list[str], list[str]]:
    """view_x='summary' uses the LLM-written section summary; view_x='raw' slices the
    document's original markdown around the section anchor. ``raw`` is the stricter
    test: summary and facts are both LLM derivatives of the same source, so their
    correlation is partly tautological. JEPA's actual claim is about predicting
    implications from RAW text."""
    import psycopg

    if view_x == "raw":
        sql = """
            WITH s AS (
              SELECT s.document_id, s.line_num, d.md_text,
                     string_agg(f.fact_text, ' | ' ORDER BY f.id) AS facts
              FROM astrocyte_pi_sections s
              JOIN astrocyte_pi_documents d ON d.id = s.document_id
              JOIN astrocyte_pi_facts f USING (document_id, line_num)
              WHERE d.md_text IS NOT NULL
              GROUP BY s.document_id, s.line_num, d.md_text
              HAVING count(f.id) >= 2
              LIMIT %s
            )
            SELECT
              array_to_string((string_to_array(md_text, E'\n'))[line_num : line_num + 40], E'\n'),
              NULL, facts
            FROM s
        """
    else:
        sql = """
        SELECT s.title, s.summary, string_agg(f.fact_text, ' | ' ORDER BY f.id) AS facts
        FROM astrocyte_pi_sections s
        JOIN astrocyte_pi_facts f USING (document_id, line_num)
        WHERE s.summary IS NOT NULL AND length(s.summary) > 40
        GROUP BY s.document_id, s.line_num, s.title, s.summary
        HAVING count(f.id) >= 2
        LIMIT %s
    """
    async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.cursor() as cur:
        await cur.execute(sql, (limit,))
        rows = await cur.fetchall()
    sections = [f"{(t or '').strip()}\n{(s or '').strip()}".strip() for t, s, _ in rows]
    facts = [(f or "").strip() for _, _, f in rows]
    return sections, facts


async def embed(texts: list[str], batch: int = 128) -> np.ndarray:
    from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

    # pad_to=None: use the model's native width. Zero-padding to 1536 would add
    # constant columns, which are degenerate for CCA (zero variance).
    p = LocalEmbeddingsProvider(pad_to=None)
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        out.extend(await p.embed(texts[i : i + batch]))
    return np.asarray(out, dtype=np.float64)


def canonical_correlations(X: np.ndarray, Y: np.ndarray, k: int) -> np.ndarray:
    """Canonical correlations via QR + SVD — numerically stabler than fitting
    sklearn's iterative CCA, and it returns all k at once."""
    Xc = X - X.mean(0)
    Yc = Y - Y.mean(0)
    Qx, _ = np.linalg.qr(Xc)
    Qy, _ = np.linalg.qr(Yc)
    s = np.linalg.svd(Qx.T @ Qy, compute_uv=False)
    return np.clip(s[:k], 0.0, 1.0)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("BENCH_DSN",
                    "postgresql://astrocyte:astrocyte@127.0.0.1:5434/astrocyte_bench"))
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--components", type=int, default=32)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--view-x", choices=("summary", "raw"), default="summary")
    ap.add_argument("--pca", type=int, default=64,
                    help="Reduce each view to this many PCs before CCA (0 = off). "
                         "CCA needs n >> d; at d=384 with n~2000 the null "
                         "correlation is ~0.77 from overfitting alone, which "
                         "swamps the signal. 64 PCs gives n/d ~ 30.")
    a = ap.parse_args()

    sections, facts = await load_pairs(a.dsn, a.limit, a.view_x)
    n = len(sections)
    if n < 100:
        print(f"only {n} pairs — too few to interpret")
        return 1
    print(f"view_x={a.view_x}  pairs: {n}\n  section[0]: {sections[0][:90]!r}\n  facts[0]:   {facts[0][:90]!r}\n")

    X = await embed(sections)
    Y = await embed(facts)
    print(f"embeddings: X{X.shape} Y{Y.shape}")

    if a.pca:
        from sklearn.decomposition import PCA
        d = min(a.pca, X.shape[1], Y.shape[1], n - 2)
        px, py = PCA(n_components=d, random_state=a.seed), PCA(n_components=d, random_state=a.seed)
        Xv, Yv = px.fit_transform(X), py.fit_transform(Y)
        print(f"PCA -> {d} comps/view (n/d = {n/d:.0f}); "
              f"variance retained X {px.explained_variance_ratio_.sum():.1%} "
              f"Y {py.explained_variance_ratio_.sum():.1%}")
        X, Y = Xv, Yv
    print()

    k = min(a.components, X.shape[1], Y.shape[1], n - 1)
    true_c = canonical_correlations(X, Y, k)

    rng = np.random.default_rng(a.seed)
    perm = rng.permutation(n)
    null_c = canonical_correlations(X, Y[perm], k)

    print(f"{'rank':>5}{'true':>10}{'shuffled':>11}{'gap':>9}")
    for i in (0, 1, 2, 4, 9, 19, k - 1):
        if i < k:
            print(f"{i+1:>5}{true_c[i]:>10.3f}{null_c[i]:>11.3f}{true_c[i]-null_c[i]:>9.3f}")

    t10, n10 = true_c[:10].mean(), null_c[:10].mean()
    print(f"\nmean top-10   true {t10:.3f}   shuffled {n10:.3f}   gap {t10-n10:+.3f}")
    print(f"mean top-{k}  true {true_c.mean():.3f}   shuffled {null_c.mean():.3f}   "
          f"gap {true_c.mean()-null_c.mean():+.3f}")

    gap = t10 - n10
    verdict = ("STRONG — view pair has real shared structure; JEPA/Deep-CCA track justified"
               if gap >= 0.25 else
               "MODERATE — some signal; cheaper embedder A/B first, JEPA only if that stalls"
               if gap >= 0.10 else
               "WEAK — no meaningful shared subspace beyond chance; do NOT invest in JEPA "
               "on this view pair; redesign the views first")
    print(f"\nVERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
