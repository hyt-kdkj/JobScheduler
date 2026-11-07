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

## 協調方式 method 1・2

[method-1](method-1/README.md)（案A）と[method-2](method-2/README.md)（案B）を用意。
両方とも **initial-0.5・Transformerのみ**、履歴長10・20・30、実行先は利用可能な全サーバーから自動選択。
既存のinitial-0.5のmethod 0を読み込んで協調枝を学習する。method 0の学習ジョブは含めない。
各ディレクトリには、学習6件・通常測定12件・詳細解析12件・通常描画4件・詳細描画4件の別々のJSONがある。
学習と測定を混在させた一括JSONは廃止し、測定は対応する学習が完了してから投入する。
測定はinitial-0.5の学習完了済みslot 149999を要求する。ファイル変更のみでジョブ投入は行わない。

## 固定モデルを使う method 3・4

[method-3](method-3/README.md)はIDEAS.mdの順位1位・α7（選択確率を保つ抽選結合）、
[method-4](method-4/README.md)は順位2位・α6（合成干渉の条件付き通知）。
**method 0の既存モデルを使う評価時の拡張で、追加学習は不要。**
両方ともinitial-0.5・Transformerのみ、履歴長10・20・30、mode=safe、option=A、XLA=off。
highway 120台／urban 200台のmethod 0のslot 149999を、対応する各2密度で評価する。

各ディレクトリに基底モデル学習6件・通常評価12件・詳細解析12件・通常描画4件・
詳細描画4件の計38ジョブを定義した5つのJSONを用意する。
`train_propose.json`は明示的に`--method 0`を指定し、共通の基底モデルを学習する。
method-3とmethod-4のtrainは同じ保存先なので、基底モデルが未学習の場合に
どちらか一方だけを使う。既存モデルがあれば学習を省略できる。
描画はSPS・IPMI・method 0・対象methodを比較し、履歴長10・20・30もまとめて表示する。
既存の測定が不足していれば描画時に補うため、対応する評価完了後に描画を投入する。
同条件のmethod-3/4描画は比較図・比較CSVの出力先を共有するため、同時投入しない。
両方の図を残す場合は先の出力を別の場所へ保存する。元測定レコードは方式別に残る。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-3/evaluate_propose_analysis.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-4/evaluate_propose_analysis.json --dry-run
```

投入時は対象コマンドから`--dry-run`を外す。method 3/4のソースをサーバー側へ
配置してから利用する。ジョブ定義の作成だけでは登録・実行しない。


## 学習から協調情報を使う method 5

[method-5](method-5/README.md)は、CAMで受信した予告を観測へ加えてEncoder・Qヘッドを同時に学習する。
initial-0.5・Transformer・履歴長10/20/30、highway120台／urban200台で15万stepを新規学習し、
専用method 5モデルのslot149999を対応する各2密度で評価する。
学習6件・通常評価12件・詳細解析12件・通常描画4件・詳細描画4件の5つのJSONを用意する。
描画はSPS・IPMI・method 0・method 5の比較。method 0のモデルへフォールバックしない。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-5/train_propose.json --dry-run
```

学習→評価→描画の順に、先の処理の完了後に次を投入する。バッチ間の依存関係は自動登録されない。
定義を作成しただけではジョブを登録・実行しない。
