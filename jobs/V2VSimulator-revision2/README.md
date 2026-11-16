# V2VSimulator-revision2 のジョブ定義

全JSONの`defaults.project`は`V2VSimulator-revision2`です。現行methodは0・1・2です。

| ディレクトリ | 内容 |
|---|---|
| `method-0/` | 基底モデルの学習・通常評価・詳細解析・特徴量比較 |
| `method-1/` | 旧method 11：累積確率95%・最大8候補の予告、単一移動元、移動上限10ポイント |
| `method-2/` | 旧method 15：同じ予告、最大4移動元、移動総量上限20ポイント |

method 1/2は同じmethod 0モデルを参照します。各`train_propose.json`も`--method 0`を指定するため、学習は一つのバッチだけを使います。
履歴長は全バッチで`--step 20`に固定しています。その他の評価条件は旧method 11/15のバッチを引き継いでいます。履歴長・Encoder・シナリオ・台数・学習ε profile・モデル参照先・checkpointは各JSONを参照してください。

共通の`evaluate_baseline.json`・`evaluate_baseline_analysis.json`はSPS/IPMI用です。
`plot.json`・`plot_analysis.json`はmethod 0/1/2を比較します。比較対象は`parameters.methods`で指定します。
QV測定の専用バッチは削除しました。現在は全14バッチです。

```bash
# JobScheduler内で実行。展開確認だけで登録・SSH接続は行いません。
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/method-0/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/method-1/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/method-2/evaluate_propose_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/plot_analysis.json --dry-run
```

投入するときは`--dry-run`を外します。バッチ間の依存関係は自動登録されません。
各SSH先に`~/workspace/V2VSimulator-revision2`と必要なモデルを配置してください。ソース・モデルは自動転送されません。
revision2の現行仕様は[Methods.md](../../../V2VSimulator-revision2/markdown/Methods.md)を参照してください。過去のmethod番号と現行の番号を混同しないでください。
