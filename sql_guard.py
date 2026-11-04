"""SQL 安全校验：让模型生成的语句在可控范围内执行。

四道闸门：
1. 只读     —— 只允许 SELECT / WITH 开头
2. 禁多语句 —— 分号后还有内容直接拒绝（防注入、防误删）
3. 限行     —— 没有 LIMIT 时自动补上
4. 超时     —— 由执行层设置（见 text2sql.run_query）
"""
from __future__ import annotations

import re

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|load_file|into\s+outfile)\b",
    re.I,
)


class UnsafeSQLError(ValueError):
    pass


def validate(sql: str, max_rows: int = 200) -> str:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQLError("模型未返回有效 SQL")

    if not re.match(r"^(select|with)\b", sql, re.I):
        raise UnsafeSQLError("只允许 SELECT / WITH 查询")

    if ";" in sql:
        raise UnsafeSQLError("检测到多条语句，已拒绝执行")

    if FORBIDDEN.search(sql):
        raise UnsafeSQLError("检测到写操作或危险函数，已拒绝执行")

    if not re.search(r"\blimit\b", sql, re.I):
        sql = f"{sql}\nLIMIT {max_rows}"
    return sql
