# Mashu(摩周)

複数の AI Agent が共有する、Version 管理された外部 Knowledge State。

Agent に長期記憶を持たせるのではなく、記憶を外に置く。Agent は毎回そこから読む。

*名前は北海道の摩周湖から。流入する川も流出する川もない閉じた水盆で、どの Agent にも所有されない外部の層のメタファーにした。*

設計判断とその理由は `docs/mashu-mvp.md` にある。この README は何があるかと、どう使うかだけを書く。

## 解決対象

- 古い情報が現在情報として利用される
- 終了済み Task が未完了として扱われる
- 棄却された仮説が再利用される
- Agent ごとに異なる認識状態になる
- 過去判断の理由が追跡できない

## 知識の形

```
Scope（作業の領域。作成は User のみ）
  └ Memory Entity（概念。type と title を持つ）
      └ Memory Version（内容。不変。status と由来を持つ）
```

いま有効な知識は `entity.active_version` が指す Version 一つだけ。Version は書き換えない。訂正するときは新しい Version を作って、active をそちらへ移す。

Version の status は 6 つ。

| status | 意味 |
|---|---|
| `candidate` | 提案されたが Review 未通過 |
| `superseded` | 新しい Version に置き換わった |
| `disproven` | 誤りだと判明した |
| `dormant` | 現在は使わないが将来再評価しうる |
| `rejected` | Proposal が却下された。誰も提案できない status |
| `completed` | Task が終わった |

## 書き込みの経路

Agent は知識を直接書けない。すべて Proposal として出し、Commit Gate が振り分ける。

| 区分 | 対象 |
|---|---|
| Auto Commit | User が明示した変更（type を問わない）、単純な Task 完了 |
| Candidate Commit | fact / interpretation / hypothesis / state、および User 明示でない preference |
| Human Review Required | Active の切替、Disproven 化、Restore、Merge、type の訂正、類似度超過での Entity 作成 |

preference が Candidate 側にあるのは、それが以後のセッションで従う指示だから。User が述べた preference は上の行に落ちて Auto Commit になる。

## 読み出しの経路

検索は三層で返る。

| 層 | 対象 | 渡すもの |
|---|---|---|
| Layer 1 Active | active_version | 本文 |
| Layer 2 Unreviewed | 未審査の candidate | 本文 + `unreviewed` タグ |
| Layer 3 Retired | superseded を除く退役 Version | title / status / reason のみ。本文は返さない |

Layer 1 の項目に退役の提案が出ているときは、本文と一緒に「誰が、どの status を、どんな理由で」提案しているかも返る。

未審査の候補もタグ付きで本文を渡すので、Review が遅れても知識は使える。Layer 2 の上限は合計 1500 token。先頭 1 件だけは超えても通す。

セッション開始時に押し込まれる塊は、type ではなく delivery で決まる。

| delivery | いつ渡すか |
|---|---|
| `startup_required` | 全セッションに押し込む |
| `scope_required` | Scope が決まったら押し込む |
| `pull_only` | 検索されたときだけ返す |

押し込むときに運ぶのは、その Version の directive（短形）があればそれ、無ければ本文全体。短形は `mashu directive` で後から書ける。書けるのは User だけ。

delivery を上げる操作は admission control を通る。上限は 2000 token で、超える変更は拒否される。測る対象はそのセッションが実際に受け取る塊で、startup と Scope の両方を足したもの。

## 必要なもの

- PostgreSQL と [pgvector](https://github.com/pgvector/pgvector)（HNSW を使う）。PostgreSQL 17.11 / pgvector 0.8.6 で動かしている
- Python 3.13、[uv](https://docs.astral.sh/uv/)
- 埋め込みモデル `intfloat/multilingual-e5-large`（1024 次元）

## 用意する

```bash
uv sync --extra embed --extra mcp
createdb mashu
uv run mashu admin migrate
```

`migrate` が `CREATE EXTENSION vector` から流す。pgvector が入っていれば他の準備は要らない。接続先は既定で `dbname=mashu`、`MASHU_DATABASE_URL` で変えられる。

fresh clone では git hook を入れる。

```bash
./hooks/install.sh
```

## 使う

毎日の面。

| コマンド | 内容 |
|---|---|
| `mashu status` | 捕捉が生きているか、queue がどれだけ遅れているか、開始時の塊がいくらか |
| `mashu review` | 最古の束を開き、一括承認・却下・編集・明示の先送りまで 1 回で終える |
| `mashu find <query>` | 三層を通して引く |
| `mashu inspect <id>` / `--bundle <id>` | 1 件、または束を 1 つの読み物として表示 |
| `mashu remember <body>` | User が述べた知識を記録する。Review を待たず active になる |
| `mashu retire <id> <status> --reason` | 終わった・覆ったことを User が直接記録する |
| `mashu active --scope <name>` | その Scope が真として持つもの全件 |
| `mashu scope` | Scope ごとの採用済み・未審査の件数 |
| `mashu scope --add <name> --about <line>` | Scope を作る。`--route` で対応づけまで一手 |
| `mashu route --add <path> --scope <name>` | 作業ディレクトリを Scope に対応づける |
| `mashu route --ignore <path>` | そのディレクトリは捕捉しないと決める |
| `mashu directive <id> <short>` | 押し込むときに渡る短形を書く |
| `mashu deliver <id> <delivery>` | push と pull のあいだで動かす |
| `mashu bootstrap` | セッション開始時に渡る塊とその token |
| `mashu incident --cause <c> --note` | 事故を原因つきで記録する。無引数で集計を読む |

機械が打つ 3 つ。名前は MCP 設定と launch agent との約束なので、上に置いてある。

| コマンド | 内容 |
|---|---|
| `mashu serve` | MCP Server を stdio で起動 |
| `mashu sweep` | hook が取り落とした transcript を台帳へ積む |
| `mashu work` | 抽出 worker を queue に対して走らせる |

保守・移植・評価は `admin` の下。Review を決めている最中には読まないので分けてある。

| コマンド | 内容 |
|---|---|
| `mashu admin queue` | Review 待ちをセッション束ごとに並べる |
| `mashu admin approve <id>` / `--bundle <id>` | 承認。`--skip` で束から抜ける |
| `mashu admin reject <id> --reason` | 却下。理由は必須 |
| `mashu admin state <file> --scope <name>` | Current State を references 付きで提案する |
| `mashu admin evidence <id>` | 何に依拠しているか、何がそれに依拠しているか |
| `mashu admin retype <id> --to <type>` | Entity の type を訂正する |
| `mashu admin merge <id> --into <id>` | Entity を統合する |
| `mashu admin preview <query> --scope` | 上限をかけずに順位だけ見る |
| `mashu admin import <file.json> --scope` | JSON を candidate として取り込む |
| `mashu admin backfill` | 埋め込みを持たない行を後から埋める |
| `mashu admin enqueue` | transcript を抽出のために台帳へ積む |
| `mashu admin runs` | 捕捉台帳の生の行（`status` の元） |
| `mashu admin stale` | 自分で期限を名乗っている Active を洗い出す |
| `mashu admin thresholds` | 類似度の分布を実在庫から測り直す |
| `mashu admin eval-retire` | worker の退役洗い出しを marker と突き合わせる |
| `mashu admin migrate` | 未適用の migration を流す |

移った名前を打つと行き先が出る。`search` は `find`、`queue` は `admin queue`。

## Agent から使う

Claude Code へ登録する場合。

```bash
claude mcp add mashu --scope user --env MASHU_DATABASE_URL=dbname=mashu -- /path/to/mashu/.venv/bin/mashu serve --agent claude
```

公開するツールは 9 つ。

`session_bootstrap` / `memory_search` / `memory_get` / `scope_list` / `entity_resolve` / `memory_propose` / `scratch_put` / `scratch_get` / `context_put`

Agent 名は `--agent` か環境変数 `MASHU_AGENT` で渡す。既定は `agent`。誰が書いたかは Commit Gate の判定に効くので、Agent ごとに違う名前にする。

## 捕捉

放っておくと、書き込みは人か Agent が意識して動かしたときにしか起きない。それを畳むための層。

各 CLI の SessionEnd hook（`tools/session_end_hook.sh`）が transcript を台帳へ積むだけで返る。常駐 worker が後からそれを読んで Proposal を作る。hook 側でモデルを走らせないのは、終了 hook の時間枠が数秒しかないから。hook が取り落とした分は `mashu sweep` がディスクと台帳を突き合わせて拾う。

worker の出力はすべて Agent 由来として扱う。User 由来の唯一の経路は `mashu remember`、つまり人が打った事実。

退役の提案も無人では落ちない。worker が死んでも読み取り側は劣化しない。知識が増えなくなるだけで、嘘は返らない。

Scope は推測しない。`mashu route` で対応づけられていない作業ディレクトリの transcript は保留され、`status` の警告に載る。

```bash
uv run mashu route --add /path/to/project --scope <scope name>
uv run mashu work --limit 4
```

### モデルの差し替え

`MASHU_EXTRACTOR` に `api`（`ANTHROPIC_API_KEY` が要る）、`cli:codex`、`cli:claude`、`auto`（既定）を渡す。`auto` は鍵があれば API、無ければ CLI へ落ちる。CLI のうちは codex を先に試す。

どちらの CLI を使うかで、費用と、その費用がどの枠に乗るかが変わる。このリポジトリを触っている CLI とは別の CLI へ回せば、鍵が無くても抽出を会話とは別の枠に置ける。

モデルは `MASHU_EXTRACTOR_MODEL` で指定できる。省略時の既定は CLI ごとに違う。

| extractor | 既定のモデル |
|---|---|
| `cli:codex` | `gpt-5.6-luna` |
| `cli:claude` | `claude-haiku-4-5` |
| `api` | `claude-haiku-4-5` |

### 動かしつづける

`mashu work` は一回走って終わる。それを呼ぶものが要る。

```bash
./tools/launchd/install.sh
```

これは `~/Library/LaunchAgents/` に置くだけで読み込まない。読み込むと毎晩モデルの枠を使いはじめるので、その判断は枠の持ち主に残してある。読み込むコマンドは最後に表示する。

セッション終了 hook を各 CLI の設定に登録すると、transcript が終わった時点で台帳に載る。登録しなくても `sweep` がディスクから拾う。遅れるだけで落ちはしない。

同梱のユニットは 4:30 に走る。`StartCalendarInterval` は cron と違って、その時刻に機械が寝ていても発火を捨てない。次に目覚めたときに走る。複数回ぶん過ぎていても 1 回にまとまる。夜間スリープする機械では実行がスリープ明けになるので、夜のうちに走らせたければ `pmset` で wake を予約する。

### 一晩の費用

日次の入力予算で頭打ちにしてある。既定は 40 万 token。超えた分は捨てずに翌日へ回し、理由を台帳に書く。

抽出プロンプトの大きさは、そのセッションの記録に加えて、その Scope が Active として持つもの全件で決まる。退役の洗い出しはクエリからは働かない。誰も検索しようと思わなかった Memory こそが間違ったまま残るので、この読み出しだけは順位も上限も持たない。

結果として一晩の費用は在庫と一緒に伸びる。退役させることは知識状態の衛生であり、同時に運用費の対策でもある。

## 測る

```bash
uv run mashu status
```

捕捉が生きているか、queue がどれだけ遅れているか、Scope ごとに何割が未審査か、渡した文脈のうちタグ付きが何割か、セッション開始時の塊が上限に対していくらか。

出所の無い指標は省かず、名前だけ出す。答えられるものだけ並べると、見ていない失敗モードまで込みで「これで全部」に読める。

```bash
uv run mashu incident --cause <cause> --note "何が起きたか"
```

事故は体感で判定しない。件数と原因で記録する。原因は二方向にある。

| kind | cause | 意味 |
|---|---|---|
| `missed` | `bootstrap` | 押し込むべきものが入っていなかった |
| `missed` | `pull` | 索引はあったが引きに行かなかった |
| `missed` | `capture` | そもそも書かれていなかった |
| `stale` | `filter` | 期限つきの条件が窓を越えて生き残った |
| `stale` | `inventory` | 寿命のある規則が indefinite 側に書かれていた |
| `stale` | `marker` | User が「終わった」と言ったのを拾えなかった |

理由を書かない記録は拒む。後から原因に仕分けられない行では、原因を数えられない。

```bash
uv run mashu admin eval-retire --sample 5
```

worker の退役洗い出しを、人が述べた退役（marker）と突き合わせる。採点するのは marker が終了済みかつ抽出済みのセッションに入っている場合だけ。まだ読まれていない log を「探して見つけられなかった」と数えると、やっていない仕事が失敗として出る。

## 配置

```
docs/mashu-mvp.md              実装仕様書。設計判断とその理由の正本
docs/prompts/                  Session End Extraction のプロンプト
migrations/                    連番の SQL。mashu admin migrate が順に流す
src/mashu/                     実装
src/mashu/metrics.py           指標。既存の行から読み直すだけで、計測機構を足さない
src/mashu/incidents.py         事故の記録と原因の切り分け
src/mashu/thresholds.py        類似度分布の実測
tests/                         実 PostgreSQL に対して走る。harness/ は仕様 28 節のシナリオ
tools/session_end_hook.sh      SessionEnd hook。台帳へ積むだけで返る
tools/launchd/                 常駐ユニット。install.sh は置くだけで読み込まない
tools/condense_session.py      セッションログの圧縮
hooks/                         pre-commit / commit-msg
```

設計の正本は `docs/mashu-mvp.md` で、コードはその実装。仕様を変えずにコードだけ動かさない。撤回した設計は削除せず、何を・なぜ・どうなったら間違いだったと分かるかを本文に残す。

## 開発

```bash
uv run pytest -q
uv run ruff check src tests --fix && uv run ruff format src tests
```

テストは実際の PostgreSQL に対して走る。この層の約束の大半は制約・トリガ・トランザクション境界に載っているので、偽物のデータベースでは別のものを試すことになる。テスト用データベースは実行ごとに作り直される。既定は `mashu_test`、`MASHU_TEST_DB` で変えられる。

埋め込みはテスト中はハッシュによる代用に差し替わる。パイプラインの検査にモデルの重みは要らない。モデル自体の数値は `mashu admin thresholds` で別に測る。
