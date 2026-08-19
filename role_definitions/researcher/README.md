# Researcher Role Definitions

`researcher/agents/*.py`と`researcher/manager.py`に対応する9 RDです。

Manager、Expert、Academic、Government、News、Public Opinion、Politician、Industry、Quality Reviewerを定義します。登録の正本は`../registry.json`です。

Quality ReviewerはfindingをEvidence sufficiency、hard integrity、unclassifiedへ分けます。Managerはその結果をHuman Evidence Gateへ渡し、人間判断、Provider authorization、下流Handoffを別々の監査可能な境界として扱います。
