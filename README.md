# JobScheduler

ローカル PC から SSH 先の GPU に学習ジョブを投入します。性能の高い GPU を優先し、**1 GPU 最大2ジョブ**まで実行します。SSH は投入・確認時だけ接続し、常時接続しません。

## 普段の使い方

以下はローカルの `JobScheduler/` で実行します。

```bash
cd ~/workspace/JobScheduler

# 一括投入：highway / urban × step 10〜80 の16件
python3 scheduler.py submit-batch jobs/train_transformer.json

# 状態を確認（新しいジョブは開始しない）
python3 scheduler.py status

# 空き GPU を確認して、待機中のジョブを投入
python3 scheduler.py dispatch

# 実行中の全ジョブの末尾1000行をファイルに保存
python3 scheduler.py logs --running --lines 1000
```

**待機ジョブを進めるには `dispatch` を使います。同じ JSON を再投入すると、別のジョブが追加されます。** GPU が空いた瞬間に自動起動する仕組みではありません。この PC では cron が15分ごとに `dispatch` を実行します。

### 登録だけする・内容を確認する

```bash
# 内容確認だけ。登録・SSH 接続なし
python3 scheduler.py submit-batch jobs/train_lstm.json --dry-run

# ローカルのキューに登録だけ。次の dispatch で共有・投入される
python3 scheduler.py submit-batch jobs/train_lstm.json --queue-only

# 1件だけ投入
python3 scheduler.py submit --name highway_4_30 \
  'python3 train_transformer.py --scenario highway --method 4 --step 30'
```

`--queue-only` でも、次の定期実行では投入対象になります。

### 状態・ログ・キャンセル

`JOB_ID` は `status` の左端の ID、`BATCH_ID` は投入時の `Batch ...` の値に置き換えます。どちらも一意な先頭部分で指定できます。

| 操作 | コマンド |
|---|---|
| 失敗・キャンセル済みも表示 | `python3 scheduler.py status --all` |
| SSH 接続せず、手元の状態だけ表示 | `python3 scheduler.py status --local` |
| 状態確認と待機ジョブの投入 | `python3 scheduler.py status --dispatch` |
| コマンド・実行先・Batch ID の詳細 | `python3 scheduler.py info JOB_ID` |
| 1件のログを保存（終了後も可） | `python3 scheduler.py logs JOB_ID --lines 1000` |
| 1件をキャンセル | `python3 scheduler.py cancel JOB_ID` |
| バッチ全体をキャンセル | `python3 scheduler.py cancel --batch BATCH_ID` |
| GPU の使用状況を確認 | `python3 scheduler.py gpus` |

`status` は **正常終了 → 実行中 → 待機中** の順に表示します。同じグループ内は登録順で、失敗・キャンセル済みは通常非表示です。`--all`を付けると失敗ジョブは正常終了の次、キャンセル済みは末尾に表示します。履歴は保持されます。`ELAPSED` は待機時間を含まない実行時間です。

状態確認は同じホストのジョブを1回の SSH 接続にまとめ、ホスト間では並列に実行します。共有履歴は変更分だけをまとめてローカルに保存します。接続先が応答しない場合はタイムアウトまで待つため、すぐ表示したい場合は `status --local` を使ってください。

`AGENTS`列には、コマンドから判別した`LSTM`・`Transformer`などの手法名を表示します。比較ジョブは`SPS+IPMI+LSTM`のように表示し、判別できないコマンドは`-`になります。登録済みのジョブにも反映され、同じジョブ名でも手法を区別できます。

| STATE | 意味 |
|---|---|
| `succeeded` / `failed` | 正常終了（EXIT 0）/ 失敗 |
| `running` | 実行中。初回の環境イメージ準備も含む |
| `reserved` / `dispatching` | GPU 確保済み / 投入処理中 |
| `queued` | 実行枠や VRAM の空き待ち |
| `cancelling` / `cancelled` | 停止処理中 / キャンセル済み |
| `unknown` | 状態不明。残った処理やコンテナを確認する |

- ログは `logs/current/ジョブ名_ID.log` に保存し、本文はターミナルに表示しません。`--output-dir logs/checkpoint` で保存先を変更できます。同じファイルは取得のたびに上書きし、取得量は各ジョブ末尾256 KiBまでです。
- キャンセルはそのジョブ専用のコンテナを停止します。停止完了は `status --all` で確認してください。
- 通信失敗だけではジョブを失敗扱いにしません。`connection_error` が出たら、後で `status` を再実行してください。

## バッチ JSON

`matrix` の組合せごとに `name` と `command` を展開します。

```json
{
  "jobs": [
    {
      "name": "{scenario}_4_{step}",
      "command": "python3 train_transformer.py --scenario {scenario} --method 4 --step {step}",
      "matrix": {
        "scenario": ["highway", "urban"],
        "step": [30, 40, 50, 60, 70, 80]
      }
    }
  ]
}
```

`jobs` に異なるコマンドを複数並べても構いません。各ジョブの `host` で実行先を限定し、`min_free_gb` で必要 VRAM（既定2 GiB）を指定できます。トップレベルの `defaults` は全ジョブ共通の省略値です。保存後は `--dry-run` で確認してください。

ジョブ名は表示用で、モデルの保存先は変更しません。**SAN は全サーバーで共有されるため、同じ保存先への同時書込みに注意してください。**

## 初期設定

- ローカル：Python 3.9 以降と SSH。`hosts.json` の `glockenspiel`、`marimba`、`xylophone0`、`xylophone1`、`xylophone3` にパスワード入力なしで接続できること。
- 各サーバー：Docker、稼働する NVIDIA ドライバ、tmux、Python 3.9 以降。
- V2VSimulator 本体：サーバー側の SAN 上の `~/workspace/V2VSimulator` に配置。ローカルからの自動転送や Git pull は行いません。

使用する各サーバーでテンプレート用コンテナを準備します。

```bash
ssh glockenspiel
cd ~/workspace/V2VSimulator
./docker-shell.sh start
exit

# ローカルで確認
python3 scheduler.py gpus
```

既定のテンプレートは `v2vsimulator-v2v-dev-1`、作業先は `/workspace/V2VSimulator/Simulator` です。変更する場合は `hosts.json` を編集します。接続・GPU 確認に失敗したサーバーは投入先から外します。

### GPU の割当て

優先順は **RTX 5000 Ada → Quadro RTX 6000 → GTX 1080 Ti → P100**。`hosts.json` で変更できます。

1 GPU 最大2ジョブで、起動準備中の予約と外部の GPU 計算プロセスも枠に数えます。投入ごとに使用状況を確認し、必要 VRAM と既定1 GiBの余裕がある場合だけ開始します。`gpus` の `slots` は使用枠、`budget` は追加投入に使える VRAM です。

`max_jobs_per_gpu` と `gpu_memory_headroom_mib` で上限・余裕を変更できます。必要 VRAM は投入判断用で、実行時の使用量を制限しません。割当ては `CUDA_VISIBLE_DEVICES` で行うため、学習コマンドの `--gpu` は省略するか `auto` にしてください。

### ドライバ不一致への対応

カーネルドライバと同じ版の `/opt/nvidia-driver-<版>/lib` がコンテナ内にあれば、`LD_LIBRARY_PATH` を自動設定します。各 tmux ウィンドウでの export は不要です。

xylophone0・marimba・xylophone1 には `580.173.02` 用ライブラリを配置済みです。テンプレートを作り直した場合、カーネルも引き続き同じ版なら、対象サーバーで再配置できます。

```bash
docker exec --user 0 v2vsimulator-v2v-dev-1 mkdir -p /opt/nvidia-driver-580.173.02
docker cp ~/.local/share/job-scheduler/driver-libraries/580.173.02/lib \
  v2vsimulator-v2v-dev-1:/opt/nvidia-driver-580.173.02/
```

既存の環境イメージは、全ジョブ終了後に対象キャッシュを削除して更新します。ホストの NVIDIA ドライバ自体が動いていない場合は、この対応では解決しません。xylophone3 はホスト側の整備が必要です。

## 自動投入（cron）

この PC は `cron-dispatch.sh` を15分ごとに実行し、`dispatch` の後に実行中ジョブの末尾1000行を `logs/current/` に保存します。PC 停止中は動きません。別PCへ clone しても crontab は引き継がれず、定期実行は起動している1台に設定すれば十分です。

```bash
crontab -l
tail -100 ~/workspace/JobScheduler/state/cron-dispatch.log
```

スクリプトは必要に応じて `~/.ssh/agent.env` を読み込みます。再起動後も SSH 認証できる状態にしてください。前回の定期実行が残っている場合は重複実行しません。

## 複数 PC・保存先

**PC 同士が SAN に接続されている必要はありません。** 同じ SSH ユーザーと設定を使えば、各PCの `status` が SSH 経由でサーバー側の共有情報を取得します。SAN のロックで同じジョブの二重投入を防ぎます。

`state/` は各PCのローカル情報なので Git 管理不要です。ただし、**`--queue-only` の登録は、そのPCで `dispatch` するまで他PCには公開されません。** 通常の問い合わせ先は glockenspiel で、接続できなければ他の設定済みサーバーを試します。

| 保存物 | 場所 |
|---|---|
| ローカルのキャッシュ・未公開キュー | `state/queue.sqlite3` |
| SAN の共有キュー | `~/.local/state/job-scheduler/shared/` |
| サーバー別のログ・状態 | `~/.local/state/job-scheduler/<hostname>/<全ジョブID>/` |

コード・設定・バッチ定義は Git で管理できます。履歴を JSON に保存する場合は次を使い、commit / push は手動で行います。

```bash
python3 scheduler.py export            # 手元の履歴を jobs/history.json に保存
python3 scheduler.py export --refresh  # サーバーの最新状態を取得して保存
```

## コンテナと tmux

指定した SSH 先のコンテナを一括削除するには、以下を実行します。既定の対象は `marimba`・`xylophone1`・`xylophone3` の `v2vsimulator-v2v-dev-1` です。実行中の場合も停止・削除します。既に存在しない場合は成功扱いとし、一部のサーバーで失敗しても残りを処理します。

```bash
# 対象の確認だけ（SSH 接続なし）
python3 remove_containers.py --dry-run

# 既定の3台から削除
python3 remove_containers.py

# サーバーやコンテナ名を指定
python3 remove_containers.py marimba xylophone1 --container v2vsimulator-v2v-dev-1
```

削除したテンプレートは、次のジョブ投入前に元のプロジェクトで再作成してください。

ジョブごとに新しいコンテナを作り、終了後に削除します。正常終了・異常終了・キャンセル時にはテンプレート用コンテナも停止しますが、削除しません。他の実行中ジョブは専用コンテナで動作を続けます。停止済みテンプレートは GPU 確認時に一時起動して元の停止状態に戻し、次のジョブは初回に保存した環境イメージを再利用します。初回準備には数分以上かかる場合があります。テンプレート由来の Compose 管理ラベルは無効にし、元プロジェクトの Compose 操作との干渉を防ぎます。

この動作は変更後に投入したジョブに適用されます。テンプレートの停止に失敗した場合は、ジョブの終了結果を保持し、`status` に `template_cleanup_error` を表示します。

環境イメージはテンプレートのコンテナ ID ごとに1つです。依存関係やライブラリを変更したら、全ジョブ終了後に対象キャッシュを削除すると次回作り直します。`hosts.json` の `runtime_image` で準備済みイメージを指定することもできます。

tmux は各サーバーの専用ソケットに `jobs` セッションを1つ使い、ジョブごとのウィンドウを終了時に閉じます。SSH 先で画面を見るには次を使います（通常の `tmux ls` には表示されません）。

```bash
tmux -L job-scheduler attach -t jobs
```

結果は SAN 上に保存し、自動 rsync は無効です。マウント外にだけ保存したファイルはコンテナ削除時に消えます。ログと実行履歴は残ります。

開発用テスト：`python3 -m unittest -v test_scheduler.py`（隔離した tmux と偽 Docker を使用）。
