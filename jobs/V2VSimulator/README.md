# V2VSimulator のジョブ定義

このディレクトリの実行プロジェクトは `V2VSimulator` です。各 SSH 先で `~/workspace/V2VSimulator` のソースと必要なモデルを参照します。

---

## 運用ルール・命名規則（厳守）

### 1. 高密度シナリオ（Highway 300 / Urban 500）の選抜追加計測ポリシー
- 高密度シナリオは計測時間が長いため、**すべての実験やパラメータ格子を高密度で網羅実行しない**。
- **最初から `evaluate_*_high_density.json` / `plot_*_high_density.json` を作成してはいけない**。必ずまず **低密度（Highway 120 / Urban 200）** のバッチのみを作成・実行し、低密度でうまくいった条件の数件だけを選抜した後に、初めて高密度の追加計測用バッチを作成する。

### 2. ディレクトリ構成とファイル命名規則
- **ディレクトリ = 実験系統（トラック）**：1 実験系統ごとに 1 フォルダを切る（ファイル名にフォルダ名の重複接頭辞は付けない）。
- **密度分離**：`evaluate` と `plot` は必ず **低密度（`low_density`）と高密度（`high_density`）** で別ファイルに分ける（Highway と Urban は同一ファイルにまとめる）。
- 各フォルダ内のファイル名は次の統一形式とする：
  - 学習：`train.json`
  - 評価：`evaluate[_<variant>]_{low,high}_density.json`
  - 描画：`plot[_<variant>]_{low,high}_density.json`（対応する `evaluate_*.json` と 1 対 1 で同名対応）

---

## ディレクトリ一覧（単一統一候補 `WP-Base-Gen` 系統＋ベースラインのみ）

| ディレクトリ | 対象実験 | 含まれるバッチ（件数） |
|---|---|---|
| `baseline/` | SPS・IPMI ベースライン（H120/U200、H300/U500） | `evaluate_low_density.json` (4), `evaluate_high_density.json` (4) |
| `wp_base_gen/` | WP-Base-Gen 学習・ベース評価（`gen_prr1` / `gen_prr2`：`camhist6`＋`alt_rank`＋`advinv`＋`physres`） | `train.json` (4), `evaluate_low_density.json` (4), `plot_low_density.json` (4), `evaluate_high_density.json` (2), `plot_high_density.json` (2) |
| `wp_base_infer/` | **【最新主力】WP-Base-Infer**（`Gen_prr1` / `Gen_prr2` 既存重み＋推論最適化＋相対物理バイアス） | v1：`evaluate_{low,high}_density.json` (各 5), `plot_{low,high}_density.json` (各 5)<br>**v2（実行対象）**：`evaluate_v2_low_density.json` (8), `plot_v2_low_density.json` (8)（※v2 高密度は低密度結果から数件選抜後に作成） |

---

## 実行例

```bash
# JobScheduler ルートで実行（投入時は --dry-run を外す）
python3 scheduler.py submit-batch jobs/V2VSimulator/wp_base_infer/evaluate_v2_low_density.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/wp_base_infer/plot_v2_low_density.json --dry-run
```
