"""主流程：自然语言 → SQL → 安全校验 → 执行 → 失败自动纠错 → 结果表格。

用法：
    python text2sql.py "2017 年每个月延迟率是多少"
    python text2sql.py "销售额最高的 10 个商品类目" --version v3
    python text2sql.py --no-fix "复购客户有多少"        # 关闭自动纠错，看首次生成的原始表现
"""
from __future__ import annotations

import argparse
import sys
import time

import pandas as pd

import config
from prompt_templates import VERSIONS
from schema_context import build_schema_context
from selfcorrect import generate_and_run
from sql_guard import UnsafeSQLError

_schema_cache: str | None = None


def get_schema() -> str:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = build_schema_context()
    return _schema_cache


def run_query(sql: str) -> pd.DataFrame:
    from schema_context import get_connection

    con = get_connection()
    try:
        with con.cursor() as cur:
            # MySQL 5.7.8+ 支持会话级超时，避免大表全扫拖死连接
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {config.QUERY_TIMEOUT * 1000}")
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        con.close()
    return pd.DataFrame(rows)


def ask(question: str, version: str = "v5", show_sql: bool = True, max_fix: int = 2):
    t0 = time.time()
    sql, df, fixes, _ = generate_and_run(question, get_schema(), version, max_fix)
    elapsed = time.time() - t0

    if show_sql:
        tag = f"自纠错 {fixes} 次" if fixes else "一次通过"
        print(f"\n[生成 SQL · {version} · {tag} · {elapsed:.1f}s]\n{sql}\n")

    print(f"[结果 · {len(df)} 行]")
    if df.empty:
        print("（无数据，检查过滤条件）")
    else:
        with pd.option_context("display.max_columns", None, "display.width", 200):
            print(df.to_string(index=False))
    return sql, df


def main() -> int:
    ap = argparse.ArgumentParser(description="Text-to-SQL 智能查询")
    ap.add_argument("question", nargs="?", help="自然语言问题")
    ap.add_argument("--version", default=config.PROMPT_VERSION, choices=VERSIONS)
    ap.add_argument("--show-sql", action="store_true", help="打印生成的 SQL")
    ap.add_argument("--no-fix", action="store_true", help="关闭自动纠错（只看首次生成）")
    ap.add_argument("--max-fix", type=int, default=2, help="自动纠错最多重试次数，默认 2")
    args = ap.parse_args()

    config.check()
    if not args.question:
        print(__doc__)
        return 1

    try:
        ask(args.question, args.version, show_sql=True,
            max_fix=0 if args.no_fix else args.max_fix)
    except UnsafeSQLError as e:
        print(f"[安全拦截] {e}")
        return 2
    except Exception as e:  # 数据库或模型异常
        print(f"[执行失败] {type(e).__name__}: {e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
