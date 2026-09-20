# Mashu v2 — 実証主義の Knowledge State / 設計書 v2.1

**Mashu は「覚えるべき情報」を推測するシステムではない。忘却による損害が観測された知識だけを常設化するシステムである。**
(Mashu is not a memory extraction system. It is an evidence-gated persistent instruction system.)

この一行が、外形の似た仕組みとの分かれ目である。経験・嗜好・手順を自動抽出して統合する系（Codex の native memory がその代表）は「将来役に立つか」を最適化する。Mashu は役に立つかを一切問わない。問うのは「忘れた結果として実害が観測されたか」だけであり、実害が観測されるまでは何も常設化しない。抽出も統合も、Agent の判断で知識になる経路も持たない。

本書は v1（`docs/mashu-mvp.md`、v0.16 まで）を全面的に置き換える。v1 の文書は撤回の記録として残すが、以後の正本は本書である。名称の由来は v1 の 0 節を参照。

## 1. v1 の何が破綻したか

v1 は運用実測で破綻した。原因は個々の機構ではなく、入口の基準にある。

入口を「長期価値がありそうな情報の抽出」に置いた結果、一日 100 件クラスの Proposal が流入した。価値の的中率が低い入口から大量に入れると、下流のすべて、すなわち束 Review、点検期日、重複検出、退役の洗い出し、配信のトークン予算が、その量を捌くためだけに肥大する。v0.4 から v0.16 までの改訂の大半は、量が生んだ問題への対症療法だった。仕様書が 2000 行に達したのはその症状である。

さらに、読む側も破綻していた。検索（pull）は Agent が「知らないことを知らない」限り起動されず、在庫が大きいために push は予算内に収まらなかった。書く側の過剰と読む側の不達は、どちらも同じ原因、在庫の大きさから出ている。

## 2. コンセプト

**覚えておくと便利そうな内容は全部捨てる。「忘れたら本当に困る」が実証されたものだけ残す。**

| | v1 | v2 |
|---|---|---|
| 入口 | 価値の見込み | 損害の実証 |
| 配信 | 検索（pull）中心 | 全量 push |
| 量の管理 | 下流の機構で捌く | 入口で絶つ |
| Review | 定常業務（束で捌く） | 例外処理（週に数件） |
| 未審査の知識 | タグ付きで配信される | 一切配信されない |

この原理には既に存在証明がある。ユーザーの常設指示ファイル（CLAUDE.md / AGENTS.md）は、繰り返された実害から生まれた規則だけを持ち、小さいから毎セッション確実に読まれ、実際に機能している。v2 は「常設指示の一行が実害から生まれる過程」を、複数 CLI が共有できる形で制度化するものである。

置き場の分担も明確になる。便利メモは Mashu の対象外であり、各プロジェクトのドキュメントか Obsidian が持つ。Memory が持つのは、忘れると損害が出ることが実証された規則と事実だけである。作業状態とプロジェクトの経緯は v2 では同じく対象外としていたが、v3 で Mashu 内の別身分（Project State）へ移した。置き場は `docs/mashu-v3.md` 2 節である。

## 3. 実証の定義

次の三つだけを実証と認める。

| 実証 | 定義 | 昇格候補になる条件 |
|---|---|---|
| **事故** | 知識の欠落・陳腐化が誤った作業を実際に生んだ | 1 回で候補になる |
| **再導出** | 同じ情報を調べ直した、または同じ訂正を受けた | 2 回目で候補になる。1 回目は台帳か痕跡（4.2 節）に残るだけ |
| **明示** | User が記録を明示的に指示した | 即時（経路は 5.1 節） |

「あとで役立ちそう」はどれにも該当しない。該当しないものは候補にすらならない。

この基準の値段は、初回の痛みを一度払うことである。緩和は二つある。台帳（4 節）が初回を安く記録するので二度目が即実証になること、そして事故は初回で候補になることである。

## 4. 台帳と痕跡（配信されない記録）

Memory の手前に、知識ではない記録を二つ持つ。どちらも Review を要さず、他のセッションへ主張として配信されることはない。

### 4.1 事故台帳（ledger）

append-only の記録。1 エントリが一度の痛みに対応する。

| フィールド | 内容 |
|---|---|
| kind | `incident`（誤った作業が出た）/ `friction`（調べ直し・訂正で時間を失った）/ `explicit`（User 明示の記録。5.1 節の即時経路が evidence として残す）/ `claimed`（Agent が運んだ User の記録指示。5.1 節の経路 3 が evidence として残す） |
| what | 何が起きたか |
| prevention | 何を知っていれば防げたか。**照合のキーになる本文** |
| scope_id | 起きた Scope |
| source | セッション参照 |
| created_at | 記録時刻 |

台帳は知識ではない。配信されず、他のセッションに主張として届くことはない。役割は二つ、初回の痛みを安く残すことと、二度目を検出する照合対象になることである。

**照合は pg_trgm で行う。** 埋め込みモデルは使わない。`pain_report`（8 節）は登録時に prevention を、既存の台帳エントリ・失効前の痕跡・退役済み Memory の tombstone と照合して返す。

照合先には優先順があり、先に当たったものが答えになる。

1. **退役済み Memory**（tombstone）に閾値以上で当たった場合も、退役理由・種別・後継または移動先を返す。invalidated / legacy との衝突は候補に添え、通常の incident / friction 条件による候補生成を止めない。理由を読まずに採用することは許さない（5.1 節）。out_of_scope / relocated は警告として示すが候補を止めない。superseded に対応する後継が同じ内容で active なら、active Memory に当たった場合として配信を調べる
2. **active な Memory** に閾値以上で当たった場合も昇格候補を作らず、「この規則は既に配信されている。入口ではなく配信を疑え」と返す。これは 12 節の三度目の痛みそのものであり、実証基準が三度満たされたのではなく、**配信が失敗した**証拠である。同じ文に二つ目の席を与えても何も直らないので、候補は作らない。台帳行は残り、`delivery_failure_suspected` として event_log に記録され、`mashu status` が直近 30 日の件数を出す
3. **pending の昇格候補** に閾値以上で当たった場合は、新しい候補を作らず**今回の台帳行を既存候補の evidence に追加する**（5.1 節）
4. 類似の `friction` または痕跡が既にある場合、二度目が成立し、双方を根拠とする昇格候補を自動生成する。**二度目の相手になれるのは friction と痕跡だけ**である。incident はその時点で自分の候補を作り終えており、explicit / claimed は人の言明であって痛みではない。これらまで相手に数えると、一度も調べ直していないものが再導出を名乗る
5. `incident` は単独で昇格候補を生成する

閾値は運用開始後に実測で較正する。当初は保守的に置き、機械判定を確定に使わず、類似候補を提示して人または報告した Agent に「同じ穴か」を確認させる。

### 4.2 痕跡（trace）

再導出の 1 回目は、痛みとして自覚されない。初めて何かを調べるのはただの作業であり、誰も `pain_report` を打たない。すると 2 回目に調べ直したとき、照合する相手が台帳に居ない。**投機的な記録が無いと、二度目は証明できない。** 痕跡はこの検出のために置く。

- セッション中の Agent が、調べて分かったこと・導出した結論を一行で `trace_put` する。抽出パイプラインは無く、走行中に書くだけである
- Review 無し、配信無し。bootstrap にも guard にも一切載らない
- 既定 30 日で自動失効する。痕跡は現在の真実を主張しない。「この日にこう分かった」という日付付きの観測であり、腐るのは「今も真」という主張のほうである。v1 で期限つき Memory が想定より速く腐った問題は、身分をこちらへ降ろすことで構造的に消える
- `pain_report` の照合対象に含まれる（4.1 節）。根拠に採られた痕跡は、失効で根拠が消えないよう台帳へ写して凍結する
- `trace_search` で明示的に検索できる。返る行には日付と、未検証の当時の観測である印を必ず付ける。知識（memory）はこの経路からは返らない
- 個人識別情報の入口拒否（5 節）は痕跡にも掛かる

v1 の投機的捕捉との関係。セッションから安く残すという入口は正しかった。誤りは、残したものを知識にしようとしたことである。そこから Review 負荷・配信予算・腐敗管理の三つが付いてきて系を沈めた。痕跡は同じ入口を保ちながら、残したものの身分を「未審査の知識」から「実証の検出器の部品」へ降ろす。Agent が書き忘れれば検出が弱くなるだけで、嘘は生まれない。

## 5. 記憶（memory）

実証つきで昇格したものだけが入る。

| フィールド | 内容 |
|---|---|
| content | push される全文。短い規則文の形で書く |
| scope_id | 所属 Scope。無指定は全域を意味する（delivery = `always` の規則と、全セッションで発火する guard がこれに当たる） |
| delivery | `always` / `scope` / `guard:<action>` |
| evidence | 根拠となる台帳エントリへの参照（1 件以上、必須）。参照先が実在する台帳エントリであることは DB のトリガが検証する。台帳は append-only なので、書き込み時に実在した参照は失われない |
| status | `active` / `retired` |
| retirement_kind / retire_reason | 退役時のみ。理由は必須。kind は `invalidated` / `superseded` / `out_of_scope` / `relocated` / `legacy` |
| superseded_by | `superseded` の後継 Memory ID |
| relocated_to_kind / relocated_to_id | `relocated` の移動先 |

- v1 の汎用 Entity / Version / Proposal の三層は持たない。本文の revision 表と、既存 Memory の retire / replace / restore だけを扱う `memory_change` を使う
- **evidence の無い active な記憶は存在できない**（User 明示の場合は明示の記録そのものが evidence になる）
- content の改訂は User のみが行い、revision に旧本文が残る
- 個人識別情報（本名、所属、学籍番号、ホームディレクトリを含む絶対パス）を含む書き込みは入口で拒否する。パターン一覧はリポジトリ外の既存ファイル（commit hook と共用）を読む。一覧が見つからないときは「合格」ではなく「検査できなかった」と報告する

### 5.1 昇格（admission）

候補（nomination）を作れるのは三経路である。

1. 台帳の二度目照合（自動生成）
2. `pain_report` の incident（自動生成）
3. Agent が会話中の User の記録指示を運ぶ場合（`memory_nominate`。指示は kind = `claimed` の台帳エントリとして残り、それが evidence になる。明示指示がある場合は `memory_admit` まで続けて同じセッションで完了できる。実行者は Agent のまま記録し、承認根拠に短い原文と取得可能な会話参照を別に記録する）

incident / friction から自動生成した候補と、Agent 自身の追加・変更案は pending のまま残る。**明示指示を受けて Agent が実行する新規採用や変更には、別画面での再承認を要求しない。** 出所には `user_instruction` と指示の短い原文、会話参照を記録し、actor は Agent として残す。これは認証ではなく監査用の出所記録であり、サーバーは会話中に本当にその指示があったか検証できない。Agent 自身の有用性判断、confidence、反対が無かったことは承認ではない。MCP instructions と運用上の信頼でこの境界を守る。

`memory_nominate` は新規採用候補を作るだけであり、明示指示の根拠を伴う `memory_admit` が必要である。MCP 側に会話の真正性を検証する仕組みはない。**Agent が独自に判断した採用・退役・置換・復帰は提案にとどめ、明示指示かサーバーに実装済みの条件が無ければ適用しない。**直接 CLI / TUI 操作は `user_direct` として Agent の実行とは区別する。

`memory_nominate` は判断対象の `version` を返す。`memory_admit` は読み取った version を必須で受け取り、本文・scope・kind・evidence・conflict が変わっていれば適用を拒否する。admission の request ID には初回応答全体を保存し、後から Memory が編集・退役されても再送には同じ応答を返す。

直接操作では User が delivery を選ぶ。会話中の明示指示から Agent が採用する場合は、nomination の Scope と既定 delivery を使い、必要な指定は提案に含める。

**重複の防止は照合が担い、lock は同時到着だけを担う。**候補の生成前に pending の候補と照合し、閾値以上なら新規候補を作らず今回の台帳行を既存候補の evidence に追加する。同じ規則が待ち行列に二行並ぶと、人は二度決めて一つしか席を渡せず、しかも重複は読むまで見えない。ただし二度目・三度目の痛みを捨ててよいわけではない。**何回起きたかは、席を渡すかどうかの判断のほとんどを占める。**保留中（`deferred_at`）の候補に evidence が付いた場合は保留を解いて一覧へ戻す。保留は「今は決めない」という当時の状況についての判断であり、その後さらに痛んだなら状況が変わっている。保留の理由（`defer_reason`）は残し、次の読み手が自分の以前の言葉を新しい痛みの隣で読めるようにする。

**退役済み Memory との衝突は、admission のすべての経路で確認する。**invalidated / legacy は conflict として理由を提示し、その判断を踏まえて採用する指示を記録する。経路ごとの扱いは次のとおり。

| 経路 | 扱い |
|---|---|
| `pain_report`（1・2） | 通常の条件で候補を生成し、衝突した tombstone の id を `conflicts` に記録する。active な後継と一致すれば配信問題として扱う |
| `memory_nominate`（3） | 候補に衝突した tombstone を記録する。invalidated / legacy はその conflict を踏まえた明示指示なしに採用できない。out_of_scope / relocated / superseded は警告として表示する |
| `memory_admit` / `memory_change_apply` | nomination または proposal の version、対象、後継内容・Scope・evidence、conflict の現在状態を照合し、読み取り後に変わっていたら拒否する。適用承認根拠を event に保存する |
| CLI / TUI の直接操作 | 直接操作として `user_direct` を保存する。invalidated / legacy の conflict は理由を見せ、対象 ID の明示的な確認を要求する |

衝突を候補に積んで運ぶことで、新しい反証は候補一覧から消えない。一方で invalidated / legacy は理由を確認して、それを踏まえて採用する明示指示がなければ適用されない。

### 5.2 席数（配信予算を上限でなく定員として使う）

bootstrap で push される合計に硬い天井（当初 2000 token）を置く。天井に達した状態での昇格は、既存の記憶の退役か guard/scope への格下げとセットでない限り拒否される。

これは v1 の admission control と似た形だが、意味が違う。v1 では溢れた分が検索へ落ちた。v2 には検索が無いので、**定員は「全量 push が成立するサイズに在庫を保つ」ことそのものを強制する**。定員があるから push が成立し、push が成立するから「読むべきタイミングで読まない」問題が消える。

押し込まれるものが定員の外に居られないのは、期限つきであっても同じである。期限で消えるからといって、押し込まれている間だけ重さを免れる理由にはならない。

**ただし v3 で、この 2000 token が数えるのは memory だけになった。**Temporary Context は v2 ではこの定員の内側にあったが、v3 8 節で自分の枠（400 token）へ移した。subsystem が互いの枠を暗黙に借りないためである（2 週間分の期限つき条件が、実証を経た規則の昇格を拒否できてはいけない）。それぞれが自分の入口で拒否され、押し込まれる総量（4000 token）は Mashu が一元管理する。

**always 層には、その内側にもう一段低い天井（当初 800 token）を置く。**always の 1 token は、どの scope も使えない 1 token である。共有の天井だけを持たせると always 層は決して溢れない。溢れる代わりに、scope が必要とするはずの余地を、大半のセッションには要らなかった規則で静かに使い切る。scope の側は何一つ拒否されないまま枠だけが無くなるので、どこにも失敗が現れない。

**行数で数える代理指標は使わない。**v1 からの再入場では「always 上限 10 行 × 約 60 token」として運用したが、実測は 1 行 43 token で、10 行は意図した枠の半分強しか使わなかった。規則の長さは一定でないので、件数は枠の代理にならない。

always 層で拒否されたものには、scope 規則には無い出口が一つある。一箇所でしか効かない規則なら、どこで効くかを言えばよい（`mashu deliver <id> scope --scope <name>`）。

### 5.3 退役

- 退役理由は必須で、理由の再入力は要求しない。状態は `active` / `retired` のまま保ち、退役理由の分類を `retirement_kind` に記録する。

| retirement_kind | 意味 | 類似内容の再登場時 |
|---|---|---|
| `invalidated` | 内容に誤りがある | 理由を conflict として示し、採用にはそれを踏まえた明示指示が要る |
| `superseded` | 後継 Memory に置き換えた | 後継 ID を示し、重複・矛盾を確認する |
| `out_of_scope` | 適用条件が終了した | 警告として示し、再候補化は妨げない |
| `relocated` | Temporary Context などへ移した | 移動先の種類と ID を示し、再候補化は妨げない |
| `legacy` | 旧データで分類されていない | 旧理由を conflict として示す。自由文から自動分類しない |

- `superseded` は後継 Memory ID を、`relocated` は移動先の種類と ID を保存する。自己参照、循環、存在しない参照は拒否する。
- bootstrap と類似照合結果は退役本文を返さない。明示的な `memory_get` と管理画面では、本文・revision・evidence と retired 状態・理由・後継または移動先をまとめて返す。
- 復帰は通常の本文・定員・重複・conflict 検査を通す。invalidated / legacy の復帰には、退役判断を覆す明示指示と理由を記録し、過去の退役 event は残す。
- `replace` は一つの transaction で後継採用と旧 Memory 退役を行う。定員は完了後の active 集合に対して判定し、失敗時は元の active 集合を保つ。
- Temporary Context への変換は `relocated` と移動先を記録する。逆変換はその移動済み行だけを扱い、無関係な invalidated / legacy の理由を一括上書きしない。
- CLI と TUI の新しい退役操作は `--kind` / 種別選択を必須にする。`legacy` は既存データと移行用で、新しい退役理由には使わない。
- Agent による提案の詳細には対象本文・種別・入力済み理由・根拠・後継・conflict を並べる。User の適用キー自体が `user_direct` の承認であり、同じ確認は重ねない。
- 点検期日・stale 掃き出しは持たない。在庫が定員内なら User は bootstrap の内容を日常的に目にしており、腐った行に気づく点検器は人自身である。この前提が破れた（在庫が目視で追えない規模になった）なら、それは定員の設定が誤っている

## 6. 配信

Agent に知識を届ける経路はすべて push である。Agent 用の検索が存在するのは痕跡層（4.2 節）だけで、そこから知識は返らない。人が CLI / TUI で Memory の在庫を調べる操作は配送ではない。push には三つの経路があり、受け持つ失敗が異なる。

| delivery | 誰に届くか | いつ届くか | 受け持つ失敗 | 例 |
|---|---|---|---|---|
| `always` | 全セッション | 開始時（bootstrap） | どの作業でも破ると損害が出る規律の不達 | 検証の証拠なしに完了と言わない |
| `scope` | route が当たるセッションだけ | 開始時（bootstrap） | 無関係な領域の規則が always 層を希釈する | 特定の計算機環境の接続規律、特定ソフトの落とし穴 |
| `guard:<action>` | その行為に至ったセッション | 行為の直前 | 文脈にはあったのに判断の前に無かった | 委譲先の選定規則を、委譲ツールの実行直前に |

軸は二つある。**scope は「どこで」を絞り、guard は「いつ」を絞る。**

- always と scope の違いは配達先の絞り込みである。目的は always 層を最小に保つこと。押し込む量が増えるほど一行あたりの遵守は薄まるので、領域固有の規則を全セッションに送ることは、その規則のためでなく always 層全体の効力のために有害である
- bootstrap と guard の違いは提示の時機である。開始時に読んだ規則が数十ターン後の判断の瞬間に効いていない、という失敗は v1 で実測されている。guard はその規則を bootstrap から外し、判断の直前に呼び出しを一度拒否する形で突きつける

guard は scope と直交する。`guard:<action>` の記憶が scope を持つ場合、発火はその scope のセッションに限られる。

### 6.1 session_bootstrap

セッション開始時に一度呼ぶ。**呼び出し規律の置き場所は MCP server の instructions である。**規律（開始時に一度 bootstrap、調べたら trace_put、痛んだら pain_report、User の記録指示は memory_nominate）はそこが運ぶ。

**ただし `session_bootstrap` だけは、そこに預けきらない。**v2.0 は「server の instructions は接続した CLI へ自動で届くので、指示ファイル側には何も書かない」としたが、これは撤回する。MCP の仕様が規定するのは instructions の *配送* であって、client がそれをモデルに *提示する* ことではない。Claude Code は使うと文書化されているが 2 KB で truncate し、Codex には instructions が agent guidance として安定に機能しないという未解決の問題がある。

`trace_put` と `pain_report` が呼ばれなかった場合の被害は、検出率が落ちることだけで、嘘は生まれない。`session_bootstrap` が呼ばれなかった場合の被害は違う。**active な知識そのものが届かず、しかもセッションは届いていないことに気づけない**（v2 には探しに行くための検索が無い）。これは v1 を沈めた失敗そのものが、push の見ていない扉から入ってくることを意味する。同じ扱いにしてよい失敗ではない。

そこで配送は二段にする。

1. **フックのあるクライアントでは、harness が配る**。SessionStart フック（`startup` / `resume` / `compact`）が `mashu bootstrap` の出力をそのまま文脈へ入れる。モデルが指示を読んで従うかどうかに依存しない。`compact` を含むのは、圧縮が同じ失敗の第二の扉だからである（session_id は変わらず、契約上 bootstrap は一度しか呼ばれず、push が書いた文脈のほうが落ちる）
2. **フックの無いクライアント（Codex CLI）向けに、指示ファイルへ 2 行の shim を置く**。「Mashu MCP が接続されていればセッション開始時に一度 `session_bootstrap` を呼ぶ。SessionStart フックが既に配信していれば不要」。指示ファイルを痩せさせる方針との衝突は、2 行という量で受け止める

フックが失敗したときは何も出力せず終了する。したがって「出たら呼ばない、出なければ呼ぶ」が自然に成立し、二重配信も無配信も起きない。指示ファイル側に書くのはこの 2 行だけで、trace / pain / nominate の規律は引き続き server の instructions が運ぶ。

返すもの。

- delivery = `always` の記憶（全文）
- 現在 Scope の記憶（全文）。Scope は route（cwd 対応表）か引数で決まる
- active な Task の Current State（v3 8 節で追加。各行に最終確認日時が本文として付く。dormant / closed は載らない）
- 有効な Temporary Context
- pending の候補件数（1 件でもあれば一行で知らせる）

これで全部である。v1 の三層・索引・health の大半は、対応する機構ごと消えた。

### 6.2 行為の門（guard）

v1 で実際に機能した部品なので、そのまま中核に据える。

- delivery = `guard:<action>` の記憶は bootstrap に載らず、PreToolUse フックが該当する行為の直前に呼び出しを一度拒否して突きつける
- 発火は 1 セッションにつき行為ごとに一度。発火は event_log に残る
- 蔵に届かないときは通す。接続できないことは、いま下そうとしている判断についての証拠ではない

bootstrap は「セッションの前提」を、guard は「判断の直前」を受け持つ。文脈にあることと、判断の前にあることは違う、という v1 の教訓の直接の継承である。

## 7. Temporary Context

期限つきの条件（今週のメンテナンス、明日リセットされる quota）は、書いた時点で失効時刻が分かっており、Review も退役も要らず、期限で勝手に消える。window の上限は 14 日。それより長い主張は期限つきの顔をした恒久の主張なので、通常の実証経路へ回す。

Temporary Context は既存の User 向け CLI / TUI から作成する。開始時点で expiry を指定した条件は、期限到来時に追加承認なしで配信対象から外れる。Agent が観測した期限つきの条件は trace に残す。期限管理は Temporary Context の既存実装を使い、Memory 専用の expiry や汎用条件 engine は作らない。

Memories TUI の `c` は、active な always / scope Memory と Temporary Context を相互変換する。Memory からの変換では期限を必須とし、元の Memory を `relocated` として移動先 ID とともに記録してから、本文と Scope を保った Temporary Context を作る。逆変換では Temporary Context をその時点で終了し、同じ本文と Scope の active Memory を作る。無関係な invalidated / legacy conflict は上書きせず、定員判定が拒否した場合は元の行を有効なまま残す。guard は action という配送条件を Temporary Context へ移せないため対象外とする。

## 8. インターフェース

### 8.1 Memory 関連 MCP Tool

| Tool | 役割 |
|---|---|
| `session_bootstrap` | 6.1 節の内容を返す。セッション開始時に一度 |
| `pain_report` | 痛みを台帳へ記録し、類似の台帳エントリ・痕跡・tombstone・active な記憶を返す。二度目・incident なら候補を生成 |
| `trace_put` | 調べて分かったことを一行残す（4.2 節） |
| `trace_search` | 痕跡の検索。日付と未検証の印を付けて返す |
| `memory_list` | 指定 Scope の active な記憶を列挙する |
| `memory_get` | 指定した Memory の本文、revision、evidence、退役情報を返す。退役本文は明示取得でのみ読む |
| `memory_nominate` | 新規 Memory の pending 候補を作る。会話中の明示指示なら `memory_admit` と続けて同じセッションで完了できる |
| `memory_admit` | 読み取った nomination version と承認根拠を必須にして採用する。実行者 Agent と `user_instruction` の出所を別に保存し、request replay には初回応答を返す |
| `memory_change_propose` | retire / replace / restore 案を作成・更新する。replace は後継 nomination version と配信条件を固定する |
| `memory_change_apply` | proposal version、対象 revision、conflict、承認根拠、request ID を照合して一度だけ適用する |
| `memory_change_withdraw` | 不要になった pending Memory change を理由つきで取り下げる |

他の Project State 系 Tool は `docs/mashu-v3.md` 13.1 節に記す。`session_bootstrap` の返却内容は 6.1 節にある。

v1 の 9 ツールに対し、`memory_search` / `memory_get` / `entity_resolve` / `memory_propose` は対応する機構ごと廃止する。`scratch_put` / `scratch_get` は `trace_put` / `trace_search` が継ぐが、身分が変わる（4.2 節）。`context_put` に後継は居ない。Temporary Context は User 専用になり（7 節）、Agent の期限つき観測は痕跡が受ける。

### 8.2 CLI

| コマンド | 内容 |
|---|---|
| `mashu` | 人向け dashboard。Attention / Memories / Work / Settings & health を開く |
| `mashu status` | 在庫と定員の使用量（always 層と、最も重い開き方の二つ）、pending 件数、台帳の直近、配信失敗の疑い件数 |
| `mashu review` / `mashu review --changes` | nomination または Memory change proposal を読み、編集・適用・却下・取り下げ |
| `mashu remember <body>` | User 明示。CLI で即時 active にする経路。invalidated / legacy conflict は理由を表示して個別 ID を確認する |
| `mashu retire <id> --kind K --reason <r>` | 退役。kind と理由が必須。`legacy` は既存データと移行用 |
| `mashu revise <id>` | 本文の改訂（User のみ） |
| `mashu show <id>` | 記憶・候補・台帳・Memory change proposal を 1 件、根拠と履歴つきで表示 |
| `mashu memories` | 記憶の一覧。既定は active、`--retired` で退役分と理由 |
| `mashu pain` | 痛みの手動記録（CLI から） |
| `mashu ledger` | 台帳の閲覧 |
| `mashu trace [query]` | 痕跡の閲覧と検索 |
| `mashu guard <action> --pin <id>` | 行為の門への留めつけ・解除・照会 |
| `mashu deliver <id> <delivery>` | delivery の変更 |
| `mashu scope` / `mashu route` | Scope 台帳と cwd 対応表（v1 と同じ、User のみ作成） |
| `mashu bootstrap` | push される内容と token を表示 |
| `mashu admin migrate` | migration 実行 |

Review UI は nomination と Memory change proposal の一覧・個別画面を持つ。Memory change の詳細では対象本文、対象 revision、退役種別、入力済み理由、evidence、conflict、後継または移動先を同時に読める。理由は編集して proposal version を更新できる。`y` で適用し、そのキー操作自体が直接承認になるため二度目の確認は挟まない。対象 revision や conflict が表示後に変わっていれば適用を止め、新しい情報を再読してから適用する。定員などで store が拒んだときは、決定にならず同じ proposal に留まり、拒否の文面がその場に出る。

まとめて承認するキーは無い。席は 1 件ずつ、その根拠を見て渡す。決めずに退ける保留（`s`、理由必須）だけは別で、status は pending のまま `deferred_at` と理由を持ち、既定の一覧から外れる。決定ではないので pending の件数も短縮 ID での直接操作も変わらず、`mashu review --all` で一覧に戻る。

dashboard の Memories では active / retired Memory と未失効の Temporary Context を閲覧・検索する。Memory の詳細には evidence、revision history、退役種別、後継または移動先を含める。User は direct remember、Memory と Temporary Context の編集、Temporary Context の登録、kind を選んだ retire、delivery / guard の変更を 1 件ずつ実行できる。提案 review は Attention と `mashu review --changes` から開ける。

id を取る引数は、表示される短縮 ID（先頭 8 文字）の前方一致で解決する。一覧が短縮 ID しか出さない以上、完全 UUID しか受け付けない引数は、人に画面外の値を打たせることになる。4 文字未満の前置きと、複数行に当たる前置きは、候補を挙げて拒否する。

`show` は Memory・candidate・台帳エントリを ID から非対話で読む経路として残す。dashboard の Memories でも Memory の evidence と revision history を読めるが、candidate と台帳エントリは対象にしない。台帳参照は記憶が席を占める理由そのものであり、どちらの経路でも退役前に確認できる状態を保つ。

## 9. スキーマ（10 表）

```
scope             台帳。User のみ作成
route             cwd から scope への対応表
ledger            痛みの記録。append-only
trace             痕跡。自動失効
memory            記憶。active / retired と退役種別・後継・移動先
memory_revision   本文の改訂履歴。append-only
nomination        昇格候補。pending / admitted / declined
memory_change     既存 Memory の retire / replace / restore proposal
temporary_context 期限つき条件
event_log         全操作の記録。append-only
```

pgvector は使わない。拡張は pg_trgm のみ。埋め込みモデルへの依存が消えるので、`--extra embed` 相当の依存も消える。

`memory_nominate` が返す nomination は `version` と evidence（根拠の台帳行）のほかに `conflicts`（衝突した退役済み Memory の id）を持つ。本文・scope・kind・evidence・conflict が変わるたびに version を進め、`memory_admit` は読み取った version を必須で照合する。conflict 配列に外部キーは張らないが、Memory 行は削除されず退役するだけなので、書いた時点の id は後から必ず解決できる。

`memory_change` は対象 Memory ID と revision、operation、変更内容、根拠、提案者、version、状態、承認根拠、idempotency request ID を保持する。replace 提案は読み取った後継 nomination version を受け取り、後継の本文、Scope、kind、evidence、conflicts の snapshot を保持する。提案前または適用前に後継が変わっていれば拒否し、再読と提案更新を要求する。後継の delivery・scope・guard action は提案に固定し、既定では旧 Memory から引き継ぐ。変更する場合は `successor_settings`（`delivery`・`scope_id`・`guard_action`）として提案 version に含め、適用 event にも記録する。旧 Memory の退役と後継採用は同じ transaction で確定する。`event_log` には退役種別・理由・移動先・actor と別の承認出所を判断時点の記録として保存する。承認出所は認証情報ではない。

書き込みは直列化する。複数の MCP プロセスと CLI が同じ知識状態に同時に書くので、定員の判定と候補の生成は advisory lock を取ってから行う。取らなければ、残り 1 席に二人が同時に座れてしまう。

**ただし lock だけでは候補の重複は防げない。**lock が防ぐのは同時到着だけであり、日をまたいで逐次に届く「同じ穴」は素通りする。重複防止の本体は 5.1 節の照合、すなわち候補生成前の pending 照合と evidence の追加である。lock はその照合と生成の間に別のプロセスが割り込まないことだけを保証する。

照合と書き込みの間で候補が確定されることは起こりうる（確定は別の lock を取る）。その場合 evidence の追加は失敗を投げず、候補が無かったものとして扱う。報告された痛みは事実であり、タイミングの事故で失われるのが最悪の答えだからである。最悪の場合に生まれるのは重複候補一つで、それは人が却下できる。

## 10. v1 から捨てるものの一覧

| 捨てるもの | 理由 |
|---|---|
| Session End Extraction / 常駐 worker / sweeper / launchd | 投機的捕捉そのものが対象外になった |
| pgvector / 埋め込み / 類似度閾値の較正 | 検索が無い。照合は pg_trgm で足りる |
| 三層 Retrieval / `memory_search` | 全量 push に置き換え。検索が残るのは痕跡層だけで、そこから知識は返らない |
| Layer 2（未審査の配信） | 未確定の知識は配信しない。信頼境界の単純化 |
| 束 Review / `--batch` / 束の三段 UI | 量が消えれば束は要らない |
| 点検期日 / stale 掃き出し / 類似対の検出 | 定員内の在庫は人が目視できる |
| Entity / Version / Proposal の三層スキーマ | memory + revision + nomination に縮退 |
| Scratch（セッション限りの作業状態） | 痕跡（4.2 節）が後継。知識候補の入力から、実証の検出器の部品へ身分が変わる |
| directive / delivery の admission control | content 自体を短く書く。定員（5.2 節）が代替 |
| Current State（type = state） | 作業状態は Mashu の対象外。プロジェクト側の文書が持つ（v3 で身分を分けたうえで Project State として戻した。理由は `docs/mashu-v3.md` 1 節） |
| type 体系（8 種） | 実証で入るものに分類は要らない。必要になったら足す |

撤回した設計の記録は v1 文書（`docs/mashu-mvp.md`）が保持する。削除しない。

## 11. 移行

1. v1 の DB は dump して読み取り専用で保管する
2. 採用済み 226 件は持ち越さない。過去の実害を具体的に言えるものだけ、根拠を添えて `mashu remember` で再入場させる。再入場の作業自体が、新基準の最初の運用テストになる
3. 実装は新規に書き下ろす。旧実装は git 履歴が保持する
4. 流用してよい部品は guard（`tools/pretooluse_guard.py`）、Scope/route の考え方、commit hook と禁止パターン機構、テスト基盤（実 PostgreSQL、テスト DB 作り直し）

## 12. 成功条件と、誤りだったと知る方法

成功条件。

- Review に使う時間が週数分以下に収まる
- 記憶が入ったあと、同じ痛みの再発が台帳上で止まる
- bootstrap の内容を User が全行把握できている（定員が機能している）

このコンセプトが誤りだったと知る方法。

- **三度目の痛み**が観測される。記憶が active なのに同じ穴で再発したなら、配信（push / guard）が機能していない。入口ではなく配信を疑う。**これは人の気づきに委ねない。**`pain_report` が active な Memory に閾値以上で当たった時点で候補生成を止め、`delivery_failure_suspected` を event_log に残し、`mashu status` が直近 30 日の件数を出す（4.1 節）。反証条件が観測量になっていなければ、それは反証条件ではなく願望である
  - 疑うべきものは二つある。ひとつは配信経路の選択（bootstrap で読んだ規則が数十ターン後の判断の瞬間に効いていないなら、その規則は `guard` へ移すべきだった）。もうひとつは文言（規則が、それが効くべき瞬間に自分のこととして認識されない書き方をしている）
- 初回の痛みのコストが頻発して耐えられない。そのときは実証の基準を緩める判定材料になる
- 二度目の照合が実際には拾えていない（台帳や痕跡に類似エントリがあるのに気づかれない）。pg_trgm の閾値か、prevention と痕跡の書き方を較正する
