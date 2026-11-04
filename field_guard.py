"""SQL 静态字段校验：在执行之前就查列名归属。

模型最常犯的错是把列挂到错误的表上（p.price、oi.is_late、o.seller_id）。
MySQL 要到执行时才报错，而且只说"不存在"，不告诉你该去哪张表找。
这里提前静态检查，并把正确归属一并告知，纠错效率比等数据库报错高得多。

只检查 `别名.列名` 这种带限定的写法（可靠），裸列名交给数据库判断。
"""
from __future__ import annotations

import re

import config
from schema_context import ALLOWED_TABLES, get_connection

_cols_cache: dict[str, set[str]] | None = None


class ColumnError(Exception):
    """SQL 里引用了不属于该表的列。"""


def table_columns() -> dict[str, set[str]]:
    """{表名: {列名, ...}}，结果缓存，避免每次查询都读 INFORMATION_SCHEMA。"""
    global _cols_cache
    if _cols_cache is None:
        con = get_connection()
        try:
            with con.cursor() as cur:
                placeholders = ",".join(["%s"] * len(ALLOWED_TABLES))
                cur.execute(
                    f"""SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders})""",
                    [config.DB_NAME, *ALLOWED_TABLES],
                )
                cache: dict[str, set[str]] = {}
                for r in cur.fetchall():
                    cache.setdefault(r["TABLE_NAME"], set()).add(r["COLUMN_NAME"].lower())
                _cols_cache = cache
        finally:
            con.close()
    return _cols_cache


_ALIAS_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_]\w*)\s*(?:as\s+)?([A-Za-z_]\w*)?", re.I)
_QUALIFIED_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")

# 这些词跟在 FROM 后面不是表名，避免误判
_NOT_TABLE = {"select", "dual"}


def check_columns(sql: str) -> list[str]:
    """返回问题清单，空列表表示没发现问题。"""
    cols = table_columns()

    alias: dict[str, str] = {}
    for m in _ALIAS_RE.finditer(sql):
        tbl = m.group(1)
        if tbl.lower() in _NOT_TABLE:
            continue
        if tbl in cols:
            alias[(m.group(2) or tbl).lower()] = tbl

    problems: list[str] = []
    seen: set[str] = set()

    for m in _QUALIFIED_RE.finditer(sql):
        al, col = m.group(1).lower(), m.group(2).lower()
        if al not in alias or (al, col) in seen:
            continue
        seen.add((al, col))
        tbl = alias[al]
        if col in cols[tbl]:
            continue

        owners = [t for t, cs in cols.items() if col in cs]
        if owners:
            problems.append(f"{al}.{col} 写错了表：{tbl} 没有该列，它属于 {owners[0]}")
        else:
            problems.append(f"{al}.{col} 不存在：库里没有名为 {col} 的列，请只用表结构清单里的列")

    return problems
