"""提示词模板：v1 基础版 → v2 规则增强 → v3 few-shot → v4 修正版。

保留四个版本不是历史包袱，而是为了在 README 里做**迭代对比**：
同一批 20 道题，各版本执行成功率 / 口径正确率是多少 —— 这就是"提示词调优"的实证。

迭代记录（真实踩坑）：
- v1：只有基础规则，口径大体对，但结果不排序、比率输出成小数（0.0439），不可读
- v2：补 4 条业务规则（别名、时间字段、百分比、平均延迟天数必须 is_late=1）
- v3：补 3 个 few-shot 示例 —— 但示例本身有两个缺陷：
    1) 示例 2 用了库里不存在的 product_category_name，模型照着抄，类目类题目必然失败
    2) 示例 1、3 与评估集第 3、6 题完全重复，等于答案泄漏，评出的准确率虚高
  实测 v3 执行成功率 75%（15/20），失败 5 题全部集中在多表关联
- v4：修正 few-shot 字段名（category_en / freight_value）、示例全部换成评估集之外的题，
      并把"字段名必须逐字取自 schema"写成硬规则
"""
from __future__ import annotations

BASE_RULES = """你是 MySQL 8.0 的 SQL 专家。根据用户的自然语言问题，生成一条可直接执行的查询语句。

要求：
1. 只输出 SQL，不要解释、不要 Markdown 代码块标记。
2. 只允许 SELECT 或 WITH 开头的查询，禁止任何写操作。
3. 只使用下面给出的表和字段，禁止臆造字段名。
4. 涉及明细列表时加 LIMIT 200。
"""

V1_PROMPT = """{rules}

【可用表结构】
{schema}

【问题】
{question}

SQL:"""

V2_PROMPT = """{rules}
5. 聚合结果请给出有意义的别名（中文或英文均可），便于阅读。
6. 时间过滤用 purchase_date 或 purchase_ts；按年月聚合优先用 purchase_year / purchase_month。
7. 计算比率时用 ROUND(x * 100, 2) 转成百分数并标注含义。
8. 如果问题涉及"平均延迟天数"，必须加 is_late = 1 条件。

【可用表结构】
{schema}

【问题】
{question}

请先在心里确认表关联关系与过滤条件，然后输出 SQL：

SQL:"""

# v3 用的示例：字段名有误 + 与评估集重复，仅保留用于迭代对照，不要再用
FEW_SHOT_V3 = """
【示例 1】
问题：2017 年每个月的延迟率是多少
SQL:SELECT purchase_year, purchase_month,
       ROUND(SUM(is_late = 1) * 100.0 / COUNT(*), 2) AS late_rate_pct
FROM dwd_orders
WHERE purchase_year = 2017 AND is_delivered = 1
GROUP BY purchase_year, purchase_month
ORDER BY purchase_month;

【示例 2】
问题：销售额最高的 10 个商品类目
SQL:SELECT p.product_category_name,
       ROUND(SUM(oi.price), 2) AS gmv
FROM dwd_order_items oi
JOIN dwd_orders o ON o.order_id = oi.order_id
JOIN dim_product p ON p.product_id = oi.product_id
WHERE o.order_status = 'delivered'
GROUP BY p.product_category_name
ORDER BY gmv DESC
LIMIT 10;

【示例 3】
问题：下单两次及以上的客户有多少人
SQL:SELECT COUNT(*) AS repeat_customers
FROM (
    SELECT customer_unique_id
    FROM dwd_orders
    GROUP BY customer_unique_id
    HAVING COUNT(DISTINCT order_id) >= 2
) t;
"""

# v4 用的示例：字段名与库一致，且三题都不在 eval_questions.csv 里（避免答案泄漏）
FEW_SHOT_V4 = """
【示例 1】
问题：平均每个订单包含几件商品
SQL:SELECT ROUND(COUNT(*) / COUNT(DISTINCT order_id), 2) AS items_per_order
FROM dwd_order_items;

【示例 2】
问题：取消订单占全部订单的比例是多少
SQL:SELECT ROUND(SUM(order_status = 'canceled') * 100.0 / COUNT(*), 2) AS cancel_rate_pct
FROM dwd_orders;

【示例 3】
问题：运费最高的 5 个州
SQL:SELECT c.state,
       ROUND(SUM(oi.freight_value), 2) AS total_freight
FROM dwd_order_items oi
JOIN dwd_orders o ON o.order_id = oi.order_id
JOIN dim_customer c ON c.customer_unique_id = o.customer_unique_id
WHERE o.is_delivered = 1
GROUP BY c.state
ORDER BY total_freight DESC
LIMIT 5;
"""

# v5 用：在 v4 三个示例基础上补一个"先收敛到订单粒度再聚合"的示范，
# 该题（各州平均订单金额）不在 eval_questions.csv 里，不构成答案泄漏
FEW_SHOT_V5 = FEW_SHOT_V4 + """
【示例 4】（多表关联后聚合，注意先把粒度收敛到订单再算平均）
问题：每个州的平均订单金额是多少
SQL:WITH order_level AS (
    SELECT o.order_id, c.state,
           SUM(oi.price + oi.freight_value) AS order_amount
    FROM dwd_orders o
    JOIN dwd_order_items oi ON oi.order_id = o.order_id
    JOIN dim_customer c ON c.customer_unique_id = o.customer_unique_id
    WHERE o.is_delivered = 1
    GROUP BY o.order_id, c.state
)
SELECT state, ROUND(AVG(order_amount), 2) AS avg_order_amount
FROM order_level
GROUP BY state
ORDER BY avg_order_amount DESC;
"""

V3_PROMPT = """{rules}
5. 聚合结果请给出有意义的别名（中文或英文均可），便于阅读。
6. 时间过滤用 purchase_date 或 purchase_ts；按年月聚合优先用 purchase_year / purchase_month。
7. 计算比率时用 ROUND(x * 100, 2) 转成百分数并标注含义。
8. 如果问题涉及"平均延迟天数"，必须加 is_late = 1 条件。
9. 多表关联时确认关联键：订单相关一律通过 order_id 关联。

{few_shot}

【可用表结构】
{schema}

【问题】
{question}

SQL:"""

V4_PROMPT = """{rules}
5. 聚合结果请给出有意义的别名（中文或英文均可），便于阅读。
6. 时间过滤用 purchase_date 或 purchase_ts；按年月聚合优先用 purchase_year / purchase_month。
7. 计算比率时用 ROUND(x * 100, 2) 转成百分数并标注含义。
8. 如果问题涉及"平均延迟天数"，必须加 is_late = 1 条件。
9. 多表关联时确认关联键：订单相关一律通过 order_id 关联；
   客户用 customer_unique_id 关联 dim_customer；商品用 product_id 关联 dim_product；
   卖家用 seller_id 关联 dim_seller（注意 seller_id 在 dwd_order_items 上，不在 dwd_orders）。
10. **字段名必须逐字复制自下面的表结构清单**。不要凭"常见电商表"的习惯改写：
    品类是 category_en 不是 product_category_name；支付总额是 payment_total 不是 payment_value。
    写完请逐个核对每个字段是否真的在清单里出现。
11. 结果按业务含义排序（时间序列按月份、排行榜按指标降序），不要让结果乱序输出。

{few_shot}

【可用表结构】
{schema}

【问题】
{question}

SQL:"""

V5_PROMPT = """{rules}
5. 聚合结果请给出有意义的别名（中文或英文均可），便于阅读。
6. 时间过滤用 purchase_date 或 purchase_ts；按年月聚合优先用 purchase_year / purchase_month。
7. 计算比率时用 ROUND(x * 100, 2) 转成百分数并标注含义。
8. 如果问题涉及"平均延迟天数"，必须加 is_late = 1 条件。
9. 多表关联时确认关联键：订单相关一律通过 order_id 关联；
   客户用 customer_unique_id 关联 dim_customer；商品用 product_id 关联 dim_product；
   卖家用 seller_id 关联 dim_seller（注意 seller_id 在 dwd_order_items 上，不在 dwd_orders）。
10. **字段名必须逐字复制自下面的表结构清单**。不要凭"常见电商表"的习惯改写：
    品类是 category_en 不是 product_category_name；支付总额是 payment_total 不是 payment_value。
    写完请逐个核对每个字段是否真的在清单里出现。
11. 结果按业务含义排序（时间序列按月份、排行榜按指标降序），不要让结果乱序输出。

12. **聚合前先对齐粒度（最容易错的一条）**：dwd_order_items 一行是一件商品，
    把它和"一单一行"的 dwd_orders / dwd_payments 按 order_id 关联后，同一订单会被重复计 N 次
    （N = 该单商品件数），金额与比率都会虚高。规则：
    - 求金额：卖家 / 商品 / 品类维度直接在 dwd_order_items 上 SUM(price)，
      不要 JOIN dwd_payments 再 SUM(payment_total)（那会把订单总额乘上件数）
    - 求比率：先用 CTE 或子查询收敛到目标粒度（如 SELECT DISTINCT order_id, category_en），
      再在收敛后的结果上算分子与分母，保证两者计数单位一致
    - 求订单数：COUNT(DISTINCT order_id)，不要 COUNT(*)；
      需要"每单平均值"时先把明细聚合到 order_id 粒度，再对订单粒度求平均

13. **GROUP BY 的列必须出现在 SELECT 中**。按年月分组时必须输出 purchase_year / purchase_month，
    否则结果只有一列数字、没有维度，无法解读。

14. **中位数**用窗口函数定位中间行：ROW_NUMBER() OVER (ORDER BY 列) 配合 COUNT(*) OVER ()，
    在 rn 落在 (cnt+1)/2 附近的行上取平均；禁止用"最接近平均值的某个值"代替中位数。

15. **同比 / 环比 / 相邻行比较**用窗口函数 LAG(列) OVER (ORDER BY 月份)，
    OVER 子句不可省略；不要用自连接拼相邻月份。

{few_shot}

【可用表结构】
{schema}

【问题】
{question}

SQL:"""

V6_PROMPT = """{rules}
5. 聚合结果请给出有意义的别名（中文或英文均可），便于阅读。
6. 时间过滤用 purchase_date 或 purchase_ts；按年月聚合优先用 purchase_year / purchase_month。
7. 计算比率时用 ROUND(x * 100, 2) 转成百分数并标注含义。
8. 如果问题涉及"平均延迟天数"，必须加 is_late = 1 条件。
   **但这条仅适用于"延迟订单的平均延迟天数"这类只针对延迟订单的指标。
   计算延迟率 / 准时率时禁止在 WHERE 里写 is_late = 1** —— 那会把分子分母同时砍掉，
   比率会算出超过 100% 的荒谬结果。延迟率只筛 is_delivered = 1，is_late 放进 CASE WHEN 当分子。
9. 多表关联时确认关联键：订单相关一律通过 order_id 关联；
   客户用 customer_unique_id 关联 dim_customer；商品用 product_id 关联 dim_product；
   卖家用 seller_id 关联 dim_seller（注意 seller_id 在 dwd_order_items 上，不在 dwd_orders）。
10. **字段名必须逐字复制自下面的表结构清单**。不要凭"常见电商表"的习惯改写：
    品类是 category_en 不是 product_category_name；支付总额是 payment_total 不是 payment_value。
    写完请逐个核对每个字段是否真的在清单里出现。
11. 结果按业务含义排序（时间序列按月份、排行榜按指标降序），不要让结果乱序输出。

12. **聚合前先对齐粒度（最容易错的一条）**：dwd_order_items 一行是一件商品，
    把它和"一单一行"的 dwd_orders / dwd_payments 按 order_id 关联后，同一订单会被重复计 N 次
    （N = 该单商品件数）。三类写法分别要求：
    - 求金额：卖家 / 商品 / 品类维度直接在 dwd_order_items 上 SUM(price)，
      不要 JOIN dwd_payments 再 SUM(payment_total)（那会把订单总额乘上件数）
    - **求比率：分两步走，第一步的 CTE 里禁止出现任何聚合函数**
      （不许有 COUNT / SUM / AVG / GROUP BY，写了就等于没收敛），只允许 SELECT DISTINCT 原始列：
        WITH base AS (
          SELECT DISTINCT 维度列, order_id, is_late
          FROM dwd_orders o
          JOIN dwd_order_items oi ON o.order_id = oi.order_id
          JOIN ... WHERE o.is_delivered = 1
        )
      第二步才在外层做聚合，此时每一行 = 一个"订单 × 维度"组合，分子分母单位天然一致：
        SELECT 维度列, ROUND(SUM(is_late) * 100.0 / COUNT(*), 2) AS rate_pct
        FROM base GROUP BY 维度列
      错误形态（分子按商品行数、分母按订单数，比率虚高数倍，但 MySQL 不会报错）——
        SELECT 维度列, SUM(CASE WHEN is_late = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(DISTINCT order_id) ...
      判断方法：如果分母用了 COUNT(DISTINCT order_id) 而分子用了 SUM(CASE ... THEN 1)，两者单位必不相同，
      说明没有先收敛粒度，必须改写成上面的两步式。
    - 求订单数：COUNT(DISTINCT order_id)；
      需要"每单平均值"时先把明细聚合到 order_id 粒度，再对订单粒度求平均

13. **GROUP BY 的列必须出现在 SELECT 中**。按年月分组时必须输出 purchase_year / purchase_month，
    否则结果只有一列数字、没有维度，无法解读。

14. **中位数**必须拆成两步，不要试图一步算出来：
    第一步，CTE 里用窗口函数打标——
        SELECT 列, ROW_NUMBER() OVER (ORDER BY 列) AS rn, COUNT(*) OVER () AS cnt FROM 表
    第二步，外层求值——
        SELECT AVG(列) AS 平均值,
               AVG(CASE WHEN rn IN (FLOOR((cnt + 1) / 2), CEIL((cnt + 1) / 2)) THEN 列 END) AS 中位数
        FROM 第一步
    rn / cnt 是窗口函数产生的列，只能放进 CASE WHEN 中参与聚合；直接写进外层 SELECT 的表达式
    或出现在 WHERE 里都会触发 1140 错误（聚合查询引用了非聚合列）。

15. **同比 / 环比 / 相邻行比较**用窗口函数 LAG(列) OVER (ORDER BY 月份)，
    OVER 子句不可省略；不要用自连接拼相邻月份。

{few_shot}

【可用表结构】
{schema}

【问题】
{question}

SQL:"""

# v7 = v5 + 中位数 / 环比两条写法约束。
# 依据：v5 的第 16 题（中位数）两次纠错都撞 1140 错误，补模板后 v6 该题一次性成功；
# 而 v6 里粒度规则的详细示例反而污染了小模型对字段表的认知（第 3、15 题退化），故不保留。
_EXTRA_V7 = """
14. **中位数**必须拆成两步，不要一步算出来：
    第一步 CTE 里用窗口函数打标——
        SELECT 列, ROW_NUMBER() OVER (ORDER BY 列) AS rn, COUNT(*) OVER () AS cnt FROM 表
    第二步外层求值——
        SELECT AVG(列) AS 平均值,
               AVG(CASE WHEN rn IN (FLOOR((cnt + 1) / 2), CEIL((cnt + 1) / 2)) THEN 列 END) AS 中位数
        FROM 第一步
    rn / cnt 是窗口函数产生的列，只能放进 CASE WHEN 参与聚合；写进外层 SELECT 的表达式
    或出现在 WHERE 里会触发 1140 错误。

15. **同比 / 环比 / 相邻行比较**用 LAG(列) OVER (ORDER BY 月份)，不要用自连接拼相邻月份。
"""

V7_PROMPT = V5_PROMPT.replace("{few_shot}", _EXTRA_V7.strip() + "\n\n{few_shot}", 1)

_TEMPLATES = {
    "v1": (V1_PROMPT, ""),
    "v2": (V2_PROMPT, ""),
    "v3": (V3_PROMPT, FEW_SHOT_V3),
    "v4": (V4_PROMPT, FEW_SHOT_V4),
    "v5": (V5_PROMPT, FEW_SHOT_V5),
    "v6": (V6_PROMPT, FEW_SHOT_V5),
    "v7": (V7_PROMPT, FEW_SHOT_V5),
}

VERSIONS = list(_TEMPLATES)


def build_prompt(question: str, schema: str, version: str = "v6") -> str:
    tpl, few_shot = _TEMPLATES.get(version, _TEMPLATES["v6"])
    return tpl.format(
        rules=BASE_RULES.strip(),
        schema=schema,
        question=question,
        few_shot=few_shot.strip(),
    )
