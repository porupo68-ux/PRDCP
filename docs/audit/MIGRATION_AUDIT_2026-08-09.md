# PRDCP v2 Migration Audit

監査日: 2026-08-09

## 1. Scope

次の5 Prototypeの`prdcp/`を相対パスとSHA-256で比較しました。

- Producer RDLoader
- Researcher RDLoader
- Deliberation RDLoader
- Conclusion
- Playwright

`__pycache__`、`.pyc`、`desktop.ini`は構成物から除外しました。

## 2. Hash Audit

- Union path: 555
- 5 Prototypeすべてで完全一致: 285
- 5 Prototypeすべてに存在し内容差分あり: 6
- 一部Prototypeだけに存在し内容は非衝突: 264
- 一部Prototype間で同一パス・異内容の衝突: 0

コード上の差分は`cli_app/commands.py`、`config/settings.py`、`tests/unit/test_formatter.py`、Layer名だけが異なる5つのDiscord botに集中していました。残りの差分は主に実行時データとログです。

## 3. Canonical Decisions

- `main.py`、`runtime.py`、`config/`、`common/`、`specifications/`、`providers/`、`storage/`実装を各1コピーに統合
- 5つのDiscord botは本文が同一だったため`discord_app/bot.py`へ統合
- CLIのDiscord起動先を`discord_app.bot.create_bot`へ固定
- Producer版`settings.py`に残っていた一時的なdebug printを除去し、他4 Prototypeと同じ設定実装へ統一
- Playwright版の拡張Formatter testをCanonical testとして採用
- Channel Router testを`tests/discord/`へ配置
- 31 Agent RDを`role_definitions/<layer>/`に維持し、共通RD LoaderとSTRICT modeを維持
- PMPとRegistryは`specifications/common/`の1コピーを正本として維持
- 添付されたFile Placement PlanとMigration Mapを`docs/migration/`へ保存
- 独立責務を持つディレクトリにREADMEを追加

## 4. Storage Migration

5 PrototypeのRuntime Dataは、同一パスに異内容の衝突がないことを確認して`storage/data/`へ和集合しました。

- Migrated runtime files: 257
- Workflows: `storage/data/workflows/<layer>/`
- Deliveries: `storage/data/deliveries/<workflow_id>/`
- Artifacts / Outbox: 既存Repository互換のため維持
- Legacy logs: 稼働中プロセスの追記対象だったため移行せず、Canonical `logs/`を空の状態で開始

移行済みWorkflow内に記録された旧絶対パスは履歴データとして変更していません。新規実行はCanonical Storageへ書き込みます。

## 5. Security Hygiene

旧`.env.example`に実Credential形式の値が含まれていたため、Canonical `.env.example`では`DISCORD_BOT_TOKEN`と`OPENROUTER_API_KEY`を空欄にしました。実設定は`.gitignore`対象の`.env`だけで管理します。

旧PrototypeとZIPには過去の値が残る可能性があるため、Discord tokenとOpenRouter API keyのローテーションを推奨します。

## 6. Verification

### Doctor

`READY`、fail=0。31 RD、32 Agent ID + delivery endpoint、31 Model entry、29 Message Type、7 Status、4 Handoffを確認しました。

任意機能に関するwarnは、検証環境でDiscord package/tokenとOpenRouter model設定を使用しなかったための3件です。

### Automated Tests

- Existing unit/integration suite: 155 PASS
- Discord routing suite: 11 PASS
- Total: 166 PASS

### Mock E2E

Workflow `cc0d9eee-2ee8-4a15-8df9-d35070d099be`で次を完走しました。

Producer → Researcher → Deliberation → Conclusion → Human Selection → Playwright → Deliveries

最終Delivery 6ファイルの生成を確認しました。

## 7. Legacy Preservation

旧Prototypeは統合成功後も削除していません。監査開始時はProducer側のApplication logが実行中プロセスにより使用されていたため移動を保留しました。その後、プロセス終了とログ解放を確認し、5 Prototypeを`archive/legacy_prototypes/`へ移して保全しました。
