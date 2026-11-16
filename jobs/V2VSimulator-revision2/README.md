# V2VSimulator-revision2 のジョブ定義

2026-11-16更新：Proposeの既定報酬は`packet-prr-difference-reward3`（自送信の期待PRR−3×他パケットのPRR損失の和、各パケットの受信対象数で正規化、固定÷10なし）です。候補＋現在資源のNN、17枠の周辺要約、softmax温度0.5、協調Q低下予算2.5を維持し、CPU集計とReplay準備の高速化を有効にします。ジョブ名は`prrdiff3_packedv4_T05`、モデル名は`per-packet-prr-candidates-v4`で旧リンク数報酬と区別します。通常100,000 step×10 seed、詳細100,000 step、method 1/2の学習参照先method 0は維持します。バッチ更新はジョブ投入を行いません。

全JSONの`defaults.project`は`V2VSimulator-revision2`です。現行methodは0・1・2です。

| ディレクトリ | 内容 |
|---|---|
| `method-0/` | 基底モデルの学習・通常評価・詳細解析・特徴量比較 |
| `method-1/` | 旧method 11：累積確率95%・最大8候補の予告、単一移動元、移動上限10ポイント |
| `method-2/` | 旧method 15：同じ予告、最大4移動元、移動総量上限20ポイント |

method 1/2は同じmethod 0モデルを参照します。各`train_propose.json`も`--method 0`を指定するため、学習は一つのバッチだけを使います。
総学習期間は200,000 step、最終checkpointはslot 199999です。学習開始10,000 step、40 stepごとに2更新、target同期・学習ログ1,000 step、replay 5,000 stepです。履歴長は全バッチで`--step 20`に固定しています。観測は1 ms間隔です。通常評価はwarmup 10000 step・評価100000 step・seed 10001〜10010の10種類、詳細解析はwarmup 10000 step・評価100000 step・seed 10000です。1エピソードは1000 stepの連続集計です。旧20 msモデルは互換性がないため再学習が必要です。履歴長・Encoder・シナリオ・台数・学習ε profile・モデル参照先・checkpointは各JSONを参照してください。

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

## 詳細解析の並列実行

詳細解析の4バッチ（共通baselineとmethod 0/1/2）は、4交通条件×5方式＝20ジョブへ展開する。SPSとIPMIも独立したジョブとし、既存の各サーバー最大2ジョブ・1 GPU最大2ジョブの枠へ割り当てる。プロセス内のBLAS/OpenMPは1 thread、TensorFlowはintra-op 2・inter-op 1に制限し、複数プロセスでCPU threadが増えすぎるのを防ぐ。同じ測定を重複投入しない。

各バッチを`submit-batch ... --queue-only`で登録し、`python3 scheduler.py dispatch`で空き枠へ割り当てる。登録は実行対象を増やす操作なので、内容確認には従来どおり`--dry-run`を使う。共有GPUやCPUの競合により短縮率は変わる。全体の同時実行上限と他プロジェクトの設定は変更していない。
