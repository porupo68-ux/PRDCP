# PRDCP v2 Migration Map v0.1

**目的:** 現在分離している5つのPrototypeを、単一のCanonical Project `PRDCP_v2` に安全に統合するための移行設計図。

**基本原則:** 「5つを混ぜる」のではなく、**共通部分を1つにし、Layer固有部分だけを明確に分離して残す**。

---

## 1. 統合後の目標構造

```text
PRDCP_v2/
│
├─ main.py
├─ runtime.py
├─ README.md
├─ .env
├─ .env.example
├─ requirements.txt
├─ pyproject.toml
│
├─ config/
│  ├─ README.md
│  ├─ settings.py
│  └─ models.json
│
├─ common/
│  ├─ README.md
│  ├─ role_definitions/
│  │  └─ RDLoader関連
│  ├─ protocol/
│  │  └─ PMP関連
│  ├─ models/
│  └─ ...
│
├─ specifications/
│  ├─ README.md
│  ├─ schemas/
│  ├─ registries/
│  └─ ...
│
├─ role_definitions/
│  ├─ README.md
│  ├─ producer/
│  │  ├─ README.md
│  │  └─ *.json
│  ├─ researcher/
│  │  ├─ README.md
│  │  └─ *.json
│  ├─ deliberation/
│  │  ├─ README.md
│  │  └─ *.json
│  ├─ conclusion/
│  │  ├─ README.md
│  │  └─ *.json
│  └─ playwright/
│     ├─ README.md
│     └─ *.json
│
├─ producer/
│  ├─ README.md
│  ├─ manager.py
│  ├─ registry.py
│  ├─ workflow.py
│  ├─ state.py
│  └─ agents/
│
├─ researcher/
│  ├─ README.md
│  └─ ...
│
├─ deliberation/
│  ├─ README.md
│  └─ ...
│
├─ conclusion/
│  ├─ README.md
│  └─ ...
│
├─ playwright/
│  ├─ README.md
│  └─ ...
│
├─ providers/
│  ├─ README.md
│  ├─ mock.py
│  └─ openrouter.py
│
├─ discord_app/
│  ├─ README.md
│  ├─ bot.py
│  ├─ commands.py
│  ├─ channel_router.py
│  ├─ message_formatter.py
│  └─ ...
│
├─ storage/
│  ├─ README.md
│  └─ data/
│     ├─ workflows/
│     │  ├─ producer/
│     │  ├─ researcher/
│     │  ├─ deliberation/
│     │  ├─ conclusion/
│     │  └─ playwright/
│     ├─ deliveries/
│     └─ logs/
│
├─ tests/
└─ docs/
```

これは**目標構造**であり、現行コードに存在しないディレクトリを無理に新設する必要はありません。実際の差分監査後に確定します。

---

## 2. Migration Map

| 現在 | 統合後 | 処理 | 理由 |
|---|---|---|---|
| 5つの`.env` | `/.env` | 統合 | Runtime設定を一元管理 |
| 5つの`.env.example` | `/.env.example` | 統合 | 設定仕様を一本化 |
| 各`main.py` | `/main.py` | 統合 | Entry Pointを一本化 |
| 各`runtime.py` | `/runtime.py` | 統合 | Manager構築処理を一本化 |
| 各`config/` | `/config/` | 統合 | Model・Settings一元管理 |
| 各PMP実装 | `/common/`または`/specifications/` | 統合 | 全Layer共通規格 |
| 各RDLoader | `/common/role_definitions/` | 統合 | RD参照機構を一本化 |
| Producer RD | `/role_definitions/producer/` | 維持 | Producer固有 |
| Researcher RD | `/role_definitions/researcher/` | 維持 | Researcher固有 |
| Deliberation RD | `/role_definitions/deliberation/` | 維持 | Deliberation固有 |
| Conclusion RD | `/role_definitions/conclusion/` | 維持 | Conclusion固有 |
| Playwright RD | `/role_definitions/playwright/` | 維持 | Playwright固有 |
| Producer実装 | `/producer/` | 維持 | Layer責務を保持 |
| Researcher実装 | `/researcher/` | 維持 | 同上 |
| Deliberation実装 | `/deliberation/` | 維持 | 同上 |
| Conclusion実装 | `/conclusion/` | 維持 | 同上 |
| Playwright実装 | `/playwright/` | 維持 | 同上 |
| 各`providers/` | `/providers/` | 統合 | OpenRouter/Mock共通 |
| 各`discord_app/` | `/discord_app/` | 統合 | Discord UI一本化 |
| `channel_router.py` | `/discord_app/channel_router.py` | 共通化 | Layer routingの唯一の実装 |
| 各`storage/`実装 | `/storage/` | 統合 | Repository共通化 |
| 各`storage/data` | `/storage/data/` | 統合 | Workflow保存場所固定 |
| 各`tests/` | `/tests/` | 統合・分類 | 全体テストを一元化 |
| 各README | 各責務フォルダ | 再構成 | 管理・説明性向上 |

---

# 3. RDのMigration Policy

ここは重要なので独立したルールにします。

### RDそのもの

31 AgentのRDは**重複排除対象ではあるが、Agent単位の分離は維持**します。

例えば、

```text
role_definitions/
└─ researcher/
   ├─ research_manager.json
   ├─ academic_researcher.json
   ├─ government_researcher.json
   ├─ industry_researcher.json
   ├─ expert_researcher.json
   ├─ politician_researcher.json
   ├─ news_researcher.json
   ├─ public_opinion_researcher.json
   └─ quality_reviewer.json
```

のようにします。

### RDLoader

一方でRDLoaderは共通機構なので、

```text
Agent
 ↓
Registry
 ↓
共通RDLoader
 ↓
対応するAgent RD
 ↓
RDの制約を取得
 ↓
Agent実行
```

とします。

**統合後もAgentが作業するときにRDを参照する現在の設計を維持することを必須条件とします。**

さらに現在の

```text
PRDCP_RD_STRICT=true
```

も維持します。

RD統合によって、

> RDはフォルダに存在するがRuntimeでは使われていない

という状態になることは禁止します。

---

# 4. PMP・Specification

PMPは全Layer共通契約なのでCanonical Copyを**1つだけ**持ちます。

```text
Producer ───────┐
Researcher ─────┤
Deliberation ───┼──→ Canonical PMP
Conclusion ─────┤
Playwright ─────┘
```

Agent Registry、RD Registry、PMP Message Types、Metadata Status、Cross-layer Handoffなども同様です。

現在Doctorで確認されている、

```text
Agent Registry
Model Configuration
RD Registry
PMP Message Types
PMP Metadata Status
Cross-layer Handoff
```

について、**統合後もDoctorによる整合性検査対象とします。**

---

# 5. Storage Migration

現在の、

```text
PRDCP_Producer_Prototype_v2.../
└─ prdcp/
   └─ storage/
      └─ data/
```

が事実上の共通Storageになっている状態を廃止します。

統合後は、

```text
PRDCP_v2/
└─ storage/
   └─ data/
```

のみをCanonical Storageとします。

そして、

```text
storage/data/
├─ workflows/
│  ├─ producer/
│  ├─ researcher/
│  ├─ deliberation/
│  ├─ conclusion/
│  └─ playwright/
│
├─ deliveries/
└─ logs/
```

とします。

同じ、

```text
workflow_id
```

を5 Layerで共有する現在の方式は維持します。

---

# 6. Discord Migration

Discordも5コピーを廃止して、

```text
PRDCP_v2/discord_app/
```

だけにします。

現在確認できている、

```text
#producer
#researcher
#sources
#deliberation
#conclusion
#playwright
#deliveries
#workflow-status
```

などへのルーティング構造を維持します。

`channel_router.py`は**PRDCP全体で1つ**です。

また、すでに確認した、

```text
#researcher で !producer_topic
        ↓
拒否
```

のようなChannel Guardも維持します。

---

# 7. README Migration

READMEは単純に1個へ統合しません。

### Root README

```text
PRDCP_v2/README.md
```

には、

- PRDCPとは何か
- 5 Layer
- 全体Architecture
- Installation
- `.env`
- 起動方法
- Discord
- Doctor
- Mock/OpenRouter
- E2E
- ディレクトリ構造
- 成果物
- Troubleshooting

を記載します。

### Layer README

例えば、

```text
researcher/README.md
```

には、

- Layer Purpose
- Agents
- Input
- Workflow
- Output
- RDとの関係
- Producer / DeliberationとのInterface
- 主要ファイル

を記載します。

### Infrastructure README

さらに、

```text
discord_app/README.md
storage/README.md
role_definitions/README.md
```

など、**独立して理解する価値のある共通機構**にはREADMEを置きます。

逆に小さなディレクトリすべてにREADMEを置く必要はありません。

---

# 8. 削除対象

統合後に削除できる可能性が高いものは、

```text
同一内容の .env.example × 4
同一 main.py × 4
同一 runtime.py × 4
同一 discord_app × 4
同一 common × 4
同一 specifications × 4
同一 providers × 4
同一 storage実装 × 4
同一config × 4
重複RD
重複README
```

などです。

ただし、**内容を比較せず削除することは禁止**します。

ファイル名が同じでも内容が違えばMigration対象です。

---

# 9. Migration Procedure

実際の統合では次の順番を固定します。

```text
Phase 0
現行5 Prototypeをバックアップ
        ↓
Phase 1
全ファイルのHash/Path比較
        ↓
Phase 2
完全一致 / 差分 / 固有ファイルに分類
        ↓
Phase 3
Canonical Copyを決定
        ↓
Phase 4
新規 PRDCP_v2/ を構築
        ↓
Phase 5
共通機構を移行
        ↓
Phase 6
5 Layer固有コード + RDを移行
        ↓
Phase 7
Storage / Discord / READMEを統合
        ↓
Phase 8
静的整合性検査
        ↓
Phase 9
Doctor
        ↓
Phase 10
Mock E2E
        ↓
Phase 11
Discord E2E
        ↓
Phase 12
旧PrototypeをArchive
```

**旧Prototypeは統合成功が確認されるまで削除しません。**

---

# 10. Acceptance Criteria

統合完了を「ファイルを1つにまとめた」とは定義しません。

以下を**すべてPASSした時点で統合完了**とします。

### Architecture

- 5 Layerが明確に分離されている
- 共通機構の重複コピーがない
- Canonical PMPが1つ
- Canonical Configurationが1つ
- Canonical Storageが1つ
- Discord Appが1つ

### RD

- 31 RDをロード可能
- STRICT mode PASS
- Agent → RDの対応が維持されている
- Agent実行時にRDが実際に参照される

### Runtime

```text
py main.py --doctor
```

PASS。

### E2E

```text
Producer
→ Researcher
→ Deliberation
→ Conclusion
→ Human Selection
→ Playwright
→ Deliveries
```

Mockで完走。

### Persistence

Bot再起動後も既存Workflowを復元可能。

### Discord

各LayerのChannel RoutingとChannel Guardが正常。

### Deliveries

成果物が、

```text
PRDCP_v2/storage/data/deliveries/<workflow_id>/
```

へ固定保存される。

---

## 最終方針

今回のMigrationでは、

> **「ファイル数を最小化する」**

こと自体を目標にはしません。

目標は、

> **1つの責務に対してCanonicalな実装を1つだけ持つ**

ことです。

その結果として、現在5 Prototypeに重複しているファイルが大幅に削減されます。

この **PRDCP v2 Migration Map v0.1** を基準に、次は実ファイルを変更せずに**5 Prototypeの差分監査**を行えば、安全に統合作業へ移れます。