# Config

全Layer共通のRuntime Configurationです。`.env`を読む場所は`settings.py`に限定します。

- `settings.py`: 環境変数の読込と型付き設定
- `models.json`: AgentごとのModel設定仕様
- `agents.json`: Canonical Agent Registry
- `implementation_overrides.json`: 実装上の明示的な例外

秘密情報は`.env`だけに置き、`.env.example`には実トークンやAPI keyを入れません。

Modelの実効値は各`environment_key`に対応する`.env`またはprocess environmentを正本とします。`display_name`は説明用であり、実行model IDの代替ではありません。OpenRouterのstrict Structured Outputでは`--doctor`が公開Endpoint metadataを監査し、非対応またはUNKNOWNのmodelをProvider送信前に拒否します。新規セットアップのTopic Scout、General Opinion、Research Planner、7 Researcher specialistの推奨Reasoning modelは`google/gemini-3.7-flash`です。

Cycle 029以降、検索はReasoning modelの暗黙機能ではありません。`PRDCP_RETRIEVAL_PROVIDER`、`OPENROUTER_RETRIEVAL_MODEL`、`OPENROUTER_RETRIEVAL_ENGINE`を独立設定し、OpenRouter Web Searchのcitation結果を`retrieval_contexts`へ保存してからStructured Outputを実行します。

`--provider`はLLM Providerだけを一時上書きし、Retrieval Providerは上書きしません。`PRDCP_RETRIEVAL_PROVIDER`が未設定なら起動時の`PRDCP_PROVIDER`を継承しますが、明示設定後は独立です。完全Mock検証では`PRDCP_PROVIDER=mock`と`PRDCP_RETRIEVAL_PROVIDER=mock`の両方を設定してください。本番OpenRouter検索では両方を`openrouter`にし、実行前に`py main.py --doctor`で31 Reasoning modelと8 Retrieval-required Agentを別々に確認します。
