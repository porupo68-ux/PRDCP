# PRDCP Role Definitions

このディレクトリのJSONが、共通RD Loaderで使用する実行時の正本です。

- `registry.json`: Agent IDとRDファイルの一意な対応
- `producer/`: Producer Managerを含む6 RD
- `researcher/`: Research Managerを含む9 RD
- `deliberation/`: Deliberation Managerを含む6 RD
- `conclusion/`: Conclusion Managerを含む5 RD
- `playwright/`: Playwright Managerを含む5 RD
- `SOURCE_REPAIRS.json`: Word原稿からJSON化する際の構文修復記録

RDの`runtime_contract`は、現在のPMP v2.0列挙値とPythonのPydantic Schemaを結び付けます。RD本文に残る将来候補のMessage Typeではなく、実行時にはこの契約だけを使用します。

Agent timeoutの正規ソースは各RDの`runtime_contract.timeout_seconds`だけです。全Agentで600秒以上を必須とし、`configuration.timeout_seconds`や`execution_contract.timeout_seconds`などへの重複定義は禁止します。この値は共通Agent実行層からProviderのリクエストtimeoutまで引き渡されます。

Agent IDまたはファイルを追加・変更した場合は、`registry.json`も更新し、次を実行してください。

```powershell
py -m unittest tests.unit.test_rd_loader -v
py -m unittest discover -s tests -p "test_*.py" -v
```
