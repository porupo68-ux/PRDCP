# CLI App

`main.py`から呼び出される唯一のCLI実装です。

- `arguments.py`: 引数定義
- `commands.py`: コマンドの実行と各Managerへの接続
- `diagnostics.py`: `--doctor`の整合性検査
- `events.py`: Runtime Eventの出力
- `output.py`: 人間向け表示とJSON表示

CLI固有の表示・引数処理だけを置き、Agentロジックは各Layerに残します。
