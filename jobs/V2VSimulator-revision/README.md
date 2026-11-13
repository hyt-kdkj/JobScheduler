# V2VSimulator-revision のジョブ定義

全JSONの `defaults.project` は `V2VSimulator-revision` です。
共通baseline評価と描画のJSONはこのディレクトリ直下に置き、各モデルのディレクトリには学習・評価のJSONだけを置きます。
`initial-0.3/` のジョブ定義は削除しました。

## モデルごとの学習・評価

| ディレクトリ | 内容 |
|---|---|
| [method-0](method-0/README.md) | method 0の学習・通常評価・詳細解析・特徴量比較 |
| [method-1](method-1/README.md) | 共通method 0の学習・100 ms以内の上位2候補予告の通常評価・詳細解析 |
| [method-2](method-2/README.md) | 共通method 0の学習・100 ms以内の上位4候補予告の通常評価・詳細解析 |
| [method-3](method-3/README.md) | 共通method 0の基底モデル学習・method 3の通常評価・詳細解析 |
| [method-4](method-4/README.md) | 共通method 0の基底モデル学習・method 4の通常評価・詳細解析 |
| [method-5](method-5/README.md) | 最近傍1台・上位2候補予告の通常評価・詳細解析、共通method 0の学習 |
| [method-6](method-6/README.md) | 最近傍1台・上位4候補予告の通常評価・詳細解析、共通method 0の学習 |
| [method-7](method-7/README.md) | 予告入りCAMの最近傍3台・両flow・上位2候補の通常評価・詳細解析、共通method 0の学習 |
| [method-8](method-8/README.md) | 予告入りCAMの最近傍3台・両flow・上位4候補の通常評価・詳細解析、共通method 0の学習 |
| [method-9](method-9/README.md) | 最近傍3台の予告を使うQ低下制約付き確率移動の通常評価・詳細解析、共通method 0の学習 |
| [method-10](method-10/README.md) | 全ての有効予告を使うQ低下制約付き確率移動の通常評価・詳細解析、共通method 0の学習 |

履歴長は20・30、初期εは0.5です。学習はhighway 120台／urban 200台、評価・描画はhighway 120・360台／urban 200・500台です。
モデルの評価上限は150,000学習steps（slot 149999）。method 1〜10はmethod 0の固定モデルを使うため追加学習は不要です。
method-0〜10のtrainは同じ基底モデルを学習するので、必要な場合に一つだけ使います。
method 1/2は100 ms以内の短期予告、method 3/4は再選択前の最後のCAMで予告し、RC=0で全近傍を使って最新Qの選択確率を補正します。
method 5/6も最後のCAMで予告し、RC=0で条件を満たす最近傍1台だけを参照して抽選確率を減らします。通知候補数はそれぞれ2・4です。
method 7/8は有効な予告入りCAMの送信元から最近傍3台を先に選び、Basic・Extra両flowを参照します。通知候補数はそれぞれ2・4です。
method 9/10は上位4候補を通知し、重なりの不確かさと平均Qの低下を制約に抽選確率を移します。参照範囲はmethod 9が最近傍3台、method 10が全ての有効予告です。
旧method 5の協調RLモデルは使いません。旧method 1/2の協調枝モデル・キャッシュも使いません。
各構成・ジョブ数・前提条件はモデルごとのREADMEを参照してください。

## method 0のQ・V測定

`measure_method0_qv_highway.json`はHighway 120台・360台だけの2ジョブです。
両方ともHighway 120台で学習したmethod 0の同じモデル（履歴長20、Transformer、initial-0.5、slot149999）を使用します。
seed100、warmup40秒＋評価160秒で、実際のRC=0のQとDueling Vを`--measure-qv`で記録します。追加学習は不要です。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/measure_method0_qv_highway.json --dry-run
# 投入するときは --dry-run を外す
```

出力・読み方は対象プロジェクトの`markdown/Method0_QV_Measurement.md`を参照してください。
ファイル作成とdry-runだけではジョブは登録・実行されません。実行前に対応ソースをサーバーへ配置してください。

## 共通baseline評価

SPS・IPMIの評価はEncoder・εに依存しないため共有します。各4ジョブです。
通常評価は15 seed（43〜57）、詳細解析はseed 100です。
保存済みの互換測定を再利用し、不足分だけ測定します。強制refreshのオプションは付けません。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_baseline.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/evaluate_baseline_analysis.json
```

## 描画

描画用JSONは通常評価用と詳細解析用の2種類だけです。
履歴長20/30を各ジョブでまとめて渡し、各JSONはhighway・urbanの2密度、計4ジョブに展開します。

| ファイル | 内容 |
|---|---|
| `plot.json` | 通常評価の比較描画 |
| `plot_analysis.json` | `--analysis-only` の比較描画 |

比較対象は各JSONの先頭にある `parameters.methods` のリストだけで指定します。現在、通常描画は `[0]`、詳細解析の描画は `[0, 3, 4]` です。
例えば次のように変更すると、highway・urbanの全ジョブでmethod 0・3・4をまとめて比較します。コマンドの編集は不要です。

```json
"parameters": {
  "methods": [0, 3, 4]
}
```

`matrix` と異なりmethodごとにジョブを分割せず、`--method 0 3 4` として1つの比較に渡します。
通常描画とanalysis-onlyで対象を変える場合は、それぞれのJSONのリストを編集してください。
SPS・IPMIは `--agents SPS IPMI Propose` により共通で比較します。
method 0は既存Proposeで、省略時の既定値でもあります。保存名では `method0` が省略され、例えば `Propose_step20` がmethod 0に対応します。
対応する学習・評価を完了してから描画を投入してください。

```bash
# JobScheduler/で実行。--dry-runは登録・SSH接続なしの確認
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-0/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-3/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-5/evaluate_propose_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-6/evaluate_propose_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/plot.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/plot_analysis.json --dry-run
```

投入するときは対象コマンドから `--dry-run` を外します。バッチ間の依存関係は自動登録されません。
描画時は保存済み測定を再利用しますが、不足分は描画プログラムが測定するため、評価完了後に投入してください。
同じシナリオ・台数・Encoder・εの比較図・比較CSVは方式間で出力先を共有します。同条件の描画は同時投入せず、両方残す場合は先の図を別の場所へ保存してください。元測定は方式別に残ります。
ファイル変更だけでは登録済み・実行中のジョブのコマンドは変更されません。ソースのサーバーへの自動転送も行いません。
