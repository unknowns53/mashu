# Mashu — Shared Agent Memory Layer / MVP 実装仕様書 v0.3

改訂履歴:

- v0.1: MVP 初版
- v0.2: Actor モデル、Commit Gate、Write Policy、Entity Resolution、Review UI を追加
- v0.3: スキーマ確定、接続方式を MCP に確定、MVP スコープ縮小、開発計画を実測ベースに改訂、プロジェクト名を Mashu に確定

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

MVP では Claude 1体のみを接続する。
Codex / Gemini は MVP 後の接続とする。

注記: 各 CLI の MCP 対応状況は変化が速いため、Phase 3 着手時に最新ドキュメントで再確認する。

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

## 16. Write Policy

Proposal 作成タイミングは二本立てとする。

1. **User Explicit**: 「これは覚えておいて」等の明示要求。即 Proposal
2. **Session End Extraction**: Session 終了時、Agent が長期価値のある情報を抽出して Proposal 化。それ以外は Scratch に残す

Session End Extraction の抽出品質は実装前にオフラインで検証する(27節)。

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
- Entity Merge
- Conflict 解決

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

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- 埋め込み次元は採用モデルに依存するため、決定後に置換する
-- 以下では仮に 1024 とする

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
    content_embedding vector(1024)
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

## 27. Agent 接続前検証

Phase 3(Agent 接続)の前に、User 自身が Agent 役として CLI から運用する期間を 2〜3 週間設ける。

検証項目:

### 27.1 Retrieval 品質

実在プロジェクト 1 件(例: SSD 障害解析)から Memory を 30〜50 件手動登録し、想定質問に対して:

- 正しい Memory が上位に来るか
- superseded / disproven が混入しないか

Harness シナリオ 1・2 はこの段階で Agent なしで検証できる。

### 27.2 Entity Resolution 閾値

紛らわしい Entity 名(SSD障害解析 / SSDデバッグ / 外付けSSD問題)を意図的に登録し、採用した埋め込みモデルでの類似度スコア分布を実測して閾値を決定する。

### 27.3 Review 負荷

手動運用期間中、週あたりの Candidate 件数と処理時間を記録する。
1日10分を超える場合、Agent 接続前に Auto Commit の対象範囲を拡大する。

### 27.4 Session End Extraction のオフライン検証

過去の Agent セッションログ 5 本程度に対し、抽出プロンプトを適用して Proposal 案を生成させる。
抽出結果が Scratch 級の内容ばかりであれば、Write Policy 自体を再設計する。
本検証はシステム実装を必要とせず、いつでも実施可能。最優先で早期に行う。

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

## 30. 開発計画(3ヶ月、週10〜15時間想定)

| 期間 | Phase | 内容 |
|---|---|---|
| Week 1–2 | Phase 0 | DB スキーマ、event_log、Status 遷移の決定的ロジック、Harness シナリオ書き下し |
| Week 3–5 | Phase 1 | Proposal / Commit Gate / Memory Hub API |
| Week 6–8 | Phase 2 | Review CLI、実データ移植(30〜50件)、手動運用開始 |
| 随時(早期) | — | 27.4 抽出オフライン検証(実装不要のため最優先) |
| Week 9–10 | Phase 2 | Retrieval Pipeline、Entity Resolution 閾値実測 |
| Week 11–13 | Phase 3 | MCP Server 化、Claude 接続、シナリオ 1〜4 通過 |

較正点:

Phase 0 を 2 週間で完了できるかを最初の較正点とし、超過した場合は全体計画を引き直す。

3ヶ月時点の到達目標:

**Claude 1体を MCP 接続し、Harness シナリオ 1〜4 が通過すること。**

MVP 後(4ヶ月目以降): Codex / Gemini 接続、Web UI、Conflict 検出、Auto Commit 範囲の調整。

## 31. 成功条件

MVP(3ヶ月)の成功条件:

- 古い Version が Active な Context に混入しない
- Disproven Memory が再利用されない
- 変更履歴と判断理由(却下理由を含む)を追跡できる
- 人間が CLI から Memory 状態を修正できる
- Claude が MCP 経由で Retrieval / Proposal を実行できる

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
