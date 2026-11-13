# method 10：全ての有効予告に基づく、Q低下制約付き確率移動

再選択前の最後のBasic CAMで上位4候補と元の選択確率を通知し、RC=0で最新Qを計算する。
受信済みの全ての有効な予告を集計し、Basic・Extra両flowを参照する。
最近傍3台には限定せず、移動先の別の競合も含めて重なりの下限・上限を計算する。
method 9と同じく、重なりが減る根拠と平均Qの低下予算を満たす場合だけ、抽選確率の一部を移す。
確定資源の事前通知は送らない。

**同条件のmethod 0の学習済みモデルを使用し、追加学習は不要。**
`train_propose.json`は共通method 0を学習する補助バッチで、`--method 0`を指定する。
method-0〜10のtrainは同じモデル保存先を使うため、未学習の場合に一つだけ投入する。
既存モデルがあれば評価から開始できる。

## 実行条件

- 履歴長20・30、Encoder=Transformer、initial-0.5、mode=safe、option=A、XLA=off。
- Highwayは120台で学習したmethod 0を120・360台で評価する。
- Urbanは200台で学習したmethod 0を200・500台で評価する。
- 読み込むモデルは150,000学習stepsのslot 149999に固定する。
- 実行先は`hosts.json`の利用可能な全サーバーから選ぶ。除外GPUと同時実行上限は維持する。

| ファイル | 内容 | 展開ジョブ数 |
|---|---|---:|
| `train_propose.json` | 共通method 0の学習。既存モデルがあれば不要 | 4 |
| `evaluate_propose.json` | method 10の通常評価、15 seed（43〜57）、評価5,000 steps | 8 |
| `evaluate_propose_analysis.json` | method 10の詳細解析、seed100、評価8,000 steps | 8 |

## 内容確認と投入

JobSchedulerディレクトリで実行する。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-10/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-10/evaluate_propose_analysis.json --dry-run
# method 0のモデルが未学習の場合のみ
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-10/train_propose.json --dry-run
```

投入時は対象コマンドから`--dry-run`を外す。バッチ間の依存関係は自動登録されない。
method 9・10を実装したソースをサーバー側に配置してから使用する。ソースは自動転送されない。
ファイル変更とdry-runだけではジョブは登録・実行されず、登録済みジョブの名前・コマンドも変わらない。

描画は親ディレクトリの`plot.json`／`plot_analysis.json`を使い、先頭の
`parameters.methods`に対象番号を含める。例：`[0, 8, 9, 10]`。
評価完了後に描画する。同条件の比較図・CSVは出力先を共有するため、描画は同時投入しない。
方式別の元測定は、プロトコル識別子とモデル情報で区別する。

詳細は対象プロジェクトの`markdown/Method9_Method10_Proposals.md`を参照。
