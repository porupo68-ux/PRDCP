# Providers

全Agentが使用する共通Provider Interfaceです。

- `base.py`: Provider契約
- `mock_provider.py`, `mock/`: API不要の決定論的Provider
- `openrouter_provider.py`: OpenRouter接続
- `openrouter_capabilities.py`: 公開Model/Endpoint metadataによる無課金Preflight

特定Layer専用のProviderコピーは作成せず、`config/settings.py`から選択します。

OpenRouter Providerは全生成で`response_format=json_schema`、`strict=true`、`provider.require_parameters=true`を維持します。Preflightは`response_format`と`structured_outputs`を同じ稼働Endpointが扱えることを予約・有料POST前に検証します。aliasは実体modelへ解決し、metadata取得不能はUNKNOWNとしてfail closedします。Capability確認はchat completionではありません。

LLMとRetrievalは別Providerです。`--provider`はLLMだけを上書きし、検索は`PRDCP_RETRIEVAL_PROVIDER`を使用します。LLM予約は`provider_call_reservations/`、検索予約は`retrieval_call_reservations/`へ分離し、Recoveryは保存済みResult、Context hash、Authorization、Reservationを照合して二重送信を防止します。

Structured Output Schemaは送信直前に共通strict normalizerを通します。全objectをclosedにし、全propertyをrequiredにする一方、自由形式dictへ機械的に`additionalProperties: false`を設定しません。Provider応答はPydantic検証前にobject rootと有限JSON数値を検査し、無効な本文は保存せずhash・長さ・不正pathだけをError PMPへ記録します。
