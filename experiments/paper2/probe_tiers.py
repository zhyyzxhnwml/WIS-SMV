# -*- coding: utf-8 -*-
"""临时探针：比较两档模型在同一提示下的输出形态与成本。

要回答的问题：
  1. qwen3:8b 是否会输出思维链（think 块）？它会不会污染 JSON 解析？
  2. `/api/chat` 的 `think` 参数是否被接受？
  3. 同一提示下两档模型的 token 成本差多少？

跑完即可删，不进论文产物。
"""
from __future__ import annotations

import json
import time
import urllib.request

OLLAMA = "http://localhost:11434"

PROMPT = (
    '只输出一个 JSON 对象，不要任何解释文字。判断这句话描述的船是否违规：'
    '"本船为让路船，全程保向保速未采取避让动作，与来船最近距离 0.2 海里"。'
    '字段：{"violation_detected": true|false, "violation_type": "give_way_failure"|null, '
    '"confidence": 0~1, "reason": "一句话"}'
)


def chat(model: str, prompt: str, think: bool | None = None, num_predict: int = 320) -> tuple[dict, float]:
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0, "seed": 0, "num_predict": num_predict,
                    "num_ctx": 8192},
    }
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(f"{OLLAMA}/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        obj = json.loads(resp.read().decode("utf-8"))
    return obj, time.perf_counter() - t0


def main() -> None:
    for model, thinks in [("qwen2.5:7b-instruct-q4_K_M", [None]),
                          ("qwen3:8b", [None, False])]:
        for th in thinks:
            tag = f"{model} think={th}"
            try:
                obj, dt = chat(model, PROMPT, th)
            except Exception as exc:                                   # noqa: BLE001
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:200]          # type: ignore[attr-defined]
                except Exception:                                      # noqa: BLE001
                    pass
                print(f"--- {tag}\n    ERROR {type(exc).__name__}: {exc}\n    {detail}")
                continue
            content = (obj.get("message") or {}).get("content", "") or ""
            print(f"--- {tag}")
            print(f"    latency={dt:6.1f}s  prompt_tok={obj.get('prompt_eval_count')}"
                  f"  completion_tok={obj.get('eval_count')}"
                  f"  thinking_tok={obj.get('thinking') and len(obj['thinking'])}")
            print(f"    content={content!r}"[:400])


if __name__ == "__main__":
    main()
