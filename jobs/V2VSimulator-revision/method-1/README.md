# method 1：評価時のSPS候補10%・通常softmax

method 0で学習したモデルを固定して使い、評価時のSPS候補だけを20%から10%へ変更する。予告の送受信や確率補正は行わず、通常のsoftmaxで抽選する。

予告方式による補正はなく、旧method 1の短期予告とは別方式。

「候補10%」はQの上位10%ではなく、SPSと同じ予約RSRP除外後に平均S-RSSIが小さい資源から選ぶ割合。現在資源が候補外でも、method 0と同じく抽選対象へ追加する。

**学習時は20%のまま。method 0の既存モデルを使うため追加学習は不要。**
train_propose.jsonは不足している共通method 0のモデルを学習する補助バッチで、明示的に --method 0 を使う。全methodのtrainは同じ保存先を使うため、モデルが既にある場合は評価だけを実行する。

## 実行条件

- 履歴長20・30、Transformer、initial-0.5、mode=safe、option=A、XLA=off、評価epsilon=0。
- Highwayは120台で学習したmethod 0を120・360台で評価する。
- Urbanは200台で学習したmethod 0を200・500台で評価する。
- 同じ履歴長・Encoder・学習条件のslot 149999を固定して読み込む。
- サーバーの選択・除外GPU・同時実行上限は既存設定を使う。

| ファイル | 内容 | 展開ジョブ数 |
|---|---|---:|
| train_propose.json | 候補20%で共通method 0を学習。既存モデルがあれば不要 | 4 |
| evaluate_propose.json | method 1の通常評価、15 seed（43〜57）・各5,000 steps | 8 |
| evaluate_propose_analysis.json | method 1の詳細解析、seed100・8,000 steps | 8 |

ジョブ名にはsps10_softmaxを含め、新しい方式であることを区別する。学習ジョブ名はsps20を含む。

## 内容確認と投入

JobSchedulerディレクトリで実行する。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/evaluate_propose_analysis.json --dry-run
# method 0のモデルが未学習の場合のみ
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/train_propose.json --dry-run
```

投入時は対象コマンドから --dry-run を外す。学習・評価・描画の依存関係は自動登録されない。
**サーバー側にも新しいmethod 1・2・3のソースを配置してから使う。** ソースは自動転送されない。
旧method 1・2の短期予告、旧method 3の抽選結合・指数補正とは別方式で、旧測定キャッシュは新方式の結果として流用しない。

描画は親ディレクトリのplot.json／plot_analysis.jsonを使い、parameters.methodsに比較対象を指定する。例：[0, 1, 2, 3, 9]。同条件の比較図・CSVは保存先を共有するため、描画を同時投入しない。

ファイル変更とdry-runは、登録済み・実行中のジョブの名前・コマンドを変更しない。
詳細は対象プロジェクトのmarkdown/Method1_Method2_Method3.mdを参照。PRRの改善は未検証。
