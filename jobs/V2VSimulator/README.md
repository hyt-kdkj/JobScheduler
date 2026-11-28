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
| `baseline/` | SPS・IPMI、H120/U200 と H300/U500（既定 TR 36.885 および 5G NR 伝搬 `tr37885`） | `evaluate_low_density.json`、`evaluate_high_density.json`、`evaluate_tr37885_low_density.json`、`evaluate_tr37885_high_density.json` |
| `gen_abs8/` | 絶対8方位、H120/U200 ＋ 選抜 H300/U500（`269999`, `299999`） | `train.json`、`evaluate_{low,high}_density.json`、`plot_{low,high}_density.json` |
| `gen_ego6/` | 自車基準6セクタ、H120/U200 ＋ 選抜 H300/U500（`299999`） | `train.json`、`evaluate_{low,high}_density.json`、`plot_{low,high}_density.json` |
| `ego_rsrp/` | 自車基準6セクタ・復号 RSRP 時系列、H120/U200 ＋ 選抜 H300/U500（`269999`, `299999`；動的期待 Q 値 2 段階 `expected_q` 選抜 `p0.9_top{1.0,0.5}`） | `train.json`、`evaluate_{low,high}_density.json`、`plot_{low,high}_density.json`、`evaluate_expected_q_{low,high}_density.json`、`plot_expected_q_{low,high}_density.json` |
| `uni_mix/` | 単一統合モデル（`--channel-model tr37885`・固定メートル `ego6_m4`・81次元ピュア無線観測・物理アンカー撤廃・H120+U200 50:50 混合学習・エンコーダ `transformer` / `lstm` 両学習；0-byte 低密度全3スロット＋選抜高密度 `239999`/`269999`、`top_fraction=0.5` 低密度比較 `239999`、および 3GPP Rel-17 IUC 新方式低密度比較 `iuc_exclude`/`iuc_weight` × `2`/`3` 条件） | `train.json`（2 ジョブ）、`evaluate_{low,high}_density.json`（6 / 4 ジョブ）、`plot_{low,high}_density.json`（6 / 4 ジョブ）、`evaluate_top50_low_density.json`（2 ジョブ）、`plot_top50_low_density.json`（2 ジョブ）、`evaluate_iuc_low_density.json`（8 ジョブ）、`plot_iuc_low_density.json`（8 ジョブ） |
| `uni_mix2/` | TR 37.885、H120+U200 50:50 の単一重み学習（`transformer` / `lstm`）、評価・描画は Transformer の低密度 2 条件 × 3 保存スロット。学習時 IUC オフ、評価時に後付け可 | `train.json`（2 ジョブ）、`evaluate_low_density.json`（6 ジョブ）、`plot_low_density.json`（6 ジョブ） |
| `uni_mix3/` | TR 37.885、固定メートル `ego6_c3`、H120+U200 50:50。両エンコーダを低密度 2 条件 × 3 保存スロットで比較 | `train.json`（2 ジョブ）、`evaluate_low_density.json`（12 ジョブ）、`plot_low_density.json`（12 ジョブ） |

提案法は H120/U200 で各 300,000 step 学習し、保存スロット 239999、269999、299999 を低密度で評価・描画したうえで、上位候補のみ高密度（H300/U500）へ選抜して追加計測する。先に共通ベースラインを計測すると、提案法の評価と描画で同じキャッシュを参照できる。不採用・比較完了となった `top25`・`simple`・`boltzmann` バッチは整理・削除済み。

`ego_rsrp/evaluate_expected_q_low_density.json` と `plot_expected_q_low_density.json` は、`expected_q_two_stage`（`max_hold_prob ∈ {1.0, 0.9, 0.8}`, `top_fraction ∈ {1.0, 0.5}`）を低密度先行（各 12 ジョブ）で評価・描画する。低密度上位 2 条件の **`p_max=0.9, top_fraction ∈ {1.0, 0.5}`** を選抜し、`evaluate_expected_q_high_density.json` / `plot_expected_q_high_density.json`（各 4 ジョブ、H300/U500）で高密度検証する（不採用となった `T=0.01` バッチは削除済み）。SPS・IPMI のキャッシュは固定スケールと共用する。

`uni_mix3` は `expected_q_two_stage`（保持上限 0.9、全代替、温度 0.03）で統一し、評価・描画で対応する単一混合チェックポイントを参照する。学習は IUC オフ。描画の `--plot-only` は評価済みキャッシュを読む。

```bash
python3 scheduler.py submit-batch jobs/V2VSimulator/ego_rsrp/evaluate_low_density.json --dry-run
```
