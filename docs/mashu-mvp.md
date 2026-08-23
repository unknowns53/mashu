# Mashu — Shared Agent Memory Layer / MVP 実装仕様書 v0.12

改訂履歴:

- v0.1: MVP 初版
- v0.2: Actor モデル、Commit Gate、Write Policy、Entity Resolution、Review UI を追加
- v0.3: スキーマ確定、接続方式を MCP に確定、MVP スコープ縮小、開発計画を実測ベースに改訂、プロジェクト名を Mashu に確定
- v0.4: Harness シナリオの書き下しで判明した欠落を補う。Retrieval の三層出力(21節)、Entity Status と Merge 手順(20節)、Proposal の重複チェック(15節)、Commit Gate への Entity 作成の追加(17節)、entity.status 列と disproven の reason 必須制約(26節)、滞留時間指標(27.3)
- v0.5: Session Bootstrap を復活させる。v4 の Always Inject Context が MVP 化で落ちていたことによる退行の修復。session_bootstrap Tool と呼び出し要件(6節)、Bootstrap の内容と token 上限(21.2節)、native memory からの移植規則と棚卸し単位(27.1節)、切替試験(27.5節)、成功条件2件の追加(31節)
- v0.6: Write Policy に退役の契機を追加(16.1節)。27.4 を前提条件の異なる 27.4a / 27.4b に分割。開発計画を較正点の発動により引き直す(30節)
- v0.7: 27.4a の実測を受けた Review 負荷対策。Review の単位をセッション束へ(18.1節)、candidate を準承認として本文まで渡す(21.1節)、Task の範囲を限定(16.2節)、proposal に由来セッションを追加(15・26節)
- v0.8: 準承認の導入で開いた二つの穴を塞ぐ。Layer 2 の相対・絶対上限(21.1節)、Review 負荷指標を束単位へ改め未審査比率を追加、行き先を失っていた閾値を差し替え(27.3節)
- v0.9: 却下された Proposal の後始末を dormant から分離する。version.status に rejected を追加(11・12・26節)、Layer 3 に rejected を追加(21.1節)、重複チェックの対象を却下済みへ拡大(15.1節)
- v0.10: 実データを移植した直後に出た問題への対応。Preference の Auto Commit を User 明示のものに限定(17節)、Scope に lifecycle と readiness manifest を追加(7.1節)、Review 用の preview 経路を分離(27.1節)、Bootstrap の対象を type でなく delivery で決める(21.2節)、version に directive を追加(9・26節)
- v0.12: 誰も面倒を見ない一週間を成立させる。寿命の三分(13.1節)、Temporary Context の新設(25.2節)、Scratch の実体化(25.1節)、捕捉パイプラインの仕様化(16.3節)、Review の随意化(17節)、開発計画を依存の鎖で引き直し(30節)。撤回: Queue の義務性、未審査比率 50% の反証条件(27.3節)、手動 Conflict 記録(23節)、27.4b の全文ラベリング
- v0.11: 手動運用に入る前に、Review が構造的に必須になっている経路を断つ。Layer 2 の相対上限を撤回し、守っていたものを強制から観測へ移す(21.1・27.3節)。前提が消えた lifecycle と readiness manifest を撤回(7.1節)。Entity の type を訂正する操作を追加(8・15節)

---

## 0. 名称

本プロジェクトの名称は **Mashu(摩周)** とする。北海道・摩周湖に由来する。

由来:

- **透明度**: 摩周湖は世界最高クラスの透明度が観測された湖である。変更履歴と判断理由を底まで見通せる Knowledge State のメタファーとする
- **閉じた水盆**: 摩周湖には流入する川も流出する川もない。どの Agent にも所有されず、それ自体として独立に存在する外部 Knowledge Layer に対応する。各 Agent は湖に水を注ぐ雨のひとつにすぎない
- **霧**: 「霧の摩周湖」の霧は、古い情報・棄却済み仮説による視界の濁りに対応する。本システムの仕事は霧を晴らし、湖面を見せることである

表記:

- プロジェクト名: Mashu
- リポジトリ名 / パッケージ名 / CLI コマンド名: `mashu`

名称確認(2026-08 時点):

- PyPI: `mashu` / `mashuko` / `mashu-kb` いずれも配布物なし(空き)
- GitHub: `mashu` というユーザー名は既存だが、自アカウント配下のリポジトリ名としては問題なし。同名の小規模な趣味プロジェクト(Discord Bot 等)が散在するが、Knowledge Layer / AI Agent 領域での衝突はなし
- 検索性: 綴りが Fate/Grand Order のキャラクター名と一致するため Web 検索では埋もれやすい。実害が出た場合はパッケージ名のみ `mashu-kb` 等へ変更する余地を残す

## 1. 目的

複数の AI Agent(Claude、Codex、Gemini 等)が共有利用できる外部 Knowledge Layer を構築する。

本システムは AI Agent に直接的な長期記憶を与えるものではない。
目的は、複数 Agent が利用する Knowledge State を人間が管理可能な形で保持することである。

解決対象:

- 古い情報が現在情報として利用される
- 終了済み Task が未完了として扱われる
- 棄却された仮説が再利用される
- Agent ごとに異なる認識状態になる
- 過去判断の理由が追跡できない

## 2. 基本思想

Memory は文章保存ではなく、Version 管理された Knowledge Object とする。

Knowledge Object = Content + Status + Version + Provenance + Scope + History

重要なのは情報量ではなく、状態管理である。

## 3. 設計モデル

Git に近い変更管理モデルを採用する。

| Git | Memory Layer |
|---|---|
| Commit | Memory Version |
| Pull Request | Memory Proposal |
| Code Review | Human Review |
| HEAD | Active Version(Entity のポインタ) |
| Revert | Restore Version |
| Log | Event Log |

ただし Memory はコードではなく、知識状態を扱う。

## 4. Actor モデル

### User

最高権限。

- Proposal 承認 / 却下
- Active Version の切替
- Disproven 化
- Restore
- Entity Merge
- Scope 作成 / 統合

### Agent

提案者。

可能:

- Memory Proposal 作成
- Evidence 添付
- Context Retrieval

禁止:

- Active Memory の直接変更
- Version 削除
- Status 強制変更

### System

決定的処理のみを担当する。

可能:

- Version 作成
- Event Log 記録
- Status 遷移制御
- 楽観ロック判定
- Auto Commit(Commit Gate の規則に従う場合のみ)

禁止:

- 内容解釈
- 仮説判断

## 5. 全体アーキテクチャ

```
                User
                 |
        Human Review CLI/UI ----+
                 |              |
                 v              v
           Context Gateway   Memory Hub API
           (MCP Server)         |
                 |              |
        +----------------+      |
        |   AI Agents    |      |
        |   Claude       |      |
        |   (Codex)      |      |
        |   (Gemini)     |      |
        +----------------+      |
                 |              |
          Memory Proposal       |
                 |              |
                 v              |
            Commit Gate --------+
                 |
                 v
            PostgreSQL + pgvector
```

## 6. Agent 接続方式

Context Gateway は MCP(Model Context Protocol)Server として実装する。

理由:

- Claude、Codex、Gemini の各 CLI が MCP に対応しており、Agent ごとの個別アダプターが不要になる
- 成功条件「Agent の種類に依存しない」を接続方式のレベルで担保できる

提供する MCP Tool(初期セット):

- `memory_search`: Retrieval Pipeline の実行
- `memory_get`: Memory ID 指定取得
- `memory_propose`: Proposal 作成
- `scope_list`: Scope 一覧
- `entity_resolve`: Entity 候補検索(Proposal 前の同一性確認)
- `session_bootstrap`: Session 開始時に渡す固定コンテキストの取得(21.2節)

MVP では Claude 1体のみを接続する。
Codex / Gemini は MVP 後の接続とする。

注記: 各 CLI の MCP 対応状況は変化が速いため、Phase 3 着手時に最新ドキュメントで再確認する。

### 6.1 Session 開始時の呼び出し要件

MCP Tool は Agent が呼ばなければ動かない。一方、CLI が備える native な記憶機構は毎セッション自動で文脈へ入る。この非対称を放置すると、Agent が `memory_search` を呼ばなかったセッションは知識状態ゼロで始まる。

呼ぶかどうかを Agent の判断に委ねると、Agent は「知らないことを知らない」ため呼ぶ動機を持てない。したがって Bootstrap の取得は Retrieval とは別に、呼び出しを強制する経路を用意する。

二段構えとする。

**第一段(MVP)**: 各 CLI の指示ファイル(`CLAUDE.md` / `AGENTS.md` / `GEMINI.md`)に「セッション開始時に必ず `session_bootstrap` を呼ぶ」と記載する。

Agent の従順さに依存する擬似 push であるが、三 CLI に共通して効くのが利点である。MVP はこれで足りる。

**第二段(MVP 後)**: Claude Code のセッション開始フックから注入する。

フック経由であれば Agent の判断と無関係に文脈へ入るため、本物の push になる。ただしフック機構は製品側の変化が速い領域のため、Phase 3 着手時に最新ドキュメントで確認する。

## 7. Scope

Scope は文字列ではなく台帳管理する。

理由:

表記ゆれによる Scope 分裂(「SSD障害解析」と「SSD 障害解析」)を防止する。
Scope Detection と Review UI のグルーピングは Scope 台帳を前提とする。

Scope の作成は User のみが行う。
Agent は既存 Scope から選択する。

### 7.1 Scope の lifecycle(v0.11 で撤回)

v0.10 で seeding / operational の区別と readiness manifest を置いた。v0.11 で撤回する。

置いた理由は、**何も知らない Scope と、まだ開いていない Scope が外から区別できない**ことだった。どちらも Retrieval が空を返すためである。しかしその「空を返す」は 21.1節の相対上限が作っていた挙動であり、上限を外した以上、採用済みが無い Scope も候補をタグ付きで返す。区別すべき二つの状態が、そもそも同じ見え方をしなくなった。

したがって lifecycle は宣言すべき状態ではなくなる。Agent は返ってきた内容にタグが付いているかどうかで、その Scope に確定した知識があるかを直接読める。維持されない表示だけを残すと、実態とずれた古い札が残る。

readiness manifest と昇格操作も同時に落とす。昇格に効果が無くなれば、必要項目の宣言と昇格の実行は判断ではなく事務になる。**Review 負荷の実体は件数ではなく、判断でない操作をどれだけ人間に踏ませるかにある**——これは 18.1節が Review 単位について出したのと同じ結論である。

撤回の記録として残すこと: 7.1節が対処しようとした症状(移植直後の Scope が空を返す)は実在した。誤っていたのは原因の同定で、症状は Scope の側ではなく上限の側から出ていた。

Scope 台帳そのもの(7節)は変わらない。

## 8. Memory Entity

Memory Entity は概念単位。

例:

- Python 環境
- SSD 障害解析
- PNIPAM 研究条件

保持:

- memory_id
- scope_id(scope 台帳への外部キー)
- type
- title
- active_version(ポインタ)
- latest_version(ポインタ)
- title_embedding(Entity Resolution 用)
- created_at

**type の訂正(v0.11)**

type を変える操作を持つ。v0.10 まで操作が無く、結果として不変だった。

不変であることに理由があったわけではない。無かっただけである。実害が出たのは 27.1 の移植で、数十件の type を一括で当てた結果、後から見て別の type であるべきものが混じった。操作が無いと、直すには**同内容の Entity を新設して Merge する**しかない——Proposal・承認・Merge の三段で、しかも中身は一文字も変わらない。判断は「これは Decision か Fact か」の一つだけなのに、操作が三つ要る。

type の変更が軽い操作でないことは変わらない。type は 17節の Commit Gate の分類軸であり、type が変われば**その Entity への以後の変更がどの経路を通るかが変わる**。Agent が自分で変えられるなら、自分の書き込み経路を選べることになる。したがって:

- Agent が提案する retype は Human Review Required とする(17節)
- Version は動かさない。内容についての判断ではないので、履歴に新しい Version を作らない
- delivery(21.2節)は連動させない。type が state になっても push には入らない。push へ入れるのは admission control を通る別の操作である

**変えないもの**: Entity の同一性。retype は概念が同じままその分類を訂正する操作であり、別概念になったのなら新しい Entity を作って Merge する(20.2節)。

## 9. Memory Version

Memory Version は不変履歴。
一度作成された Version は変更しない。変更は新 Version の作成として行う。

保持:

- version_id
- memory_id
- content
- directive(常設の規則としての短い形。無い場合は NULL。21.2節)
- status
- supersedes
- reason
- source_type(user / agent / tool / file / web)
- source_reference
- created_by
- created_at
- content_embedding(Retrieval 用)

v0.2 からの変更:

Provenance は独立テーブルとせず、Version の列として統合する。
Version と Provenance は 1対1 であり、分離する利点がない。

## 10. Active の定義(真実の一元化)

v0.2 の構造では entity.active_version ポインタと version.status = Active が併存し、真実が二重化していた。

v0.3 では以下に統一する。

**「Active な Version」とは entity.active_version が指している Version のことである。**

- version.status から Active を廃止する
- version.status は candidate / superseded / disproven / dormant / rejected / completed のみを持つ
- active_version の切替は System が Status 遷移と同一トランザクションで行う

これにより「ポインタは v2 を指すが v3 が Active を名乗る」という不整合が構造的に発生しない。

## 11. Memory Status

Version が持つ状態:

- **candidate**: Proposal 済み。まだ採用されていない
- **superseded**: 新しい Version に置換された
- **disproven**: 誤りと判明した
- **dormant**: 現在利用しないが、将来再評価可能
- **rejected**: その Version を運んできた Proposal が却下された
- **completed**: 終了済み(Task 等)

Active は状態ではなくポインタで表現する(10節)。

rejected だけは、内容についての読みではない。

dormant は「いまは使わないが将来再評価しうる」という**知識の見込みについての評価**であり、rejected は「その Proposal を通さないと判断した」という**手続きについての判断**である。内容が将来有望かどうかとは別の軸なので、却下の後始末を dormant に寄せない。寄せると、16.1節の再評価候補の洗い出しが却下の残骸を拾って濁る。却下はこの系で最も件数の出る事象なので、埋もれるのは数少ない本物の dormant のほうである。

この区別は誰が書けるかにも現れる。completed / disproven / dormant は Agent が Proposal として提出できる読みだが、**rejected は Review の行為そのものなので Agent は提出できない**。書き込むのは却下処理だけである。

## 12. Status 遷移

許可される基本遷移:

```
candidate ──(承認 + active_version 切替)──> 採用
                                             |
              +──────────────+──────────────+
              v              v              v
         superseded     disproven        dormant
                                            |
                                     (再評価 → 新 Version)

candidate ──(却下)──> rejected
```

rejected は candidate からのみ到達する。既に結論の出た Version は誰の裁定も待っていないためである。

過去状態の復活は、既存 Version の状態変更ではなく新 Version の作成(Restore)として行う。
既存 Version の復活は禁止。

## 13. Memory Type

- **Observation**: 直接観測。例: SATA 直結でも timeout 発生
- **Fact**: 確認済み情報
- **Interpretation**: 観測から導いた解釈。例: SSD 内部状態遷移の可能性
- **Hypothesis**: 未検証仮説
- **Decision**: 判断
- **Task**: 作業。ただし Memory にするのはセッションを跨ぐものに限る(16.2節)
- **Preference**: ユーザー設定
- **State**: Scope の現在状態(14節)

### 13.1 寿命の三分(v0.12)

type と直交する軸として、知識の**寿命**を三つに分ける。

| 寿命 | 定義 | 置き場所 |
|---|---|---|
| **session-scoped** | セッションの終了とともに死ぬ。作業途中、デバッグ状態 | Scratch(25.1節) |
| **window-scoped** | 書いた時点で失効時刻が分かっている。明日リセットされる quota、金曜まで続くメンテナンス。セッションと Agent を跨ぐ | Temporary Context(25.2節) |
| **indefinite** | 誰かが誤りだと気づくまで真であり続ける。この系が作られた対象 | Memory Entity / Version |

window は新しい type ではない。**内容が書かれた時点で自分について宣言した属性**であり、type とも status とも直交する。

11節が rejected を dormant から分離した論理をそのまま当てる。dormant は内容の見込みへの評価、rejected は手続きへの裁定であり、**expiry はどちらでもない**。そして expiry による退役は、期限到来時に新しい判断をしていない。書いた時点の判断を時計が執行しているだけであり、17節が Human Review を要求する「後からの判断」に該当しない。

この分解が効くのは、**「腐った項目が返らなくなる」という要求の大半が、寿命の型で機械的に片づく**ためである。時限つきの事実は expiry で消え、Task の完了は Auto Commit で落ち、残る解釈の退役だけが判断を要する。誰も面倒を見ない期間の成立条件は「全退役の自動化」ではなく、この分解を実装することである。

## 14. Current State

v0.2 からの変更:

current_state 専用テーブルを廃止する。
Current State は **type = State の Memory Entity** として扱う(Scope ごとに原則1つ)。

これにより Current State は自動的に以下を相続する:

- Version 履歴
- Proposal 経由の変更(自由な直接編集の禁止)
- Commit Gate(State 変更は Candidate Commit)
- Event Log 記録
- Human Review

内容の構造:

- references: 根拠となる Memory ID の集合(memory_evidence として保持)
- summary: 人間可読の短い説明

summary は references に含まれない新情報を導入してはならない。
Open Question は references で表現できないため、summary 内の自由記述として許可する(ハイブリッド方式)。

## 15. Memory Proposal

すべての変更は Proposal として作成する。

保持:

- proposal_id
- actor
- operation(create / update_version / change_status / restore / merge / retype)
- target_memory
- based_on_version(楽観ロック用。24節)
- session_id(由来セッション。Review の束ね単位。18.1節)
- payload
- status(pending / approved / rejected / auto_committed)
- reviewer
- decided_at
- decision_reason
- created_at

v0.2 からの変更:

reviewer / decided_at / decision_reason を追加。
成功条件「過去判断の理由が追跡できる」は、却下理由の記録まで含めて成立する。

### 15.1 重複チェック

Proposal 作成時、同じ対象に対する Proposal が既に存在するかを確認する。対象は **pending と rejected の両方**とする。

target_memory が指定されている場合:

同一 target_memory の pending / rejected Proposal を検索する。

target_memory が NULL(operation = create)の場合:

同一 Scope 内の pending / rejected な create Proposal のうち、payload の title の title_embedding 類似度が Entity Resolution と同じ閾値(20節)以上のものを検索する。

該当がある場合、新規 Proposal を作成せず、既存 Proposal の proposal_id・payload・status・created_at・滞留日数を提案者へ返す。却下済みのものについては decision_reason(15節)も返す。

理由:

未承認の候補は Retrieval の Layer 1 に現れない(21.1節)。v0.7 で Layer 2 が本文を渡すようになったため、その Scope を実際に引いた Agent であれば自分の提案に気づけるが、Retrieval は Query 起点であり、引かなかった Agent は気づけない。重複チェックがなければ、Review が遅れるほど同一内容の Proposal が Queue に積み上がる。これは User の Review 負荷を、知識状態の改善を伴わずに増やす。

却下済みを対象に含めるのは、pending の場合より重い理由による。**提案者に伝わらない却下は、同じ却下をもう一度踏みにいく。** User は同じ判断に同じ時間を二度払うことになり、しかも二度目に払ったことは記録のどこにも現れない。却下理由を添えて返せば、提案者は同じ結論へ向かう前にその理由を読む。

重複チェックは作成の機械的な禁止ではない。目的は既存 Proposal の存在を提案者に見せることであり、提案者が確認したうえで「別物である」と明示した場合は新規作成を許す。その場合は両方が Review Queue に並び、User が Merge するか一方を却下する。

## 16. Write Policy

Proposal 作成タイミングは二本立てとする。

1. **User Explicit**: 「これは覚えておいて」等の明示要求。即 Proposal
2. **Session End Extraction**: Session 終了時、Agent が長期価値のある情報を抽出して Proposal 化。それ以外は Scratch に残す

Session End Extraction の抽出品質は実装前にオフラインで検証する(27節)。

### 16.1 退役の契機

上の二つは、いずれも新しい知識を**書き込む**契機である。既存の Active な Memory を completed / disproven / dormant へ落とす契機は、v0.5 まで仕様のどこにも定義されていなかった。

これは 1 節の解決対象のうち「終了済み Task が未完了として扱われる」「棄却された仮説が再利用される」に対応する機構が無い状態である。15 節の operation には change_status があるため、欠けていたのは操作ではなく契機のほうである。

21.1 節の Layer 3 とも直結する。disproven な Memory を警告として返す設計にしても、disproven 化を提案する契機が無ければ Layer 3 は永久に空のままになる。

したがって、二つのタイミングの守備範囲を次のように広げる。

**User Explicit(拡張)**

「これは覚えておいて」等の記録要求に加えて、「終わった」「その説は違った」「もうその前提は無い」といった発話を change_status Proposal の契機とする。

対象 Memory の同定が曖昧な場合は、候補を提示して User に選ばせる。Agent が対象を推定して確定させない。

**Session End Extraction(拡張)**

新しい知識の抽出に加えて、そのセッションで完了または棄却が観測された既存 Memory の洗い出しを行う。

このため入力は会話ログだけでは足りず、当該 Scope の Active な Memory 一覧を含める。

判定の非対称:

新規の抽出では、迷ったら Scratch にする。偽陽性が Review 負荷を直接増やすためである。

退役の洗い出しでは、**迷ったら提案する**。理由は二つある。disproven 化と Active Version の切替は 17 節で Human Review Required であり、契機が増えても User の承認なしに落ちることはない。一方、退役し損ねた Memory は誰にも気づかれないまま、以後のすべてのセッションを汚染しつづける。偽陰性のコストのほうが高い。

洗い出しの品質も、新規抽出と同じく 27.4 でオフラインに検証する。

### 16.2 Task の範囲

type = task として Memory にするのは、**セッションを跨いで持ち越され、完了したかどうかが後続の判断を変える作業**に限る。

セッション内で閉じる作業状態(いまどこを編集しているか、どの検査が走っているか)は Scratch に残す(25節)。

理由:

1 節の解決対象は「終了済み Task が未完了として扱われる」であり、これはセッションを跨いだときにだけ起こる。跨がない作業状態を Memory に入れても、Review 負荷が増えるだけで解決対象には寄与しない。

日々の作業リストの役割を Mashu が引き受けるものではない。

該当する例は、次のセッションが知らないまま進むと誤った前提で作業することになるもの。たとえば「検証を経ずに統合した」「この検査は待ち時間不足で対象に到達していない」など。

### 16.3 捕捉パイプライン(v0.12)

16節の二契機は変えない。変えるのは、**契機を撃発する装置を仕様の対象にする**ことである。v0.11 までこれは仕様のどこにも無く、結果として書き込みは人か Agent が意識的に動かしたときにしか起きなかった。

構成は四段の hybrid とする。

| 経路 | 役割 | Model と予算 |
|---|---|---|
| Agent の MCP 呼び出し | Scratch の随時更新のみ | 走行中の Agent 自身 |
| SessionEnd hook(各 CLI) | 永続 queue に session ID / transcript path / cwd を積む**だけ** | Model を走らせない |
| 常駐 worker | Scratch + transcript 差分から Proposal / Temporary Context を抽出 | 専用予算の小型 model |
| 定期 sweeper | hook が取り落とした transcript を拾い worker へ | 通常 Model なし |

hook で Model を走らせないのは、終了 hook の時間枠が短いためである。transcript の形式は安定インターフェースではないので、CLI ごとの adapter と fixture test を持つ。

**Model を動かすのは worker であり、対話用の枠とは別の予算で動かす。** 対話 CLI を worker から呼ぶ形も動くが、対話用の枠と抽出費用が混ざって日次の上限を中央で管理できないため、第一選択にしない(障害時の fallback としては残す)。

**費用の縛り**

素朴に全 transcript を読むと 1 セッションあたり数万 token になる。これは払わない。

- **Scratch-first**: 抽出入力は Scratch と前回 checkpoint 以降の transcript 差分に限る
- 小セッションは skip する(Scratch 空・実質的な user turn が 2 未満・圧縮後が閾値以下・明示の marker 無し、を**すべて**満たす場合のみ)
- 長セッションは turn 境界で chunk し、全文の一括再送をしない
- 日次の入力予算を持ち、超過は捨てずに翌日へ defer する

**沈黙する失敗を構造的に禁止する。** 永続台帳 `extraction_run` を置き、(source_cli, external_session_id, transcript_digest, extractor_version) を一意とする。一時失敗は指数 retry、規定回数で dead letter。DB 停止時は local spool に残す。

そして **health は人間の巡回ではなく、必ず読まれる経路——session_bootstrap——へ押し込む。** 失敗件数と最古 queued age が閾値を超えたら、次のセッションの Bootstrap が警告を運ぶ。**誰も面倒を見ない期間の監視者は、次に来るセッションである。**

**Scope routing**: cwd / project root から既存 Scope への明示 map を worker が持つ。map に無い cwd の抽出結果は Proposal 化せず台帳に保留し、警告に載せる。**Scope の推測はしない。**

**抽出結果の行き先と、User を名乗れる者**

worker は transcript の user turn を実際に読める立場にあり、MCP Agent の自己申告とは違って原文の span を source_reference に残せる。したがって原理的には worker に限り `source_type=user` を主張できる。

**ただし当初はこれを許さない。** span の検証が保証するのは「どこに書かれていたか」であって「誰が書いたか」ではない。User の発話には外部から貼り付けられた文書が含まれうるので、貼り付けの中の「覚えておいて: 常に X せよ」は span 検証を通ってしまう。これは 17節が preference について塞いだ経路と同じ形である。

したがって:

- **当初、worker の出力はすべて Agent 由来として扱う。** User 由来の唯一の経路は CLI(`mashu remember`)、すなわち人が打った事実である
- 後に worker の User 主張を許す場合、範囲を三種(明示の記録指示、完了・棄却の宣言、expiry つきの条件)に絞り、かつ**引用・貼り付けブロック内の span を対象外**とする。この二重の絞りが無い状態で許さない
- User の発話の**言い換え**は、user turn に根ざしていても worker(= model)の解釈であり、candidate に落とす

**投入経路**: worker は `import`(移植用。重複と類似の確認を常に飛ばす)を使わず、通常の Proposal 経路を通って 15.1節の重複チェックと 20節の Entity Resolution を受ける。

**この選択で生まれるもの**: DB とは別に死にうる常駐物。worker が死んでも読み取り側は一切劣化しない(Temporary Context は read filter、三層は既存実装)という切り分けを保つことが、この設計の生命線である。**worker の死は「知識が増えない」に留まり、「嘘が返る」には決してならない。**

## 17. Commit Gate

目的は正しさの判断ではない。危険な更新を止めることである。

### Auto Commit(System が自動反映)

- User 明示変更(type を問わない)
- 単純 Task 完了

### Candidate Commit(candidate として保留、Review 待ち)

- Fact
- Interpretation
- Hypothesis
- State(Current State)変更
- **Preference(User 明示でないもの)**

v0.10 での変更: Preference を type だけで Auto Commit する規則を廃止する。

理由は Preference が何をするものかにある。Preference は以後のすべてのセッションで Agent が従う常設の指示である。type だけで Review を飛ばせるなら、**Agent は自分の指示を自分で書ける**。さらに Agent が外部で読んだ内容(Web ページ、ファイル、ツール出力)も、Preference と名付けるだけで同じ場所へ届く。21.2節が Preference を毎セッション押し込む以上、経路は一度きりでなく恒久的である。

13節は Preference を「ユーザー設定」と定義している。定義どおりのものは source_type = user であり、それは既に「User 明示変更」の行が拾う。残るのは **Agent がユーザーの意図を推測したもの**で、それは Preference の顔をした Interpretation である。

### Human Review Required(User 承認必須)

- Active Version の切替
- Disproven 化
- Restore
- Entity 作成(類似度が閾値超過)
- Entity Merge
- Entity の type 訂正(8節)
- Conflict 解決

Entity 作成が Review 対象になるのは、類似度が閾値を超えているにもかかわらず Agent が新規作成を選んだ場合に限る(20節)。閾値未満の新規作成は Review を経ない。

Auto Commit の対象範囲は、Review 負荷の実測(27節)に基づいて調整する。

**Queue は義務ではない(v0.12)**

三区分は変えない。撤回するのは、その周りに暗黙のまま残っていた運用概念——**「candidate はいつか全件 Review されて確定する」**——のほうである。

v0.7 が「Review は使えるようにする門ではなく品質を確定する門」と定めたとき、Queue が義務であることは言語化されずに残った。v0.11 が相対上限を外して未審査の本文が常に渡るようになり、さらに捕捉が自動化されれば(16.3節)、未審査の在庫が定常的に多数派になるのは設計の帰結であって故障ではない。

したがって明示する。**Queue は義務ではなく選択肢であり、`unreviewed` タグは「順番待ち」ではなく「人間が確認していない」という恒久的にありうる出自表示である。**

Gate を緩めるのではない。**未審査の解釈が確立した事実として書かれることはない**という制約は変わらず、Layer 1 に載るのは User 由来か人間が採用したものだけである。変わるのは、載らなかったものが永久に載らないままでも系が機能する、という点だけである。

**この撤回で失うもの**: 「未審査の在庫は有限で、いつか空になる」という性質。Layer 2 は原理的に増えつづける在庫になり、絶対上限(1500 token)と類似度順位が文脈側の防波堤のすべてになる。取得されないまま古びる candidate の扱い(年齢や未取得期間による順位の減衰、退避)は必要になる見込みだが、**どの速度で溜まるかの実測なしに設計しない。**

**撤回が誤りだったと知る方法**: User による訂正が、古い未審査項目に集中して観測されること。そうであれば Review の義務性は品質を実際に支えていたのであり、そのとき Queue を義務へ戻すか、流入を絞る。

## 18. Human Review Interface

MVP では CLI として実装する。Web UI は MVP 後。

理由:

UI の使い勝手要求は運用してみるまで見えず、先に Web 化すると手戻りが大きい。

### 18.1 Review の単位はセッション束

Review の単位は Proposal 1 件ではなく、**1 セッションから上がった Proposal の束**とする。

理由:

Review 負荷の正体は件数ではなく**文脈スイッチ**である。27.4a の実測では 1 セッションあたり平均 5.8 件の Candidate が上がった。これを 1 件ずつ捌くと、件数と同じ回数だけ文脈を組み立て直すことになる。同一セッション由来の Proposal は文脈を共有しているため、束で読めば文脈の構築は 1 回で済む。

必要機能:

- **Session Queue**: 未 Review の Proposal を由来セッションごとに束ねた一覧。束ごとに件数と滞留日数を表示する
- **束の内部の並び**: 束の中は **observation → interpretation → decision** の依存順に並べる。根拠から結論へ読める順序でなければ、文脈を 1 回で組めるという利点が出ない
- **Diff View**: 変更前後の比較(active_version の content との差分)
- **Action**: 束の一括 Approve と、Proposal ごとの Reject(理由必須)/ Edit / Merge

操作は「束で通し、外すものだけ個別に外す」形にする。Reject には理由が必須で手間がかかる一方、通すべきものが多数派になるためである。

由来セッションは proposal.session_id が持つ(15節)。

## 19. Evidence Reference

完全な Dependency Graph は作らない。
根拠参照のみを中間テーブル memory_evidence として保持する。

- from_version: 根拠を利用する側の Version
- to_memory: 根拠となる Memory Entity

目的:

- 根拠追跡
- 逆引きによる影響範囲確認

「M010 が disproven になったとき、M010 を根拠とする Memory はどれか」を単純な逆引き検索で列挙できる。
Cascade の自動処理はせず、列挙結果を Review Queue に提示するのみとする。

## 20. Entity Resolution

新規 Entity 作成の完全自動化は禁止。

```
New Proposal
  ↓
title_embedding による類似検索
  ↓
既存 Entity 候補の提示(類似度スコア付き)
  ↓
Agent が選択(既存 Entity への Version 追加 or 新規作成)
  ↓
類似度が閾値以上なのに新規作成を選んだ場合 → Human Review へ回す
```

閾値は仕様では定めない。
紛らわしい Entity 名を意図的に登録した実測(27節)によって決定する。

### 20.1 Entity Status

Entity は Version の status とは別に、Entity 自身の status を持つ。

- **active**: 通常。Retrieval の Layer 1 の対象
- **provisional**: 類似度が閾値以上であるにもかかわらず新規作成されたもの。Human Review 待ち
- **merged**: 他 Entity へ統合済み。merged_into に統合先を持つ
- **archived**: 利用しないが履歴として残す

provisional の扱い:

Entity は実際に作成され、Agent は Version をぶら下げられる。ただし Retrieval の Layer 1 からは除外し、Layer 2 に「未確定の Entity」として現れる(21.1節)。

これにより、Review 待ちのあいだ Agent の作業が止まらず、かつ同一概念に対して2つの Entity が Active として並ぶ状態も発生しない。

Review の結果、既存 Entity と同一と判断された場合は Merge(20.2節)、独立と判断された場合は active へ移す。

### 20.2 Merge 手順

Merge は User のみが実行する(17節)。Entity B を Entity A へ統合する場合、以下を同一トランザクションで行う。

1. B の active_version と latest_version を NULL にする
2. B の全 Version の memory_id を A へ付け替える。version_id・content・status は変更しない
3. A の active_version を決める。B が Active を持っていた場合、A の既存 Active との二者択一を User が選ぶ。選ばれなかった側は superseded へ移す
4. A の latest_version を、付け替え後の全 Version のうち created_at が最新のものへ更新する
5. memory_evidence の to_memory が B を指す行を A へ付け替える。付け替えた結果 (from_version, A) が重複する場合は片方を削除する
6. B の status を merged とし、merged_into に A を設定する

手順1を先に行うのは、Entity のポインタが自 Entity の Version のみを指せるよう複合外部キーで拘束されているためである(26節)。ポインタを外す前に Version を付け替えると制約に触れる。

B の行自体は削除しない。過去の Context Assembly の event_log に B の Memory ID が記録されており、削除すると当時の判断ログから参照が解決できなくなる。

Merge は event_log に entity_merged として記録し、detail に付け替えた Version 数・Evidence 数と、手順3で選ばれなかった Version の version_id を残す。

## 21. Retrieval Pipeline

```
Query
  ↓
Scope Detection(scope 台帳と照合)
  ↓
Type Filter
  ↓
Active Version Filter(active_version の content のみ対象)
  ↓
Vector Search(content_embedding)
  ↓
Ranking
  ↓
Context Assembly(Memory ID を必ず付与)
```

Ranking 優先順位:

1. Scope 一致
2. Active であること
3. Type 適合
4. Semantic Similarity

Recency は補助にとどめる。Superseded / Disproven の除外は Active Version Filter で構造的に行われるため、Recency に古い情報の排除を期待しない。

Agent へ渡す Context には Memory ID を必ず付与する。判断ログと Memory の紐付け(Test Harness の前提)に必要である。

### 21.1 三層出力

Context Assembly は単一の本文列ではなく、三層に分けて返す。

理由:

Active Version Filter は古い情報と棄却済み情報を Context から除く。しかし除くことと再利用を防ぐことは別である。

棄却された仮説が見えないだけなら、Agent は同じ仮説を最初から導き直せる。未承認の候補が見えないだけなら、Agent は同じ提案を繰り返す。どちらも1節が解決対象に挙げた状態そのものであり、フィルタだけでは達成されない。

| 層 | 対象 | 渡すもの | Agent への指示 |
|---|---|---|---|
| Layer 1 Active | status = active な Entity の active_version | Memory ID / type / title / content | 現在の知識として利用してよい |
| Layer 2 Unreviewed | active_version が指していない candidate、および status = provisional な Entity | Memory ID / title / content / 提案者 / created_at / 滞留日数 / `unreviewed` タグ | 未審査の情報として利用してよい。ただしこれを根拠に確定的な主張をしない。同一内容を再提案しない |
| Layer 3 Retired | status = disproven / dormant / rejected の Version | Memory ID / title / status / reason | 再導出しない。reason に反する主張をする場合は新たな根拠を示す |

Layer 3 で content を渡さないのは、退役した知識だからである。本文を渡せば Agent がそれを現在値として扱う危険があり、Active Version Filter を設けた意味がなくなる。渡すのは「何が、なぜ否定されたか」のみとする。否定された主張そのものより、否定した根拠のほうが Agent の再導出を止めるからである。

**三層と並ぶもの、三層に足すもの(v0.12)**

Temporary Context(25.2節)は**第四層にしない**。三層は indefinite な知識の認識論的な立場の分類であり、当面の作業条件はその分類の外にある。三層と並ぶ独立ブロックとして返す。

Layer 1 の項目には、**保留中の変更が存在する場合の注記**を添える。その Entity に pending の update_version / change_status Proposal があれば「更新候補あり」「退役候補あり(理由)」を付ける。スキーマ変更なしに Retrieval 時の join で導ける。

古い Active が返ること自体は防げなくても、**新しい候補の存在を知らずに古い Active を読む**ことは防げる。これは 16.1節の「Agent 推論による退役」を無人で落とさないという判断(30節)と対になっている——落とさない代わりに、見えるようにする。

**この注記に固有の失敗様式**: 注記が増えすぎて Agent が読み飛ばす。反証条件に載せる(30節)。

rejected は内容についての読みではない(11節)が、この層に置く。層の役割は「再導出させないこと」であり、「Review が見て通さなかった、理由はこれ」はその指示そのものだからである。読みなのか手続きなのかは、同時に渡す status で Agent 側が判別できる。

#### 準承認としての Layer 2

Layer 2 は「人間の目は通っていないが、本文は渡す」層である。

v0.4 では Layer 2 も本文を渡さない設計にしていた。これを改める。

理由:

本文を渡さないと、Review が終わるまでその知識は使えない。Review が三日遅れれば三日ぶん知識が欠ける。27.3 が測る滞留時間は「知識が利用できなかった期間」を表すはずだが、本文を渡さない設計ではそれがそのまま実損になり、Review 負荷が溜まった時点で運用が止まる。

本文に `unreviewed` タグを付けて渡せば、Review が遅れても知識は使える状態になる。Agent はタグを見て未審査だと分かるため、認識論的な正直さは保たれる。

これにより Review の役割が変わる。**Review は「使えるようにする門」ではなく「品質を確定する門」になる。**

17 節の Commit Gate の三区分は変えない。変えるのは candidate の可視性だけである。未審査の内容が Active を名乗ることは引き続き無く、Layer 1 と Layer 2 はタグで区別される。

Layer 2 と Layer 3 の非対称:

**保留中の知識は使ってよいが、退役した知識は使ってはならない。** この非対称が、両者を別の層に分ける意味である。

superseded は三層のいずれにも含めない。置換済みの内容に対応する現在値は Layer 1 が持っており、追加の情報を持たない。

取得経路:

Layer 1 は 21節のパイプラインをそのまま通す。Layer 2 は本文を渡すため、Layer 1 と同じく content_embedding で引く。Layer 3 は、Scope Detection で確定した Scope の範囲内で title_embedding により引く。content を渡さない層に対して本文の類似度で順位をつける意味がないためである。

Layer 2 と Layer 3 に含めた Memory ID も、Layer 1 と同様に event_log の context_assembled へ記録する(22節)。

MVP の既定:

Layer 1 は常に返す。Layer 2 と Layer 3 は、Layer 1 で当たった Entity と同一 Scope のものに限る。Scope 全体を返す形は、Layer 3 が肥大したときに Context を圧迫するため MVP では採らない。

#### Layer 2 の上限

- **絶対上限**: Layer 2 の合計を 1500 token とする

順位は Layer 1 と同じ類似度順とし、上限を超えた分は落とす。

**相対上限の撤回(v0.11)**

v0.8 で置いた相対上限(単一 Retrieval 内で Layer 2 の件数は Layer 1 の件数を超えない)を撤回する。

理由は、それが準承認と両立していなかったことである。v0.7 は「**Review は使えるようにする門ではなく、品質を確定する門である**」と決めた。しかし相対上限は Layer 1 が空のとき Layer 2 も空にする。したがって採用済みが 1 件も無い Scope は、候補が何件あっても何も返さない。**「使えるようにするには先に承認しろ」という構造が、Commit Gate から上限の側へ移っていただけだった。**

これは実損として現れた。27.1 の移植で 63 件を入れた 2 つの Scope が空を返し、その状態を説明するために 7.1節を足すことになった。足したのは症状への対処である。

放棄しないもの: 相対上限が守っていたのは Token 予算ではなく `unreviewed` タグの識別力だった。**強制をやめ、観測に替える**(27.3節)。渡した文脈のうちタグ付きが何割かを測り、恒常的に張り付くならそのとき手を打つ。1:1 という比率はもともと導出値ではなく選択であり(v0.8 が自らそう書いている)、選択を不変条件として運用したことが誤りだった。

絶対上限は残す。こちらは Layer 2 が Layer 1 と Token を奪い合うことへの対処であり、準承認の是非とは無関係である。ただし現在の実装は先頭の候補が単独で 1500 token を超えても 1 件は通すため、厳密な上限ではない。1 件も返さないより 1 件返すほうがよいという判断だが、**これは意図した挙動として記録しておく**。

**この撤回で変わらないもの**

Commit Gate の三区分(17節)は触らない。あれは危険度の分類であって負荷の調整弁ではなく、27.3.1 が「負荷の問題を安全性の予算で払わない」と決めた対象そのものである。今回動かしたのは、承認していないものを**渡すかどうか**であって、承認していないものが **Active を名乗るかどうか**ではない。後者は変わらず不可である。

### 21.2 Session Bootstrap

21節のパイプラインは Query 起点であり、Agent が引かなければ何も返さない。Session Bootstrap は Query なしで、Session 開始時に必ず渡す固定コンテキストである。

本節は v4 に Always Inject Context として存在した機構の復活である。MVP 化の際に落ち、v0.1 から v0.4 まで欠けていた。

内容は三つ。

1. **startup_required な Memory の全件**: セッション開始時、まだ何も分かっていない段階で渡すもの
2. **scope_required な Memory**: セッションが対象 Scope を特定した時点で渡すもの
3. **Scope 索引**: scope 台帳の全件と、各 Scope の一行要約

三つ目が最も重要である。

セッション中の pull が機能しない根本の理由は、Agent が「知らないことを知らない」ために検索の動機を持てないことにある。三つ目は知識の中身ではなく**知識の地図**であり、Mashu が何を知っているかの索引が最初に入って初めて、以後の `memory_search` が動機を持つ。地図があれば pull は生きる、地図が無ければ pull は死ぬ。

#### delivery — 何であるかと、どこへ渡すかを分ける

v0.10 での変更: 押し込む対象を type で決める規則を廃止する。

v0.9 までは「type = preference の Active Version 全件」を押し込んでいた。これを実データで測ったところ、**固定費が在庫量の関数になっていた**。

```
移植後の preference / state       : 55 件
全件承認された場合の Bootstrap    : 45,840 token
上限                              : 2,000 token(22.9 倍)
一件あたり中央値                  : 663 token
2,000 token に収まる件数          : 55 件中 5 件
```

上限どおり削ると 55 件中 50 件が title だけになり、「Preference を毎セッション押し込む」機構が実質「Preference の目次を押し込む」になる。上限を上げても構造は変わらない。**Preference であることは「何であるか」であって、「毎セッション目の前に要る」ではない。**

そこで Entity に **delivery** を持たせる。

| delivery | 渡す時点 |
|---|---|
| `startup_required` | セッション開始時。Scope が分かる前 |
| `scope_required` | セッションが対象 Scope を特定した後 |
| `pull_only` | 押し込まない。検索と `memory_get` で届く |

既定は `pull_only`。ただし **type = state だけは `scope_required` を既定とする**。14節が Scope ごとに Current State を原則1つとしているため、件数が在庫量でなく Scope 数で抑えられるからである。それ以外は意図的に昇格させない限り押し込まれない。

delivery を設定できるのは **User だけ**である。Agent が設定できるなら、17節が Preference について塞いだ穴を別経路で開けることになる——type が何であれ、delivery を startup_required にすれば以後のすべてのセッションの目の前に置ける。

#### directive — 短い形と、理由の付いた形

version に **directive** を追加する(9節)。directive はその知識の常設の規則としての短い形で、content は理由と適用法を含む全文である。push は directive を、pull は content を渡す。

directive を実行時の要約にしないのは、実行時の要約が**誰も Review していない解釈**だからである。この系は Agent の解釈を無審査で保存しない。directive は Version の一部として Review を通り、履歴を持つ。

Token 上限:

Bootstrap は毎セッション必ず消費する固定費であり、上限は 2000 token とする。

**上限は削って守るのではなく、超える変更を拒んで守る。** startup_required への昇格時に pack の合計を計算し、超えるなら昇格を拒否する。削って守ると、落ちるのはまさにそのセッションに伝えるはずだった常設の規則であり、固定費が黙って配達をやめたことに誰も気づかない。拒否であれば、その時点でまだ差し戻す相手がいる。

削る処理は残すが、これは admission control が乗っていない経路(User 明示変更など)の受け皿である。削る場合は Scope 索引を全件残し、scope_required、次いで startup_required の順に本文を落とす。地図さえ残れば Agent は `memory_get` で取りに行けるが、地図を削ると取りに行く先が分からなくなるためである。

Scope 索引だけで上限を超える場合、削る先が無い。その場合は超過したまま返し、**超過したことを明示する**(`over_budget`)。上限値は 27.5 の切替試験で実測して見直す。

記録:

Bootstrap で渡した Memory ID の集合も、Context Assembly と同様に event_log へ記録する(22節)。

## 22. Context Artifact(簡略化)

v0.2 からの変更:

専用テーブルと管理機構は MVP から除外する。

MVP では、Context Assembly 実行時に event_log へ以下を記録するのみとする。

- 注入した Memory ID の集合
- 生成方式(extractive / summarized)
- Agent Session ID

Interpretive な圧縮(解釈入り要約)は MVP では使用しない。抜粋と機械的短縮のみとする。

## 23. Conflict(簡略化)

v0.2 からの変更:

自動検出は MVP から除外する。

v0.12 からの変更:

**手動 Conflict 記録を撤回する。**

「User が気づいた矛盾を手動で Conflict レコードとして記録し、解決は Human Review Required とする」という操作は、**記録の消費者を持たないまま残っていた。** 解決経路は CLI にも MCP にも実装されず、記録を読んで何かをするものが仕様のどこにも書かれていない。

一人で運用していて矛盾に気づいた人間が実際にするのは、片方を理由つきで退役させるか、新しい Version を書くことである。「矛盾がある」とだけ記録して裁定を保留する中間状態は、自動検出(MVP 外)が生まれるまで生産者がいない。

テーブル定義は残すが、操作の実装は自動検出の設計と同時に戻す。

**撤回が誤りだったと知る方法**: 運用中に「どちらが正しいか今は決められないが、矛盾の存在は残したい」場面が実際に起きること。起きたら、そのとき最小の記録経路を作る。

## 24. Concurrent Update

Proposal は作成時点の based_on_version を保持する。

Commit 時に current の latest_version != based_on_version であれば reject し、Agent に再取得と再提案を要求する(楽観ロック)。

MVP は Claude 1体接続のため顕在化しないが、構造として最初から入れておく。

## 25. Scratch

Agent Session 内のみ存在する。

- 作業途中
- 未検証仮説
- デバッグ

長期保存対象ではない。長期 Memory への移行は Write Policy(16節)経由のみ。

### 25.1 Scratch の実体化(v0.12)

25節は Scratch を定義したが、読み書きする者が仕様にも実装にも存在しなかった。`agent_session.scratch` 列は作られたまま、一度も使われていない。次のように定める。

- **可視性は同一の論理セッションに限る。** resume・compaction・MCP プロセス再起動を跨いでも、同じ CLI セッション ID なら読める。別セッション・別 Agent からは読めない
- 読めるのは当該セッションと System(抽出 worker)のみ。worker はセッション終了後に抽出の入力として読む
- **寿命は抽出の成功まで。** 成功後に削除する。抽出が失敗した場合は retry 用に隔離保持するが、Agent には返さない
- blob ではなく item の配列として構造化する(item_id / kind / content / source_turn / created_at)
- MCP tool `scratch_put` / `scratch_get` を追加する

別セッションに見せない理由: 見せた瞬間、それは名前が Scratch であっても provenance も status も持たない共有状態になり、1節が挙げた汚染を作り直す。

Scratch の役割はゴミ捨て場ではなく、**抽出の第一入力**である。Agent がセッション中に「後で残す価値がありそうなもの」を随時置き、終了時の抽出は Scratch とその周辺 turn を中心に読む。これが捕捉の費用構造を決める(16.3節)。

**この選択で失うもの**: 並行する別 Agent が作業途中の状態を覗く使い方は不可能になる。セッション間の作業調整は本系の守備範囲外とし、必要になった事実が観測されたときに設計する。

### 25.2 Temporary Context(v0.12)

window-scoped な項目の置き場所。**Memory ではない。**

Memory は知識を持ち、知識は判断によって退役する。Temporary Context が持つのは知識ではなく**当面の作業条件**であり、作業条件は判断を経ずに効かなくなる。

**memory_version に expires_at を足す案は採らない。** 10節が「active_version が指す Version こそ単一の真実」と定めた以上、期限切れの Version をポインタが指しつづける状態は「定義上 Active、Retrieval 上は無効」という二重性であり、10節が潰した不整合の復活である。修理は Active の定義に validity window を織り込むか、期限時にポインタを外す worker を必須にするかだが、前者は 10節の書き換え、後者は worker 停止時に stale なポインタが残る。**独立テーブルなら read の filter だけで壁時計を通過した瞬間に消え、止まりうる worker が介在しない。**

読み取り条件は `valid_from <= now() AND now() < expires_at AND revoked_at IS NULL` のみとする。

**Proposal と Commit Gate を通さない。** Gate は indefinite な知識状態を守る装置であり、壁時計で自壊する項目に Review の費用を払うのは、被害の上限が期限で切られている以上釣り合わない。書き込みは event_log に記録する。

**第四層にはしない。** 三層(21.1節)は indefinite な知識の認識論的な立場の分類である。Temporary Context は Retrieval と Bootstrap に、三層と並ぶ独立ブロックとして返す。Agent への規則は四つ。

- 現在の作業条件として従ってよい
- indefinite な知識として引用・再保存しない
- expiry を越えたら使わない
- 恒久 Memory へ昇格させない

**書き込み権限と、User を名乗れる者**

expiry は機械的に一意に解釈できる表現だけを採用する(ISO 日時、「24時間」、OS のタイムゾーンで一意に決まる表現)。曖昧なものは Scratch へ落とす。

| 書き手 | 経路 | Bootstrap への掲載 | 上限 |
|---|---|---|---|
| User | CLI `mashu remember --until` | する | なし |
| 抽出 worker(User 発話由来) | 16.3節。**当初は不可** | する | なし |
| Agent | MCP `context_put` | **しない**(Retrieval には出る) | window 14 日 |

Agent 由来を Bootstrap に載せない理由: Bootstrap は全セッションに押し込まれる最も特権的な経路であり、誤った window がそこに座ると、引きに行かなかったセッションまで巻き込む。Retrieval に出しておけば、その話題を実際に引いた Agent は知ることができ、被害は「引いた者だけ」に閉じる。**押し込む権限と、書ける権限を分ける。**

Bootstrap ブロックの上限は Scope あたり 10 件・500 token とする。超過時は**書き込みを拒否せず、掲載だけを expiry の近い順に絞り、絞ったことを警告する。** 誰も見ていない期間に書き込みを拒否すると、拒否された観測は消え、警告を読む人間はその週いない。受けてから掲載を絞れば、失われるのは即時性だけで観測は残る。

**この選択で失うもの**: 保存と検索の経路が一本増える。Temporary Context は embedding を持たず、`memory_search` の類似検索に乗らない(決定的 filter のみ)。そして「短期なら何でも入れる箱」へ膨張する誘惑が構造的に生じるため、kind を fact / preference に制限し、それ以外を拒む。汎用の短期 DB が要ると分かった時点で、それは設計変更として本節に書く。

**移行**: 現在 Active な在庫に、title 自身が特定の月時点の状態だと宣言している Memory が存在する。**時限を自称する Active は、window の置き場所が無かった時代の借金**であり、棚卸しで Temporary Context へ移すか、期限情報を落とした indefinite な形に書き直す。

## 26. Database スキーマ

PostgreSQL + pgvector。

v0.2 からの変更点:

- provenance テーブル廃止(memory_version に統合)
- current_state テーブル廃止(type = State の Entity に統合)
- context_artifact テーブル廃止(event_log 記録に置換)
- scope テーブル新設
- memory_evidence 中間テーブル新設
- 埋め込み列を明記

v0.3 からの変更点:

- memory_entity に status 列と merged_into 列を追加(20.1節)
- memory_version に disproven の reason 必須制約を追加
- 埋め込み次元を 1024 に確定(採用モデル: intfloat/multilingual-e5-large)

v0.6 からの変更点:

- proposal に session_id 列を追加(18.1節の束ね単位)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- 埋め込み次元は採用モデル intfloat/multilingual-e5-large に合わせて 1024 とする
-- モデルを変更する場合は埋め込み列を作り直すマイグレーションが必要になる

CREATE TABLE scope (
    scope_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active',  -- active / archived
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memory_entity (
    memory_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id        UUID NOT NULL REFERENCES scope(scope_id),
    type            TEXT NOT NULL,
        -- observation / fact / interpretation / hypothesis /
        -- decision / task / preference / state
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
        -- active / provisional / merged / archived(20.1節)
    merged_into     UUID REFERENCES memory_entity(memory_id),
        -- status = merged のときの統合先。それ以外では NULL
    active_version  UUID,   -- FK は後付け(相互参照のため)
    latest_version  UUID,   -- FK は後付け
    title_embedding vector(1024),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memory_version (
    version_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id         UUID NOT NULL REFERENCES memory_entity(memory_id),
    content           TEXT NOT NULL,
    status            TEXT NOT NULL,
        -- candidate / superseded / disproven / dormant / rejected / completed
        -- active は状態ではなく entity.active_version で表現(10節)
    supersedes        UUID REFERENCES memory_version(version_id),
    reason            TEXT,
    directive         TEXT,   -- 常設の規則としての短い形。push はこちら(21.2節)
    -- provenance(統合)
    source_type       TEXT NOT NULL,  -- user / agent / tool / file / web
    source_reference  TEXT,
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_embedding vector(1024),

    -- disproven は理由が無ければ Layer 3 で渡すものが無く(21.1節)、
    -- 31節の「判断理由を追跡できる」も満たさない
    CONSTRAINT disproven_needs_reason
        CHECK (status <> 'disproven' OR reason IS NOT NULL)
);

ALTER TABLE memory_entity
    ADD CONSTRAINT fk_active_version
        FOREIGN KEY (active_version) REFERENCES memory_version(version_id),
    ADD CONSTRAINT fk_latest_version
        FOREIGN KEY (latest_version) REFERENCES memory_version(version_id);

CREATE TABLE memory_evidence (
    from_version UUID NOT NULL REFERENCES memory_version(version_id),
    to_memory    UUID NOT NULL REFERENCES memory_entity(memory_id),
    PRIMARY KEY (from_version, to_memory)
);
CREATE INDEX idx_evidence_reverse ON memory_evidence(to_memory);

CREATE TABLE proposal (
    proposal_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor            TEXT NOT NULL,
    operation        TEXT NOT NULL,
        -- create / update_version / change_status / restore / merge / retype
    target_memory    UUID REFERENCES memory_entity(memory_id),
    based_on_version UUID REFERENCES memory_version(version_id),
    session_id       UUID REFERENCES agent_session(session_id),
        -- 由来セッション。Review はこの単位で束ねる(18.1節)
    payload          JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
        -- pending / approved / rejected / auto_committed
    reviewer         TEXT,
    decided_at       TIMESTAMPTZ,
    decision_reason  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- v0.12: window-scoped な項目。Memory ではないので Proposal も status も持たず、
-- 壁時計を過ぎた瞬間に read の filter だけで消える(25.2節)。
CREATE TABLE temporary_context (
    context_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id          UUID REFERENCES scope(scope_id),  -- NULL は全 Scope 共通
    kind              TEXT NOT NULL CHECK (kind IN ('fact', 'preference')),
    content           TEXT NOT NULL,
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    source_type       TEXT NOT NULL,   -- 境界で強制。自己申告不可
    source_reference  TEXT,            -- 由来セッションと user turn の span
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at        TIMESTAMPTZ,
    revocation_reason TEXT,
    CHECK (expires_at > valid_from)
);
CREATE INDEX idx_temporary_live ON temporary_context (scope_id, expires_at)
    WHERE revoked_at IS NULL;

-- v0.12: 抽出の実行台帳。沈黙する失敗を構造的に禁止するためにあり、
-- 成功だけでなく skip と失敗も行として残す(16.3節)。
CREATE TABLE extraction_run (
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_cli          TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    transcript_digest   TEXT NOT NULL,
    extractor_version   TEXT NOT NULL,
    state               TEXT NOT NULL
                        CHECK (state IN ('queued', 'running', 'succeeded',
                                         'skipped', 'retrying', 'failed')),
    attempts            INT NOT NULL DEFAULT 0,
    model               TEXT,
    input_tokens        INT,
    output_tokens       INT,
    last_error          TEXT,
    next_retry_at       TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_cli, external_session_id, transcript_digest, extractor_version)
);

CREATE TABLE event_log (
    event_id    BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,
        -- proposal_created / committed / rejected / status_changed /
        -- active_switched / context_assembled / conflict_recorded / ...
    actor       TEXT NOT NULL,
    proposal_id UUID,
    memory_id   UUID,
    version_id  UUID,
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- event_log は追記専用。UPDATE / DELETE 権限を付与しない

CREATE TABLE conflict (
    conflict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_a    UUID NOT NULL REFERENCES memory_entity(memory_id),
    memory_b    UUID NOT NULL REFERENCES memory_entity(memory_id),
    type        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_session (
    session_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent       TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    scratch     JSONB,
    -- v0.12: 外部セッションとの対応。これが無いと同じ transcript を二度
    -- 処理したかを判定できず、無人での再実行が冪等にならない(16.3節)。
    source_cli          TEXT,
    external_session_id TEXT,
    transcript_digest   TEXT,
    checkpoint          TEXT
);
```

補足:

- 埋め込みは Version 作成時に同期生成する(非同期化は量が増えてから)
- active_version / latest_version の更新と Status 遷移は同一トランザクションで行う
- reason は「その Version が現在の状態にある理由」を表す。Status 遷移のたびに更新され、履歴は event_log 側が保持する

## 27. Agent 接続前検証

Phase 3(Agent 接続)の前に、User 自身が Agent 役として CLI から運用する期間を 2〜3 週間設ける。

検証項目:

### 27.1 Retrieval 品質

実在プロジェクト 1 件(例: SSD 障害解析)から Memory を 30〜50 件手動登録し、想定質問に対して:

- 正しい Memory が上位に来るか
- superseded / disproven が混入しないか

Harness シナリオ 1・2 はこの段階で Agent なしで検証できる。

本節は Retrieval の検証であると同時に、CLI の native な記憶機構に溜まった内容を Mashu へ移す作業の先行分でもある。登録元は新規に書き下ろすのではなく、実在の記憶機構の中身を用いる。

移植の規則:

- インポートした項目は例外なく **candidate として入れ、Review を通す**。native な記憶機構の内容は要約の塊で、Observation と Interpretation が溶け合っている。無審査で Active にすると、Mashu が防ごうとしている汚染をそのまま初期在庫として輸入することになる
- **source_reference に由来を必ず記録する**(元ファイル名、あるいはセッション id)

棚卸しの単位:

移行は履歴の再生ではなく、状態の再構築である。棚卸しの単位はログやファイルの本数ではなく **Scope** とする。

各 Scope の Current State、Active な Preference、主要な Decision が立ち上がった時点で、その Scope は移行済みとみなす。残りのログは必要が生じたときの遅延移植でよい。

v0.10 ではこの判定を readiness manifest として機械化し、宣言と昇格を User の操作にしていた。v0.11 で撤回した(7.1節)。**「移行済みか」は次に何をするかを決めるための読みであって、系が保持すべき状態ではなかった。** 昇格に効果が無い以上、機械化して残るのは事務だけである。判断が要るなら `mashu scope` の件数を見て人が読めばよい。

全件処理を切替の前提条件にすると、移行コストが在庫量に比例して膨らみ、移行そのものが失敗する。

Retrieval 検証の経路:

本節の Retrieval 検証(正しい Memory が上位に来るか)は、Agent が使う Retrieval とは別の経路で行う。

理由は、Agent 向けの Retrieval が順位を確かめる用途に向かないためである。v0.11 で相対上限を外したので、移植直後の Scope も候補を返すようになった。しかし絶対上限(1500 token)と件数の上限は残り、本節が確かめたいのは**上限が隠す側にある順位**である。移植した数十件のうち何位に来るかは、上限をかけた結果からは読めない。

したがって上限を適用せず候補を順位付けだけする **preview** を分けて持つ。preview の結果は Agent の Context に混ぜず、Context Assembly として event_log にも記録しない。

preview を Agent 側のフラグにしないのは、フラグにすれば「上限を外して返せ」という要求を Agent から出せることになるためである。

### 27.2 Entity Resolution 閾値

紛らわしい Entity 名(SSD障害解析 / SSDデバッグ / 外付けSSD問題)を意図的に登録し、採用した埋め込みモデルでの類似度スコア分布を実測して閾値を決定する。

**実測(2026-08-24、multilingual-e5-large、`tools/measure_thresholds.py`)**

合成した名前ではなく、移植済みの実在 Entity を負例に使った。同一 Scope 内の全ペア 1046 組はいずれも別概念であり、実在タイトルの言い換え 10 組を正例とした。

| | 最小 | 中央値 | p99 | 最大 |
|---|---|---|---|---|
| 負例(別概念、1046 組) | 0.759 | 0.834 | 0.890 | **0.921** |
| 正例(同一概念の言い換え、10 組) | **0.891** | 0.913 | — | 0.953 |

**二つの分布は重なる。** したがって完全に分離する閾値は存在せず、閾値の選択は「どちらの誤りを選ぶか」の判断になる。

| 閾値 | 正例を捕まえる | 負例を誤って引く |
|---|---|---|
| 0.89 | 10 / 10 | 11 / 1046 |
| **0.90** | 8 / 10 | 5 / 1046 |
| 0.91 | 5 / 10 | 1 / 1046 |
| 0.94(v0.10 までの placeholder) | **1 / 10** | 0 / 1046 |

placeholder の 0.94 は、実際には言い換えを 10 件中 1 件しか捕まえない。**設定されていたのではなく、切れていた。**

**0.90 を採る。** 見逃しは知識状態を分裂させ、同じ内容が別のタイトルで二つ残る。しかも自動捕捉が始まれば毎晩積み上がる。誤検出は判断が 1 回増えるだけで、しかも 0.90 付近で引っかかる負例は実際に隣接した概念(同じ検査への二つの規律、同じ設計の二つの案)であり、人が並べて見る価値がある。

ただしこの値は、**自動捕捉が「似ている」と言われたときに何をするかが決まった時点で見直す。** 保留するのか、`allow_similar` で作るのかによって誤検出の価格が変わるためである。

**Scope Detection の実測(同上)**

同じ測定で、21節の Scope Detection が**機能していない**ことが判明した。

実在の 3 Scope に対する 7 クエリの類似度はすべて 0.72〜0.81 に収まり、閾値 0.85 は一度も超えない。それだけでなく順位が 7 問中 4 問で誤っており、委譲の作法を問うクエリが、委譲の規律を持つ Scope より無関係な 2 つを上位に置いた。さらに完全に無関係なクエリ(天気)が 0.759 を取り、これは実際の一致が占める範囲の内側である。

**Scope の名前と一行要約にクエリを当てる方式そのものが、この規模では成立していない。** 閾値を下げても誤った Scope に絞り込むだけなので、**意図的に観測最大値より高いまま据え置く。** `detect_scopes` は空を返し、呼び出し側はそれを「全体を検索する」と読む(21節)ので、安全な側へ倒れる。

修理は定数ではなく設計変更である。Scope はラベルではなく**その Scope が実際に保持している Memory** から判定するべきで、それは Layer 1 の検索そのものと同じ動作になる。

### 27.3 Review 負荷

測る単位:

Review の単位をセッション束に変えたため(18.1節)、負荷の単位も 1 件あたりではなく**束あたり**とする。文脈スイッチが負荷の正体である以上、件数で測ると実態を外す。

運用中、以下の五つを記録する。

1. **束あたりの処理時間**(中央値)
2. **1日あたりの束数**
3. **滞留時間**: Proposal ごとの created_at から decided_at まで(中央値と 90 パーセンタイル)
4. **未審査比率**: Scope ごとの、未審査 Version 数 ÷ (Active Version 数 + 未審査 Version 数)
5. **タグ比率**: Context Assembly ごとの、Layer 2 の件数 ÷ (Layer 1 + Layer 2 の件数)

滞留時間を別に測る理由:

処理時間は User の負担を表すが、滞留時間は**その知識が未審査のまま使われた期間**を表す。両者は別の指標であり、片方だけでは対策を決められない。

v0.7 で Layer 2 が本文を渡すようになったため(21.1節)、滞留は知識の断絶ではなく品質確定の遅れになった。それでも測る価値は残る。未審査のまま参照される期間が長いほど、誤りが訂正されないまま使われる機会が増えるためである。

平均ではなく中央値と 90 パーセンタイルを取る。平均は少数の長期滞留に引きずられ、Queue の底に沈んだ Proposal を見えなくする。

未審査比率をどこで測るかの注意:

未審査比率は **Retrieval が返した結果ではなく、上限をかける前の母集団**で測る。母集団の側が、Review が追いついているかという問いに答えるものだからである。

v0.8 ではこれに加えて「相対上限により、返した結果の未審査比率は構造的に 50% を超えない。返した側で測れば、測っているのは上限であって系ではなくなる」と書いていた。v0.11 で相対上限を外したため、この理由は消えた。**返した側は返した側で測る意味を持つようになった**——それが下のタグ比率である。二つは別の問いに答えるので、両方測る。

閾値と、超えたときの行動:

| 指標 | 閾値 | 超えたときの行動 |
|---|---|---|
| 1日あたりの Review 時間(束あたり処理時間 × 1日あたりの束数) | 10分 | 単発の超過では動かさない。観測を続ける |
| 同上が 2 週間継続 | 10分 | 18.1 の束の並びと操作を見直す。それでも下がらない場合に限り、副次策として抽出基準の見直しを検討し、何を取りこぼす判断をしたかを記録する |
| 未審査比率 | 50% | **Agent 接続の範囲を広げない。** Scope 追加も Agent 追加も止め、比率が下がるまで待つ |
| タグ比率の中央値 | 80% | 単発の超過では動かさない。観測を続ける |
| 同上が 2 週間継続 | 80% | `unreviewed` タグが識別力を失っている。件数による上限を戻すか、準承認そのものを見直す |

1日10分を据え置く理由:

この値は設計パラメータではなく、User の許容量という要求である。設計が満たせないなら設計を直すのであって、要求のほうを動かすのは筋が違う。負荷の問題を安全性の予算で払わないと決めた 27.3.1 と同じ理由による。

ただし 27.4a の実測から、着手前の時点で超過が見込まれている(27.3.1)。したがってこの閾値は「達成済みか」を判定するものではなく、**束 Review と準承認によってどこまで下がったかを測る基準**として使う。

v0.6 までの「1日10分を超える場合、Auto Commit の対象範囲を拡大する」は削除する。27.3.1 の通りその行動を採らないと決めたため、この閾値は行き先を失っていた。閾値だけ残して行動を書かないと、超過したときに場当たりで対策を選ぶことになる。

タグ比率の測り方と、閾値の根拠(v0.11):

タグ比率は event_log の `context_assembled` から直接引ける。detail に `layer1` と `layer2` の Memory ID 列が残っているためで、そのための新しい計測機構は要らない。

閾値を 80% に置くのは、タグが働くのに必要なのは 1:1 の均衡ではなく**対照が存在すること**だからである。5 件に 1 件が確定済みなら、Agent は「確定しているものとしていないものが両方来る」という前提で読める。100% に張り付いたときにだけ、タグは全件に付くことで情報を失う。

この指標は 21.1節で相対上限を外したことの安全弁である。**上限は不変条件として保証していたが、こちらは事後に観測して気づくだけである**——その違いを引き受けたうえで外した。気づくのが遅れる代わりに、Review が構造的に必須である状態を解消した。どちらが良いかは運用でしか決まらないので、閾値と行動を先に書いておく。

未審査比率 50% を準承認の反証条件とすることを撤回する(v0.12):

v0.8 はこれを「準承認という設計そのものの反証条件」と置いた。**その反証条件は「Review が遅れても最終的には追いつく」という前提の上に立っていた。** 17節で前提ごと撤回した以上、比率が 50% を超えることは設計の帰結であって故障ではない。

捕捉が自動化されれば(16.3節)、比率は着手と同時に閾値を超える。そこで「2 週間観測する」のは儀式にしかならない。

**撤回が誤りだったと知る方法**は 17節に書いた(訂正が古い未審査項目に集中するか)。

タグ比率(80%)の観測は残すが、意味を変える。準承認の健全性ではなく、**その Scope に確定済みの対照が存在するか**の観測とする。張り付いた Scope は故障ではなく「人間がまだ一度も目を通していない Scope」であり、対策は Review でなくてもよい——使う頻度が低ければ放置も正しい判断である。

代わりに測るのは露出と実害とする。

- 取得文脈中の未審査項目の露出率
- 100 Retrieval あたりの User 訂正数
- 期限切れ・完了済みの内容が現在値として使われた件数
- 例外的な Review に使った時間

訂正数の閾値は今は決めない。**基準線が存在しないため**であり、運用開始から一定期間で基準を取ってから引く。

### 27.3.1 Review 負荷への対策と、採らない対策

27.4a の実測で、1 セッションあたり平均 5.8 件の Candidate が上がった。1 件 1 分で捌いても、1 日 2 セッションで 12 分となり、着手前から上の閾値を超える見込みである。

**採る対策**は二つ。Review の単位をセッション束にすること(18.1節)と、candidate を準承認として本文まで渡すこと(21.1節)。前者は文脈スイッチの回数を減らし、後者は滞留が実損に直結する構造を外す。

**採らない対策**を、理由とともに記録する。

**Auto Commit の対象範囲を先に広げること。** 17 節の三区分は負荷調整の弁ではなく、危険度の分類である。observation や interpretation を Auto Commit に落とすことは、未審査の解釈が Active になる経路を開くことであり、既存の記憶置き場の監査で見つかった汚染を Mashu の中に作り直すことになる。負荷の問題を安全性の予算で払うのは筋が違う。

**抽出の閾値を上げて件数を絞ること(主対策としては採らない)。** 31 件中 29 件が Candidate になったことは、抽出が過剰である証拠ではない。分布として妥当である可能性が高く、既存の記憶置き場との重複 6 件は判定基準3(v0.5)によって既に別枠へ割れている。残りは実在する知識候補であり、閾値で削ることは知識の取りこぼしと引き換えになる。取りこぼしは Review 疲れと違って観測できない損失であり、後から気づけない。

### 27.4 Session End Extraction のオフライン検証

16.1 節により Session End Extraction は新規抽出と退役の洗い出しの二つを担う。両者は**前提条件が異なる別の検証**であるため、分けて扱う。

本節は Write Policy の検証であると同時に、移行後の運用で実際に何が Proposal として上がってくるかを事前に見る先行分でもある。27.1 が既存在庫の移植を扱うのに対し、本節は移行後の流入を扱う。

#### 27.4a 新規抽出の検証

前提条件なし。システム実装を必要とせず、いつでも実施可能。**最優先で早期に行う。**

過去の Agent セッションログ 5 本程度に対し、抽出プロンプトを適用して Proposal 案を生成させる。抽出結果が Scratch 級の内容ばかりであれば、Write Policy 自体を再設計する。

見るもの: Scratch 級を拾っていないか、粒度は適切か、Observation と Interpretation を分けられているか。

注記(実測から):

新規抽出からは type = hypothesis がほとんど出ない。未検証のまま複数セッションを跨ぐ仮説は稀で、棄却された筋は「何を追っても無駄だったか」という observation として現れるためである。

したがって、**21.1節の Layer 3 の材料になる「disproven な既存 Memory」は、新規抽出からは原理的に生まれない。** Layer 3 を空でなくするのは 27.4b の退役の洗い出しだけである。両者を別の検証として分けた理由のひとつがここにある。

#### 27.4b 退役の洗い出しの検証

**前提条件: 27.1 の移植が完了していること。** したがって 30節の段 A より前には実施できない。

入力は二つ。**移植済みの Active 集合**と、その後に発生したセッションログ 1 本。

**全文ラベリングを撤回する(v0.12)**

v0.6 の手順はこうだった。

1. User が先に正解ラベルを付ける。そのセッションで実際に完了した Task、実際に覆った前提はどれかを、エージェントの出力を見る前に確定させる
2. エージェントに洗い出しを実行させる
3. 出力と正解ラベルを突き合わせる

**順序の原則は正しい。** 出力を見てから正解を決めると判定が出力に引きずられる。撤回するのは原則ではなく、**その原則を人間の全文精読で払うという支払い方**である。

実測すると、圧縮済みのセッションログ 1 本が約 38,000 token あり、これを Active 全件と突き合わせて事前に正解集合を作るのは、ラベリング予算のある評価設計を一人用の系に持ち込んでいる。しかも一人・一セッションでは、高コストなのに統計的には弱い——読み落としが正解として固定され、退役事例が数件しか出なければ Recall の評価が安定しない。そして**この負荷自体が、実運用で退役 Review を避ける行動を誘発する。**

**代わりに、通常運用が正解を先に生む形にする。**

1. User が完了・棄却を述べた時点で、それを **retirement marker** として保存する(16.1節の User Explicit 経路の副産物)
2. 抽出器には通常どおり全文と Active 集合を与える
3. marker を回収できたかで Recall を、追加提案の承認・却下で Precision を測る
4. 定期的に少数を無作為抽出して「このセッション後も Active で正しいか」を確認し、黙って見逃された退役を推定する

marker は出力より**時間的に先に確定する**ので、アンカリング防止は構造的に保たれる。専用のラベリング作業は要らない。

**撤回が誤りだったと知る方法**: 無作為抽出が、marker にならない退役の系統的なクラスを繰り返し検出すること。そのときはそのクラスについてだけ小さな固定ラベルセット(既知の退役 10 件 + 不変対照 10 件)を作って戻る。

代用しない理由:

移植前に、既存の native な記憶機構のファイル群を「当時の Active 集合」の代用とすることは**採らない**。理由は三つ。

- その在庫は未承認の解釈が混入していることが分かっている。汚染された入力で測ると、退役判定が外れたときに抽出プロンプトの欠陥なのか入力の汚染なのかを切り分けられない。較正に汚れた標準試料を使うのと同じである
- ファイルの更新日時から復元できるのは「その時点で存在したか」であって「その時点で Active だったか」ではない。native な記憶機構には Mashu の状態機械が無く、Active という概念が代用側に存在しない
- 移植を待てば本物の Active 集合が手に入る。数週間を節約するために汚染された近似を使うのは割に合わない

### 27.5 切替試験

CLI の native な記憶機構を無効化した状態で 2 週間運用する。

記録するもの:

**「Mashu 内に Active として存在する知識を、Agent が取得できないまま作業した」事故の件数。**

事故の定義: Mashu に Active な Version として存在する内容があるにもかかわらず、Agent がそれを取得せずに作業し、その結果として誤った前提で進んだ場合を 1 件と数える。

「数週間まわる」のような体感で判定しない。判定が印象に依存すると、移行できたかどうかが測れないためである。

事故が発生した場合は、原因を次のどちらかに切り分けて記録する。

- Bootstrap の内容不足(21.2節に入れるべきものが入っていなかった)
- pull の動機不足(索引はあったが Agent が引きに行かなかった)

前者は Bootstrap の内容と token 上限の見直しへ、後者は 6.1節の呼び出し要件の見直し(第一段から第二段への移行判断)へつなぐ。

## 28. Test Harness

Phase 0 から作成し、各 Phase と並走させる。
実装前にシナリオを書き下し、シナリオ自体を仕様の曖昧さの検出器として使う。

最低限シナリオ:

1. 棄却 Memory 再利用(Day1 仮説化 → Day3 棄却 → Day10 同問題を質問)
2. 古い Version 混入(Superseded が Context に現れないこと)
3. Entity 重複(類似名での新規作成が防止されること)
4. 同時更新(based_on_version 不一致の reject)
5. 承認遅延(Candidate が滞留したときの Retrieval 挙動)

シナリオ 1〜3 は Agent 接続前に、30節の段 A から始まる shadow 運用で検証する。

## 29. 実装しないもの(MVP 外)

- 完全 Dependency Graph(memory_evidence の逆引きで代替)
- 自動 Conflict 検出・解決(手動記録のみ)
- Retrieval Decay(Version 管理と Active Filter で主要問題を解決)
- 自動 Promotion
- Interpretive Context 圧縮
- Web UI(CLI で開始)
- Codex / Gemini 接続(MCP 化により追加コストは小さいが、MVP 後)
- Graph Database
- Policy Learning

## 30. 開発計画

v0.3 は実装時間を軸に組み、v0.6 は暦を軸に組み直した。**v0.12 は依存の鎖を軸に組む。**

週番号を書かない。前の二つの計画はどちらも週に紐づけて書かれ、そのことが「その週に何をするか」を先に決めさせ、**「その項目が無いと次が始まらない」という関係のほうを見えなくした。** 実際に v0.6 の計画は 13 週すべてを人間の出席で埋め、撃発装置を作る行を一つも持たないまま閉じている。番号を外して鎖だけを書けば、抜けた項目は「順番が飛んでいる」として現れる。

### 旧計画の何が誤っていたか

事実は三つで足りる。

1. 仕様が命名する 70 操作のうち 14 が CLI からも MCP からも到達できない。その分布が問題の全体である——**16.1節の捕捉契機は三つとも、27節の検証・計測は八つ、18.1節の Diff View と Edit、そして Conflict 解決。** 知識を保存し取り出す操作はすべて作られ、頼まれずに捕捉する操作と、系が機能しているかを測る操作だけが作られていない
2. 旧 30節に、人間が出席せずに動くものを作る行が一つも無い。16節は Session End Extraction を二本の書き込み契機の一本と定め、27.4a はそのプロンプトをオフラインで検証済みだが、**それを撃発する装置を作る項目がどこにも無い。** 芯とされた 5 週も、較正点(手動運用の開始日)も、すべて人間の出席を測っている
3. 帰結として、CLI 組み込みの記憶機構は勝手に(下手に)書くのに対し、本系は誰かが意図的に動かさない限り何も書かない。厳密なほうが退行として体感される

**モデルへの含意。** 誤りは「厳密すぎた」ことではない。27.3.1 が「負荷の問題を安全性の予算で払わない」と Auto Commit の拡大を拒んだ判断は今も正しい。誤っていたのは選択肢の張り方で、**厳密 対 緩和の一軸しか盤上になく、手動 対 自動という第三の軸が書き込み側には置かれなかった。**

正確には、第三の軸は一度だけ盤上に載っている。**v0.7 の準承認は、読み側について「負荷を緩和でなく構造変更で外す」操作そのものだった。** 同じ操作が書き側(捕捉の撃発)には適用されず、計画は v0.3 の「人間が唯一のモーターである」という前提のまま、以後のどの改訂でも引き直されなかった。

### 最適化する対象

**系は誰も面倒を見ない一週間、機能しつづける。** 知識は溜まり、引け、腐った項目は返らなくなる。その週に人間の操作はゼロである。

生き残る安全制約はただ一つ。**未審査の解釈が、確立した事実として書かれることはない。**

なお「無人」とは Review と保守をしないことであって、会話が起きないことではない。無人の期間にもセッションは通常どおり発生する。この区別が設計を軽くする——**User の発話は無人の週にも系へ届いており、届いた発話は書き込み契機として使える。**

### 依存の鎖

```
A 撃発装置 ──> B 退役の三形 ──> D 突き合わせ ──> E 切替 ──> F 無人期間 ──> G 反映
     |               |                              ^
     +──> C 人間面 ──+                              |
                     (C は E の前提。無人期間の直前に一度は人が触れる形が要る)
```

**段 A: 撃発装置**

これが無いと以後のすべてが始まらない。捕捉されないものは退役もできず、測る対象も生まれない。

- SessionEnd hook(各 CLI)。enqueue のみ
- 常駐 worker と `extraction_run` 台帳、retry、dead letter、Bootstrap 経由の health 警告
- Scratch の読み書き(25.1節)と `agent_session` の外部セッション対応
- Scratch-first の新規抽出、skip 基準、日次の入力予算
- Temporary Context(25.2節)の最小形。**User 由来のみ**
- cwd から Scope への明示 map
- 15.1節の重複チェックを仕様(title embedding 照合)へ揃える —— **実装済み**

ここから **shadow 運用**を始める。native な記憶機構は切らない。捕捉品質は実セッションで測れるので、旧計画の 27.4a(過去ログへのオフライン適用)はこれに置き換わる。

**段 B: 退役の三形**

「腐った項目が返らなくなる」を無人で成立させる機構を、判断の重さで三段に分ける。

1. **時限による退役** —— Temporary Context の read filter。判断ゼロ、worker 不要
2. **User 発話による退役** —— 完了・棄却の宣言を拾って Auto Commit で落とす。書いた本人の判断を時計と transcript が執行するだけで、新しい判断はない。人が直接叩く `mashu retire <id> completed|disproven|dormant --reason` を足す(16.1節が User Explicit を契機と定めながら、人間用の入口が無かった)
3. **Agent 推論による退役** —— **無人では落とさない。** candidate として積み、Layer 1 の当該項目に「退役候補あり(理由)」の注記を付けて返す(21.1節)

3 で「N 日誰も異議を挟まなければ自動で dormant へ落とす」を採らない理由: **それは「無反応」を「同意」と読む機構であり、無人の週にはまさに全員が無反応である。** 誤退役は誤追加より発見が遅い——消えたものは検索に現れない。

同じ段で、Agent の Temporary Context 書き込み(`context_put`、window 上限あり、Bootstrap には載せない)を入れる。ここから **retirement marker** が溜まりはじめ、段 D の突き合わせが可能になる。

**段 C: 人間面**

Review は随意になるが、**随意だからこそ一回の着席で完結しなければ二度と開かれない。**

- 日常面を `review` / `remember` / `retire` / `find` / `inspect` に寄せ、保守・移植・評価を `admin` 以下へ分離する
- `mashu review` は最古の束を開き、一括承認・番号指定の Edit・理由つき却下・**skip の明示化**まで一画面で終える。現在の skip は pending へ黙って戻るので、後で見る / 却下 / 保留の宣言を要求する
- Diff View(既存 Active との差分)を束表示に含める。**Edit の定義**は「承認時に User が本文を修正し、修正後の Version を User の編集として記録する」とする(18.1節で未定義だった)
- Scope 作成コマンド(7節が User のみ作成と定めながら入口が無い)
- `mashu status`: 27.3 改の指標の自動集計と、health の Bootstrap 掲載
- **時限を自称する Active の棚卸し**と Temporary Context への移行(25.2節)

**段 D: 計測と突き合わせ**

- `admin eval-retire`: marker と抽出出力の突き合わせ(27.4b 改)
- `admin thresholds`: 実在庫の類似度分布から閾値を決める(27.2) —— **実装済み**
- **Scope Detection の作り直し。** 27.2 の実測で、名前と一行要約にクエリを当てる方式が成立していないことが判明した(全クエリが 0.72〜0.81 に収まり、順位が 7 問中 4 問で誤る)。閾値ではなく機構の問題であり、Scope は**保持している Memory** から判定する形へ変える。捕捉が自動化されると Scope の割り当てが毎晩効くので、ここは段 E の前に要る
- `mashu incident`: 事故の記録と三分類
- semantic dedup の有効化(閾値が実測で決まってから)

**段 E: 切替**

native な記憶機構を無効化する。切替は本系の機能ではなく設定手順として文書化し、試験窓の開始を event_log に記録する。

事故の分類に**三つ目を足す**。旧二分類は Bootstrap の内容不足と pull の動機不足だったが、自動捕捉を入れると **捕捉漏れ——そもそも書かれていなかった** が独立の原因になる。旧計画はこの分類を表現できなかった。

**段 F: 無人期間**

Review・保守・admin 操作をゼロにする。実装もしない。

**段 G: 反映**

Bootstrap の内容と上限、6.1節の第二段への移行判断。

### 暦に縛られる部分

段の順序とは別に、**日数でしか進まないもの**が二つある。

- **shadow 運用**: 段 A から段 D まで並走する。捕捉品質は実セッションの本数でしか測れない
- **切替試験と無人期間**: 事故は運用日数に比例してしか観測されない。両者を合わせて 2 週間程度

旧計画の芯 5 週(手動運用 3 + 切替 2)から手動運用 3 週が消えるのは、それが測ろうとした Review 負荷と滞留が随意化で降格したためである。捕捉品質はその間 native を切らずに shadow で測れるので、危険を伴わない。

**測定上の注意**: 無人期間を切替試験の中に置くと、「native を切った」と「誰も見ていない」が同時にかかり、事故を三分類のどれかに帰属できない。分けて測るのが厳密だが、**両方が同時にかかっている状態こそ実運用の条件**なので、交絡を承知で同時に測り、事故が出たときだけ切り分けを試みる。

### 到達不能 14 操作の行き先

旧計画の失敗を構造的に繰り返さないため、**全項目に行き先を書く。**

| 操作 | 行き先 |
|---|---|
| User 発話からの change_status 契機(16.1) | 段 B: `mashu retire`(人間用)+ worker の発話検出 |
| Session 終了時の新規抽出(16.1) | 段 A: worker |
| Session 終了時の退役洗い出し(16.1) | 段 B: worker の退役検出 |
| Conflict 解決(17) | **撤回**(23節) |
| Diff View(18.1) | 段 C: `mashu review` |
| Proposal Edit(18.1) | 段 C: `mashu review`。定義は「承認時の User 修正」 |
| 類似度分布と閾値決定(27.2) | 段 D: `admin thresholds` —— **実装済み** |
| 五指標の記録(27.3) | 段 C: `mashu status`。指標自体は 27.3 で改定 |
| 過去ログへの抽出適用(27.4a) | **置換**: 段 A からの shadow 運用が実セッションで同じ問いに答える |
| 正解ラベル付け(27.4b) | **置換**: 段 B の retirement marker(27.4b) |
| 退役洗い出しの実行(27.4b) | 段 B: worker |
| 出力と正解の突き合わせ(27.4b) | 段 D: `admin eval-retire` |
| 切替試験の制御(27.5) | 段 E: 設定手順の文書化と試験窓の記録。専用機構は作らない |
| 事故の記録と分類(27.5) | 段 D で実装、段 E から使用: `mashu incident` |

### 較正点

**段 B の終わり**に置く。実セッション連続 5 本について、

1. `extraction_run` に全 transcript の行があり(sweeper が拾い漏れを検出しない)
2. 沈黙する失敗がゼロで
3. 各セッションから少なくとも 1 件の Proposal または Temporary Context 項目が**人手ゼロで**生まれている

満たせない場合、原因が hook の信頼性にあるなら第一段を放棄して sweeper 起点(定期巡回)へ寄せ、原因が抽出品質にあるなら段 C を先行させて引き直す。

**旧計画の較正点が人間の開始日を見張ったのに対し、新しい較正点は装置の自走を見張る。** 引き直しの要点はここに尽きる。

### 反証条件

無人期間の明けに、この計画が誤っていたと分かる観測を、起きうる向きごとに先に書く。

1. **毒** —— worker が書いた candidate への訂正・却下が過半を占める。捕捉の質が負債を生んでいる。抽出の閾値を上げて Scratch 側へ寄せる(取りこぼしは Scratch に残るので観測可能な損失で済む)。改善しなければ、自動捕捉を Scratch 蓄積 + 定期の人間トリアージへ縮退する
2. **不毛** —— Proposal がほぼゼロ、または Scratch 級ばかり。Scratch-first が絞りすぎている。skip 基準と入力範囲を広げ、予算を実測で引き直す
3. **腐敗** —— 期限切れ・完了済みの内容が現在値として返った事故が 1 件でもある。機構別に切り分ける。Temporary Context の filter 漏れなら実装の不具合、時限つきの内容が indefinite 側に書かれていたなら棚卸し(段 C)の網の目、User 発話の拾い漏れなら marker の Recall の問題。三つ目が支配的なら、時限つき自動 dormant の再検討へ進む
4. **注記の無効化** —— Layer 1 の「退役候補あり」注記が付いた項目を、Agent が注記に言及せず現在値として使った事例が繰り返し観測される。21.1節の注記に固有の失敗である。注記を本文より前へ移すか、当該項目を注記つきの Layer 2 相当へ降格する形を設計する
5. **費用** —— 週次の抽出入力が予算を超過する。scratch-first が効いていない。chunk と skip を締め、それでも超えるなら「全セッション捕捉」から「marker のあるセッションのみ捕捉」へ縮退する

いずれも観測されなければ、この計画の中心仮説——**捕捉と退役の大半は判断ではなく寿命の型であり、型は無人で執行できる**——は当面反証されない。

MVP 後: Codex / Gemini 接続の拡張、Web UI、Conflict の自動検出、worker の User 主張(16.3節の二重の絞りを満たしたうえで)。

## 31. 成功条件

MVP(3ヶ月)の成功条件:

- 古い Version が Active な Context に混入しない
- Disproven Memory が再利用されない
- 変更履歴と判断理由(却下理由を含む)を追跡できる
- 人間が CLI から Memory 状態を修正できる
- Claude が MCP 経由で Retrieval / Proposal を実行できる
- CLI の native な記憶機構の内容が Mashu へ移植済みである。移植は全件 candidate として入れ Review を通したものであり、各項目が source_reference に由来を持つ(27.1節)
- 切替試験(27.5節)を実施し、native な記憶機構を切った状態での取得失敗事故の件数を記録している
- **誰も面倒を見なかった一週間の後で、知識は増えており、期限切れは返らなくなっており、そのどちらにも人間の操作を要していない**(v0.12)

最終成功条件(MVP 後):

- 複数 Agent 間で同一の Knowledge State を共有できる
- Agent の種類に依存しない(MCP による担保)

## 32. 最終目標

AI に人間のような記憶を与えることではない。

AI Agent が共有する外部 Knowledge State に、

- 記録
- 訂正
- 保留
- 棄却
- 再評価

の仕組みを与える。

Agent は記憶主体ではなく、Knowledge State を利用する推論エンジンとして扱う。
