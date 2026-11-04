"""从 MySQL 自动抽取表结构，拼成给大模型看的 schema 文本。

为什么要动态抽取而不是手写真名？
- 手写会和库脱节（加字段忘了改），动态抽取永远准
- 列注释（COLUMN_COMMENT）本身就是业务知识，能显著提升生成准确率
"""
from __future__ import annotations

import pymysql

import config

# 只给模型看这几张表，避免无关表干扰（ODS 原始层不给，业务问法都走 DWD/DIM）
ALLOWED_TABLES = [
    "dwd_orders",
    "dwd_order_items",
    "dwd_payments",
    "dwd_reviews",
    "dim_customer",
    "dim_product",
    "dim_seller",
]

# 模型不知道的业务口径，必须人工补充（这是提示词迭代中最重要的部分）
# 每一条都踩过坑才写上去的：字段名一律按库里真实结构核对过，不按"通用电商表"的习惯想当然
BUSINESS_NOTES = """
【业务口径说明 — 必须遵守】
1. 金额：订单支付总额取 dwd_payments.payment_total（这张表是**订单级汇总**，一单一行，没有明细支付记录）；
   商品金额取 dwd_order_items.price（不含运费），运费 freight_value，二者合计为 item_total。
2. 延迟判定：is_late = 1 表示延迟送达，**只对已签收订单（is_delivered = 1）有效**，未签收订单 is_late 为 NULL。
   算延迟率时分母必须限定 is_delivered = 1，否则会被未签收订单稀释。
3. 延迟天数：late_days 为正表示延迟、为负表示提前。计算"平均延迟天数"必须加 is_late = 1，
   否则提前送达的负值会把均值拉低（历史上有 88,644 条提前送达记录）。
4. 客户去重：用 customer_unique_id（真实客户，跨订单不变），不要用 customer_id（每单唯一，会把同一人算多次）。
5. 时间：下单时间 purchase_ts、下单日期 purchase_date；按年月聚合直接用 purchase_year / purchase_month。
6. 订单状态：order_status（delivered / shipped / canceled / unavailable 等），统计销售额通常限定 delivered，
   也可用 is_delivered = 1。
7. 商品品类：字段是 dim_product.category_en（英文）和 category_pt（葡语原名）。
   **本库没有 product_category_name 这个字段**；has_category = 0 表示品类缺失。
8. 支付方式：只有 dwd_payments.main_payment_type（订单级"金额最大的支付方式"），
   **没有明细级的 payment_type / payment_value 字段**，按支付方式分组直接用这一列。
9. 卖家：seller_id 位于 dwd_order_items 与 dim_seller，**dwd_orders 上没有该字段**；
   卖家所在地在 dim_seller.state / city。
10. 字段名必须逐字取自上面的 CREATE TABLE 清单，不要替换成你熟悉的同类名
   （例如把 category_en 写成 product_category_name、把 payment_total 写成 payment_value）。拿不准就只用清单里存在的字段。
11. dwd_order_items 是订单商品明细，**一行就是一件商品，没有 quantity / amount / qty 字段**：
   求销售额直接 SUM(price)，求件数用 COUNT(*)，求涉及订单数用 COUNT(DISTINCT order_id)。
   同理 dim_product 没有 product_name（只有 name_length / description_length）。
"""


def get_connection():
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


def fetch_schema(tables: list[str] | None = None) -> str:
    """返回形如 CREATE TABLE 的 schema 文本（含列类型与注释）。"""
    tables = tables or ALLOWED_TABLES
    placeholders = ",".join(["%s"] * len(tables))
    sql = f"""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders})
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """
    con = get_connection()
    try:
        with con.cursor() as cur:
            cur.execute(sql, [config.DB_NAME, *tables])
            rows = cur.fetchall()
    finally:
        con.close()

    grouped: dict[str, list[str]] = {}
    for r in rows:
        comment = f"  -- {r['COLUMN_COMMENT']}" if r["COLUMN_COMMENT"] else ""
        grouped.setdefault(r["TABLE_NAME"], []).append(
            f"  {r['COLUMN_NAME']} {r['COLUMN_TYPE']}{comment}"
        )

    parts = []
    for table in tables:
        if table not in grouped:
            continue
        cols = "\n".join(grouped[table])
        parts.append(f"CREATE TABLE {table} (\n{cols}\n);")
    return "\n\n".join(parts)


def build_schema_context(tables: list[str] | None = None) -> str:
    return fetch_schema(tables) + "\n" + BUSINESS_NOTES


if __name__ == "__main__":
    print(build_schema_context())
