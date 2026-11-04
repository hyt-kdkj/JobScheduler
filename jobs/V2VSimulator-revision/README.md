# V2VSimulator-revision のジョブ定義

`V2VSimulator-revision/Simulator` の現行CLIに合わせた定義です。Propose（旧Transformer method 4）を使用し、履歴長は **step 10・20・30・40** に限定します。旧Transformer名の定義はPropose名に変更し、廃止されたLSTMの学習・評価・比較定義は削除しました。

| ファイル | 内容 | ジョブ数 |
|---|---|---:|
| `train_propose.json` | Propose学習 | 8 |
| `evaluate_propose.json` | Propose通常評価 | 16 |
| `evaluate_propose_analysis.json` | Propose詳細解析 | 16 |
| `evaluate_baseline.json` | SPS・IPMI通常評価 | 4 |
| `evaluate_baseline_analysis.json` | SPS・IPMI詳細解析 | 4 |
| `evaluate_propose_feature_ablation.json` | Propose特徴量の個別無効化（詳細解析） | 16 |
| `plot.json` | SPS・IPMI・Proposeの通常評価・比較グラフ | 4 |
| `plot_analysis.json` | SPS・IPMI・Proposeの詳細解析・比較グラフ | 4 |

学習はhighway 120台・urban 200台（シミュレータの既定値）、評価・描画はhighway 120・360台、urban 200・500台です。評価台数によらず、各シナリオの学習台数のモデルを使用します。methodは現行CLIの既定値4を使います。

通常評価は既定15 seed（43〜57）、詳細解析は`--analysis-only`で既定seed 100を使用します。特徴量比較も`--analysis-only`を維持し、1条件につき通常入力と各特徴量のゼロ置換を比較します。測定時間はスクリプトの設定に従います。

baseline評価は学習済みモデル不要で、1ジョブでSPS・IPMIを測定します。通常評価・詳細解析ともに`--refresh-baseline-cache --refresh-results`を指定し、既存結果を再利用せず再測定して更新します。履歴長stepに依存しないため、step別に分割せず、highway 120・360台／urban 200・500台の4ジョブにしています。

描画は1ジョブに`--step 10 20 30 40`をまとめて渡します。不足する測定は自動で追加します。各stepの学習済みモデルを用意してから評価・描画を実行してください。

`JobScheduler/`で実行します。全JSONに`defaults.project`を設定済みです。

```bash
# 定義を確認（登録・SSH接続なし）
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/train_propose.json --dry-run

# 学習を投入
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/train_propose.json

# baseline評価（学習不要）：通常評価 / 詳細解析
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_baseline.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_baseline_analysis.json

# 学習完了後に評価・描画を投入
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_propose.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/plot.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/plot_analysis.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_propose_feature_ablation.json
```
