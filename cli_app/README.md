# CLI App

`main.py`から呼び出される唯一のCLI実装です。

- `arguments.py`: 引数定義
- `commands.py`: コマンドの実行と各Managerへの接続
- `diagnostics.py`: `--doctor`の整合性検査
- `events.py`: Runtime Eventの出力
- `output.py`: 人間向け表示とJSON表示

CLI固有の表示・引数処理だけを置き、Agentロジックは各Layerに残します。

`--provider`はLLM Providerだけの実行時overrideです。Retrievalは`PRDCP_RETRIEVAL_PROVIDER`を使用するため、API 0件のMock E2Eでは両方を`mock`にします。`--status WORKFLOW_ID`は5層の保存状態を横断表示し、通常再開、上流Revision、checkpoint Recovery、明示的Provider Retry、Human Gateのどれを使うべきか案内します。

再開系コマンドの責務は次のとおりです。

- `--*-resume`: 正常な上流Revision結果を受領した後の再分析
- `--*-recover`: 保存済みcheckpoint/state/outboxを照合する障害復旧
- `--*-provider-retry`: 課金済みの可能性がある一時障害の一回限り明示再送
- `--*-revise`: 保存済みQuality Findingに対する明示的一サイクル
- `--conclusion-contract-repair` / `--playwright-capability-repair`: 異なる明示modelを使う一回限り契約修復

Researcherの`runtime-*-repair`群は、旧失敗のauthorization、reservation、Error PMP、Retrieval Context hashが一致するときだけ保存済み検索を再利用します。`--researcher-retrieval-reconstruct`だけは新規検索を行う別操作です。最新の完全な引数一覧は常に`py main.py --help`を正本とします。

Researcher Quality Review後は`--researcher-evidence WORKFLOW_ID`で分類と対象を確認し、`--researcher-accept`、`--researcher-accept-limitations`、`--researcher-revise`のいずれかを`--reason`付きで明示します。`--researcher-revise`はRevision Planを保存するだけでProviderを呼びません。障害復旧の`--researcher-recover`と人間判断は別操作です。

Playwrightの修復可能なFinal Gate停止は`--playwright-revise WORKFLOW_ID`で一サイクルだけ再開します。
これはProvider通信障害用の`--playwright-provider-retry`、checkpoint障害用の
`--playwright-recover`、Conclusion Handoff更新用の`--playwright-resume`とは別の操作です。ただし
`CITATION_MAPPING_MISSING`だけが残り、保存済みtraceabilityから一意に再構成できる場合に限り、
`--playwright-recover`がProvider 0件のdeterministic repairを実行します。このlocal repairは
`--playwright-revise`の回数を消費しません。
