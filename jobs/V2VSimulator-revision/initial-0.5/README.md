# initial-0.5 のジョブ定義

追加の構成：150,000 steps、初期ε0.5、5,000 stepsごとにεを減衰します。学習率は40,000・80,000・120,000 stepsで8e-5・6e-5・4e-5へ減衰し、120,000〜150,000 stepsは4e-5を維持します。2e-5の区間は設けません。モデル・結果には `_epsilon_high_exploration` が付きます。 εは120,000 stepsで0.05に到達します。

EncoderはTransformer・no、履歴長は10・20・30、学習シナリオはhighway 120台・urban 200台です。評価はhighway 120・360台／urban 200・500台です。各ファイルの `epsilon_profile` は `initial-0.5` だけに固定しています。期間・学習率はシミュレータ側の対応プロファイルから読み込みます。

| ファイル | ジョブ数 |
|---|---:|
| `train_propose.json` | 12 |
| `evaluate_propose.json` | 24 |
| `evaluate_propose_analysis.json` | 24 |
| `evaluate_propose_feature_ablation.json` | 24 |
| `plot.json` | 8 |
| `plot_analysis.json` | 8 |

`JobScheduler/`で実行します。評価・描画は対応モデルの学習完了後に実行してください。既存の初期ε0.3のモデルを利用する場合、再学習は不要です。

```bash
# 確認のみ（登録・SSH接続なし）
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/train_propose.json --dry-run

# 学習を投入
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/train_propose.json

# 評価・詳細解析・描画
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/evaluate_propose.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/evaluate_propose_analysis.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/plot.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/plot_analysis.json
```

SPS・IPMIのbaseline評価は親ディレクトリの共通JSONを使います。[全体の説明](../README.md)を参照してください。

評価対象は150,000学習stepsまで（slot上限149999）です。以前の170,000 stepsのモデル・測定結果は評価用の自動選択から除外し、上限を超えるslotの明示指定はエラーになります。
