# Mashu（摩周）

Mashu は、複数の AI agent が共有する外部記憶だ。保存するのは「忘れたことで実際に損害が出た」と確認できたルールだけ。あとで役に立ちそう、という理由だけでは保存しない。

Mashu には、恒久的な Memory だけでなく、現在の作業状態を扱う Project State と、再調査を検出するための短期記録 Trace もある。Memory と Project State は目的も寿命も違うので、同じものとして扱わない。

設計判断の正本は次の文書にある。

- Memory の仕様: [docs/mashu-v2.md](docs/mashu-v2.md)
- Project State の仕様: [docs/mashu-v3.md](docs/mashu-v3.md)

この README は、インストールと日常の使い方に絞る。

## 何を保存するか

| 種類 | 役割 | 別のセッションに届くか | 寿命 |
|---|---|---|---|
| Memory | 今後も守る恒久ルール | 直接操作または明示指示の実行後、bootstrap か topic で届く | 原則恒久 |
| Project State | Project / Task の現在地、試行、判断、成果物の参照先 | active Task の短い card だけ届き、詳細は task_get で読む | close または Activity Lease が切れるまで |
| Trace | 調べて分かったことを残す日付つきの観測 | 届かない。検索で明示的に読む | 既定 30 日 |
| Ledger | 忘却によって起きた事故や再調査の記録 | 届かない | append-only |
| Temporary Context | 期限つきの条件 | User が書いたものだけ bootstrap で届く | 最大 14 日 |

Memory 以外の記録は、Memory の代わりではない。便利なメモや長い資料は、プロジェクトの文書や Obsidian など正本を置く場所で管理する。

### Memory ができるまで

記録の入口と、その後の扱いは次のとおり。

~~~text
調べた・導出した
    └─ trace_put
         └─ Trace（未検証・非配信・30日で失効）

実際に困った
    ├─ incident（誤った作業が出た）
    │    └─ pain_report → pending candidate
    └─ friction（同じことを調べ直した）
         └─ 既存の Trace / friction と照合できれば candidate

User が会話中に「覚えて」と言った
    └─ memory_admit（content を渡す）→ active Memory（1 回の呼び出しで完了）
         └─ 未読の pending candidate に合流した、または invalidated / legacy と衝突した
              └─ 採用せず停止 → 返った nomination を読み、memory_admit（nomination_id）

Agent が既存 Memory の変更を考えた（退役・置換・復帰・配信先の移動）
    └─ memory_get → memory_change_propose → pending proposal
         └─ User の明示指示があれば memory_change_apply

User が CLI / TUI で Memory を直接登録・変更した
    └─ active Memory / retired Memory（user_direct として記録）

pending candidate
    └─ mashu review → active Memory → bootstrap / topic
~~~

incident / friction から自動生成された candidate と Agent 独自の提案は pending に留まる。ユーザーが会話中に明示した記録・変更指示は `memory_admit` / `memory_change_apply` に渡し、同じセッションで実行できる。実行者は Agent のまま記録し、指示の原文と会話参照を別に保存する。これは監査用で、サーバーが会話の真正性を認証するものではない。Agent の有用性判断・confidence・ユーザーの沈黙は承認として扱わない。Project State は別扱いで、Agent が更新した active Task の card は次のセッションへ届く。Current State 全文は常時注入せず、card の完全な task_id から `task_get` で明示取得する。

`memory_admit` に `content` を渡すと、候補の作成と採用を一つの transaction で行う。候補が既存の pending candidate に合流した場合と、invalidated / legacy の退役 Memory と衝突し、その conflict に触れた指示が無い場合は採用せずに止まり、作った候補と台帳行だけを残して nomination を返す。Agent はその本文と conflict を読み、`nomination_id` と nomination `version` を `nomination_version` として `memory_admit` に渡して続ける。この経路では本文・scope・根拠・conflict が読み取り後に変わっていれば拒否する。同じ request ID の再送は、どちらの経路でも、Memory が後から編集・退役されていても初回の採用応答を返す。replace は後継 nomination の内容と旧 Memory の配信条件を提案に固定し、配信条件を変える場合は `successor_settings` に `delivery` と `scope_id`（topic なら `topic` も）をまとめて proposal に含める。

一度直せば以後は覚えなくてよい変更は、Memory ではなく Task の next_actions に記録する。pain の prevention-kind を work にするとこの扱いになる。

## まず動かす

### 前提

- PostgreSQL と pg_trgm。pg_trgm は PostgreSQL 標準の contrib 拡張で、PostgreSQL 17 で動作確認している
- Python 3.11 以上
- [uv](https://docs.astral.sh/uv/)

埋め込みモデルは使わず、類似判定は pg_trgm で行う。

### インストールと初期化

PostgreSQL が起動している状態で実行する。

~~~bash
uv sync --extra mcp
createdb mashu
uv run mashu admin migrate
~~~

Git の pre-commit / commit-msg hook も使う場合は、次を実行する。これは hook の有効化と、ローカル専用の禁止パターンファイルの雛形作成を行う。

~~~bash
./hooks/install.sh
~~~

接続先の既定値は dbname=mashu。別の PostgreSQL に接続するときは MASHU_DATABASE_URL を設定する。

~~~bash
export MASHU_DATABASE_URL='dbname=mashu host=localhost'
~~~

PATH の通った場所から mashu を直接呼びたい場合は、仮想環境の実行ファイルへ symlink を張る。

~~~bash
ln -s /path/to/mashu/.venv/bin/mashu ~/.local/bin/mashu
~~~

### 初期化後の確認

~~~bash
uv run mashu status
uv run mashu bootstrap
~~~

status の先頭に未適用 migration が表示されたら、uv run mashu admin migrate を実行する。bootstrap は、現在の作業ディレクトリのセッションに配信される内容と token 数を表示する。

## 日常の使い方

### 人向けの画面を開く

引数なしの `mashu` は人向け dashboard を開く。

~~~bash
mashu
~~~

| 領域 | TUI からできること |
|---|---|
| Attention | candidate の review、close proposal の判断、dormant Task の確認 |
| Memories | active / retired Memory と Temporary Context の閲覧・検索、直接登録、編集、always / scope と Temporary Context の相互変換、retire、delivery / topic の変更 |
| Work | active / dormant / closed Task と Project の閲覧・検索、Task の履歴・artifact の確認、Task / Current State / Project の作成・編集、touch / close / reopen |
| Settings & health | 容量と queue の状態、bootstrap preview、Scope と route の作成・編集・削除、Topic の作成・編集（action の結びつけを含む）・削除、migration の確認・適用 |

各画面では矢印または j / k で移動し、Enter で開く。`/` のある一覧は部分検索できる。Esc または ← で一段戻り、q でその領域を閉じる。TUI は alternate screen 上で動くため、再描画した画面は terminal の履歴へ残らない。

`e` で開く編集入力には現在値が最初から入る。全文を打ち直すのではなく、カーソル移動、Backspace、追記で既存の文章を直接直し、Enter で保存する。Vim などの外部エディタは既定では起動しない。自分で `VISUAL` または `EDITOR` を設定した場合だけ、そのエディタで既存本文を開く。

Agent が使うサブコマンドと MCP は変わらない。引数を付けたコマンドは従来どおり非対話で動作する。人は TUI から現行の本文・設定・Current State を訂正できる。Ledger、Memory revision、Task の checkpoint / attempt / decision / artifact、event_log は履歴なので編集しない。

### 恒久ルールを直接登録する

User が CLI の `mashu remember` または dashboard の Memories から直接登録した内容は、review を待たずに active Memory になる。会話中に「覚えて」と明示された内容は、Agent が `memory_admit` に本文を渡して 1 回で登録できる。

~~~bash
mashu remember "Run migrations before restarting the service"
mashu remember "Use the deployment scope" --scope deployment
mashu remember "Keep the win rate between 40 and 60 percent" --topic difficulty
~~~

配信先を省略したときは、scope も省略すれば always、scope を指定すれば scope、--topic を指定すれば topic になる。

期限つきの条件は --until で登録する。Temporary Context は review 不要で期限に消える。--until は --scope、--delivery、--topic、--force と併用できず、期限は最大 14 日。

Memories TUI では `c` で active な always / scope Memory を Temporary Context に変換できる。日数を入力すると元の Memory は `relocated` と移動先 ID を記録して retire され、同じ本文と Scope の Temporary Context が有効になる。Temporary Context で `c` を押すと、同じ本文と Scope の active Memory に戻る。無関係な invalidated / legacy conflict は上書きしない。topic の Memory は発動条件を失うため Temporary Context には変換しない。

~~~bash
mashu remember "The staging host is down" --until 2d
~~~

### 忘却による損害を記録する

pain は、忘れたことによって起きたことと、その再発を防ぐ内容を一緒に記録する。

- incident: 知識が無かったため、誤った作業を実際に行った
- friction: 同じ情報をもう一度調べた
- prevention-kind rule: 毎回覚えておく必要があるルール。既定値で、candidate の対象になる
- prevention-kind work: 一度だけ行う変更。candidate にはせず、指定した Task の next_actions に入れる

~~~bash
mashu pain \
  --kind incident \
  --what "Deployed twice" \
  --prevention "Check the release ledger"

mashu pain \
  --kind friction \
  --what "Looked up the quota again" \
  --prevention "Quota resets at midnight"

mashu pain \
  --kind incident \
  --what "Bad export" \
  --prevention "Validate the manifest" \
  --prevention-kind work \
  --task 1a2b3c4d
~~~

incident は 1 回で candidate になる。friction は、過去の Trace または friction と類似すると「再調査」として candidate になる。最初に調べた時点で trace_put を残しておくと、次の pain で再調査を証明できる。

### candidate を review する

引数なしの mashu review は TUI を開く。候補を一覧だけ表示したり、端末を使わずに 1 件決めたりもできる。

~~~bash
mashu review
mashu review --list
mashu review --list --all
mashu review --admit 1a2b3c4d --version 3 --delivery scope --scope deployment
mashu review --decline 1a2b3c4d --reason "Too specific to one run"
mashu review --changes
mashu review --changes --list
mashu review --apply-change 1a2b3c4d --version 2
~~~

既定では保留中の nomination を表示しない。--all を付けると保留分も一覧に戻る。変更 proposal は `mashu review --changes` で確認し、対象本文・revision・理由・根拠・後継・conflict を読み、入力済みの理由を編集して適用できる。TUI の `y` は直接操作の承認でもあるため、適用後に同じ確認を重ねない。Agent は明示指示なしに apply しない。

非対話で採用・変更を適用する場合は、一覧や詳細で読んだ version を `--version` に指定する。候補が読み取り後に変わっていれば `--admit` は拒否され、pending のまま残る。出力された request ID を再送時に `--request-id` へ渡すと同じ結果が返る。

TUI の操作は次のとおり。

| キー | 動作 |
|---|---|
| ↑ / ↓ または j / k | 候補を移動 |
| Home / End、PageUp / PageDown | 先頭・末尾または画面単位で移動 |
| / | ID、kind、scope、本文、evidence を絞り込み。空入力で解除 |
| Enter | 選択した候補を開く |
| Esc / ← | 詳細から一覧へ戻る。絞り込み中は解除 |
| y | 確定。delivery を選ぶ |
| e | 現在の候補本文をその場で編集し、Enter で保存。確定はせず、続けて y で再試行できる |
| r | 理由を入力して却下 |
| s | 理由を入力して保留 |
| Space | 本文をページ送り |
| ? | キーの説明 |
| q | 退出 |

`mashu review --changes` の TUI は提案を一件ずつ開き、`y` で適用、`e` で理由編集、`d` で却下、`w` で取り下げる。対象 Memory の revision や conflict が変わっていれば適用できず、最新情報を読み直して proposal を更新する。

一覧では選択中の本文、evidence 数、退役済み Memory との衝突、以前の保留理由を preview できる。保留は却下ではなく、candidate を pending のまま一時的に一覧から隠す操作だ。途中で退出しても、済んだ決定は保存される。

確定が容量超過で拒否された場合も候補は選択されたまま残る。`e` で本文を短くして保存し、`y` で同じ候補の確定をやり直せる。編集しても evidence と pending 状態は変わらず、編集自体も event_log に残る。

## 見る・変更する

| コマンド | 役割 |
|---|---|
| mashu status | schema、容量、pending 件数、最近の ledger、配信失敗の疑いとその経路別の内訳を表示 |
| mashu bootstrap | 現在のディレクトリのセッションへ配信される内容と token 数を表示 |
| mashu show REF | Memory、candidate、Ledger、Memory change proposal の 1 件を根拠・履歴つきで表示 |
| mashu memories | active Memory の一覧を表示 |
| mashu memories --retired | 退役済み Memory と退役理由を表示 |
| mashu ledger | Ledger を新しい順に表示 |
| mashu trace [QUERY] | Trace を表示・検索 |
| mashu retire REF --kind KIND --reason REASON | `invalidated` / `superseded` / `out_of_scope` / `relocated` の理由を付けて Memory を退役させる。`legacy` は既存データと移行専用 |
| mashu review --changes | retire / replace / restore / redeliver の pending proposal を TUI で読む |
| mashu revise REF | Memory を改訂する。旧本文は revision history に残る |
| mashu deliver REF always\|scope\|topic | active Memory の配信先を変更する。topic は --topic NAME で指定する |
| mashu topic | topic の一覧・作成・編集・削除。`mashu topic show NAME` で本文を読む |
| mashu topic --edit NAME --action ACT | topic を action に結びつける。`--no-action` で外す |
| mashu guard ACTION | ACTION に結びついた topic のルールを表示する。PreToolUse hook が読む |
| mashu admin migrate | 未適用の migration を実行する |

ID は list コマンドが表示する先頭 8 文字を使える。4 文字以上の一意な前方一致も受け付ける。複数の候補に一致する場合は拒否される。

## 配信の仕組み

Memory の通常の読み取りは検索ではなく push だ。session_bootstrap がセッション開始時にまとめて配信する。topic は見出しだけを開始時に配信し、本文は Agent がその作業に入るときに読む。action を結びつけた topic は、その行為の直前に PreToolUse hook も本文を示す。検索できるのは Trace だけで、Trace から Memory は返さない。

| delivery | 届く範囲 | 届くタイミング | 向いているルール |
|---|---|---|---|
| always | 全セッション | 開始時の bootstrap | どの作業でも守るルール |
| scope | route が一致するセッション | 開始時の bootstrap | 特定の領域だけで必要なルール |
| topic:NAME | 見出しは topic の Scope が一致するセッション（Scope が無ければ全セッション） | 見出しは開始時、本文はその作業に入るとき | 特定の作業のあいだだけ必要なルール |

scope は「どこで」、topic は「何の作業のあいだか」を絞る。topic に action を結びつけると、その作業に当たる tool call（`delegate` なら Agent / Task による subagent の起動）の直前に、hook がその topic のルールを示して呼び出しを一度止める。hook が発火するのは topic の見出しが載るセッション、つまり topic に Scope があればその Scope のセッションだけだ。

初期の容量は次のとおり。各枠は独立しており、超過した書き込みは黙って切り捨てず拒否する。

~~~text
全体                 4000 token
Memory               2000 token（always は 800 まで。各 Scope は always と合わせて 2000 まで）
Task cards           1600 token / scope（1 Task 200）
Task detail           800 token / Task（常時配信しない）
Topic body            800 token / topic（常時配信しない）
Temporary Context     400 token
~~~

Scope ごとの Memory に独立した上限は無い。2000 から always の使用量を引いた残りが、各 Scope で使える量になる。topic の見出しの行もこの席で数える。Scope を持たない topic の行は always に、Scope を持つ topic の行はその Scope に入る。

環境変数 MASHU_TOTAL_CAPACITY、MASHU_CAPACITY、MASHU_ALWAYS_CAPACITY、MASHU_PROJECT_CAPACITY、MASHU_TASK_CARD_CAPACITY、MASHU_TASK_DETAIL_CAPACITY、MASHU_TEMPORARY_CAPACITY、MASHU_TOPIC_CAPACITY で変更できる。

Task card の枠は Scope ごとに独立している。Scope を持たない Project の card は全 Scope 共通分として各枠に含まれ、unrouted session にはこの共通分だけが届く。`mashu status` の card 使用量は、最も重い Scope の値である。Task detail は配信枠には含めず、1 Task ごとの上限だけを持つ。

## Scope と route

Scope は Memory の配信範囲、Project は Task の所属先だ。似ているが別の概念である。

~~~bash
mashu scope
mashu scope --add deployment
mashu scope --add deployment --about "Production releases"

mashu route
mashu route --add /work/service --scope deployment
mashu route --ignore /work/scratch
mashu route --remove /work/service
~~~

route は作業ディレクトリの path prefix と Scope を対応づける。bootstrap は現在の cwd から Scope を解決する。無関係なディレクトリを --ignore で明示的に unscoped にもできる。
Scope の summary は任意で、CLI と Settings TUI のどちらでも省略できる。

## Topic

topic は、特定の作業をしているあいだだけ必要なルールの束だ。名前、発動条件の一文、所属する Scope（省略すると全セッション）を持つ。bootstrap には `難易度較正 (9 rules): Before changing difficulty levers or win rates` のような見出しの一行だけが載り、Agent はその作業に入るときに `memory_list(topic=...)` で本文を読む。

~~~bash
mashu topic
mashu topic --add 難易度較正 --scope enrai --trigger "Before changing difficulty levers or win rates"
mashu topic --edit 難易度較正 --trigger "Before calibrating difficulty"
mashu topic --edit 委譲 --action delegate
mashu topic show 難易度較正
mashu topic --remove 難易度較正

mashu remember "Do not change ship stats to set difficulty" --topic 難易度較正
mashu deliver 1a2b3c4d topic --topic 難易度較正
~~~

使い分けの目安は次のとおり。プロジェクトのどの作業でも守るなら scope、そのプロジェクトの一部の作業でだけ効くなら topic を選び、特定の操作の直前に必ず目に入れたいなら、その topic に action を結びつける。action は topic の編集でだけ設定でき、作成時には聞かれない。一つの action に結びつく open な topic は一つだけで、別の topic に移すときは先に元の topic から外す。一覧と `mashu topic show` は結びついた action を `before: delegate` のように示す。remove できるのは active なルールを持たない topic だけだ。一度も使われていない topic は削除され、名前も空く。退役したルールや提案が名前を参照している topic は、履歴を保つために archive され、一覧から消える。topic の作成と編集は Settings TUI の Topics からもできる。

## Project と Task

Project は Task をまとめる単位。Scope が知識の配信範囲を決めるのに対し、Project は作業状態の所属を決める。

~~~bash
mashu project list
mashu project create website --scope frontend
mashu project show website

mashu task list
mashu task list --dormant
mashu task list --closed
mashu task show 1a2b3c4d
mashu task create "Add CLI help" --project mashu --goal "Every command explains itself"
mashu task touch 1a2b3c4d
mashu task close
mashu task close 1a2b3c4d --outcome completed --reason "Merged and released"
mashu task close 1a2b3c4d 5e6f7a8b --outcome superseded
mashu task reopen 1a2b3c4d
~~~

`mashu task create` は `--project` を省略できる。cwd の route から一意に決まる Project、または開いている Project が一つだけならそれを使う。候補が複数なら選択を求める。Work TUI でも同じ候補解決を使い、一件だけならその Project を既定値として表示する。

`mashu task close` を引数なしで叩くと決定の画面が開く。open な Task が 1 行ずつ並び、選択中の goal、status、approach、open questions、blockers、next actions と close proposal を一覧の下で読める。↑↓ または j / k で移動し、`/` で ID、Task 名、Project、Current State、proposal の理由を絞り込む。c / a / s で outcome を選ぶ。Agent が終了を提案している Task は先頭に集まり、Enter で表示中の proposal を受理する。Work 画面の `c` も選択中の一件を同じ close 画面で開く。proposal と state が表示後に変わっていたら受理を拒否し、stale proposal の確認は受理するときだけ行う。c / a / s で明示的に選んだ outcome は proposal を暗黙に受理しない。reason 入力の Ctrl+C は close 全体を取り消す。まとめて閉じる鍵は無い。

Task の Current State には goal、approach、status、open questions、blockers、next actions が入る。Attempt、Decision、Checkpoint、Artifact Reference は履歴として別に残る。

bootstrap が常時配信するのは、完全な Task ID、Task 名、goal、status、詳細項目の件数、最終確認日からなる短い card だけである。approach、open questions、blockers、next actions の本文は配信しない。Agent は Task に着手する前に、card の完全な Task IDで `task_get` を呼び、Current State 全文を取得する。

- Agent は MCP で Current State と履歴を更新する
- User は CLI / TUI で Task を作成・touch・close・reopen でき、TUI では Task 名・Project・Current State も訂正できる
- open Task は活動が 14 日途切れると dormant になり、bootstrap から外れる。履歴は残る
- dormant は終了ではない。task touch で活動期限を延長して戻せる
- Task を closed にできるのは User だけ。outcome は completed、abandoned、superseded のいずれか
- Agent は task_propose_close で「終わったと思う」と根拠つきで提案できる。提案は open / closed を動かさず、lease も延ばさない。User が同じ outcome で理由を書かずに close すると、提案の根拠がそのまま close_reason になる

task_update と task_checkpoint は、渡した欄だけを置き換える。省いた欄は前の値を保ち、欄を空にするときは空文字か空リストを渡す。MCP で Current State 全文を返すのは task_get だけで、他の Task 系 Tool は goal・status・詳細件数からなる card view を返す。task_get と write の成功応答は card と full detail の予算残量を含み、card 上限を超えた write は超過量と欄ごとの内訳を返して event_log に記録される。

## Agent から使う

### MCP server の接続

Mashu は stdio の MCP server として動く。Claude Code から接続する例は次のとおり。

~~~bash
claude mcp add mashu \
  --scope user \
  --env MASHU_DATABASE_URL=dbname=mashu \
  -- /path/to/mashu/.venv/bin/mashu serve --agent claude
~~~

Agent 名は serve の --agent、または MASHU_AGENT で指定する。別の MCP client を使う場合も、同じく mashu serve を stdio server として登録する。

MCP tool は 19 個ある。

| 分類 | Tool | 役割 |
|---|---|---|
| Knowledge | session_bootstrap | セッション開始時に一度呼び、Memory・active Task card・Temporary Context を受け取る |
| Knowledge | pain_report | 事故または再調査を Ledger に記録する |
| Knowledge | trace_put | 調べて分かったことを日付つき Trace に残す |
| Knowledge | trace_search | Trace だけを検索する |
| Knowledge | memory_list | 指定 Scope の active Memory を一覧する。`topic` を渡すと、その topic の本文を読み、読んだことを記録する |
| Knowledge | memory_get | 指定 Memory の本文、revision、evidence、退役種別・理由・後継を読む |
| Knowledge | memory_nominate | 新規 Memory の pending candidate を作る。replace の後継に使う |
| Knowledge | memory_admit | 承認根拠を必須にして採用する。`content` を渡せば候補の作成と採用を 1 回で行い、読むべき候補や conflict があれば止まる。止まった候補は `nomination_id` と読み取った version で採用する。request replay は初回応答を返す |
| Knowledge | memory_change_propose | retire / replace / restore / redeliver を作成または更新する。redeliver は本文を変えずに配信条件だけを移し、新しい topic も作れる。replace は後継の内容・version と配信条件を固定する |
| Knowledge | memory_change_apply | proposal の version と対象・conflict・承認根拠を照合して適用する |
| Knowledge | memory_change_withdraw | 不要になった pending proposal を取り下げる |
| Project State | project_list | Project と Task 件数を一覧する |
| Project State | task_create | 類似する open Task を確認して Task を作る |
| Project State | task_get | Task の Current State 全文を取得し、指定した履歴だけ展開する。着手前に必須 |
| Project State | task_search | Task 名と Current State を検索する |
| Project State | task_update | Current State を置換する。`name` を渡すと Task 名も変える |
| Project State | task_checkpoint | Current State を置換し、区切りとして履歴を凍結する。`attempts`（試したことと結果）、`decisions`（判断と理由）、`artifacts`（外部成果物の原典）も同じ transaction で追記し、artifacts は checkpoint の evidence になる |
| Project State | task_propose_close | Task の終了案を理由つきで置く |
| Project State | task_withdraw_close_proposal | close proposal を取り下げる |

Agent が守る基本の動詞は次のとおり。

~~~text
調べて分かった          → trace_put
実際に困った            → pain_report
User が「覚えて」と言った → memory_admit（content）
User が既存 Memory の変更を指示 → memory_get → memory_change_propose → memory_change_apply
作業の区切り            → task_checkpoint（attempts / decisions / artifacts を添える）
~~~

task_checkpoint の `attempts` には失敗した試行の結末、`decisions` には理由を失うと再導出コストが高い判断、`artifacts` には外部成果物の原典を入れる。一つでも拒否されれば checkpoint 全体が書かれない。Agent が独自に考えた Memory 変更は提案に留め、明示指示があった場合だけ MCP から適用する。ユーザーの会話指示の出所は記録されるが、認証されるわけではない。Temporary Context の登録、直接 Memory 操作、proposal review、Task の close / reopen は User の CLI / TUI 操作だ。終わったと思ったら status_text にそう書くのではなく task_propose_close を使う。前者は User に読み直しと打ち直しをさせ、後者は 1 打鍵で決まる。

### PreToolUse hook で action つきの topic を示す

tools/pretooluse_guard.py を PreToolUse hook に登録すると、action を結びつけた topic のルールが、その action に当たる呼び出しの直前に表示される。最初の呼び出しは一度止まり、読んだうえで同じ判断なら同じ呼び出しをもう一度行う。止めるのはセッションごと（compaction をまたぐと改めて）一回だけだ。DB に接続できない場合は作業を止めない。

以前の delivery `guard` の Memory は、migration `0011_guard_into_topics.sql` が action と同じ名前の topic へ移し、その topic を action に結びつける。

既存の hook 設定がある場合は、次の entry をその設定に追加する。

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

Task と Agent は組み込みの delegate action として扱われる。それ以外の client tool や、Bash の command から action を判定する規則は、インストールごとに異なるのでリポジトリ外に置く。既定のファイルは .mashu-guard-actions、別の場所を使うときは MASHU_GUARD_ACTIONS で指定する。

1 行 1 規則で、形式は judgement、subject、expression。

~~~text
delegate tool mcp__some-server__start_task
remote-shell command \b(cluster-wrapper|scheduler-cmd)\b
~~~

subject は tool または command。tool は名前全体、command は正規表現で照合する。壊れた行はその行だけ無視される。

### SessionStart hook で開始時の配信を自動化する

tools/sessionstart_guard.py を SessionStart hook に登録すると、startup / resume では bootstrap の内容を配信し、compact では圧縮で落ちた内容を再配信する。

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

hook が DB に接続できない場合は何も出力せず終了する。フックのない client では、AGENTS.md または CLAUDE.md に次の 2 行を置く。

~~~markdown
# Mashu（外部記憶）

- Mashu MCP が接続されているセッションでは、開始時に一度 session_bootstrap を呼ぶ。SessionStart hook が既に配信していれば不要
~~~

### セッション開始時に返るもの

session_bootstrap は次の順序で返す。

1. always Memory
2. 現在の Scope の Memory
3. topic の見出し（名前・ルール数・発動条件）
4. active Task の card（完全な Task ID・名前・goal・status・詳細件数・最終確認日時）
5. 有効な Temporary Context
6. pending candidate の件数

topic の本文、approach、open questions、blockers、next actions の本文、および Trace、Ledger、Attempt、Decision、Checkpoint、dormant Task、closed Task は bootstrap に載らない。active Task を続ける前に `task_get` で詳細を取得する。

## 書き込みの安全規則

- 本名、所属、学籍番号、ホームディレクトリを含む絶対パスなど、個人識別情報を含む書き込みは入口で拒否される
- 禁止パターンはリポジトリ外の .git-banned-patterns に置く。MASHU_BANNED_PATTERNS で場所を変更できる
- 禁止パターンの一覧が見つからない場合は、検査を通すのではなく「検査できない」として扱う
- commit hook は staged path と blob、commit message を検査し、禁止パターンの構文エラーや staged blob の取得失敗でも commit を拒否する
- Ledger、Memory の revision history、event_log は append-only で、DB の trigger が書き換えを拒否する
- bootstrap と類似照合結果は退役した Memory の本文を返さない。明示的な `memory_get` / `mashu show` では管理判断のため全文・revision・evidence と退役種別・理由・後継または移動先を返す。invalidated / legacy conflict を採用するには理由を踏まえた明示指示が要る

## 開発

~~~bash
uv run pytest -q
uv run ruff check
uv run ruff format
~~~

テストは実 PostgreSQL に対して実行し、テスト用データベースは実行ごとに作り直す。既定値は mashu_test、変更には MASHU_TEST_DB を使う。

main への push と pull request では、GitHub Actions の CI（`.github/workflows/ci.yml`）が ruff の lint と format の確認、tests/ の行数予算の確認、テストを実行する。テストは Ubuntu の Python 3.11 と 3.13、Windows の Python 3.13 で、それぞれ PostgreSQL 17 に対して走る。

tests/ の空行を除いた行数には予算があり、`tests/line-budget` が持つ。Agent は変更のたびにテストを足して減らさないので、放っておくとテストがコードより速く育つ。tests/ に触れる commit では pre-commit hook が、push と pull request では CI が、予算と実際の行数の一致を確かめる。行数が予算を超えた commit は拒否され、減った commit では予算をその値まで下げる必要がある。テストを足すときは、先に既存のテストを広げること、conftest の共有 fixture で状態を作ること、一つの不変条件を一つの層でだけ確かめること、画面や CLI の文言ではなく状態を確かめることで場所を空ける。予算を上げるかどうかはユーザーが決める。上げるときは予算ファイルだけを変える commit を作り、ユーザーの指示を `Test-Budget-Instruction:` の trailer に引用する。

仕様を変えるときは、コードと対応する設計書を一緒に更新する。撤回した設計は削除せず、何を撤回したかと理由を設計書に残す。

## 主な配置

~~~text
docs/mashu-v2.md             Memory の実装仕様書
docs/mashu-v3.md             Project State の実装仕様書
migrations/                  連番の SQL。mashu admin migrate が順に実行
src/mashu/                   実装
tests/                       実 PostgreSQL に対するテスト
tools/pretooluse_guard.py    action つき topic 用 PreToolUse hook
tools/sessionstart_guard.py  SessionStart 用 hook
hooks/                       pre-commit / commit-msg
.github/workflows/ci.yml     GitHub Actions の CI
~~~

*摩周湖は世界最高クラスの透明度が観測されたことで知られており、どこまでも遡って追跡できる Knowledge State を目指して命名された。摩周湖の流入する川も流出する川もなく、外部の流れに属さない閉じた水盆という地形は、特定の Agent に依存しない独立の Knowledge Layer という設計思想に対応する。摩周湖は霧で見えないことで有名であり、本プロジェクトの意義は霧を晴らして何を信頼してよいかを人が判断できる状態にすることである。*
