# PRDCP v2

Inter-layer readiness values are canonical lowercase contracts. Deliberation -> Conclusion uses `ready`, `ready_with_conditions`, `not_ready`, or `undetermined`; Conclusion -> Playwright uses `ready`, `ready_with_conditions`, `not_ready`, or `not_applicable`. Legacy uppercase checkpoint values are normalized only while reading and existing storage files are not rewritten.
## Quick Concept

PRDCPは、複数のAIエージェントを5つの層に分け、社会で広く共有されている一般論を検証するマルチエージェントシステムです。

1回あたり数百円程度、場合によっては1日かけて調査・分析・審議を行い、世間で広く共有されている見解が「なぜそう考えられているのか」を紐解きます。

## Background

私は普段から、Opper.aiのAI RoundtableやGoogle AI Studioなどを使い、気になったニュースやネット上の意見など、社会で一般的に受け入れられている知見を検証していました。

しかし、さまざまな意見を集めることはできても、「なぜその見解に至ったのか」まで調査し、多様な情報源から比較・分析するには、大きな作業量が必要でした。

そこで、一般論の発見から情報収集、審議、結論、動画台本の作成までを、複数のAIエージェントで分担・自動化するシステム「PRDCP」を開発しました。

また、今回はLangGraphやLangChainを使用していません。これは、この作品を通してMASのオーケストレーション構造そのものを体系的に学ぶことを目的としていたためです。

その結果、エージェント間の通信を安定させるPMPという共通プロトコルの設計や、各層にオーケストレーターとなるManager Agentを配置し、処理の進行・検証・再実行を管理することで、システム全体の動作を安定化させる設計を学ぶことができました。


## Execution Modes

Provider（LLM backend）、Retrieval Provider（検索）、Demo Safe Mode（自動的な追加呼び出し、retry、revisionの許可範囲）は独立した設定です。CLI指定は、その実行に限って `.env` / 環境変数より優先されます。ただし、`--provider`が上書きするのはLLM Providerだけで、`PRDCP_RETRIEVAL_PROVIDER`は変更しません。

```powershell
# 完全Mockにする場合はRetrievalも明示的にMockへ固定する
$env:PRDCP_RETRIEVAL_PROVIDER = "mock"

# 完全Mock E2E（revision loopを許可）
py main.py --demo-e2e --provider mock --no-safe-mode

# 安全なMock検証
py main.py --demo-e2e --provider mock --safe-mode

# 実APIの単発検証（追加呼び出しを抑止）
py main.py --deliberation-recover <WORKFLOW_ID> --provider openrouter --safe-mode

# 応答途中切断後の一回限り明示retry
py main.py --deliberation-provider-retry <WORKFLOW_ID> --provider openrouter --safe-mode

# 実運用（自動revisionと追加呼び出しを許可）
py main.py --deliberation-recover <WORKFLOW_ID> --provider openrouter --no-safe-mode
```

`--provider`、`--safe-mode`、`--no-safe-mode` を省略した場合は、従来どおり `PRDCP_PROVIDER` と `PRDCP_DEMO_SAFE_MODE` が使われます。Retrievalは`PRDCP_RETRIEVAL_PROVIDER`を使用し、未設定時だけ起動時の`PRDCP_PROVIDER`を継承します。したがって、`.env`でRetrievalを`openrouter`に固定した後にCLIだけを`--provider mock`へ変えても、検索はMockになりません。API 0件の検証では両方を`mock`にしてください。`--safe-mode` と `--no-safe-mode` は同時指定できません。`--doctor` は現在の実効設定を表示し、OpenRouterかつSafe Mode OFFの場合は警告します。OpenRouter利用時は公開Model/Endpoint metadataも読み取り、31 Agentの実効model（環境変数と検証済み互換bindingを反映）が`response_format`と`structured_outputs`を同じ稼働Endpointで扱えるかを監査します。この確認はchat completionを生成しないためAPI生成料金は発生しません。model不存在、非対応、alias解決失敗、metadata取得不能はPASSにせず、`MODEL CAPABILITY PREFLIGHT`をBLOCKEDにします。

Cycle 029では、General Opinion Analystと7 Researcher specialistの検索をStructured Reasoningから分離しました。Canonical PMP payloadは変更せず、Search ProviderのURL・title・excerptを`storage/data/retrieval_contexts/<workflow_id>/`へ先に保存し、そのruntime viewだけをReasoning modelへ追加します。検索予約は`retrieval_call_reservations`、LLM予約は従来の`provider_call_reservations`へ分離され、LLM失敗後のRecoveryは保存済みRetrievalを再利用します。Research Resultのsource ID・URL・title・excerptはRetrieval集合へschemaとlocal contractの両方で束縛されます。Research PlannerにはRetrievalを付与しません。`--doctor`は31 Reasoning modelと8 Retrieval-required Agentを別々に監査します。

Cycle 030では、Geminiのcontrolled-generation compilerへretrieved title、URL、excerptを動的enumとして渡しません。General Opinion Analystと7 Researcher specialistの全8 Retrieval Agentは、Structured Output Schemaでは短い`source_id`だけをenum拘束します。title、URL、retrieved_atはLLMに再転記させず、生成後に`source_id`から保存済みRetrieval Contextの値を決定論的に復元します。excerptと分析metadataはlocal contractで保存済みRetrievalへ照合します。Gemini向け最終SchemaはHTTP送信とProvider reservationの前に、Schema全体のbyte数とenum literal/総量を検査します。失敗したProducer checkpointは検索を再実行せず、`py main.py --producer-provider-retry <WORKFLOW_ID> --provider openrouter --safe-mode`で明示的一回だけReasoningを再送できます。通常の未予約checkpointは`--producer-recover`で再開します。

Cycle 031では、Cycle 030の一回retryがProvider応答後の旧metadata照合で停止した場合に限り、`py main.py --producer-output-repair <WORKFLOW_ID> --provider openrouter --safe-mode`を使用します。この経路は、消費済みretry authorization、元・retry双方のProvider reservation、PMP error、保存済みRetrieval ContextのSHA-256を照合して、新しい`_provider_output_repair_1` task identityを一度だけ発行します。Retrieval、Topic Scout、Topic Selectorは実行せず、成功時もGeneral Opinion checkpointを保存した時点でResearch Plannerの前に停止します。

### Deliberationの複合Revision

Quality ReviewerがResearcher追加調査とDeliberation内部Revisionを同時に要求した場合は、内部Agentを先に再実行しません。内部対象とfindingをpending計画として保存し、Researcher更新を待って `--deliberation-resume <WORKFLOW_ID>` で再開します。再開後は、保存済み対象と依存関係に基づいて必要なPrimary分析、Manager統合、Counterargument、Final統合、決定論的検証、Quality Reviewだけを順に再計算します。

`--deliberation-recover` は通信・Provider・Schema・プロセス中断などの技術障害用です。正常なResearcher return後の継続には `--deliberation-resume` を使用し、両者を混同しません。Demo Safe ModeでもResearcher追加調査のPMP／Outbox、pending internal revision、`WAITING_UPSTREAM_REVISION`への状態遷移は保存しますが、Researcher AgentやDeliberation Agentは自動実行しません。Internal-only RevisionはAgent再実行前に停止します。

OpenRouter応答が`IncompleteRead`などで途中切断された場合は、Provider側で生成・課金まで完了した可能性があるため自動retryしません。Stateに保存されたRetryable failure、元task、元reservationを照合したうえで、`py main.py --deliberation-provider-retry <WORKFLOW_ID> --provider openrouter --safe-mode`を一回だけ使用できます。元reservationは削除せず、`_operator_retry_1` task、承認台帳、retry reservationを別々に保存します。Manager統合とDeliberation Quality Reviewerのどちらで停止しても同じ規則を使い、再送の再送は拒否します。Quality Review応答がPMPへ保存済みでState反映だけ失敗した場合は、artifact identity一致を確認して応答を再利用し、Providerを呼びません。

OpenRouterのStructured Output応答は、Provider境界でroot object、標準JSON数値、再帰的な有限数を検査します。`Infinity`、`NaN`、指数overflow、array/scalar rootはPydanticへ渡しません。応答本文は保存せず、hash・長さ・root型・不正pathだけをError PMPへ記録します。Conclusionでこの種の失敗または課金済みの可能性がある通信失敗から再開する場合は、`--conclusion-provider-retry`をDemo Safe Modeで一回だけ使用します。通常の`--conclusion-recover`は未消費の明示認可なしにProviderを再送しません。

すべてのOpenRouter Structured Output要求は `provider.require_parameters=true` を送信し、`response_format` を実装しないEndpointへのroutingを禁止します。有料chat completionの予約・送信前にも同じ公開Endpoint metadataを検査します。必要能力を持つ稼働Endpointがない場合は`MODEL_CAPABILITY_ERROR`として停止し、reservationとchat completionを作成しません。`require_parameters=true`、`json_schema.strict=true`、`response_format.type=json_schema`を弱めるfallbackはありません。`~vendor/...-latest`形式のaliasはcatalogの`alias_target`をたどって実体Endpointを検査します。元のConclusion taskと一回限りのoperator retryがどちらも `ProviderResponseContractError` になった場合だけ、`py main.py --conclusion-contract-repair <WORKFLOW_ID> <REPAIR_MODEL_ID> --provider openrouter --safe-mode` を使用できます。この操作は、元モデルと異なる明示モデル、`_provider_contract_repair_1` の決定論的task ID、専用のPENDING/CONSUMED認可、独立reservationを使う一回限りの契約修復です。同じtaskの再々送ではなく、正常なupstream checkpointを再利用します。

異なるmodelによるcontract repair結果がPydantic出力検証まで成功した場合、その事実を`storage/data/provider_model_compatibility/`へappend-onlyの互換性bindingとして保存します。bindingはprovider、Agent、output schema、元の非互換modelの完全一致時だけ将来の新しいlogical taskへ適用されます。明示model指定はbindingより優先され、環境設定のmodelが変更された場合は古いbindingを適用しません。既存workflowの旧repair成功は、保存済みPMP result、CONSUMED authorization、reservationを照合して復元します。`--doctor`は有効なbindingを表示します。

Conclusion recoveryはPosition、Evaluation、Integration、Quality Reviewの保存済みResult PMPを論理taskと依存artifactで照合します。Result保存後のcheckpoint更新だけが失敗していた場合は、そのResultを復元してProvider call 0で続行します。未応答requestが残る場合は、Provider実行有無を推測せずfail closedします。

Decision EvaluatorのStructured Outputは、最大5候補×14基準の`candidate_evaluations`を上限70件に制限します。同一candidate/criterionの完全一致反復は情報損失なしに1件へ正規化しますが、ratingや根拠が異なる重複、評価表との不一致、未知候補IDは拒否します。条件付き優位性と感度分析は複数候補IDを配列で保持し、ID連結を禁止します。課金済みProvider応答がローカルSchema検証で拒否された場合、invalid payloadとvalidation errorがPMPへ保存され、元reservationと相関できるときだけ`--conclusion-provider-retry`で一回再送できます。

Conclusion Quality Reviewの判定とroutingは排他的です。`approved` / `approved_with_conditions`は修正経路を持たず、後者は成果物変更不要の開示条件だけを`limitations_to_disclose`へ記録します。`revision_required`はConclusion内部修正か`deliberation_return`のどちらか一方だけを選び、`blocked`は審査または修正経路を確定できない場合に限定します。内部revision targetはConclusion層の有限Agent集合へSchemaで制限され、上流requestやblocking IDは同じReview内のfindingへ必ず追跡されます。

Demo Safe ModeでConclusion内部の`revision_required`が保存されて停止した場合は、`py main.py --conclusion-revise <WORKFLOW_ID> --provider openrouter --safe-mode`で、保存済みtargetから依存する工程を一サイクルだけ明示的に再実行できます。完了済みの非依存checkpointは再利用し、revision計画とcheckpoint無効化を最初のProvider呼び出し前に保存します。再Reviewが再び`revision_required`なら次サイクルへ自動進行せず、再度`BLOCKED`で停止します。viableな非primary候補がConclusion Packageのalternativesから脱落したManager所有artifactだけは例外的に、通常のrevision countと上限を変更せず、同じrevision epochで一回だけ保存済みEvaluation/Integrationから決定論的に再構築できます。この補修は計画、追加候補ID、専用Quality Review taskをProvider呼び出し前に保存し、Position Generator、Decision Evaluator、Decision Integratorを再実行しません。Position GeneratorのTraceability IDはDecision Contextの`key_claim_ids`、`analysis_ids`、`evidence_ids`だけをcanonical allowlistとし、full Deliberation payload内の他artifactからIDを採用しません。

ConclusionのPosition / Evaluation / Integration Structured Outputは、JSON Schema検証後かつ成功PMP保存前に、candidate/problem/stakeholder/claim/evidence/analysis/sourceの構造化参照を入力canonical集合と照合します。自由文は解釈・変更しません。Deliberation Result原本を変更せず、Agentへ渡すDecision Context viewでは明示IDフィールドの未知参照だけを除外し、説明文を保持します。旧checkpointのtrace-only findingを明示revisionする場合は、保存成果物を再検証し、未知参照を最初に導入したAgentからだけ依存閉包を再実行します。Conclusion Package Validatorは、Integratorのcandidate comparisonが全候補を一度ずつ覆うこと、recommended optionが一意なviable候補であること、全viable非primary候補が理由と適用条件付きでalternativesへ存在することを検査し、件数metricsを実体と同時に算出します。Safe Modeは各CLIで一サイクルだけ許可し、設定されたrevision slotを使い切った後はProvider呼び出し前に停止します。

OpenRouterへ送るConclusionのstrict schemaは、各requestに含まれるcandidate/problem/stakeholder/claim/evidence/analysis/source IDをenumとして参照フィールドへ埋め込み、Provider側でも未知IDや説明文をID欄へ生成できないようにします。Decision Integratorのsingle/conditional selectionは1候補IDを正式に許し、複数案を実際に統合する場合だけ複数IDを返します。課金済み応答がlocal reference validationだけで停止した場合、保存原文から未知の参照「配列要素」だけを除外して全契約を再検証できれば、監査記録と復元Result PMPを追加してProvider call 0でcheckpointへ昇格します。未知scalar、自由文変更、必須件数不足、Reservation不一致は自動修復しません。

Deliberation Recoveryは、Causal item IDを役割別canonical namespaceへ読込時変換し、既存checkpoint JSON自体は書き換えません。Manager Structured Outputがlocal validationで拒否された場合はraw payloadをstateへ保存し、次回RecoveryではProviderを呼ぶ前に再検証します。rawが保存されていない旧Failureだけは、決定的な`contract_repair_1` logical taskを一回だけ発行します。

Initial IntegrationにはIntegrationChange artifactが存在しないため、traceabilityの`integration_change_ids`は常に空です。Final Integrationでは、同じpayloadの`integration_changes.change_id`として宣言された`change_*`だけを参照できます。保存済みManager rawに旧dangling参照だけが残る場合は、監査記録を付けて読込時に除去し、Providerを再呼出ししません。

Final Integrationのtraceabilityでは、Steelmanの`challenge_ids`とRevision対象になる`counterargument_ids`を別の型付き配列で保持します。新規出力での混在はSchemaと成果物間照合の両方で拒否します。旧checkpointの`counterargument_ids`に、保存済みCounterargument成果物のSteelman Challengeと完全一致するIDがある場合だけ、Recovery時に`challenge_ids`へ損失なく移送して監査記録を残します。この互換修復ではPrimary Analysis、Initial Integration、Counterargument、Final Integrationを再実行せず、下流の決定論的検証とQuality Reviewだけを更新します。

Deliberationが発行した追加調査要求は `--researcher-resume <WORKFLOW_ID>` で処理します。Researcherは要求されたResearch Questionと、既存Research Planで許可済みのsource categoryに対応するAgentだけを実行し、旧Evidenceを保持したReportを再統合・Quality Reviewした後、`research_revision_result`を返します。Demo Safe Modeでもこの明示コマンド1回分は実行しますが、Researcher Quality Reviewerがさらにrevisionを要求した場合の自動再dispatchとDeliberationの自動再開は行いません。

Researcher外部RevisionのQuality Reviewerが通信切断などの `RetryableAgentError` で停止した場合に限り、`py main.py --researcher-provider-retry <WORKFLOW_ID> --provider openrouter --safe-mode` で一回だけ明示的に再送できます。元のProvider reservationは削除せず、新しい決定的task ID、承認記録、再送reservationを別々に保存します。承認はProvider呼び出し直前に消費され、同じ承認の再利用、二回目の再送、`PayloadValidationError`など非一時障害の再送、Safe Mode OFFでの利用は拒否されます。保存済みResearch Task、結果、Reportは再利用され、Quality Reviewer以外は再実行されません。

Researcher Quality Review完了後は、判定にかかわらず`WAITING_HUMAN_EVIDENCE_REVIEW`で停止します。`--researcher-evidence`でEvidence finding、hard integrity failure、決定論的repairを確認し、`--researcher-accept`、`--researcher-accept-limitations`、`--researcher-revise`のいずれかを`--reason`付きで明示します。`ACCEPT`は未解決Evidence findingがない場合だけ、`ACCEPT_WITH_LIMITATIONS`は全findingを未解決gapとして下流へ開示する場合だけ許可します。受容されたgapはEvidenceや事実確認にはなりません。`REVISE`は追加調査計画を0-callで保存して停止し、Provider実行には別の明示authorizationが必要です。Schema、PMP、provenance等のhard integrity failureは人間判断で上書きできません。保存済みReportにsource-level limitationの完全一致コピー、または認識済み報道媒体がidentityなしのEXPERTとして残る機械的矛盾がある場合、`--researcher-recover`は元Quality Reviewを変更せず、Provider/Retrieval 0件でexact dedupe・限定的なcategory repair・coverage再計算を行い、前後hash付きrepair artifactを保存します。意味的dedupe、URL・本文・trace ID変更、別Research Questionへのcoverage流用は行いません。Human DecisionはQuality Review単位のcreate-once artifactとして先に保存され、StateまたはOutbox保存障害後も`--researcher-recover`で同じ決定を再利用します。

ResearcherのStructured Outputは共通の22 root schema構成を保ちつつ、各Research Taskの `research_target` に応じて送信直前に `agent_id` と `source_type` を一カテゴリへ限定します。ローカルvalidationも同じ対応を検証します。異なるProvider namespaceの保存済みResultはJSONを削除せずReport統合対象から除外し、旧データに担当外カテゴリが混在する場合も担当カテゴリのSourceだけを読込時互換で採用します。OpenRouter等の非Mock providerでは `example.invalid`、Mock、架空識別子のplaceholder sourceを拒否します。

外部RevisionがReport再統合などの途中で停止した場合、同じ `--researcher-resume` は保存済みResearch Taskと`agent_results`を照合します。完了済みAgentは再度Providerへ送らず、最後の未完了checkpoint（結果収集、Report統合、Quality Review、返信確定）から再開します。Researcher Quality Reviewの論理task IDは初回の`research_quality_review_initial`、内部Revisionの`research_quality_review_internal_<iteration>`、外部Revisionの`research_quality_review_external_<iteration>`に分離され、同じ処理の再開時だけ同じIDを再利用します。Reviewer応答が保存済みならProviderを再呼出しせず、`research_revision_result`がOutboxへ保存済みなら返信を再生成・再上書きせず完了stateを復元します。同一Sourceを複数カテゴリが返した場合も、代表Sourceの`source_type`と型固有metadataは混在させず、共通範囲と`merged_evidence_ids`だけを統合します。

Cycle 032では、Discord起動時に構築された31 AgentのRuntime Modelと、現在の`.env`から再読込したConfigured ModelをProvider予約前に比較します。不一致は`RUNTIME_MODEL_DRIFT`として0-callで停止し、Discordの`!runtime_models [layer]`で確認できます。verified compatibility bindingはConfigured／Runtimeとは別のResolved Modelとして表示し、正当なConclusion／Playwright bindingをDriftと誤判定しません。旧Runtime Modelのendpoint 404で初期Researcher Taskが失敗した場合は、`py main.py --researcher-runtime-model-repair <WORKFLOW_ID> --provider openrouter --safe-mode`を使用できます。このRecoveryは保存済みRetrieval Contextとhashが揃う未完了Taskだけを`<task_id>_runtime_model_repair_1`で一度実行し、検索は再実行しません。Retrieval ContextがないTaskは自動検索せずfail closedとなります。

Cycle 033では、そのfail closed状態に対してオペレーターが新規検索を明示承認する場合だけ、`py main.py --researcher-retrieval-reconstruct <WORKFLOW_ID> --provider openrouter --safe-mode`を使用できます。各検索は`<task_id>_retrieval_reconstruction_1`という新しいidentity、PENDING→CONSUMEDの一回限りAuthorization、検索前Reservation、保存ContextのSHA-256を持ち、旧Task／旧Provider reservationは変更しません。検索が失敗したAuthorizationは再利用できず、Context保存後にプロセスが停止した場合は保存済みContextを再利用して検索0回で再開します。Reasoningは元のResearch Task IDへ結果を戻しつつ、対応する再構築Contextだけを参照します。

Cycle 034では、保存済みRetrievalを使ったRuntime Model Repairが`relevant_excerpt`のLLM再転記差異で停止した場合に限り、`py main.py --researcher-runtime-output-repair <WORKFLOW_ID> --provider openrouter --safe-mode`を使用できます。ResearcherのStrict Structured Outputでは引用文字列を生成させず、選択された`source_id`に対応する保存済み原文から最大1,000文字を決定論的に復元します。Recoveryは消費済みRuntime Repair authorization／reservation、相関したERROR PMP、Retrieval IDとSHA-256、正確なfailure signatureを要求し、`<task_id>_runtime_model_output_repair_1`を一度だけ実行します。成功後は未実行Taskだけを既存Cycle 032経路で継続し、検索済みContextを再利用します。

Cycle 035では、続くsource identity検証でレイアウト空白やURLから決定可能な国名を誤って未裏付けと判定した場合に限り、`py main.py --researcher-runtime-adapter-repair <WORKFLOW_ID> --provider openrouter --safe-mode`を使用できます。人名・組織名等はUnicode NFKCと空白・句読点非依存で保存原文へ照合し、政府sourceのcountryは原文または保存URLの国別domainで検証します。study/article/statement/organization typeは引用ではなく分析分類として完全一致検証から分離します。Recoveryは消費済みCycle 034 authorization／reservation、相関ERROR、Retrieval SHA-256に結び付く`<task_id>_runtime_adapter_repair_1`を一度だけ実行し、旧identityは再利用しません。

Cycle 036では、type別identity metadataと重複する`source_name`／`author_or_organization`をResearcherのProvider schemaから除外し、検証対象のprimary identityから決定論的に復元します。政府の`組織A / 組織B`形式は、保存本文または限定された公式domain aliasによって各構成要素を個別に検証できる場合だけ許可します。消費済みCycle 035 callがこの複合identity境界で停止した場合は、`py main.py --researcher-runtime-identity-repair <WORKFLOW_ID> --provider openrouter --safe-mode`で`<task_id>_runtime_identity_repair_1`を一度だけ実行できます。Retrieval Contextと既存identityは再利用され、旧taskは再送されません。

Cycle 037では、type別identity自体もRetrieval provenanceであり、Reasoning Providerへ再生成させない境界へ統一します。全7 ResearcherのStrict Structured Outputから型別の人物・組織・媒体・platform・国等を除外し、保存URLのhostname、GOVERNMENTに限定した公式domain label、country-code domainから決定論的に復元します。保存Contextだけでは人物名・所属を確定できないEXPERT／POLITICIAN項目は`None`として保持し、推測値を確定情報にしません。消費済みCycle 036 callがProvider生成provenanceで停止した場合は、`py main.py --researcher-runtime-provenance-repair <WORKFLOW_ID> --provider openrouter --safe-mode`で`<task_id>_runtime_provenance_repair_1`を一度だけ実行できます。Retrievalは再実行せず、既存authorization／reservation／ERROR PMP／Context hashへ連鎖します。

Deliberation Quality GateはRepairability Firstで判定します。blocking findingであっても、Specialist再実行、Manager再統合、Counterargument再処理、Researcher追加Evidence、または未検証主張の除外・降格で修復可能なら`revision_required`です。`blocked`は、既存Revision／Recoveryで復元不能、Revision上限到達、または人間判断なしでは安全に継続できない場合に限定します。

ProducerがResearch Planを作成し、Researcherが証拠を収集し、Deliberationが多角的に分析し、Conclusionが人間の最終選択を確定します。Playwrightは、その確定済みConclusionを変更せず、台本・引用台帳・映像指示・制作ノートへ変換します。OpenRouter APIキーがなくてもMock Providerで5層の全工程を確認できます。

通信・Agent ID・status・クロスレイヤーpayloadは、`specifications/common/`のPMP v2.0を機械可読な正本として実装しています。共通基盤は1か所、Layer固有実装とRDはLayer別、実行時データは`storage/data/`に集約したCanonical Projectです。

## Canonical構成

- `main.py`: 唯一の実行Entry Point
- `runtime.py`: 5 Layer、Provider、RD Loader、Repositoryの組み立て
- `config/`, `common/`, `specifications/`: 共通設定・共通実装・共通契約
- `producer/`, `researcher/`, `deliberation/`, `conclusion/`, `playwright/`: Layer固有実装
- `role_definitions/<layer>/`: AgentごとのRole Definition
- `discord_app/`: 全Layer共通のDiscord Control Plane
- `storage/data/`: 唯一のCanonical Storage
- `docs/migration/`, `docs/audit/`: 移行方針と監査記録
- `archive/`: 統合元PrototypeとZIPの保全領域（実行対象外）

## 最初に実行する3コマンド

```powershell
py main.py --doctor
$env:PRDCP_RETRIEVAL_PROVIDER = "mock"
py main.py --demo-e2e --provider mock --topic "生成AIは人間の仕事を奪うのか"
py main.py --status <表示されたworkflow_id>
```

- `--doctor`は依存関係、保存先、31 RD、32 Agent ID、29 Message Type、7 PMP Status、4 Handoff、Provider設定を検査します。
- `--demo-e2e`はAPI料金なしのMockで5層と6納品ファイルを確認します。
- `--status`は停止した層、Agent、error、revision回数、次に実行するコマンドを表示します。
- CLIは通常、読みやすい要約だけを表示します。内部状態が必要な場合だけ末尾へ`--json`を付けます。

Mock E2Eの完走は制御系・Schema・保存・層間接続が動くことを保証します。実OpenRouterの応答品質と外部サービス可用性は別条件なので、API keyと全model IDを設定後に`--doctor`を再実行してください。

## Operator CLIの使い分け

同じWorkflowを再開する操作でも責務が異なります。まず`py main.py --status <workflow_id>`で保存状態と推奨操作を確認してください。

| 種別 | 用途 | 代表コマンド |
| --- | --- | --- |
| Start | 正常な上流Handoffから層を開始 | `--researcher`、`--deliberation`、`--conclusion`、`--playwright` |
| Resume | 正常な上流Revision結果を受け取って再分析 | `--researcher-resume`、`--deliberation-resume`、`--conclusion-resume`、`--playwright-resume` |
| Recover | 保存済みcheckpointを照合し、完了済み処理を再利用 | `--producer-recover`、`--researcher-recover`、`--deliberation-recover`、`--conclusion-recover`、`--playwright-recover` |
| Provider Retry | 課金済みの可能性がある一時障害を、保存済み認可と別task identityで一度だけ再送 | 各層の`--*-provider-retry` |
| Revision | 保存済みQuality Findingに対する明示的一サイクル | `--researcher-revise`、`--conclusion-revise`、`--playwright-revise` |
| Contract/Capability Repair | 同一model retryでは直らないProvider契約・能力不一致を、異なる明示modelで一度だけ修復 | `--conclusion-contract-repair`、`--playwright-capability-repair` |
| Targeted Researcher Repair | 保存済みRetrievalを再利用する旧失敗専用の一回限り修復 | `--researcher-runtime-model-repair`、`--researcher-runtime-output-repair`、`--researcher-runtime-adapter-repair`、`--researcher-runtime-identity-repair`、`--researcher-runtime-provenance-repair` |

`--researcher-retrieval-reconstruct`だけは新しい検索を行うため、Retrieval 0件のRecoveryには使用しません。`--researcher-task <workflow_id> <task_id>`は保存済みroutingで単一Taskを実行する低レベル運用コマンドです。通常運用では`--status`が示す層コマンドを優先してください。全引数は`py main.py --help`、バージョンは`--version`、完全状態は`--json`、開発者向けTracebackは`--verbose`で確認できます。

## 実装済み

### 共通・Producer

- PMP v2.0モデル、親子message追跡、Agent ID・message type・status検証
- Producerの5専門工程と最大3回の修正ループ
- JSON/JSONL永続化とResearcher Inboxへの`research_plan`出力
- Mock Providerと任意利用のOpenRouter Provider

### Researcher

- Research Managerと7専門Researcher、Researcher Quality Reviewer
- Research Planから必要なAgentだけを選択するResearch Task分解
- `asyncio.gather()`による専門Researcherの並列実行
- 一部失敗のlimitation化、全失敗時の安全停止
- 全カテゴリ共通のSource Schemaとカテゴリ固有metadata
- DOI、正規化URL、書誌情報、タイトル類似度による重複整理
- Research Question・Evidence・SourceのID追跡
- Research Report、coverage、evidence gap、品質評価、source perspective生成
- Quality Review後に必ず停止するHuman Evidence Gateと、Evidence不足・hard integrity failureの型付き分類
- 人間判断、Revision Plan、Provider call authorizationを分離し、`REVISE`だけでは追加APIを呼ばない制御
- Human Decisionと受容済み未解決gapを全下流層へ明示的に伝播
- 独立Research Report artifactとDeliberation Outboxへの`research_result`出力
- 手動起動と、設定で有効化できるProducer完了後の自動起動

### Deliberation

- Argument、Causal & Structural、Stakeholder & Responseの3専門Agentを並列実行
- Deliberation Managerによる初回統合
- 初回統合後にCounterargument Analystを実行し、変更履歴付きで再統合
- task、各種analysis、初回/最終integrationを別名前空間で採番し、Workflow内のID衝突を禁止
- evidence、source、analysis、claim、counterargument等を型別に保持し、Claim→Analysis→Evidence→Sourceを追跡
- 実体から一意に導出したmetricsと検証対象集合を相互照合する決定論的Validator
- 最小限のPMP message経路とcheckpoint履歴を受け取るDeliberation Quality ReviewerによるConclusion Readiness審査
- Stakeholderの固有名詞・数値をEvidence/Sourceに拘束し、裏付け不足はunknown、unverified、またはresearch gapとして保持
- blocking counterargumentを修正、棄却、未解決保持、Researcher返送のいずれかへ必ずrouting
- 対象Agentだけを再実行し、依存する統合工程だけを再実行する最大2回の修正ループ
- Evidence不足時のResearcher追加調査要求と`WAITING_UPSTREAM_REVISION`再開処理
- 一次分析の一部失敗を条件付きで継続し、2系統未満では安全停止
- 旧保存JSONを変更せず読込時互換変換し、完了済み高コストcheckpointを再実行しない障害復旧
- Deliberation Result artifactとConclusion Outboxへの`deliberation_result`出力
- Discordからの開始、状態確認、結果表示、再開

### Conclusion

- Conclusion Manager、Position Generator、Decision Evaluator、Decision Integrator、Quality Reviewer
- Deliberation Resultから2～5件の独立したPosition Candidateを生成
- 全候補を同一の14基準で評価し、`NOT_EVALUABLE`をゼロ点扱いしない順序尺度を採用
- 5候補×14基準を上限付きmatrixとして扱い、完全一致反復のみ無損失正規化、矛盾重複・未知ID・評価表不一致を拒否
- 条件付き優位性と感度分析の同率候補は、候補IDを連結せず複数ID配列で保持
- 実行可能性・法的制約などのblocking reasonと不確実性を明示
- 重みの異なる複数ProfileによるSensitivity Analysis
- Candidate間の両立性を検査し、必要な場合だけIntegrated Optionを生成
- 対象Agentと依存工程だけを再実行する最大2回の修正ループ
- 根拠不足時のDeliberation差し戻しと`WAITING_UPSTREAM_REVISION`再開処理
- Quality Gate通過後も`WAITING_HUMAN_SELECTION`で停止し、人間の選択を必須化
- 選択済みFinal Conclusionを不変Artifactとして保存し、Playwright Outboxへ`conclusion_handoff`を出力
- Discordからの開始、候補表示、統合依頼、選択、状態確認、結果表示、再開

### Playwright

- Playwright Manager、Narrative Architect、Scriptwriter、Evidence & Citation Editor、Visual Director
- ConclusionのHuman SelectionとTraceabilityを開始前に検証
- Narrative Blueprintから台本、Citation Manifest、Visual Planを順次生成
- Final Conclusionの内容・ID・SHA-256 Hashを固定し、意味変更を禁止
- 段落、Claim、Evidence、Source、Citation、Visual CueのID追跡
- 引用必須段落、未裏付け主張、出典Locator、制限事項、Chart出典、Visual整合性の決定論的検証
- 対象Agentと依存工程だけを再実行する最大2回の修正ループ
- 上流不足時のConclusion差し戻しと`WAITING_UPSTREAM_REVISION`再開処理
- 独立Playwright Quality Reviewerを置かず、ValidatorとManager Final Gateで最終判定
- JSON・MarkdownによるFinal Script Package 6ファイルの納品
- CLI・Discordからの開始、状態確認、台本・引用・映像指示・結果表示、再開

## 1. セットアップ（Windows）

自動セットアップ:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```

手動セットアップ:

```powershell
cd prdcp
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
copy .env.example .env
```

最初は`.env`の`PRDCP_PROVIDER=mock`と`PRDCP_RETRIEVAL_PROVIDER=mock`のままで構いません。

## 2. APIなしで全工程を確認

Producerだけを実行します。

```powershell
$env:PRDCP_RETRIEVAL_PROVIDER = "mock"
py main.py --demo --provider mock --topic "生成AIは人間の仕事を奪うのか"
```

ProducerからResearcher、Deliberation、Conclusionの品質審査まで連続実行し、人間の選択待ちで停止します。

```powershell
py main.py --demo-full --provider mock --topic "生成AIは人間の仕事を奪うのか"
```

Mockで最初の候補を自動選択し、ProducerからFinal Script PackageまでE2E実行します。

```powershell
py main.py --demo-e2e --provider mock --topic "生成AIは人間の仕事を奪うのか"
```

この節のコマンドは、同じPowerShellセッションで`PRDCP_RETRIEVAL_PROVIDER=mock`を設定していることを前提にします。これによりLLM・Retrievalともに実API呼び出しは0件です。

Producerで作成済みの`workflow_id`からResearcherだけを手動起動できます。

```powershell
py main.py --researcher <workflow_id>
```

Quality Review後のHuman Evidence Gateを確認し、明示的に決定します。

```powershell
py main.py --researcher-evidence <workflow_id>
py main.py --researcher-accept <workflow_id> --reason "Evidence要件を満たす"
py main.py --researcher-accept-limitations <workflow_id> --reason "未解決gapを開示して続行する"
py main.py --researcher-revise <workflow_id> --reason "追加調査が必要"
py main.py --researcher-recover <workflow_id>
```

`--researcher-revise`はRevision Planの保存だけを行い、Providerを呼びません。`--researcher-recover`は保存障害の復旧であり、人間の決定を推測しません。

Researcherで作成済みの`workflow_id`からDeliberationだけを手動起動できます。

```powershell
py main.py --deliberation <workflow_id>
```

Researcherの追加調査結果を受領した後は、待機中Workflowを再開できます。

```powershell
py main.py --deliberation-resume <workflow_id>
```

Deliberationが障害で停止した場合は、保存済みcheckpointを検査し、最後の未完了段階から復旧できます。`--deliberation-resume`とは用途が異なり、Researcherからの追加Evidenceは読み込みません。

```powershell
py main.py --deliberation-recover <workflow_id>
```

応答途中切断として保存されたRetryable provider failureだけを一度再送する場合は、Demo Safe Modeを維持した専用コマンドを使います。

```powershell
py main.py --deliberation-provider-retry <workflow_id> --provider openrouter --safe-mode
```

Researcher ReturnでEvidence集合が変わった場合、Deliberationは旧Evidenceを参照する保存済み一次分析をstaleと判定し、明示されたrevision targetに依存Agentを自動追加します。Revision taskは決定論的IDでProvider呼び出し前に保存され、task_idを持つOpenRouter呼び出しはSafe ModeのON/OFFにかかわらずpersistent reservationで重複送信を防ぎます。旧`ev_*`/`src_*` IDは保存JSONを書き換えず読込時だけ`evidence_*`/`source_*`へ決定論的変換し、新規Structured Outputではcanonical prefix以外を拒否します。

Deliberationで作成済みの`workflow_id`からConclusionを起動できます。

```powershell
py main.py --conclusion <workflow_id>
```

Deliberationの修正版を受領した後は、待機中のConclusion Workflowを再開できます。

```powershell
py main.py --conclusion-resume <workflow_id>
```

ConclusionがProvider応答障害で停止した場合は、正常なcheckpointを再利用し、最後の未完了stageだけを再開します。再送はDemo Safe Mode下の明示的一回認可に限定されます。

```powershell
py main.py --conclusion-provider-retry <workflow_id> --provider openrouter --safe-mode
```

認可が保存済みでretry taskがまだ未実行の場合だけ、同じcheckpoint recoveryを次のコマンドで再開できます。

```powershell
py main.py --conclusion-recover <workflow_id> --provider openrouter --safe-mode
```

品質審査済みCandidateを人間が選択すると、Final ConclusionとPlaywright Handoffを確定します。

```powershell
py main.py --conclusion-select <workflow_id> <candidate_id>
```

複数Candidateの統合案を再生成・再審査する場合は、2件以上のIDを指定します。

```powershell
py main.py --conclusion-integrate <workflow_id> <candidate_id_1> <candidate_id_2>
```

Human Selection確定後にPlaywrightを手動起動します。

```powershell
py main.py --playwright <workflow_id>
```

Conclusionの修正版を受領した後は、待機中のPlaywright Workflowを再開できます。

```powershell
py main.py --playwright-resume <workflow_id>
```

PlaywrightがProvider通信または応答契約エラーで失敗した場合、通常のRecoveryは
保存済みの正常な段階だけを再利用し、未回答のProvider要求や既存Reservationを
自動再送しません。状態検査だけを行うRecoveryと、一回限りの明示再送を分けて
実行します。

```powershell
py main.py --playwright-recover <workflow_id> --provider openrouter --safe-mode
py main.py --playwright-provider-retry <workflow_id> --provider openrouter --safe-mode
py main.py --playwright-revise <workflow_id> --provider openrouter --safe-mode
py main.py --playwright-capability-repair <workflow_id> <repair_model_id> --provider openrouter --safe-mode
```

`--playwright-resume`はConclusionから修正版Handoffを受領した後の再開専用です。
障害復旧は`--playwright-recover`、課金済みの可能性がある失敗の一回限り再送は
`--playwright-provider-retry`を使用します。各Playwright段階はupstream/revision番号を
含むLogical Task IDで予約され、完了済み段階は再実行されません。

Final Gateが`CITATION_MAPPING_MISSING`だけで停止し、既存Paragraph→Evidence→Sourceと保存済みの
意味分類からmappingを一意に復元できる場合、`--playwright-recover`はallowlist型のlocal repairを
Provider/Retrieval 0件で実行します。これはLLM Revision Budgetとは独立し、内容、Evidence、
Conclusion、Script、Visualを変更しません。競合、locator不足、別ERROR、accepted unresolved gapの
Evidence化が必要な場合はFail Closedです。

Manager Final Gateが修復可能なCitation/Visual findingを保存してDemo Safe Modeで停止した場合は、
`--playwright-revise`で保存済みtargetの最初の工程から依存閉包を一サイクルだけ明示実行します。
revision planとcheckpoint無効化は最初のProvider呼び出し前に保存され、再検証がさらにrevisionを
要求しても同じCLI実行内では次サイクルへ進みません。Citationのparagraph/claim/evidence/sourceと
Visualのsection/paragraph/evidence/sourceはrequest別strict schemaへ束縛し、Visual Cueが参照する
asset IDは同じVisualPlan内で定義されていることをPydanticでも検証します。上流の
`limitations_to_disclose`はManager所有の正本metadataとしてCitation結果へ決定論的に同期するため、
LLMへ長い正本配列の逐語再転記を委ねません。

`require_parameters=true`のStructured Output要求に対してOpenRouterがHTTP 404
`No endpoints found that can handle the requested parameters`を返した場合は、一時障害ではなく
設定モデルの能力不一致として扱います。同一モデルretryは拒否し、保存済みError PMPと元reservationを
照合できる場合だけ`--playwright-capability-repair`で異なるモデルを一回実行します。
専用`_provider_capability_repair_1` task、PENDING/CONSUMED認可、独立reservationを使い、
成功したprovider-Agent-output schemaの組合せは既存の互換性bindingへ保存され、同じ設定モデルを使う
将来taskだけに再利用されます。`require_parameters`やstrict schemaを無効化して通過させることはありません。

## 3. 保存先

```text
storage/data/workflows/producer/<workflow_id>.json
storage/data/workflows/producer/<workflow_id>.messages.jsonl
storage/data/outbox/researcher/<workflow_id>.json

storage/data/workflows/researcher/<workflow_id>.json
storage/data/workflows/researcher/<workflow_id>.messages.jsonl
storage/data/artifacts/research_reports/<workflow_id>.json
storage/data/outbox/deliberation/<workflow_id>.json

storage/data/workflows/deliberation/<workflow_id>.json
storage/data/workflows/deliberation/<workflow_id>.messages.jsonl
storage/data/artifacts/deliberation_results/<workflow_id>.json
storage/data/outbox/conclusion/<workflow_id>.json
storage/data/outbox/researcher_revision/<workflow_id>.json

storage/data/workflows/conclusion/<workflow_id>.json
storage/data/workflows/conclusion/<workflow_id>.messages.jsonl
storage/data/artifacts/conclusion_packages/<workflow_id>.json
storage/data/artifacts/final_conclusions/<workflow_id>.json
storage/data/outbox/playwright/<workflow_id>.json
storage/data/outbox/deliberation_revision/<workflow_id>.json

storage/data/workflows/playwright/<workflow_id>.json
storage/data/workflows/playwright/<workflow_id>.messages.jsonl
storage/data/artifacts/narrative_blueprints/<workflow_id>.json
storage/data/artifacts/script_drafts/<workflow_id>.json
storage/data/artifacts/citation_manifests/<workflow_id>.json
storage/data/artifacts/visual_plans/<workflow_id>.json
storage/data/artifacts/final_script_packages/<workflow_id>.json
storage/data/outbox/conclusion_revision/<workflow_id>.json
storage/data/retrieval_contexts/<workflow_id>/
storage/data/retrieval_call_reservations/
storage/data/provider_call_reservations/
storage/data/provider_*_authorizations/
storage/data/retrieval_reconstruction_authorizations/
storage/data/provider_model_compatibility/
storage/data/artifacts/human_evidence_decisions/<workflow_id>/
storage/data/artifacts/playwright_deterministic_repairs/<workflow_id>/
storage/data/deliveries/<workflow_id>/final_script_package.json
storage/data/deliveries/<workflow_id>/script.md
storage/data/deliveries/<workflow_id>/citation_manifest.json
storage/data/deliveries/<workflow_id>/source_list.md
storage/data/deliveries/<workflow_id>/visual_plan.md
storage/data/deliveries/<workflow_id>/production_notes.md
```

旧Producer Prototypeの`storage/data/workflows/<workflow_id>.json`も読込互換を維持しています。

## 4. テスト

```powershell
py scripts\verify.py
```

この検証はcompile、全単体・統合テスト、STRICT RD読込、共通仕様のdrift検査、5層Mock E2E、6納品ファイル確認を順番に実行します。GitHubへ公開した後は`.github/workflows/ci.yml`がPython 3.11と3.14で同じ検証を自動実行します。

## 5. Discord Bot

`.env`の`DISCORD_BOT_TOKEN`を設定し、Developer PortalでMessage Content Intentを有効にしてから実行します。

```powershell
py main.py
```

Producerコマンド:

```text
!producer
!producer_topic 生成AIは人間の仕事を奪うのか
!producer_status <workflow_id>
!runtime_models [layer]
```

Researcherコマンド:

```text
!researcher <workflow_id>
!researcher_status <workflow_id>
!researcher_result <workflow_id>
!researcher_evidence <workflow_id>
!researcher_accept <workflow_id> <reason>
!researcher_accept_limitations <workflow_id> <reason>
!researcher_revise <workflow_id> <reason>
!researcher_recover <workflow_id>
```

Deliberationコマンド:

```text
!deliberation <workflow_id>
!deliberation_status <workflow_id>
!deliberation_result <workflow_id>
!deliberation_resume <workflow_id>
```

Conclusionコマンド:

```text
!conclusion <workflow_id>
!conclusion_status <workflow_id>
!conclusion_options <workflow_id>
!conclusion_select <workflow_id> <candidate_id>
!conclusion_integrate <workflow_id> <candidate_id_1> <candidate_id_2>
!conclusion_result <workflow_id>
!conclusion_resume <workflow_id>
```

Playwrightコマンド:

```text
!playwright <workflow_id>
!playwright_status <workflow_id>
!playwright_script <workflow_id>
!playwright_citations <workflow_id>
!playwright_visuals <workflow_id>
!playwright_result <workflow_id>
!playwright_resume <workflow_id>
```

初期値ではResearcherは手動起動です。Producer完了後の自動起動を使う場合は`.env`で次を設定します。

```text
PRDCP_AUTO_START_RESEARCHER=true
PRDCP_AUTO_START_DELIBERATION=true
PRDCP_AUTO_START_CONCLUSION=true
PRDCP_AUTO_START_PLAYWRIGHT=true
```

## 6. OpenRouterへ切り替える場合

`.env`で次を設定します。

```text
PRDCP_PROVIDER=openrouter
PRDCP_RETRIEVAL_PROVIDER=openrouter
OPENROUTER_RETRIEVAL_MODEL=google/gemini-3.7-flash
OPENROUTER_RETRIEVAL_ENGINE=exa
OPENROUTER_API_KEY=<your key>
```

`PRDCP_PROVIDER`はStructured Reasoning、`PRDCP_RETRIEVAL_PROVIDER`は検索を選択します。本番検索を行う場合は両方を`openrouter`にします。保存済みRetrieval Contextを使うRecoveryでは、ManagerがContext hashとReservationを照合し、不要な再検索を行いません。

各`MODEL_...`には、利用時点でOpenRouterに登録されている実際のmodel IDを指定してください。設計上の表示名と環境変数の対応は`config/models.json`に記録しています。

OpenRouter応答は指定JSON Schemaとして検証されます。Schema不一致や技術エラーのretryは、Quality Reviewerによる成果物修正ループとは別に記録されます。

OpenRouterへ送るDeliberation入力は、保存済みResearch Reportを変更せず、Evidence・Source・Metadata・Qualityの重複表をtrace IDで統合した実行時viewを使用します。Strict JSON Schemaは`response_format`だけを権威ある境界として一度送信し、system promptへ全文を重複埋め込みしません。既知のmodel context上限はProvider reservation作成前にローカル検査されます。Context超過で停止したCounterargument recoveryは、元reservationを保持したまま`*_context_repair_1`という決定的な一回限りの論理taskで再開します。

Counterargument Structured Outputは`required_revision=true/false`を`anyOf`で分離します。trueでは内部Deliberation revision targetとacceptance conditionを必須化し、falseではtarget配列を空にします。Researcher追加調査は架空のDeliberation Agent IDではなく`research_gap_required`で表現します。Provider応答後に判明した旧rawの誤prefix・非内部targetは、保存rawを上書きせず監査記録付きread adapterで再利用します。
Structured Output境界では、root、nested model、array items、`$defs`、`anyOf`/union以下を含む全objectが閉じた明示Schemaとして送信されます。全objectに`additionalProperties: false`を要求し、`properties`を持つobjectでは`required`を全property keyと完全一致させます。Pydantic内部のdefaultは維持したままAPI Schemaからのみ除去され、自由形式dict、不正な`$ref` sibling、未解決参照がoutput modelへ追加された場合はAPI呼び出し前の22 root schema監査で停止します。自由形式payloadへ機械的に`additionalProperties: false`を付けて意味を変えず、Structured Output境界では用途に合う明示的なPydantic modelへ置き換えてください。

## 仕様上の正規化

- PMPはCommon仕様のv2.0を使用
- message typeと品質判定はstatus registryのlower snake caseへ統一
- Researcherの7カテゴリは共通`ResearchSource` Schemaで表現
- `sources`をAgent共通の出力名とし、カテゴリは`source_type`で識別
- Researcher→Deliberation payloadはCommonのhandoff contractが要求する項目に加え、Human Evidence Decision、受容済みgap、integrity repairをトップレベルに保持
- Researcherは証拠収集・整理に限定し、因果分析・解決策・結論を生成しない
- Deliberationの工程差は既存`deliberation_task_assignment/result`のpayloadで識別し、未登録message typeを追加しない
- 上流Evidence不足は`revision_required`と`revision_scope=researcher_return`で表現
- Deliberation→Conclusion payloadはCommon handoff contractの17項目をトップレベルに保持
- Deliberationは多角的分析に限定し、最終結論や政策選択を生成しない
- Conclusionの工程差は既存`conclusion_task_assignment/result`のpayloadで識別し、未登録message typeを追加しない
- Deliberation差し戻しは登録済み`revision_request`と`revision_scope=deliberation_return`で表現
- Quality Reviewerは候補を承認するだけで最終選択せず、人間の明示選択を必須とする
- Conclusion→Playwright payloadは実行用の`final_conclusion`、`conclusion_package`、`human_selection`、`human_evidence_decision`、`accepted_evidence_gaps`、`traceability_manifest`、`limitations_to_disclose`を正本とし、Common handoff contract項目も互換用に保持
- Playwrightは新しいMessage Typeを追加せず、層内処理を登録済み`task`・`result`・`revision_request`で表現
- PlaywrightのFinal GateはCommonの列挙値を増やさず、Playwright State内の5状態へ限定
- Playwrightは独立Quality Reviewerを実行せず、決定論的Validator、Evidence & Citation Editor、Manager Final Gateで品質を保証

## RD Loader v2

Producer、Researcher、Deliberation、Conclusion、Playwrightの実装済み全31 Agentは、実行ごとに`role_definitions/registry.json`から対応RDを特定します。RuntimeはJSON検証後にSnapshotを固定し、LLM向けRole ContextとRuntime設定を分離してからSystem Promptを組み立てます。

```text
common/role_definitions/  Loader・Registry・Validator・Cache・Extractor・Access Log
common/prompting/         共通規則とPrompt Builder
role_definitions/producer/
role_definitions/researcher/
role_definitions/deliberation/
role_definitions/conclusion/
role_definitions/playwright/
```

本番では起動時に全RDを検証し、1件でも不正なら起動を停止します。

```text
PRDCP_RD_STRICT=true
PRDCP_RD_RELOAD=false
```

RD編集中は、Agent実行のたびにファイル更新を確認できます。不正な更新が見つかった場合、旧Cacheへ自動Fallbackせず、そのAgent実行を停止します。

```text
PRDCP_RD_RELOAD=true
```

Agentの応答PMPには`metadata.extensions.role_definition`としてRD ID、Version、SHA-256 Hashを記録します。Manager自身のRD使用情報はWorkflow Stateの`role_definition_usage`へ保存され、参照履歴は`storage/data/logs/rd_access.jsonl`へ記録されます。Role Definition本文全体や認証情報はAccess Logへ保存しません。

元のWord RDからJSON正本へ変換する際に行った構文修復は`role_definitions/SOURCE_REPAIRS.json`に記録しています。

## 開発・障害調査ガイド

- [Architecture](docs/architecture/ARCHITECTURE.md): 5層の統一構成と共通基盤
- [Maintenance](docs/operations/MAINTENANCE.md): 変更内容ごとの編集場所とRelease checklist
- [Troubleshooting](docs/operations/TROUBLESHOOTING.md): 状態値、ログ、よくある停止理由
- [Migration](docs/migration/README.md): ファイル配置計画とMigration Map
- [Audit](docs/audit/README.md): Canonical化の差分監査と検証結果
- [Changelog](CHANGELOG.md): v2での変更点
