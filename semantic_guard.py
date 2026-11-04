"""比率类查询的语义校验（semantic guard）。

为什么需要它：
字段挂错表这类错误，MySQL 会直接报错，靠 `selfcorrect` 把报错喂回去就能修好。
但**口径错误不报错** —— 下面这条 SQL 能跑、有结果、数字看着也正常：

    SELECT category_en,
           SUM(CASE WHEN is_late = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(DISTINCT order_id)
    FROM dwd_orders JOIN dwd_order_items USING (order_id) ...
    GROUP BY category_en

问题在分子按"商品行"计数、分母按"订单"去重计数。一个订单买 3 件商品，分子 +3、分母 +1，
延迟率被放大约 1.13 倍，而且各品类放大倍数不同，**排名都会失真**。

这类错误只能靠语义检查发现。本模块就在执行前做这件事：命中就返回问题描述，
交给 `selfcorrect` 带着明确指令重写一次 —— 这是"生成 + 校验器"的标准组合。
4. **GROUP BY 了维度却没输出维度列** —— 结果只有一列数字、没有对应关系，无法解读。

"""
from __future__ import annotations

import re

# "一行一件商品"的明细表，是 fan-out 的源头
DETAIL_TABLE = "dwd_order_items"

_RATIO_NUM = re.compile(r"sum\s*\(\s*case\s+when", re.I)
_RATIO_DEN = re.compile(r"/\s*count\s*\(\s*distinct\s+(?:\w+\.)?order_id", re.I)
# 分母可能先算成别名、在外层再相除（CTE 写法），要单独认出来
_DEN_ALIAS = re.compile(
    r"count\s*\(\s*distinct\s+(?:\w+\.)?order_id\s*\)\s*\)?\s*(?:\*\s*100(?:\.0+)?\s*)?"
    r"(?:as\s+)?([a-zA-Z_][a-zA-Z0-9_]*)?",
    re.I,
)
_AMOUNT_BY_PAYMENTS = re.compile(r"sum\s*\(\s*(?:\w+\.)?payment_total", re.I)
_JOIN_PAYMENTS = re.compile(r"join\s+dwd_payments", re.I)


def _where_clause(low: str) -> str:
    """取出 WHERE 子句文本（到 GROUP BY / ORDER BY / LIMIT 为止）。"""
    m = re.search(
        r"\bwhere\b(.*?)(?:\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)",
        low,
        re.S,
    )
    return m.group(1) if m else ""


def _alias_is_divisor(low: str, alias: str | None) -> bool:
    """别名是否作为除数/被除数参与除法（说明它和分子构成了一个比率）。"""
    if not alias or alias.upper() in {"AS", "FROM", "WHERE", "GROUP", "ORDER", "ON"}:
        return False
    a = re.escape(alias)
    return bool(
        re.search(rf"/\s*\(?\s*{a}\b", low) or re.search(rf"\b{a}\s*\)?\s*/", low)
    )


def check_ratio(sql: str) -> str | None:
    """检查 SQL 是否存在口径类错误，返回问题描述；无问题返回 None。

    只覆盖两类最常见的、且数据库不会报错的错法：
    1. 比率分子分母粒度不一致（分子按明细行、分母按订单去重）
    2. 明细表 JOIN 订单级汇总表后再对汇总字段求和（金额被乘以件数）
    """
    low = sql.lower()
    if DETAIL_TABLE not in low:
        return None

    # 1) 比率粒度不一致：只认"两者确实构成一个比率"的情况，
    #    避免误伤"同时输出延迟单量 和 订单数"这种并列指标
    num = _RATIO_NUM.search(low)
    if num:
        den = _RATIO_DEN.search(low)
        same_expr = den and 0 < den.start() - num.end() < 400
        alias = _DEN_ALIAS.search(low)
        via_alias = bool(alias) and _alias_is_divisor(low, alias.group(1))
        if same_expr or via_alias:
            return (
                "分子用 SUM(CASE WHEN ... THEN 1) 按商品明细行计数，"
                "分母用 COUNT(DISTINCT order_id) 按订单去重计数，两者粒度不一致："
                "同一订单有 N 件商品时，分子会被放大 N 倍，比率虚高且排名失真。"
            )

    # 2) 明细表 JOIN 订单级汇总表后 SUM 汇总字段：一笔订单金额被按件数重复累加
    if _JOIN_PAYMENTS.search(low) and _AMOUNT_BY_PAYMENTS.search(low):
        return (
            "JOIN dwd_payments（一单一行）之后对 payment_total 求和，"
            "而 dwd_order_items 一单多行，同一笔支付会被重复累加 N 次。"
        )

    # 3) 比率类查询把 is_late = 1 写进 WHERE：分子分母同时被砍，比率失真（甚至超过 100%）
    where = _where_clause(low)
    if re.search(r"is_late\s*=\s*1", where) and "/" in low:
        return (
            "WHERE 里筛了 is_late = 1 之后又做除法：这样分子分母都只剩下延迟订单，"
            "算出来不是延迟率。延迟率只筛 is_delivered = 1，is_late 应放在 CASE WHEN 里当分子。"
        )

    return None


def _select_clause(low: str) -> str:
    m = re.search(r"\bselect\b(.*?)\bfrom\b", low, re.S)
    return m.group(1) if m else ""


def _group_by_clause(low: str) -> str:
    """取出 GROUP BY 的列清单。

    不能简单用正则截到 ORDER BY —— CTE 里的 GROUP BY 是以右括号结束的，
    而后面 LAG(...) OVER (ORDER BY ...) 里还有个 ORDER BY，会把窗口函数文本一起吃进来。
    所以按括号配对扫描：遇到最外层的右括号就停。
    """
    m = re.search(r"\bgroup\s+by\b", low)
    if not m:
        return ""
    depth, buf = 0, []
    for ch in low[m.end():]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        buf.append(ch)
    text = "".join(buf)
    # 再截到同一层的 having / order by / limit
    cut = re.search(r"\b(?:having|order\s+by|limit|window)\b", text)
    return text[: cut.start()] if cut else text


def check_group_columns(sql: str) -> str | None:
    """GROUP BY 的列必须出现在 SELECT 里，否则结果只有数字、没有维度，无法解读。"""
    low = sql.lower()
    grp = _group_by_clause(low)
    if not grp:
        return None
    sel = _select_clause(low)
    if not sel:
        return None
    missing = []
    for raw in grp.split(","):
        col = raw.strip()
        if not col or col.isdigit():  # GROUP BY 1,2 这种按序号的不检查
            continue
        base = col.split(".")[-1].strip()
        if base and base not in sel:
            missing.append(col)
    if missing:
        return (
            f"GROUP BY 了 {'、'.join(missing)}，但 SELECT 里没有输出这些列："
            "结果只有一列数字，看不出每个月/每个分组对应哪一行。"
            "请把分组列一并 SELECT 出来，并按分组列排序。"
        )
    return None


def check_semantics(sql: str) -> str | None:
    """语义口径检查总入口：返回第一个发现的问题，都通过则返回 None。"""
    return check_ratio(sql) or check_group_columns(sql)


RATIO_FIX_PROMPT = """你生成的 SQL 可以执行，但业务口径有误，请改写。

【用户的原始问题】
{question}

【上一版 SQL】
{sql}

【口径问题】
{problem}

【改写要求】
1. 涉及比率时，先用 CTE 把数据收敛到目标粒度：第一步的 CTE 里
   **只能写 SELECT DISTINCT 原始列，禁止出现 COUNT / SUM / AVG / GROUP BY**；
   第二步在外层聚合，此时每一行 = 一个"订单 × 维度"组合，分子分母单位一致。
2. 涉及金额时，卖家 / 商品 / 品类维度直接在 dwd_order_items 上 SUM(price)，
   不要 JOIN dwd_payments 再 SUM(payment_total)。
3. 其余业务口径保持不变：延迟率分母只筛 is_delivered = 1，is_late 只放在 CASE WHEN 里当分子。

【结构示范】（以"各州订单延迟率"为例，注意它和你要改的题不是同一个问题，只学结构）
WITH base AS (
    SELECT DISTINCT c.state, o.order_id, o.is_late
    FROM dwd_orders o
    JOIN dim_customer c ON c.customer_unique_id = o.customer_unique_id
    WHERE o.is_delivered = 1
)
SELECT state,
       ROUND(SUM(is_late) * 100.0 / COUNT(*), 2) AS late_rate_pct
FROM base
GROUP BY state
ORDER BY late_rate_pct DESC;

要点：base 里一个 (state, order_id) 只出现一次，所以外层 SUM(is_late) / COUNT(*) 的
分子分母都是"订单数"，单位一致；如果把 COUNT / SUM 写进 base，就等于没有收敛。

【可用表结构】
{schema}

只输出改写后的 SQL，不要解释、不要 Markdown 标记：
SQL:"""


if __name__ == "__main__":
    cases = [
        # 应命中：比率粒度不一致
        """SELECT p.category_en, SUM(CASE WHEN o.is_late = 1 THEN 1 ELSE 0 END) * 100.0
           / COUNT(DISTINCT o.order_id) FROM dwd_orders o
           JOIN dwd_order_items oi ON oi.order_id = o.order_id GROUP BY p.category_en""",
        # 应命中：明细 join 汇总表后求和
        """SELECT seller_id, SUM(p.payment_total) FROM dwd_order_items oi
           JOIN dwd_payments p ON p.order_id = oi.order_id GROUP BY seller_id""",
        # 不应命中：并列输出两个独立指标
        """SELECT category_en, COUNT(DISTINCT oi.order_id) AS orders,
           SUM(CASE WHEN o.is_late = 1 THEN 1 ELSE 0 END) AS late_rows
           FROM dwd_orders o JOIN dwd_order_items oi ON oi.order_id = o.order_id GROUP BY category_en""",
        # 不应命中：已收敛粒度后的正确写法
        """WITH base AS (SELECT DISTINCT order_id, category_en, is_late FROM dwd_orders o
           JOIN dwd_order_items oi ON oi.order_id = o.order_id)
           SELECT category_en, ROUND(SUM(is_late) * 100.0 / COUNT(*), 2) FROM base GROUP BY category_en""",
        # 应命中：分子分母先算成别名，外层再相除（CTE 写法，最容易漏检）
        """WITH base AS (SELECT p.category_en AS category, COUNT(DISTINCT o.order_id) AS order_count,
           SUM(CASE WHEN o.is_late = 1 THEN 1 ELSE 0 END) AS late_orders
           FROM dwd_orders o JOIN dwd_order_items oi ON o.order_id = oi.order_id
           WHERE o.is_delivered = 1 GROUP BY p.category_en)
           SELECT category, ROUND(late_orders * 100.0 / order_count, 2) AS rate FROM base""",
    ]
    for i, c in enumerate(cases, 1):
        r = check_ratio(c)
        print(f"case{i}: {'命中 -> ' + r[:40] if r else '通过'}")
