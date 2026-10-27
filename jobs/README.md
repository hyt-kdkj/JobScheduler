# Gitで管理するジョブ定義

ジョブ定義は次の11ファイルです。`JobScheduler/`内で実行します。

| ファイル | 内容 | ジョブ数 |
|---|---|---:|
| `train_transformer.json` | Transformer学習（method 4） | 16 |
| `train_lstm.json` | LSTM学習（method 1） | 16 |
| `evaluate_transformer.json` | Transformer通常評価（method 4） | 32 |
| `evaluate_transformer_analysis.json` | Transformer詳細解析（method 4） | 32 |
| `evaluate_transformer_feature_ablation.json` | Transformer特徴量の無効化比較（method 4、step 10・20・30） | 12 |
| `evaluate_lstm.json` | LSTM通常評価（method 4） | 32 |
| `evaluate_lstm_analysis.json` | LSTM詳細解析（method 4） | 32 |
| `plot.json` | SPS・IPMI・Transformerの比較グラフ（通常評価） | 4 |
| `plot_analysis.json` | SPS・IPMI・Transformerの比較グラフ（詳細解析） | 4 |
| `plot_with_lstm.json` | SPS・IPMI・Transformer・LSTMの比較グラフ（通常評価） | 4 |
| `plot_with_lstm_analysis.json` | SPS・IPMI・Transformer・LSTMの比較グラフ（詳細解析） | 4 |

特徴量比較以外はhighway / urban、step 10〜80（10刻み）を対象にします。評価・plotはhighway 120・360台、urban 200・500台です。学習の車両数はシミュレータの既定値を使います。既存のmethod指定を維持しているため、LSTM学習はmethod 1、LSTM単独評価はmethod 4、`plot_with_lstm.json`のLSTMはmethod 1です。method 4を評価するには対応する学習済みモデルを別途用意するか、学習定義のmethod指定を変更してください。

通常評価は`--analysis-only`なし（既定15 seed）、詳細解析は`--analysis-only`あり（既定seed 100）です。測定データはそれぞれ`save_results/evaluation/`と`save_results/analysis/`に保存されます。

`V2VSimulator/Simulator/README.md`に従い、`plot.json`は`--agents SPS IPMI Transformer`で3手法、`plot_with_lstm.json`は`--agents SPS IPMI Transformer LSTM`で4手法を同じグラフに描画します。`plot.json`はmethod 4、`plot_with_lstm.json`は`--transformer-method 4 --lstm-method 1`を指定し、plot用の各ファイルは4ジョブです（highway / urban × 各2種類の車両数）。各ジョブは`--step 10 20 30 40 50 60 70 80`をまとめて渡し、全履歴長を同じグラフ内で比較します。stepごとにジョブを分割しません。通常評価は`plot.json`・`plot_with_lstm.json`、詳細解析は`plot_analysis.json`・`plot_with_lstm_analysis.json`を使います。LSTMあり版は両方ともTransformer method 4・LSTM method 1です。不足する測定データは自動で評価します。比較対象の対応モデルをサーバー側に用意してください。片方の評価モードやシナリオだけを実行する場合は、対象ファイルの`jobs`を編集してください。

```bash
# 内容確認（登録・SSH接続なし）
python3 scheduler.py submit-batch jobs/plot.json --dry-run
python3 scheduler.py submit-batch jobs/plot_with_lstm.json --dry-run
python3 scheduler.py submit-batch jobs/plot_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/plot_with_lstm_analysis.json --dry-run

# 学習・評価・描画の投入例
python3 scheduler.py submit-batch jobs/train_transformer.json
python3 scheduler.py submit-batch jobs/evaluate_transformer.json
python3 scheduler.py submit-batch jobs/evaluate_transformer_analysis.json
# LSTMなし：通常評価 / 詳細解析
python3 scheduler.py submit-batch jobs/plot.json
python3 scheduler.py submit-batch jobs/plot_analysis.json
# LSTMあり：通常評価 / 詳細解析
python3 scheduler.py submit-batch jobs/plot_with_lstm.json
python3 scheduler.py submit-batch jobs/plot_with_lstm_analysis.json
```

ジョブ名には手法名を含め、通常評価と詳細解析を区別します。登録済みジョブの手法は`status`の`AGENTS`列でも確認できます。定義ファイルの整理は登録済み・実行中のジョブには影響しません。

`python3 scheduler.py export`で履歴を`jobs/history.json`に書き出せます。`export --refresh`はSANの共有キューとリモート状態を取得してから書き出し、結果のrsyncは行いません。履歴JSONは記録用で、一括投入の入力にはなりません。

各PCは独立したローカルSQLiteを持ち、SAN上の共有キューを参照します。稼働状態の同期はSANで行い、定義・履歴はGitで管理できます。commit・pushは手動で行います。

## 特徴量の影響を比較する

`evaluate_transformer_feature_ablation.json` は highway 120・360台、urban 200・500台 × step 10・20・30 の12ジョブです。highway は120台で学習したモデル、urban は200台で学習したモデルを使用します。通常入力＋5特徴量の個別ゼロ置換を各15 seedで比較し、結果は `save_results/evaluation/` 以下の `feature_ablation/` に `summary.csv`・`per_seed.csv`・`manifest.json` として保存されます。これはPERなどの性能変化による比較で、Q値への直接の寄与率ではありません。

```bash
python3 scheduler.py submit-batch jobs/evaluate_transformer_feature_ablation.json --dry-run
python3 scheduler.py submit-batch jobs/evaluate_transformer_feature_ablation.json
```
