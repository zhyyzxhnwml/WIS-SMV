# 📝 更新日志

> 本项目尚未接入语义化版本（SemVer）与自动发布流程。以下为基于代码库演化记录整理的**主要里程碑**，日期来自对应改动时间点。

---

## 2026-10-15 — 禁渔判定用「速度代理」当「捕捞」：真实违规 0/2 全漏，且会误报锚泊货船

给系统找**不依赖自家规则**的真值来验收时，用 NOAA MPA Inventory 的官方保护区边界
（ArcGIS 公开服务，无需注册）配 MarineCadastre 的真实 AIS（**带船型**）扫了 210 天、
1300 万个点，得到 2 例货真价实的违规：两条商业渔船进入夏威夷 Kaho'olawe
Island Reserve（州立法全面禁止商业捕捞）。拿系统去判——**0/2，一条都没报出来**。

- **根因**：`illegal_fishing` 的判据是「区域内 + 船速 ≤ `low_speed_enter_knots`(3 节)
  + 持续 ≥5 分钟」，即把"捕捞"等同于"低速"。而法定口径是「**商业渔船**在该水域内」——
  是**船型 + 在场**，与速度无关。这两条船是以 5.4 / 8.6 节**过境**的，区内 ≤3 节的
  点数为 **0**，所以永远不可能触发。
- **反向误报**：反过来，速度判据会把任何在禁渔区低速锚泊的船（货船、客船）都当"捕捞"。
- **修法**：区域禁渔判定改为**船型优先**——有船型按船型判（该船属于被禁的那一类即违规，
  过境也算，不要求低速）；船型缺失才退回速度判据。只禁商业捕捞时，报通用渔船码(30)
  的船分不出商业/休闲，**降一档报为「疑似」交人工确认**（`commercial_status=suspect`），
  既不漏也不再谎称已确证。判定依据写进 `features.judge`，证据链自责。

### 顺带挖出两个连带缺陷

1. **`ShipProfile.ship_type` 一直是 `None`** —— `ingest.py` 构造时只传了 `mmsi`，
   规则匹配层从来拿不到船型。
2. **`run.py:convert_csv` 只保留 7 列，船型在转换这一步就被丢掉** ——
   即使加了字段也白搭。它还把 `VesselType` 小写成了 `vesseltype`，与下游读的
   `vessel_type` 对不上，现在统一归一化。

### 改动

`models.py`（`AISPoint` 加 `vessel_type`）、`quality.py`（解析 + 归一化透传）、
`ingest.py`（表加列 + **缓存格式版本号**，旧 sqlite 自动失效重建）、
`config.py`（`fishing_vessel_codes` / `commercial_fishing_vessel_codes`）、
`detect.py`（船型分支 + 疑似降档）、`run.py`（转换保留船型列）。

> 缓存版本号这一步不能省：不加的话旧 sqlite 没有 `vessel_type` 列，
> 加列成功但全为 NULL —— 船型静默丢失、判定悄悄退回速度代理，**结果看着正常其实已经不对**。

### 验证

- **真实真值**：2/2 召回，对照日 0 误报（修前 0/2）
- **回归**：注入集上让路失败 + 违规追越仍 100% P/R/F1；L1→L2 链路无变化
- **单测**：7 项判据全通过（`experiments/paper2/test_fishing_judge.py`），
  含「货船低速锚泊不报」这个被消掉的误报、以及船型缺失时的速度回退路径

### 构建

`.dockerignore` 补排除 `data/public` —— 该目录实测 2.6 GB（MarineCadastre AIS +
NOAA 边界 + GFW SAR 检测），运行时不需要，不排除会整包烘进镜像。

---

## 2026-10-10 — 智能体悬浮窗：标题被压成「智能…」、第二行穿到 tab 底下

用户说「智能体框框有点大，然后上面的字有点挤，不整齐」。截图取证后确认不是审美问题，是**真的坏了**：头部 62px 里，标题被截成「智能…」，而「Lv5 · 亲切自然，像朋友聊天 · 交互 94 次」整条**溢出到 tab 上面压过去**。

- **根因**：`FloatingAIChat.vue` 的 `.ai-window-title` 写的是 `flex: 1`，即 `flex-basis: 0`
  —— 基准宽度是 0。同一行里 4 个 tab 常驻 237px，400px 的窗口扣掉头像、收起按钮和三道
  gap 之后，**剩给标题的恰好 50px**（实测值）。于是 `strong`（有 `text-overflow: ellipsis`）
  被截断，`small`（只有 `white-space: nowrap`、**没有 overflow:hidden**）直接溢出父盒。
- **修法**：tab 移到第 2 行独占一行（`order: 5; width: 100%`）—— 移动端布局本来就是这么分的
  （文末那条媒体查询），这次只是提升为基础样式，移动端规则相应简化为只保留居中。
  标题在第 1 行拿到 297px，两行都完整。`small` 也补上 `overflow: hidden; text-overflow: ellipsis`，
  以后父盒再变窄也只是省略号，不会再压到别的元素上。
- 顺带把 `strong`/`small` 的行高从继承的 1.6 收到 1.35，头部从 62px 变 89px 但没有虚高。

### 两个悬浮球叠着「不整齐」

- 级联日志（`FloatingBall.vue`）和智能体（`FloatingAIChat.vue`）两个球右边缘差 **16px**，
  边框 2px vs 1px，图标盒 18 vs 22，标签字距 0.5 vs 1，徽章一个圆一个圆角矩形。
- **根因是两处都手写死的宽度常量没写对**：`FloatingBall` 写 `FAB_W = 100`（实际渲染 110），
  `FloatingAIChat` 写 `FAB_W = 104`（实际 98）。两边跑的是同一个
  `pos.left = innerWidth - FAB_W - 16`，常量错了右边自然对不上。
  → 改成 `onMounted` 时按 DOM 实量（`getBoundingClientRect().width`），并把
  `FloatingBall` 里另一处硬编码的 `- 100` 也换成 `FAB_W`。
- 几何量统一到级联日志那一套（边框 2px、`6px 14px 6px 10px`、gap 6、图标 18px、标签 13px/0.5px）。
  徽章两边都改成 `min-width: 20px` + 圆角矩形 —— 级联日志原来是固定 `20px` 的圆，
  「121」这种三位数会被挤变形。
- **结果**：右边缘 1424 / 1424 精确对齐（原 1434 / 1418），高度 34.4 / 34，边框、内边距、
  gap、圆角全部一致。

### 窗口默认尺寸 400×540 → 400×500

- 只减高度：它本来就高得空（一条欢迎语下面一大片空白）。**宽度不动** —— 试过收到 380，
  底部那排建议词立刻从 2 行挤成 3 行，反而更乱。
- `localStorage` 的 key 从 `ai-float-pos` 换成 `ai-float-pos-v2`：旧 key 里存的就是旧默认值
  本身（`savePos` 只在卸载时把当前值写回，并没有缩放手柄），沿用旧 key 等于改了没改。

---

## 2026-10-10 — 起草一直失败的真正原因：容器里的 ollama 是纯 CPU，且 4096 上下文超窗会砍掉系统提示词

上一条记完「`{2,47}` 修好了、但真实起草仍会超时」之后继续追，发现那条把原因归给「本地小模型就是这个量级」**是错的** —— 真凶在基础设施，两处都在 compose 里。

- **一、ollama 容器根本没有显卡，一直是 100% CPU。** compose 的 `ollama` 服务没配显卡直通，容器里连 `/dev/nvidia*` 都没有（`nvidia-smi` 在镜像里也不存在）。实测同一台机器上的两个实例：

  | | 处理器 | 出字速度 |
  |---|---|---|
  | 容器内 `ais-ollama`（**app 实际连的**） | `100% CPU` | 约 **4.2 token/秒** |
  | 宿主版 Ollama | `100% GPU` | **29.3 token/秒** |

  一份插件草稿约 1500 token → CPU 上要**六分钟**，而 `DRAFT_TIMEOUT_S` 是 240 秒 —— **数学上就不可能成功**，跟模型聪不聪明毫无关系。上一条把 4.2 token/秒 当成「本地就这样」是**归因错误**：这台机器有 RTX 4060（8 GB），只是容器没接上。
- **二、上下文只有 4096，而超窗时 ollama 只保留最前 4 个 token。** 重试轮会把上一版整份插件回灌（上限 24000 字符 ≈ 8000 token），加上系统提示词 1736 token，必然超窗。ollama 自己的日志原话：

  ```
  WARN "truncating input prompt"  limit=4098  prompt=12030  keep=4  new=4098
  ```

  **`keep=4`** —— 只保留最前面 4 个 token。而系统提示词里装的正是「必须先输出 yaml 块、再输出 python 块」这个格式契约和整张特征表。契约被整段砍掉，模型失去格式要求后瞎写 —— 于是 manifest 的 `name` 写成中文（**正是上一条记的那条报错**），然后一直写不完直到超时。所以真实情形是：**第 1 轮模型手滑（正常，重试本来就是干这个的），而重试被截断毁掉了 —— 重试这条路从来没通过。**
- **修法（三处，全本地，不需要云端 key）**：
  1. `docker-compose.yml` 给 `ollama` 服务加 `deploy.resources.reservations.devices` 显卡直通。Windows + Docker Desktop 走 WSL2，显卡是 **`/dev/dxg`** 这条路（**不是 `/dev/nvidia*`** —— 按后者查会误判成「直通不可用」，本次就先误判了一次）；宿主 `docker info` 的 Runtimes 里有 `nvidia` 才可用，没有的机器上这段要删掉，否则该服务起不来。
  2. `OLLAMA_CONTEXT_LENGTH: 8192`（官方默认 4096）。
  3. `plugin_runtime/forge.py`：`MAX_DRAFT_CHARS` 24000 → **10000**（由上下文预算倒推：8192 − 系统提示词 1736 − 输出预算 1500 − 报错余量），并且**截断要说出来** —— 默默切一刀的话模型会以为看到的是完整文件，照着断口往下写，越修越乱；这和被 ollama 偷偷砍头是同一类错。
- **连带修掉 ollama 健康检查从来没通过过**：容器里 `OLLAMA_HOST=0.0.0.0` 是给**服务端**监听用的，可 ollama CLI 把这个值当**目标**地址去连，连 0.0.0.0 直接失败 → 健康检查里的 `ollama list` 永远失败，容器长期挂在 `health: starting`，看着像坏了。app 只依赖 `service_started`（注释写明「LLM 优雅降级」），所以没被它挡住，但它会挡住以后任何想依赖 `service_healthy` 的服务。健康检查改成显式 `OLLAMA_HOST=http://127.0.0.1:11434`。
- **验证**：
  - `docker compose exec ollama ollama ps` → `100% GPU`，`CONTEXT 8192`。
  - **真实起草跑通**：**83.2 秒**（改前 406.6 秒且失败），**第 1 轮就通过**，落库 id=33 `long_stay_low_speed`、`status=draft`；静态校验 316 AST 节点、注册工具 `adjudicate`；内置三份样本全过，含两份「明确不该命中」都返回 0 条（不是恒真）。草稿**未启用**，等人工按按钮。
  - 重试轮提示词按**最坏情况**量（回灌 61000 字符密集代码、截到 10000）：重试 prompt 2890 token + 系统提示词 1736 + 输出预算 1500 = **6126 < 8192**，余量 2066，不再触发 `keep=4`；截断说明确实出现在正文里。
  - **未验到的**：重试路径只验了「提示词装得下」，没在真实模型上制造一次失败来走完整个重试（那需要模型恰好写歪，不可控）。
- **顺带一条部署事实**：宿主那台 Ollama（11434）与 compose 的 `ais-ollama`（`.env` 里映射到 **11435**，因为宿主占了 11434）是**两个独立实例，模型和配置都不共享**。排查「模型慢 / 认不到显卡」时别量错对象。

---

## 2026-10-10 — AI 造插件：提示词模板里一个没转义的 `{2,47}` 让起草**一次都没成功过**

用户报「插件没能生成出来。原因：起草过程出错：KeyError: '2,47'」，并指出那段「这通常是因为模型写的代码没过静态校验…可以换个说法再试一次」的解释不对。

- **根因：提示词模板自己就抛异常，起草从来没走到模型那一步。**
  `plugin_runtime/forge.py` 的 `_SYSTEM_PROMPT` 是拿去 `str.format()` 的模板，但正文里嵌着 YAML / Python 代码样例。其中 `name: my_plugin            # ^[a-z][a-z0-9_]{2,47}$` 这一行的 `{2,47}` 被 `.format()` 当成了字段名 → `KeyError: '2,47'`。所以「换个说法再试」永远不会有用——失败与用户怎么描述需求**毫无关系**。
- **同一个模板里还有 5 处同类未转义花括号**（`PLUGIN = {...}`、`DEFAULTS = {...}`、`ctx.config or {}`、硬性规则里的两处 `{...}`）。`{2,47}` 只是 `.format()` 撞上的**第一个**；只补这一处，下一个 `KeyError` 立刻接上。
- **修法：不再用 `.format()`。** 模板改成完全字面的正文（示例代码里的花括号原样留着给模型看），两个注入点改用哨兵串 `<<FACTS_SCHEMA>>` / `<<FACTS_DOC>>`，`system_prompt()` 用 `.replace()` 注入，并加**双向自检**：占位符既不能有剩余，注入内容也必须真的进了正文 —— 模板将来被改坏时直接抛错，而不是把一份残提示词喂给模型（哨兵若被写成变量名，replace 会静默不命中，这坑在本次实现里当场踩到过一次）。
- **连带修掉「失败原因说谎」**：`shipclaw/core.py` 原先对**所有**失败都套同一段「模型没写好、换个说法」的解释，**包括系统内部故障** —— 于是 `KeyError` 被包装成「你的需求讲得不够具体」，把用户支去反复改一个不相干的东西。现在 `forge_plugin` 的失败带 `kind`（`model` / `validation` / `duplicate` / `internal`），四种成因分别解释；`internal` 明确写「这是系统故障，**不是你的描述问题**」，并把完整堆栈 `traceback.print_exc()` 写进服务端日志（原先被裸露的 `except Exception` 吞掉，出事时无从查起）。
- **连带修掉两个会让起草静默失败或截断的配置问题**：`DRAFT_TIMEOUT_S` 120 → 240 秒（本地 7B 起草实测 ~116 秒，首次还要加载模型，120 秒正卡在临界点上）；新增 `DRAFT_MAX_TOKENS = 1500`（`llm_providers.complete()` 新增 `max_tokens` 覆盖参数）——「起草」用途绑定的 `num_predict` 是给对话用的 600，产出代码会**截断在半句上**，表现为「找不到 python 块」，看着像模型不听话，其实是预算不够。截断现在会被**点名**：代码块的 ``` 计数为奇数时直接报「输出被截断，请把『起草』绑到输出更长的模型」。
- **未改信任模型**：AI 造的插件仍是「草稿 → 人工点『启用』→ 全局生效」，`can_raise_risk` 恒为 false。用户 2026-10-10 复述确认：「做完要确保这个插件的可行性…需要人为按按钮打开才可以使用，然后是所有人都可以使用的」。
- **验证**：
  - 把模型调用 stub 掉后走完整 `forge_plugin`（生成→静态校验→试跑→落库）—— 落库 `status=draft / origin=ai / can_raise_risk=False`，静态校验通过（AST 355 节点、注册工具 `adjudicate`），内置三份样本（含「明确不该命中」那份）全过，随后清掉验证残留。
  - 失败文案四种成因逐一断言（`internal` 必须明说「不是你的描述问题」且**不再劝人重试**）**6 条全过**。
  - 本地 ollama 真实起草跑通到模型：单次产出 116 秒 / 485 token、两个代码块闭合；但**重试会超时**（实测 406 秒仍未完成：第 1 轮因 `name` 不是合法英文标识被判不合格，第 2 轮带上一版重写时超过 240 秒）。
- **没改的一件事，以及为什么**：~~本地 7B 在 CPU 上起草就是这个量级（实测出字约 4.2 token/秒）~~ **【本条已被上面的「起草一直失败的真正原因」推翻：4.2 token/秒 是容器没接显卡导致的，不是「本地就这样」——同一台机器上宿主 Ollama 有 29.3 token/秒。下面这段的结论「只能换云端模型」因此不成立。】**，且第 1 轮失败的原因是**纯机械问题**（manifest 的 `name` 写成了非 ASCII 标识符，与用户要判什么无关）。想让它真正好用，正路是到「开发者 → 提供商」把「起草」用途绑到云端模型（该页正是为此而建）—— 未擅自把 `name` 自动改名或把超时继续加大：前者要正则改写 YAML 且与正文 `PLUGIN` 字典的一致性无任何校验兜底，后者会把「3 轮 × 超时」的最坏等待推到二十几分钟，两者都该由用户拍板。

---

## 2026-10-10 — 数据分析面板「实时违规告警」改读规范码：20 行里 13 行的「其他违规」清零

起因是上一条做完后用户回看界面：「数据分析面板下的违规告警没有改啊，还是那些其它违规什么的」。**类型清单本身做全了，但这个面板没接上新口径**——它读的是另一列。

- **根因：库里有两列都叫「违规类型」，来历不同，而这一处读的是旧的那列。**
  - `violation_code` —— 回填时物化的规范码（`give_way_failure` / `ais_gap` …），**唯一可信的键**；
  - `violation_type` —— 旧链路的原始输出，可能是 `AIS002` / `AIS-GAP` / `'ais off'`，也可能是**空串**。
  其余接口（`/dashboard/violation-distribution`、`/violations/catalog`、`/violations` 列表）早已是「物化码优先、原始列兜底」，只有 `/api/v1/dashboard/alerts` 仍直接 `SELECT violation_type`。空串喂给 `describe()` 落到 `unknown`，标签就是**「其他违规」**。
- **实测（`violation_detected=1` 共 132 条）**：**90 条（68.2%）的 `violation_type` 是空的**（76 条 NULL + 14 条空串），而 `violation_code` / `violation_label` **每一行都有值**。剩下 42 条有原始值的行里多数与规范码**矛盾**（`give_way_failure` 的 82 行中有 10 行原始列写着 `ais_off` / `AIS003` / `data_quality_non_increasing_time`），所以即便原始列非空也会显示成**别的**类型。
- **界面实证（修前 → 修后，同一批 20 行）**：`其他违规` **13 → 0**、`AIS信号异常/关闭` **5 → 0**（这 5 行真身是 `give_way_failure`，被原始列的 `ais_off` 顶成了 AIS 码）；修后 = **让路船未让路 15 / AIS信号缺失 2 / 违规追越 2 / 超速限速违规 1**，与库里这 20 行的规范码逐行吻合。
- **修法：给「违规类型 → 中文标签」定一个全站唯一口径**，而不是到处各写一份。`app.py` 新增 `_VCODE_SQL` / `_VLABEL_SQL` / `_VRAW_SQL` 三个 SQL 片段与 `_vlabel(code, label, raw)`（**码 > 物化中文标签 > 原始列**，三者都认不出才落 `unknown`）。`describe()` 的键表本来就同时收着规范码、中文标签和历史 LLM 输出，所以三者都能喂给它。
- **同一根因的另外几处一并修掉**（都是「读原始列」这一个 bug 的不同出口）：
  - **AI 对话的数据上下文**（`_query_data_for_message`）：问「有哪些违规类型」时喂给 LLM 的清单原先按 `violation_type` 分组，会报出「其他违规(N起)」；现在按规范码分组、按标签合并（同一个码可能带着几种原始值各成一组，不合并会把「AIS信号缺失」拆成好几项），"最近违规"「指定 MMSI」两处也改读码/标签。
  - **区域分析**（`/api/v1/zone/analyze`）：原先函数内自带一个只认 `violation_type` 的局部 `_vlabel`，违规类型分布会整片是「其他违规」；现改用全站口径（局部同名函数已删除，避免遮蔽）。实测该接口 45 条违规船分布 = 让路船未让路 28 / 超速限速违规 5 / AIS信号缺失 5 / 逆向航行 3 / 碰撞风险 3 / 违规追越 1。
  - **级联轨迹的证据标签**（`/api/v1/cascade/tracks`）：`violation_label or describe(violation_type)` → 改走 `_vlabel(code, label, type)`（该查询原先连 `violation_code` 都没查）。
  - **证据完备度统计**（`/api/v1/violations/evidence-stats`）：`normalize_violation_code(violation_type)` → 优先 `violation_code`。
- **不写库、不改 `violation_type`**：那列是 LLM 当年**原始输出**的存档，`_NORM_RAW_SQL` 与 `normalize_violation_code()` 还要靠它兜住「未回填的老行」；把原始证据覆盖掉等于毁掉追溯链。只改读取侧。
- **运维**：`Dashboard.vue` 本来就只是渲染接口给的 `violationType` 字段，**前端不需要重建**（dist 未动）；只有 app 镜像重建（`docker compose build app && docker compose up -d app`）。
- **验证**：新增 `_diag/verify_dashboard_alerts.py`（Playwright，gitignore 下）——只断言**页面渲染出来的那张表**（不读接口 JSON，接口对而页面没渲染对同样是没修好）：表头 6 列、行数 20、类型列无「其他违规」/「未知违规」/空值/英文码泄漏、页面与接口逐行一致、风险等级已中文化。**9 条断言全过**。原主验证脚本 `_diag/verify_violations.py` 重跑**全部通过**，无回归。

---

## 2026-10-10 — 违规类型体系做全：16 个规范码 / 8 个一级类目 / 15 个实体码各三级细类

起因是上一轮把类型从 1 种做到 5 种后，口径被纠正：**判据不该是「数据里恰好有没有」，而该是「违规类型具体有哪些就都要做全」**——数据里有没有触发是另一回事。于是本轮把规范码清单一次性做齐，并把三个「半拉子」码补完、两个检测器判据重写。

- **目录补到 16 个规范码 / 8 个一级类目**（`violation_catalog.py`）：新增 `give_way_failure` 让路船未让路、`fairway_anchoring` 航道内锚泊、`illegal_towing` 违规拖带/并靠；新增一级类目 `anchoring` 锚泊管理，把 `anchoring_out_of_bounds` 从 `zone_control` 迁进去（锚泊类不再挤在区域管控里）。`CODE_SEVERITY` 依次插到既有序列的语义位置上（`fairway_anchoring` 88 紧邻 `anchoring_out_of_bounds` 90、`give_way_failure` 80 与追越/逆向同级——同属「已成立的规则违反」、`illegal_towing` 72）。
- **三个「半拉子」码补完**（这是清单审计查出来的，不是数据查出来的）：
  - `ais_off` **原先连检测器和枚举成员都没有**。新判据 = 同一条报文里 SOG / COG / HDG **三项同时**取 AIS 标准的不可用编码（102.3 / 360.0 / 511，`detect.py` 走 `_contiguous_runs` 聚合）。**必须三项同时**：这三个值单独出现都是合法的——实测 `heading==511` 占**全部报文的 15.30%**、涉及 **154/535 船**（Class B 设备普遍不上报航向），`course==360` 4 点、`speed==102.3` 7 点；拿单项当异常会把 15% 的正常报文判成违规。三项全不可用**实测只有 1 个点**，才是"这条报文没有任何导航信息"。与 `ais_gap` **互斥**（那个是"没收到报文"，这个是"收到了但内容不可用"），因此 `merge.py` 的 `COMPATIBLE_TYPES` 明确**不**把两者配成一对。
  - `illegal_fishing` **检测器接错了码**：`_detect_zone_events` 的 `fishing_allowed is False` 分支原先产 `LOW_SPEED_FISHING`（低速捕捞**嫌疑**，严重度 55），把法定违规（禁渔区+禁渔期内捕捞）报成了更轻的嫌疑码——**更重的码反而是死的**。改为产 `ILLEGAL_FISHING` 后，给 `LOW_SPEED_FISHING` 另立了**开阔水域**判据（事件前中位速度 ≥8 节、随后持续 ≤3 节 ≥5 分钟、且不在任何区域内），才是它「嫌疑」语义该有的样子。
  - `position_jump` 只补细类（`implied_speed_knots`，breaks `[150,300]`，检测线 `max_implied_speed_knots=80` 在它之下）。
- **`give_way_failure` 的判据是重写的，不是新写的**（新模块 `ais_accelerator/colreg.py`）。上一轮接线的判据是「让路船 + 预测 DCPA ≤0.5nm + CRI ≥0.4」，实测**535 船里 358 船（66.9%）、1622 起事件**，连最严的一格也还有 31.8%——不是阈值松，是判据错：评估窗被 `_split_windows` 截断（时长中位 29.6 分钟），窗内"最近行"通常不是真最近会遇点，预测 DCPA 中位 0.70nm 而**实测**最近距离中位 1.60nm、p90 4.97nm（九成被点名的事件从未进过 5 海里），CRI 又在门后饱和。新判据分两段：① 门 = **实测**最近会遇距离 ≤ `give_way_close_quarters_nm`（0.2nm ≈ 370m）——没真进紧迫局面就谈不上"未让路"；② 归责 = 起点判相遇态势（`classify_encounter`）、**仅 crossing**（`give_way_duty_encounters`，30° 是第16条的交叉尺度；实测 head_on/overtaking 三档分布 0/8/141，不该混进来）、且实测未改向（<`give_way_alteration_degrees`）未减速。CRI 退出判据、只做细类分档（breaks `[0.6,0.8]`，取接近起点的值）。修后 **114 起 / 89 船（16.6%）**，三档 75/34/5，且三路互验一致（离线普查复算、真实链路、合成场景）。0.2nm 比 `collision_risk` 的 0.5nm 门**更严**是对的：那个门用的是预测值（低估约 2.3×），实测 0.2 约当预测 0.09。
- **`fairway_anchoring` 走数据驱动航道**（新判据）：锚泊态复用既有的三个条件，把"在区域内"换成"落在 `water.py` 的 `corridor`（密度 ≥2 的格网，船队航迹自己走出来的航道走廊）内"，因此不受 `regulations.json` 示例区与实测海域零重叠的限制。`detect_candidates()` 为此新增 `water` 可选参数，`pipeline.py` 转发。
- **`illegal_towing` 违规拖带/并靠**（新模块 `ais_accelerator/towing.py`）：镜像 `encounter.py` 的结构（ShipIndex 查询 + `cKDTree.query_pairs` **双向试**——它只返回 `i<j`，单向会漏一半），判据为间距 ≤0.10nm、|Δspeed| ≤容差、两船均 ≥3kn、持续 ≥20 分钟。两个新多船码都进 `merge.py` 的 `PAIRWISE_TYPES`（`target_mmsi` 是"对谁"的唯一依据，不同目标船的事件不得合并）。
- **查出并修掉 5 个「算术上不可达」的细类档位**：四个码的第一档断点坐落在自己检测器的下限之下或**恰好等于**下限，于是这些档位在真实数据上永远空桶——分档写着三级、实际只有 1~2 级。`course_anomaly` 断点 `[30,90]` 而检测线下限是 >90（两档死）、`wrong_way_navigation` `[105,135]` 而 `counterflow_angle_degrees=112.5`、`illegal_towing` `[20,60]` 而 `towing_min_minutes=20`（零测集）、`anchoring_out_of_bounds` `[30,180]` 而 `anchoring_min_minutes=30`。逐个上移到下限之上（`[120,160]` / `[130,155]` / `[30,60]` / `[45,180]`）。**修它是零成本的**：`refresh_l1_violations.py` 本来就会**无条件重写**每一行的 `violation_subtype`/`violation_metric`/`violation_label`，趁回填一次做完，否则以后要再回填一次。
- **补一条可达到性断言**（`data/logs/verify_violation_types.py`）：逐码断言 `SUBTYPE_RULES[code]["breaks"][0] > 该码检测器下限`，下限**从 `CFG` 现取、不抄数字**，另列一份"定档字段无下限"的清单（`give_way_failure` 的 CRI 已取消门、`ais_off` 的计数下限即检测线、`restricted_area_entry` 的 1 个点），好让读者看到 15 个码**个个都被考虑过**。这正是上面那一类 bug 的探针。
- **`restricted_area_entry` 由 2 档补到 3 档**（breaks `[5]` → `[5,20]`），至此 **15 个实体码全部三级**（`unknown` 是兜底码，无细类）。
- **登记补全（9 处「不崩但会静默丢新类型」的点）**：`rules.py` 的事件→码映射与规则匹配、`merge.py` 的 `TYPE_PRIORITY`（缺项退化为优先级 0，合并时永远输）、`annotator.py` 的中文标签、`adapter.py` 给 Agent 的事件描述、`plugins/fishing_zone_judge` 的 `hit_types`。其中 `orchestrator.py`、`shipclaw/skills.py`、`shipclaw/evidence.py` 三处原先**手写类型元组**，现改为从 `violation_catalog.CANONICAL` 现取——手写的那版不会崩，但每加一个码都得记得回来补一笔，忘了就在场景文本/证据面板里被静默过滤掉。
- **法规锚点**：`REG_ANCHORS` 覆盖 16 个码、共 20 条锚点，全部解析到真实法条（0 未命中）。新增的三个码各自锚到实条：`give_way_failure → COLREG 第15条`（交叉相遇局面下他船在本船右舷时本船应让路；`FALLBACK_REG` 文本另补第16条"及早、大幅度"的行动要求）、`fairway_anchoring → 海上交通安全法(2021) 第47条 + 福州港港口章程 第23条`（不得在通航航道内锚泊妨碍他船通行 / 港内禁锚区）、`illegal_towing → 海上交通安全法(2021) 第45条 + 第47条`（拖带航行的特殊安全措施与报告义务）。`anchoring_out_of_bounds` 由单锚点补成 `福州港港口章程 第12条（应在公布锚地范围内锚泊）+ 第23条（港内禁锚区）`，第23条是纯增量：它是那份 44 条章程里唯一的锚泊**禁止性**条款。
  - 一处**判断口径**留个记号：`fairway_anchoring`（航道内锚泊，判据来自数据驱动走廊）锚的是港章第23条「港内禁锚区」——这是把港章的禁锚水域当作航道走廊的近似引用，条号本身是真的，但两者口径并不完全等同。
- **顺手查出的死代码（不改，记录在案）**：`ais_accelerator/regulation_db.py` 的 `search_by_violation` 在 **1283 行**（`return anchored[:top_k]`）就把锚定法条返回了，只要该码有任一锚点命中，后面的关键词/向量兜底整条走不到——所以按违规类型手写的**二级关键词标签是死代码**。而 `REG_ANCHORS` 覆盖了全部 16 个码、20 条锚点全部命中，于是上一轮给 `fairway_anchoring` 补的关键词分支同样是惰性的。留着无害（不误伤、也不改变检索结果），只有在改锚点时才需要回来看它。
- **实测结果**（`fzais1009`，535 船，真实链路 `build_fleet_context` → `annotate_track`）：
  - 加速器层共 **583 条事件**：`ais_gap` 242/167 船、`give_way_failure` **114/89**（高中低 75/34/5）、`collision_risk` 136/106、`overtaking_violation` 39/32、`speed_violation` 31/7、`illegal_towing` 14/10、`wrong_way_navigation` 6/6、`course_anomaly` 1/1；`ais_off`/`restricted_area_entry`/`anchoring_out_of_bounds`/`fairway_anchoring`/`illegal_fishing`/`low_speed_fishing`/`position_jump` 为 0（无对应数据，**有意为 0**）。
  - 落库回填（`scripts/refresh_l1_violations.py --user-id 0df2eb08`，529 行）：**非空类型 5 种 → 6 种**，分类码变化 85 处、其中 **L1 硬证据提升 74 行**（`give_way_failure` 71 + 强行追越 3）。变更前 `{(空):482, ais_gap:20, overtaking:9, collision:7, speed:6, wrong_way:5}` → 变更后 `{(空):408, give_way_failure:82, ais_gap:13, overtaking:8, collision:7, speed:6, wrong_way:5}`。库里 `give_way_failure` 细类分布 高58/中21/低3、`wrong_way_navigation` 轻度逆向3/逆向2——**新断点确实把原先空桶的档位填上了**。
  - 接口与界面：`/api/v1/violations/catalog` 出 **16 个类型 / 8 个类目**，15 个实体码各 3 档细类（`unknown` 0 档）；`/report` 类型下拉 **17 项**（16 + 「全部」，**全目录列出，不按命中数过滤**）；「锚泊管理」类目下正好 `anchoring_out_of_bounds` / `fairway_anchoring` 两个码；仪表盘类目饼图 **3 段**，与"有船舶数的类目"数一致（避碰安全类 89 / 航行行为类 19 / AIS设备类 13）；违规类型饼图 6 段、计数之和 = 违规总数 121。
  - **提升上限不误伤**：`PROMOTION_SANITY_LIMIT = 20` 是"单船硬证据事件数超限就不提升"的护栏，而新类型下最忙的一船只有 8 起（160 船里的最大值），远在门内——不会出现"类型做全了但被护栏挡在界面外"。
- **回归**：上一轮的断言全部仍通过——非零类型 ≥5 种（现 6 种）、类目筛选 == 该类目下各类型船舶数之和、`risk_level` 无 `middle`/`medium` 残留、饼图 >1 段。前端**无需改动**：下拉与饼图全部按 `catalog` 动态渲染。
- **运维记一笔**：回填与 app 启动**不能同时跑**。app 启动时会 `ALTER TABLE detection_results ADD COLUMN …`（AccessExclusiveLock），回填的长事务持着该表的锁不放（它是 idle in transaction 状态在跑几分钟的上下文构建），两者互相等待 → **PostgreSQL 判定死锁、回填整个回滚**。第一次真实回填就是这样失败的（`psycopg2.errors.DeadlockDetected`）。顺序必须是：先让 app 起完，再回填。

---

## 2026-10-10 — 违规类型从 1 种扩到多种：补齐申报书四类 + 分类细化到三级

- **起因**：界面「违规类型」只有一个值。查库实证——`detection_results` 617 行里 42 行带违规码，**100% 是 `ais_gap`（AIS信号缺失）**，其余 575 行违规字段全空。而申报书点名的是**超速、违规追越、越界锚泊、逆向航行**四类，代码里只有超速有检测器，且因 `regulations.json` 的示例区全在 38°N（实测数据在 24–27°N）**从未触发过**；另三类连 `CandidateType` 枚举成员都没有，「逆向航行」全链路不含这个词。
- **分类目录补齐到 13 类 26 细类**（`violation_catalog.py`）：`CANONICAL` 新增 `overtaking_violation` 违规追越 / `wrong_way_navigation` 逆向航行 / `anchoring_out_of_bounds` 越界锚泊，各带 `meaning`/`penalty` 与法规锚点（追越→COLREG 第13条、逆向→COLREG 第10条分道通航制、越界锚泊→《福州港港口章程》第12条）。`SUBTYPE_RULES` 各加三档细类：**轻微/明显/强行追越**、**偏离主流向/逆向航行/严重逆行**、**短暂锚泊违规/越界锚泊/长时间违规锚泊**。`_CODE_MAP` 补中英别名（追越未让路、逆行、越界停泊…）。
- **碰撞风险不再单独制造违规、也不再盖过具体规则违反**（`violation_catalog.py`）：① 从 `PROMOTABLE` 里摘掉——提升清单的用途是"LLM 说没问题时，硬证据仍能立住违规"，而碰撞风险的两项几何依据是 `TrajectoryPredictor` **预测**出来的，首次接线后实测被 CRI 判为有风险的船对里预测最近会遇距离中位数 4.19nm、p90 达 41nm（参与评估的船对本来就都在 10nm 内），拿它单独把干净的船变成违规不成立；事件照旧产出、照旧进证据链。② `CODE_SEVERITY` 由 85 降到 78（排在追越/逆向的 80 之后）——违规追越、逆向航行是可归责于本船的具体义务违反，碰撞风险是**风险状态**（可能双方都没违规），原来那个 85 是在碰撞检测从未运行过的前提下定的，与它实际产出什么无关。
- **分类唯一入口 `classify()` 改为按严重度取类，不再按时间取先**：原先扫 `track.events` 取**第一个**能归一的 `candidate_type` 就 `break`，而 `ais_gap` 几乎总排在前面，新增类型会被它静默吃掉——这是「加了却看不见」的真正原因。现按新增的 `CODE_SEVERITY` 序（禁航区100 > 越界锚泊90 > 追越80 = 逆向80 > 碰撞78 > 非法捕捞75 > 超速70 > …）扫完全部事件取最重的一类，其余类型仍留在 `events` 里供证据弹窗展开。
- **`_event_view()` 统一事件形态**：`classify` / `_subtype_of` / `deterministic_violation` 原先只认 `dict`，传 `CandidateEvent` 对象会在遍历中途抛 `'CandidateEvent' object has no attribute 'get'`。现在两种形态都吃得下。
- **违规追越检测器**（新 `ais_accelerator/encounter.py`）：镜像 `collision.py` 的结构，判定条件为同时间窗、距离 < `overtaking_search_radius_nm`、两船均 ≥3kn、X 落在 Y 船首 112.5°–247.5° 扇区、且 X 快于 Y 超 `overtaking_speed_diff_knots`，持续 ≥ `overtaking_min_minutes`。**必须双向试**（`cKDTree.query_pairs` 只返回 `i<j`，单向会漏掉一半）。证据带 `target_mmsi` / `relative_bearing` / `min_distance_nm` / `speed_diff_knots`。
- **逆向航行 + 数据驱动超速**（新 `ais_accelerator/traffic.py` + `detect.py` 两个新检测器）：`build_direction_field` 按 0.02° 格网用**圆平均**（`atan2(mean sin, mean cos)`）算主导航向，偏离 >90° 且持续 ≥5min 即逆向；超速走**绝对 + 同格网相对**双判据（`speed_abs_min_knots` 绝对下限 **或** `> 格网主流速 × speed_relative_ratio`），这是为了不依赖人工画区域——`regulations.json` 的示例区与实测数据零重叠，区域类判据在这批数据上永远不触发。绝对判据设 `speed_abs_max_knots` 上限是刻意的：102.3kn 是 AIS 速度字段饱和值，属数据异常，交给既有的 `max_implied_speed_knots` 口径。
- **多船接线修复（碰撞风险因此复活）**：`pipeline.py` 两处 `_detect_collisions(points, {})` 一直传空 dict，而 `collision.py` 有 `if not nearby_tracks: return []` 的提前返回——**多船规则从未运行过一次**。现在 `run.py` 侧一次遍历建好走廊/场/时空索引，逐船把「按本船时间范围过滤后的近邻船」传进 `annotate_track`（`collision.py` 签名不变，内存有界）。
- **接线一通就暴露出 CRI 的一处反向打分（已修）**：`_compute_dcpa_tcpa` 里 `tcpa = min_step * step_minutes`，而 `min_step` 从 0 起只增不减——**TCPA 永远不会是负数**。所以 `_compute_cri` 里 `elif tcpa > -5: f_tcpa = 0.9`（注释写的是"已经过了最近点但很近"）实际只在 `tcpa == 0` 时命中，而 TCPA 为 0 的语义恰恰相反：**预测窗内的最近点就是此刻，两船正在远离**。于是繁忙航道里最平常的"背向行驶"被打上近满分的紧迫度，光靠 `0.3×0.9 + 0.15×f_bearing + 0.15×f_speed ≈ 0.47` 就越过 0.4 的中风险线，与两船相距多远全无关系。实测 **535 船里 494 船（92%）**被判"碰撞风险"，根因在此。修法是 `tcpa <= 0` 时该项取 0，是否有危险交给 `f(DCPA)`（最近会遇距离）决定——这不改 CRI 的加权结构，只是把一条走错的分支摆正。修后单元核对：背向行驶相距 8nm → low、相距 0.2nm → medium、正在接近且 DCPA 0.3nm/8min → high。
- **`PipelineMetrics` 补两个字段**（`cache_hits` / `agent_calls` 之外新增 `collision_events` / `overtaking_events`）：`pipeline` 累加这两个计数，字段没声明会在 `run.py` 的裸调用处抛 `AttributeError` **整条分析流程崩掉**。
- **碰撞风险加"最近会遇距离"前置门限**（`config.py` 新增 `collision_dcpa_gate_nm = 0.5`，`collision.py` 的 `detect_collision_risks` 里把关）：CRI 是四项加权和，其中相对方位与相对速度合计权重 0.3，两项都偏"危险"时即使最近会遇距离那项几乎为 0，加权和也能越过 0.4 的中风险线——单靠 CRI，繁忙航道里"周围有船"就够判成碰撞风险。修掉 TCPA 反向打分后实测仍有 535 船里 494 船中招，所以"最近会遇得多近"必须单独把关，CRI 退回它该干的活（在门内分险）。门限取 0.5nm 是 COLREG 第7条"存在碰撞危险"的口径（约 926m），且实测该门限真在起作用——门内的船对预测最近会遇距离中位数 0.219nm、p90 0.452nm，切掉的是长尾。加门后的效果：`fzais1009` 上事件从 2383 起/494 船降到 **136 起/106 船**（联合扫描里 `cri>=0.4 & dcpa<=0.5` 那一格；`dcpa<=0.3` 是 86/68，`dcpa<=0.2` 是 65/54）。
- **修 `risk_level` 词表混用导致的假分段**（`orchestrator.py` + `app.py`）：该列全链路用 `{high, mid, low}`（`llm_layer/handler.py`、`risk_engine.py` 的口径），而 `"middle"` 是**事件优先级**（`models.py` 的 `EventPriority`）的词表——两者只是恰好同名。L1 提升分支把 `"middle"` 写进了 `risk_level`，`/api/v1/violations` 有一行 `== "middle"` 的补丁遮住了，但 `/api/v1/dashboard/violation-distribution` 没有，于是仪表盘风险饼图出现**同一档两个扇区**（实测 `middle=7` 与 `mid=2` 并存）。改为提升时写 `mid`，并抽出 `app.py` 的 `norm_risk()` 统一归一（同时兼容 LLM 会吐的 `"medium"`），原先散在两处的行内三元式收敛到这一个入口；分布接口在合并分组后补了 `ORDER BY`（SQL 的 `ORDER BY count DESC` 是按合并前的分组排的，归一合并后已失效）。
- **超速补进 L1 提升清单**（`violation_catalog.py` 的 `PROMOTABLE` 新增 `speed_violation: ("max_speed", 16.0)`）：门槛就是检测器自己的绝对异常线 `speed_abs_min_knots`。原先清单里没有它不是评估的结果——是速度检测器**从未触发过**（判据依赖 `regulations.json` 的区域限速，示例区全在 38°N 与实测海域零重叠），改成数据驱动判据后才第一次真的产出事件，这个问题才第一次被问到。判它算硬证据而碰撞风险不算，依据是同一条：这一项是 **AIS 实测速度**（位置差分），与追越的实测横距同类；碰撞风险的 DCPA/TCPA 是预测器推出来的。**不提升的后果是实测可见的**：6 条船各有 24 起超速事件（实测 16.1–20.2 节、持续 10–142 点）躺在 `detailed_result.events` 里，却因 LLM 当时判了"无违规"而 `violation_detected=0`，界面上一条看不到——申报书点名的四类里就它落了空。
- **实测结果**（`fzais1009` 529 行 + `fzais1001` 87 行，走 `scripts/refresh_l1_violations.py` 重算 L1 并回填）：
  - **违规类型 1 种 → 5 种**：`ais_gap` 42 → 27 条，另 `overtaking_violation` 违规追越 10、`wrong_way_navigation` 逆向航行 8、`collision_risk` 碰撞风险 7、`speed_violation` 超速 6；违规总数 42 → **58** 行，其中 16 行是 L1 提升新立的（原先违规字段全空：追越 4 + 逆向 5 + 超速 6，另有 7 行从 `ais_gap` 改判）。申报书四类里除越界锚泊（有意为 0）外**三类都已上界面**。
  - 加速器层（535 船，真实链路 `build_fleet_context` → `annotate_track`）：`collision_risk` 2383 起/494 船（加门限后 136 起/106 船）、`ais_gap` 242/167、`overtaking_violation` 41/34、`speed_violation` 31/7、`wrong_way_navigation` 6/6、`anchoring_out_of_bounds` 0（有意）。
  - 落库后 `SELECT violation_code, count(*) … GROUP BY 1` 得 **5 种**非空类型；`/api/v1/dashboard/violation-distribution` 的 `types` 由 **1 段变为 4 段**（按一级类目聚合：AIS设备类/AIS信号缺失 20、航行行为类 14、避碰安全类 7）。两份数据合起来 `detailed_result.events` 里实际写入：`ais_gap` 268 起/185 船、`collision_risk` 152/119、`overtaking_violation` 47/40、`speed_violation` 24/6、`wrong_way_navigation` 9/9。
  - 抽查一条违规追越记录（MMSI 413703060）：`target_mmsi=413208070`、`avg_relative_bearing=217.8°`（落在 COLREG 第13条"正横后 22.5°"的 112.5°–247.5° 扇区内）、`min_distance_nm=0.051`（约 95m，压在 0.10nm 安全线内）、`speed_diff_knots=1.72`、持续 2.82 分钟/17 点，坐标 26.10°N 119.68°E——证据链可独立复核。
- **L1 确定性提升**（`orchestrator.py`）：`violation_detected` 原先只由 LLM 置位，L1 实测到的追越/逆向永远到不了界面。现在 LLM 说"没问题"而 L1 有硬证据时置位，风险等级抬到 `mid`，并在 `final_assessment` 前置一行 `[确定性检测] …（未经 LLM 复核）`。提升不产生额外 LLM 成本——`analyze` 在上面已跑完。
- **法规侧**：`regulation_db.py` 把「追越」从 `collision_risk` 分支摘出（追越是第13条的让路义务，碰撞是第14/15/16条的会遇局面，混在一起会让"违规追越"检索到碰撞条款），新增 `分道/通航分道/船舶总流向/定线制/逆行 → wrong_way_navigation`、`禁止锚泊/擅自锚泊/锚地范围 → anchoring_out_of_bounds`。
- **存量记录回填**：新增 `scripts/refresh_l1_violations.py` —— 库里的 `detailed_result.events` 是**旧管道**写的（既无这三个检测器，多船规则又从未运行），所以直接跑既有的 `backfill_violation_taxonomy.py` 分不出新类型。本脚本把 L1 **重算一遍**（确定性、零 LLM 成本），再按 `classify()` 重物化分类列与 `events`；**LLM 的产出（`violation_type`/`confidence`/`recommended_action`/`llm_latency_seconds`）一律保留**。不重跑整条流程是因为时间全在第三层：实测 529 船 × 中位 27.8 秒 ≈ 4 小时串行，而 `--workers` 参数**解析了却从未使用**；重跑 4 小时换来的只是同一批分类列加一份重新生成的 LLM 结语。
- **越界锚泊本轮计数为 0（有意）**：`_detect_zone_events` 新增 `anchoring_allowed is False` 分支（与既有 `fishing_allowed` 同构，读法是"禁锚区内保持锚泊态"），但 `regulations.json` 里没有任何区域声明它，且 fzais1009 是纯运动轨迹集（99.997% 的点 ≥3kn，全库仅 13 点 <0.5kn，**无锚泊态**）。**不为它造数据**：它作为规范类型与三档细类存在，配置了禁锚区即可触发。
- **展示层无需改动**：`/report` 与仪表盘的筛选下拉、饼图全部走 `/api/v1/violations/catalog` 按 `ship_count > 0` 动态渲染，分类层改完自动生效。`app.py` 空库兜底假数据原先硬编码「航线偏离」「通信违规」——这两个名字**不在 `CANONICAL` 里**，改为从 `list_catalog()` 现取标签。
- **顺手清理**：删掉 `ViolationManagement.vue`（无路由、零引用的死代码，`README.md` 架构页列表同步为 `ReportDocuments`）；`docs/DESIGN.md` 的「CandidateType 7 类」订正为 10 类。**明确不动**：`frontend/frontend/js/main.js`（未被引用的旧静态原型）、`scripts/init_data.py`（往旧 sqlite 表插中文假数据的历史遗留，与当前 PostgreSQL 的 `detection_results` 不是同一张表）、`regulations.json` 的 38°N 示例区、`collision.py` 的 CRI 算法。

## 2026-10-10 — 提供商平铺呈现；下线自定义技能；插件草稿可直达

- **提供商不再藏在「添加」两步路径后面**：厂商目录（`llm_presets.py` 的 21 家，本地 / 国内云 / 聚合网关 / 海外四组）在启动时由 `llm_providers._seed_presets()` **全部**补进档案，`开发者 → 提供商` 一进去就按分组平铺列出全部厂商，用户就地填密钥，**「添加提供商」这个动作不存在了**。两条护栏：① 补进来的厂商一律 `enabled=false`，否则「按能力自动回落」会挑中一个没密钥的云厂商，表现为「一说话就报未配置密钥」；② 删掉的厂商把 preset 记进 `data/llm_dismissed_presets.json` 墓碑，否则下次启动又被补回来（删了又活）。另加 `_usable()` 兜底：*已启用但无密钥* 的厂商永不参与回落（显式绑定不受影响，仍按用户钉的那条走）。
- **下线自定义技能（skill.md）整条线**：删 `DevSkills.vue`、`dev.js` 里 8 个 skill 接口、`app.py` 的 7 条 `/dev/skills*` 路由与 `custom_skills` 建表、`skills.py` 的 `CUSTOM_SKILL_TEMPLATE` / `validate_custom_skill_md` / `CustomSkill` / `load_user_custom_skills`，以及 `core.py` 的加载调用。老库里那张表和既有数据**不做 DROP**，只不再新建。`/api/v1/ai/tools` 现为「内置技能 + 已启用插件」。
- **造插件仍要人工启用，但入口做顺**：悬浮对话识别到造插件意图后生成的草稿不自动上线（超管复核后手动启用），回复文案改为点下面的按钮直达；`查看草稿` 跳 `/dev?tab=plugins&plugin=<id>`，插件页把该行高亮 + 滚动到视野中央，并挂一条说明「草稿还没生效，点本行『启用』」。

## 2026-08-22 — 证据链补强：AIS 中断类事件补中断上下文

- **文本证据链**：`ViolationEvidenceSkill._build_gap_context` 对 AIS 中断类事件（`features.gap_minutes` 存在）补全证据——证据点通常只存"中断恢复后首点"无法说明违规，现用该点时间戳在原始轨迹中定位，取前一点作为**中断前最后信号点**，配合中断时长构成完整链条：`AIS 信号中断 481 分钟（约 8.0 小时）` + `中断前：13:29 最后正常信号 …` + `恢复后：21:30 …`，直接回答"为什么这个点就违规"。
- **结构化数据**：`_build_evidence_data` 事件对象新增 `gap_minutes` / `pre_point` / `post_point`，供前端绘图。
- **前端 `EvidenceCard.vue`**：① 轨迹图新增图例说明行（轨迹全程首→尾 / 起点 / 终点 / 违规事件点 / 中断前最后信号 / 信号中断期），明确"起/终"是全程轨迹首尾而非违规点；② 黄色 pin「前」标记中断前最后信号点、红色虚线 + 箭头绘制"信号中断期（无回传）"；③ 坐标轴防重影：x/y 轴**各自独立**的小数位（按本轴跨度 2/3/4 位自适应）+ `hideOverlap: true` + 刻度降到 3 个 + x 轴标签旋转 20°。

## 2026-08-22 — 证据链展示修复（单证据点"区间 A→A" + 轨迹图重影）

- **文本证据链**：`ViolationEvidenceSkill._build_evidence` 单证据点事件（首尾同点）由误导性的「区间 A→A」改为「位置：…」，`_CODE_MAP` 归一化后仅 1 个证据点的事件同样适用（`_fmt_latlon` 有值才显示，否则隐藏该行）。
- **轨迹图重影**：`EvidenceCard.vue` 轨迹散点抽稀到最多 60 点（`MAX_DOTS=60`，相邻近重合点不再叠成一片）；坐标轴刻度小数位按数据范围自适应（跨度 >0.1° 用 3 位、否则 4 位，长标签不再互相遮挡）；x/y 轴 `splitNumber` 5/4 减少刻度重叠；起终点改 pin 标记（"起"/"终"），tooltip 支持 `_mk` 标记。

## 2026-08-22 — 违规证据链结构化展示（判定卡片 + 轨迹图 + 事件表格）

- **后端 `ViolationEvidenceSkill` 结构化输出**：`execute()` 找到记录时返回 `{"text","data"}`——`text` 保留四段纯文本证据链（兜底），`data` 为结构化 JSON（判定 / 轨迹点 / 事件 / 法规 / 复核）。新增 `_build_evidence_data()` 组装，轨迹点采样 ≤300；`quality_level` 用表列（`quality_report.level` 是 @property 取不到）。
- **`core.py` 拆包透传**：新增 `_split_result()`，`chat()` 统一执行技能后拆出 `structured` 字段；确定性技能返回 dict 不破坏 `tool_calls.summary` 与等级锁定分支。
- **`/api/v1/ai/chat` 透传 `structured`**：前端直接拿到结构化数据。
- **前端新增 `EvidenceCard.vue`**：白底自包含卡片，含 ① 判定卡片（违规标签/风险/置信度/AIS质量/L1打标）② ECharts 轨迹图（蓝线轨迹 + 红涟漪事件点，hover 显示时间/航速/航向）③ 违规事件表格 ④ 法规引用/LLM复核/建议处置；`FloatingAIChat.vue` 与 `AIChat.vue` 均 `v-if="m.structured"` 条件渲染，无结构化数据回退纯文本；两处 quickQuestions 加"413570530 的违规证据链"示例。

## 2026-08-22 — 论文研究方案 + 全量文档代码对齐

- **新增 `论文/方案A_三级级联论文方案.md`**：论文方向 A「面向海事监管的 LLM 三级异构级联：成本-质量帕累托与可追溯证据链」研究方案（GT 构建 / 6 基线 / 5 核心实验 / 统计方法 / 16 周实施计划）
- **全量文档按代码现状对齐**：
  - `docs/API.md` 重写为 v2.1：实际约 62 个端点分组（新增 dev / claw / user / admin / orgs / data）；AI 对话标注为已接入真实 ShipClaw（不再"测试中"）；标明文书上报（disposal）无后端端点、仪表盘空数据时返回硬编码演示值
  - `docs/DESIGN.md` 标记 Phase 1–6 完成状态、三分类→四分类、特征 22→28 维、相关文件索引去"待创建"
  - `docs/数据库设计方案.md` 修正 PG 默认值（`postgres` / `wmlwxhn520`，统一到 config.py）、特征向量维度说明、运行时 DDL 表补充、disposal 无写入说明
  - `docs/实验方案.md` 顶部加现状对齐：实际命令为 `experiments/run.py` 与 `experiments/feedback_experiment/`，附录对齐代码结构
  - `docs/智能体记忆系统设计.md` 标注三类记忆已实现、PatternMemory 不再用于检测管道、Milvus Lite 表述修正、特征 22→28 维
  - `README.md` 特征 22→28 维、API 分组与端点数更新、实验章节对齐、新增论文文档导航
- **新增 `shipclaw` 内置技能 `violation_evidence`（违规证据链，Lv4）**：按 MMSI 查 `detection_results`，从 `detailed_result` / `violation_event` / `regulation_ref` / `final_assessment` 组装 ① 轨迹分析 ② 规则匹配 ③ 法规引用 ④ LLM 复核 四段证据链，回答"这艘船怎么违规、对应法规第几条"；内置 Python 代码技能（查库组装），非纯 LLM 的 skill.md；配套操作手册 `shipclaw/skills/violation_evidence.md`；匹配优先级已避让 ShipQuery（0.85）/ RegulationQA（0.85）/ ViolationList（0.92）——内置技能 8→9
- **三处修复（用户实测"413570530的违规证据链给我"只回法规科普、无证据链）**：
  - **确定性技能机制**：`core.py._execute_skill` 对 `deterministic=True` 的技能直接返回查询结果、跳过 Ollama 人格渲染——避免 LLM 把"查无记录/证据链"这类确定性结果加戏编造成法规科普（`violation_evidence` 即确定性技能）
  - **系统数据对登录用户可见**：`pg_db.user_scope` 与 `app.py._user_scope` 统一为 `(user_id = ? OR user_id = 'system')`——CLI（`run.py` 以 `user_id='system'` 写入）扫描出的船此前对 Web 登录用户不可见，导致查无记录
  - **MMSI 正则修复**：`\b(\d{6,9})\b` → `(?<!\d)(\d{6,9})(?!\d)`——Python3 的 `\w` 含中文，数字紧贴中文（如"413570530的违规"）时 `\b` 边界失效提取不到 MMSI（`ship_query` 同修）

## 2026-08-21 — 开发者自定义技能（提交 skill.md 智能体直接可用）

- **开发者页新增「自定义技能」页签**：用户按模板编写 skill.md（YAML 元信息 + 操作手册正文），在线提交后智能体「小慧」在对话中命中触发词即自动使用该技能，按用户手册操作
- 新增 `custom_skills` 表（按登录用户隔离）；`/api/v1/dev/skills` CRUD + `template` + `validate` 端点（校验：name 小写英文不冲突内置、label/description 必填、min_level 1~10、触发词非空、正文非空）
- `shipclaw/skills.py` 新增 `CustomSkill`（LLM 驱动、安全不执行代码）、`validate_custom_skill_md()`、`load_user_custom_skills()`；`match_best_skill()` 支持额外技能，**平局时自定义技能优先**
- `ShipClaw.chat()` 加载用户自定义技能参与路由，按自定义 min_level 判定解锁；自定义技能手册注入 Ollama 提示；`/api/v1/ai/tools` 合并展示自定义技能
- 前端 `DevSkills.vue`：技能列表（生效/停用徽标、触发词、解锁等级）+ 编辑器（模板/校验/新建/编辑/删除/启停）

## 2026-08-21 — 智能体技能操作手册（skill.md）

- 新增 `shipclaw/skills/*.md`：为全部 8 个技能各写一份操作手册（触发条件 / 操作步骤 / 输出规范 / 禁止事项）
- `shipclaw/skills.py` 新增 `load_skill_manual()`（剥掉 YAML frontmatter）；`ShipClaw` 执行技能时把对应手册注入 Ollama 系统提示，LLM **严格按手册操作**，解决答非所问（例如闲聊不再编造船舶数据、违规记录用中文标签、法规不捏造条款号）
- 修复路由平局：`trend_analysis` 关键词分 0.85→0.9，避免「违规趋势分析」同时命中法规「违规」时误路由到法规问答

## 2026-08-21 — Milvus 连接自愈加固 + 时区修正 + 智能体数据工具

- **Milvus 容器重启后连接静默失效**：`Cannot invoke RPC on closed channel!`（elapsed 0.3ms）——容器重启导致一次性 gRPC channel 失效，法规/轨迹检索静默返回空
  - `ais_accelerator/regulation_db.py`：新增 `_call()` 自愈包装，命中连接错误标记时重建连接并重试一次（幂等读操作全部走它）
  - `triage_engine/milvus_client.py`：补齐 ORM `default` 连接（此前轨迹检索一直 `ConnectionNotExistException`、集合为空），新增 `_coll()` 自愈包装；`_connect_server` 的 host/port fallback 块保持原位
- **容器时区修正**：app 容器默认 UTC，`cascade_logs` / `detection_results` 时间比本地晚 8 小时——compose `app` 服务加 `TZ=Asia/Shanghai`；`cascade_agent` 日志时间戳加毫秒精度（同秒多行不再混淆）
- **智能体数据查询工具**：`shipclaw/skills.py` 新增 3 个技能——`ship_list`（我的船舶列表，Lv2）、`user_overview`（我的数据概况，Lv3）、`violation_records`（我的违规记录，Lv4），全部按登录用户 `user_scope` 隔离查询 `detection_results`；`ship_query` 匹配词增加「查询/查一下」以命中「查询闽海之星」这类船名问题
  - 技能补齐 `label/description/icon/params` 元数据，`get_tool_catalog()` 暴露 8 个工具
  - `ShipClaw.chat()` 返回 `tool_calls`（工具名/图标/说明/状态），等级锁定也展示「想调用的工具 + 🔒 未解锁」
  - 后端新增 `GET /api/v1/ai/tools`；前端 AI 对话（悬浮窗 + 全页）渲染工具调用卡片、新增「🔧 工具」Tab
- **本地部署踩坑**：`docker-compose.local.yml` 改用 `ports: !reset []` 清掉 ollama 端口映射（否则与宿主原生 Ollama 抢 11434）；pg 镜像切回 pg16（pg_data 卷为 pg16 初始化，pg17 版本不兼容崩循环）

## 2026-08-13 — Web 管理端迭代

- `frontend/frontend/backend/app.py` 接口重构扩容（当时约 40 个 `/api/v1` 接口；至 2026-08-22 已增至约 62 个端点）
- 前端新增/迭代页面：仪表盘、船舶监控、违规管理、文书上报、数据上传、级联日志、AI 对话
- 根目录新增 `README.md` 与文档导航（本组文档）

## 2026-08-12 — ShipClaw 可养成智能体「小慧」

- 新增 `shipclaw/` 模块：ShipClaw 主类 + 四维性格系统 + 技能系统 + 养成记忆
- 等级体系 Lv1~10，交互产生经验与性格成长
- 智能体记忆重构（`claw_memory.py`）

## 2026-08-06 — 三级级联全链路打通

- `run.py` 重构为**全系统唯一数据处理管道**，Web / CLI / CascadeAgent 统一入口
- `cascade_agent.py`：直接调用 `run.py` 的 `main()`，终端输出实时写入 `cascade_logs` 表
- 修复 **Ollama 可用性竞态**：Windows 上 Ollama 秒级启动导致「已启动却报不可用」，引入 HTTP 连通性探测 + 轮询重试（详见 [docs/QUICKSTART.md](docs/QUICKSTART.md#q1)）
- `config.py` 统一配置中心（Milvus / PostgreSQL 环境变量覆盖）
- `triage_engine/` 全面完善：router / model / train / milvus_client / pgvector_client / schema.sql
- `llm_layer/` 完善四级 Prompt 模板与反馈闭环

## 2026-08-04 — 法规向量数据库与统一入口

- 新增 `ais_accelerator/regulation_db.py`：法规条款向量化，LLM 判定注入具体条款，输出 `regulation_ref`
- 新增 `cascade_agent.py`（CascadeAgent 雏形）
- `regulations.json` 迁移：违规类型定义迁入 RegulationDB，本文件仅保留区域与船型

## 2026-08-01 — PostgreSQL + pgvector 集成

- 新增 `database/schema.sql`：完整建表（ships / raw_ais / cases / tracks / training_set / ml_feedback / violations / disposal_docs / regulations）
- `triage_engine/pgvector_client.py`：案例写入、相似检索、ML/LLM 结果回填
- 智能体记忆系统上线：`memory/`（session / ship / pattern）

## 2026-07-20 — ais_accelerator 包建立

- `ais_accelerator/` 包初始化（facade / pipeline / quality / segment / detect / merge / compress / rules / models）
- `triage_engine/` 与 `llm_layer/` 骨架建立

## 2026-07-16 — 架构设计定稿

- `docs/DESIGN.md`：三层级联架构设计（AIS 加速器 → ML Triage → LLM 判定）+ 消融实验方案
- 确立「召回优先 + 可追溯 + 优雅降级」设计原则

---

## 🎯 规划方向（Roadmap）

- [ ] 语义化版本管理与自动发布
- [ ] ML vs LLM 一致率监控仪表盘
- [ ] 人工复核工作流
- [ ] 多 LLM 模型对比评估
