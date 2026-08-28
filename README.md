# Mashu（摩周）

複数の AI Agent が共有する外部記憶。忘れたことで実際に損害が出た、と実証されたものだけを知識として蓄え、各セッションの開始時に全量 push する。「覚えておくと便利そう」なものは入らない。

名前は北海道の摩周湖に由来する。この README は使い方を説明し、設計判断とその理由は [`docs/mashu-v2.md`](docs/mashu-v2.md) を正本とする。

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

知識になる道は三つある。

1. **事故**。誤った作業が実際に出た。`pain_report` 1 回で昇格候補になる
2. **再導出**。同じことを二度調べた。2 回目で候補になる。ただし 1 回目は痛みとして自覚されないので、Agent は調べて分かったことを `trace_put` で一行残しておく。痕跡は知識ではなく、Review も配信もされず 30 日で失効する。2 回目の `pain_report` がそれと照合されたとき、初めて「二度目」が証明される
3. **User の明示**（`mashu remember`）。これだけは即時に active になる

候補を知識に確定できるのは人だけである（`mashu review`）。Agent の書き込みが、人の確定なしに他のセッションへ届く経路は存在しない。

読む経路はすべて push で、検索で知識を返すツールは無い。在庫には定員（既定 2000 token、うち always 層は 800 token）があり、満席での昇格は退役か格下げとセットでないと通らない。定員があるから全量 push が成立する。

## 配信の三経路

| delivery | 誰に | いつ |
|---|---|---|
| `always` | 全セッション | 開始時（bootstrap） |
| `scope` | route が当たるセッション | 開始時（bootstrap） |
| `guard:<action>` | その行為に至ったセッション | 行為の直前（PreToolUse フック） |

scope は場所で絞り、guard は時機で絞る。guard は該当ツールの呼び出しを一度拒否してピン留めされた記憶を提示し、読んだうえで同じ判断ならもう一度呼べば通る。発火はセッションにつき行為ごとに一度。

## セットアップ

PostgreSQL と pg_trgm（標準の contrib。PostgreSQL 17 で動作確認）、Python 3.11+ と [uv](https://docs.astral.sh/uv/) を使う。埋め込みモデルは使わず、照合はすべてトライグラム類似で行う。

```bash
uv sync --extra mcp
createdb mashu
uv run mashu admin migrate
./hooks/install.sh   # git hook (see "Write discipline" below)
```

接続先の既定値は `dbname=mashu` で、`MASHU_DATABASE_URL` で変更できる。どこからでも `mashu` と打てるようにするには、PATH の通ったところへ symlink を張る。

```bash
ln -s /path/to/mashu/.venv/bin/mashu ~/.local/bin/mashu
```

## 日常のコマンド

普段使うのはこの四つ。

| コマンド | 内容 |
|---|---|
| `mashu status` | 在庫と定員、pending 件数、台帳と痕跡の状況 |
| `mashu review [--all]` | 昇格候補を 1 件ずつ確定・却下・保留する TUI（次節） |
| `mashu remember <body> [--until 5d]` | User 明示。唯一の即時経路。`--until` を付けると期限つき条件（Temporary Context）になり、Review 不要で期限に消える |
| `mashu pain --kind {incident,friction} --what <w> --prevention <p>` | 痛みの手動記録 |

### mashu review

二画面の TUI である。待っている候補の一覧（↑↓ / j k で移動、⏎ で開く）と、1 件の全文・token 見積り・根拠の台帳エントリを並べた個別画面（← で一覧へ戻る）を行き来する。個別画面のキーは次のとおり。

| キー | 動作 |
|---|---|
| `y` | 確定。delivery を選ぶ（空 Enter で既定、`g ACTION` で guard） |
| `e` | エディタで本文を直してから確定 |
| `r` | 理由を付けて却下 |
| `s` | 理由を付けて保留 |
| `space` | 続きを読む |
| `?` / `q` | キーの説明 / 退出 |

決定は 1 件ずつその場で確定する。途中で `q` を押しても済んだ分は残り、次の `mashu review` は残りから始まる。`s` の保留は決定ではなく、pending のまま理由と一緒に脇へ置くだけで、`--all` を付けると戻ってくる。まとめて承認するキーは無い。件数は週数件のオーダーなので、1 件ごとに人が置き場を決める。

### 管理・閲覧

| コマンド | 内容 |
|---|---|
| `mashu show <id>` | 記憶・候補・台帳エントリを 1 件、全文で表示。根拠の台帳と改訂履歴、台帳なら採用先も出る |
| `mashu memories [--scope <name>] [--retired]` | 記憶の一覧。既定は active、`--retired` で退役分と理由 |
| `mashu ledger` | 台帳の閲覧 |
| `mashu trace [query]` | 痕跡の閲覧と検索 |
| `mashu retire <id> --reason <r>` | 退役。以後は照合で、何が、なぜ否定されたかだけ返る |
| `mashu revise <id>` | 本文の改訂（User のみ）。改訂履歴が残る |
| `mashu deliver <id> {always,scope,guard}` | 配信経路の変更 |
| `mashu guard <action> [--pin <id>] [--unpin <id>]` | 記憶の行為へのピン留めと照会 |
| `mashu scope [--add <name> --about <line>]` | Scope 台帳（作成は User のみ） |
| `mashu route [--add <path> --scope <name>] [--ignore <path>]` | 作業ディレクトリと Scope の対応 |
| `mashu bootstrap` | このディレクトリのセッションが受け取る内容と token |
| `mashu admin migrate` | 未適用の migration を実行 |

id を取る引数はどれも、一覧が表示する短縮 ID（先頭 8 文字）をそのまま受け付ける。4 文字以上の前方一致で一意に決まればよく、複数に当たったときは候補を並べて拒否する。

## Agent から使う

Claude Code に登録する場合。

```bash
claude mcp add mashu --scope user --env MASHU_DATABASE_URL=dbname=mashu -- /path/to/mashu/.venv/bin/mashu serve --agent claude
```

Agent 名は `--agent` または環境変数 `MASHU_AGENT` で渡す。MCP ツールは 6 つ。

| Tool | 役割 |
|---|---|
| `session_bootstrap` | セッション開始時に一度。always と現在 Scope の記憶、期限つき条件、pending 件数 |
| `pain_report` | 痛みを台帳へ記録し、類似の台帳エントリ・痕跡・退役理由を返す。二度目なら候補を生成 |
| `trace_put` | 調べて分かったことを一行残す |
| `trace_search` | 痕跡の検索。日付つき・未検証の印で返る |
| `memory_list` | 指定 Scope の active な記憶の列挙 |
| `memory_nominate` | 会話中の User の記録指示を候補として運ぶ。pending 止まりで、確定は人 |

期限つき条件（Temporary Context）を書けるのは User だけ（`mashu remember --until`）。Agent が観測した期限つきの条件は `trace_put` で痕跡に残す。

### guard 配信を有効にする（PreToolUse フック）

`tools/pretooluse_guard.py` を PreToolUse フックに入れると、ピン留めした記憶が該当ツールの実行直前に出る。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Task|Agent|mcp__codex-async__codex_start|Bash",
        "hooks": [{"type": "command", "command": "/path/to/mashu/tools/pretooluse_guard.py"}]
      }
    ]
  }
}
```

DB に接続できないときはブロックせず通す。接続できないことは、いま下そうとしている判断についての証拠ではない。

発火は行為ごとに一度だが、数え直しの単位はセッションではなく圧縮の世代である。圧縮を跨ぐと session_id は変わらないまま、フックが書き込んだ文脈のほうが落ちる。発火済みの印だけが残って guard が二度と出なくなるので、`transcript_path` の中の `isCompactSummary` を数えて世代を鍵に混ぜている。

ツールと行為の対応は `ACTIONS` にある。行為は判断の名前であって、ツールの名前ではない。`Task` / `Agent` / `codex_start` はどれも `delegate`、`Bash` は `shell`。何も留めていない行為では `mashu guard` が空で返り、フックは印も残さず通すので、対応を足すこと自体に費用は無い。逆に、留めた記憶は最初のその行為で出る。それが些細な呼び出しであっても出る。

### 圧縮のあとに配り直す（SessionStart フック）

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

これが要るのは、再配信する経路が他に無いからである。`session_bootstrap` は契約上セッションに一度しか呼ばれず、圧縮しても session_id は変わらず、探しに行くための検索が無い。つまり圧縮後のセッションは知識を持たず、持っていないことにも気づけない。

scope はフックが payload の `cwd` へ移ってから解決する。継承した cwd のまま引くと always だけが戻って scoped が空になり、その出力は Scope に何も無いのと見分けが付かない。黙って半分だけ配り直すほうが、配り直さないより悪い。

## 書き込みの規律

- 個人識別情報（本名、所属、ホームディレクトリを含む絶対パス）を含む書き込みは入口で拒否される。パターン一覧は commit hook と共用のリポジトリ外ファイル（`MASHU_BANNED_PATTERNS` で指定可）。一覧が見つからないときは合格ではなく、検査できなかったと報告される
- 台帳・改訂履歴・event_log は append-only で、DB のトリガが書き換えを拒否する

## 開発

```bash
uv run pytest -q
uv run ruff check src tests --fix && uv run ruff format src tests
```

テストは実 PostgreSQL に対して実行し、テスト用データベースは実行ごとに作り直す。既定値は `mashu_test` で、`MASHU_TEST_DB` で変更できる。

仕様を変える場合は、コードと `docs/mashu-v2.md` を一緒に更新する。撤回した設計は削除せず、何を撤回したか、理由、誤りと分かる条件を仕様書に残す。

## 配置

```
docs/mashu-v2.md      実装仕様書。設計判断とその理由の正本
migrations/           連番の SQL。mashu admin migrate が順に実行
src/mashu/            実装
tests/                実 PostgreSQL に対して実行
tools/pretooluse_guard.py     guard 配信の PreToolUse フック
tools/sessionstart_guard.py   圧縮後に配り直す SessionStart フック
hooks/                pre-commit / commit-msg
```
