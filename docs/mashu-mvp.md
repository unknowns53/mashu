# Mashu — Shared Agent Memory Layer / MVP 実装仕様書 v0.6

改訂履歴:

- v0.1: MVP 初版
- v0.2: Actor モデル、Commit Gate、Write Policy、Entity Resolution、Review UI を追加
- v0.3: スキーマ確定、接続方式を MCP に確定、MVP スコープ縮小、開発計画を実測ベースに改訂、プロジェクト名を Mashu に確定
- v0.4: Harness シナリオの書き下しで判明した欠落を補う。Retrieval の三層出力(21節)、Entity Status と Merge 手順(20節)、Proposal の重複チェック(15節)、Commit Gate への Entity 作成の追加(17節)、entity.status 列と disproven の reason 必須制約(26節)、滞留時間指標(27.3)
- v0.5: Session Bootstrap を復活させる。v4 の Always Inject Context が MVP 化で落ちていたことによる退行の修復。session_bootstrap Tool と呼び出し要件(6節)、Bootstrap の内容と token 上限(21.2節)、native memory からの移植規則と棚卸し単位(27.1節)、切替試験(27.5節)、成功条件2件の追加(31節)
- v0.6: Write Policy に退役の契機を追加(16.1節)。開発計画を較正点の発動により引き直す(30節)

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

## 9. Memory Version

Memory Version は不変履歴。
一度作成された Version は変更しない。変更は新 Version の作成として行う。

保持:

- version_id
- memory_id
- content
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
- version.status は candidate / superseded / disproven / dormant / completed のみを持つ
- active_version の切替は System が Status 遷移と同一トランザクションで行う

これにより「ポインタは v2 を指すが v3 が Active を名乗る」という不整合が構造的に発生しない。

## 11. Memory Status

Version が持つ状態:

- **candidate**: Proposal 済み。まだ採用されていない
- **superseded**: 新しい Version に置換された
- **disproven**: 誤りと判明した
- **dormant**: 現在利用しないが、将来再評価可能
- **completed**: 終了済み(Task 等)

Active は状態ではなくポインタで表現する(10節)。

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
```

過去状態の復活は、既存 Version の状態変更ではなく新 Version の作成(Restore)として行う。
既存 Version の復活は禁止。

## 13. Memory Type

- **Observation**: 直接観測。例: SATA 直結でも timeout 発生
- **Fact**: 確認済み情報
- **Interpretation**: 観測から導いた解釈。例: SSD 内部状態遷移の可能性
- **Hypothesis**: 未検証仮説
- **Decision**: 判断
- **Task**: 作業
- **Preference**: ユーザー設定
- **State**: Scope の現在状態(14節)

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
- operation(create / update_version / change_status / restore / merge)
- target_memory
- based_on_version(楽観ロック用。24節)
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

Proposal 作成時、同じ対象に対する pending な Proposal が既に存在するかを確認する。

target_memory が指定されている場合:

同一 target_memory の pending Proposal を検索する。

target_memory が NULL(operation = create)の場合:

同一 Scope 内の pending な create Proposal のうち、payload の title の title_embedding 類似度が Entity Resolution と同じ閾値(20節)以上のものを検索する。

該当がある場合、新規 Proposal を作成せず、既存 Proposal の proposal_id・payload・created_at・滞留日数を提案者へ返す。

理由:

未承認の候補は Retrieval の Layer 1 に現れない(21節)。Agent は自分が既に提案済みであることを Context の本文からは知りえないため、重複チェックがなければ Review が遅れるほど同一内容の Proposal が Queue に積み上がる。これは User の Review 負荷を、知識状態の改善を伴わずに増やす。

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

## 17. Commit Gate

目的は正しさの判断ではない。危険な更新を止めることである。

### Auto Commit(System が自動反映)

- Preference
- User 明示変更
- 単純 Task 完了

### Candidate Commit(candidate として保留、Review 待ち)

- Fact
- Interpretation
- Hypothesis
- State(Current State)変更

### Human Review Required(User 承認必須)

- Active Version の切替
- Disproven 化
- Restore
- Entity 作成(類似度が閾値超過)
- Entity Merge
- Conflict 解決

Entity 作成が Review 対象になるのは、類似度が閾値を超えているにもかかわらず Agent が新規作成を選んだ場合に限る(20節)。閾値未満の新規作成は Review を経ない。

Auto Commit の対象範囲は、Review 負荷の実測(27節)に基づいて調整する。

## 18. Human Review Interface

MVP では CLI として実装する。Web UI は MVP 後。

理由:

UI の使い勝手要求は運用してみるまで見えず、先に Web 化すると手戻りが大きい。

必要機能:

- **Candidate Queue**: 未承認 Proposal の一覧
- **Diff View**: 変更前後の比較(active_version の content との差分)
- **Action**: Approve / Reject(理由必須)/ Edit / Merge

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
| Layer 2 Pending | active_version が指していない candidate、および status = provisional な Entity | Memory ID / title / 提案者 / created_at / 滞留日数 | 同一内容を再提案しない。未承認であり、推論の前提に使わない |
| Layer 3 Retired | status = disproven / dormant の Version | Memory ID / title / status / reason | 再導出しない。reason に反する主張をする場合は新たな根拠を示す |

Layer 2 と Layer 3 で content を渡さないのは、いずれも「現在の知識ではないもの」だからである。本文を渡せば Agent がそれを現在値として扱う危険があり、Active Version Filter を設けた意味がなくなる。

Layer 2 が渡すのは「その対象について未承認の提案が既に存在する」という事実のみ、Layer 3 が渡すのは「何が、なぜ否定されたか」のみとする。Layer 3 で content ではなく reason を渡すのは、否定された主張そのものより、否定した根拠のほうが Agent の再導出を止めるからである。

superseded は三層のいずれにも含めない。置換済みの内容に対応する現在値は Layer 1 が持っており、追加の情報を持たない。

取得経路:

Layer 1 は 21節のパイプラインをそのまま通す。Layer 2 と Layer 3 は、Scope Detection で確定した Scope の範囲内で title_embedding により引く。content_embedding を使わないのは、content を渡さない層に対して本文の類似度で順位をつける意味がないためである。

Layer 2 と Layer 3 に含めた Memory ID も、Layer 1 と同様に event_log の context_assembled へ記録する(22節)。

MVP の既定:

Layer 1 は常に返す。Layer 2 と Layer 3 は、Layer 1 で当たった Entity と同一 Scope のものに限る。Scope 全体を返す形は、Layer 3 が肥大したときに Context を圧迫するため MVP では採らない。

### 21.2 Session Bootstrap

21節のパイプラインは Query 起点であり、Agent が引かなければ何も返さない。Session Bootstrap は Query なしで、Session 開始時に必ず渡す固定コンテキストである。

本節は v4 に Always Inject Context として存在した機構の復活である。MVP 化の際に落ち、v0.1 から v0.4 まで欠けていた。

内容は三つ。

1. **Active な Preference の全件**: type = preference の Active Version
2. **対象 Scope の Current State**: type = state の Active Version(14節)
3. **Scope 索引**: scope 台帳の全件と、各 Scope の一行要約

三つ目が最も重要である。

セッション中の pull が機能しない根本の理由は、Agent が「知らないことを知らない」ために検索の動機を持てないことにある。三つ目は知識の中身ではなく**知識の地図**であり、Mashu が何を知っているかの索引が最初に入って初めて、以後の `memory_search` が動機を持つ。地図があれば pull は生きる、地図が無ければ pull は死ぬ。

実装:

type が preference と state の Active Version を返し、scope 台帳を一行要約付きで並べるだけである。スキーマの追加を必要とせず、既存の Retrieval 部品の組み合わせで足りる。14節で Current State を専用テーブルから type = State の Entity へ統合した決定が、ここで効いている。

Token 上限:

Bootstrap は毎セッション必ず消費する固定費であり、上限を設けないと予算が Memory 件数に比例して膨らむ。上限は 2000 token とする。

超える場合は Scope 索引を全件残し、Preference と Current State の側を削る。削った項目は Memory ID と title のみを示し、本文を落とす。地図さえ残れば Agent は `memory_get` で取りに行けるが、地図を削ると取りに行く先が分からなくなるためである。

上限値は 27.5 の切替試験で実測して見直す。

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

MVP では User が気づいた矛盾を手動で Conflict レコードとして記録する。

保持: conflict_id / memory_a / memory_b / type / status / note

解決は Human Review Required 操作。自動解決はしない。
自動検出は運用データを見た後に設計する。

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
        -- candidate / superseded / disproven / dormant / completed
        -- active は状態ではなく entity.active_version で表現(10節)
    supersedes        UUID REFERENCES memory_version(version_id),
    reason            TEXT,
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
        -- create / update_version / change_status / restore / merge
    target_memory    UUID REFERENCES memory_entity(memory_id),
    based_on_version UUID REFERENCES memory_version(version_id),
    payload          JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
        -- pending / approved / rejected / auto_committed
    reviewer         TEXT,
    decided_at       TIMESTAMPTZ,
    decision_reason  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
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
    scratch     JSONB
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

全件処理を切替の前提条件にすると、移行コストが在庫量に比例して膨らみ、移行そのものが失敗する。

### 27.2 Entity Resolution 閾値

紛らわしい Entity 名(SSD障害解析 / SSDデバッグ / 外付けSSD問題)を意図的に登録し、採用した埋め込みモデルでの類似度スコア分布を実測して閾値を決定する。

### 27.3 Review 負荷

手動運用期間中、週あたりの Candidate 件数と処理時間を記録する。
1日10分を超える場合、Agent 接続前に Auto Commit の対象範囲を拡大する。

滞留時間:

件数と処理時間に加えて、Proposal ごとの滞留時間(created_at から decided_at まで)を記録する。

理由:

処理時間は User の負担を表すが、滞留時間は知識状態の欠落を表す。未承認の候補は Retrieval の Layer 1 に現れない(21.1節)ため、滞留時間はそのまま「その知識が利用できなかった期間」である。両者は別の指標であり、片方だけでは Auto Commit の範囲を決められない。

統計は中央値と 90 パーセンタイルを取る。平均は少数の長期滞留に引きずられ、Queue の底に沈んだ Proposal を見えなくする。

判定:

滞留時間の 90 パーセンタイルが 1 週間を超える場合、Agent 接続前に Auto Commit の対象範囲を拡大するか、Write Policy(16節)の抽出量を絞る。

### 27.4 Session End Extraction のオフライン検証

過去の Agent セッションログ 5 本程度に対し、抽出プロンプトを適用して Proposal 案を生成させる。
抽出結果が Scratch 級の内容ばかりであれば、Write Policy 自体を再設計する。
本検証はシステム実装を必要とせず、いつでも実施可能。最優先で早期に行う。

本節は Write Policy の検証であると同時に、移行後の運用で実際に何が Proposal として上がってくるかを事前に見る先行分でもある。27.1 が既存在庫の移植を扱うのに対し、本節は移行後の流入を扱う。

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

シナリオ 1〜3 は Agent 接続前に 27 節の手動運用で検証する。

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

## 30. 開発計画(3ヶ月)

v0.3 の週割りは実装時間を軸に組んでいた。較正点の発動(後述)によりこれを破棄し、**暦でしか進まない人間の検証期間**を軸に組み直す。

圧縮不能の芯は二つある。

- **手動運用 3 週**(27.1〜27.3): Review 負荷と滞留時間は日単位で積み上がるため、まとめて短縮できない
- **切替試験 2 週**(27.5): 事故は運用日数に比例してしか観測されない

この 5 週は並列化も前倒しもできない。実装はこの芯の隙間と並走に入る。

| 期間 | 内容 | 暦の消費 |
|---|---|---|
| 完了 | Phase 0: DB スキーマ、event_log、Status 遷移、Harness シナリオ書き下し | — |
| Week 1 | Phase 1: Proposal / Commit Gate / Memory Hub API | 実装 |
| Week 1 | 27.4 オフライン検証(抽出と退役の洗い出し。実装不要) | 実装と並走 |
| Week 2 | Phase 2a: 埋め込み生成、Retrieval Pipeline、三層出力、Entity Resolution | 実装 |
| Week 3 | Phase 2b: Review CLI、native memory の移植(Scope 単位、27.1 の規則) | 実装 |
| **Week 4–6** | **手動運用 3 週**: 27.1 Retrieval 品質 / 27.2 閾値実測 / 27.3 Review 負荷と滞留時間 | **暦・圧縮不能** |
| Week 4–6 | 並走: Phase 3 実装(MCP Server、session_bootstrap、指示ファイル要件) | 実装 |
| Week 7 | Claude 接続、Bootstrap 込みの通し確認、Harness シナリオ 1〜4 通過 | 実装 |
| **Week 8–9** | **切替試験 2 週**: 27.5。native な記憶機構を切り、取得失敗事故を記録 | **暦・圧縮不能** |
| Week 10 | 事故ログの反映: Bootstrap の内容と token 上限、6.1節の呼び出し要件の見直し | 実装 |
| Week 11–13 | 緩衝 | 緩衝 |

緩衝 3 週の使いみち:

手動運用が 3 週で足りなかった場合の延長、切替試験のやり直し、Auto Commit 範囲の調整。実装の遅れではなく、**芯の期間が伸びたとき**に使う。

較正点:

v0.3 の較正点「Phase 0 を 2 週間で完了できるか」は**発動済み**である。

実際の所要は 1 セッションで、超過ではなく大幅な下振れだった。しかし較正点の趣旨は所要時間そのものではなく、見積もりの前提が保たれているかにある。「人間が手で書く」という前提が崩れた以上、方向が逆でも計画は引き直す。

**新しい較正点は手動運用の開始日とする。** Week 3 の終わりまでに手動運用へ入れない場合、5 週の芯と緩衝が 3 ヶ月に収まらないため、その時点で再度引き直す。

実装の所要時間は較正点にしない。前提が崩れた以上、それは拘束条件ではなくなったためである。

3ヶ月時点の到達目標:

**Claude 1 体を MCP 接続して Harness シナリオ 1〜4 が通過し、かつ native な記憶機構を切った状態で切替試験を完了して、取得失敗事故の件数を手元に持っていること。**

MVP 後(4ヶ月目以降): Codex / Gemini 接続、Web UI、Conflict 検出、Auto Commit 範囲の調整。

## 31. 成功条件

MVP(3ヶ月)の成功条件:

- 古い Version が Active な Context に混入しない
- Disproven Memory が再利用されない
- 変更履歴と判断理由(却下理由を含む)を追跡できる
- 人間が CLI から Memory 状態を修正できる
- Claude が MCP 経由で Retrieval / Proposal を実行できる
- CLI の native な記憶機構の内容が Mashu へ移植済みである。移植は全件 candidate として入れ Review を通したものであり、各項目が source_reference に由来を持つ(27.1節)
- 切替試験(27.5節)を実施し、native な記憶機構を切った状態での取得失敗事故の件数を記録している

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
