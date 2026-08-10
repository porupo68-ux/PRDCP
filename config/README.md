# Config

全Layer共通のRuntime Configurationです。`.env`を読む場所は`settings.py`に限定します。

- `settings.py`: 環境変数の読込と型付き設定
- `models.json`: AgentごとのModel設定仕様
- `agents.json`: Canonical Agent Registry
- `implementation_overrides.json`: 実装上の明示的な例外

秘密情報は`.env`だけに置き、`.env.example`には実トークンやAPI keyを入れません。
