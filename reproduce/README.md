# 论文实验复现包（`reproduce/`）

> 两条主线：**① 违规判断准确性 ② 级联系统是否又快又准（成本-质量）**
> 本包 = 把论文全部实验固化成「一条命令可跑、可中断续跑、结果可逐项比对」的复现套件。
> 多人实验时，**每个人跑完把 `check_results.py` 的输出贴回来**即可判定结果是否相同。

---

## 1. 包里有什么

| 文件 | 作用 | 产出 |
|---|---|---|
| `run_all.py` | **一键编排**（分 4 阶段，可单独跑） | — |
| `check_results.py` | 把本次结果与基准快照逐项比对 | 一致性报告 |
| `expected.json` | **基准数字快照**（论文里的数） | 判定依据 |
| `inject_violations.py` | 注入集生成器（真值来源，条款级） | `data/logs/inject_v1.{csv,gt.json}` |
| `eval_inject.py` | 我们的 L1 在注入集上 | 表 1「我们」一行 |
| `baselines.py` | 8 个文献无监督检测器 | 表 1 其余各行 |
| `sweep_inject.py` | 门槛敏感度扫描（证伪测试） | 表 4 |
| `compare_colreg.py` | L1 vs 独立参照实现对拍 | 对拍一致率 |
| `sweep_thresholds.py` | 对拍的门槛扫描 | — |
| `llm_arms.py` | 9 个「谁来判」对照臂（A–I，实测 token） | 表 3 |
| `cascade_pareto.py` | 级联成本-质量前沿（τ 扫描，复用缓存） | 表 2 + 图 2 |
| `rule_coverage.py` | 规则覆盖消融 | 表 5 |
| `stats_tests.py` | McNemar 精确 + 配对自助 95% CI | `论文/显著性检验.md` |
| `make_tables.py` | 汇总成论文表 | `论文/实验结果_汇总.md` |
| `figures.py` | 出图 | `data/logs/paper/figures/*.png` |
| `final_compare.py` | 最终对比 | `论文/最终对比_我们的vs论文.md` |
| `gate_design.py` | 门控判据对比（优化 O1） | 优化文档用数 |
| `gate_on_hawaii.py` | 真实数据调用成本（**需本地真实数据，不在标准流程内**） | 6.2% 调用率 |

> **为什么 `benchmark/baselines.py` 与 `benchmark/reference/colreg_ref.py` 没搬进来**
> 它们被**生产包** `triage_engine/window_feat.py` 直接 import（不是实验专用）。搬走会打断生产链路。
> 本包通过 `from benchmark.reference import colreg_ref` 正常引用它们，无需额外操作。

---

## 2. 环境

```
Python 3.13
numpy / scikit-learn / lightgbm / matplotlib
Ollama（仅大模型阶段需要）
```

- 模型：`qwen2.5:7b-instruct-q4_K_M`
- **Ollama 必须以 `OLLAMA_MODELS=E:\model` 启动**，否则模型清单为空、`/api/chat` 返回 404。
- 显式设上下文长度 `num_ctx=8192`：Ollama 按显存推的默认值只有 4096，长提示会被**静默截断**。
- 并发 `OLLAMA_NUM_PARALLEL=2`（3 路会因显存溢出反而慢约 15 倍）。
- **不要** `export OLLAMA_BASE_URL`（会被脚本覆盖成 localhost）。

### 前置输入（必须逐一核对 md5，否则数字必然对不上）

| 文件 | 大小 | md5 |
|---|---|---|
| `data/input/_fzais1009_converted.csv` | 59,496,470 | `e3719f063d2c7bf10c98f4cd34aa5b2b` |
| `triage_model.txt`（L2 模型） | 1,259,192 | `95c90f22efa062bb962949876da3f73e` |
| `data/logs/inject_v1.gt.json` | 195,560 | `a48e8081ba02e52b9605f4637b3e3bcc` |

（`inject_v1.gt.json` 由步骤 ① 生成；如果 md5 不同，说明生成器的随机种子或输入数据变了。）

---

## 3. 怎么跑

```bash
# 看全部阶段与命令
python reproduce/run_all.py --list

# ① 确定性部分（不需要 Ollama，约 15 分钟）
python reproduce/run_all.py --stage det

# ② 大模型部分（需要 Ollama，约 1~2 小时；逐条落盘，断电重启同命令即续跑）
python reproduce/run_all.py --stage llm

# ③ 汇总出表出图
python reproduce/run_all.py --stage report

# ④ 与基准快照比对（多人实验就看这一步的输出）
python reproduce/run_all.py --stage check
```

或全流程一把梭：`python reproduce/run_all.py --stage all`

**零额外 token 成本说明**：`cascade_pareto.py` 与 `rule_coverage.py` **复用 `llm_arms.py` 缓存的大模型判决**——
换门控策略只改变「调用哪几条」，不改变任何一条判决的内容，所以各工作点严格可比，且不必重新调模型。

---

## 4. 预期结果（基准快照，`expected.json` 是机器可读版本）

### 表 1 违规判断（注入集 240 = 120 违规 / 120 合规）

| 方法 | AUC | oracle F1 | @1%FPR 召回 |
|---|---|---|---|
| **我们：L1 规则引擎** | — | **100.0%** | **100.0%（FPR = 0）** |
| proximity | 0.843 | 77.9% | 23.3% |
| lof | 0.368 | 66.9% | 0.0% |
| iforest | 0.292 | 66.7% | 0.0% |
| dbscan | 0.376 | 66.7% | 0.0% |
| vae | 0.383 | 66.9% | 6.7% |
| lstm_ae | 0.378 | 66.7% | 3.3% |
| vae_lstm | 0.423 | 66.7% | 5.0% |
| transformer_lstm | 0.391 | 66.7% | 3.3% |

我们：`tp=120 fp=0 fn=0 tn=120`。平凡线（全判违规）= 66.7%。

### 表 2 成本-质量前沿（τ 扫描，末段）

| LLM 调用 | 调用率 | F1 |
|---|---|---|
| 0 | 0.0% | **100.0%** |
| 1 | 0.4% | **100.0%** |
| 15 | 6.2% | 98.8% |
| 83 | 34.6% | 88.5% |
| 240 | 100.0% | 64.3% |

（共 17 个点，`expected.json → cascade_pareto.curve`。**单调下降**：最优调用数是 0。）

### 表 3 对照臂（同一批 240 船、同一份真值）

| 臂 | 谁来判 | F1 | LLM 调用 | prompt token |
|---|---|---|---|---|
| A | L1 确定性规则 | **100.0%** | **0** | **0** |
| B | L2 LightGBM 门控 | 66.7% | 0 | 0 |
| C | 生产链路 L1→L2→L3 | 100.0% | 240 | 344,337 |
| D | 原始轨迹 → LLM | 47.7% | 240 | 703,803 |
| E | RDP 压缩 → LLM | 58.7% | 240 | 270,596 |
| F | L1 标注 → LLM | 64.3% | 240 | 1,208,212 |
| G | FrugalGPT 式两级 | 73.1% | 240 | 380,053 |
| H | 几何 + 判据 → LLM | 67.2% | 240 | 169,846 |
| I | 判据写成提示 → LLM | 70.6% | 240 | 246,646 |

### 表 4 门槛敏感度扫描（5 个门槛，峰值全在生产值处或邻位）

`give_way_close_quarters_nm`: 0.05→58.0 · 0.1→66.7 · **0.2→100** · 0.3→100 · 0.5→90.9 · 1→90.9
`give_way_alteration_degrees`: 5→58.0 · 15→89.9 · **30→100** · 45→97.6 · 90→91.6
`give_way_speed_drop_knots`: 0.5→92.4 · 1→100 · **2→100** · 3→100 · 5→91.3
`overtaking_unsafe_distance_nm`: 0.02→75.0 · 0.05→76.9 · **0.1→100** · 0.2→90.2 · 0.5→82.8
`overtaking_speed_diff_knots`: 0.2→100 · 0.5→100 · **1→100** · 2→99.2 · 5→75.0

### 表 5 规则覆盖消融

| 违规类型 | L1 召回(有规则) | L1 召回(摘掉规则) | L2 风险分 AUC | L3(LLM) 召回 |
|---|---|---|---|---|
| give_way_failure | 100% | 0% | 0.244 | **100%** |
| overtaking_violation | 100% | 0% | 0.305 | **20.8%** |

---

## 5. 一致性判定规则（多人实验）

| 部分 | 容差 | 不一致意味着 |
|---|---|---|
| 表 1、表 4、表 5、对拍（纯 L1 规则 + 文献基线） | **1e-6**（必须一致） | 环境/数据/代码被改动过 → 复现失败，必须查清 |
| 表 2、表 3（含大模型） | F1 **±0.03**；调用次数一致；token **±1%** | 属**正常抖动**（Ollama 并行槽位下浮点累加顺序不同，非严格确定性） |

`check_results.py` 已按上表实现，退出码：确定性部分不一致 → `1`；仅大模型抖动 → `0`（但会显式标注）。

**先自查环境**再怀疑代码，顺序：
1. 三个前置文件的 **md5**（见 §2）；
2. `python -c "import lightgbm, sklearn, numpy; print(lightgbm.__version__, sklearn.__version__, numpy.__version__)"`；
3. 是否设了 `OLLAMA_MODELS` / `num_ctx` / `OLLAMA_NUM_PARALLEL`；
4. 是否有旧的 `data/logs/paper/llm_cache/` 残留（缓存带指纹，正常会自动重建；若手改过产物请先备份再删）。

### 多人协作流程（谁跑完谁贴这一份）

```bash
python reproduce/check_results.py --save data/logs/paper/repro_report_<你的名字>.json
```

报告里含三块：**环境指纹**（机器 / Python / numpy、sklearn、lightgbm、matplotlib 版本 + 三个前置文件的 md5）、**比对结论**（一致 / 基本一致 / 不一致）、**逐项差异**清单。

收集到多份报告后的判定顺序：

1. **先比环境指纹**：三个输入文件的 md5 有任何不同 → 数字没有可比性，先统一输入，别急着比数字。
2. **再比结论**：大家都"一致" → 复现成功。
3. **有人"基本一致"**（只有大模型部分抖动）→ 正常，看抖动量是否在 F1 ±0.03 内。
4. **有人"不一致"**（确定性部分对不上）→ 看差异项的 `path`，按 §5 的四步自查；把两份报告一起贴出来即可定位。

---

## 6. 不在本包内的部分

| 项 | 原因 |
|---|---|
| 真实数据上的 LLM 端到端复测 | 需要逐条调用大模型，机时量级约 15 小时 |
| 容器内复测线上口径 | 属部署收尾，见《优化文档/04》§十三 |
| 组合门控的阈值标定 | 历史产物口径不一致，尚未标定 |

---

## 7. 相关文档

| 文档 | 内容 |
|---|---|
| `论文/实验文档/`（5 篇） | 实验需求 → 可行性 → 设计 → 结果 → 验证 |
| `论文/优化文档/`（4 篇） | 门控优化的目的 → 设计 → 结果 → 验证 |
| `论文/实验多维度验证及重要结论说明文档.md` | 九个验证维度 + 结论边界 + 口径冲突排查 |
| `论文/实验结果_汇总.md` | 表 1–5（由 `make_tables.py` 生成） |
| `docs/论文实验交接.md` | 每次开工先读这一份 |
