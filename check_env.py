"""一键环境体检：依赖 -> .env -> MySQL -> 大模型。

出问题先跑这个，比逐条读 traceback 快。
    python check_env.py
"""
from __future__ import annotations

import sys
import time

OK = "  [OK]"
BAD = "  [!!]"
SKIP = "  [--]"


def _head(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")


def check_python() -> bool:
    _head(1, 4, f"运行环境  Python {sys.version.split()[0]}")
    print(f"       解释器：{sys.executable}")

    missing = []
    for mod, pkg in (("openai", "openai"), ("pymysql", "PyMySQL"), ("dotenv", "python-dotenv")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"{BAD} 缺少依赖：{', '.join(missing)}")
        print(f"       修复：pip install -r requirements.txt")
        return False
    print(f"{OK} openai / PyMySQL / python-dotenv 均已安装")
    return True


def check_envfile() -> bool:
    _head(2, 4, "配置文件 .env")

    import config

    try:
        config.check()
    except SystemExit as e:
        print(f"{BAD} {e}")
        print(f"       用记事本打开本目录下的 .env，把占位符换成真实值")
        return False

    print(f"{OK} 数据库：{config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
    print(f"{OK} 模型：{config.LLM_MODEL}  @ {config.LLM_BASE_URL}")
    print(f"       Key 已填（{len(config.LLM_API_KEY)} 位）")
    return True


def check_db() -> bool:
    _head(3, 4, "MySQL 连接")

    import pymysql
    import config

    try:
        conn = pymysql.connect(
            host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
            password=config.DB_PASSWORD, database=config.DB_NAME, connect_timeout=5,
        )
    except Exception as e:
        print(f"{BAD} 连不上：{type(e).__name__}: {e}")
        print(f"       检查 MySQL80 服务是否启动；密码是否为安装时设置的那个")
        return False

    with conn:
        cur = conn.cursor()
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM dwd_orders")
        n = cur.fetchone()[0]

    print(f"{OK} 连接成功，共 {len(tables)} 张表")
    print(f"       dwd_orders 行数：{n:,}")
    return True


def check_llm() -> bool:
    _head(4, 4, "大模型调用")

    import config
    import llm_client

    t0 = time.time()
    try:
        reply = llm_client.chat("只回复两个字：正常")
    except Exception as e:
        print(f"{BAD} {e}")
        return False

    cost = time.time() - t0
    print(f"{OK} 调用成功，耗时 {cost:.1f}s，返回：{reply.strip()[:40]}")
    print(f"       模型：{config.LLM_MODEL}")
    return True


def main() -> int:
    print("=" * 56)
    print("  Text-to-SQL 环境体检")
    print("=" * 56)

    checks = [check_python, check_envfile, check_db, check_llm]
    ok = True
    for i, fn in enumerate(checks):
        if not ok:
            print(f"{SKIP} 剩余 {len(checks) - i} 项跳过")
            break
        ok = fn()

    print("\n" + "=" * 56)
    if ok:
        print("  四项全过，可以跑：python text2sql.py \"你的问题\"")
    else:
        print("  有未通过项，按上面的提示修")
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
