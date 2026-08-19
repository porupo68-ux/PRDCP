# Providers

全Agentが使用する共通Provider Interfaceです。

- `base.py`: Provider契約
- `mock_provider.py`, `mock/`: API不要の決定論的Provider
- `openrouter_provider.py`: OpenRouter接続
- `openrouter_capabilities.py`: 公開Model/Endpoint metadataによる無課金Preflight

特定Layer専用のProviderコピーは作成せず、`config/settings.py`から選択します。

OpenRouter Providerは全生成で`response_format=json_schema`、`strict=true`、`provider.require_parameters=true`を維持します。Preflightは`response_format`と`structured_outputs`を同じ稼働Endpointが扱えることを予約・有料POST前に検証します。aliasは実体modelへ解決し、metadata取得不能はUNKNOWNとしてfail closedします。Capability確認はchat completionではありません。
