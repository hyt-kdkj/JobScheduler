# method 8：予告入りCAMの最近傍3台・両flow・上位4候補版

再選択前の最後のBasic CAMで上位4候補と元の選択確率を通知する。
自分のRC=0で最新Qを計算し、有効な予告入りCAMの送信元から最近傍3台を選ぶ。
選んだ車両のBasic・Extra両flowの有効な予告を参照し、資源の重なりに応じて
抽選確率を補正する。3台を選んでから重なりを評価し、重なりがない場合も
4台目へ繰り上げない。確定資源の事前通知は送らない。

**同条件のmethod 0の学習済みモデルを使用し、追加学習は不要。**
旧method 8の協調RLモデルは使わない。
`train_propose.json`は必要な場合だけ共通のmethod 0を学習する補助バッチであり、
コマンドは`--method 0`を指定する。method-0〜8のtrainは同じモデル保存先を使うため、
未学習の場合に一つだけ投入する。既存モデルがあれば評価から開始できる。

## 実行条件

- 履歴長20・30、Encoder=Transformer、initial-0.5、mode=safe、option=A、XLA=off。
- Highwayは120台で学習したmethod 0を120・360台で評価する。
- Urbanは200台で学習したmethod 0を200・500台で評価する。
- 読み込むモデルは150,000学習stepsのslot 149999に固定する。

| ファイル | 内容 | 展開ジョブ数 |
|---|---|---:|
| `train_propose.json` | 共通method 0の学習。既存モデルがあれば不要 | 4 |
| `evaluate_propose.json` | method 8の通常評価、15 seed（43〜57） | 8 |
| `evaluate_propose_analysis.json` | method 8の詳細解析、seed100・評価8,000 steps | 8 |

## 内容確認と投入

JobSchedulerディレクトリで実行する。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-8/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-8/evaluate_propose_analysis.json --dry-run
# method 0のモデルが未学習の場合のみ
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-8/train_propose.json --dry-run
```

投入時は対象コマンドから`--dry-run`を外す。バッチ間の依存関係は自動登録されない。
method 7・8を実装したソースをサーバー側に配置してから使用する。ソースは自動転送されない。
ファイル作成とdry-runだけではジョブは登録・実行されない。

描画は親ディレクトリの`plot.json`／`plot_analysis.json`を使い、先頭の
`parameters.methods`に対象番号を含める。例：`[0, 5, 6, 7, 8]`。
評価完了後に描画する。同条件の比較図・CSVは出力先を共有するため、描画は同時投入しない。
方式別の元測定と旧method 8の結果は、プロトコル識別子とモデル情報で区別する。

詳細は対象プロジェクトの`markdown/Method7_Method8.md`を参照。
PRR向上の実測比較はまだ行っていない。
