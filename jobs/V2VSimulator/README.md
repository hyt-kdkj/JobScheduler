# V2VSimulator のジョブ定義

全11バッチの実行プロジェクトは `V2VSimulator` です。各SSH先で `~/workspace/V2VSimulator` のソースと必要なモデルを参照します。

| ジョブ定義 | 内容 |
|---|---|
| `method-0/train_propose.json` | Highway・UrbanのPropose学習 |
| `method-0/evaluate_propose*.json` | Proposeの通常評価、詳細解析、特徴量比較 |
| `evaluate_baseline_analysis_*.json` | 密度別のSPS・IPMI詳細解析 |
| `plot.json`・`plot_analysis_*.json` | 通常評価・密度別詳細解析の比較図 |

ProposeはCAM履歴、履歴長20、報酬 `exp-distance-packet-prr-difference-reward5` を使用します。学習は250,000 stepで、評価・描画には現行モデルのslot 249999が必要です。密度別解析のモデルはHighway 120台・Urban 200台の学習結果を参照します。評価と描画の温度は0.25です。

```bash
# JobScheduler内で実行
python3 scheduler.py submit-batch jobs/V2VSimulator/method-0/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/method-0/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/plot_analysis_low_density.json --dry-run
```

投入するときは `--dry-run` を外します。バッチ間の依存関係は自動登録されません。各バッチの実行条件はJSONを参照してください。
