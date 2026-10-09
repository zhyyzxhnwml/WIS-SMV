"""
llm_presets.py — 开箱即用的模型提供商预设目录
================================================

【这个文件做什么？】
  「添加提供商」时不该让人手工去查每家厂商的 base_url。这里放一份**纯数据**目录：
  一键选中某个厂商 → 名称/类型/服务地址/常见模型名都填好，用户只需要粘一个 API 密钥。

【为什么是纯数据 + 一个展开函数？】
  接入一家云厂商的全部差异就是「地址 + 协议形状」。协议形状只有两种：
    ollama（/api/generate）
    openai_compat（/v1/chat/completions）
  所以预设不需要插件、不需要适配器代码，只是一张表（同 `llm_providers.KINDS` 的取向）。
  `apply_preset()` 负责把 {preset: 'deepseek', api_key: '...'} 展开成
  `llm_providers.upsert_provider()` 能直接吃的完整载荷。

【收录标准（重要）】
  只收**真的说 OpenAI 兼容协议**的厂商。像 MiniMax 的 text/chatcompletion_v2 那种
  「自有路径、只是长得像」的端点一律不收 —— 收了就是一个必然「测试连接失败」的按钮，
  比没有这个按钮更糟。表达不了就退回「自定义 OpenAI 兼容」，手填地址。

【模型名会漂】
  这里的 models 只是「填个能用的大概」，厂商改模型名比改地址勤得多。
  界面上「拉取模型」的结果才是权威 —— 它打的是 /v1/models 或 /api/tags 的真实响应。
  所以不要把这里的列表当成事实来源，它只是让表单不为空。

【base_url 与 _norm_openai_base 的关系】
  地址按官方文档原样填写，**允许不以 /v1 结尾**（智谱是 /api/paas/v4、千帆是 /v2、
  火山方舟是 /api/v3、Gemini 是 /v1beta/openai）。llm_providers._norm_openai_base()
  只对「没有路径的根地址」补 /v1，自带版本段的原样使用 —— 两侧是一套口径，改一边要改另一边。
"""

from __future__ import annotations

# ── 分组（仅用于界面分区展示） ──
PRESET_GROUPS: list[dict] = [
    {"key": "local",   "label": "本地部署"},
    {"key": "cn",      "label": "国内云厂商"},
    {"key": "gateway", "label": "聚合网关 / 自建"},
    {"key": "global",  "label": "海外厂商"},
]

# ── 目录 ──
# kind: ollama | openai_compat（与 llm_providers.KINDS 同一套取值）
# doc_url: 去哪申请密钥（界面渲染成链接）
# note: 这一家特有的坑（地址在容器里的写法、模型名格式等）
PRESETS: list[dict] = [
    # ══ 本地部署 ══
    {
        "id": "ollama", "group": "local", "name": "本地 Ollama", "kind": "ollama",
        "base_url": "http://localhost:11434",
        "models": ["qwen2.5:7b-instruct-q4_K_M"],
        "doc_url": "",
        "note": "不需要密钥。本系统在容器里跑时地址要写 http://ollama:11434（compose 服务名），"
                "写 localhost 会指向应用容器自己。",
    },
    {
        "id": "custom", "group": "local", "name": "自定义 / 其他 OpenAI 兼容", "kind": "openai_compat",
        "base_url": "",
        "models": [],
        "doc_url": "",
        "note": "vLLM、LM Studio、one-api、内网网关都走这个。地址由你填：根地址会自动补 /v1，"
                "自带版本段的按原样用。",
    },

    # ══ 国内云厂商 ══
    {
        "id": "deepseek", "group": "cn", "name": "DeepSeek 深度求索", "kind": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "doc_url": "https://platform.deepseek.com/api_keys",
        "note": "",
    },
    {
        "id": "moonshot", "group": "cn", "name": "月之暗面 Kimi", "kind": "openai_compat",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "doc_url": "https://platform.moonshot.cn/console/api-keys",
        "note": "长上下文是它的强项，128k 那档适合整篇法规喂进去。",
    },
    {
        "id": "zhipu", "group": "cn", "name": "智谱 GLM", "kind": "openai_compat",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"],
        "doc_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "note": "地址自带 /api/paas/v4 版本段，不要手动补 /v1。",
    },
    {
        "id": "dashscope", "group": "cn", "name": "通义千问（阿里云百炼）", "kind": "openai_compat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"],
        "doc_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "note": "必须走 compatible-mode 这条路径；百炼控制台的密钥与 DashScope 通用。",
    },
    {
        "id": "siliconflow", "group": "cn", "name": "硅基流动 SiliconFlow", "kind": "openai_compat",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct"],
        "doc_url": "https://cloud.siliconflow.cn/account/ak",
        "note": "模型名带「厂商/模型」前缀，直接从「拉取模型」里挑最省事。",
    },
    {
        "id": "ark", "group": "cn", "name": "火山方舟（豆包）", "kind": "openai_compat",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-pro-32k", "doubao-lite-32k"],
        "doc_url": "https://console.volcengine.com/ark",
        "note": "地址自带 /api/v3，不要补 /v1。模型名可以用官方名，也可以填你创建的推理接入点 ID（ep-…）。",
    },
    {
        "id": "hunyuan", "group": "cn", "name": "腾讯混元", "kind": "openai_compat",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models": ["hunyuan-turbos-latest", "hunyuan-turbo", "hunyuan-lite"],
        "doc_url": "https://console.cloud.tencent.com/hunyuan/api-key",
        "note": "密钥要在控制台「为兼容接口创建」的那个 API Key，别填腾讯云的 SecretId/SecretKey。",
    },
    {
        "id": "qianfan", "group": "cn", "name": "百度千帆（文心）", "kind": "openai_compat",
        "base_url": "https://qianfan.baidubce.com/v2",
        "models": ["ernie-4.5-turbo-128k", "ernie-speed-128k"],
        "doc_url": "https://console.bce.baidu.com/iam/#/iam/apikey/list",
        "note": "地址是 /v2 不是 /v1。密钥形如 bce-v3/ALTAK-…",
    },
    {
        "id": "spark", "group": "cn", "name": "讯飞星火", "kind": "openai_compat",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "models": ["generalv3.5", "4.0Ultra", "lite"],
        "doc_url": "https://console.xfyun.cn/services/cbm",
        "note": "用 HTTP 端点（spark-api-open）配 APIPassword；WebSocket 那条路是 HMAC 签名，走不通。",
    },
    {
        "id": "stepfun", "group": "cn", "name": "阶跃星辰 StepFun", "kind": "openai_compat",
        "base_url": "https://api.stepfun.com/v1",
        "models": ["step-3.5-flash"],
        "doc_url": "https://platform.stepfun.com/interface-key",
        "note": "国际区是 api.stepfun.ai，密钥不通用。",
    },
    {
        "id": "yi", "group": "cn", "name": "零一万物 01.AI", "kind": "openai_compat",
        "base_url": "https://api.lingyiwanwu.com/v1",
        "models": ["yi-lightning", "yi-large"],
        "doc_url": "https://platform.lingyiwanwu.com/apikeys",
        "note": "",
    },

    # ══ 聚合网关 / 自建 ══
    {
        "id": "openrouter", "group": "gateway", "name": "OpenRouter", "kind": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["deepseek/deepseek-chat", "openai/gpt-4o-mini", "anthropic/claude-sonnet-4"],
        "doc_url": "https://openrouter.ai/keys",
        "note": "一个密钥代理几十家模型，模型名是「厂商/模型」。",
    },
    {
        "id": "oneapi", "group": "gateway", "name": "自建 one-api / new-api", "kind": "openai_compat",
        "base_url": "",
        "models": [],
        "doc_url": "",
        "note": "内网网关填自己的地址（如 http://192.168.1.10:3000）。一个网关后面可以挂多家厂商，"
                "监管内网部署常用这个。",
    },

    # ══ 海外厂商 ══
    {
        "id": "openai", "group": "global", "name": "OpenAI", "kind": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "doc_url": "https://platform.openai.com/api-keys",
        "note": "国内直连需要代理；容器内出网要确认代理配置（见部署说明）。",
    },
    {
        "id": "gemini", "group": "global", "name": "Google Gemini", "kind": "openai_compat",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.0-flash", "gemini-1.5-pro"],
        "doc_url": "https://aistudio.google.com/apikey",
        "note": "走 Google 的 OpenAI 兼容端点（/v1beta/openai），不是原生 Gemini 协议。",
    },
    {
        "id": "xai", "group": "global", "name": "xAI Grok", "kind": "openai_compat",
        "base_url": "https://api.x.ai/v1",
        "models": ["grok-3", "grok-3-mini"],
        "doc_url": "https://console.x.ai/",
        "note": "",
    },
    {
        "id": "mistral", "group": "global", "name": "Mistral AI", "kind": "openai_compat",
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-large-latest", "mistral-small-latest"],
        "doc_url": "https://console.mistral.ai/api-keys/",
        "note": "",
    },
    {
        "id": "groq", "group": "global", "name": "Groq", "kind": "openai_compat",
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile"],
        "doc_url": "https://console.groq.com/keys",
        "note": "主打推理速度，适合高并发的批量判定。",
    },
    {
        "id": "together", "group": "global", "name": "Together AI", "kind": "openai_compat",
        "base_url": "https://api.together.xyz/v1",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        "doc_url": "https://api.together.ai/settings/api-keys",
        "note": "开源权重模型为主，模型名带「厂商/模型」。",
    },
]

_PRESET_BY_ID: dict[str, dict] = {p["id"]: p for p in PRESETS}


def list_presets() -> list[dict]:
    """全部预设（拷贝，调用方改不脏目录）。"""
    return [dict(p) for p in PRESETS]


def get_preset(preset_id: str) -> dict | None:
    p = _PRESET_BY_ID.get((preset_id or "").strip())
    return dict(p) if p else None


def list_groups() -> list[dict]:
    """分组 + 每组的预设，界面直接渲染。"""
    out = []
    for g in PRESET_GROUPS:
        items = [dict(p) for p in PRESETS if p["group"] == g["key"]]
        if items:
            out.append({"key": g["key"], "label": g["label"], "presets": items})
    return out


# 用户显式给了值就绝不被预设覆盖的字段
_FILLABLE = ("name", "kind", "base_url", "models", "capabilities")


def apply_preset(data: dict) -> tuple[dict | None, str]:
    """把带 `preset` 的入参展开成完整的提供商载荷。返回 (载荷, 错误信息)。

      {"preset": "deepseek", "api_key": "sk-…"}          → 名称/类型/地址/模型全填好
      {"preset": "deepseek", "base_url": "http://内网代理"} → 地址用你给的，其余取预设

    **只填空缺**：用户写了的字段一律不动 —— 预设是「帮你把表单填满」，不是「覆盖你的输入」。
    返回的载荷里不再有 `preset` 键（它不是档案字段，留着会一路带到落盘结构里）。
    """
    if not isinstance(data, dict):
        return None, "载荷必须是对象"
    payload = {k: v for k, v in data.items() if k != "preset"}
    preset_id = str(data.get("preset") or "").strip()
    if not preset_id:
        return payload, ""
    preset = get_preset(preset_id)
    if preset is None:
        return None, f"未知的预设提供商：{preset_id}"
    for key in _FILLABLE:
        cur = payload.get(key)
        if cur in (None, "", [], {}):
            if preset.get(key) not in (None, "", [], {}):
                payload[key] = preset[key]
    return payload, ""
