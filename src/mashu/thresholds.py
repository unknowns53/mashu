"""Specification 27.2: measure the similarity distributions, do not guess them.

Two thresholds are placeholders. Entity resolution decides whether a proposed
title is a rival to an existing entity or a new concept; scope detection
decides which scopes a query is narrowed to. Both were set by hand for a model
whose similarity scale nobody had looked at.

The negative set here is the real store rather than invented names: every pair
of titles inside one scope, which are overwhelmingly distinct concepts. The
positive set is rewordings of real titles, written to say the same thing in
different words, which is section 27.2's deliberately confusable case.
"""

# ruff: noqa: E501 — the pairs below are real titles from the store, and
# reflowing them would change the thing being measured.

import itertools

from mashu.db import transaction
from mashu.embed import get_embedder

# Same concept, different words. The left side is a title that is in the store.
POSITIVE = [
    (
        "測定を起動する前に「X なら A、Y なら B」と書く。両枝が同じ行動なら、あるいは閾値が好みの問題なら流さない",
        "計測を回す前に、結果ごとの行動を先に書き出す。どちらでも同じことをするなら測らない",
    ),
    (
        "zsh は変数を単語分割しないので引数列が 1 トークンに潰れ、probe は既定値で走ってそれらしい数字を出す",
        "シェル変数に引数をまとめると zsh では 1 語として渡り、probe が既定値のまま走る",
    ),
    (
        "Godot はログをファイルへリダイレクトすると stdout をブロックバッファリングする。ログが空でも死んだ証拠にならない。生死は ps で確認する",
        "ログが止まっていても Godot が落ちたとは限らない。リダイレクト時は出力が溜め込まれる",
    ),
    (
        "enrai のプレイ検証は倍速で行われる。sim 時間の周期・遅延は実時間では半分に体感される",
        "手触りの確認は 2 倍速なので、シミュレーション秒をそのまま体感時間として語れない",
    ),
    (
        "記録に「今朝」「夜」と書かない。私は時刻を持っておらず、memory の modified は UTC で日本時間と 9 時間ずれる",
        "相対的な時刻表現を記録に残さない。こちらは現在時刻を持たないため",
    ),
    (
        "memory とコメントの参照は行番号でなくシンボル名で書く。行番号は書いた瞬間から腐り、grep で追えないので誰も気づかない",
        "コード参照は行番号ではなく関数名や定数名で書く。行番号はすぐずれる",
    ),
    (
        "外部サイトからの連続ダウンロードは逐次・小休止で行う。2026-07-07 の学内ネットワーク遮断（原因未確定、ssh 作業と当セッションの freesound 連続取得が時間的に重複）を受けた予防措置",
        "外部からまとめて取得するときは並列にせず、間隔を空けて順に落とす",
    ),
    (
        "効果量が最大になる数値を選ぶ前に、その差が何でできているかを見る。指標を最大化して指標の目的を失う型",
        "差が最も開く値をそのまま採用しない。何がその差を作っているかを先に確かめる",
    ),
    (
        "探索帯で見えた効果は確認帯でほぼ消える。8 シードの点推定を「効いた」と呼ばず、確認が済むまで結論の言葉を使わない",
        "少数シードの探索で出た差は追試で消えることが多いので、確定的な言い方をしない",
    ),
    (
        "保険で張った ScheduleWakeup は、待っていた完了通知が先に届いた時点で stop:true で畳む",
        "予備で仕掛けた定時起動は、本来の通知が来たら止める",
    ),
]

QUERIES = [
    ("作業の規律", "測定を始める前に何を決めておくべきか"),
    ("作業の規律", "サブエージェントに仕事を任せるときの作法"),
    ("enrai", "空母戦のレバーはどの probe で測るのが正しいか"),
    ("enrai", "難易度を上げるときに敵をどう作るべきか"),
    ("mashu", "未審査の知識をエージェントにどう渡すか"),
    ("作業の規律", "今日の天気はどうか"),
    ("enrai", "PNIPAM の曇点測定の昇温速度"),
]


def main(dsn: str | None = None) -> None:
    embedder = get_embedder()
    print(f"model: {embedder.name}\n")

    with transaction(dsn) as cur:
        cur.execute(
            "SELECT s.name AS scope, e.title FROM memory_entity e "
            "JOIN scope s ON s.scope_id = e.scope_id WHERE e.status = 'active' ORDER BY s.name"
        )
        rows = cur.fetchall()
        cur.execute("SELECT name, description FROM scope WHERE status = 'active'")
        scopes = cur.fetchall()

    by_scope: dict[str, list[str]] = {}
    for row in rows:
        by_scope.setdefault(row["scope"], []).append(row["title"])

    # --- negatives: real pairs inside one scope -----------------------------
    negatives = []
    for scope, titles in by_scope.items():
        vectors = embedder.embed_documents(titles)
        for (i, _), (j, _b) in itertools.combinations(enumerate(titles), 2):
            negatives.append((cosine(vectors[i], vectors[j]), scope, titles[i], titles[j]))
    negatives.sort(reverse=True)

    print(f"entity resolution — {len(negatives)} real same-scope pairs (distinct concepts)")
    report(negatives)
    print("  closest real pairs:")
    for sim, scope, a, b in negatives[:3]:
        print(f"    {sim:.3f}  [{scope}] {a[:34]}… / {b[:34]}…")

    # --- positives: the same claim, reworded --------------------------------
    left = embedder.embed_documents([a for a, _ in POSITIVE])
    right = embedder.embed_documents([b for _, b in POSITIVE])
    positives = sorted((cosine(left[i], right[i]), POSITIVE[i][0]) for i in range(len(POSITIVE)))
    print(f"\n  {len(positives)} reworded pairs (same concept)")
    report([(p[0],) for p in positives])
    print(f"    lowest: {positives[0][0]:.3f}  {positives[0][1][:50]}…")

    print("    all positives: " + " ".join(f"{v:.3f}" for v, _ in positives))
    sweep([n[0] for n in negatives], [v for v, _ in positives])

    gap = (negatives[0][0], positives[0][0])
    print(f"\n  separation: highest negative {gap[0]:.3f}, lowest positive {gap[1]:.3f}")
    print(f"  -> {'separable' if gap[1] > gap[0] else 'OVERLAPPING'}")

    # --- scope detection: the label method, withdrawn -----------------------
    # Kept as the comparison. 27.2 measured it and found it does not separate;
    # what replaced it is measured directly below, on the same queries.
    texts = [f"{s['name']}. {s['description']}" if s["description"] else s["name"] for s in scopes]
    scope_vectors = embedder.embed_documents(texts)
    print("\nscope detection, old (query against each scope's label)")
    label_right = 0
    for want, query in QUERIES:
        sims = sorted(
            (
                (cosine(embedder.embed_query(query), scope_vectors[i]), scopes[i]["name"])
                for i in range(len(scopes))
            ),
            reverse=True,
        )
        best = sims[0]
        label_right += best[1] == want
        mark = "hit " if best[1] == want else "MISS"
        listed = "  ".join(f"{n}={v:.3f}" for v, n in sims)
        print(f"  {mark} want={want:<12} {listed}   << {query[:26]}…")
    print(f"  -> {label_right}/{len(QUERIES)} ranked correctly")

    # --- scope detection: the held-memory method ----------------------------
    # The query goes against the memories themselves, and the scopes the best
    # matches live in are the answer. It asks about concentration rather than
    # about an absolute similarity, which is the thing these embeddings can
    # actually answer.
    print("\nscope detection, new (query against what each scope holds)")
    with transaction(dsn) as cur:
        cur.execute(
            "SELECT s.name AS scope, count(*) AS held FROM memory_entity e "
            "JOIN scope s ON s.scope_id = e.scope_id "
            "WHERE e.status = 'active' AND e.active_version IS NOT NULL GROUP BY s.name"
        )
        sizes = {r["scope"]: r["held"] for r in cur.fetchall()}
        total = sum(sizes.values())
        print("  在庫: " + "  ".join(f"{k}={v}" for k, v in sizes.items()) + f"  合計 {total}\n")
        for want, query in QUERIES:
            vector = "[" + ",".join(f"{v:.6f}" for v in embedder.embed_query(query)) + "]"
            cur.execute(
                """
                SELECT s.name AS scope, 1 - (v.content_embedding <=> %(q)s::vector) AS sim
                FROM memory_entity e
                JOIN memory_version v
                  ON v.version_id = e.active_version AND v.memory_id = e.memory_id
                JOIN scope s ON s.scope_id = e.scope_id
                WHERE e.status = 'active' AND v.content_embedding IS NOT NULL
                ORDER BY v.content_embedding <=> %(q)s::vector
                LIMIT %(probe)s
                """,
                {"q": vector, "probe": PROBE},
            )
            hits = cur.fetchall()
            for floor in FLOORS:
                kept = [h for h in hits if h["sim"] >= floor]
                counts: dict[str, int] = {}
                for h in kept:
                    counts[h["scope"]] = counts.get(h["scope"], 0) + 1
                got = []
                lifts = {}
                for s, c in counts.items():
                    expected = sizes[s] / total
                    lifts[s] = (c / len(kept)) / expected if expected and kept else 0.0
                    if c >= MIN_HITS and lifts[s] >= LIFT:
                        got.append(s)
                got.sort(key=lambda s: -max(h["sim"] for h in kept if h["scope"] == s))
                shape = "  ".join(
                    f"{s}={c}(x{lifts[s]:.2f})"
                    for s, c in sorted(counts.items(), key=lambda kv: -kv[1])
                )
                verdict = "detect nothing" if not got else ",".join(got)
                print(
                    f"  floor {floor:.2f}  n={len(kept):>2}  "
                    f"[{shape or 'none'}]  ->  {verdict:<16} want={want:<12} << {query[:24]}…"
                )
            print()


PROBE = 25
MIN_HITS = 2
LIFT = 1.2
FLOORS = (0.78, 0.80, 0.82, 0.84)


def sweep(negatives, positives):
    print("\n  threshold sweep")
    print(f"    thr    caught/10 positives   flagged negatives (of {len(negatives)})")
    for thr in (0.88, 0.89, 0.90, 0.91, 0.92, 0.94):
        caught = sum(1 for p in positives if p >= thr)
        flagged = sum(1 for n in negatives if n >= thr)
        print(f"    {thr:.2f}   {caught:>2}                    {flagged:>3}")


def cosine(a, b):
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


def report(rows):
    values = sorted(r[0] for r in rows)
    n = len(values)

    def q(p):
        return values[min(n - 1, int(n * p))]

    print(
        f"    min {values[0]:.3f}  median {q(0.5):.3f}  p95 {q(0.95):.3f} "
        f" p99 {q(0.99):.3f}  max {values[-1]:.3f}"
    )
