# JobScheduler

ローカルでコマンドを登録し、SSH 先の空いている GPU を選んで、ジョブ専用の Docker コンテナ内で実行するツール。Python 標準ライブラリだけで動作する。

**SSH 接続はコマンド実行時だけ。ローカルの常駐監視や定期ポーリング、SSH 接続の維持は行わない。** 投入後のジョブはサーバーごとに1つの専用 tmux セッション内のウィンドウで動くため、ローカルのコマンドが終了した後や、ローカル PC を閉じた後も実行を続ける。

## 準備

ローカルには Python 3.9 以降、`ssh`、`rsync` が必要。対象ホストには Python 3.9 以降、tmux、Docker、および起動済みの実行用コンテナが必要。結果の取得元 glockenspiel にも `rsync` が必要。

`hosts.json` は次の SSH エイリアスを使用する。ローカルの `~/.ssh/config` と SSH 鍵の設定を利用し、パスワード入力が必要な接続はエラーとしてスキップする。

- `glockenspiel`
- `marimba`
- `xylophone0`
- `xylophone1`
- `xylophone3`

既定のテンプレート用コンテナ名は `v2vsimulator-v2v-dev-1`、作業ディレクトリは `/workspace/V2VSimulator/Simulator`。テンプレートは GPU 情報の取得と、ジョブ専用コンテナの環境のひな型に使う。テンプレート自体は起動・停止・再作成しない。ジョブは SSH ユーザーの UID/GID で実行する。

初回は準備済みテンプレートを `docker commit --pause=false` でキャッシュイメージに保存し、以後ジョブごとに `docker run --rm --init` で新しいコンテナを作る。テンプレートの仮想環境・環境変数と GPU デバイス、SAN のマウントを引き継ぐ。ジョブ終了後は専用コンテナを削除する。キャッシュ作成中も既存コンテナを一時停止しない。

初回のキャッシュ作成は環境サイズによって数分以上かかる。作成待ちはサーバー側のワーカーが行い、投入元の SSH を維持する必要はない。準備のメッセージも `logs` で確認できる。キャッシュ作成の上限は30分。

## 基本操作

以下はすべてローカルの `JobScheduler/` で実行する。

```bash
cd /home/hayate-desktop/workspace/JobScheduler

# 各ホストの GPU・空きメモリ・使用状況を一度だけ取得
python3 scheduler.py gpus

# コマンドを登録し、その場で空き GPU への投入を試みる
python3 scheduler.py submit --name highway1192 \
  'python3 train_transformer.py  --method 1 --scenario highway'

# リモートに一度接続して、実行中・成功・失敗を確認
python3 scheduler.py status

# ログの末尾を取得。ID は表示された先頭8文字などで指定できる
python3 scheduler.py logs 1234abcd --lines 200

# 実行中ジョブのログを一括取得。ログの問い合わせはサーバーごとにまとめる
python3 scheduler.py logs --running --lines 200

# ジョブ別のログファイルとして保存する場合
python3 scheduler.py logs --running --lines 200 --output-dir logs/current

# 実行コマンド、ホスト、GPU UUID、終了コード、時刻などを表示（SSH 接続なし）
python3 scheduler.py info 1234abcd
```

コマンド全体をシングルクォートで囲む。環境変数を指定する場合も、コマンドの中に書ける。

`logs --running` は共有キューを取得して対象を選び、各サーバーに一度ずつログを問い合わせる。実行中・起動準備中・停止要求中のジョブをまとめて表示し、見出しでジョブ ID とホストを区別する。`--output-dir` 指定時は全 ID をファイル名にした `.log` に保存する。取得中に完了が判明したジョブも最後のログを表示して状態を更新する。各ジョブのログ取得量は末尾256 KiBまで。

```bash
python3 scheduler.py submit --min-free-gb 8 --name evaluation \
  'V2V_SAVE_RESULTS_DIR=save_results/unique-evaluation python3 your_evaluation.py ...'
```

`your_evaluation.py ...` は実際の評価コマンドに置き換える。コマンド中の `$変数` はリモートのコンテナ内で展開される。Python のコマンドはコンテナの既存 PATH、仮想環境を利用する。

## 空き GPU がないとき

ジョブはローカルのキューに `queued` として残る。**GPU が後から空いても、自動では開始しない。** 次のいずれかを実行した時に再判定する。

```bash
# 待機ジョブをもう一度投入する。終了状態も更新する
python3 scheduler.py dispatch

# 状態確認と待機ジョブの投入を一度に行う
python3 scheduler.py status --dispatch
```

新しい `submit` を実行した時も、先に登録された待機ジョブから投入を試みる。必要なメモリや指定ホストが合わないジョブは残し、実行可能な後続ジョブを進める。

定期接続をしない設計なので、`watch`、cron、バックグラウンド監視などは設定しない。

## ジョブの一括投入

JSON ファイルにジョブを記載し、`submit-batch` でまとめて登録する。

```bash
# 展開後のコマンドを確認。登録も SSH 接続も行わない
python3 scheduler.py submit-batch examples/seeds.json --dry-run

# 登録し、その場で一度だけ空き GPU への投入を試みる
python3 scheduler.py submit-batch examples/seeds.json

# 登録だけ行う
python3 scheduler.py submit-batch examples/seeds.json --queue-only
```

`examples/seeds.json` は車両数2通り × seed 3通りの6ジョブを生成する。サンプルのコマンドは条件と割り当て GPU を表示する動作確認用なので、実験用のコマンドへ置き換える。`matrix` の各キーについて、`command` と `name` の `{変数名}` を置き換える。組合せはすべて展開する。テンプレートの値はシェルコマンドへそのまま挿入するので、seed や車両数などの数値を指定するのが簡単。

異なるコマンドを列挙する形式にも対応する。

```json
{
  "defaults": {"min_free_gb": 4},
  "jobs": [
    {
      "name": "method1-seed42",
      "command": "python3 train_transformer.py --scenario highway --method 1 --seed 42"
    },
    {
      "name": "method2-seed43",
      "command": "python3 train_transformer.py --scenario highway --method 2 --seed 43",
      "host": "glockenspiel",
      "min_free_gb": 8
    }
  ]
}
```

`defaults` は各ジョブの省略値。ジョブ側の指定が優先される。全件を検証してから一度のトランザクションで登録するため、不正なジョブがあれば一部だけが登録されることはない。上限は1回10000ジョブ。表示された Batch ID と元ファイルのパスは各ジョブの `info` にも保存する。

空き GPU の数だけ開始し、残りは `queued` になる。自動で順次投入する監視は行わないため、後続を進める時は `dispatch` または `status --dispatch` を実行する。

V2VSimulator の学習モデル名には seed が含まれないため、同じ条件で seed だけを変えた学習は保存先が重なる。学習を並列化する場合は別の配置・コンテナを設定するなど、モデルと結果の保存先も分ける。スケジューラの `name` は表示用で、シミュレータの保存先は変更しない。

## ジョブのキャンセル

```bash
# ジョブ ID を指定。複数指定も可能
python3 scheduler.py cancel 1234abcd
python3 scheduler.py cancel 1234abcd 5678efab

# 一括投入したバッチ全体をキャンセル（Batch ID の先頭部分でも可）
python3 scheduler.py cancel --batch abcdef12

# 実行中だったジョブの停止完了を確認
python3 scheduler.py status
```

ローカルだけに登録した待機ジョブは SSH 接続せず `cancelled` にする。SAN に公開済みの待機ジョブは、共有キューのロックを取ってキャンセルする。実行中のジョブは `cancelling` と表示し、専用ワーカーがそのジョブ専用コンテナを `docker stop` で停止する。停止後に専用コンテナとウィンドウを削除する。テンプレート用コンテナや他のジョブは停止しない。

停止とコンテナの削除を確認したら `cancelled` に変わり、GPU の予約を解放する。停止に伴う終了コードは通常143または137。キャンセルの要求と自然終了が重なった場合は、実際の終了状態が優先される。既に終了したジョブへ再度キャンセルしても再実行や状態変更は行わない。

SSH の応答が失われた場合もキャンセルの意図を保存し、次の `status` または `dispatch` で同じジョブへ再要求する。キャンセル処理のための定期 SSH 接続は行わない。実行済みジョブの途中結果も、停止確認後に通常の結果同期の対象になる。実行していない待機ジョブのキャンセルでは結果同期を行わない。

この機能の追加前に投入した実行中ジョブと、ワーカーが消失して `unknown` になったジョブは、自動停止できない場合がある。その場合はエラーを表示し、停止したとは扱わない。

## GPU の選択

1ジョブにつき GPU 1台を割り当てる。空いている GPU の中から、`hosts.json` の優先度が高いものを選ぶ。値が大きいほど優先される。

| GPU 名の一致文字列 | 優先度 |
|---|---:|
| RTX 5000 Ada | 100 |
| Quadro RTX 6000 | 60 |
| GTX 1080 Ti | 40 |
| P100 | 30 |

これは既定の優先順位であり、実測速度ではない。未登録の GPU は優先度 0 になるので、GPU を追加した場合は設定を更新する。

既定では、次の条件をすべて満たした GPU を空きと判断する。

- このツールからの別ジョブの予約がない。
- `nvidia-smi` に計算プロセスが表示されていない。
- 使用中の GPU メモリが 512 MiB 以下。
- GPU 使用率が 10% 以下。
- ジョブに指定した必要空きメモリを満たす。既定は 2 GiB。

```bash
# 最低 8 GiB の空きを要求
python3 scheduler.py submit --min-free-gb 8 'python3 ...'

# 特定ホストだけを使う
python3 scheduler.py submit --host glockenspiel 'python3 ...'
```

`--min-free-gb` は投入時の条件であり、メモリ予約や上限設定ではない。VRAM 消費の見積もりに合わせて指定する。

GPU は番号ではなく UUID を使い、コンテナ内の `CUDA_VISIBLE_DEVICES` に設定する。**ジョブ側は既存の `CUDA_VISIBLE_DEVICES` を尊重する必要がある。** V2VSimulator では `--gpu` を省略するか `--gpu auto` を使い、`--gpu all` や物理 GPU 番号の明示で割当てを上書きしない。

別のツールや手作業の GPU 利用者は、このスケジューラの予約を共有しない。投入直前にも再確認するが、外部ジョブとの同時開始を完全には防げない。

## LD_LIBRARY_PATH の自動設定

サーバーで稼働中の NVIDIA カーネルモジュールの版を読み、その版に一致する次のディレクトリがコンテナ内に存在すれば、ジョブと GPU 状態取得の両方で自動的に優先する。

```text
/opt/nvidia-driver-<稼働中の版>/lib
```

例えば `580.173.02` なら、プロセス内で次と同等の設定をする。

```bash
export LD_LIBRARY_PATH=/opt/nvidia-driver-580.173.02/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
```

手動での export や tmux ウィンドウごとの設定は不要。既存の環境やコンテナの設定は変更しない。対応ライブラリがなければ既存環境を使い、GPU を認識できなければそのホストをスキップする。ドライバ自体のインストールは行わない。

作成時の確認では xylophone0 は対応ライブラリで認識できた。marimba と xylophone1 はドライバ不一致、xylophone3 は NVIDIA ドライバ／コンテナへの GPU 提供が未整備だったため、利用するには別途環境の復旧が必要。

## 終了と進捗の確認

```bash
python3 scheduler.py status
python3 scheduler.py logs 1234abcd
```

| 状態 | 意味 |
|---|---|
| `queued` | ローカルで待機中 |
| `dispatching` | 投入要求中、または応答が不明。次の `dispatch` で同じ ID に対して再確認・再要求する |
| `reserved` | リモートで GPU を確保済み |
| `running` | コンテナ内でコマンドを実行中 |
| `cancelling` | 停止要求済み。GPU の予約はまだ保持している |
| `succeeded` | 終了コード 0 で終了 |
| `failed` | 非ゼロの終了コード、または起動時のエラー |
| `cancelled` | 待機中に取り消し、または実行中ジョブの停止を確認済み |
| `unknown` | tmux 消失などにより確認できない。自動再投入しない |

`EXIT` 列で終了コードを確認できる。パイプラインも `pipefail` を有効にして実行するため、`python ... | tee ...` の Python 側が失敗した場合も失敗として記録する。実行中の進み具合は、コマンドが出力するログで確認する。共通の進捗率は計算しない。

`ELAPSED` 列は実行時間を `時:分:秒` で表示する。実行中は確認時点までの経過時間、終了後は開始から終了までの所要時間。待機時間は含めず、未開始なら `-` を表示する。`info` では `elapsed` と `elapsed_seconds` も確認できる。`status --local` では最後に取得した終了状態を使うため、実際には終了していても、終了を取得するまでは実行中として時間を表示する。

SSH 接続に失敗しただけではジョブを `failed` にしない。最後の状態と `connection_error` を表示するので、後から `status` を再実行する。失敗したジョブは自動再実行しない。

### 終了確認時の結果の自動同期

`status`、`logs`、`dispatch`、通常の `submit` / `submit-batch` で、実行済みかつ終了済み（`succeeded`、`failed`、`cancelled`）の未同期ジョブが見つかったら、次と同等の rsync を自動実行する。失敗・キャンセルしたジョブの途中結果も取得する。

```bash
rsync -az glockenspiel:~/workspace/V2VSimulator/Simulator/save_results/ \
  ~/workspace/V2VSimulator/Simulator/save_results/
```

**同期するのは終了を確認するコマンドを実行した時。ジョブ終了の瞬間にバックグラウンド接続は行わない。** ローカル PC を閉じていた場合も、次の `status` で同期できる。

複数のジョブの終了が同時に判明した場合、rsync は1回にまとめる。成功したジョブは同期済みとして記録し、以後同じ終了に対して再実行しない。同期に失敗した場合は、次の確認・投入操作で再試行する。ジョブ本体の終了状態は変更しない。

`status` の `SYNC` 列に同期の `succeeded` / `failed` を表示する。詳細な同期エラーや同期時刻・保存先は `info <ID>` でも確認できる。`status --local`、`info`、`submit --queue-only` は接続も同期も行わない。

`-az` に従い、取得元で変更された同名ファイルは更新し、ローカルにしかないファイルは削除しない。取得範囲はジョブ別ではなく `save_results/` 全体なので、他の実行中ジョブの途中結果も含まれる場合がある。

glockenspiel・marimba・xylophone は SAN で同じファイルを共有しているため、取得元は実行ホストにかかわらず glockenspiel 固定にする。全ホストで同じ結果ディレクトリを参照している。

設定は `hosts.json` の `results_sync` で変更できる。ローカルパスの `~` は、操作しているユーザーのホームに展開する。rsync の SSH 接続も接続共有・接続維持を無効にする。

```json
"results_sync": {
  "source": "glockenspiel:~/workspace/V2VSimulator/Simulator/save_results/",
  "destination": "~/workspace/V2VSimulator/Simulator/save_results/",
  "timeout_seconds": 1800
}
```

`results_sync` を削除するか `null` にすると自動同期を無効にできる。

`unknown` の場合はコンテナ内に処理が残っている可能性があるので、実行状態を調べてから再投入する。安全のため予約も自動解放しない。管理用ファイルは下記のリモート保存先にある。

`status`、`logs` はそれぞれ一回限りの問い合わせ。`wait` や常駐監視のコマンドは用意していない。

## SSH 接続を行わない操作

```bash
# その場では投入せず、ローカルに登録だけする
python3 scheduler.py submit --queue-only 'python3 ...'

# 最後に取得した状態だけを見る
python3 scheduler.py status --local

# 保存された詳細を確認
python3 scheduler.py info 1234abcd
```

## 保存先と tmux

- ローカルのキャッシュ・未公開キュー：`JobScheduler/state/queue.sqlite3`
- SAN 上の共有キュー：`~/.local/state/job-scheduler/shared/`
- リモートのログ・要求・終了状態：`~/.local/state/job-scheduler/<hostname>/<ジョブの全ID>/`
- リモート tmux：専用ソケット `job-scheduler`、セッション `jobs`、ジョブ別ウィンドウ
- ジョブ専用コンテナ：`job-scheduler-<ジョブの全ID>`
- シミュレータのモデル・結果：既存コンテナの作業ディレクトリに従う。`save_results/` は終了確認時にローカルへ同期する。モデルは同期対象に含めない。

コンテナはジョブごとに作成する。成功・失敗・キャンセル時は `--rm` とラベル検証付きの後片付けで、そのジョブのコンテナを削除してから終了状態を保存する。削除できなければ `unknown` と `cleanup_error` を表示し、GPU の予約を自動解放しない。

スケジューラ用の tmux セッションは各サーバーに1つ。`overview` ウィンドウを次回の投入用に残し、ジョブごとにウィンドウを追加する。成功・失敗・キャンセル後はそのウィンドウだけを閉じる。ジョブがなくなってもセッション数は1つのままで、SSH 接続は残らない。既存の対話用 tmux とは別のソケットを使い、そちらのセッションには触れない。

手動で画面を見たい場合は、対象サーバーに入って `tmux -L job-scheduler attach -t jobs` で接続できる。通常は `logs --running` でまとめてログを取得できる。終了済みジョブのログは tmux のウィンドウが閉じた後も取得できる。

ジョブのコンテナ内だけに書き込んだファイルはコンテナ削除時に消えるため、結果は SAN のマウント先へ保存する。キャッシュイメージはテンプレートのコンテナ ID ごとに1つ保持する。ジョブごとにイメージは作らない。結果・ログ・実行履歴と、リモートの要求・ワーカーファイルは保存するので、ディスク上の履歴は増える。ホスト停止や強制終了で `unknown` になった場合は状態を調べてから対処する。

ローカルの接続は ControlMaster / ControlPersist を無効にしており、接続共有のために SSH を残さない。リモートのワーカーから SSH で折り返すこともない。

同じ SSH ユーザーのジョブ間は、リモートのホスト別予約とファイルロックで GPU の重複割当てを防ぐ。SSH の応答が途中で失われても、同じジョブ ID の再要求で二重実行しない。

## 複数 PC で使う

同じ SSH ユーザーで同じ SAN にアクセスする PC 間では、待機キュー・ジョブ情報・ログを共有できる。各 PC にこのツールを配置し、同じ `hosts.json` と接続先の SSH エイリアスを設定する。

```bash
# 別の PC で投入されたジョブも取り込んで確認
python3 scheduler.py status

# 別 PC のジョブのログ確認とキャンセル
python3 scheduler.py logs --running --lines 200
python3 scheduler.py cancel <ジョブID>
```

通常の投入ではジョブを SAN の共有キューへ公開し、投入先の割当ても SAN のロックで管理する。複数 PC が同時に `dispatch` しても、同じジョブを異なるサーバーで二重実行しない。`status` / `logs --running` は共有キューを取得し、他 PC の待機・実行・終了済みジョブも取り込む。

`shared_queue.host` の glockenspiel を問い合わせ先にし、接続できなければ他の設定済みサーバーへ切り替える。いずれも同じ SAN の共有キューを参照する。キューの読み書きはコマンド実行時だけで、常時接続は行わない。

`--queue-only` はその PC 内への登録のみ。次にその PC で `dispatch` するまで SAN に公開されない。`status --local` と `info` はその PC の最後に取得した情報を表示する。結果の rsync 済みフラグは PC ごとに保持するため、片方で同期済みでも、もう片方ではその PC へ取得できる。

共有キュー対応前に投入したジョブは、元の PC のローカル履歴に残る。稼働中の古いジョブを新しい共有ジョブとして再登録しない。

## GitHub で管理する

コード・`hosts.json`・`examples/`・`jobs/` は private リポジトリに保存できる。ジョブ定義や書き出した履歴 JSON も Git 管理の対象にする。

```bash
# 最後に取得した履歴を jobs/history.json へ保存（SSH 接続なし）
python3 scheduler.py export

# SAN とリモートの状態を取得してから保存
python3 scheduler.py export --refresh
```

`export --output jobs/別名.json` で保存先を変更できる。JSON にはコマンド・状態・ホスト・時刻・同期結果などを含む。`export --refresh` は結果の rsync を実行しない。commit / push は手動で行う。

実行中に更新される `state/queue.sqlite3` とロックファイルは `.gitignore` で除外する。複数 PC の稼働状態は SAN が基準で、Git の JSON は実験履歴の記録に使う。

## 実行上の注意

- コマンドはフォアグラウンドで実行する。末尾の `&`、`nohup`、ジョブ内の追加 tmux などで切り離すと、実際の学習処理の終了コードを追跡できない。
- 各サーバーには事前に必要なコード、設定、モデルがあること。実行中に Git pull やコンテナ再作成をしない。
- 複数ジョブが同じ出力先へ書き込まないよう、実験名・出力先を分ける。全ホストは SAN で作業ファイルを共有するため、ホストが異なっても同じファイルへの書込みが競合する。
- tmux は SSH 切断からジョブを守る。コンテナの停止やサーバーの再起動からは守れない。
- コマンドや引数は履歴ファイルに保存されるので、秘密情報は書き込まない。

## 設定の変更

`hosts.json` の既定値は、ホストごとに上書きできる。

`runtime_image` を指定すると、その準備済みイメージを使い、自動スナップショットの作成を省略できる。自動キャッシュにテンプレート内の依存関係の変更を反映したい場合は、全ジョブの終了後に `docker image ls job-scheduler-runtime` で対象を確認し、対象のキャッシュタグを `docker image rm <タグ>` で削除する。次のジョブで再作成される。テンプレートを作り直した場合は新しいコンテナ ID 用のキャッシュを作るため、不要になった旧キャッシュは手動で削除できる。

```json
{
  "ssh": "glockenspiel",
  "container": "別のコンテナ名",
  "workdir": "/別の作業ディレクトリ"
}
```

設定ファイルやローカル状態の保存先を分ける場合は、グローバル引数をサブコマンドの前に置く。

```bash
python3 scheduler.py --config hosts.json --state-dir /tmp/job-scheduler-demo gpus
```

## テスト

```bash
python3 -m unittest -v test_scheduler.py
```

実 GPU、SSH、実コンテナを使わずに、GPU の選択、一括投入、共有キュー、複数 PC の同時投入、キャンセル、専用コンテナの削除、一括ログ、結果同期、履歴エクスポートを検証する。Docker はテスト用の代替プログラムを使い、tmux は専用ソケットの隔離したサーバーでウィンドウの後片付けまで確認する。tmux のローカルソケット作成が制限されたサンドボックスでは、通常のターミナルから実行する。
