# Mashu v3 — Persistent Agent State / 設計書 v3

**Mashu v3 は、覚えておくべきことと、いまやっていることを、同じ窓口から見えるようにする。ただし両者を同じものとしては扱わない。**

v2 の中核原理

> 忘却による損害が実証された知識だけを常設化する

は撤回しない。v3 が足すのは、それとは身分の異なる「現在の作業状態・試行・判断」を扱う **Project State** である。

本書は v3.0 草稿を全面改稿した v3 の正本である。**Memory subsystem の正本は引き続き `docs/mashu-v2.md` であり、本書はそれを置き換えない。**本書が規定するのは Project State の追加と、bootstrap・token budget の統合、および v2 文書への修正点（12 節）だけである。実装も同じ関係にある。v2.1 のコードベース（9 表、migration 0001–0003、MCP Tool 6 つ）は書き直さず、そこへ増築する。

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
| 主な書き手 | User | Agent |
| 人の確定 | 必須 | 不要 |
| 寿命 | 原則恒久 | 短い |
| 更新 | 稀 | 頻繁 |
| 腐敗対策 | 入口を絞る | lease による退場 |
| bootstrap | 全量 push | active な Current State のみ |
| 履歴 | revision | checkpoint / attempt / decision |
| 終了 | retire（User・理由必須） | close（User）/ dormant（lease） |

Project State から Memory への昇格経路は作らない。作業中に恒久規則が見つかったなら、それは通常どおり `pain_report` / `memory_nominate` の実証経路を通る。

### 3.1 信頼境界の緩和 — v3 が v2 の保証を破る唯一の点

v2 は「**Agent の書き込みが人の確定なしに他セッションへ届く経路は一つも存在しない**」を中核保証とした（v2 5.1 節）。Project State はこの保証を**意図的に緩和する**。Agent が書いた Current State は、人の review を経ずに次のセッションの bootstrap へ push される。

緩和する理由は、作業状態の性質にある。頻繁に変わるものに毎回人の確定を要求すれば書かれなくなり、書かれない state は存在しないのと同じである。週数件の Memory と違い、Review を挟む運用が成立しない。

緩和の被害は三方向から bound する。

1. **サイズ** — Current State には store が強制する hard limit がある（5.3 節）。長大な誤情報は物理的に書けない
2. **時間** — Activity Lease（7 節）により、活動の証拠が絶えた state は bootstrap から退場する
3. **提示** — 正しさそのものは bound できないので、配信の形式で受ける。**Current State は「現在の真実」の顔で配信しない。**bootstrap に載る Current State には必ず最終確認日時を添え、「YYYY-MM-DD に最後に確認された状態」として提示する。Memory（今後も守るべき規則）と同じ顔をさせない

v2 の trace が持つ「書き忘れても検出が弱くなるだけで、嘘は生まれない」という性質を、Current State は持たない。セッションが作業したのに state を更新しなければ、次のセッションには古い状態が届く。日付の明示はこの失敗を無くすのではなく、**受け手が古さを判定できる形にする**ための最低限である。Agent は日付が古い state を鵜呑みにせず、repository と artifacts で裏を取ってから作業に入る。この規律は server instructions が運ぶ。

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

Project と Scope は同じ概念ではない。Scope は delivery / routing の境界、Project は work state の所属である。初期実装では一つの Project が一つの主要 Scope を持つ形から始め、多対多への一般化は必要が実測されるまでしない。Scope を持つ Project の Current State はその Scope だけに配信する。Scope を持たない Project は全 Scope 共通であり、unrouted session にはこの共通分だけを配信する。

### 5.2 Task

一回の chat request ではなく、**継続して状態を保持する意味的な作業単位**である（例: 「v3 schema 実装」「PNtBAm 文献調査」「ポスター改稿」）。

保存される status は `open` / `closed` の二つだけである。active と dormant は保存されず、lease から導出する（7 節）。

**生成時に重複照合を行う。**nomination が pending 照合で二重の席を防ぐのと同じ理屈で、`task_create` は生成前に既存の open な Task（dormant 相当を含む）と名前・goal を pg_trgm で照合する。閾値以上の一致があれば新規作成せず候補を返し、Agent に既存 Task の継続か明示的な新規作成かを選ばせる。これが無いと、Agent が `task_search` で既存 Task を見つけ損ねるたびに Task が分裂し、state が二箇所に育つ。

### 5.3 Current State

Task の「今」だけを持つ。履歴を書かない。状態が変われば**置換**し、古い記述を残さない。

```text
goal / approach / status_text / open_questions / blockers / next_actions / updated_at / updated_by
```

**hard limit は store が強制する。**「簡潔に書け」という指示は情報量制御にならないので、DB / service 層で拒否する。

```text
goal            300 chars
approach        500 chars
status_text     500 chars
open_questions  最大 5 件 × 300 chars
blockers        最大 5 件 × 300 chars
next_actions    最大 5 件 × 300 chars
```

初期値であり、実測後に変更してよい。長文が必要なら原典を Artifact Reference に置く。

**追記は一箇所だけ許す。**`append_next_action` は next_actions に 1 件足すだけで他の欄に触れない。呼び出すのは痛みの記録（9 節の `work`）であって、state を読み終えたセッションではない。痛みは痛かった当人がその場で書くものなので、置換のために state 全体を持って来いと要求すれば、報告は他の 5 欄を捏造するか、行われないかのどちらかになる。上限・入口拒否・予算判定は置換とまったく同じものを通すので、追記で書ける state は置換でも書けた state に限られる。追記も `updated_at` を動かすため、追記前に読んだ置換は楽観チェックで拒否される。

**並行する置換は楽観チェックで受ける。**複数セッションが同じ Task を同時に更新しうる。`task_update` は読み取り時の `updated_at` を添えて置換し、不一致なら拒否して現在の state を返す。黙って last-writer-wins にすると、並行セッションの一方の作業が痕跡なく消える。

**予算境界の挙動も store が持つ。**Project State 枠は Scope ごとに独立する。`task_update` の結果、その Scope に配信される active な Current State の合計が枠（8 節）を超えるなら、書き込みを拒否し、現在の内訳と出口を返す。Scope を持たない Project の state は全 Scope 共通分として各 Scope の合計に含め、共通分への書き込みは最も重い Scope に対して判定する。別 Scope の固有 state は競合しない。溢れたぶんを黙って切り詰めたり検索へ落としたりしない。v2 の定員が Memory admission でしていることと同じ扱いである。

ただし**その Task 自身の state を小さくする書き込みは、枠を超えている状態からでも通す**。枠は設定値であり、state の重さは配信の形から計算されるので、どちらも誰も触っていない行の下で動きうる。つまり書き込みを経ずに枠を超えた状態が成立する。そこで一律に拒否すると、他の active Task だけで枠を超えている場合、対象を空にしても総量が枠を割らないため拒否され、「他を縮めろ」と言いながらその縮約自体を拒む閉じた輪になる。入口拒否の思想は保ったまま、枠が求めている向きの書き込みだけを通す。

### 5.4 Attempt / Decision

**Attempt** は試行錯誤の append-only 記録である。

```text
task_id / attempt (300) / result (500) / reason (500) / next (300) / created_at / created_by
```

**Decision** は「なぜそうしたか」を後から失うと再導出コストが高い判断だけを残す。append-only で、変更は `supersedes_id` を張った新 Decision で行い、過去の行を書き換えない。

```text
task_id / decision / reason / supersedes_id / created_at / created_by
```

どちらもコード・ログ・長文引用は文字上限が物理的に拒否する。どちらも bootstrap には載らず、`attempt_list` / `decision_list` で明示取得する。

Trace と Attempt は似ているが統合しない。Trace は「この日に調べて分かった」、Attempt は「この Task でこう試してこうなった」であり、重複が運用で実測された場合のみ再検討する。

### 5.5 Artifact Reference

Mashu 外の正本への参照。

```text
task_id / kind / locator / label / created_at
```

kind は `git_commit` / `git_branch` / `file` / `document` / `obsidian` / `issue` / `dataset` / `log` / `url` / `other`。本文は保存しない。

### 5.6 Checkpoint

まとまった作業の区切りで Agent が打つ。`task_checkpoint` は **Current State の置換と、履歴行の凍結を一度に行う**。すなわち checkpoint は task_update の上位互換であり、Agent が覚える動詞を増やさない。

```text
what_changed / 凍結時点の Current State / evidence (artifact 参照) / created_at / created_by
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

open な Task は `last_activity_at` と `active_until` を持つ。`task_update` / `task_checkpoint` / `attempt_record` / `decision_record` / `artifact_link` / User の `task touch` が lease を延長する。初期値は 14 日とし、計算のキュー待ちや査読待ちで正当に週単位の空白が生じる研究のリズムに対して短すぎるかどうかは、reactivate の頻度（17 節)で較正する。

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
3. active な Task の Current State（各行に最終確認日時を明示）
4. Temporary Context
5. pending nomination 件数
```

Attempt / Decision / Checkpoint / Trace / Ledger / dormant / closed は載らない。

Current State は**欄の名前を本文に含めて**配信する。MCP の `states[].content` と端末が同じ文字列を運ぶので、ラベルを描画側に置くと片方だけが読める形になる。特に open_questions と next_actions は、読み手が取り違えたときに「着手すべきでないものに着手する」形で外へ出る。ラベルは token を食うが、安く配って誤読される state は安くない。

初期 hard cap は次のとおりで、実測較正する。既存の `MASHU_CAPACITY`（2000）/ `MASHU_ALWAYS_CAPACITY`（800）と同じ環境変数方式で構成する。

```text
TOTAL            4000 tokens
  Memory         2000（always 800 / scope 1200）
  Project State  1600 / scope
  Temporary       400
```

重要なのは、**Agent に押し込まれる総量を Mashu が一元管理し、subsystem が互いの枠を暗黙に借りない**ことである。Memory が余った Project State 枠を恒久的に使うことも、その逆もしない。Memory の定員判定は v2 のまま admission 時に、Project State は Scope ごとの配信量を 5.3 節のとおり書き込み時に判定する。`mashu status` は Project State の全 Scope 合計ではなく、最も重い Scope の配信量を表示する。溢れの解決はどちらも入口側（retire / 格下げ、dormant 化 / 縮約）であり、「入りきらないので検索に落とす」は行わない。v2 に知識の検索が無いのと同じ理由である。

guard は v2 のまま Durable Memory の delivery 機構であり、Project State は guard に使わない。

## 9. 書き込み規律 — Agent が覚える動詞を最小にする

Agent の書き分けは次の四行に収まるように設計する。これを超える規律は server instructions に載せても守られない。

```text
調べて分かった            → trace_put
痛かった                  → pain_report
User が覚えてと言った     → memory_nominate
作業の区切り              → task_checkpoint
```

attempt_record と decision_record は「失敗した試行の結末」「後から理由を失うと高くつく判断」に限る補助動詞であり、毎ターン打つものではない。

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
| Memory の active 化・revise・retire | × | ○ |
| Temporary Context | × | ○ |
| Scope / Route / delivery / guard | × | ○ |

## 10. PII と入口拒否

v2 の入口拒否（`redact.py`、commit hook と共用の禁止パターン）を Project State の全書き込みに適用する。本名・所属・学籍番号・secret・token・ホームディレクトリを含む絶対パス。検査ファイルが読めないときは「合格」ではなく「検査できなかった」と報告したうえで書き込みを通す（`unchecked` の印が結果に付く）。v2 の既存挙動と同じ扱いである。

## 11. 自動処理は決定的なもののみ

LLM background worker は置かない。存在する自動処理は、Trace expiry・Temporary Context expiry・lease の導出判定・token count・hard limit・重複照合・orphan reference check だけであり、いずれも決定的である。「この Task は終わったように見える」という LLM 判定はしない。

## 12. v2 文書への修正点

v3 実装のマージ時に、`docs/mashu-v2.md` の次の箇所へ注記を入れる（前提を変えた実装と同じコミットで直す）。

1. **5.1 節の保証文** — 「Agent の書き込みが人の確定なしに他セッションへ届く経路は一つも存在しない」は Memory についての保証に限定し、Current State の緩和（本書 3.1 節）への参照を付す
2. **6.1 節の返却内容** — 「これで全部である」に active Task State の追加を反映する
3. **8.1 節の Tool 数** — 6 つに Project State 系が加わる
4. **2 節の置き場の分担** — 「作業状態は Obsidian・プロジェクト文書が持つ」を本書への参照に差し替える

同時に、ユーザーのグローバル CLAUDE.md / AGENTS.md の「情報の置き場所」の層分担も同じタイミングで更新する。

## 13. インターフェース追加

### 13.1 MCP Tool（既存 6 + 追加 9）

既存 6 つのうち `trace_put` / `trace_search` / `memory_list` / `memory_nominate` は変更しない。`session_bootstrap` は返却内容が広がり、`pain_report` は 9 節の二つの形を受けるため `prevention_kind`（`rule` 既定 / `work`）と `task_id` を取る。追加は

| Tool | 役割 |
|---|---|
| `task_create` | 重複照合つきの Task 作成（5.2 節） |
| `task_get` / `task_search` / `project_list` | 明示取得・検索 |
| `task_update` | Current State の置換（楽観チェック・hard limit・予算判定つき） |
| `task_checkpoint` | 置換 + 履歴凍結（5.6 節） |
| `attempt_record` / `decision_record` / `artifact_link` | 履歴の追記 |
| `task_propose_close` / `task_withdraw_close_proposal` | 終了の提案と取り下げ（6.1 節） |

`attempt_list` / `decision_list` / `artifact_list` は `task_get` の展開引数として提供し、Tool 数の増殖を避ける。**`task_close` は Agent 用 MCP に置かない。**提案は close ではないので `task_propose_close` はこの禁止に触れない。

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

dashboard は Attention / Memories / Work / Settings & health の四領域を持ち、各画面から戻るたびに件数を再集計する。Attention は review、close proposal、dormant Task を扱う。Work は active / dormant / closed Task と Project を閲覧・検索し、Current State、proposal、全履歴、artifact を表示する。User は Task の create / touch / close / reopen と Project の作成を実行できるが、Agent が担う Current State と履歴の書き込みは置かない。Settings & health は status、現在の cwd に対する bootstrap preview、Scope、route、migration を扱う。引数つき CLI と MCP の契約は変えない。

短縮 ID の解決は既存の規則（表示される先頭 8 文字、4 文字未満と多重一致は候補を挙げて拒否）を共用する。

### 13.3 配送

`session_bootstrap` の二段配送（SessionStart フック + フック無しクライアント向け 2 行 shim）は v2 6.1 節のまま変えない。task 系の規律（9 節の四行と、古い日付の state の裏取り）は server instructions が運ぶ。

## 14. スキーマ

既存 9 表に 7 表を足して 16 表。migration は 0004 から追加する。0001–0003 の内容には触れないが、`ledger` には 0005 で 2 列を足し（`prevention_kind`、`filed_task`。9 節）、0006 で両者を結ぶ CHECK を張る（`filed_task` が非 NULL なら `prevention_kind='work'`）。台帳行そのものは append-only のままで、**提出先は行が書かれる前に決まり、後から書き込むことはできない**。矛盾した行を後から直す手段が無いことが、規約ではなく制約で持つ理由である。

```text
既存: scope route ledger trace memory memory_revision
      nomination temporary_context event_log

追加: project task task_state task_checkpoint
      attempt decision artifact_reference
```

PostgreSQL の schema 分離（`memory.*` / `project.*`）は行わない。既存 9 表が public にある以上、途中から分離しても境界は語られない。**境界はコードの module boundary で持つ**（`projects.py` / `tasks.py` / `task_history.py` を新設し、既存 module に Project State のロジックを混ぜない）。拡張は引き続き pg_trgm のみで、pgvector は導入しない。

並行書き込みは v2 と同じく advisory lock で直列化する。lock 対象に Task 生成時の重複照合と生成の間、および checkpoint（置換 + 凍結）の原子性を加える。lease に遷移が無い（7 節）ため、遷移の競合対策は不要である。

## 15. 不変条件

DB / service 層のテストで保証する。

```text
Task lifecycle:
  Agent は Task を close できない
  Agent の close proposal は Task の status を変えない
  沈黙は Task を close しない（lease は dormant 導出までしかしない）
  dormant な state は現在の真実として配信されない
  Current State は日付なしで配信されない

Memory（v2 のまま）:
  Agent は active な Memory を作れない
  active な Memory には必ず evidence がある
  退役には理由が必須で、tombstone は再入場の前に提示される

Bootstrap:
  active な Memory と active な Task State だけが push される
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
`bootstrap.py` の拡張（active Task State の選択・日付つき整形）、budget の一元管理（TOTAL / Project State 枠の追加）、`mashu bootstrap` / `mashu status` の表示拡張。

**Phase D — インターフェース**
MCP Tool 追加、server instructions への 9 節の規律の追記、CLI 追加、v2 文書と CLAUDE.md の修正（12 節）。

**Phase E — 運用開始**
過去の会話・文書からの自動 ingest はしない。v3 導入時点の作業から Task を起こす。Memory の腐った作業状態行（1 節で観測されたもの）は、対応する Task へ写したうえで User が retire する。この作業自体が Project State の最初の運用テストになる。

各 Phase は feature branch で行い、既存テストを常に green に保つ。

## 17. 観測指標と、この設計が誤りだったと知る方法

event_log から最低限次を測れるようにする。active / dormant Task 数、自動 dormant 率、reactivate 頻度、Current State の token 使用量、Task あたり attempt 数、checkpoint の打たれた率、stale-state incident（古い state を信じて誤った作業が出た pain_report）。

| 観測 | 意味 |
|---|---|
| Current State が長文化する | schema が広すぎる。field 数・文字数を削る |
| active Task が増え続ける | lease が長すぎるか activity の定義が広すぎる |
| dormant から頻繁に reactivate される | lease が短すぎる（研究のリズムに合っていない） |
| User がほぼ close しない | dormant が機能していれば失敗ではない。close を lifecycle の必須条件と考えない |
| Attempt が大量で役に立たない | 記録基準を見直す。自由文書化では解決しない |
| Agent が state を記録しない | instructions / hooks を改善する。記録漏れは誤情報の自動生成より安全 |
| stale-state incident が起きる | 日付つき提示と裏取り規律（3.1 節）が機能していない。配信形式を疑う |
| Memory nomination が再び大量になる | Project State の情報を Memory へ安易に昇格させていないか疑う |
| 再開の拾い直しが減らない | Project State そのものの誤りを疑う。1 節の動機が反証されたことになる |

成功条件。新セッションで現在地が bootstrap だけから短時間で復元できる。同じ試行の無駄な再実行が減る。重要な Decision の理由を再導出しなくてよい。Agent が長大な作業日誌を生成できない。User が close を忘れても腐った state が push され続けない。そして Memory の定員から作業状態の圧力が消える。

## 18. 非目標と、復活させない v1 の機構

Mashu v3 は、chat history の全保存・AI 日記・Git / Obsidian / Issue tracker の置き換え・document management・vector knowledge base・自律的な project manager・完了の自動判定・universal semantic memory のいずれでもない。

Project State が入っても、speculative extraction・Session End Extraction・LLM sweeper・pgvector 既定・unified Knowledge Entity・未審査知識の配信・束 Review・stale の自動書き換え・AI による Memory admission・AI による Task 完了判定は復活させない。

## 19. 中心命題

> **人間と Agent が毎回読むものは極小にする。大量に残すものは履歴として隔離する。恒久化するものには実証を要求する。そして現在の状態は、それが最後に確認された日付とともにしか語らせない。**

Memory は少なく、強く、人が確定する。Current State は Agent が頻繁に置換するが、小さく、日付つきで、lease の内側でだけ「現在」を名乗る。履歴は残すが配信しない。Artifacts は複製せず参照する。Task の完了は人が決め、進行中かどうかは活動の証拠が決める。

> **沈黙を完了と解釈しない。だが沈黙した Task を永遠に現在として配信もしない。**
