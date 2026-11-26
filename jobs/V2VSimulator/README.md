# V2VSimulator のジョブ定義

このディレクトリの実行プロジェクトは `V2VSimulator` です。各 SSH 先で `~/workspace/V2VSimulator` のソースと必要なモデルを参照します。

---

## 運用ルール・命名規則（厳守）

### 1. 高密度シナリオ（Highway 300 / Urban 500）の選抜追加計測ポリシー
- 高密度シナリオは計測時間が長いため、**すべての実験やパラメータ格子を高密度で網羅実行しない**。
- **最初から `evaluate_*_high_density.json` / `plot_*_high_density.json` を作成してはいけない**。必ずまず **低密度（Highway 120 / Urban 200）** のバッチのみを作成・実行し、低密度でうまくいった条件の数件だけを選抜した後に、初めて高密度の追加計測用バッチを作成する。共通 SPS・IPMI の単体計測は先に定義する。

### 2. ディレクトリ構成とファイル命名規則
- **ディレクトリ = 実験系統（トラック）**：1 実験系統ごとに 1 フォルダを切る（ファイル名にフォルダ名の重複接頭辞は付けない）。
- **密度分離**：`evaluate` と `plot` は必ず **低密度（`low_density`）と高密度（`high_density`）** で別ファイルに分ける（Highway と Urban は同一ファイルにまとめる）。
- 各フォルダ内のファイル名は次の統一形式とする：
  - 学習：`train.json`
  - 評価：`evaluate[_<variant>]_{low,high}_density.json`
  - 描画：`plot[_<variant>]_{low,high}_density.json`（対応する `evaluate_*.json` と 1 対 1 で同名対応）

---

## ディレクトリ一覧

| ディレクトリ | 条件 | バッチ |
|---|---|---|
| `baseline/` | SPS・IPMI、H120/U200 と H300/U500 | `evaluate_low_density.json`、`evaluate_high_density.json` |
| `gen_abs8/` | 絶対8方位、H120/U200 ＋ 選抜 H300/U500（`269999`, `299999`） | `train.json`、`evaluate_low_density.json`、`plot_low_density.json`、`evaluate_high_density.json`、`plot_high_density.json` |
| `gen_ego6/` | 自車基準6セクタ、H120/U200 ＋ 選抜 H300/U500（`299999`） | `train.json`、`evaluate_low_density.json`、`plot_low_density.json`、`evaluate_high_density.json`、`plot_high_density.json` |
| `ego_rsrp/` | 自車基準6セクタ・復号 RSRP 時系列、H120/U200（低密度評価前） | `train.json`、`evaluate_low_density.json`、`plot_low_density.json` |

提案法は H120/U200 で各 300,000 step 学習し、保存スロット 239999、269999、299999 を低密度で評価・描画したうえで、上位候補のみ高密度（H300/U500）へ選抜して追加計測する。先に共通ベースラインを計測すると、提案法の評価と描画で同じキャッシュを参照できる。

`gen_abs8/` と `gen_ego6/` の `evaluate_top25_low_density.json` は、既存の保存スロット 269999・299999 を H120/U200 で `--policy-scale-mode top25` に切り替えて評価する。SPS・IPMI のキャッシュは固定スケールの評価と共用する。描画時も評価と同じ引数で `plot_evaluation.py` に `--policy-scale-mode top25` を渡す。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator/gen_ego6/train.json --dry-run
```
