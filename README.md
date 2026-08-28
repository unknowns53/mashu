# Mashu（摩周）

Mashu は、複数の AI Agent が共有する外部 Knowledge State だ。v2 の入口はひとつしかない。**忘れたことに実際の損害が出た、と実証されたものだけが知識になる。**「覚えておくと便利そう」なものは入らない。

名前は北海道の摩周湖に由来する。設計判断とその理由は [`docs/mashu-v2.md`](docs/mashu-v2.md) にまとめている。v1 の設計と撤回の記録は [`docs/mashu-mvp.md`](docs/mashu-mvp.md) が保持する。この README は機能と使い方を説明する。

## 仕組み

```
痛み（事故・調べ直し）── pain_report ──▶ 事故台帳（ledger）
調べて分かったこと ──── trace_put ────▶ 痕跡（trace、30 日で失効）
                                          │ 照合（pg_trgm）
                                          ▼
                                   昇格候補（nomination）
                                          │ 人が 1 キーで確定
                                          ▼
                                   記憶（memory）── 全量 push ──▶ 各セッション
```

- **実証は三つ。** 事故（誤った作業が出た）は 1 回で、再導出（同じことを調べ直した）は 2 回目で昇格候補になる。User の明示（`mashu remember`）は即時に active になる
- **再導出の 1 回目は痛みとして自覚されない。** だから走行中の Agent は調べて分かったことを `trace_put` で一行残す。痕跡は知識ではなく、Review も配信もされず 30 日で失効する。2 回目の `pain_report` がそれと照合されたとき、初めて「二度目」が証明される
- **読む経路はすべて push。** 検索で知識を返すツールは無い。在庫には定員（既定 2000 token、うち always 層は 800 token）があり、満席での昇格は退役か格下げとセットでないと通らない。定員があるから全量 push が成立する
- **Agent の書き込みが、人の確定なしに他のセッションへ届く経路は存在しない**

## 配信の三経路

| delivery | 誰に | いつ |
|---|---|---|
| `always` | 全セッション | 開始時（bootstrap） |
| `scope` | route が当たるセッション | 開始時（bootstrap） |
| `guard:<action>` | その行為に至ったセッション | 行為の直前（PreToolUse フック） |

scope は「どこで」を絞り、guard は「いつ」を絞る。guard は該当ツールの呼び出しを一度拒否して留めた内容を突きつけ、読んだうえで同じ判断ならもう一度呼べば通る。発火はセッションにつき行為ごとに一度。

## 必要なもの

- PostgreSQL と pg_trgm（標準の contrib。PostgreSQL 17 で動作確認）
- Python 3.11+ と [uv](https://docs.astral.sh/uv/)

埋め込みモデルは使わない。照合はすべてトライグラム類似で行う。

## 用意する

```bash
uv sync --extra mcp
createdb mashu
uv run mashu admin migrate
```

接続先の既定値は `dbname=mashu`。`MASHU_DATABASE_URL` で変更できる。新しく clone したら git hook を入れる。

```bash
./hooks/install.sh
```

どこからでも `mashu` と打てるようにするには、PATH の通ったところへ symlink を張る。

```bash
ln -s /path/to/mashu/.venv/bin/mashu ~/.local/bin/mashu
```

## 使う

| コマンド | 内容 |
|---|---|
| `mashu status` | 在庫と定員、pending 件数、台帳と痕跡の状況 |
| `mashu review [--all]` | 昇格候補の一覧から 1 件を開き、根拠の台帳エントリと並べて 1 キーで確定・却下・保留 |
| `mashu remember <body>` | User 明示。即時 active。唯一の即時経路 |
| `mashu remember <body> --until 5d` | 期限つき条件（Temporary Context）。Review 不要、期限で消える |
| `mashu retire <id> --reason <r>` | 退役。以後は照合で「何が、なぜ否定されたか」だけ返る |
| `mashu revise <id>` | 本文の改訂（User のみ）。改訂履歴が残る |
| `mashu show <id>` | 記憶・候補・台帳エントリを 1 件、全文で表示。根拠の台帳と改訂履歴、台帳なら採用先も出る |
| `mashu memories [--scope <name>] [--retired]` | 記憶の一覧。既定は active、`--retired` で退役分と理由 |
| `mashu pain --kind {incident,friction} --what <w> --prevention <p>` | 痛みの手動記録 |
| `mashu ledger` | 台帳の閲覧 |
| `mashu trace [query]` | 痕跡の閲覧と検索 |
| `mashu guard <action> [--pin <id>] [--unpin <id>]` | 行為の門への留めつけ・照会 |
| `mashu deliver <id> {always,scope,guard}` | 配信経路の変更 |
| `mashu scope [--add <name> --about <line>]` | Scope 台帳（作成は User のみ） |
| `mashu route [--add <path> --scope <name>] [--ignore <path>]` | 作業ディレクトリと Scope の対応 |
| `mashu bootstrap` | このディレクトリのセッションが受け取る内容と token |
| `mashu admin migrate` | 未適用の migration を実行 |

`mashu review` は二画面。待っている候補の一覧（↑↓ / j k で移動、⏎ で開く）と、1 件の全文・token 見積り・根拠の台帳エントリを並べた個別画面（`←` で一覧へ戻る）である。個別画面のキーは `y` 確定、`e` エディタで本文を直してから確定、`r` 理由を付けて却下、`s` 理由を付けて保留、`space` で続きを読む、`?` でキーの説明、`q` で退出。

確定するとき delivery を選ぶ（空 Enter で既定、`g ACTION` で行為の門）。件数は週数件のオーダーなので、1 件ごとに人が置き場を決める。まとめて承認するキーは置いていない。

決定は 1 件ずつその場で確定するので、途中で `q` を押しても後ろは残り、次の `mashu review` は残りから始まる。`s` の保留は決定ではなく、pending のまま理由と一緒に脇へ置くだけで、`mashu review --all` と `mashu review --list --all` で戻ってくる。

id を取る引数はどれも、一覧が表示する短縮 ID（先頭 8 文字）をそのまま受け付ける。4 文字以上の前方一致で一意に決まればよく、複数に当たったときは候補を並べて拒否する。

## Agent から使う

Claude Code に登録する場合。

```bash
claude mcp add mashu --scope user --env MASHU_DATABASE_URL=dbname=mashu -- /path/to/mashu/.venv/bin/mashu serve --agent claude
```

MCP ツールは 6 つ。

| Tool | 役割 |
|---|---|
| `session_bootstrap` | セッション開始時に一度。always と現在 Scope の記憶、期限つき条件、pending 件数 |
| `pain_report` | 痛みを台帳へ記録し、類似の台帳エントリ・痕跡・退役理由を返す。二度目なら候補を生成 |
| `trace_put` | 調べて分かったことを一行残す |
| `trace_search` | 痕跡の検索。日付つき・未検証の印で返る |
| `memory_list` | 指定 Scope の active な記憶の列挙 |
| `memory_nominate` | 会話中の User の記録指示を候補として運ぶ。pending 止まりで、確定は人 |

期限つき条件（Temporary Context）を書けるのは User だけ（`mashu remember --until`）。Agent が観測した期限つきの条件は `trace_put` で痕跡に残す。

Agent 名は `--agent` または環境変数 `MASHU_AGENT` で渡す。

### 行為の門を張る

`tools/pretooluse_guard.py` を PreToolUse フックに入れると、留めた記憶が該当ツールの実行直前に出る。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Task|Agent|mcp__codex-async__codex_start",
        "hooks": [{"type": "command", "command": "/path/to/mashu/tools/pretooluse_guard.py"}]
      }
    ]
  }
}
```

蔵に届かないときは通す。接続できないことは、いま下そうとしている判断についての証拠ではない。

発火は行為ごとに一度だが、数え直しの単位はセッションではなく**圧縮の世代**である。圧縮を跨ぐと session_id は変わらないまま、門が書き込んだ文脈のほうが落ちる。マーカーだけが残って門が閉じたままになるので、`transcript_path` の中の `isCompactSummary` を数えて世代を鍵に混ぜている。

### 圧縮のあとに配り直す

`tools/sessionstart_guard.py` を SessionStart フックの `compact` matcher に入れると、圧縮で落ちた開始時の配信が戻る。

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "compact",
        "hooks": [{"type": "command", "command": "/path/to/mashu/tools/sessionstart_guard.py"}]
      }
    ]
  }
}
```

これが要るのは、再配信する経路が他に無いからである。`session_bootstrap` は契約上セッションに一度しか呼ばれず、圧縮しても session_id は変わらず、v2 には探しに行くための検索が無い。つまり圧縮後のセッションは知識を持たず、持っていないことにも気づけない。

scope の側は作業ディレクトリから解決されるので、フックは payload の `cwd` へ移ってから引く。継承した cwd のまま引くと、always だけが戻って `scoped` が空になり、その出力は「この scope には何も無い」と読める。黙って半分だけ配り直すほうが、配り直さないより悪い。

## 書き込みの規律

- 個人識別情報（本名、所属、ホームディレクトリを含む絶対パス）を含む書き込みは入口で拒否される。パターン一覧は commit hook と共用のリポジトリ外ファイル（`MASHU_BANNED_PATTERNS` で指定可）。一覧が見つからないときは「合格」ではなく「検査できなかった」と報告される
- 台帳・改訂履歴・event_log は append-only で、DB のトリガが書き換えを拒否する

## 開発

```bash
uv run pytest -q
uv run ruff check src tests --fix && uv run ruff format src tests
```

テストは実 PostgreSQL に対して実行する。テスト用データベースは実行ごとに作り直す。既定値は `mashu_test`。`MASHU_TEST_DB` で変更できる。

仕様を変える場合は、コードと `docs/mashu-v2.md` を一緒に更新する。撤回した設計は削除せず、何を撤回したか、理由、誤りと分かる条件を仕様書に残す。

## 配置

```
docs/mashu-v2.md      実装仕様書。設計判断とその理由の正本
docs/mashu-mvp.md     v1 の仕様書。撤回の記録として保持
migrations/           連番の SQL。mashu admin migrate が順に実行
src/mashu/            実装
tests/                実 PostgreSQL に対して実行
tools/pretooluse_guard.py   行為の門の PreToolUse フック
hooks/                pre-commit / commit-msg
```
