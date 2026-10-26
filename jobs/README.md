# Git で管理するジョブ情報

このディレクトリの JSON は Git 管理の対象です。

`evaluate_transformer_method4.json` は Transformer method 4 を、highway / urban × step 10〜80（10刻み）の16ジョブで評価します。各ジョブは通常の15 seed評価です。車両数・チェックポイントは `evaluate_methods.py` と評価設定の既定値を使い、SPS・IPMIは含めません。対応する学習モデルをサーバー側に用意してから投入してください。

```bash
python3 scheduler.py submit-batch jobs/evaluate_transformer_method4.json --dry-run
python3 scheduler.py submit-batch jobs/evaluate_transformer_method4.json
```

- 一括投入の定義ファイルを保存し、`python3 scheduler.py submit-batch jobs/名前.json` で使えます。
- `python3 scheduler.py export` で、手元のジョブ履歴を `jobs/history.json` に書き出せます。
- `python3 scheduler.py export --refresh` なら、SAN の共有キューとリモート状態を取得してから書き出します。このコマンドでは結果の rsync は行いません。

履歴 JSON にはコマンド・ジョブ ID・状態・実行時間の元データ・同期結果などが含まれます。履歴は記録用で、そのまま一括投入の入力にはなりません。

各 PC は独立したローカル SQLite を持ち、SAN 上の共有キューを参照します。稼働状態の同期は SAN で行い、Git の履歴 JSON は実験の記録に使います。commit・push は手動で行います。
