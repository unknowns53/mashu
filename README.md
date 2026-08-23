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

**Review は「使えるようにする門」ではなく「品質を確定する門」である。** 未審査の候補も本文を渡す（タグ付きで）ので、Review が遅れても知識は使える。承認済みが 1 件も無い Scope も同じように答える。Layer 2 は合計 1500 token を上限とする（先頭の 1 件だけは超えても通す。当たったのに何も返さないより 1 件返すほうがよいという判断）。

セッション開始時に押し込まれる固定の塊は、type ではなく delivery で決まる。

- `startup_required` — 全セッションに押し込む
- `scope_required` — Scope が決まったら押し込む
- `pull_only` — 検索されたときだけ返す

delivery を上げる操作は admission control を通る。予算を超える変更は黙って削らず、拒否する。

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
uv run mashu migrate
```

`migrate` が `CREATE EXTENSION vector` から流すので、pgvector が入っていれば他の準備は要らない。接続先は既定で `dbname=mashu`、環境変数 `MASHU_DATABASE_URL` で変えられる。

fresh clone では git hook を入れる。

```bash
./hooks/install.sh
```

## 使う

| コマンド | 内容 |
|---|---|
| `mashu queue` | Review 待ちをセッション束ごとに並べる |
| `mashu show <id>` / `--bundle <id>` | 1 件、または束を 1 つの読み物として表示 |
| `mashu approve <id>` / `--bundle <id>` | 承認。`--skip` で束から抜ける |
| `mashu reject <id> --reason` | 却下。理由は必須 |
| `mashu search <query>` | 三層を通して引く |
| `mashu active --scope <name>` | その Scope が真として持つもの全件（退役の洗い出しの入力） |
| `mashu state <file> --scope <name>` | Current State を references 付きで提案する |
| `mashu evidence <id>` | 何に依拠しているか、何がそれに依拠しているか |
| `mashu retype <id> --to <type>` | Entity の type を訂正する |
| `mashu merge <id> --into <id>` | Entity を統合する |
| `mashu deliver <id> <delivery>` | push と pull のあいだで動かす |
| `mashu bootstrap` | セッション開始時に渡る固定の塊とその token |
| `mashu scope` | Scope ごとの採用済み・未審査の件数 |
| `mashu preview <query> --scope` | 上限をかけずに順位だけ見る（移植の検証用） |
| `mashu import <file.json> --scope` | JSON を candidate として取り込む |
| `mashu backfill` | 埋め込みを持たない行を後から埋める |
| `mashu serve` | MCP Server を stdio で起動 |

## Agent から使う

Claude Code へ登録する場合。

```bash
claude mcp add mashu --scope user --env MASHU_DATABASE_URL=dbname=mashu -- /path/to/mashu/.venv/bin/mashu serve --agent claude
```

公開するツールは 6 つ。

`session_bootstrap` / `memory_search` / `memory_get` / `scope_list` / `entity_resolve` / `memory_propose`

Agent 名は `--agent`、または環境変数 `MASHU_AGENT` で渡す。既定は `agent`。誰が書いたかは Commit Gate の判定に効く（Agent が述べた preference は Review を待つ）ので、Agent ごとに違う名前を渡す。

## 配置

```
docs/mashu-mvp.md              実装仕様書。設計判断とその理由の正本
docs/prompts/                  Session End Extraction のプロンプト
migrations/                    連番の SQL。mashu migrate が順に流す
src/mashu/                     実装
tests/                         実 PostgreSQL に対して走る。harness/ は仕様 28 節のシナリオ
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
