はい。これまで決めてきた方針を踏まえると、PRDCP v2のファイル配置は「何でも共通化する」のではなく、**共通基盤は1か所、Layer固有物はLayerごと、実行時データは共通Storage、説明は各責務のREADME**という形が最も管理しやすいです。

# PRDCP v2 ファイル配置計画 v0.1

## 1. 配置の基本原則

PRDCP v2では、次の4種類を明確に分離します。

| 種類 | 方針 |
|---|---|
| 全Layer共通機構 | 1つだけ配置 |
| Layer固有実装 | Producer等の各フォルダに分離 |
| Agent RD | `role_definitions/<layer>/` に分離 |
| 実行時データ | `storage/data/` に集約 |

特に重要なのは、**「共通」と「固有」を物理的なフォルダ構造でも表現すること**です。

---

# 2. 最終ディレクトリ構成

```text
PRDCP_v2/
│
├─ main.py
├─ runtime.py
│
├─ README.md
├─ CHANGELOG.md
├─ FINAL_AUDIT.md
├─ VERSION
│
├─ .env
├─ .env.example
├─ .gitignore
├─ requirements.txt
├─ pyproject.toml
│
├─ .github/
│
├─ cli_app/
│  ├─ README.md
│  ├─ __init__.py
│  ├─ arguments.py
│  ├─ commands.py
│  ├─ diagnostics.py
│  ├─ events.py
│  └─ output.py
│
├─ config/
│  ├─ README.md
│  ├─ settings.py
│  └─ models.json
│
├─ common/
│  ├─ README.md
│  │
│  ├─ models/
│  ├─ role_definitions/
│  ├─ specifications/
│  ├─ logger.py
│  └─ ...
│
├─ specifications/
│  ├─ README.md
│  │
│  ├─ common/
│  │  ├─ PMP関連
│  │  ├─ agent_registry.json
│  │  ├─ rd_registry.json
│  │  ├─ model_registry.json
│  │  ├─ message_types.json
│  │  └─ ...
│  │
│  └─ schemas/
│
├─ role_definitions/
│  ├─ README.md
│  ├─ registry.json
│  │
│  ├─ producer/
│  │  ├─ README.md
│  │  └─ *.json
│  │
│  ├─ researcher/
│  │  ├─ README.md
│  │  └─ *.json
│  │
│  ├─ deliberation/
│  │  ├─ README.md
│  │  └─ *.json
│  │
│  ├─ conclusion/
│  │  ├─ README.md
│  │  └─ *.json
│  │
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
│  ├─ schemas/
│  └─ agents/
│
├─ researcher/
│  ├─ README.md
│  ├─ manager.py
│  ├─ registry.py
│  ├─ workflow.py
│  ├─ state.py
│  ├─ schemas/
│  └─ agents/
│
├─ deliberation/
│  ├─ README.md
│  ├─ manager.py
│  ├─ registry.py
│  ├─ workflow.py
│  ├─ state.py
│  ├─ schemas/
│  └─ agents/
│
├─ conclusion/
│  ├─ README.md
│  ├─ manager.py
│  ├─ registry.py
│  ├─ workflow.py
│  ├─ state.py
│  ├─ schemas/
│  └─ agents/
│
├─ playwright/
│  ├─ README.md
│  ├─ manager.py
│  ├─ registry.py
│  ├─ workflow.py
│  ├─ state.py
│  ├─ schemas/
│  └─ agents/
│
├─ providers/
│  ├─ README.md
│  ├─ __init__.py
│  ├─ mock.py
│  └─ openrouter.py
│
├─ discord_app/
│  ├─ README.md
│  ├─ __init__.py
│  ├─ bot.py
│  ├─ commands.py
│  ├─ channel_router.py
│  ├─ message_formatter.py
│  └─ views.py
│
├─ storage/
│  ├─ README.md
│  ├─ repositories.py
│  ├─ json_repository.py
│  └─ data/
│     │
│     ├─ workflows/
│     │  ├─ producer/
│     │  ├─ researcher/
│     │  ├─ deliberation/
│     │  ├─ conclusion/
│     │  └─ playwright/
│     │
│     ├─ deliveries/
│     └─ logs/
│
├─ tests/
│  ├─ README.md
│  ├─ common/
│  ├─ producer/
│  ├─ researcher/
│  ├─ deliberation/
│  ├─ conclusion/
│  ├─ playwright/
│  ├─ discord/
│  └─ e2e/
│
└─ docs/
   ├─ README.md
   ├─ architecture/
   ├─ protocols/
   ├─ operations/
   ├─ migration/
   └─ audit/
```

実際の既存ファイル名は差分監査後に合わせます。**統合のためだけに不要なファイルやフォルダを増やすことはしません。**

---

# 3. Rootに置くもの

ルートは「PRDCPを起動・理解する入口」に限定します。

```text
main.py
runtime.py
README.md
.env
.env.example
requirements.txt
pyproject.toml
VERSION
CHANGELOG.md
FINAL_AUDIT.md
```

ここを見れば、

> 「どう起動するのか」  
> 「どのバージョンなのか」  
> 「設定はどこなのか」

が分かる状態にします。

### `main.py`

唯一の実行Entry Point。

```powershell
py main.py
py main.py --doctor
py main.py --demo-e2e
```

### `runtime.py`

5 LayerとProvider、RDLoader、Repositoryの組み立てだけを担当。

ここにAgentロジックは入れません。

---

# 4. `config/` — 共通設定

```text
config/
├─ README.md
├─ settings.py
└─ models.json
```

ここは**Runtime Configuration専用**です。

現在5 Prototypeにある、

```text
settings.py × 5
models.json × 5
```

は1つにします。

`.env`の値を読み取るのもここだけです。

---

# 5. `specifications/` — PRDCPの共通契約

ここはかなり重要です。

```text
specifications/
├─ README.md
└─ common/
   ├─ PMP
   ├─ Agent Registry
   ├─ RD Registry
   ├─ Message Type Registry
   └─ Metadata Status Registry
```

Producerなどは**自分専用のPMPを持たない**ようにします。

```text
Producer ──────┐
Researcher ────┤
Deliberation ──┼─→ specifications/common/
Conclusion ────┤
Playwright ────┘
```

これがCanonical Specificationです。

Doctorが現在確認している共通契約もここを基準にします。

---

# 6. `common/` — 共通実装

`specifications`との違いを明確にします。

```text
specifications/
→ 「何が正しいか」を定義

common/
→ その共通仕様を実際に扱うコード
```

例えば、

```text
common/
├─ role_definitions/
│  └─ loader.py
├─ models/
├─ logger.py
└─ ...
```

です。

PMP SchemaそのものはSpecification。

PMPをValidationするPythonコードはCommon。

という分離です。

---

# 7. `role_definitions/` — 31 AgentのRD

これは統合して消す場所ではありません。

むしろ**Layer単位で整理して明確に残すべき場所**です。

```text
role_definitions/
├─ registry.json
│
├─ producer/
├─ researcher/
├─ deliberation/
├─ conclusion/
└─ playwright/
```

例えばResearcherなら、

```text
role_definitions/researcher/
├─ README.md
├─ research_manager.json
├─ academic_researcher.json
├─ government_researcher.json
├─ news_researcher.json
├─ public_opinion_researcher.json
├─ politician_researcher.json
├─ industry_researcher.json
└─ quality_reviewer.json
```

となります。

---

# 8. RD参照ルート

統合後もここは絶対に崩しません。

```text
Agent起動
   ↓
Layer Registry
   ↓
RDLoader
   ↓
role_definitions/registry.json
   ↓
role_definitions/<layer>/<agent>.json
   ↓
Agent Prompt / Constraints
   ↓
実行
```

つまり、

**RDが「資料として置いてあるだけ」にならないこと**

をAcceptance Criteriaにします。

現在の

```env
PRDCP_RD_STRICT=true
```

も維持します。

---

# 9. 5 Layer

ここは統合して1フォルダにはしません。

```text
producer/
researcher/
deliberation/
conclusion/
playwright/
```

の5区画を明確に残します。

各Layer内の基本構造をできるだけ統一します。

```text
<layer>/
├─ README.md
├─ manager.py
├─ registry.py
├─ workflow.py
├─ state.py
├─ schemas/
└─ agents/
```

これによって、例えばResearcherからConclusionを見たときにも、

> 「Managerはここ」  
> 「Workflowはここ」  
> 「Registryはここ」

とすぐ分かります。

これは今まで重視してきた**外見上の一貫性**にもつながります。 

---

# 10. Agent実装とRDは分離する

例えば、

```text
researcher/
└─ agents/
   └─ academic.py
```

と、

```text
role_definitions/
└─ researcher/
   └─ academic_researcher.json
```

は別にします。

理由は、

```text
Python
→ How

RD
→ What / Why / Constraints
```

だからです。

ただしREADMEには必ず対応関係を書きます。

```text
Academic Researcher
Implementation:
researcher/agents/academic.py

Role Definition:
role_definitions/researcher/academic_researcher.json
```

とします。

---

# 11. `discord_app/` — Discord Control Plane

5コピーは廃止します。

```text
discord_app/
├─ README.md
├─ bot.py
├─ commands.py
├─ channel_router.py
├─ message_formatter.py
└─ views.py
```

のみ。

現在実機確認できている、

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

へのRoutingもここだけが担当します。

### `channel_router.py`

全Layer共通。

```text
producer     → #producer
researcher   → #researcher
...
sources      → #sources
deliveries   → #deliveries
status       → #workflow-status
```

---

# 12. `storage/` — 唯一の永続化領域

ここが今回の統合でかなり重要です。

現在は、

```text
Producer Prototype/
└─ storage/data/
```

が事実上全体Storageになっています。

統合後は、

```text
PRDCP_v2/
└─ storage/
   └─ data/
```

が**唯一のCanonical Storage**になります。

---

## 13. Workflow保存

```text
storage/data/workflows/
├─ producer/
├─ researcher/
├─ deliberation/
├─ conclusion/
└─ playwright/
```

同じWorkflow IDを横断して使用します。

例えば、

```text
3c70d168-3482-4e49-bf94-5092ec80df61
```

なら、

```text
workflows/producer/3c70....json
workflows/researcher/3c70....json
workflows/deliberation/3c70....json
workflows/conclusion/3c70....json
workflows/playwright/3c70....json
```

という対応です。

---

# 14. Deliveries

最終成果物は必ず、

```text
storage/data/deliveries/<workflow_id>/
```

です。

例えば、

```text
deliveries/
└─ 3c70d168-.../
   ├─ final_script_package.json
   ├─ script.md
   ├─ source_list.md
   ├─ citation_manifest.json
   ├─ visual_plan.md
   └─ production_notes.md
```

これで、

> 「成果物どこ？」

に対して、

> `storage/data/deliveries/<workflow_id>/`

と必ず答えられます。

---

# 15. Logs

```text
storage/data/logs/
```

へ統一。

例えば、

```text
runtime_events.jsonl
rd_access.jsonl
```

などです。

RD access logがここにあることで、

**どのRDが実際に参照されたか**

も監査できます。

---

# 16. `providers/`

ここも全Layer共通です。

```text
providers/
├─ README.md
├─ mock.py
└─ openrouter.py
```

ProducerだけOpenRouter Providerを持つ、といった構造は禁止。

全Agentが共通Provider Interfaceを通します。

---

# 17. `cli_app/`

CLIも1つです。

```text
cli_app/
├─ arguments.py
├─ commands.py
├─ diagnostics.py
├─ events.py
└─ output.py
```

現在の、

```text
--doctor
--demo
--demo-full
--demo-e2e
--researcher
--deliberation
--conclusion
--playwright
```

を維持します。

---

# 18. `tests/`

統合後はテストも整理します。

```text
tests/
├─ common/
├─ producer/
├─ researcher/
├─ deliberation/
├─ conclusion/
├─ playwright/
├─ discord/
└─ e2e/
```

例えば今回作った、

```text
test_channel_router.py
```

は、

```text
tests/discord/test_channel_router.py
```

へ配置するのが自然です。

---

# 19. `docs/`

READMEに全部書くと逆に読みにくくなるので、詳細資料を分離します。

```text
docs/
├─ architecture/
├─ protocols/
├─ operations/
├─ migration/
└─ audit/
```

例えば今回作っている、

```text
PRDCP v2 Migration Map
PRDCP v2 ファイル配置計画
```

は、

```text
docs/migration/
```

へ置くのが適切です。

---

# 20. README配置ルール

READMEは「全フォルダ」ではなく、**独立した責務を持つ単位**に置きます。

最低限、

```text
/README.md

/config/README.md
/common/README.md
/specifications/README.md
/role_definitions/README.md

/producer/README.md
/researcher/README.md
/deliberation/README.md
/conclusion/README.md
/playwright/README.md

/providers/README.md
/discord_app/README.md
/storage/README.md
/tests/README.md
/docs/README.md
```

です。

---

# 21. Layer READMEの共通テンプレート

5 LayerのREADMEは外見も統一します。

```text
# Layer Name

## 1. Purpose

## 2. Responsibilities

## 3. Agent Structure

## 4. Workflow

## 5. Inputs

## 6. Outputs

## 7. Role Definitions

## 8. PMP Interfaces

## 9. Storage

## 10. Discord Operations

## 11. Main Files

## 12. Testing
```

こうすると審査員にも、

> ProducerとResearcherでREADME構造まで統一されている

と見せられます。

---

# 22. `__pycache__`

これはPRDCPの構成物として管理しません。

```text
__pycache__/
*.pyc
```

はPythonが自動生成するため、

```gitignore
__pycache__/
*.py[cod]
```

で除外します。

Prototypeから統合する際にもコピー対象外です。

---

# 23. 統合対象 / 非統合対象

最終的にはこう整理できます。

### 1つにする

```text
main.py
runtime.py
.env
.env.example

config/
common/
specifications/
providers/
discord_app/
cli_app/
storage/

PMP
RDLoader
Common Registry
Logger
Provider
Doctor
```

### Layer別に残す

```text
producer/
researcher/
deliberation/
conclusion/
playwright/

各Layer Agent
Manager
Workflow
State
Schema
Registry

各Agent RD
```

---

# 24. ファイル数削減の考え方

今回削減するのは**意味のあるファイルではなく、コピー**です。

例えば現状、

```text
bot.py × 5
runtime.py × 5
settings.py × 5
models.json × 5
channel_router.py × 5
PMP × 5
RDLoader × 5
json_repository.py × 5
```

なら、

```text
bot.py × 1
runtime.py × 1
settings.py × 1
models.json × 1
channel_router.py × 1
PMP × 1
RDLoader × 1
json_repository.py × 1
```

にします。

一方、

```text
Academic Researcher RD
Decision Evaluator RD
Scriptwriter RD
```

のような**意味のある固有ファイルは削りません。**

---

# 25. 配置判断ルール

統合時に「このファイルどこ？」となった場合は、次のルールを使います。

```text
全Layerが使う？
    YES
     ↓
    共通領域

    NO
     ↓
特定Layerだけ？
    YES
     ↓
    <layer>/

Agentの役割定義？
    ↓
role_definitions/<layer>/

実行時生成物？
    ↓
storage/data/

人間向け詳細資料？
    ↓
docs/

その責務の入口説明？
    ↓
README.md
```

---

# 最終的な狙い

この配置にすると、PRDCP v2は、

```text
コードを見る
→ Layerごとの実装場所が分かる

READMEを見る
→ 各フォルダの意味が分かる

RDを見る
→ Agentの役割が分かる

Discordを見る
→ 実行状況が分かる

workflow-statusを見る
→ どこまで進んだか分かる

sourcesを見る
→ 根拠が分かる

deliveriesを見る
→ 成果物が分かる

storageを見る
→ 永続化データの場所が分かる
```

という状態になります。

したがって、今回の統合は単なる**「5 Prototypeを1フォルダに詰める作業」ではなく、「PRDCP v2のCanonicalな管理構造を確立する作業」**として扱うべきです。

この**ファイル配置計画 → Migration Map**を統合作業時の基準にすれば、実際の数百ファイルを機械的に分類できるようになります。