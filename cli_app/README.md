# CLI App

`main.py`から呼び出される唯一のCLI実装です。

- `arguments.py`: 引数定義
- `commands.py`: コマンドの実行と各Managerへの接続
- `diagnostics.py`: `--doctor`の整合性検査
- `events.py`: Runtime Eventの出力
- `output.py`: 人間向け表示とJSON表示

CLI固有の表示・引数処理だけを置き、Agentロジックは各Layerに残します。

Researcher Quality Review後は`--researcher-evidence WORKFLOW_ID`で分類と対象を確認し、`--researcher-accept`、`--researcher-accept-limitations`、`--researcher-revise`のいずれかを`--reason`付きで明示します。`--researcher-revise`はRevision Planを保存するだけでProviderを呼びません。障害復旧の`--researcher-recover`と人間判断は別操作です。

Playwrightの修復可能なFinal Gate停止は`--playwright-revise WORKFLOW_ID`で一サイクルだけ再開します。
これはProvider通信障害用の`--playwright-provider-retry`、checkpoint障害用の
`--playwright-recover`、Conclusion Handoff更新用の`--playwright-resume`とは別の操作です。ただし
`CITATION_MAPPING_MISSING`だけが残り、保存済みtraceabilityから一意に再構成できる場合に限り、
`--playwright-recover`がProvider 0件のdeterministic repairを実行します。このlocal repairは
`--playwright-revise`の回数を消費しません。
