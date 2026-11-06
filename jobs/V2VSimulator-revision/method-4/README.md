# Propose method 4 の基底モデル学習・測定・描画

IDEAS.mdの順位2位・α6：合成干渉の条件付き通知。
**method 0の学習済みモデルを固定して評価する方式で、追加学習は不要。**
`train_propose.json`は、method 4が利用する**method 0の基底モデルを学習する**バッチ。
コマンドは明示的に`--method 0`を指定し、method 4の制御機構自体は学習しない。
method-3とmethod-4のtrainは同じmethod 0の保存先を使うため、基底モデルが
未学習の場合にどちらか一方だけを投入する。既存モデルがあれば学習を省略できる。

既存method-1/2と同じく **initial-0.5・Transformerのみ**、履歴長10・20・30、
mode=safe、option=A、XLA=off。利用可能な全サーバーから実行先を自動選択する。
highwayは120台で学習したモデルを120・360台で、urbanは200台で学習した
モデルを200・500台で評価する。全コマンドは150,000学習stepsの
**slot 149999**を指定し、同条件のmethod 0のモデルを読み込む。
履歴長・Encoder・εプロファイル・モデル互換性の検査は維持する。
method 4用の補正枝ファイルは要求しない。

| ファイル | 内容 | 展開ジョブ数 |
|---|---|---:|
| `train_propose.json` | 共通のmethod 0をhighway 120台／urban 200台、150,000 stepsで学習 | 6 |
| `evaluate_propose.json` | method 4の通常評価、15 seed（43〜57） | 12 |
| `evaluate_propose_analysis.json` | method 4の詳細解析、seed 100・8,000 steps | 12 |
| `plot.json` | SPS・IPMI・method 0・method 4、履歴長10/20/30の比較 | 4 |
| `plot_analysis.json` | 同じ比較の詳細解析版、seed 100 | 4 |

JobSchedulerディレクトリで内容確認する：

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-4/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-4/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-4/evaluate_propose_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-4/plot.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-4/plot_analysis.json --dry-run
```

ジョブを投入するときは対象コマンドから`--dry-run`を外す。
ソースは自動転送されないため、method 3/4の実装をサーバー側へ配置し、
共有プロジェクトの`Simulator/save_model/`に対応するmethod 0の
initial-0.5・slot 149999のモデルを用意する。

評価と描画は別バッチであり、依存関係は自動登録されない。
描画は`--step 10 20 30`をまとめて渡し、不足するSPS・IPMI・method 0・
method 4の測定は描画プログラムが自動で実行する。
重複実行を避けるため、対応する評価が完了してから描画を投入する。

現在の描画プログラムでは、同じシナリオ・台数・Encoder・εの比較図と比較CSVの
保存先を共有する。method-3とmethod-4の同条件の描画は同時投入せず、
比較図を両方残す場合は先の出力を別ディレクトリへ保存してから次を実行する。
最後の比較対象は`comparison_manifest.json`で確認できる。
方式別の元測定レコードは独立して保存される。

結果はmethod 4としてmethod 0や他方式と分離し、通知・失効・fallback等の
指標も保存する。通知は既存300-byte CAM内の13-byte研究用領域を使う。
初期版はBasic CAMで識別・測定した干渉源を対象とし、通常再選択時にだけ候補を制御する。早期再選択は行わない。
PRR向上はまだ未検証。方式の詳細と制約は対象リポジトリの
`IDEAS.md`と`Simulator/COOPERATION.md`を参照する。

ファイル作成・dry-runだけではジョブを登録・実行しない。
