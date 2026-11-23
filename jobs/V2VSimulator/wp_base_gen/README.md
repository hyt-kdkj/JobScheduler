# WP-Base-Gen の実行条件

学習は H120 と U200 の計 6 件。評価は Highway と Urban を別ファイルに分け、描画は低密度 1 ファイルにまとめる。学習・評価温度は 0.05。

Phase 1 のバッチは `--epoch-batch-rows 512` を使う。評価・描画にも同じ値を渡して学習済み checkpoint を照合する。`--mobility-trace-cache` は Urban の軌跡を再利用する。Q 値の絶対値と float32 の比較を維持するため、バッチはフルネットワークで推論する。
