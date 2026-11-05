# V2VSimulator-revision のジョブ定義

学習構成別にディレクトリを分けています。全JSONの `defaults.project` は `V2VSimulator-revision` です。

| ディレクトリ | 学習構成 | モデル・結果 |
|---|---|---|
| [initial-0.3](initial-0.3/README.md) | 元の150,000 steps、初期ε0.3、元の学習率減衰 | 従来の保存先。既存モデルを利用可能 |
| [initial-0.5](initial-0.5/README.md) | 150,000 steps、初期ε0.5、120,000 steps以降は学習率4e-5 | 別名で保存 |

どちらもEncoderは **Transformer・noのみ**、履歴長は **10・20・30のみ** です。各ディレクトリは学習12ジョブ、通常評価24ジョブ、詳細解析24ジョブ、特徴量比較24ジョブ、通常描画8ジョブ、詳細描画8ジョブです。学習・評価・描画のεプロファイルはディレクトリ名に固定します。ジョブ名にもEncoderとεプロファイルを含めます。

学習はhighway 120台／urban 200台、評価・描画はhighway 120・360台／urban 200・500台です。描画は `--step 10 20 30` をまとめて渡します。通常評価は15 seed（43〜57）、詳細解析は単一seed 100です。特徴量比較も `--analysis-only` を維持します。

## 共通のbaseline評価

SPS・IPMIの評価はEncoder・εに依存しないため、親ディレクトリで共有します。各4ジョブで、従来の再測定オプションを維持します。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_baseline.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_baseline_analysis.json
```

## 学習の投入例

`JobScheduler/`で実行します。ジョブ定義を変更しても、登録済み・実行中のジョブは変更されません。

```bash
# 定義確認（登録・SSH接続なし）
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/train_propose.json --dry-run

# 追加の初期ε0.5だけを学習
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.5/train_propose.json

# 既存の初期ε0.3モデルを評価
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/initial-0.3/evaluate_propose.json
```

評価対象は150,000学習stepsまで（slot上限149999）です。以前の170,000 stepsのモデル・測定結果は評価用の自動選択から除外し、上限を超えるslotの明示指定はエラーになります。
