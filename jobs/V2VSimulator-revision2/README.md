# V2VSimulator-revision2 のジョブ定義

全JSONの`defaults.project`は`V2VSimulator-revision2`です。ジョブ名には`revision2_`を付けています。
既存の`../V2VSimulator-revision/`の全54バッチと同じ条件を使い、実行するプロジェクトをrevision2へ切り替えた定義です。

- `method-0/`〜`method-15/`：学習・通常評価・詳細解析。評価専用methodの学習ジョブはmethod 0を指定します。
- `method-0/evaluate_propose_feature_ablation.json`：特徴量比較。
- `evaluate_baseline.json`・`evaluate_baseline_analysis.json`：SPS・IPMIの通常評価・詳細解析。
- `plot.json`・`plot_analysis.json`：method間の比較描画。対象methodは各JSONの`parameters.methods`で指定します。
- `measure_method0_qv_highway.json`：method 0のHighway Q・V測定。

履歴長、Encoder、台数、モデル参照条件、checkpoint、学習ε profileは対応する既存revisionのJSONを引き継いでいます。詳細は各JSONを参照してください。
revision2の自車両内の送信重複の除外、20 ms：50 ms＝20：80の車両割合はシミュレータ側で適用されます。通信仕様は[Methods.md](../../../V2VSimulator-revision2/markdown/Methods.md)を参照してください。

JobSchedulerディレクトリで実行します。

```bash
# 展開確認（登録・SSH接続なし）
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/method-0/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/method-0/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/method-0/evaluate_propose_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision2/plot_analysis.json --dry-run
```

投入するときは対象コマンドの`--dry-run`を外します。バッチ間の依存関係は自動登録されません。
評価対象のモデルはrevision2側に用意してください。評価専用methodは同じrevision2内のmethod 0モデルを使います。

各SSH先に`~/workspace/V2VSimulator-revision2`と`docker-shell.sh`が必要です。コンテナ名は`v2vsimulator-revision2-v2v-dev-1`、コンテナ内の作業先は`/workspace/V2VSimulator/Simulator`です。ソースやモデルの自動転送は行いません。
設定の追加とdry-runでは、ジョブ投入やコンテナの起動は行われません。
