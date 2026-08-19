# Playwright

## 1. Purpose

選択済みFinal Conclusionを変更せず、台本と制作向け最終成果物へ変換します。

## 2. Responsibilities

Narrative設計、Script執筆、Evidence/Citation編集、Visual設計、Manager Final Gateを担当します。

## 3. Agent Structure

Manager、Narrative Architect、Scriptwriter、Evidence Citation Editor、Visual Directorで構成します。

## 4. Workflow

Final Conclusion→Narrative Blueprint→Script Draft→Citation/Visual編集→Final Script Packageです。

## 5. Inputs

ConclusionのHuman Selection済みhandoffです。

## 6. Outputs

Script、Source List、Citation Manifest、Visual Plan、Production Notes、Final Script Packageです。

## 7. Role Definitions

`role_definitions/playwright/*.json`を共通RD Loader経由で参照します。

## 8. PMP Interfaces

Conclusionから受信し、`system.final_output`へ最終納品messageを送信します。

## 9. Storage

`storage/data/workflows/playwright/`と`storage/data/deliveries/<workflow_id>/`を使用します。

## 10. Discord Operations

`#playwright`、`#deliveries`、`#workflow-status`へ進捗と成果物を出力します。

## 11. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`validator.py`、`agents/`、`schemas/`です。

## 12. Testing

Citation、Visual、Delivery、Full Mock E2Eで検証します。

## 13. Safe Mode Revision

Final Gateが内部修正可能なfindingを返した場合、Demo Safe Modeは自動再実行せず`BLOCKED`で
checkpointを保存します。`py main.py --playwright-revise <workflow_id> --safe-mode`は、保存済み
findingからCitation/Visual等の最小依存閉包を一サイクルだけ実行します。各task IDにはrevision番号を
含め、完了済みの非依存artifactを再利用します。再検証が再び修正を要求した場合は自動継続しません。

CitationとVisualのOpenRouter strict schemaはrequest内のID集合へ動的に束縛されます。Visual Cueの
asset参照は同一VisualPlan内で解決必須です。`limitations_to_disclose`はManagerがFinal Packageまで
正本として保持し、Providerに逐語再転記させません。

## 14. Deterministic Citation Repair

`--playwright-recover <workflow_id>`は、通常の`FAILED` checkpoint recoveryに加えて、Final Gateが
`CITATION_MAPPING_MISSING`だけで`BLOCKED`になった場合のlocal repairを扱います。Script段落の
claim/evidence、Production Contextのevidence→source、Citation Manifestのsource locator、および
保存済みmappingの意味分類が一意に一致するときだけ、欠落mappingを決定論的IDで再構成します。

この経路はallowlist方式で、Provider/Retrievalを呼ばず、`revision_count`を消費しません。mapping競合、
未知Evidence、locator不足、意味分類の不一致、accepted unresolved gapのEvidence化、または別のERRORが
ある場合はFail Closedです。修復履歴と前後hashは
`artifacts/playwright_deterministic_repairs/<workflow_id>/`へ保存されます。完了後の同じrecoverはno-opで、
Deliveryを二重生成しません。

## 15. Provider Failure Recovery

`--playwright-provider-retry`は保存済み一時障害の一回限り再送、`--playwright-capability-repair <workflow_id> <repair_model_id>`はStructured Outputs対応Endpointがない能力不一致を異なるmodelで修復する操作です。どちらもDemo Safe Mode、専用Authorization、別task identity、独立Reservationを必須とします。`require_parameters=true`やstrict schemaを弱めるfallbackは行いません。Conclusion Handoff更新後は`--playwright-resume`を使用し、これらの技術障害経路と分離します。
