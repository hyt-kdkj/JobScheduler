# V2VSimulator-revision2 のジョブ定義

2026-11-17更新：Proposeの既定報酬は`packet-prr-difference-reward3`（自送信の期待PRR−3×他パケットのPRR損失の和、各パケットの受信対象数で正規化、固定÷10なし）です。候補＋現在資源のNN、17枠の周辺要約、softmax温度0.5、協調Q低下予算2.5を維持し、CPU集計とReplay準備の高速化を有効にします。学習は250,000 step、最終slot249999です。εと学習率は実optimizer更新回数に基づいて減衰します。ジョブ名に`u250k1`、モデル保存名に`u250k1`を付け、旧scheduleのモデルと区別します。通常50,000 step×15 seed、詳細100,000 step、通信warmupはHighway 10,000／Urban 20,000 step、method 1/2の学習参照先method 0は維持します。バッチ更新はジョブ投入を行いません。

全JSONの`defaults.project`は`V2VSimulator-revision2`です。現行methodは0・1・2です。

| ディレクトリ | 内容 |
|---|---|
| `method-0/` | 基底モデルの学習・通常評価・詳細解析・特徴量比較 |
| `method-1/` | 旧method 11：累積確率95%・最大8候補の予告、単一移動元、移動上限10ポイント |
| `method-2/` | 旧method 15：同じ予告、最大4移動元、移動総量上限20ポイント |

method 1/2は同じmethod 0モデルを参照します。各`train_propose.json`も`--method 0`を指定するため、学習は一つのバッチだけを使います。
総学習期間は250,000 step、最終checkpointはslot 249999です。学習開始10,000 step、100 stepごとに1更新、target同期2,000 step・学習ログ1,000 step、replay 5,000 stepです。履歴長は全バッチで`--step 20`に固定しています。観測は1 ms間隔です。通常評価は評価50000 step・seed 10001〜10015の15種類、詳細解析は評価100000 step・seed 10000。通信warmupは両モードともHighway 10000／Urban 20000 stepです。1エピソードは1000 stepの連続集計です。旧20 msモデルは互換性がないため再学習が必要です。履歴長・Encoder・シナリオ・台数・学習ε profile・モデル参照先・checkpointは各JSONを参照してください。

εはinitial-0.5で0.5を400更新まで保持し、2000更新まで線形に0.02へ減衰（initial-0.3も選択可）。学習率は1e-4を1000更新まで保持し、2200更新までcosineで5e-5へ減衰します。未更新の区間では進みません。250,000 stepで通常2400更新、εの減衰は約50k–210k step、学習率は約110k–230k stepです。

共通の`evaluate_baseline.json`・`evaluate_baseline_analysis.json`はSPS/IPMI用です。
評価・描画はhighway 120・360台とurban 200・500台の4交通条件です。360台・500台のPropose評価には、それぞれhighway 120台・urban 200台で学習したモデルを使用します。
`plot.json`・`plot_analysis.json`はmethod 0/1/2を比較し、`plot_baseline_analysis.json`はSPS/IPMIを描画します。比較対象は`parameters.methods`で指定します。
QV測定の専用バッチは削除しました。現在は全15バッチです。

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
