# 论文实验套件（级联系统 + 违规判断）

这一目录把论文要用到的实验**固化成可复现脚本**（原先散落在 gitignore 的 `data/logs/` 下，
不入库、不可复现）。所有脚本只读产物、可续跑、失败不静默。

## 0. 一句话结论

| 主张 | 证据 | 脚本 |
|---|---|---|
| **违规判断**：规则感知的级联决定性优于文献无监督异常检测器 | 注入集上 L1 = **100% P/R/F1**；8 个文献方法的 oracle F1 全部 ≤ 77.9%（7 个恰为 66.7% 的平凡线），McNemar p < 1e-20 | `benchmark/eval_inject.py`、`benchmark/baselines.py`、`stats_tests.py` |
| **级联**：以近乎零的 LLM 成本达到最优质量，帕累托支配纯 LLM | 门控 τ 扫描：**0 次调用 → 100% F1**；100% 调用率（240 次、1.21M prompt token）→ 64.3% F1，全链单调下降 | `llm_arms.py`、`cascade_pareto.py` |
| **外部体检**：文献报告的高分在非循环的真实基准上复现不出来 | HawaiiCoast_GT 11064 窗：复现 oracle F1 **3.9–6.4%**（文献报 51–95%），@1%FPR 召回 ≤2.8%、一半为 0 | `benchmark/hawaii_eval.py`、`make_tables.py` |
| **级联的独立价值**：规则盖不到的类别，L3 能捞回来 | 规则覆盖消融：摘掉规则后 L1 召回 0%，**L3 召回 100% / 20.8%** | `rule_coverage.py` |

完整表见 `论文/实验结果_汇总.md`，结论解读见 `论文/实验结论与写作说明.md`。

## 0.1 三轮子实验（不需要额外 LLM 成本）

- `cascade_pareto.py`：**复用 F 臂的逐船判决**，扫门控阈值 τ 即得整条成本-质量曲线。
  换工作点只改变"调用哪几条"，不改变任何一条船的判决内容 → 曲线上的点互相可比。
- `rule_coverage.py`：把某类违规**从 L1 判定规则里摘掉**，量后两级的补偿能力。
- `stats_tests.py`：基线一律用 **oracle 阈值**（它们的上界），我们用**部署即所得**的固定判据。

## 1. 数据

| 数据集 | 用途 | 来源 |
|---|---|---|
| **注入集** `data/logs/inject_v1.{csv,gt.json}` | 违规判断的主战场：240 条合成船（120 违规 / 120 合规），真值由场景设计给定，并用**独立参照实现**回放校验 | `benchmark/inject_violations.py` 从 `data/input/_fzais1009_converted.csv` 生成 |
| **HawaiiCoast_GT** `实验数据/HawaiiCoast_GT_GitHub/` | 外部体检：真实事故真值（美国海岸警卫队报告），与我们的实现无关 | `benchmark/hawaii_eval.py --build`（从 MarineCadastre 日包抽取） |

> ⚠ **两个数据集测的不是同一个任务**。注入集测「违没违反避碰规则」，
> HawaiiCoast_GT 测「这条船出没出事」（拖带异常/推进失效/搁浅…）。
> 论文里必须分开写，不能把两边的数直接相减比高低。

## 2. 复现顺序

```bash
# ① 生成注入集（240 场景 = 120 违规 / 120 合规）
python benchmark/inject_violations.py --scenes 240 --out data/logs/inject_v1

# ② 违规判断：我们的 L1 + 8 个文献基线（同一评测集、同一判据）
python benchmark/eval_inject.py --gt data/logs/inject_v1.gt.json
python benchmark/baselines.py --only all --out data/logs/baselines_inject.json

# ③ 级联成本-质量：7 个对照臂（含真实 token 计量，可续跑）
python experiments/paper/llm_arms.py --arms A B C D E F G --threads 2
python experiments/paper/cascade_pareto.py                 # 复用 F 臂判决，零额外调用
python experiments/paper/rule_coverage.py                  # 规则覆盖消融，零额外调用

# ③b 门槛敏感度扫描（回应「门槛是不是照测试集调的」）
python benchmark/sweep_inject.py

# ④ 公开基准外部体检（续跑安全，逐日缓存）
python benchmark/hawaii_eval.py --build
python benchmark/hawaii_eval.py --run --only all
python data/logs/l1_hawaii.py

# ⑤ 统计检验与汇总
python experiments/paper/stats_tests.py
python experiments/paper/make_tables.py
python experiments/paper/figures.py
```

## 3. 对照臂（`llm_arms.py`）

| 臂 | 谁来判 | LLM 调用 |
|---|---|---|
| A | L1 确定性规则引擎 | 0 |
| B | L2 LightGBM 风险门控 | 0 |
| C | 生产链路 L1 → L2 → L3 | 只给门控放行的船 |
| D | 原始轨迹（本船 + 邻近船）→ LLM | 全部 |
| E | RDP 压缩轨迹 → LLM | 全部 |
| F | L1 标注轨迹 → LLM（跳过 ML 门控） | 全部 |
| G | FrugalGPT 式两级（短提示 → 不确定才升级） | 1~2 |

**D/E/F 都把 3 海里内的邻近船轨迹一并喂进去**——违规住在两船的关系里，
只给本船轨迹再强的 LLM 也判不出来，那不是对比是稻草人。

**成本口径**：token 数取自 Ollama 原生 `/api/chat` 的 `prompt_eval_count` /
`eval_count`，是**实测值**，不是"字符数 ÷ 4"。
`num_ctx=8192` 必须显式给：Ollama 按显存推的默认上下文只有 4096，
超长提示会被**静默截断**（不报错，只是模型看到半截轨迹）。

## 4. 防自欺清单

- ✅ 真值不循环：注入集真值来自场景设计 + 独立参照实现，**不是**规则引擎的输出；
  Hawaii 真值来自海岸警卫队报告。
- ✅ 基线用 **oracle 阈值**（它们的上界），我们用**部署即所得**的固定判据。
  这是对我们最不利、对基线最有利的比法。
- ✅ `@1%FPR` 列不碰真值，是对得上部署现实的数字。
- ✅ 成对检验（McNemar 精确 + 配对自助 2000 次 95% CI），不只报点估计。
- ✅ 阈值敏感度扫描（`data/logs/sweep_inject.py`）证明门槛不是照着测试集挑的。
- ✅ `temperature=0` + 固定 `seed`；Ollama 模型版本写进产物。
- ✅ 缓存带指纹（数据集/代码版本变了就重建），避免静默复用旧特征。
- ✅ 未跑完的实验在汇总表里写「未完成」，**绝不补零**（补零会把"还没跑"伪装成"跑得很差"）。

## 5. 运行环境

- Python 3.13 + `lightgbm` / `numpy` / `scikit-learn` / `matplotlib`
- LLM：Ollama `qwen2.5:7b-instruct-q4_K_M`（本机 RTX 4060 Laptop 8 GB）
  **必须以 `OLLAMA_MODELS=E:\model` 启动**，否则模型清单为空、`/api/chat` 返回 404。
- 时长：`llm_arms.py` 全臂约 1~2 小时（240 船 × ~5 次调用），逐条落盘、随时可中断续跑。
