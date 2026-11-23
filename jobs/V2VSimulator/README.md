# V2VSimulator のジョブ定義

このディレクトリの実行プロジェクトは `V2VSimulator` です。各 SSH 先で `~/workspace/V2VSimulator` のソースと必要なモデルを参照します。

---

## 運用ルール・命名規則（厳守）

### 1. 高密度シナリオ（Highway 360 / Urban 500）の選抜追加計測ポリシー
- 高密度シナリオは計測時間が非常に長いため、**すべての実験やパラメータ格子を高密度で網羅実行しない**。
- まず **低密度（Highway 120 / Urban 200）** で評価・比較し、**低密度で性能が優れていたもの・確認したい気になる条件だけ** を選抜して、高密度の追加計測用バッチ（`evaluate_*_high_density_{highway,urban}.json` / `plot_*_high_density.json`）を作成する。

### 2. ディレクトリ構成とファイル命名規則
- **ディレクトリ = 実験系統（トラック）**：1 実験系統ごとに 1 フォルダを切る（ファイル名にフォルダ名の重複接頭辞は付けない）。
- **密度分離**：`evaluate` と `plot` は必ず **低密度（`low_density`）と高密度（`high_density`）** で別ファイルに分ける。
- **シナリオ分離（`evaluate` のみ）**：`evaluate` はさらに **`highway` と `urban` で別ファイルに分ける**（Highway 学習完了後に Urban 完了を待たず即評価できるようにするため）。**`plot` はシナリオで分けず密度ごとに 1 ファイルにまとめる。**
- 各フォルダ内のファイル名は次の統一形式とする：
  - 学習：`train.json`
  - 評価：`evaluate[_<variant>]_{low,high}_density_{highway,urban}.json`（通常多 seed 評価のみ `evaluate_full_{highway,urban}.json`）
  - 描画：`plot[_<variant>]_{low,high}_density.json`（通常多 seed 評価のみ `plot_full.json`）

---

## ディレクトリ一覧

| ディレクトリ | 対象実験 | 含まれるバッチ |
|---|---|---|
| `baseline/` | SPS・IPMI ベースライン | `evaluate_{low,high}_density_{highway,urban}.json` |
| `method-0/` | 従来 Propose（`camhist3`・WP1-A/B） | `train.json`, `evaluate[_two_stage]_low_density_{highway,urban}.json`, `plot[_two_stage]_low_density.json` |
| `wp2/` | WP2（`camhist4`・二段階選択） | `train.json`, `evaluate[_two_stage]_low_density_{highway,urban}.json`, `plot[_two_stage]_low_density.json` |
| `wp3/` | WP3（`camhist3`・3 頭分解・再選択 Replay・Expected-Softmax） | `train.json`, `evaluate_low_density_{highway,urban}.json`, `plot_low_density.json` |
| `wp_base_a/` | WP-Base-A（既存 `camhist4` 重み＋再学習不要の選択修正） | `evaluate_{low,high}_density_{highway,urban}.json`（高密度は選抜各 1 件：`p08 + ownhd` のみ）, `plot_{low,high}_density.json` |
| `wp_base_b/` | WP-Base-B（0-byte 抜本再設計：`camhist5`＋`prr1/2`＋`rc_epoch_mean`） | `train.json`, `evaluate[_sweep]_low_density_{highway,urban}.json`, `plot[_sweep]_low_density.json`（※高密度は低密度の優位条件を選抜後に別途作成） |
| `wp_base_gen/` | WP-Base-Gen（`camhist6`・代替 SPS 順位・密度不変 Advantage・物理残差・広窓探索） | `train.json`, `evaluate_low_density_{highway,urban}.json`, `plot_low_density.json`（※高密度は低密度の優位条件を選抜後に別途作成） |

---

## 実行例

```bash
# JobScheduler ルートで実行（投入時は --dry-run を外す）
python3 scheduler.py submit-batch jobs/V2VSimulator/baseline/evaluate_low_density_highway.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/wp_base_a/evaluate_high_density_highway.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/wp_base_b/evaluate_low_density_highway.json --dry-run
python3 scheduler.py submit-batch jobs/V2VSimulator/wp_base_b/plot_low_density.json --dry-run
```

バッチ間の依存関係は自動登録されないため、原則として `train.json` → 完了したシナリオから `evaluate_*_low_density_{highway,urban}.json` → 両シナリオ完了後に `plot_*_low_density.json` の順に実行し、低密度結果を確認してから選抜条件のみ `evaluate_*_high_density_{highway,urban}.json` → `plot_*_high_density.json` を作成・実行する。

---

## 現行の主実験（WP-Base-A / WP-Base-B / WP-Base-Gen）の推奨実行順

| 順 | バッチ | 内容 |
|---|---|---|
| 1 | `wp_base_a/evaluate_low_density_{highway,urban}.json` → `plot_low_density.json` | 既存 `camhist4` 重みでの低密度アブレーション（実施済み） |
| 1b | `wp_base_a/evaluate_high_density_{highway,urban}.json` → `plot_high_density.json` | 既存 `camhist4` 重みでの高密度選抜確認（気になる `p08 + ownhd` のみ H360・U500 各 1 件） |
| 2 | `wp_base_b/train.json` | B1（`camhist4`＋`prr1`）、B2（`camhist5`＋プール文脈＋`prr1`）、B2-r2（B2＋`prr2`）。H120・U200 各 250k step（計 6 件） |
| 3 | `wp_base_b/evaluate_low_density_highway.json` / `evaluate_low_density_urban.json` → `plot_low_density.json` | 全 3 モデル（B1 / B2 / B2-r2）の低密度評価（各シナリオ 14 件：`p ∈ {0.8, 0.9, 1.0}` 比較＋B2-r2 温度/帯域確認）。Highway は Urban 完了を待たず即投入可能 |
| 4 | （低密度結果確認後に作成）`wp_base_b/evaluate_high_density_{highway,urban}.json` → `plot_high_density.json` | 低密度で優位だった上位条件のみを選抜して H360 / U500 を追加計測 |
| 5 | `wp_base_gen/train.json` → `evaluate_low_density_{highway,urban}.json` → `plot_low_density.json` | ゼロショット密度汎化版（3 モデル × 2 シナリオ、学習・評価温度 `0.05`）。各シナリオ評価 15 件（固定上限 `0.8/0.9/1.0`＋RRI 別上限＋盲点時 `0.7` 上限） |
| 6 | （低密度結果確認後に作成）`wp_base_gen/evaluate_high_density_{highway,urban}.json` → `plot_high_density.json` | 低密度で優位だった上位 1〜2 条件のみを選抜して H360 / U500 のゼロショット汎化を確認 |

※ `wp_base_b/evaluate_sweep_low_density_{highway,urban}.json` / `plot_sweep_low_density.json` は `p0.8` 固定の補助格子スイープ（必要時のみ使用）。全バッチ `--path-layout v2`・スレッド数制限付きで、解析は既存の SPS/IPMI 測定キャッシュを再利用する。
