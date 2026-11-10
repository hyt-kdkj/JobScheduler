# method 2：100 ms以内の上位4候補予告

通常Basic CAMで、実送信時刻から100 ms以内（上端を含む）の再選択予定・候補・確率を予告します。
受信車は自分の通常再選択時に最新Qの分布を小幅に補正します。相手の指名・結果通知は不要です。
method 3/4の二段階抽選結合とは異なります。初期ε0.5・Transformer・履歴長20/30、
highway 120台／urban 200台のmethod 0のslot149999を、各2密度で評価します。

旧method 2の協調枝を学習する方式は置き換えました。旧モデル・キャッシュは使いません。
**追加学習は不要です。** `train_propose.json`は共通のmethod 0の基底モデルを学習します。
method-0/1/2/3/4のtrainは同じ保存先を使うため、必要な場合に一つだけ投入します。

| ファイル | 内容 | 展開ジョブ数 |
|---|---|---:|
| `train_propose.json` | method 0の共通基底モデル学習 | 4 |
| `evaluate_propose.json` | 通常評価、15 seed（43〜57） | 8 |
| `evaluate_propose_analysis.json` | 詳細解析、seed100・8,000 steps | 8 |

```bash
# JobScheduler/で実行。登録・SSH接続なしの展開確認
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-2/evaluate_propose.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator-revision/method-2/evaluate_propose_analysis.json --dry-run
```

学習済み基底モデルがあれば、対応する評価だけを投入してください。実際の投入時は`--dry-run`を外します。
ファイル変更だけでは登録済み・実行中のジョブを変更しません。旧協調枝の存在確認コマンドは削除しました。

描画用JSONは親ディレクトリの `plot.json`／`plot_analysis.json` です。
`parameters.methods` に対象番号を追加してください（例：`[0, 1, 2, 3, 4]`）。
先に基底モデルの学習・評価を完了してください。描画時は不足する測定を自動実行します。
仕様・制約はシミュレータの `Simulator/ShortPreview.md` を参照してください。
