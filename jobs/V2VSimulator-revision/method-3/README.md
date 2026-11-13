# method 3：SPS候補10%・確率移動上限20ポイント

method 2と同じ予告・候補・補正式を使い、移す確率量の上限だけを10ポイントから20ポイントへ広げる。上位4候補の予告、最近傍3台・Basic／Extra両flowの参照、未通知分の上下限、平均Q低下の上限0.25はmethod 2と同じ。

移動量は最大20ポイントで、毎回この量を動かすわけではない。平均Q低下の上限0.25や予告の不確かさが先に制限となり、補正を見送る場合もある。決定資源の事前通知・応答・ユニキャストは追加しない。

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
| evaluate_propose.json | method 3の通常評価、15 seed（43〜57）・各5,000 steps | 8 |
| evaluate_propose_analysis.json | method 3の詳細解析、seed100・8,000 steps | 8 |

ジョブ名にはsps10_tv20を含め、新しい方式であることを区別する。学習ジョブ名はsps20を含む。

## 内容確認と投入

JobSchedulerディレクトリで実行する。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-3/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-3/evaluate_propose_analysis.json --dry-run
# method 0のモデルが未学習の場合のみ
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-3/train_propose.json --dry-run
```

投入時は対象コマンドから --dry-run を外す。学習・評価・描画の依存関係は自動登録されない。
**サーバー側にも新しいmethod 1・2・3のソースを配置してから使う。** ソースは自動転送されない。
旧method 1・2の短期予告、旧method 3の抽選結合・指数補正とは別方式で、旧測定キャッシュは新方式の結果として流用しない。

描画は親ディレクトリのplot.json／plot_analysis.jsonを使い、parameters.methodsに比較対象を指定する。例：[0, 1, 2, 3, 9]。同条件の比較図・CSVは保存先を共有するため、描画を同時投入しない。

ファイル変更とdry-runは、登録済み・実行中のジョブの名前・コマンドを変更しない。
詳細は対象プロジェクトのmarkdown/Method1_Method2_Method3.mdを参照。PRRの改善は未検証。
