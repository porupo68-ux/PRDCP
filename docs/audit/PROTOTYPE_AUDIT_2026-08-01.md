# PRDCP v2 最終監査

監査日: 2026-08-01

## 結論

この配布物は、`PRDCP_PROVIDER=mock` の構成で5層MASを実行し、
ProducerからPlaywrightの最終6成果物まで完走できることを確認済みです。
RD、PMP、層間Handoff、保存・再開、修正ループ、人間選択Gateも自動テストの
対象です。

OpenRouterを使う本番相当の実行には、利用者側のAPI keyと31個の有効な
model IDが必要です。これらは配布物に含められないため、外部モデルの可用性や
回答品質までを「確認済み」とは判定していません。設定後に `--doctor` を実行し、
警告が消えたことを確認してください。

## 検証結果

| 対象 | 結果 |
| --- | --- |
| Python構文コンパイル | PASS |
| 単体・統合テスト | 154件 PASS |
| Mock 5層E2E | PASS |
| 最終納品物 | 規定6ファイルを生成 |
| RD STRICT読込 | 31件 PASS |
| Agent Registry | 32件一致 |
| 実行対象Agent/RD | 31件一致 |
| PMP Message Type | 29件一致 |
| PMP metadata status | 7件一致 |
| Cross-layer handoff | 4件一致 |
| `pyproject.toml` wheel build | PASS |
| wheel導入後の `--doctor` | PASS（任意設定は警告） |
| Discord command登録 | discord.py 2.7.1で24件を確認 |

Playwright Quality ReviewerはCommon設計にIDがありますが、後発のPlaywright
実装計画が独立Reviewerを使わない5 Agent構成を指定しています。そのため
`config/implementation_overrides.json` に例外を明示し、Manager Final Gate、
Evidence & Citation Editor、決定論的Validatorで代替しています。黙った仕様逸脱には
していません。

## 保守性の改善

- 5層に重複していたAgent実行処理を `common/agents/base.py` に集約しました。
- 各層の `workflow.py` は同じ公開フィールドと並びに統一しました。
- 機械可読なCommon仕様を `specifications/common/` に同梱しました。
- `--doctor` で依存関係、保存先、仕様ドリフト、全RD、外部設定を一括確認できます。
- `--status` で停止層、エラー、修正回数、次の操作を確認できます。
- 通常出力を短い進捗表示にし、完全JSONは `--json` に分離しました。
- application log、runtime event log、RD access logを用途別に分けました。
- `docs/operations/MAINTENANCE.md` と `docs/operations/TROUBLESHOOTING.md` に、変更場所と障害調査順を記載しました。
- `pyproject.toml`、Windowsセットアップ、1コマンド検証、GitHub Actions CIを追加しました。

## 自分で再確認する方法

Windows PowerShellでは次を順に実行します。

```powershell
.\scripts\setup_windows.ps1
py main.py --doctor
py scripts\verify.py
```

個別に動作を見る場合:

```powershell
py main.py --demo-e2e --topic "確認したいテーマ"
```

実OpenRouterを使用する前には `.env.example` を `.env` としてコピーし、API keyと
全 `MODEL_*` を設定します。秘密情報をZIPやGitへ追加しないでください。

## 採用した一般的な基準

- Python Packaging User Guideの `pyproject.toml` 中心の配布構成
  - https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
- Python公式Logging Cookbookの、モジュールから分離した一元設定とRotating File Handler
  - https://docs.python.org/3/howto/logging-cookbook.html
- GitHub公式の、複数Python版でテストするCI構成
  - https://docs.github.com/actions/guides/building-and-testing-python

このプロジェクトは、単一アプリとして相互参照が多く、今回の段階で `src/` 配置へ
全面移行するとimport互換性を壊す危険があるため、flat layoutを維持しました。
パッケージングを導入したうえで、将来の大きな版で移行できるよう境界を共通モジュールへ
寄せています。
