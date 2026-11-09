# Mashu（摩周）

Mashu は、Claude Code や Codex のような複数の AI agent が共有する外部記憶だ。保存するのは「忘れたことで実際に損害が出た」と確認できたルールだけで、あとで役に立ちそう、という理由だけでは保存しない。覚えるべき情報を推測して溜め込む memory system とは逆に、損害の実証を通ったものだけを常設する evidence-gated persistent instruction system である。

扱っているのは、CLAUDE.md や AGENTS.md と同じく「次のセッションの agent に何を読ませるか」だ。指示ファイルは、繰り返された実害から生まれた行だけを持つうちは小さく、毎セッション確実に読まれて機能する。Mashu は、その一行が実害から生まれる過程を、複数の CLI が共有できる形で制度化したもので、指示ファイルには無い次の仕組みを持つ。

- 入口の審査。損害の実証が無いものは候補にすらならず、agent が出した案は User が承認するまで有効にならない。
- 配信先の選択。全セッション、特定のディレクトリ、特定の作業のあいだ、の三段階で、要るセッションにだけ push する。
- provenance。規則の改訂、誰がどの指示で承認したか、守られなかった記録を append-only で残し、後から書き換えさせない。
- 作業状態の引き継ぎ。セッションをまたぐ作業の現在地、試して捨てた方法、判断の理由を Task として次のセッションに渡す。

長い本文は持たない。研究ノートや設計の文書は Obsidian の vault やリポジトリが正本であり続け、Mashu は原典の場所を指すだけである。

設計判断の正本は二つの仕様書にある。Memory、pain、review、配信は [docs/memory.md](docs/memory.md) が、Project State は [docs/project-state.md](docs/project-state.md) が扱う。

## Mashu が持つ記録

| 種類 | 中身 | 次のセッションへの届き方 | 寿命 |
|---|---|---|---|
| Memory | 今後も守る規則。本文は 120 字まで | 配信先に応じて、開始時か、その作業に入るときに届く | User が retire（廃止）するまで |
| Task | 複数のセッションにまたがる作業の現在地、試した方法、判断、成果物の場所 | 名前と goal と status だけの短い card が届き、全文は agent が `task_get` で読む | User が close するまで。14 日活動が無いと dormant になり、届かなくなる |
| Temporary Context | 期限つきの条件 | User が登録したものだけが開始時に届く | 最長 14 日 |
| Ledger | 忘れたせいで起きた事故の記録 | 届かない | 追記のみで、消えない |

Task は Project にまとめる。仕様書では、この二つを合わせて Project State と呼ぶ。

## 規則が Memory になるまで

Memory の総量を小さく保つことが、Mashu の設計のほとんどを決めている。総量が小さいから全量を push でき、全量を push できるから「読むべきときに読まれない」問題が起きない。そのため量は入口で絶ち、Memory ができる経路を次の三つに限っている。

1. agent が事故を報告する。誤った作業が実際に出たとき、agent は `pain_report` で、何が起きたかと、防ぐための規則を Ledger に書く。防ぐ規則がまだ無ければ candidate（採用を待つ候補）ができ、User が review で採用か却下を決める。
2. User が会話の中で「覚えて」と言う。agent は `memory_admit` に本文を渡し、その場で Memory にする。指示の原文と会話の参照が、承認の出所として残る。
3. User が直接登録する。CLI の `mashu remember`、デスクトップアプリ、TUI のどれかで登録すると、review を経ずに Memory になる。

`pain_report` が candidate を作らない場合が二つある。一つは、規則が既に届いていたのに守られなかった場合である。agent はその Memory を `violated_memory_id` で指し、Mashu はこれを配信の失敗の疑いとして記録する。もう一つは、Task の記録や、CLAUDE.md、skill に書いてあったのに使われなかった場合である。agent はその場所を `ignored_task_id` か `ignored_source` で指し、Mashu は新しい規則を作らずに件数だけを数える。件数は `mashu status` の `unused` の行に出る。どちらの場合も知識はその場所にあったので、同じ規則を Memory に足しても何も直らない。直すべきは届け方のほうである。

既にある Memory の retire、置き換え、復帰、配信先の移動は、User の明示的な指示があれば agent がその場で適用する。agent が自分で考えた変更は proposal（変更案）として置き、User が review で適用か却下を決める。agent の確信や User の沈黙は、承認として扱わない。

## 導入

### 必要なもの

- PostgreSQL と、その標準の拡張 pg_trgm。PostgreSQL 17 で動作を確かめている。類似の判定には pg_trgm を使い、埋め込みモデルは使わない
- Python 3.11 以上
- [uv](https://docs.astral.sh/uv/)
- git

### デスクトップアプリから入れる

GitHub Release に、正式版ごとに Mac（Apple silicon）用の `Mashu_aarch64.app.tar.gz` と、Windows 用のインストーラ `Mashu_<版>_x64-setup.exe` を置いている。Mac の配布物は、展開すると `Mashu.app` になる。

Apple と Microsoft の署名は受けていない。Mac では、初回に「開発元を確認できない」と表示されるので、システム設定の「プライバシーとセキュリティ」で「このまま開く」を押す。Windows では、SmartScreen の警告から実行を許可する。

初めて起動すると、アプリは Mashu の本体を探す。見つからなければ、入っている場所を指定するか、アプリに入れさせるかを選ぶ画面が出る。入れさせる場合、アプリは git、uv、PostgreSQL がそろっていて PostgreSQL が動いているかを確かめ、足りないものとその入れ方を示す。そろっていれば、空のフォルダに最新の正式版を clone し、`uv sync --extra mcp`、DB `mashu` の作成、`mashu admin migrate`（DB のスキーマを最新にする SQL の適用）を順に実行する。

agent への接続（MCP server と hook の登録）は、各ツールの設定ファイルを書き換えるので、アプリは行わない。[Agent につなぐ](#agent-につなぐ) の手順で設定する。

### コマンドラインから入れる

PostgreSQL が動いている状態で、clone したディレクトリで次を実行する。

~~~bash
uv sync --extra mcp
createdb mashu
uv run mashu admin migrate
~~~

接続先の既定値は `dbname=mashu` である。別の PostgreSQL を使うときは `MASHU_DATABASE_URL` を設定する。`mashu` をどのディレクトリからでも呼べるようにするには、仮想環境の実行ファイルへ symlink を張る。

~~~bash
export MASHU_DATABASE_URL='dbname=mashu host=localhost'
ln -s /path/to/mashu/.venv/bin/mashu ~/.local/bin/mashu
~~~

入ったことは次のコマンドで確かめる。`mashu status` の先頭に未適用の migration が出たら、`mashu admin migrate` を実行する。`mashu bootstrap` は、今のディレクトリで開くセッションに届く内容と、その token 数を表示する。

~~~bash
mashu status
mashu bootstrap
mashu --version
~~~

## Agent につなぐ

### MCP server を登録する

Mashu は stdio の MCP server として動く。Claude Code には次のように登録する。

~~~bash
claude mcp add mashu \
  --scope user \
  --env MASHU_DATABASE_URL=dbname=mashu \
  -- /path/to/mashu/.venv/bin/mashu serve --agent claude
~~~

agent の名前は `serve` の `--agent` か、環境変数 `MASHU_AGENT` で指定する。Codex など別の MCP client でも、同じように `mashu serve` を stdio server として登録する。

### SessionStart hook で開始時の配信を自動にする

`tools/sessionstart_guard.py` を SessionStart hook に登録すると、セッションの開始時と再開時に bootstrap の内容を配信する。compaction（文脈の要約）のあとには、要約で落ちた内容をもう一度配信する。DB に接続できないときは何も出力せずに終わる。

~~~json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|compact",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/mashu/tools/sessionstart_guard.py"
          }
        ]
      }
    ]
  }
}
~~~

hook の無い client では、AGENTS.md か CLAUDE.md に次の行を置き、agent に `session_bootstrap` を呼ばせる。

~~~markdown
# Mashu（外部記憶）

- Mashu MCP が接続されているセッションでは、開始時に一度 session_bootstrap を呼ぶ。SessionStart hook が既に配信していれば不要
~~~

### PreToolUse hook で特定の操作の前に規則を示す

`tools/pretooluse_guard.py` を PreToolUse hook に登録すると、action を結びつけた topic（[Topic](#topic) を参照）の規則を、その action に当たる tool call に `additionalContext` として添える。呼び出しは止めない。添えるのはセッションごとに一回で、compaction のあとには改めて添える。topic に source があれば、規則の後に `Source: ...` の一行で原典を示す。DB に接続できないときは何も添えずに通す。

~~~json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Task|Agent|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/mashu/tools/pretooluse_guard.py"
          }
        ]
      }
    ]
  }
}
~~~

Task と Agent の呼び出しは、組み込みの `delegate` action として扱う。それ以外の tool や、Bash のコマンドから action を判定する規則は、インストールごとに違うのでリポジトリの外に置く。既定のファイルは `.mashu-guard-actions` で、別の場所を使うときは `MASHU_GUARD_ACTIONS` で指定する。一行に一つの規則を `action 名`、`subject`、`式` の順に書く。subject が `tool` なら tool の名前全体と照合し、`command` なら正規表現として Bash のコマンドと照合する。壊れた行は、その行だけ無視される。

~~~text
delegate tool mcp__some-server__start_task
remote-shell command \b(cluster-wrapper|scheduler-cmd)\b
~~~

## 日々の使い方

### 人が操作する三つの画面

人が Mashu を操作する画面は三つある。どれも同じ DB を読み書きする。

| 画面 | 起動 | できること |
|---|---|---|
| デスクトップアプリ | トレイに常駐するアプリを開く | TUI と同じ操作をすべてウィンドウの中で行う。review を待つものが増えると OS の通知を出す |
| TUI | 引数なしの `mashu` | review、Memory と Task の閲覧と編集、設定 |
| ブラウザの一覧 | `mashu gui` | Memory、Task、review を待つものの一覧を読むだけ |

デスクトップアプリは、左のナビゲーションから四つの画面を開く。

| 画面 | できること |
|---|---|
| 判断待ち（Inbox） | candidate、Memory change proposal、close の提案を一つの一覧に並べる。candidate の採用、却下、保留と文面の編集、proposal の適用、却下、取り下げ、close の提案の受け入れを行う。topic へ採用するときは、その場で新しい topic を作れる |
| メモリ（Memory） | active と retired の Memory、Temporary Context の一覧と検索。根拠と revision を表示する。直接の登録、本文と理由の編集、配信先の変更、retire と復元、Temporary Context との相互の変換を行う |
| 作業（Work） | active、dormant、closed の Task と Project。Task の作成、編集、lease（活動期限）の延長、close、reopen と、Project の作成と編集を行う |
| 設定（Settings） | 容量と最近の記録、Bootstrap preview、Scope と Topic と Route の編集、checkpoint の間隔、配信量の上限、migration の適用、表示の言語、バージョンと更新 |

編集は、詳細の表示をその場で入力欄に変えて行う。⌘/Ctrl + Enter で保存し、Esc で取り消す。一覧は ↑↓ か j/k で移動し、/ で検索欄へ移る。retire した規則を書き戻す操作、似た Task がある中での Task の作成、削除、migration の適用は、理由や候補を見せて一度確かめてから行う。Bootstrap preview は入力したディレクトリで開くセッションに届く内容を示し、TUI と同じく配信として記録される。ウィンドウを閉じてもトレイに残り、終えるときはトレイのメニューの Quit を使う。表示の言語は日本語と英語を切り替えられる。CLI と TUI の文言は英語だけである。

TUI は四つの領域（Attention、Memories、Work、Settings & health）を持ち、デスクトップアプリとほぼ同じことができる。矢印か j/k で移動し、Enter で開き、Esc か ← で一段戻る。q はどの画面からでも Mashu を終え、? はその画面のキーの説明を出す。`e` で開く編集欄には今の値が入っているので、打ち直さずにその場で直して Enter で保存する。`VISUAL` か `EDITOR` を設定していれば、そのエディタで開く。

ブラウザの一覧は 127.0.0.1 で開き、端末の幅に収まらない一覧を、本文を省略せずに一画面に並べる。Memory は status、配信先、Scope、topic、本文の部分一致で、Task は状態と Project で絞り込める。採用や編集はできない。GET と HEAD 以外の要求は 405 で拒否し、ページを作る DB の transaction も READ ONLY にしている。localhost の HTTP から書き込めると、shell を持つ agent が curl で User として承認できてしまうからである。

~~~bash
mashu gui
mashu gui --port 8700 --no-open
~~~

Ledger、Memory の revision、Task の checkpoint と attempt と decision と artifact、event_log は履歴なので、どの画面からも編集できない。

### 規則を直接登録する

本文は該当するすべてのセッションに届くので、規則だけを 120 字以内で書く。理由、測定値、日付、経緯は `--why`（500 字まで）に渡す。`--why` は Ledger に根拠として残り、`mashu show` で読めるが、セッションには届かない。

~~~bash
mashu remember "Run migrations before restarting the service"
mashu remember "Run migrations before restarting" --why "a restart against an old schema broke the API"
mashu remember "Use the deployment scope" --scope deployment
mashu remember "Keep the win rate between 40 and 60 percent" --topic difficulty
~~~

配信先は、何も指定しなければ always、`--scope` を指定すれば scope、`--topic` を指定すれば topic になる。配信先の違いは[配信の仕組み](#配信の仕組み)で説明する。

期限つきの条件は `--until` で Temporary Context として登録する。review は要らず、期限が来ると消える。期限は最長 14 日で、`--until` は `--scope`、`--delivery`、`--topic`、`--force` と一緒には使えない。TUI の Memories で `c` を押すと、always か scope の Memory と Temporary Context を相互に変換できる。topic の Memory は、発動条件を失うので変換できない。

~~~bash
mashu remember "The staging host is down" --until 2d
~~~

### 事故を記録する

agent が `pain_report` で書く記録は、CLI の `mashu pain` からも書ける。記録するのは、知識が無かったために誤った作業を実際に行った incident だけである。同じことを調べ直しただけの手間は記録しない。

| オプション | 使う場面 | 結果 |
|---|---|---|
| なし（`--prevention-kind rule`） | 毎回覚えておく必要がある規則が無かった | `--prevention` が candidate の本文になる。120 字までで、経緯は `--what` に書く |
| `--prevention-kind work --task TASK` | 一度直せば済む | candidate を作らず、その Task の next actions に入れる |
| `--violated MEMORY` | 届いていた Memory が守られなかった | candidate を作らず、配信の失敗の疑いとして記録する |
| `--ignored-task TASK` | Task の decisions や attempts に書いてあったのに使わなかった。decision が一つに絞れるときは `--ignored-decision` も添える | candidate を作らず、使われなかった知識として数える |
| `--ignored-source FILE` | CLAUDE.md、AGENTS.md、skill に書いてあったのに使わなかった。場所は `~` かリポジトリからのパス、または `skill <name>` で書く | 同上 |

~~~bash
mashu pain --what "Deployed twice" --prevention "Check the release ledger"
mashu pain --what "Deployed twice" --prevention "Check the release ledger" --violated 5e6f7a8b
mashu pain --what "Re-proposed a settled change" --prevention "Weigh the task decisions" --ignored-task 1a2b3c4d
mashu pain --what "Bad export" --prevention "Validate the manifest" --prevention-kind work --task 1a2b3c4d
~~~

`--prevention` が既にある active な Memory と似ている場合（pg_trgm の類似度が 0.45 以上）も、candidate を作らずに配信の失敗の疑いとして記録する。日本語の言い換えはこの類似度では拾えないので、守られなかった規則が分かっているときは `--violated` で明示する。

### candidate を review する

引数なしの `mashu review` は TUI を開く。端末の画面を使わずに一覧を見たり、一件ずつ決めたりもできる。

~~~bash
mashu review                      # TUI で candidate を一件ずつ判断する
mashu review --list               # 一覧だけ表示する（--all で保留中のものも出す）
mashu review --admit 1a2b3c4d --version 3 --delivery scope --scope deployment
mashu review --decline 1a2b3c4d --reason "Too specific to one run"
mashu review --changes            # Memory change proposal を TUI で判断する
mashu review --apply-change 1a2b3c4d --version 2
~~~

TUI では `y` で採用して配信先を選び、`e` で本文を直し、`r` で却下し、`s` で保留する。保留は candidate を pending のまま一時的に一覧から隠す操作で、隠したものは `d` で表示できる。容量を超えて採用できなかったときは、`e` で本文を短くしてから `y` をやり直す。

コマンドで採用するときは、一覧で読んだ version を `--version` に渡す。読んだあとで candidate が変わっていれば拒否され、pending のまま残る。

### Task を閉じる

Task を close できるのは User だけである。agent は終わったと判断したら `task_propose_close` で理由つきの提案を置き、User がそれを受け入れるか、自分で outcome（completed、abandoned、superseded）を選んで閉じる。

~~~bash
mashu task close                  # open な Task と close の提案を一覧する画面を開く
mashu task close 1a2b3c4d --outcome completed --reason "Merged and released"
mashu task close 1a2b3c4d 5e6f7a8b --outcome superseded
mashu task reopen 1a2b3c4d
~~~

引数なしの `mashu task close` の画面では、提案のある Task が先頭に並ぶ。Enter で選択中の提案を受け入れ、`c`、`a`、`s` で outcome を自分で選び、`y` で一覧にあるすべての提案をまとめて受け入れる。提案のあとで Task の state が書き換わっていた提案（◌ の印）は、まとめての受け入れから外れ、一件ずつ確かめてから受け入れる。

## 配信の仕組み

agent は自分が何を知らないかを知らないので、検索に任せた規則は要るときに引かれない。そのため Mashu には Memory を検索する経路が無く、`session_bootstrap` がセッションの開始時に、そのセッションに当てはまる Memory をまとめて push する。

| 配信先 | 届くセッション | 届く時点 | 向いている規則 |
|---|---|---|---|
| always | すべて | 開始時 | どの作業でも守る規則 |
| scope | route が一致するディレクトリで開いたセッション | 開始時 | 特定のプロジェクトだけで要る規則 |
| topic | topic の Scope が一致するセッション。Scope が無ければすべて | 開始時には見出しの一行だけ。本文は agent がその作業に入るときに読む | 特定の作業のあいだだけ要る規則 |

scope は「どこで」を絞り、topic は「何の作業のあいだか」を絞る。topic は本文を push しないが、見出しと発動条件は毎回届くので、agent は何があっていつ読むべきかを知ったうえで本文を読みにいける。

`session_bootstrap` は次の順に返す。

1. always の Memory
2. 今の Scope の Memory
3. topic の見出し（名前、規則の数、発動条件）
4. active な Task の card（完全な Task ID、名前、goal、status、詳細の件数、最終確認日時）
5. 有効な Temporary Context
6. review を待つ candidate の件数

topic の本文と source、Task の approach、open questions、blockers、next actions の本文、Ledger、dormant と closed の Task は届かない。agent は Task を続ける前に `task_get` で全文を読む。

### 容量

容量は、全量 push が成り立つ大きさに Memory の総量を保つためにある。溢れた分を逃がす検索は無いので、上限を超える書き込みは一部も受け付けずに全体を拒否する。拒否の文面は、空きの量と、容量を空ける方法を示す。

~~~text
全体                 4000 token
Memory               2000 token（always は 800 まで。各 Scope は always と合わせて 2000 まで）
Task cards            800 token / session（1 Task 200。入りきらない Task は名前だけを 200 まで）
Task detail           800 token / Task（常時は配信しない）
Topic body            800 token / topic（常時は配信しない）
Temporary Context     400 token
~~~

topic の見出しの行も Memory の容量に数える。Scope を持たない topic の行は always に、Scope を持つ topic の行はその Scope に入る。Task の card の予算は配信のときに効き、書き込みは拒否しない。最後の活動が新しい Task から順に card を載せ、入りきらない Task は短い ID、名前、最後の活動日だけを並べる。

上限と Memory 本文の字数（既定 120 字）は、デスクトップアプリの設定の「配信量の上限」か、TUI の Settings の Delivery limits で変えられる。値は DB の `setting` テーブルにあり、MCP server は起動のたびに読むので、変更は次に開くセッションから効く。上限を今の量より下げても記録は消えないが、超えているあいだは量を増やす書き込みが拒否され、Task の card と有効な decision は古いものから配信されなくなる。

## Scope と route

Scope は Memory を配る範囲で、route は作業ディレクトリの path prefix を Scope に対応づける。`mashu bootstrap` と `session_bootstrap` は、今のディレクトリから route をたどって Scope を決める。無関係なディレクトリは `--ignore` で、どの Scope にも属さないと明示できる。

~~~bash
mashu scope
mashu scope --add deployment --about "Production releases"

mashu route
mashu route --add /work/service --scope deployment
mashu route --ignore /work/scratch
mashu route --remove /work/service
~~~

Project は Task の所属先で、Scope とは別の概念である。Project が Scope を持つと、その Task の card はその Scope のセッションにだけ届く。

## Topic

topic は、特定の作業をしているあいだだけ要る規則のまとまりである。名前、発動条件の一文、Scope を持ち、任意で action と原典（source）を持つ。セッションの開始時には `難易度較正 (9 rules): Before changing difficulty levers or win rates` のような見出しの一行だけが届き、agent はその作業に入るときに `memory_list(topic=...)` で本文を読む。

~~~bash
mashu topic
mashu topic --add 難易度較正 --scope enrai --trigger "Before changing difficulty levers or win rates"
mashu topic --edit 難易度較正 --source "Projects/Enrai/難易度.md#レバー"
mashu topic --edit 委譲 --action delegate
mashu topic show 難易度較正
mashu topic --remove 難易度較正

mashu remember "Do not change ship stats to set difficulty" --topic 難易度較正
mashu deliver 1a2b3c4d topic --topic 難易度較正
~~~

プロジェクトのどの作業でも守る規則なら scope を選び、そのプロジェクトの一部の作業でだけ効くなら topic を選ぶ。特定の操作の直前に必ず目に入れたいなら、その topic に action を結びつけ、[PreToolUse hook](#pretooluse-hook-で特定の操作の前に規則を示す) に示させる。一つの action に結びつく topic は一つだけで、action は topic の編集でだけ設定できる。

`--remove` で消せるのは active な規則を持たない topic だけである。一度も使われていない topic は削除され、retire した規則や提案が名前を参照している topic は、履歴を保つために archive されて一覧から消える。topic を作るのは User で、agent が作れるのは、User の明示的な指示で規則を追加するか移すときだけである。

### topic の原典（source）

source は、topic の規則が要約している長い本文の場所である。Obsidian の vault のルートか、リポジトリのルートからの相対パスで書き、`#見出し` を添えてよい。絶対パス、`~` で始まるパス、`..` で外へ出るパスは拒否する。

vault の場所は環境変数 `MASHU_VAULT_ROOT` だけが持ち、DB には保存しない。絶対パスには個人を特定できる名前が含まれるからである。`MASHU_VAULT_ROOT` が設定されていて、source の最初の要素が vault の直下にあれば、`memory_list(topic=...)` は source に加えて、絶対パス `source_path` と、そこにファイルがあるか `source_exists` を返す。確かめるのはファイルの有無だけで、ノートの中身は読まない。bootstrap の見出しには source を載せない。

## Project と Task

Project State は、セッションを開き直すたびに現在地を拾い直す手間を無くすためにある。どこまで進んだか、何を試して駄目だったか、なぜ今の方針なのかは、放っておくと git の履歴、リポジトリの文書、Obsidian、過去の会話に散らばり、agent が一か所から復元できなくなる。かといって作業の現況を Memory に入れると、古くならないことを前提にした Memory の容量を、やがて古くなる事実が使ってしまう。

Project は Task をまとめる単位である。Task は複数のセッションにまたがる作業だけに作り、一つの依頼や、大きな作業の一工程には作らない。

~~~bash
mashu project list
mashu project create website --scope frontend
mashu project edit website --scope backend
mashu project show website

mashu task list                   # --dormant、--closed で状態を変える
mashu task show 1a2b3c4d
mashu task create "Add CLI help" --project mashu --goal "Every command explains itself"
mashu task touch 1a2b3c4d
~~~

`mashu project create` で `--scope` を省くと、今のディレクトリの route が指す Scope を使う。route の無いディレクトリでは拒否するので、すべての Scope に card を配る Project は `--scope -` で作る。`mashu task create` で `--project` を省くと、route から一つに決まる Project か、open な Project が一つだけならそれを使う。

Task の Current State は goal、approach、status、open questions、blockers、next actions からなる。agent が書く attempt（試したことと結果）、decision（判断と理由）、artifact（外部成果物の場所）と、close のときの checkpoint は、履歴として別に残る。

- open な Task は、活動が 14 日途切れると dormant になり、bootstrap から外れる。`mashu task touch` で活動期限を延ばすと戻る
- User は CLI、TUI、デスクトップアプリで Task を作り、touch し、close し、reopen し、Current State を直せる
- agent は MCP で Current State と履歴を書く

## Agent の使い方

### MCP tool

MCP tool は 18 個ある。引数と制限は各 tool の説明に載っている。

| 分類 | Tool | 役割 |
|---|---|---|
| Memory | `session_bootstrap` | 開始時に一度呼び、Memory、Task の card、Temporary Context を受け取る |
| Memory | `pain_report` | 事故を Ledger に記録し、必要なら candidate を作る |
| Memory | `memory_list` | Scope の Memory を一覧する。`topic` を渡すと、その topic の本文と source を読む |
| Memory | `memory_get` | Memory の本文、revision、根拠、retire の理由を読む |
| Memory | `memory_admit` | User が残すよう求めた規則を採用する。本文を渡せば候補の作成と採用を一回で行う |
| Memory | `memory_nominate` | agent 自身の置き換え案の後継を candidate として作る |
| Memory | `memory_change_propose` | agent 自身の変更案（retire、replace、restore、redeliver）を proposal として置く |
| Memory | `memory_change_apply` | User が承認した proposal を適用する |
| Memory | `memory_change_instructed` | User が指示した retire、置き換え、復帰を、提案から適用まで一回で行う |
| Memory | `memory_redeliver` | User が指示した配信先の変更を、提案から適用まで一回で行う |
| Memory | `memory_change_withdraw` | 要らなくなった proposal を取り下げる |
| Project State | `project_list` | Project と Task の件数を一覧する |
| Project State | `task_create` | 似た open な Task が無いか確かめてから Task を作る |
| Project State | `task_get` | Current State の全文と有効な decision を読む。Task を続ける前に必ず呼ぶ |
| Project State | `task_search` | Task の名前、Current State、decision を検索する |
| Project State | `task_checkpoint` | Current State を書き換え、attempt、decision、artifact を追記する。agent が Task を書く唯一の tool |
| Project State | `task_propose_close` | Task の終了を理由つきで提案する |
| Project State | `task_withdraw_close_proposal` | 終了の提案を取り下げる |

### agent が守ること

~~~text
誤った作業が実際に出た           → pain_report（あった知識は violated_memory_id / ignored_task_id / ignored_source で指す）
User が「覚えて」と言った          → memory_admit
User が Memory の retire、置き換え、復帰を指示した → memory_change_instructed
User が配信先の変更を指示した      → memory_redeliver
次のセッションへ作業を引き継ぐ     → task_checkpoint
作業が終わったと判断した           → task_propose_close
~~~

- `task_checkpoint` には、次のセッションが要るものだけを渡す。失敗した試行、理由を失うと調べ直しが高くつく判断、外部成果物の場所、goal と approach と blockers の変更、作業を止める前の状態がそれにあたる。進捗と測定の途中結果は commit とプロジェクトの文書に書く
- 同じセッションが同じ Task を書けるのは、一定の間隔ごとに一回である。間隔は既定で 30 分で、TUI の Settings & health の Checkpoint interval で 0〜1440 分に変えられ、0 にすると制限が外れる
- 作業が終わったと思ったら `task_propose_close` で提案する。status に書くと User が読んで close を打ち直すことになるが、提案なら一回のキー操作で受け入れられる。終わったとみなすのは User がマージや受け入れをしたあとである。提案のあとで state を書くと提案が古くなるので、checkpoint を先に打ち、提案を最後にする
- agent 自身が考えた Memory の変更は proposal に留める。Temporary Context の登録、Memory の直接の操作、review は User だけが行う
- 会話の中の指示は出所として記録されるが、Mashu はそれが本当に User の発言かを認証しない

## 書き込みの安全規則

Mashu に入った記録は毎回のセッションに push され、複数の agent が読む。一度入った個人情報はそこから広がるので、書き込みの時点で止める。

- 本名、所属、学籍番号、ホームディレクトリを含む絶対パスのような個人を特定できる情報を含む書き込みは、書き込みの時点で拒否する。参照のフィールド（`conversation_ref`、根拠の `ref`、artifact の `locator`）だけは、ホームディレクトリを `~` に書き換えてから検査して保存する
- 禁止パターンは、リポジトリの外の `.git-banned-patterns` に置く。場所は `MASHU_BANNED_PATTERNS` で変えられる。一覧が見つからないときは、検査を通ったことにはせず、何も検査していないという警告を出したうえで書き込みを通す
- agent の tool call の記法（`<parameter name=...>` など）を含む書き込みも拒否する。ある引数の本文に次の引数が入り込んだまま保存されるのを防ぐためで、禁止パターンの一覧が無くても検査する
- 拒否の応答は、当たった引数の位置を `field` に、本文の何文字目から何文字目かを `span` に返し、書き直し方も示す。当たった文字列とパターンは、応答にもログにも出さない
- Ledger、Memory の revision、event_log は追記のみで、DB の trigger が書き換えを拒否する
- bootstrap と類似の照合結果は、retire した Memory の本文を返さない。`memory_get` と `mashu show` だけが、管理の判断のために全文と retire の理由を返す
- `mashu desktop-backend` は shell から誰でも起動できるので、User としての承認を agent から守る強さは CLI と TUI と同じである。agent に `mashu review` を禁じているなら、`mashu desktop-backend` も禁じる

## コマンド一覧

各コマンドの詳しい使い方は `mashu <コマンド> --help` で読める。ID は一覧が表示する先頭 8 文字か、4 文字以上の一意な前方一致で指定できる。

| コマンド | 役割 |
|---|---|
| `mashu` | TUI を開く |
| `mashu status` | スキーマ、容量、review 待ちの件数、最近の Ledger、配信の失敗の疑い、使われなかった知識の件数、action つき topic の発火回数を表示する |
| `mashu bootstrap` | 今のディレクトリで開くセッションに届く内容と token 数を表示する |
| `mashu gui` | 読み取り専用の一覧をブラウザで開く |
| `mashu remember` | Memory か Temporary Context を直接登録する |
| `mashu pain` | 事故を記録する |
| `mashu review` | candidate と proposal を判断する |
| `mashu show REF` | Memory、candidate、Ledger、proposal の一件を、根拠と履歴つきで表示する |
| `mashu memories` | active な Memory を一覧する。`--retired` で retire したものと理由を出す |
| `mashu ledger` | Ledger を新しい順に表示する |
| `mashu revise REF` | Memory を改訂する。前の本文は revision として残る |
| `mashu deliver REF always\|scope\|topic` | Memory の配信先を変える |
| `mashu retire REF --reason REASON` | Memory を retire する。似た規則があとで来ると、この理由を見せて確認を求める |
| `mashu restore REF --reason REASON` | retire した Memory を戻す |
| `mashu scope`、`mashu route`、`mashu topic` | Scope、route、topic を一覧し、編集する |
| `mashu project`、`mashu task` | Project と Task を一覧し、編集する |
| `mashu guard ACTION` | ACTION に結びついた topic の規則を表示する。PreToolUse hook が呼ぶ |
| `mashu serve` | MCP server を起動する |
| `mashu desktop-backend` | デスクトップアプリが子プロセスとして起動する |
| `mashu admin migrate` | 未適用の migration を実行する |

## 環境変数

| 変数 | 役割 |
|---|---|
| `MASHU_DATABASE_URL` | 接続先の PostgreSQL。既定値は `dbname=mashu` |
| `MASHU_AGENT` | MCP server が記録する agent の名前。`serve --agent` と同じ |
| `MASHU_VAULT_ROOT` | Obsidian の vault の絶対パス。topic の source を解決するのに使う |
| `MASHU_BANNED_PATTERNS` | 禁止パターンのファイルの場所 |
| `MASHU_GUARD_ACTIONS` | PreToolUse hook が action を判定する規則のファイルの場所。既定は `.mashu-guard-actions` |
| `MASHU_EXE` | デスクトップアプリが起動する `mashu` の実行ファイル |
| `MASHU_TEST_DB` | テスト用の DB の名前。既定は `mashu_test` |
| `MASHU_TOTAL_CAPACITY`、`MASHU_CAPACITY`、`MASHU_ALWAYS_CAPACITY`、`MASHU_PROJECT_CAPACITY`、`MASHU_CARD_OVERFLOW_CAPACITY`、`MASHU_DECISION_CAPACITY`、`MASHU_TASK_CARD_CAPACITY`、`MASHU_TASK_DETAIL_CAPACITY`、`MASHU_TEMPORARY_CAPACITY`、`MASHU_TOPIC_CAPACITY`、`MASHU_CONTENT_LIMIT` | 容量と本文の字数の既定値を差し替える。DB の `setting` テーブルに値がある上限には効かない |

## デスクトップアプリの仕組み

`desktop/` は Tauri 2 で作ったアプリである。アプリは `mashu desktop-backend` を子プロセスとして起動し、標準入出力に JSON を一行ずつ流して要求と応答をやりとりする。待ち受けるポートは無い。書き込みは TUI と同じ関数（`review_actions`、`memory_actions`、`task_actions`、`application`）を通るので、version の照合、容量の上限、個人情報の検査は TUI と同じに掛かる。

起動する `mashu` は次の順に探す。

1. 環境変数 `MASHU_EXE`
2. 設定の「バージョン」で指定した checkout の `.venv` にある `mashu`
3. 手元でビルドしたときは、そのアプリがある checkout の `mashu`
4. PATH と `~/.local/bin` にある `mashu`。checkout の `.venv` への symlink なら、その checkout を更新の対象にする

どれも見つからなければ、[デスクトップアプリから入れる](#デスクトップアプリから入れる)で説明した最初の画面を出す。checkout はあるのに DB が無いときは「データベースを作成」を、PostgreSQL に接続できないときは「再試行」を出す。

60 秒ごとに DB を読み、review を待つ candidate、proposal、close の提案が増えると OS の通知を出す。トレイのアイコンのツールチップに、それぞれの件数を出す。画面の文言は `desktop/ui/i18n.js` の辞書にまとめてあり、キーは DB と CLI で使う名前（`admit`、`dormant` など）である。

### 更新

アプリは起動時と一日ごとに origin のリリースのタグを確かめ、アプリか checkout より新しい正式版があれば、ナビゲーションの下に知らせを出す。設定の「バージョン」の「更新」を押すと、backend を止めたまま次を順に行う。

1. checkout の main をそのタグまで fast-forward する
2. `uv sync --extra mcp` と `mashu admin migrate` を実行する
3. アプリを GitHub Release の配布物で入れ替えて、再起動する

checkout に commit していない変更があるとき、main 以外のブランチにいるとき、main にタグに無いコミットがあるときは、何も変えずに理由を示す。途中で失敗しても、もう一度「更新」を押せば続きから進む。手元でビルドしたアプリは checkout だけを更新するので、アプリは `cargo run` で起動し直す。Windows では、動いている MCP server が `.venv` の実行ファイルを開いたままだと `uv sync` が失敗するので、agent のセッションを閉じてから更新する。

### 手元でビルドする

Rust と、Windows では WebView2 が要る。

~~~bash
cd desktop/src-tauri
cargo run
~~~

Mac の配布物には、.app 全体に ad-hoc 署名を掛けている。掛けていないと、ダウンロードしたものが「壊れている」と表示されて開けない。

## 開発

~~~bash
uv run pytest -q
uv run ruff check
uv run ruff format
~~~

個人を特定できる情報が commit に入るのを止める git hook（pre-commit と commit-msg）は、`./hooks/install.sh` で有効にする。このスクリプトは `core.hooksPath` を設定し、禁止パターンのファイル `.git-banned-patterns` の雛形を作る。このファイルは git に入れず、本体の checkout に一つだけ置き、git worktree で切った作業ツリーの hook もそれを読む。hook は staged のパスと内容と commit message を検査し、パターンの構文が壊れているときや内容を取り出せないときも commit を拒否する。

### テスト

テストは実際の PostgreSQL に対して実行し、テスト用の DB を実行ごとに作り直す。DB の名前の既定は `mashu_test` で、`MASHU_TEST_DB` で変えられる。テストは一つの DB を共有し、各テストの前に TRUNCATE で空にする。追記のみの表の trigger を `session_replication_role = replica` で外すので、テストを実行する role には superuser の権限が要る。

main への push と pull request では、GitHub Actions の CI（`.github/workflows/ci.yml`）が ruff の検査とテストを実行する。テストは Ubuntu の Python 3.11 と 3.13、Windows の Python 3.13 で、それぞれ PostgreSQL 17 に対して走る。

テストを足すときは、次を優先する。

- 新しいテストを書く前に、既存のテストを広げる
- 状態は conftest の共有 fixture で作る
- 一つの不変条件は、MCP、service、DB のうち一か所でだけ確かめる
- 画面や CLI の文言ではなく、DB の状態を確かめる

実際の呼び出し元（MCP の tool、CLI、TUI、git hook）が作らない入力や状態のテストは足さない。TUI の見た目（列幅、カーソル位置、端末の高さ、ANSI の有無）やキーの移動だけを確かめるテストも置かない。

### リリース

版は `src/mashu/__init__.py` の `__version__` だけに書き、pyproject.toml はそこから読む。版は PEP 440 の正規形で書く。`3.0.1` のように数字だけなら正式なリリースで、`3.1.0b1` のように a、b、rc と番号で終わればプレリリースである。

リリースは、main の checkout で `tools/release.sh <版>` を実行して作る。スクリプトは版を書き換えて main へ commit と push をし、main の CI が通るのを待ってから `v<版>` のタグを push する。タグの CI は、タグの版が `__version__` と一致すること、そのコミットが main にあって CI を通っていることを確かめてから、GitHub Release を下書きで作る。続いて Mac と Windows のアプリをビルドし、更新用の配布物、その署名、両方を載せた `latest.json` を下書きに加えてから公開する。

アプリの updater は、`tauri.conf.json` の公開鍵で配布物の署名を確かめる。秘密鍵はリポジトリの secret `TAURI_SIGNING_PRIVATE_KEY` に置き、パスフレーズを付けたときは `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` にも置く。無いとアプリのビルドが失敗する。

### 仕様を変えるとき

コードと、対応する仕様書（`docs/`）を同じコミットで直す。仕様書には今の仕様とその理由だけを書き、撤回した設計と経緯は消して git の履歴に任せる。

## 主な配置

~~~text
docs/memory.md               Memory の仕様書
docs/project-state.md        Project State の仕様書
migrations/                  連番の SQL。mashu admin migrate が順に実行する
src/mashu/                   実装
desktop/                     Tauri のデスクトップアプリ（src-tauri/ が Rust、ui/ が画面）
tests/                       実際の PostgreSQL に対するテスト
tools/sessionstart_guard.py  SessionStart hook
tools/pretooluse_guard.py    action つき topic を示す PreToolUse hook
tools/release.sh             版を上げてタグと GitHub Release を作る
hooks/                       pre-commit と commit-msg
.github/workflows/ci.yml     GitHub Actions の CI
~~~

*摩周湖は世界最高クラスの透明度が観測されたことで知られており、どこまでも遡って追跡できる knowledge state を目指して命名した。摩周湖には流れ込む川も流れ出る川も無く、外の流れに属さない閉じた湖という地形は、特定の agent に依存しない独立した knowledge layer という設計思想に対応する。摩周湖は霧で見えないことでも有名であり、本プロジェクトの意義は、霧を晴らして何を信頼してよいかを人が判断できる状態にすることにある。*
