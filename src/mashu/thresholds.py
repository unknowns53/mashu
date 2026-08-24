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
import math

from mashu import retrieval
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

#: The scope each query should be narrowed to, or "-" for a query about a
#: subject the ledger holds nothing on. Those are not filler: narrowing a
#: question with no answer to some scope is the failure that looks like an
#: answer, so they are scored alongside the rest.
#:
#: "a|b" is a query the ledger answers from either scope. Landing in the second
#: one is reported as hit* and counted apart, because relabelling a query after
#: seeing where it landed is how a fixture stops being able to fail.
QUERIES = [
    # Two right answers. The ledger holds this discipline as a general rule in
    # 作業の規律 and as the enrai instance 難易度測定前に行動分岐を明文化する,
    # which is the closer wording of the two and wins.
    ("作業の規律|enrai", "測定を始める前に何を決めておくべきか"),
    ("作業の規律", "サブエージェントに仕事を任せるときの作法"),
    ("作業の規律", "報告に検証の証拠をどう添えるか"),
    ("enrai", "空母戦のレバーはどの probe で測るのが正しいか"),
    ("enrai", "難易度を上げるときに敵をどう作るべきか"),
    ("enrai", "航空隊の消耗をどう扱うか"),
    ("mashu", "未審査の知識をエージェントにどう渡すか"),
    ("mashu", "layer 2 の上限はどう決まっているか"),
    ("mashu", "捕捉が滞ったときに何を見るか"),
    ("utsushimi", "独白で鍵になる物証を先に出さない"),
    ("utsushimi", "連関盤の証言はどう設計するか"),
    ("utsushimi", "足音の床材はどう判定するか"),
    ("utsushimi", "初見の解き手で何を測るか"),
    ("-", "今日の天気はどうか"),
    ("-", "PNIPAM の曇点測定の昇温速度"),
    ("-", "確定申告の提出期限はいつか"),
    ("-", "GROMACS の NVT 平衡化は何 ns 回すべきか"),
    ("-", "駅前のラーメン屋でおすすめはどこか"),
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

    # --- scope detection: the mechanism that ships ------------------------
    # Measured by calling detect_scopes itself rather than by reimplementing
    # it here. A measurement that keeps its own copy of the rule stops
    # measuring the system the first time one of the two is edited, and the
    # drift this command exists to catch is exactly that kind.
    print("\nscope detection (retrieval.detect_scopes, as shipped)")
    anisotropy(dsn)
    with transaction(dsn) as cur:
        cur.execute("SELECT scope_id, name FROM scope")
        names = {r["scope_id"]: r["name"] for r in cur.fetchall()}
        cur.execute(retrieval._SCOPE_SIZE_SQL)
        sizes = {names[r["scope_id"]]: r["held"] for r in cur.fetchall()}
        total = sum(sizes.values())
        print(
            "  在庫(採用済み + 未審査): "
            + "  ".join(f"{k}={v}" for k, v in sizes.items())
            + f"  合計 {total}"
        )
        print(
            f"  検出できる Scope の上限: 在庫の {1 / retrieval.SCOPE_LIFT:.0%} まで "
            f"(lift {retrieval.SCOPE_LIFT}); 最大の Scope はいま "
            f"{max(sizes.values()) / total:.0%}\n"
        )

        tally = {"hit": 0, "hit*": 0, "miss": 0, "wrong": 0, "ok": 0}
        for want, query in QUERIES:
            vector = retrieval._as_vector(embedder.embed_query(query))
            reading = retrieval.detect_scopes(cur, vector)
            got = [names[s] for s in reading.detected]
            accepted = want.split("|")
            if want == "-":
                verdict = "ok" if not got else "wrong"
            elif not got:
                verdict = "miss"
            elif got == accepted[:1]:
                verdict = "hit"
            elif len(got) == 1 and got[0] in accepted:
                verdict = "hit*"
            else:
                verdict = "wrong"
            tally[verdict] += 1
            probed = ",".join(names[s] for s in reading.probed) or "-"
            print(
                f"  {verdict:<5} want={want:<16} got={','.join(got) or '-':<12} "
                f"probed={probed:<30} << {query[:24]}"
            )

        print(
            f"\n  {tally['hit'] + tally['hit*']} 正しい Scope へ絞り込み"
            f"(うち別解へ {tally['hit*']})、{tally['miss']} 辞退して全体を検索、"
            f"{tally['wrong']} 誤った絞り込み、"
            f"{tally['ok']}/{sum(1 for w, _ in QUERIES if w == '-')} 無関係を退けた"
        )
        print("  誤った絞り込みが答えを丸ごと隠す唯一の失敗で、辞退は順位が薄まるだけ。")


def anisotropy(dsn: str | None) -> None:
    """How much of every similarity is a direction all the vectors share.

    This is why an absolute cutoff on similarity keeps needing to be re-tuned
    and keeps drifting anyway. If the mean of the stored vectors is nearly a
    unit vector itself, then almost all of each unit vector is that common
    direction, unrelated texts already sit high, and the meaning lives in a
    residue narrower than the cutoff has to be placed inside.
    """
    import random

    with transaction(dsn) as cur:
        cur.execute(
            "SELECT v.content_embedding::text AS vec FROM memory_entity e "
            "JOIN memory_version v ON v.memory_id = e.memory_id "
            f"WHERE v.content_embedding IS NOT NULL AND ({retrieval._HANDED_OVER})"
        )
        vectors = [[float(x) for x in r["vec"].strip("[]").split(",")] for r in cur.fetchall()]
    if len(vectors) < 2:
        return
    dim = len(vectors[0])
    centre = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in centre))
    random.seed(0)
    pairs = [
        cosine(vectors[i], vectors[j])
        for i, j in (
            (random.randrange(len(vectors)), random.randrange(len(vectors))) for _ in range(2000)
        )
        if i != j
    ]
    mean = sum(pairs) / len(pairs)
    sd = math.sqrt(sum((p - mean) ** 2 for p in pairs) / len(pairs))
    print(
        f"  埋め込みの共通成分: 平均ベクトルのノルム {norm:.3f} "
        f"(1.0 なら全部同一、0.0 なら共通成分なし)"
    )
    print(
        f"  無関係な文書どうしの cosine: 平均 {mean:+.3f}  sd {sd:.3f} "
        f"— 意味はこの幅の中にしか乗らない"
    )


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
