# Propose method 1 の学習・測定

案A：受信側の脆弱度の共有。全コマンドで`--method 1 --epsilon-profile initial-0.5`を指定する。
実行先は **glockenspiel**、プロジェクトは`V2VSimulator-revision`。
Encoderは **Transformerのみ**、履歴長は10・20・30、学習期間は150,000 steps。
mode=safe、option=A、XLA=offを固定する。

既存method 0のinitial-0.5モデルを読み込み、協調枝を学習する。
method 0の再学習は行わない。同じシナリオ・履歴長・Encoder・εプロファイルのモデルを、
glockenspielから見えるプロジェクトの`Simulator/save_model/`に用意する。
method 1のモデル・結果はmethod 0とは別名で、保存名には
`_epsilon_high_exploration`が付く。

学習と測定は別々のJSONに分ける。一括版の`train_and_evaluate.json`は廃止した。

| ファイル | 内容 | 展開ジョブ数 |
|---|---|---:|
| `train_propose.json` | highway 120台／urban 200台で学習 | 6 |
| `evaluate_propose.json` | highway 120・360台／urban 200・500台、通常15 seed測定 | 12 |
| `evaluate_propose_analysis.json` | 同じ4条件の詳細解析、seed 100 | 12 |

測定は完了した150,000 steps（slot 149999）に固定する。測定前にinitial-0.5の保存先で
メタデータのmethod番号・slot、基底eval/targetと協調枝eval/targetの存在を確認する。
未学習の協調枝をmethod 0へのfallbackで測定しない。
通常測定のseedは既存設定の43〜57。使用モデルはhighway 120台／urban 200台を明示する。

JobSchedulerディレクトリで実行する。

```bash
# 展開内容の確認（ジョブ登録・SSH接続なし）
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/train_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/evaluate_propose.json --dry-run

# 学習を投入
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/train_propose.json

# 対応する学習が完了してから測定を投入
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/evaluate_propose.json
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-1/evaluate_propose_analysis.json
```

Schedulerにはバッチ間の依存関係機能がないため、学習と測定を同時に投入しない。
ソースは自動転送されない。method 1・2を実装した版をサーバー側へ配置してから投入する。
これらのファイルの変更だけでは登録済みジョブの設定は変わらず、新しいジョブも投入しない。
