"""大模型接口封装（OpenAI 兼容协议）。

DeepSeek / 智谱 GLM / 通义千问 / Moonshot 都兼容这套写法，换 base_url 与 model 即可，
所以这里不绑定任何一家厂商 —— 这是"接口调用"层面最省事的工程做法。

免费额度（如 GLM-4-Flash）有限速，触发 429 时接口直接报错而不排队，
所以这里加了指数退避重试；同时对 401/402/404 给出人话解释，方便定位。
"""
from __future__ import annotations

import re
import time

from openai import APIStatusError, APITimeoutError, OpenAI

import config

_client: OpenAI | None = None

MAX_RETRY = 3                       # 最多尝试 3 次（首次 + 2 次重试）
RETRY_STATUS = {429, 500, 502, 503, 504}


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)
    return _client


def _explain(status: int | None, raw: str) -> str:
    """把 HTTP 状态码翻译成能直接照着做的提示。"""
    hints = {
        401: "API Key 无效或已过期 —— 检查 .env 里的 LLM_API_KEY 是否复制完整（智谱的 Key 形如 xxxx.yyyy 两段）",
        402: "账户余额不足 —— DeepSeek 需先充值；想零成本请把 LLM_BASE_URL 换成智谱并改用 glm-4-flash",
        403: "没有该模型的调用权限 —— 确认该模型已开通",
        404: "模型名或接口地址不对 —— 检查 LLM_MODEL 与 LLM_BASE_URL（智谱是 .../api/paas/v4）",
        429: "触发限流（免费额度常见）—— 已自动重试仍失败，请降低调用频率或换付费模型",
        500: "服务端内部错误 —— 稍后重试",
    }
    return hints.get(status, f"调用失败（HTTP {status}）：{raw}")


def chat(prompt: str) -> str:
    last_err: Exception | None = None

    for attempt in range(1, MAX_RETRY + 1):
        try:
            resp = _get_client().chat.completions.create(
                model=config.LLM_MODEL,
                temperature=config.LLM_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content or ""

        except APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status in RETRY_STATUS and attempt < MAX_RETRY:
                wait = 2 ** attempt
                print(f"  [重试 {attempt}/{MAX_RETRY - 1}] HTTP {status}，{wait}s 后重试…")
                time.sleep(wait)
                last_err = e
                continue
            raise RuntimeError(_explain(status, str(e))) from e

        except APITimeoutError as e:
            if attempt < MAX_RETRY:
                print(f"  [重试 {attempt}/{MAX_RETRY - 1}] 请求超时，稍后重试…")
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise RuntimeError("请求超时，检查网络或代理设置（可尝试关闭代理）") from e

    raise RuntimeError(f"多次重试仍失败：{last_err}")


def extract_sql(text: str) -> str:
    """从模型输出里剥离 Markdown 代码块等多余内容，只留 SQL。"""
    text = text.strip()
    m = re.search(r"```(?:sql|mysql)?\s*(.+?)```", text, re.S | re.I)
    if m:
        text = m.group(1)
    # 截断到第一个完整语句的分号
    if ";" in text:
        text = text.split(";")[0] + ";"
    return text.strip()


def generate_sql(question: str, schema: str, version: str = "v3") -> str:
    from prompt_templates import build_prompt

    return extract_sql(chat(build_prompt(question, schema, version)))
