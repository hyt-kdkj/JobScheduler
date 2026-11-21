# V2VSimulator のジョブ定義

このディレクトリの実行プロジェクトは `V2VSimulator` です。各SSH先で `~/workspace/V2VSimulator` のソースと必要なモデルを参照します。

| ディレクトリ / ジョブ定義 | 内容 |
|---|---|
| `baseline/evaluate_baseline_analysis_*.json` | 密度別のSPS・IPMI詳細解析 |
| `method-0/train_propose.json` | Highway・UrbanのPropose学習（`camhist3`） |
| `method-0/evaluate_propose*.json` | Proposeの通常評価、密度別詳細解析、WP1-A（`margin_stability`）、WP1-B（`two_stage_softmax`） |
| `plot/plot*.json` | 通常評価・密度別詳細解析・WP1-B二段階選択の比較図 |
| `wp2/wp2_*.json` | WP2（`camhist4`）の学習・密度別詳細解析・`two_stage_softmax`解析・描画 |
| `wp3/wp3_*.json` | WP3（`camhist3` + 3頭分解・再選択Replay・Expected-Softmax TD）の学習・密度別詳細解析・描画 |

ProposeはCAM履歴、履歴長20、報酬 `exp-distance-packet-prr-difference-reward5` を使用します。全バッチは `--path-layout v2` で短縮レイアウトへ保存します。学習は250,000 step（学習温度0.25）で、評価・描画には現行モデルのslot 249999（`--training-q-temperature 0.25`）が必要です。密度別解析のモデルはHighway 120台・Urban 200台の学習結果を参照します。評価と描画の温度は0.25です。

```bash
# JobScheduler内で実行
python3 scheduler.py submit-batch jobs/V2VSimulator/baseline/evaluate_baseline_analysis_low_density.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/method-0/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/plot/plot_analysis_low_density.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/wp2/wp2_evaluate_propose_two_stage_analysis_low_density.json --dry-run
```

投入するときは `--dry-run` を外します。バッチ間の依存関係は自動登録されません。各バッチの実行条件はJSONを参照してください。

method-0 の二段階選択を高密度で比較するときは、
`method-0/evaluate_propose_two_stage_analysis_high_density.json`、
`method-0/plot_two_stage_analysis_high_density.json` の順に実行します。

## WP2 と WP3 の低密度バッチ

| 実験 | 学習 | 低密度解析 | 描画 |
|---|---|---|---|
| WP2 `camhist4` | `wp2/wp2_train_propose.json` | `wp2/wp2_evaluate_propose_analysis_low_density.json` | `wp2/wp2_plot_analysis_low_density.json` |
| WP2 `camhist4` × `two_stage_softmax` | （上記WP2モデルを使用） | `wp2/wp2_evaluate_propose_two_stage_analysis_low_density.json` | `wp2/wp2_plot_two_stage_analysis_low_density.json` |
| WP3 `camhist3` | `wp3/wp3_train_propose.json` | `wp3/wp3_evaluate_propose_analysis_low_density.json` | `wp3/wp3_plot_analysis_low_density.json` |

各実験とも Highway 120台、Urban 200台を学習→解析→描画の順に実行します。WP2 × `two_stage_softmax` は `hold_margin=0` × `band ∈ {unbounded, 0.5, 1.0, 2.0}` の8条件を評価・描画します。WP3 は3頭分解、再選択 Replay 25%、Expected-Softmax TD を有効にします。解析は既存の SPS/IPMI 測定キャッシュを参照します。

## WP2 と WP3 の高密度バッチ

| 実験 | 高密度解析 | 描画 |
|---|---|---|
| WP2 `camhist4` | `wp2/wp2_evaluate_propose_analysis_high_density.json` | `wp2/wp2_plot_analysis_high_density.json` |
| WP2 `camhist4` × `two_stage_softmax` | `wp2/wp2_evaluate_propose_two_stage_analysis_high_density.json` | `wp2/wp2_plot_two_stage_analysis_high_density.json` |
| WP3 `camhist3` | `wp3/wp3_evaluate_propose_analysis_high_density.json` | `wp3/wp3_plot_analysis_high_density.json` |

Highway 360台は Highway 120台で、Urban 500台は Urban 200台で学習した各 WP の checkpoint を読み込みます。評価条件ごとに解析、描画の順に実行します。
