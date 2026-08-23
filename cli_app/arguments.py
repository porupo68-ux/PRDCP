from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PRDCP v2 — Producer → Researcher → Deliberation → Conclusion → Playwright",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--doctor", action="store_true", help="設定・RD・共通契約を起動前診断する")
    operation.add_argument("--status", metavar="WORKFLOW_ID", help="5層すべての保存状態と次の操作を表示する")
    operation.add_argument("--demo", action="store_true", help="Discordを使わずProducerを1件実行する")
    operation.add_argument("--demo-full", action="store_true", help="Human Selection待ちまで4層を連続実行する")
    operation.add_argument("--demo-e2e", action="store_true", help="MockでFinal Script Packageまで5層を連続実行する")
    operation.add_argument(
        "--producer-recover",
        metavar="WORKFLOW_ID",
        help="Resume Producer from its first incomplete saved checkpoint",
    )
    operation.add_argument(
        "--producer-revise",
        metavar="WORKFLOW_ID",
        help=(
            "Authorize and execute exactly one saved Producer internal Revision "
            "cycle; Safe Mode remains enabled"
        ),
    )
    operation.add_argument(
        "--producer-provider-retry",
        metavar="WORKFLOW_ID",
        help=(
            "Run one repaired General Opinion reasoning call from saved Retrieval; "
            "do not run downstream Producer agents"
        ),
    )
    operation.add_argument(
        "--producer-output-repair",
        metavar="WORKFLOW_ID",
        help=(
            "After the consumed General Opinion retry hit the repaired Retrieval "
            "metadata hydration contract, run one new audited reasoning task and "
            "stop before Research Planner"
        ),
    )
    operation.add_argument("--researcher", metavar="WORKFLOW_ID", help="保存済みResearch PlanからResearcherを実行する")
    operation.add_argument(
        "--researcher-resume",
        metavar="WORKFLOW_ID",
        help="Deliberationからの追加Evidence要求を処理してResearcherを再開する",
    )
    operation.add_argument(
        "--researcher-recover",
        metavar="WORKFLOW_ID",
        help=(
            "Human Evidence Gateの保存済みdecision/state/outboxをProvider呼び出し0件で"
            "照合・復旧する。decision未入力ならGate待ちを維持する"
        ),
    )
    operation.add_argument(
        "--researcher-integrity-repair",
        metavar="WORKFLOW_ID",
        help=(
            "保存済みResearch Reportのallowlist対象Hard Integrity Findingを、"
            "Provider/Retrieval呼び出し0件で一回だけ決定論的に修復する"
        ),
    )
    operation.add_argument(
        "--researcher-evidence",
        metavar="WORKFLOW_ID",
        help="Human Evidence GateのIntegrity Finding・Evidence Gap・次の選択肢を表示する",
    )
    operation.add_argument(
        "--researcher-accept",
        metavar="WORKFLOW_ID",
        help="Evidence GapがないQuality AssessmentをHuman Operatorとして承認する",
    )
    operation.add_argument(
        "--researcher-accept-limitations",
        metavar="WORKFLOW_ID",
        help="Evidence Sufficiency Findingを未解決の制約として受容し、Researcherを完了する",
    )
    operation.add_argument(
        "--researcher-provider-retry",
        metavar="WORKFLOW_ID",
        help=(
            "RetryableAgentErrorで停止したResearcher Quality Reviewerを、"
            "Demo Safe Modeのまま一度だけ明示的に再送する"
        ),
    )
    operation.add_argument(
        "--researcher-runtime-model-repair",
        metavar="WORKFLOW_ID",
        help=(
            "Recover only incomplete Researcher tasks after runtime model drift, "
            "reusing persisted Retrieval Contexts with one-shot repair identities"
        ),
    )
    operation.add_argument(
        "--researcher-retrieval-reconstruct",
        metavar="WORKFLOW_ID",
        help=(
            "Reconstruct missing Researcher Retrieval Contexts with new one-shot "
            "identities, then continue runtime-model recovery"
        ),
    )
    operation.add_argument(
        "--researcher-runtime-output-repair",
        metavar="WORKFLOW_ID",
        help=(
            "Repair one consumed runtime-model call that failed the deterministic "
            "Retrieval excerpt hydration contract, then resume untouched tasks"
        ),
    )
    operation.add_argument(
        "--researcher-runtime-adapter-repair",
        metavar="WORKFLOW_ID",
        help=(
            "Repair one consumed runtime-output call that failed deterministic "
            "ResearchSource identity canonicalization, then resume untouched tasks"
        ),
    )
    operation.add_argument(
        "--researcher-runtime-identity-repair",
        metavar="WORKFLOW_ID",
        help=(
            "Repair one consumed adapter call that failed redundant composite "
            "ResearchSource identity hydration, then resume untouched tasks"
        ),
    )
    operation.add_argument(
        "--researcher-runtime-provenance-repair",
        metavar="WORKFLOW_ID",
        help=(
            "Repair one consumed identity call whose source provenance was still "
            "Provider-generated, then resume untouched Researcher tasks"
        ),
    )
    operation.add_argument(
        "--researcher-revise",
        metavar="WORKFLOW_ID",
        help=(
            "Evidence Sufficiency FindingへのHuman REVISE decisionとAPI call planを"
            "Provider呼び出し0件で保存する（有料実行許可とは別）"
        ),
    )
    operation.add_argument(
        "--researcher-revision-execute",
        metavar="WORKFLOW_ID",
        help=(
            "Human REVISEで保存したResearcher revision planを、別の明示承認として"
            "一サイクルだけ実行する"
        ),
    )
    operation.add_argument(
        "--researcher-task",
        nargs=2,
        metavar=("WORKFLOW_ID", "TASK_ID"),
        help="保存済みResearcher Taskを既存routingで1件だけ実行する",
    )
    operation.add_argument("--deliberation", metavar="WORKFLOW_ID", help="保存済みResearch ReportからDeliberationを実行する")
    operation.add_argument("--deliberation-resume", metavar="WORKFLOW_ID", help="追加Evidence受領後にDeliberationを再開する")
    operation.add_argument(
        "--deliberation-recover",
        metavar="WORKFLOW_ID",
        help="保存済みcheckpointを検査し、Deliberationの障害発生箇所から復旧する",
    )
    operation.add_argument(
        "--deliberation-revise",
        metavar="WORKFLOW_ID",
        help="保存済みDeliberation internal Revision planを明示的一回だけ実行する",
    )
    operation.add_argument(
        "--deliberation-provider-retry",
        metavar="WORKFLOW_ID",
        help=(
            "途中切断で停止したDeliberation ManagerまたはQuality Reviewerを、"
            "Demo Safe Modeのまま一度だけ明示的に再送する"
        ),
    )
    operation.add_argument("--conclusion", metavar="WORKFLOW_ID", help="保存済みDeliberation ResultからConclusionを実行する")
    operation.add_argument("--conclusion-resume", metavar="WORKFLOW_ID", help="Deliberation修正後にConclusionを再開する")
    operation.add_argument(
        "--conclusion-recover",
        metavar="WORKFLOW_ID",
        help="保存済みcheckpointを検査し、Conclusionの最後の未完了stageから復旧する",
    )
    operation.add_argument(
        "--conclusion-provider-retry",
        metavar="WORKFLOW_ID",
        help=(
            "Provider応答障害で停止したConclusion taskを、Demo Safe Modeのまま"
            "一度だけ明示的に再送する"
        ),
    )
    operation.add_argument(
        "--conclusion-revise",
        metavar="WORKFLOW_ID",
        help=(
            "保存済みrevision_required Quality Reviewに対するConclusion内部revisionを、"
            "Demo Safe Modeのまま一サイクルだけ明示実行する"
        ),
    )
    operation.add_argument(
        "--conclusion-contract-repair",
        nargs=2,
        metavar=("WORKFLOW_ID", "REPAIR_MODEL_ID"),
        help=(
            "After an original Conclusion call and its one explicit retry both "
            "violate the structured-output contract, run one distinct audited "
            "repair task on a different OpenRouter model"
        ),
    )
    operation.add_argument(
        "--conclusion-select",
        nargs=2,
        metavar=("WORKFLOW_ID", "CANDIDATE_ID"),
        help="Conclusion候補を人間選択として確定する",
    )
    operation.add_argument(
        "--conclusion-integrate",
        nargs="+",
        metavar="VALUE",
        help="WORKFLOW_IDに続けて2件以上のcandidate_idを指定して再統合する",
    )
    operation.add_argument("--playwright", metavar="WORKFLOW_ID", help="Human Selection済みConclusionからPlaywrightを実行する")
    operation.add_argument("--playwright-resume", metavar="WORKFLOW_ID", help="Conclusion修正後にPlaywrightを再開する")
    operation.add_argument(
        "--playwright-recover",
        metavar="WORKFLOW_ID",
        help=(
            "失敗したPlaywrightをcheckpointから復旧する。Final GateでBLOCKEDの場合は、"
            "allowlist対象のlocal deterministic repairだけをProvider呼び出し0件で試行する"
        ),
    )
    operation.add_argument(
        "--playwright-provider-retry",
        metavar="WORKFLOW_ID",
        help=(
            "失敗したPlaywright Provider taskをDemo Safe Modeのまま"
            "一度だけ明示的に再送する"
        ),
    )
    operation.add_argument(
        "--playwright-revise",
        metavar="WORKFLOW_ID",
        help=(
            "保存済みdeterministic gateの内部revisionを、Demo Safe Modeのまま"
            "一サイクルだけ明示実行する"
        ),
    )
    operation.add_argument(
        "--playwright-capability-repair",
        nargs=2,
        metavar=("WORKFLOW_ID", "REPAIR_MODEL_ID"),
        help=(
            "Structured Outputs対応endpointがないPlaywright taskを、"
            "異なる明示モデルで一度だけ監査付き実行する"
        ),
    )

    parser.add_argument("--topic", help="demoで使用するユーザー指定Topic")
    parser.add_argument(
        "--reason",
        default=None,
        help="Human Evidence Gate decisionの監査理由",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="要約ではなく完全な状態JSONを表示する")
    parser.add_argument("--verbose", action="store_true", help="エラー時に開発者向けTracebackを表示する")
    parser.add_argument(
        "--provider",
        choices=("mock", "openrouter"),
        default=None,
        help="LLM provider (overrides PRDCP_PROVIDER for this invocation)",
    )
    safe_mode = parser.add_mutually_exclusive_group()
    safe_mode.add_argument(
        "--safe-mode",
        action="store_true",
        dest="demo_safe_mode",
        default=None,
        help="enable Demo Safe Mode for this invocation",
    )
    safe_mode.add_argument(
        "--no-safe-mode",
        action="store_false",
        dest="demo_safe_mode",
        help="disable Demo Safe Mode for this invocation",
    )
    parser.add_argument("--version", action="version", version="PRDCP 2.0.0")
    return parser.parse_args(argv)
