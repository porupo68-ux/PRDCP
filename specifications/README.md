# Specifications

PRDCP全体のCanonical Contractです。PMP Message Type、Metadata Status、Agent/RD/Model Registry、Cross-layer Handoffを`common/`配下の機械可読ファイルで定義します。

全Layerはこの1コピーを参照し、Layer別のPMPコピーは作成しません。契約を変更した場合は`py main.py --doctor`と全テストを実行してください。
