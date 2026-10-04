# Mashu v3 — Persistent Agent State / 設計書 v3

**Mashu v3 は、覚えておくべきことと、いまやっていることを、同じ窓口から見えるようにする。ただし両者を同じものとしては扱わない。**

v2 の中核原理

> 忘却による損害が実証された知識だけを常設化する

は撤回しない。v3 が足すのは、それとは身分の異なる「現在の作業状態・試行・判断」を扱う **Project State** である。

本書は v3.0 草稿を全面改稿した v3 の正本である。**Memory subsystem の正本は引き続き `docs/mashu-v2.md` であり、本書はそれを置き換えない。**本書が規定するのは Project State の追加と、bootstrap・token budget の統合だけである。現在の Memory schema と信頼境界は v2 文書に記す。

## 1. なぜ Project State を足すか

v2 は作業状態を意図的に対象外とし、「プロジェクト側の文書か Obsidian が持つ」とした。運用を始めると、この線引きの圧力が二つの形で観測された。

**第一に、作業状態が Memory の入口に押し寄せている。**再入場（2026-08-28）の台帳を見ると、「TBP/TOPO だけ `_reopt2` が最終構造」「モデル化合物の正称は HEMP」「fgroup のパイプラインの置き場所」のような、恒久規則ではなく特定プロジェクトの現況に属する事実が相当数、Memory として入場している。これらは腐る。プロジェクトが進めば最終構造の枝番も置き場所も変わるが、Memory の退役は人の手作業である。作業状態に置き場が無いために、腐る事実が腐らない前提の席（定員 2000 token）を使っている。

**第二に、セッション再開のたびに現在地を拾い直している。**複数マシン・複数 CLI で運用しているが、現在どこまで進んだか・何を試して駄目だったか・なぜ今の方針なのかは、git 履歴・repository 内文書・Obsidian・過去の会話に分散しており、Agent が一つの窓口から復元できない。再開のたびの拾い直しは、v2 の言葉で言えば friction の反復である。

ただし正直に書く。「再開の失敗が誤った作業を生んだ」という incident としての台帳記録はまだ薄い。したがって v3 自身の反証条件（17 節）に stale-state incident と再開コストの指標を含め、この判断そのものを運用で検証可能にする。

もう一つの選択肢、すなわち作業日誌 Markdown への統合は採らない。Agent が追記を続ける自由文書は数万行の履歴になることが v1 型の失敗として予測でき、読む側の予算を必ず破る。

## 2. 三つの身分

Mashu 内には保存目的の異なる三種類の情報が存在する。

```text
Mashu
├── Memory          忘れると実害が出ることが実証された恒久知識（v2 のまま）
├── Project State   現在の作業状態・試行・判断・成果物参照（v3 で追加）
└── Trace           再導出を検出するための短寿命・未検証観測（v2 のまま）
```

この三者を共通の knowledge 型へ一般化しない。`type = memory / task / state` のような万能 Entity は v1 への逆戻りであり、作らない。

時間的意味も混同しない。

| 情報 | 主張していること |
|---|---|
| Memory | 今後も守るべき |
| Current State | この日時にそうであると最後に確認された |
| Attempt | この日時にこう試して、こうなった |
| Decision | この日時にこの理由でこう決めた |
| Trace | この日時にこう分かった |

## 3. 境界の扱い

**Product boundary は統合する。**repository・PostgreSQL・migration・MCP server・CLI・Scope/Route・bootstrap・event_log・token budget・PII 拒否・短縮 ID 解決は一つであり、ユーザーと Agent からは `mashu` 一つだけが見える。

**Semantic boundary は分離する。**Memory と Project State は同じテーブルにも同じ lifecycle にも入れない。

| | Memory | Project State |
|---|---|---|
| 主目的 | 恒久的な失敗防止 | 作業の継続 |
| 入口 | 損害の実証 | 作業の発生 |
| 主な書き手 | User の直接操作、または明示指示を運ぶ Agent | Agent |
| 決定境界 | Agent 独自の案は pending。明示指示の実行は出所を記録するが、server は真正性を認証しない | 日々の state 更新に都度の承認は不要 |
| 寿命 | 原則恒久 | 短い |
| 更新 | 稀 | 頻繁 |
| 腐敗対策 | 入口を絞る | lease による退場 |
| bootstrap | 全量 push | active Task の索引 card のみ。Current State 全文は `task_get` |
| 履歴 | revision | checkpoint / attempt / decision |
| 終了 | retire / replace / restore。理由と退役種別を記録 | close（User）/ dormant（lease） |

Project State から Memory への昇格経路は作らない。作業中に恒久規則が見つかったなら、それは通常どおり `pain_report` の実証経路か、User の明示指示による `memory_admit` を通る。

### 3.1 信頼境界 — 明示指示と Project State の継続更新

Memory の信頼境界は v2 5.1 節に定める。Agent は自分の判断で恒久 Memory を採用・退役・置換・復帰できない。Agent が独自に考えた変更は根拠つき proposal として pending に置く。一方、会話中のユーザー明示指示を Agent が運ぶ場合は、その指示を `user_instruction` として記録して同じセッションで実行できる。actor は Agent のまま残す。短い原文と会話参照は監査用であり、サーバーが指示の真正性を認証するものではない。Agent の有用性判断・confidence・ユーザーの沈黙は承認ではない。

Project State は性質が異なる。Agent が書いた Current State のうち Task 名・goal・status・詳細件数からなる索引 card は、人の review を経ずに次のセッションの bootstrap へ push される。approach・open questions・blockers・next actions の本文は `task_get` で明示取得する。

都度の承認を省く理由は、作業状態の性質にある。頻繁に変わるものに毎回人の確定を要求すれば書かれなくなり、書かれない state は存在しないのと同じである。恒久 Memory はこの性質を持たず、明示指示のない Agent の変更は proposal のまま残す。

緩和の被害は三方向から bound する。

1. **サイズ** — bootstrap card と Current State 全文には別々に store が強制する hard limit がある（5.3 節）。長大な誤情報は物理的に書けず、詳細が常時注入を膨らませることもない
2. **時間** — Activity Lease（7 節）により、活動の証拠が絶えた state は bootstrap から退場する
3. **提示** — 正しさそのものは bound できないので、配信の形式で受ける。**Task card は「現在の真実」の顔で配信しない。**card には必ず最終確認日時を添え、「YYYY-MM-DD に最後に確認された索引」として提示する。Memory（今後も守るべき規則）と同じ顔をさせず、着手前の `task_get` を要求する

v2 の trace が持つ「書き忘れても検出が弱くなるだけで、嘘は生まれない」という性質を、Current State は持たない。セッションが作業したのに state を更新しなければ、次のセッションには古い card が届く。日付の明示はこの失敗を無くすのではなく、**受け手が古さを判定できる形にする**ための最低限である。Agent は card だけで着手せず、完全な task_id で `task_get` したうえ、日付が古ければ repository と artifacts で裏を取る。この規律は bootstrap と server instructions の両方が運ぶ。

## 4. Single storage ではなく single view

Mashu は外部成果物を吸収しない。Git・Obsidian・repository 文書・実験データは、それぞれの場所が正本であり続ける。Mashu が持つのは「現在どうなっているか・何を試したか・なぜそうしたか・原典はどこか」だけである。

```text
result:   parser の error handling を実装
evidence: git commit 9d12f5a
```

で十分であり、diff・コード本文・ログを Mashu に複製しない。これは禁止事項として schema で強制する（5.4 節の文字上限がその実装である）。

## 5. Project State subsystem

構成要素は Project、Task、Current State、Checkpoint、Attempt、Decision、Artifact Reference の七つである。

### 5.1 Project

Task の所属単位。通常は repository または研究単位に対応する。

```text
id / name / scope_id / created_at / archived_at
```

Project と Scope は同じ概念ではない。Scope は delivery / routing の境界、Project は work state の所属である。初期実装では一つの Project が一つの主要 Scope を持つ形から始め、多対多への一般化は必要が実測されるまでしない。Scope を持つ Project の active Task card はその Scope だけに配信する。Scope を持たない Project の card は全 Scope 共通であり、unrouted session にはこの共通分だけを配信する。Current State 全文は Scope にかかわらず、Task IDを指定した `task_get` にだけ返す。

### 5.2 Task

一回の chat request ではなく、**継続して状態を保持する意味的な作業単位**である（例: 「v3 schema 実装」「PNtBAm 文献調査」「ポスター改稿」）。

保存される status は `open` / `closed` の二つだけである。active と dormant は保存されず、lease から導出する（7 節）。

**生成時に重複照合を行う。**nomination が pending 照合で二重の席を防ぐのと同じ理屈で、`task_create` は生成前に既存の open な Task（dormant 相当を含む）と名前・goal を pg_trgm で照合する。閾値以上の一致があれば新規作成せず候補を返し、Agent に既存 Task の継続か明示的な新規作成かを選ばせる。これが無いと、Agent が `task_search` で既存 Task を見つけ損ねるたびに Task が分裂し、state が二箇所に育つ。

### 5.3 Current State

Task の「今」だけを持つ。履歴を書かない。状態が変われば**置換**し、古い記述を残さない。

置換は欄ごとに行う。渡した欄は丸ごと置き換え、省いた欄は前の値を保ち、空文字か空リストを渡した欄は空になる。欄の中身へ継ぎ足すことはしない。goal と approach はめったに変わらないので、全欄の送信を求めると、status だけを書き換えるつもりの呼び出しがこの二つを黙って消す。

```text
goal / approach / status_text / open_questions / blockers / next_actions / updated_at / updated_by
```

**hard limit は store が強制する。**「簡潔に書け」という指示は情報量制御にならないので、DB / service 層で拒否する。

```text
goal             80 chars
approach        500 chars
status_text     120 chars
open_questions  最大 5 件 × 300 chars
blockers        最大 5 件 × 300 chars
next_actions    最大 5 件 × 300 chars
bootstrap card  200 estimated tokens / Task
full detail     800 estimated tokens / Task
```

goal と status は Scope の全セッションへ card として push されるので、他の欄より短く抑える。運用では status が「何をしたか」の日誌になり、コミットのハッシュ・パス・測定値が並んで、一つの Scope の card 枠を埋めた。status は Task がいまどこにいるかを一、二文で言う欄で、したことは checkpoint の what_changed、コミットやパスは Artifact Reference、残りの作業は next_actions が持つ。上限は書き込む値にだけ掛け、書き換えずに引き継がれた値には掛けない。上限を締める前に書かれた state を、別の欄の更新のたびに書き直させないためである。DB の CHECK 制約はそのため 0004 の広い上限のまま残す。

初期値であり、実測後に変更してよい。bootstrap card は Task 名・goal・statusと、approach / open questions / blockers / next actions が存在することを示す件数だけから作る。full detail は全欄を含むが常時配信しない。長文が必要なら原典を Artifact Reference に置く。

**上限までの距離を書き手に返す。**上限は拒否されて初めて知るものにしない。`task_get` と MCP の `task_create` / `task_update` / `task_checkpoint` の成功応答は `card_tokens`・`card_limit`・`card_remaining`・`detail_tokens`・`detail_limit` を含む。card 上限を超えた書き込みは、`field`・`limit`・`actual`・`unit`・`over_by` と、card の token を Task 名・goal・status_text・詳細件数・行 overhead に割り振った `breakdown` を構造化して返す。内訳の合計は card の token 数に一致するので、Agent はどの欄を何 token 削れば通るかを試行せずに知れる。MCP の write がこの理由で拒否されたときは、本文を保存せず、拒否された書き込みの transaction とは別に `task_card_write_refused` を event_log に記録し、応答の `refusal_recorded` で記録できたかを示す。

MCP で全文の state を返すのは `task_get` だけである。検索結果、重複候補、楽観チェックで返す現在の state、write の成功応答は card view、つまり goal・status_text・詳細の有無と件数・`updated_at`・`updated_by` だけを返す。全文を write の応答で返すと、読んだつもりのない state が文脈に積もり、card と full detail を分けた意味が薄れる。checkpoint の応答も凍結した state の本文を除く。

**追記は一箇所だけ許す。**`append_next_action` は next_actions に 1 件足すだけで他の欄に触れない。呼び出すのは痛みの記録（9 節の `work`）であって、state を読み終えたセッションではない。痛みは痛かった当人がその場で書くものである。置換は next_actions を丸ごと置き換え、読み取り時の `updated_at` も要求するので、置換で書かせれば、報告は既存の next actions を消すか、state を読みに行く往復のせいで行われないかのどちらかになる。上限・入口拒否・予算判定は置換とまったく同じものを通すので、追記で書ける state は置換でも書けた state に限られる。追記も `updated_at` を動かすため、追記前に読んだ置換は楽観チェックで拒否される。

**並行する置換は楽観チェックで受ける。**複数セッションが同じ Task を同時に更新しうる。`task_update` は読み取り時の `updated_at` を添えて置換し、不一致なら拒否して現在の state を返す。黙って last-writer-wins にすると、並行セッションの一方の作業が痕跡なく消える。

**予算境界の挙動も store が持つ。**Task card 枠は Scope ごとに独立する。`task_update` の結果、その Scope に配信される active Task card の合計が枠（8 節）を超えるなら、書き込みを拒否し、現在の内訳と出口を返す。Scope を持たない Project の card は全 Scope 共通分として各枠に含め、共通分への書き込みは最も重い Scope に対して判定する。別 Scope の固有 card は競合しない。full detail の本文はこの配信枠を消費せず、1 Task 800-token の上限で別に bound する。溢れたぶんを黙って切り詰めない。

ただし**その Task 自身の card を小さくする書き込みは、枠を超えている状態からでも通す**。枠は設定値であり、card の重さは配信の形から計算されるので、どちらも誰も触っていない行の下で動きうる。つまり書き込みを経ずに枠を超えた状態が成立する。そこで一律に拒否すると、他の active Task だけで枠を超えている場合、対象を空にしても総量が枠を割らないため拒否され、「他を縮めろ」と言いながらその縮約自体を拒む閉じた輪になる。入口拒否の思想は保ったまま、枠が求めている向きの書き込みだけを通す。

### 5.4 Attempt / Decision

**Attempt** は試行錯誤の append-only 記録である。

```text
task_id / attempt (300) / result (500) / reason (500) / next (300) / created_at / created_by
```

**Decision** は「なぜそうしたか」を後から失うと再導出コストが高い判断だけを残す。append-only で、変更は `supersedes_id` を張った新 Decision で行い、過去の行を書き換えない。

```text
task_id / decision / reason / supersedes_id / created_at / created_by
```

どちらもコード・ログ・長文引用は文字上限が物理的に拒否する。Agent はどちらも `task_checkpoint` の `attempts` / `decisions` として区切りの checkpoint と一緒に書き、追記だけの Tool は持たない（5.6 節）。どちらも bootstrap には載らず、`attempt_list` / `decision_list` で明示取得する。

Trace と Attempt は似ているが統合しない。Trace は「この日に調べて分かった」、Attempt は「この Task でこう試してこうなった」であり、重複が運用で実測された場合のみ再検討する。

### 5.5 Artifact Reference

Mashu 外の正本への参照。

```text
task_id / kind / locator / label / created_at
```

kind は `git_commit` / `git_branch` / `file` / `document` / `obsidian` / `issue` / `dataset` / `log` / `url` / `other`。本文は保存しない。Agent は `task_checkpoint` の `artifacts` として書き、そこで作った参照はその checkpoint の evidence に加わる。

### 5.6 Checkpoint

まとまった作業の区切りで Agent が打つ。`task_checkpoint` は **Current State の置換と、履歴行の凍結を一度に行う**。すなわち checkpoint は task_update の上位互換であり、Agent が覚える動詞を増やさない。

区切りまでに生じた Attempt・Decision・Artifact Reference も同じ呼び出しの `attempts` / `decisions` / `artifacts` で渡す。state の置換、それらの追記、checkpoint の凍結は一つの transaction で行い、文字上限・入口拒否・kind・supersedes の検査は書き込みの前にすべての行へかける。一行でも拒否されれば何も書かない。記録の粒度を checkpoint に揃えることで、履歴を書くための動詞を別に覚えさせずに済む。

```text
what_changed / 凍結時点の Current State / evidence (artifact 参照。同じ呼び出しで作った参照を含む) / created_at / created_by
```

Checkpoint は append-only の履歴であり、bootstrap には載らない。そして **Checkpoint は Task の終了ではない**。

### 5.7 要約を永続化しない

session summary・daily summary・進捗レポートの類は保存しない。必要なら Current State・Attempt・Decision・Artifact Reference からその場で生成し、生成物を新しい正本にしない。要約の要約が育ちはじめたら、それは v1 の再演である。

## 6. Task の終了 — close は人だけ

Task の完了を Agent に推測させない。Agent が「実装は完了したと思われる」と判断しても `closed` にはできない。User が最後の手作業をする、User が結果を見て判断する、Agent が完了条件を誤認する、Agent の担当範囲だけが終わる。いずれも v1 で実測された誤判定の形である。

```text
mashu task close                     決定の画面。open な Task を一覧し、1 件 1 打鍵
mashu task close <id>... --outcome   outcome: completed / abandoned / superseded
```

close 時は最終 checkpoint を凍結し、bootstrap から除外する。attempt / decision / artifact は保持する。closed の再開は `mashu task reopen <id>`（User のみ）。

### 6.1 Close Proposal — 判定は渡さない、転記だけ消す

原則が禁じているのは Agent が終了を**決める**ことであって、終了したと**言う**ことではない。両者を一緒に禁じた結果が実測された摩擦である。Agent はブランチをマージした直後に「削除済み。閉じてよい」と status_text に書き、User はその文を読み直し、ID をコピーし、そこに既に書かれている outcome を打ち直す。18 件なら判断 18 回ではなく、判断 18 回と転記 18 回だ。消せるのは後者だけである。

そこで Agent は `task_propose_close`（outcome と理由、理由は必須）で提案だけを置く。提案は Task の open / closed に一切影響せず、lease も延長しない（完了に見える Task を opening の先頭に留めてしまうため）。提案が立った状態で User が同じ outcome で close し、自分の理由を書かなかった場合、提案の理由がそのまま close_reason になる。同意した文をもう一度打たせないためである。outcome が違えば User は提案に反対したのだから、その理由は記録しない。

提案は `task_state.updated_at` を控える。提案後に state が書き換えられていれば「追い越された提案」として表示され、決定の画面では 1 打鍵での受理ではなく確認を挟む。画面と repository が食い違っていると分かっている唯一の形だからである。

## 7. Activity Lease — 沈黙は完了ではないが、現在でもない

close を User に強制すると、忘れられた Task が腐った state を永久に push し続ける。かといって沈黙を完了と解釈すれば、Agent に終了判定をさせないという 6 節の原則が裏口から破れる。この間を lease で取る。

open な Task は `last_activity_at` と `active_until` を持つ。`task_update` / `task_checkpoint` / User の `task touch` が lease を延長する。初期値は 14 日とし、計算のキュー待ちや査読待ちで正当に週単位の空白が生じる研究のリズムに対して短すぎるかどうかは、reactivate の頻度（17 節)で較正する。

**dormant は保存された状態ではなく、導出される状態である。**

```text
active  = open かつ now() <= active_until
dormant = open かつ now() >  active_until
```

遷移処理・常駐 worker は存在しない。読み出し側（bootstrap・task list）が判定するだけである。これにより「active→dormant の遷移の競合」という問題そのものが消え、reactivate は `task touch`（lease の再延長）と同じ操作になる。

dormant の意味は「終了した」ではなく「**現在進行中である証拠が一定期間観測されていない**」である。

```text
silence ≠ completion
silence = absence of evidence for current activity
```

dormant な Task は bootstrap に載らず、履歴は保持され、明示検索と reactivate が可能である。dormant の Current State を返すときは「Last known state as of YYYY-MM-DD」と明示し、現在の真実として提示しない（3.1 節の提示規律の一貫）。

これで「User が README を直して close を忘れた」ケースは、14 日後に自動で bootstrap から消え、勝手に completed にもならず、後日検索すれば「この日が最後に確認できた状態」と返る。**終了を正しく判定できなくても、腐った working state を配信し続けない。**

## 8. Bootstrap と token budget の統合

`session_bootstrap` は一箇所で全 persistent context を組み立てる。返す順序は

```text
1. Memory: always
2. Memory: 現在 Scope
3. topic の見出し（名前・規則数・発動条件。本文は memory_list(topic=...) で読む）
4. active Task の bootstrap card（完全な Task IDと最終確認日時を明示）
5. Temporary Context
6. pending nomination 件数
```

approach / open questions / blockers / next actions の本文、および Attempt / Decision / Checkpoint / Trace / Ledger / dormant / closed は載らない。

Task card は Task 名・goal・statusをラベルつきで運び、approach / open questions / blockers / next actions は本文でなく存在と件数だけを示す。card は作業を開始するための state ではなく、どの Task の詳細を取得するか選ぶ索引である。各 row は MCP がそのまま `task_get` に渡せる完全な `task_id` を持ち、bootstrap 全体も「着手前に task_get」を明記する。これにより、存在を忘れて検索できない問題は card の push で防ぎ、詳細の常時注入は避ける。

初期 hard cap は次のとおりで、実測較正する。既存の `MASHU_CAPACITY`（2000）/ `MASHU_ALWAYS_CAPACITY`（800）と同じ環境変数方式で構成する。

```text
TOTAL            4000 tokens
  Memory         2000（always は 800 まで。各 Scope は always と合わせて 2000 まで。topic の見出しを含む）
  Topic body      800 / topic（非配信）
  Task cards     1600 / scope（200 / Task）
  Task detail     800 / Task（非配信）
  Temporary       400
```

重要なのは、**Agent に押し込まれる総量を Mashu が一元管理し、subsystem が互いの枠を暗黙に借りない**ことである。Memory が余った Task card 枠を恒久的に使うことも、その逆もしない。Memory の定員判定は v2 のまま admission 時に、Task card は Scope ごとの配信量を 5.3 節のとおり書き込み時に判定する。`mashu status` は card の全 Scope 合計ではなく、最も重い Scope の配信量を表示する。full detail は「入りきらないため検索へ落とす」のではない。存在と取得先を必ず push したうえで、選んだ Task の既知の IDから決定的に pull する。Memory は存在自体を忘れるため本文 push が必要だが、Work は card が取得契機を運ぶため、この二段構造を取れる。

guard（v2 6.2 節、action を持つ topic）は Durable Memory の配信機構であり、Project State はそこに使わない。

## 9. 書き込み規律 — Agent が覚える動詞を最小にする

Agent の書き分けは次の四行に収まるように設計する。これを超える規律は server instructions に載せても守られない。

```text
調べて分かった            → trace_put
痛かった                  → pain_report
User が覚えてと言った     → memory_admit
作業の区切り              → task_checkpoint
```

task_checkpoint の `attempts` と `decisions` は「失敗した試行の結末」「後から理由を失うと高くつく判断」に限って埋める欄であり、毎回の checkpoint で書くものではない。独立した動詞にはしないので、上の四行は増えない。

**痛みの答えには二つの形がある。**v2 では、痛みを防いだはずのものはすべて「誰かが毎回持っているべき一文」＝規則の形をしていた。それ以外の置き場が無かったので、そう書くしかなかったのである。Project State ができた以上その前提は無くなる。一度直せば以後は何も覚えなくてよい変更 ＝ 作業は、規則ではない。

```text
毎回持っていないと再発する    → prevention_kind=rule（既定）。候補になり、人が席を決める
一度直せば終わる              → prevention_kind=work。候補にならず、task の next_actions へ入る
```

`prevention_kind=work` は review を回避する経路ではない。台帳行はどちらでも書かれ、忘却が何を払わせたかを数えるのは台帳だからである。変わるのは、Review 卓に何を載せるかだけである。Review 卓の動詞は確定・却下・保留の三つで、そこに「やる」は無い。作業をそこへ載せると、決められない項目が席を待ち続ける。

正確には、`work` は**自分では候補を作らない**のであって、台帳から消えるわけではない。同じ文面が後日 `rule` として報告されたとき、`work` の friction 行は再導出の前半として数えられる。二度調べたという事実は事実であり、答えが規則か作業かという報告者の見立ては、穴が実在するかどうかとは別の話だからである。

`task_id` を伴わない `work` は拒否せず記録し、**「どこにも提出されていない」と名指しして返す**。台帳側は `filed_task` が NULL のまま残るので、後から未提出の作業を数えられる。Task を立てていない場面で痛みの記録そのものが失敗する方が高くつく、という判断である。

記録漏れは起こる前提で設計する。checkpoint が打たれなかったセッションの被害は「次のセッションが古い日付の state を見て、裏を取りに行く」であり（3.1 節）、誤情報の自動生成より安全側にある。記録率は instructions と hooks（Stop フックでの促し等）で改善するが、記録の強制はしない。

書き込み権限の全体は次のとおり。

| 書けるもの | Agent | User |
|---|---|---|
| trace / pain / nomination | ○ | ○ |
| task 作成・state・checkpoint・attempt・decision・artifact | ○ | ○ |
| task close / reopen | × | ○ |
| close proposal（提案のみ・決定ではない） | ○ | ○（取り下げ） |
| Memory admission / retire / replace / restore / redeliver | 明示指示どおりの実行、または pending proposal | 直接操作 |
| Memory revise | × | ○ |
| Temporary Context | × | ○ |
| Scope / Route | × | ○ |
| Topic | 明示指示どおりの採用・配信変更に伴う作成のみ | ○（適用された redeliver / replace 提案でも作られる） |

## 10. PII と入口拒否

v2 の入口拒否（`redact.py`、commit hook と共用の禁止パターン）を Project State の全書き込みに適用する。本名・所属・学籍番号・secret・token・ホームディレクトリを含む絶対パス。検査ファイルが読めないときは「合格」ではなく「検査できなかった」と報告したうえで書き込みを通す（`unchecked` の印が結果に付く）。v2 の既存挙動と同じ扱いである。

## 11. 自動処理は決定的なもののみ

LLM background worker や、利用頻度・経過日数・confidence による自律的な Memory 採用・退役は置かない。実行できるのは、明示指示に従った操作、登録済み Temporary Context の期限失効、承認済み置換に伴う旧 Memory の退役までである。その他の存在する自動処理は Trace expiry・lease の導出判定・token count・hard limit・重複照合・orphan reference check であり、決定的である。「この Task は終わったように見える」という LLM 判定はしない。

## 12. v2 文書への修正点

v3 実装のマージ時に、`docs/mashu-v2.md` の次の箇所へ注記を入れる（前提を変えた実装と同じコミットで直す）。

v2 の Memory 信頼境界と Tool は `docs/mashu-v2.md` を正本として更新する。Project State 側の本文は本書 3.1 節で、明示指示による Memory 実行と、Agent が自律判断で恒久化しない境界を説明する。

## 13. インターフェース追加

### 13.1 MCP Tool（Memory 関連 11 + Project State 関連 8）

Memory 関連 Tool は `docs/mashu-v2.md` 8.1 節に記す。`session_bootstrap` は返却内容が広がり、`pain_report` は 9 節の二つの形を受けるため `prevention_kind`（`rule` 既定 / `work`）と `task_id` を取る。Project State 関連の追加は

| Tool | 役割 |
|---|---|
| `task_create` | 重複照合つきの Task 作成（5.2 節） |
| `task_get` / `task_search` / `project_list` | 明示取得・検索 |
| `task_update` | Current State の置換と任意の Task 名変更（楽観チェック・重複照合・hard limit・予算判定つき） |
| `task_checkpoint` | 置換 + attempt / decision / artifact の追記 + 履歴凍結（5.6 節） |
| `task_propose_close` / `task_withdraw_close_proposal` | 終了の提案と取り下げ（6.1 節） |

`attempt_list` / `decision_list` / `artifact_list` は `task_get` の展開引数として提供し、Tool 数の増殖を避ける。全文の state を返すのは `task_get` だけで、他の Task 系 Tool は card view と予算を返す（5.3 節）。**`task_close` は Agent 用 MCP に置かない。**提案は close ではないので `task_propose_close` はこの禁止に触れない。

### 13.2 CLI 追加

```text
mashu                           人向け dashboard。Attention / Memories / Work / Settings を開く
mashu project list / create / show <id>
mashu task list [--dormant] [--closed]
mashu task show <id>            state・履歴・artifacts を全文
mashu task create / touch <id>
mashu task close                決定の画面（引数なし）
mashu task close <id>... [--outcome ...] / reopen <id>

mashu pain --prevention-kind {rule,work} [--task <id>]   9 節の二つの形
```

dashboard は Attention / Memories / Work / Settings & health の四領域を持ち、各画面から戻るたびに件数を再集計する。Attention は review、close proposal、dormant Task を扱う。Work は active / dormant / closed Task と Project を閲覧・検索し、Current State、proposal、全履歴、artifact を表示する。User は Task の create / touch / close / reopen に加え、open Task の名前・所属 Project・Current State と Project の名前・Scope を編集できる。編集入力は現在値を入力バッファへ入れて開き、全文の再入力ではなく既存値の部分修正として扱う。現在値を人が訂正する経路であり、checkpoint / attempt / decision / artifact の追記履歴は編集しない。Settings & health は status、現在の cwd に対する bootstrap preview、Scope と route の作成・編集、migration を扱う。

Work の `c` は選択中の Task だけを同じ close decision screen で開く。専用一覧、Work、CLI の `task close <id> --outcome ...` は、明示 outcome と proposal acceptance を別操作にする。proposal acceptance は画面に出した proposal と state を transaction 内で再照合する。stale 確認は proposal を受理するときだけで、reason の入力中に Ctrl+C を受けたら変更しない。

短縮 ID の解決は既存の規則（表示される先頭 8 文字、4 文字未満と多重一致は候補を挙げて拒否）を共用する。

### 13.3 配送

`session_bootstrap` の二段配送（SessionStart フック + フック無しクライアント向け 2 行 shim）は v2 6.1 節のまま変えない。task 系の規律（9 節の四行と、古い日付の state の裏取り）は server instructions が運ぶ。

## 14. スキーマ

現在の Memory 11 表に Project State 7 表を加えて 18 表。Project State の migration は 0004 から始まる。`ledger` には 0005 で 2 列を足し（`prevention_kind`、`filed_task`。9 節）、0006 で両者を結ぶ CHECK を張る（`filed_task` が非 NULL なら `prevention_kind='work'`）。台帳行そのものは append-only のままで、**提出先は行が書かれる前に決まり、後から書き込むことはできない**。矛盾した行を後から直す手段が無いことが、規約ではなく制約で持つ理由である。

```text
既存: scope route ledger trace memory memory_revision topic
      nomination memory_change temporary_context event_log

追加: project task task_state task_checkpoint
      attempt decision artifact_reference
```

PostgreSQL の schema 分離（`memory.*` / `project.*`）は行わない。既存表が public にある以上、途中から分離しても境界は語られない。**境界はコードの module boundary で持つ**（`projects.py` / `tasks.py` / `task_history.py` を新設し、既存 module に Project State のロジックを混ぜない）。拡張は引き続き pg_trgm のみで、pgvector は導入しない。

並行書き込みは v2 と同じく advisory lock で直列化する。lock 対象に Task 生成時の重複照合と生成の間、および checkpoint（置換 + 凍結）の原子性を加える。lease に遷移が無い（7 節）ため、遷移の競合対策は不要である。

## 15. 不変条件

DB / service 層のテストで保証する。

```text
Task lifecycle:
  Agent は Task を close できない
  Agent の close proposal は Task の status を変えない
  沈黙は Task を close しない（lease は dormant 導出までしかしない）
  dormant な state は現在の真実として配信されない
  Task card は日付なしで配信されない
  Current State 全文は card だけでは配信されず、着手前に task_get する

Memory（v2 のまま）:
  Agent 独自の Memory change は pending proposal に留まる
  明示指示の実行では actor と承認根拠を別に記録する
  MCP の承認出所は監査用であり、会話の真正性を認証しない
  active な Memory には必ず evidence がある
  退役には理由と retirement_kind が必須で、conflict を種類に応じて扱う
  proposal version、対象 revision、conflict が変われば古い判断で適用できない

Bootstrap:
  active な Memory と active な Task card だけが push される
  topic は見出しだけが push され、本文は bootstrap に載らない。見出しは Memory の席で数える
  dormant / closed / 履歴 / trace は黙って push されない
  総 token が hard cap を超えない
```

## 16. 実装計画 — v2.1 への増築

v2 の凍結・再現・移植の工程は存在しない。すでに動いているものは動かしたままにする。

**Phase A — Project State core**
migration 0004（7 表）、`projects.py` / `tasks.py`、Task 生成の重複照合、Current State の置換・hard limit・楽観チェック・予算判定、lease の導出判定、task_search。

**Phase B — History**
checkpoint（置換 + 凍結の原子性）、attempt、decision、artifact reference、supersedes、`task_get` の展開。

**Phase C — Bootstrap 統合**
`bootstrap.py` の拡張（active Task card の選択・日付つき整形・task_get 導線）、budget の一元管理（TOTAL / Task card 枠の追加）、`mashu bootstrap` / `mashu status` の表示拡張。

**Phase D — インターフェース**
MCP Tool 追加、server instructions への 9 節の規律の追記、CLI 追加、v2 文書と CLAUDE.md の修正（12 節）。

**Phase E — 運用開始**
過去の会話・文書からの自動 ingest はしない。v3 導入時点の作業から Task を起こす。Memory の腐った作業状態行（1 節で観測されたもの）は、対応する Task へ写したうえで User が retire する。この作業自体が Project State の最初の運用テストになる。

各 Phase は feature branch で行い、既存テストを常に green に保つ。

## 17. 観測指標と、この設計が誤りだったと知る方法

event_log から最低限次を測れるようにする。active / dormant Task 数、自動 dormant 率、reactivate 頻度、Task card と full detail の token 使用量、`task_card_write_refused` の件数と超過量、Task あたり attempt 数、checkpoint の打たれた率、stale-state incident（古い state を信じて誤った作業が出た pain_report）。

| 観測 | 意味 |
|---|---|
| Task card が長文化する | goal / status が索引でなく日誌になっている。card 上限を較正する |
| `task_card_write_refused` が多い | card 上限が実際の書き方に対して狭いか、goal / status に詳細を書いている。内訳でどの欄が超過を生んだかを見る |
| full detail が長文化する | schema が広すぎるか、原典を Task に複製している。field 数・上限を見直す |
| active Task が増え続ける | lease が長すぎるか activity の定義が広すぎる |
| dormant から頻繁に reactivate される | lease が短すぎる（研究のリズムに合っていない） |
| User がほぼ close しない | dormant が機能していれば失敗ではない。close を lifecycle の必須条件と考えない |
| Attempt が大量で役に立たない | 記録基準を見直す。自由文書化では解決しない |
| Agent が state を記録しない | instructions / hooks を改善する。記録漏れは誤情報の自動生成より安全 |
| stale-state incident が起きる | 日付つき提示と裏取り規律（3.1 節）が機能していない。配信形式を疑う |
| Memory nomination が再び大量になる | Project State の情報を Memory へ安易に昇格させていないか疑う |
| 再開の拾い直しが減らない | Project State そのものの誤りを疑う。1 節の動機が反証されたことになる |

成功条件。新セッションで bootstrap card から継続対象を短時間で選び、`task_get` 1 回で現在地を復元できる。同じ試行の無駄な再実行が減る。重要な Decision の理由を再導出しなくてよい。Agent が長大な作業日誌を生成できない。User が close を忘れても腐った card が push され続けない。そして Memory の定員から作業状態の圧力が消える。

## 18. 非目標と、復活させない v1 の機構

Mashu v3 は、chat history の全保存・AI 日記・Git / Obsidian / Issue tracker の置き換え・document management・vector knowledge base・自律的な project manager・完了の自動判定・universal semantic memory のいずれでもない。

Project State が入っても、speculative extraction・Session End Extraction・LLM sweeper・pgvector 既定・unified Knowledge Entity・未審査知識の配信・束 Review・stale の自動書き換え・Agent の自律的な Memory 採用や退役・AI による Task 完了判定は復活させない。明示指示に従う Memory 操作は v2 の記録済み provenance を伴う。

## 19. 中心命題

> **人間と Agent が毎回読むものは極小にする。大量に残すものは履歴として隔離する。恒久化するものには実証を要求する。そして現在の状態は、それが最後に確認された日付とともにしか語らせない。**

Memory は少なく、強く保つ。Agent 独自の案は pending とし、明示指示を受けた Agent の実行は出所を残すが、記録は認証ではない。Current State は Agent が頻繁に置換するが、常時配信するのは小さな日付つき card だけで、全文は既知の Task IDから必要時に読む。履歴は残すが配信しない。Artifacts は複製せず参照する。Task の完了は人が決め、進行中かどうかは活動の証拠が決める。

> **沈黙を完了と解釈しない。だが沈黙した Task を永遠に現在として配信もしない。**
