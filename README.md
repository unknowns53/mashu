# Mashu(摩周)

複数の AI Agent が共有する、Version 管理された外部 Knowledge State。

Agent に長期記憶を持たせるのではなく、記憶を外に置いて、Agent は毎回そこから読む。Agent は記憶主体ではなく、Knowledge State を利用する推論エンジンとして扱う。

*パッケージ名の由来は北海道の摩周湖である。流入する川も流出する川もない閉じた水盆であり、どの Agent にも所有されない外部の層のメタファーとする。*

## 解決対象

- 古い情報が現在情報として利用される
- 終了済み Task が未完了として扱われる
- 棄却された仮説が再利用される
- Agent ごとに異なる認識状態になる
- 過去判断の理由が追跡できない

保存する単位は文章ではなく Knowledge Object であり、`Content + Status + Version + Provenance + Scope + History` を持つ。重要なのは情報量ではなく状態管理である。

## 知識の形

```
Scope（作業の領域。台帳管理。作成は User のみ）
  └ Memory Entity（概念。type と title を持つ）
      └ Memory Version（内容。不変。status と由来を持つ）
```

**「いま有効な知識」は `entity.active_version` が指す Version ただ一つ**とする。これが真実の一元化であり、古い Version が Context に混入しない保証の根拠になる。Version は書き換えないので、訂正は新しい Version を作って active を移す操作になる。

Version の status は 6 つ。

| status | 意味 |
|---|---|
| `candidate` | 提案されたが Review 未通過 |
| `superseded` | 新しい Version に置き換わった |
| `disproven` | 誤りだと判明した |
| `dormant` | 現在は使わないが将来再評価しうる |
| `rejected` | Proposal が却下された。誰も提案できない status |
| `completed` | Task が終わった |

`dormant` は知識の内容についての評価、`rejected` は Proposal という手続きへの判断であり、別の軸として分けてある。

## 書き込みの経路

Agent は知識を直接書けない。すべて Proposal として出し、Commit Gate が振り分ける。

| 区分 | 対象 |
|---|---|
| Auto Commit | User が明示した変更（type を問わない）、単純な Task 完了 |
| Candidate Commit | fact / interpretation / hypothesis / state、および User 明示でない preference |
| Human Review Required | Active の切替、Disproven 化、Restore、Merge、type の訂正、類似度超過での Entity 作成 |

preference が Candidate 側にあるのは、preference が「以後のセッションで従う指示」だからである。type だけで自動採用すると、Agent が自分の指示を書ける経路になる。安全にするのは出どころのほうで、User が述べた preference は上の行で Auto Commit に落ちる。

## 読み出しの経路

検索は三層で返る。

| 層 | 対象 | 渡すもの |
|---|---|---|
| Layer 1 Active | active_version | 本文 |
| Layer 2 Unreviewed | 未審査の candidate | 本文 + `unreviewed` タグ |
| Layer 3 Retired | superseded を除く退役 Version | title / status / reason のみ。**本文は返さない** |

Layer 3 が本文を返さないのは、退役した知識だからである。渡すのは「何が、なぜ否定されたか」だけで、否定された主張そのものより否定した根拠のほうが Agent の再導出を止める。

Layer 1 の項目に退役の提案が出ているときは、本文と一緒に「誰が、どの status を、どんな理由で」提案しているかが返る。Agent の推論による退役は無人では落とさないが、疑義が出ていることを伏せて手渡すのはその裏返しの誤りになる。

**Review は「使えるようにする門」ではなく「品質を確定する門」である。** 未審査の候補も本文を渡す（タグ付きで）ので、Review が遅れても知識は使える。承認済みが 1 件も無い Scope も同じように答える。Layer 2 は合計 1500 token を上限とする（先頭の 1 件だけは超えても通す。当たったのに何も返さないより 1 件返すほうがよいという判断）。

セッション開始時に押し込まれる固定の塊は、type ではなく delivery で決まる。

- `startup_required` — 全セッションに押し込む
- `scope_required` — Scope が決まったら押し込む
- `pull_only` — 検索されたときだけ返す

押し込むときに運ぶのは、その Version の **directive**（レビュー済みの短形）があればそれ、無ければ本文全体である。短形は本文を圧縮した同じ知識であって新しい主張ではないので、新しい Version を作らずその場に書く（`mashu directive`）。書けるのは User だけで、理由は delivery と同じ。毎セッション押し込まれる文面を Agent が自分で書けたら、17 節が preference で塞いだ穴を一列隣で開け直すことになる。

delivery を上げる操作は admission control を通る。予算を超える変更は黙って削らず、拒否する。測るのは**そのセッションが実際に受け取る開き方**、すなわち startup の塊と Scope の塊を合わせたものであって、全員に共通する部分だけではない。共通部分だけを測ると、Scope を特定しなかったセッションには収まり、居場所を知ったセッションすべてで溢れる昇格が通ってしまう。

## 必要なもの

- PostgreSQL と [pgvector](https://github.com/pgvector/pgvector)（HNSW を使う）。PostgreSQL 17.11 / pgvector 0.8.6 で動かしている
- Python 3.13、[uv](https://docs.astral.sh/uv/)
- 埋め込みモデル `intfloat/multilingual-e5-large`（1024 次元）

## 用意する

```bash
uv sync --extra embed --extra mcp
```

```bash
createdb mashu
```

```bash
uv run mashu admin migrate
```

`migrate` が `CREATE EXTENSION vector` から流すので、pgvector が入っていれば他の準備は要らない。接続先は既定で `dbname=mashu`、環境変数 `MASHU_DATABASE_URL` で変えられる。

fresh clone では git hook を入れる。

```bash
./hooks/install.sh
```

## 使う

毎日の面。Review は随意なので、決めている最中に読む必要のないものは `admin` 以下へ分けてある。

| コマンド | 内容 |
|---|---|
| `mashu status` | 捕捉が生きているか、queue がどれだけ遅れているか、開始時の塊がいくらか |
| `mashu review` | 最古の束を開き、一括承認・却下・編集・明示の先送りまで 1 回で終える |
| `mashu find <query>` | 三層を通して引く |
| `mashu inspect <id>` / `--bundle <id>` | 1 件、または束を 1 つの読み物として表示 |
| `mashu remember <body>` | User が述べた知識を記録する。Review を待たず active になる |
| `mashu retire <id> <status> --reason` | 終わった・覆ったことを User が直接記録する |
| `mashu active --scope <name>` | その Scope が真として持つもの全件（退役の洗い出しの入力） |
| `mashu scope` | Scope ごとの採用済み・未審査の件数 |
| `mashu scope --add <name> --about <line>` | Scope を作る。`--route` で対応づけまで一手 |
| `mashu route --add <path> --scope <name>` | 作業ディレクトリを Scope に対応づける |
| `mashu route --ignore <path>` | そのディレクトリは捕捉しないと決める |
| `mashu directive <id> <short>` | 押し込むときに渡る短形を書く |
| `mashu deliver <id> <delivery>` | push と pull のあいだで動かす |
| `mashu bootstrap` | セッション開始時に渡る固定の塊とその token |
| `mashu incident --cause <c> --note` | 事故を原因つきで記録する。無引数で集計を読む（27.5） |

機械が打つ 3 つ。名前はこのリポジトリの外——MCP 設定と launch agent——との約束なので上に残してある。

| コマンド | 内容 |
|---|---|
| `mashu serve` | MCP Server を stdio で起動 |
| `mashu sweep` | hook が取り落とした transcript を台帳へ積む |
| `mashu work` | 抽出 worker を queue に対して走らせる |

保守・移植・評価。

| コマンド | 内容 |
|---|---|
| `mashu admin queue` | Review 待ちをセッション束ごとに並べる |
| `mashu admin approve <id>` / `--bundle <id>` | 承認。`--skip` で束から抜ける |
| `mashu admin reject <id> --reason` | 却下。理由は必須 |
| `mashu admin state <file> --scope <name>` | Current State を references 付きで提案する |
| `mashu admin evidence <id>` | 何に依拠しているか、何がそれに依拠しているか |
| `mashu admin retype <id> --to <type>` | Entity の type を訂正する |
| `mashu admin merge <id> --into <id>` | Entity を統合する |
| `mashu admin preview <query> --scope` | 上限をかけずに順位だけ見る（移植の検証用） |
| `mashu admin import <file.json> --scope` | JSON を candidate として取り込む |
| `mashu admin backfill` | 埋め込みを持たない行を後から埋める |
| `mashu admin enqueue` | transcript を抽出のために台帳へ積む |
| `mashu admin runs` | 捕捉台帳の生の行（`status` の元） |
| `mashu admin stale` | 自分で期限を名乗っている Active を洗い出す（25.2 の移行） |
| `mashu admin thresholds` | 類似度の分布を実在庫から測り直す（27.2） |
| `mashu admin eval-retire` | worker の退役洗い出しを marker と突き合わせる（27.4b） |
| `mashu admin migrate` | 未適用の migration を流す |

## Agent から使う

Claude Code へ登録する場合。

```bash
claude mcp add mashu --scope user --env MASHU_DATABASE_URL=dbname=mashu -- /path/to/mashu/.venv/bin/mashu serve --agent claude
```

公開するツールは 9 つ。

`session_bootstrap` / `memory_search` / `memory_get` / `scope_list` / `entity_resolve` / `memory_propose` / `scratch_put` / `scratch_get` / `context_put`

Agent 名は `--agent`、または環境変数 `MASHU_AGENT` で渡す。既定は `agent`。誰が書いたかは Commit Gate の判定に効く（Agent が述べた preference は Review を待つ）ので、Agent ごとに違う名前を渡す。

## 捕捉

書き込みは人か Agent が意識して動かしたときにしか起きない、という状態を畳むための層がある。

各 CLI の SessionEnd hook（`tools/session_end_hook.sh`）が transcript を台帳へ積むだけで返り、常駐 worker が後からそれを読んで Proposal を作る。hook で Model を走らせないのは、終了 hook の時間枠が数秒しかないためである。hook が取り落とした分は `mashu sweep` がディスク上の transcript と台帳を突き合わせて拾う。

worker の出力は**すべて Agent 由来として扱う**。transcript の user turn を読めても、それが保証するのは「どこに書かれていたか」であって「誰が書いたか」ではない。貼り付けられた文書の中の「常に X せよ」は span 検証を通ってしまう。User 由来の唯一の経路は `mashu remember`、すなわち人が打った事実である。

退役の提案も無人では落とさない。17 節が Auto Commit に置いている「単純な Task 完了」は人が「終わった」と言う場合の行であって、worker の推論はそこに乗らない。**worker が死んでも読み取り側は劣化しない**——期限つきの条件は read filter で消え、三層は既存の実装で動く。worker の死は「知識が増えない」に留まり、「嘘が返る」にはならない。

Scope は推測しない。`mashu route` で対応づけられていない作業ディレクトリの transcript は保留され、警告に載る。

```bash
uv run mashu route --add /path/to/project --scope <scope name>
```

```bash
uv run mashu work --limit 4
```

Model を呼ぶ部分は差し替えできる。`MASHU_EXTRACTOR` に `api`（`ANTHROPIC_API_KEY` が要る）、`cli:claude`、`cli:codex`、`auto`（既定）を渡す。仕様の第一選択は専用予算の小型モデルだが、鍵が無ければ対話 CLI の subprocess へ落ちる。

### 動かしつづける

`mashu work` は一回走って終わる。**それを呼ぶものが要る。** 無ければ結局「人が打ったときだけ書き込まれる」ままで、この層を作った理由が消える。

```bash
./tools/launchd/install.sh
```

これは `~/Library/LaunchAgents/` に置くだけで**読み込まない**。読み込むと、毎晩誰も見ていないところでモデルの枠を使いはじめる。その判断は枠の持ち主のものなので、コマンドは表示するが実行はしない。

セッション終了 hook（`tools/session_end_hook.sh`）を各 CLI の設定に登録すると、transcript が終わった時点で台帳に載る。登録しなくても `sweep` がディスクから拾うので、遅れるだけで落ちはしない。

同梱のユニットは 4:30 に走る。`StartCalendarInterval` は cron と違い、その時刻に機械が寝ていても発火を捨てず、次に目覚めたときに走る（複数回ぶん過ぎていても 1 回にまとめられる）。したがって夜間スリープする機械では、実際の実行はスリープ明けになる。夜のうちに走らせたければ `pmset` で wake を予約する。

一晩の費用は日次の入力予算で頭打ちにしてある（既定 40 万 token）。超えた分は捨てずに翌日へ回し、理由を台帳に書く。誰も読まなかった transcript は届かなかった知識であり、台帳はそれを「言うことが無かった夜」と区別できないためである。

抽出プロンプトの大きさは、そのセッションの記録に加えて **その Scope が Active として持つもの全件**で決まる。退役の洗い出しはクエリからは働かない（誰も検索しようと思わなかった Memory こそが間違ったまま残る）ので、この読み出しだけは順位も上限も持たない。結果として一晩の費用は在庫と一緒に伸びる。退役させることは知識状態の衛生であると同時に、運用費の対策でもある。

## 測る

```bash
uv run mashu status
```

仕様 27.3 の指標を、既に書かれている行から読み直す。捕捉が生きているか、queue がどれだけ遅れているか、Scope ごとに何割が未審査か、渡した文脈のうちタグ付きが何割か、セッション開始時の塊が上限に対していくらか。

**出所の無い指標は省かず名前を出す。** 答えられるものだけ並べると、失敗モードの半分を誰も見ていない系について「これで全部です」と読まれるためである。

```bash
uv run mashu incident --cause <cause> --note "何が起きたか"
```

事故は体感で判定しない（27.5）。記録するのは件数と原因で、原因ごとに戻る先が違う。二方向ある。`missed` は在ったのに届かなかった側で `bootstrap` / `pull` / `capture`、`stale` は覆ったものが現在値として届いた側で `filter` / `inventory` / `marker`。理由を書かない記録は拒む。後から原因に仕分けられない行は、原因を数えるという目的に寄与しない。

```bash
uv run mashu admin eval-retire --sample 5
```

worker の退役洗い出しを、人が述べた退役（marker）と突き合わせる（27.4b）。採点するのは、marker が **終了済みかつ抽出済み**のセッションに入っている場合だけ。まだ読まれていない log に対して「探して見つけられなかった」と報告すると、Recall がやっていない仕事の失敗として出る。

## 配置

```
docs/mashu-mvp.md              実装仕様書。設計判断とその理由の正本
docs/prompts/                  Session End Extraction のプロンプト
migrations/                    連番の SQL。mashu admin migrate が順に流す
src/mashu/                     実装
src/mashu/metrics.py           27.3 の指標。既存の行から読み直すだけで、計測機構を足さない
src/mashu/incidents.py         事故の記録と原因の切り分け（27.5）
src/mashu/thresholds.py        類似度分布の実測（27.2）。mashu admin thresholds が呼ぶ
tests/                         実 PostgreSQL に対して走る。harness/ は仕様 28 節のシナリオ
tools/session_end_hook.sh      SessionEnd hook。台帳へ積むだけで返る
tools/launchd/                 常駐ユニット。install.sh は置くだけで読み込まない
tools/condense_session.py      セッションログの圧縮
hooks/                         pre-commit / commit-msg
```

**設計の正本は `docs/mashu-mvp.md`** であり、コードはその実装である。仕様を変えずにコードだけ動かさない。撤回した設計は削除ではなく、何を・なぜ・どうなったら間違いだったと分かるかを本文に残す。

## 開発

```bash
uv run pytest -q
```

```bash
uv run ruff check src tests --fix && uv run ruff format src tests
```

テストは代用ではなく実際の PostgreSQL に対して走る。この層が約束していることの大半は制約・トリガ・トランザクション境界に載っているため、偽物のデータベースでは別のものをテストすることになる。テスト用データベースは実行ごとに作り直される（既定 `mashu_test`、`MASHU_TEST_DB` で変えられる）。

埋め込みはテスト中はハッシュによる代用に差し替わる。パイプラインの検査にモデルの重みを読み込む必要はなく、モデル自体の数値は仕様 27.2 で別に測る。
