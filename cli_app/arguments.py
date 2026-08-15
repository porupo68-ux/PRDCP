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
    operation.add_argument("--researcher", metavar="WORKFLOW_ID", help="保存済みResearch PlanからResearcherを実行する")
    operation.add_argument(
        "--researcher-resume",
        metavar="WORKFLOW_ID",
        help="Deliberationからの追加Evidence要求を処理してResearcherを再開する",
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
    operation.add_argument("--conclusion", metavar="WORKFLOW_ID", help="保存済みDeliberation ResultからConclusionを実行する")
    operation.add_argument("--conclusion-resume", metavar="WORKFLOW_ID", help="Deliberation修正後にConclusionを再開する")
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

    parser.add_argument("--topic", help="demoで使用するユーザー指定Topic")
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
