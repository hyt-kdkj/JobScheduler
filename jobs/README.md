# Gitで管理するジョブ定義

現行の学習・評価・描画バッチは [V2VSimulator](V2VSimulator/README.md) にあります。`JobScheduler/` 内で実行します。実行プロジェクトは `V2VSimulator` です。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator/method-0/train_propose.json --dry-run
```

`python3 scheduler.py export`で履歴を`jobs/history.json`に書き出せます。`export --refresh`はSANの共有キューとリモート状態を取得してから書き出し、結果のrsyncは行いません。履歴JSONは記録用で、一括投入の入力にはなりません。

各PCは独立したローカルSQLiteを持ち、SAN上の共有キューを参照します。稼働状態の同期はSANで行い、定義・履歴はGitで管理できます。commit・pushは手動で行います。
