# Mashu（摩周）

Mashu は、複数の AI Agent が共有する外部 Knowledge State だ。知識は Version ごとに管理する。

Agent の長期記憶は外に置く。Agent は各セッションで Mashu から知識を読む。

名前は北海道の摩周湖に由来する。設計判断とその理由は [`docs/mashu-mvp.md`](docs/mashu-mvp.md) にまとめている。この README では、機能と使い方を説明する。

## 解決対象

- 古い情報が現在情報として利用される
- 終了済み Task が未完了として扱われる
- 棄却された仮説が再利用される
- Agent ごとに認識状態が分かれる
- 過去の判断理由を追跡できない

## 知識の形

```
Scope（作業の領域。作成は User のみ）
  └ Memory Entity（概念。type と title を持つ）
      └ Memory Version（内容。不変。status と由来を持つ）
```

有効な知識は `entity.active_version` が指す Version 一つだけ。Version は書き換えない。訂正時は新しい Version を作り、active を新しい Version に移す。

Version の status は 6 種類ある。

| status | 意味 |
|---|---|
| `candidate` | 提案済みだが Review 未通過 |
| `superseded` | 新しい Version に置き換わった |
| `disproven` | 誤りだと判明した |
| `dormant` | 現在は使わないが、将来再評価できる |
| `rejected` | Review が通さなかった。Agent からは提案できない status |
| `completed` | Task が終わった |

上は **Version の status** である。Proposal 自身の status は `pending` / `approved` / `declined` / `auto_committed` で、別の語彙を使う。**Version の `rejected` は「その内容は通らなかった」という知識の側の読み**で Layer 3 から返り、**Proposal の `declined` は「その提案は決着した」という手続きの記録**で Review の列に出る。v0.13 まで後者も `rejected` を名乗っており、どちらの表を見ているか分からないと意味が定まらなかった。

## 書き込み

Agent は知識を直接書けない。すべて Proposal として出し、Commit Gate が処理する。

| 区分 | 対象 |
|---|---|
| Auto Commit | User が明示した変更（type を問わない）、単純な Task 完了 |
| Candidate Commit | fact、interpretation、hypothesis、state、および User が明示していない preference |
| Human Review Required | Active の切替、Disproven 化、Restore、Merge、type の訂正、類似度超過時の Entity 作成 |

User が述べた preference は Auto Commit になる。それ以外の preference は Candidate Commit になる。

## 読み出し

検索結果は三層で返る。

| 層 | 対象 | 渡すもの |
|---|---|---|
| Layer 1 Active | active_version | 本文 |
| Layer 2 Unreviewed | 未審査の candidate | 本文と `unreviewed` タグ |
| Layer 3 Retired | superseded を除く退役 Version | title、status、reason。本文は返さない |

Layer 1 の項目に退役の提案があるときは、本文と一緒に提案者、status、理由も返る。

未審査の候補もタグ付きで本文を渡す。Review が遅れても知識を使える。Layer 2 の上限は合計 1500 token。先頭 1 件だけは上限を超えても通す。

セッション開始時に渡す内容は delivery で決まる。type は使わない。

| delivery | いつ渡すか |
|---|---|
| `startup_required` | 全セッションに渡す |
| `scope_required` | Scope が決まったら渡す |
| `pull_only` | 検索されたときだけ返す |

渡す内容は、その Version の directive（短形）があれば directive、なければ本文全体。短形は `mashu directive` で後から書ける。書けるのは User だけ。

delivery を上げる操作は admission control を通る。上限は 2000 token。超える変更は拒否される。測定対象は、そのセッションが実際に受け取る内容だ。startup と Scope の内容を合算する。

## 必要なもの

- PostgreSQL と [pgvector](https://github.com/pgvector/pgvector)（HNSW を使う）。PostgreSQL 17.11 と pgvector 0.8.6 で動作確認済み
- Python 3.13 と [uv](https://docs.astral.sh/uv/)
- 埋め込みモデル `intfloat/multilingual-e5-large`（1024 次元）

## 用意する

```bash
uv sync --extra embed --extra mcp
createdb mashu
uv run mashu admin migrate
```

`migrate` は `CREATE EXTENSION vector` から実行する。pgvector が入っていれば、追加の準備は要らない。接続先の既定値は `dbname=mashu`。`MASHU_DATABASE_URL` で変更できる。

新しく clone したら git hook を入れる。

```bash
./hooks/install.sh
```

## 使う

| コマンド | 内容 |
|---|---|
| `mashu status` | 捕捉の稼働状態、queue の遅延、開始時の内容を表示 |
| `mashu review` | 待っている束を古い順に開き、キー 1 打で承認、却下、編集、先送り |
| `mashu find <query>` | 三層を通して検索 |
| `mashu inspect <id>` / `mashu inspect --bundle <id>` | 1 件または束を表示 |
| `mashu remember <body>` | User が述べた知識を記録。Review を待たず active になる |
| `mashu retire <id> {completed,disproven,dormant} --reason <reason>` | 完了や反証を User が直接記録 |
| `mashu active --scope <name>` | Scope が真として持つものをすべて表示 |
| `mashu scope` | Scope ごとの採用済み件数と未審査件数を表示 |
| `mashu scope --add <name> --about <line>` | Scope を作成。`--route` で対応づけも実行 |
| `mashu route --add <path> --scope <name>` | 作業ディレクトリを Scope に対応づける |
| `mashu route --ignore <path>` | そのディレクトリを捕捉対象から外す |
| `mashu directive <id> <short>` | 渡す短形を書く |
| `mashu deliver <id> <delivery>` | push と pull の間で切り替える |
| `mashu bootstrap` | セッション開始時に渡す内容と token を表示 |
| `mashu incident --cause <cause> --note <note>` | 事故を原因つきで記録。無引数で集計を表示 |

### Review の進め方

`mashu review` は待っている束を順に開く。矢印で読み、キー 1 打で決める。

```
mashu admin queue                 待っている束の一覧
mashu inspect --bundle <id>       束の中身を読む（何も変わらない）
mashu review                      待っている束を古い順に開く
mashu review --bundle <id>        その束だけ
```

最初に出るのは束の目次で、題名が 1 行ずつ並ぶ。ここで `a` を押せば読まずに束ごと通る。仕様 18.1 節が普通はこれで済むと見ている形。

| 目次で | 意味 |
|---|---|
| `↑` `↓` | 選ぶ項目を動かす |
| `⏎` | その項目を開く |
| `a` | 束の未決を全部承認 |
| `s` | 束ごと先送り。理由を聞かれる |
| `q` | 抜ける |

| 項目を開いた先で | 意味 |
|---|---|
| `y` | 承認して次へ |
| `r` | 却下。理由を聞かれる |
| `e` | 本文をエディタで直し、自分の名義で承認 |
| `s` | 先送り。理由を聞かれる |
| `↑` `↓` | 決めずに前後へ |
| `←` | 目次へ戻る |
| `a` | 残り全部を承認 |
| `q` | 抜ける |

決めた分はその場で確定するので、途中で `q` を押しても手前は残る。次の `review` は残りから開く。1 つの束が終わると自動で次の束へ進む。

先送りした項目は次の `review` に出てこない。`--all` で戻る。

端末が無いところ（パイプ、スクリプト、テスト）では束を一気に印字して `--batch` の文字列で決める。

```
mashu review --bundle <id> --batch "all; r 2 根拠が薄い; s 3 明日確かめる"
```

| `--batch` に書くもの | 意味 |
|---|---|
| `all` | この束の未決を全部承認 |
| `r N 理由` | N 番を却下。理由は必須 |
| `e N` | N 番を直して承認 |
| `s N 理由` | N 番を先送り。理由は必須 |
| `q` | 何も決めない |

`;` で区切って一度に渡せる。`all` は他の指定と併せると「残り全部」の意味になるので、`r 3 古い; all` は 3 番だけ却下して残りを承認する。

### Review を通さずに書く

`remember` と `retire` は User 発話が出どころなので Auto Commit で反映される。仕様 17 節。

```
mashu remember <本文> --scope <名前> --type <型> --title <題>
mashu remember <本文> --until 5d
mashu retire <id> completed --reason <理由>
```

`--scope --type --title` は必須で、`--until` を付けたときだけ三つとも不要になる。`--until` で書いたものは Temporary Context になり、Review も退役も要らず期限で消える。仕様 25.2 節。`--kind` は既定が `fact` で、規律として書くなら `preference` を渡す。

### 機械が実行する操作

次の 3 つを MCP 設定と launch agent から呼ぶ。

| コマンド | 内容 |
|---|---|
| `mashu serve` | MCP Server を stdio で起動 |
| `mashu sweep` | hook が取り落とした transcript を台帳へ積む |
| `mashu work` | 抽出 worker を queue に対して実行 |

### 保守、移植、評価

保守、移植、評価の操作は `admin` の下にある。

| コマンド | 内容 |
|---|---|
| `mashu admin queue` | Review 待ちをセッション束ごとに表示 |
| `mashu admin approve <id>` / `mashu admin approve --bundle <id>` | 承認。`--skip` で束から外す |
| `mashu admin reject <id> --reason <reason>` | 却下。理由は必須 |
| `mashu admin state <file> --scope <name>` | references 付きの Current State を提案 |
| `mashu admin evidence <id>` | 依拠先と依拠している項目を表示 |
| `mashu admin retype <id> --to <type> --reason <reason>` | Entity の type を訂正 |
| `mashu admin merge <id> --into <id> --reason <reason>` | Entity を統合 |
| `mashu admin preview <query> --scope <name>` | 上限なしで順位だけ表示 |
| `mashu admin import <file.json> --scope <name>` | JSON を candidate として取り込む |
| `mashu admin backfill` | 埋め込みがない行を後から埋める |
| `mashu admin enqueue` | transcript を抽出用に台帳へ積む |
| `mashu admin runs` | 捕捉台帳の生の行（`status` の元）を表示 |
| `mashu admin stale` | 自分で期限を持つ Active を抽出 |
| `mashu admin thresholds` | 実在庫から類似度分布を測定 |
| `mashu admin eval-retire` | worker の退役候補を marker と照合 |
| `mashu admin migrate` | 未適用の migration を実行 |

移行前の名前を実行すると、移行先が表示される。`search` は `find`、`queue` は `admin queue` に移行した。

## Agent から使う

Claude Code に登録する場合は次を実行する。

```bash
claude mcp add mashu --scope user --env MASHU_DATABASE_URL=dbname=mashu -- /path/to/mashu/.venv/bin/mashu serve --agent claude
```

ツールを 9 つ公開する。

`session_bootstrap` / `memory_search` / `memory_get` / `scope_list` / `entity_resolve` / `memory_propose` / `scratch_put` / `scratch_get` / `context_put`

Agent 名は `--agent` または環境変数 `MASHU_AGENT` で渡す。既定値は `agent`。Agent ごとに異なる名前を設定する。

## 捕捉

書き込みは、人または Agent が明示的に操作したときだけ発生する。捕捉層はセッション終了後の記録を処理する。

各 CLI の SessionEnd hook（`tools/session_end_hook.sh`）は transcript を台帳へ積んで終了する。常駐 worker が後から読み、Proposal を作る。hook 側ではモデルを実行しない。hook が取り落とした分は `mashu sweep` がディスクと台帳を照合して拾う。

worker の出力はすべて Agent 由来として扱う。User 由来の経路は `mashu remember` だけである。

worker が停止しても読み取りは続く。停止中は新しい Proposal が増えない。

worker は Scope を推測しない。`mashu route` で対応づけていない作業ディレクトリの transcript は保留され、`status` の警告に載る。

```bash
uv run mashu route --add /path/to/project --scope <scope name>
uv run mashu work --limit 4
```

### モデルの差し替え

`MASHU_EXTRACTOR` には `api`、`cli:codex`、`cli:claude`、`auto`（既定）を指定できる。`api` には `ANTHROPIC_API_KEY` が必要。`auto` は鍵があれば API、なければ CLI を使う。CLI では codex を先に試す。

CLI の選択で費用と請求枠が変わる。別の CLI に処理を回せば、鍵がなくても抽出費用を会話とは別の枠に置ける。

モデルは `MASHU_EXTRACTOR_MODEL` で指定できる。省略時の既定値は CLI ごとに異なる。

| extractor | 既定のモデル |
|---|---|
| `cli:codex` | `gpt-5.6-luna` |
| `cli:claude` | `claude-haiku-4-5` |
| `api` | `claude-haiku-4-5` |

### 動かし続ける

`mashu work` は 1 回で終了する。定期的に実行するユニットが必要になる。

```bash
./tools/launchd/install.sh
```

このスクリプトは `~/Library/LaunchAgents/` にユニットを置くだけで、読み込まない。読み込むと毎晩モデルの枠を使い始める。読み込み用のコマンドは最後に表示される。

同梱のユニットは 4:30 に動く。`StartCalendarInterval` は cron と異なり、指定時刻に Mac がスリープしていても発火を捨てない。起床時に動く。複数回分の実行時刻を過ぎていても 1 回にまとまる。夜間スリープする Mac で夜中に動かす場合は、`pmset` で wake を予約する。

セッション終了 hook を各 CLI の設定に登録すると、transcript の終了時に台帳へ載る。登録しなくても `sweep` がディスクから拾う。反映が遅れるだけで、記録は失われない。

### 一晩の費用

日次の入力予算を設定している。既定値は 40 万 token。超過分は捨てず、翌日に回して理由を台帳へ書く。

抽出プロンプトの大きさは、セッションの記録と、その Scope が Active として持つ全項目で決まる。退役の洗い出しでは、Scope の Active 全件を順位も上限も付けずに読み込む。

一晩の費用は在庫とともに増える。退役は知識状態の管理と費用の抑制に関わる。

## 測る

```bash
uv run mashu status
```

`mashu status` は捕捉の稼働状態と queue の遅延を表示する。Scope ごとの未審査の割合、渡した文脈に占めるタグ付き項目の割合、セッション開始時の内容と上限の差も確認できる。

出所のない指標は、値を表示せず名前だけ表示する。

```bash
uv run mashu incident --cause <cause> --note "何が起きたか"
```

事故は件数と原因で記録する。原因は次の 2 種類に分かれる。

| kind | cause | 意味 |
|---|---|---|
| `missed` | `bootstrap` | 渡すべき項目が入っていなかった |
| `missed` | `pull` | 索引はあったが検索しなかった |
| `missed` | `capture` | そもそも書かれていなかった |
| `stale` | `filter` | 期限つきの条件が期限を越えて残った |
| `stale` | `inventory` | 寿命のある規則が indefinite 側に書かれていた |
| `stale` | `marker` | User の完了宣言を拾えなかった |

理由のない記録は受け付けない。

```bash
uv run mashu admin eval-retire --sample 5
```

worker の退役候補を、人が述べた退役（marker）と照合する。採点対象は、marker が終了済みかつ抽出済みのセッションに入っている場合だけ。まだ読まれていない log を失敗として数えない。

## 切替

CLI が持っている元の記憶機構を止めて、Mashu だけで 2 週間運用する。この切替は Mashu の機能ではない。**別のプログラムの設定を変える作業**であり、Mashu 側にできるのは窓の開始を記録して、その中で起きた事故を数えることだけである。

手順は四つ。

**① 自動書き込みを止める。** CLI が会話から勝手に記憶を書き足す機能を切る。Claude Code なら `~/.claude/settings.json` の `autoMemoryEnabled` を `false` にする。

**② 古い記憶層を指す指示を外す。** 自動書き込みを切っても、CLI の常設指示（Claude Code の `CLAUDE.md`、Codex の `AGENTS.md`）が「memory ディレクトリを読め」と書いていれば Agent はそこを読む。その記述を外す。**①だけでは切り替わらない。**

**③ 窓を開ける。**

```bash
uv run mashu trial --open --note "何を止めたか"
```

**④ 窓の中で起きた事故を記録する。** `mashu incident` で、原因つきで書く。判定は体感でしない。「Mashu に Active として在る内容を Agent が取得できないまま作業し、誤った前提で進んだ」場合を 1 件と数える。

読むときと閉じるとき。

```bash
uv run mashu trial            # 開いてからの日数と、窓の中の事故
uv run mashu trial --close --note "2 週間経過"
```

窓を開けずに数えた件数は、いつからの件数か言えない。集計の意味は窓が与える。

## 配置

```
docs/mashu-mvp.md              実装仕様書。設計判断とその理由の正本
docs/prompts/                  Session End Extraction のプロンプト
migrations/                    連番の SQL。mashu admin migrate が順に実行
src/mashu/                     実装
src/mashu/metrics.py           指標。既存の行を読み直すだけで、計測機構を追加しない
src/mashu/incidents.py         事故の記録と原因の切り分け
src/mashu/thresholds.py        類似度分布の実測
tests/                         実 PostgreSQL に対して実行。harness/ は仕様 28 節のシナリオ
tools/session_end_hook.sh      SessionEnd hook。台帳へ積むだけで終了
tools/launchd/                 常駐ユニット。install.sh は置くだけで読み込まない
tools/condense_session.py      セッションログの圧縮
hooks/                         pre-commit / commit-msg
```

仕様を変える場合は、コードと `docs/mashu-mvp.md` を一緒に更新する。撤回した設計は削除しない。何を撤回したか、理由、誤りと分かる条件を仕様書に残す。

## 開発

```bash
uv run pytest -q
uv run ruff check src tests --fix && uv run ruff format src tests
```

テストは実 PostgreSQL に対して実行する。テスト用データベースは実行ごとに作り直す。既定値は `mashu_test`。`MASHU_TEST_DB` で変更できる。

テスト中の埋め込みはハッシュで代用する。モデル自体の数値は `mashu admin thresholds` で測定する。
