# JobScheduler

ローカル PC から学習コマンドを登録し、SSH 先の GPU で実行するツールです。性能の高い GPU を優先し、**同じ GPU に最大2ジョブ**を割り当てます。ジョブごとにコンテナを作成して、終了後に削除します。

**SSH 接続はコマンドを実行した時だけです。常時接続はしません。** この PC は cron で毎時0分にも `dispatch` を実行します。空き GPU がなかったジョブは、次の手動操作または cron 実行時に再判定します。

## 最初に：待機ジョブは dispatch の実行時に始まります

**`queued` に入ったジョブは、GPU が空いた瞬間には始まりません。** この PC では毎時0分に cron が `dispatch` を実行するので、その時に空き枠へ投入します。次の時刻を待たずに進めたい場合は、手動で次を実行します。

```bash
python3 scheduler.py dispatch
```

| やりたいこと | 実行するコマンド | 待機ジョブを開始する？ |
|---|---|---|
| 状態を見るだけ | `python3 scheduler.py status` | いいえ |
| 待機ジョブを進める | `python3 scheduler.py dispatch` | はい。実行枠と VRAM に余裕があれば開始 |
| 状態確認と投入をまとめる | `python3 scheduler.py status --dispatch` | はい |
| 実行中のログをファイルに保存 | `python3 scheduler.py logs --running --lines 1000` | いいえ |

`dispatch` はサブコマンド、`--dispatch` は `status` に付けるオプションです。`--dispach` という指定はありません。

例えば、5件が実行中、7件が `queued` の時に1件が終わると、4件実行中・7件待機になります。次の毎時0分、または手動の `dispatch` 実行時に、その時の空き枠へ待機ジョブが入ります。

新しい `submit` / `submit-batch` を実行した時も、登録済みの待機ジョブを含めて投入を試みます。**待機ジョブを進める目的で同じ JSON を再投入すると、重複した新規ジョブが増えるため、`dispatch` を使ってください。**

### この PC の毎時実行（cron）

この PC の crontab に、**毎時0分に1回** `cron-dispatch.sh` を実行する設定を登録しています。実行中は SSH 接続し、処理が終わると切断します。PC が停止している間は動きません。

```bash
# 登録内容を確認
crontab -l

# 毎時の投入結果を確認
tail -100 ~/workspace/JobScheduler/state/cron-dispatch.log
```

スクリプトは `~/.ssh/agent.env` から既存 SSH エージェントの設定を読み込みます。再起動後など、SSH 鍵をまだ利用できない状態では接続に失敗するので、普段の SSH 認証ができる状態にしてください。前の毎時実行が残っていれば、新しい実行は重ねずスキップします。

crontab はこの PC の OS 設定です。GitHub から別 PC に clone しても自動登録はされません。複数 PC で使う場合も、毎時実行は常時起動している1台に設定すれば十分です。

## 1. 今回の12件の学習を投入する

以下はすべて**ローカル PC**で実行します。

```bash
cd ~/workspace/JobScheduler
```

12件の学習用サンプルは [jobs/transformer_method4_highway_urban_steps30_80.json](jobs/transformer_method4_highway_urban_steps30_80.json) です。

| 項目 | 内容 |
|---|---|
| 学習スクリプト | `train_transformer.py` |
| シナリオ | `highway`、`urban` |
| method | `4` |
| step | `30, 40, 50, 60, 70, 80` |
| ジョブ数 | 2シナリオ × 6種類のstep = **12件** |
| ジョブ名 | `highway_4_30`、`urban_4_80` など |

まず、実行されるコマンドを確認します。この段階では登録も SSH 接続も行いません。

```bash
python3 scheduler.py submit-batch jobs/transformer_method4_highway_urban_steps30_80.json --dry-run
```

内容を確認したら、次のコマンドを**一度だけ**実行します。

```bash
python3 scheduler.py submit-batch jobs/transformer_method4_highway_urban_steps30_80.json
```

`Batch ...: 12 jobs` と表示され、GPU の実行枠と VRAM に余裕がある分だけ開始します。1 GPU 最大2ジョブです。残りが `queued` になるのは正常です。

**既にこの12件を投入済みなら、同じ `submit-batch` を再実行しないでください。再実行すると別の12件が追加されます。** 待機中のジョブを進めるには、次の節の `dispatch` を使います。

## 2. 投入後に使うコマンド

### 終わったか確認する

```bash
python3 scheduler.py status
```

他の PC から投入したジョブも SAN から取り込んで表示します。`ELAPSED` は待機時間を含まない実行時間です。

| STATE | 意味 |
|---|---|
| `queued` | GPU の空き待ち |
| `reserved` | GPU を確保済み、起動準備中 |
| `running` | 学習中。初回の環境イメージ準備中も含む |
| `succeeded` | 正常終了。`EXIT` は `0` |
| `failed` | 実行失敗。`EXIT` とログを確認する |
| `cancelling` | 停止要求済み、停止完了待ち |
| `cancelled` | 取り消し、または停止済み |
| `dispatching` | 投入処理中、または投入応答の確認待ち |
| `unknown` | 状態が確定できない。実行が残っていないか調べる |

通信失敗だけで学習を失敗扱いにはしません。`connection_error` が出たら、後で `status` を再実行してください。

### 実行中のログをまとめて保存する

```bash
python3 scheduler.py logs --running --lines 1000
```

実行中・起動準備中・停止要求中の各ジョブについて、末尾1000行をカレントディレクトリの `logs/current/` に保存します。ターミナルには保存先だけを表示し、ログ本文は表示しません。ログの問い合わせはサーバーごとにまとめます。

保存先を変更する場合は `--output-dir` を指定します。

```bash
python3 scheduler.py logs --running --lines 1000 --output-dir logs/checkpoint
```

標準では `logs/current/highway_4_30_11ded7e0.log` のように、**ジョブ名＋短い ID** のファイル名で保存します。同じジョブ名を再実行してもログが混ざらないよう、末尾に ID を付けます。再取得すると同じファイルを最新の末尾ログで上書きします。名前がないジョブは `job_<ID>.log` になります。各ジョブの取得量は末尾256 KiBまでです。以前の ID だけのファイルはそのまま残します。

### 空いた GPU に待機ジョブを流す

```bash
python3 scheduler.py dispatch
```

終了状態を更新し、その時点で空いている GPU に待機ジョブを投入して切断します。手動で実行しなくても、この PC では次の毎時0分に同じ処理が走ります。

状態確認と投入をまとめて行う場合はこちらです。

```bash
python3 scheduler.py status --dispatch
```

### 特定のジョブのログ・詳細を見る

次の `JOB_ID` は、`status` の左端の ID に置き換えます。先頭8文字でも指定できます。`highway_4_30` のようなジョブ名ではなく、ID を指定してください。

```bash
python3 scheduler.py logs JOB_ID --lines 1000
python3 scheduler.py info JOB_ID
```

単一ジョブの `logs` も標準では `logs/current/` に保存します。`--output-dir` で保存先を変更できます。終了したジョブのログも取得できます。`info` には実行コマンド、サーバー、GPU、開始・終了時刻、Batch ID などが出ます。

## 3. ジョブをキャンセルする

ここでも `JOB_ID` / `BATCH_ID` は実際の ID に置き換えます。

```bash
# 1件をキャンセル
python3 scheduler.py cancel JOB_ID

# 一括投入した12件をまとめてキャンセル
python3 scheduler.py cancel --batch BATCH_ID

# 停止が完了したか確認
python3 scheduler.py status
```

Batch ID は一括投入時の `Batch ...` の値です。各ジョブの `info` の `batch_id` でも確認できます。

待機ジョブは取り消し、実行中のジョブはそのジョブ専用のコンテナだけを停止します。テンプレート用コンテナや他のジョブは停止しません。停止と削除の確認後に `cancelled` となり、GPU の予約を解放します。

SSH の応答が失われた場合も停止要求を記録し、次の `status` や `dispatch` で再確認します。`unknown` のジョブや古い形式のジョブは、自動停止できない場合があります。

## 4. 新しいジョブ定義を作る

今回の JSON は次の形式です。`matrix` の組合せごとに、`name` と `command` の変数を置き換えます。

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

例えば先頭は次の1ジョブに展開されます。

```text
名前: highway_4_30
実行: python3 train_transformer.py --scenario highway --method 4 --step 30
```

条件の異なるコマンドを `jobs` に複数並べても構いません。`host` で実行先を限定し、`min_free_gb` で必要な空き VRAM を GiB 単位で指定できます。`defaults` は全ジョブ共通の省略値です。

```json
{
  "defaults": {"min_free_gb": 4},
  "jobs": [
    {
      "name": "highway_4_30",
      "command": "python3 train_transformer.py --scenario highway --method 4 --step 30",
      "host": "glockenspiel"
    },
    {
      "name": "urban_4_40",
      "command": "python3 train_transformer.py --scenario urban --method 4 --step 40"
    }
  ]
}
```

新しいファイル名で `jobs/` に保存し、`submit-batch ... --dry-run` で確認してから投入します。全件を検証してから登録し、1回の上限は10000件です。文字列の行列値はコマンドにそのまま挿入するため、引用符が必要な値には注意してください。

1件だけなら JSON は不要です。

```bash
python3 scheduler.py submit --name highway_4_30 \
  'python3 train_transformer.py --scenario highway --method 4 --step 30'
```

`name` はスケジューラの表示名で、モデルの保存先は変更しません。同じ条件の学習を重ねる場合は、出力先が競合しないことを確認してください。全サーバーでファイルが共有されるので、別サーバーでも同じパスへの書込みは競合します。

## 5. 最初に必要な準備

### ローカル PC

Python 3.9 以降と `ssh` が必要です。この `JobScheduler/` を配置し、次の SSH エイリアスでパスワード入力なしに接続できるようにします。

```text
glockenspiel
marimba
xylophone0
xylophone1
xylophone3
```

### サーバーと SAN

V2VSimulator 本体は SAN 上の `~/workspace/V2VSimulator` に置きます。同じ SAN を参照するため、サーバーごとに別々に clone する必要はありません。ローカルの V2VSimulator の自動転送や、リモートでの自動 Git pull は行いません。

Docker・NVIDIA ドライバ・tmux・Python 3.9 以降は、使用する各サーバーに必要です。Docker のイメージやコンテナはホストごとに準備します。例えば glockenspiel の初回準備は次のとおりです。

```bash
# ローカル PC で実行
ssh glockenspiel

# SSH 先で実行
cd ~/workspace/V2VSimulator
./docker-shell.sh start
exit
```

同様の準備を使用する他のサーバーでも行います。既定のテンプレート名は `v2vsimulator-v2v-dev-1`、コンテナ内の作業ディレクトリは `/workspace/V2VSimulator/Simulator` です。必要なら `hosts.json` で変更します。

GPU が使えるかを、ローカルから確認します。

```bash
python3 scheduler.py gpus
```

`UNAVAILABLE` のサーバーはスキップします。ジョブ全体が失敗したという意味ではありません。

## 6. GPU の選択とドライバ不一致

空いている GPU の中では、次の順で優先します。優先度は `hosts.json` で変更できます。

| GPU | 優先度 |
|---|---:|
| RTX 5000 Ada | 100 |
| Quadro RTX 6000 | 60 |
| GTX 1080 Ti | 40 |
| P100 | 30 |

1ジョブにつき1台を割り当て、**1 GPU あたり最大2ジョブ**まで共有します。実行中だけでなく、GPU を予約してイメージを準備中のジョブも1枠と数えます。スケジューラ外の GPU 計算プロセスも各1枠として数え、管理中ジョブのプロセスと二重には数えません。

共有モードでは GPU 使用率が高いだけでは除外しません。必要な VRAM が残っていることを確認し、さらに既定で1 GiBの余裕を残します。起動直後で CUDA がメモリを使い始めていないジョブも、必要 VRAM の予算を確保して判定します。必要 VRAM の既定値は1ジョブ2 GiBで、JSON の `min_free_gb` で変更できます。これは投入判断用の見積もりで、実行中のメモリ使用量を制限する設定ではありません。

`gpus` の `slots=1/2` は1枠使用中、`slots=2/2` は上限です。`budget` は起動待ちジョブの予算と余裕分を差し引いた追加投入用の VRAM です。

```json
"max_jobs_per_gpu": 2,
"gpu_memory_headroom_mib": 1024
```

設定は `hosts.json` でホストごとにも上書きできます。`max_jobs_per_gpu` を1にすると従来の専有方式に戻り、計算プロセスなし・使用メモリ512 MiB以下・使用率10%以下の条件も適用します。状態不明の予約がある GPU は、安全に再確認できるまで追加投入しません。

割当ては GPU UUID を `CUDA_VISIBLE_DEVICES` に設定して行います。V2VSimulator の `--gpu` は省略するか `--gpu auto` とし、ジョブ側で割当てを上書きしないでください。手作業で起動した外部ジョブはこの予約を共有しないため、外部利用者との同時開始を完全には防げません。

ドライバの版が食い違う場合は、稼働中の NVIDIA カーネルモジュールと同じ版のライブラリをコンテナ内に用意する方法があります。

```text
/opt/nvidia-driver-580.173.02/lib
```

対応するディレクトリがあれば、スケジューラが `LD_LIBRARY_PATH` を自動設定します。tmux の各ウィンドウで export する必要はありません。ライブラリはテンプレートから新しい環境イメージにも引き継ぎます。ホストのカーネルドライバ自体が動いていない場合は、この方法だけでは GPU を使えません。

現在、xylophone0・marimba・xylophone1 のテンプレートに `580.173.02` 用ライブラリを配置済みです。SAN の `~/.local/share/job-scheduler/driver-libraries/580.173.02/lib/` にも保存しています。テンプレートを作り直した場合、稼働中の版が引き続き `580.173.02` なら、対象サーバーで次を実行すると再配置できます。

```bash
docker exec --user 0 v2vsimulator-v2v-dev-1 mkdir -p /opt/nvidia-driver-580.173.02
docker cp ~/.local/share/job-scheduler/driver-libraries/580.173.02/lib \
  v2vsimulator-v2v-dev-1:/opt/nvidia-driver-580.173.02/
```

既に作成済みの環境イメージにも変更を反映したい場合は、次の節に従ってキャッシュを更新します。xylophone3 はホスト側の NVIDIA ドライバが認識されていないため、別途ホスト環境の整備が必要です。

## 7. コンテナ・tmux・結果の扱い

- テンプレート用コンテナは変更・停止せず、準備済み環境のひな型として使います。
- 初回に `docker commit --pause=false` で環境イメージを保存し、以後は再利用します。大きな環境では初回に数分以上かかります。準備ログも取得でき、準備待ちのために SSH を維持する必要はありません。作成の上限は30分です。
- ジョブは `docker run --rm --init` で新しいコンテナを作り、終了・失敗・キャンセル後に削除します。削除対象はジョブラベルで検証します。
- ジョブ用コンテナではテンプレート由来の Compose 管理ラベルを無効にし、V2VSimulator の `docker compose up/down` による停止対象から外します。この対策は既存のキャッシュイメージにも適用されます。
- tmux は各サーバーの専用ソケット `job-scheduler` に、`jobs` セッションを1つだけ使います。ジョブごとのウィンドウは終了時に閉じ、`overview` は再利用用に残します。既存の対話用 tmux には触れません。
- モデルと結果は SAN 上の V2VSimulator に保存します。**自動 rsync は無効です。** コンテナ内のマウント外にだけ書いたファイルは、コンテナ削除時に消えます。
- ログ、要求、ワーカー、実行履歴は確認用に保存します。履歴のディスク使用量は増えます。
- ホスト停止・ワーカー消失・削除失敗時などは `unknown` になる場合があります。その場合は残った処理を調べてから対処します。

手動で tmux の画面を見る場合だけ、SSH 先で次を実行します。

```bash
tmux -L job-scheduler attach -t jobs
```

キャッシュイメージはテンプレートのコンテナ ID ごとに1つです。依存関係やコンテナ内ライブラリを更新した場合は、全ジョブ終了後に対象キャッシュを削除すると次回に再作成します。`runtime_image` を設定すれば、指定した準備済みイメージを使うこともできます。

## 8. 複数 PC と GitHub

同じ SSH ユーザー・SAN・設定を使う PC 間で、待機キューと実行状態を共有します。どの PC からも `status`、`logs --running`、`cancel` を使えます。SAN のロックで同じジョブの二重投入と GPU の重複割当てを防ぎます。

通常は glockenspiel に共有キューを問い合わせ、接続できなければ他の設定済みサーバーを試します。常時接続は行いません。

GitHub の private リポジトリでは、コード、`hosts.json`、`jobs/` の定義・履歴 JSON を管理できます。

```bash
# 手元の履歴を jobs/history.json に保存。SSH 接続なし
python3 scheduler.py export

# SAN とサーバーの最新状態を取得してから保存
python3 scheduler.py export --refresh
```

commit / push は手動です。稼働状態の共有は SAN が基準で、Git の JSON は記録用です。`state/` の SQLite とロックファイルは Git 管理から除外しています。

## 9. その他の操作・保存先

```bash
# 接続せず、手元に登録だけする。次の dispatch まで他 PC には公開されない
python3 scheduler.py submit-batch jobs/transformer_method4_highway_urban_steps30_80.json --queue-only

# SSH 接続せず、最後に取得した状態だけ見る
python3 scheduler.py status --local
```

`--queue-only` も新しいジョブの登録なので、既に投入済みの定義を再登録する用途には使いません。

| 保存物 | 場所 |
|---|---|
| ローカルのキャッシュ・未公開キュー | `JobScheduler/state/queue.sqlite3` |
| SAN の共有キュー | `~/.local/state/job-scheduler/shared/` |
| サーバー別のログ・要求・状態 | `~/.local/state/job-scheduler/<hostname>/<全ジョブID>/` |
| ジョブ専用コンテナ | `job-scheduler-<全ジョブID>` |

別の設定やローカル状態を使う場合、グローバル引数をサブコマンドの前に置きます。

```bash
python3 scheduler.py --config hosts.json --state-dir /tmp/job-scheduler-demo gpus
```

結果転送の旧機能は任意設定として残していますが、現在の `results_sync: null` では実行しません。過去の同期記録があれば `SYNC` 列に表示される場合があります。

テストは次で実行できます。実 GPU・SSH・実コンテナは使わず、隔離した tmux と Docker の代替プログラムで検証します。

```bash
python3 -m unittest -v test_scheduler.py
```
