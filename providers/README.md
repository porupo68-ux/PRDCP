# Providers

全Agentが使用する共通Provider Interfaceです。

- `base.py`: Provider契約
- `mock_provider.py`, `mock/`: API不要の決定論的Provider
- `openrouter_provider.py`: OpenRouter接続

特定Layer専用のProviderコピーは作成せず、`config/settings.py`から選択します。
