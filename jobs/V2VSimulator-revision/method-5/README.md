# method 5：協調情報を入力に含めて学習する

受信したCAM予告を観測に加え、EncoderとQヘッドを一緒に学習する方式。
**method 5専用モデルの新規学習が必要。** `train_propose.json`は`--method 5`を指定する。
method 0のモデルへ読み替えたり、method 1/2の補正枝だけを学習したりするバッチではない。
報酬関数・集計・割引・dense replayはmethod 0と共通。

既存method別バッチに合わせて、initial-0.5・Transformerのみ、履歴長20/30、
mode=safe、option=A、XLA=off。実行先は利用可能なサーバーから自動選択する。
学習はhighway120台／urban200台で150,000 steps。
評価はそれぞれ120/360台、200/500台で、対応するmethod 5のslot149999を読む。

| ファイル | 内容 | 展開ジョブ数 |
|---|---|---:|
| `train_propose.json` | method 5専用モデルの学習 | 4 |
| `evaluate_propose.json` | method 5の通常評価、15 seed（43〜57） | 8 |
| `evaluate_propose_analysis.json` | method 5の詳細解析、seed100・評価8,000 steps | 8 |

## 定義の確認と投入

JobSchedulerディレクトリで実行する。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-5/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-5/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-5/evaluate_propose_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/plot.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/plot_analysis.json --dry-run
```

投入時は対象コマンドから`--dry-run`を外す。
学習→評価→描画の順に、先のバッチが完了してから次を投入する。
バッチ間の依存関係は自動登録されない。
ソースは自動転送されないため、method 5を実装したソースをサーバー側へ配置してから使用する。

method 5のモデルは`propose_method5_step20`等の専用保存先を使う。
描画の比較対象にはmethod 0の同条件モデルも必要。
描画プログラムは不足する測定を実行するため、重複を避けて評価完了後に描画する。
他methodの同条件の描画と比較図・比較CSVの出力先を共有するので、同時投入しない。
方式別の元測定レコードは独立して保存される。

通信性能の改善は学習後の評価で確認する。実装・報酬・23 Bの予告形式・計算時間の詳細は、
対象プロジェクトの`Simulator/Method5.md`を参照。
ファイル作成とdry-runだけではジョブは登録・実行されない。

描画用JSONは親ディレクトリに集約しています。このディレクトリ内のJSONは学習・評価用です。

共通描画JSONの先頭にある `parameters.methods` に対象の番号を追加してください（例：`[0, 3, 4]`）。コマンドの編集は不要です。
