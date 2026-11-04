"""配置：数据库与模型参数统一从环境变量读取（.env 文件）。"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# ---------- 数据库 ----------
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "olist_db")

# ---------- 大模型（OpenAI 兼容协议，DeepSeek / 通义 / 智谱均可）----------
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))

# ---------- 运行参数 ----------
QUERY_TIMEOUT = int(os.getenv("QUERY_TIMEOUT", "30"))   # 单条 SQL 执行超时（秒）
MAX_ROWS = int(os.getenv("MAX_ROWS", "200"))           # 默认返回行数上限
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v4")     # v1 / v2 / v3 / v4


def _not_filled(v: str) -> bool:
    """判断一项配置是不是还没填（空值或仍是模板里的中文占位符）。"""
    if not v:
        return True
    low = v.strip().lower()
    return low.startswith("你的") or low in {"todo", "xxx", "changeme", "none", "null"}


def check():
    """启动前的必填项检查。"""
    missing = []
    if _not_filled(DB_PASSWORD):
        missing.append("DB_PASSWORD（数据库密码）")
    if _not_filled(LLM_API_KEY):
        missing.append("LLM_API_KEY（大模型 API Key）")
    if missing:
        raise SystemExit(
            "还没填完。请用记事本打开 text2sql 目录下的 .env 文件，把这两项改成真实值："
            + "、".join(missing)
        )
