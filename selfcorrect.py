"""基于执行反馈的 SQL 自动纠错（self-correction / fix loop）。

两种错误，两条处理路径：

1. **执行报错**（列挂错表、语法错误）
   MySQL 的报错原文已经把话说清楚了（Unknown column 'p.price'），
   把报错连同原 SQL 喂回去，模型通常一次就能改对，用 FIX_PROMPT。

2. **口径错误**（能跑、有结果、但数字错）
   数据库不会报错，只能靠语义检查发现，见 semantic_guard.py。
   命中后用 RATIO_FIX_PROMPT 重写，指令里直接说清"哪一步写错了"。

这就是工业界 Text2-SQL 的标配做法（生成 + 校验器 + 重试），
比"换个更贵的模型"划算，也比把标准答案塞进提示词干净。
"""
from __future__ import annotations

import config
from field_guard import ColumnError, check_columns
from llm_client import chat, extract_sql, generate_sql
from semantic_guard import RATIO_FIX_PROMPT, check_semantics
from sql_guard import UnsafeSQLError, validate


class RatioError(Exception):
    """SQL 能执行但业务口径错误（不会触发数据库报错的那类）。"""


FIX_PROMPT = """你是一位 MySQL 8.0 专家。下面这条 SQL 执行报错了，请修正。

【用户的原始问题】
{question}

【上一版 SQL】
{sql}

【MySQL 报错信息】
{error}

【优先检查这几类原因】
- 列挂错了表：price / freight_value 在 dwd_order_items；is_late / is_delivered / purchase_year / late_days 在 dwd_orders；
  category_en / category_pt 在 dim_product；state / city 在 dim_customer 与 dim_seller
- WHERE 或 GROUP BY 里引用了 SELECT 的列别名（MySQL 不允许，改用 HAVING 或重复写表达式）
- 使用了 MySQL 不支持的函数（如 PERCENTILE_CONT，8.0 没有该聚合；中位数需用窗口函数或排序取中间值）
- 用了 COUNT(*) / SUM() 之外的非聚合列却没进 GROUP BY（ONLY_FULL_GROUP_BY 会报错）

【可用表结构】
{schema}

只输出修正后的 SQL，不要解释、不要 Markdown 标记：
SQL:"""


def generate_and_run(
    question: str,
    schema: str,
    version: str = "v6",
    max_fix: int = 2,
    use_ratio_guard: bool = True,
    verbose: bool = True,
):
    """生成 → 静态校验 → 执行 → 有问题则带反馈重生成，最多 max_fix 次。

    返回 (sql, df, 重写次数, 过程记录)

    verbose=False 供 Streamlit 等前端调用：过程改由界面展示，不再打印到终端。
    """
    from text2sql import run_query  # 延迟导入，避免与主流程循环引用

    def _log(msg: str) -> None:
        if verbose:
            print(msg)

    sql = generate_sql(question, schema, version)
    history: list[dict] = []

    for attempt in range(max_fix + 1):
        try:
            clean = validate(sql, config.MAX_ROWS)

            # 执行前静态字段校验：列挂错表时不用等 MySQL 报错
            problems = check_columns(clean)
            if problems:
                raise ColumnError("；".join(problems))

            # 执行前语义校验：比率粒度不一致这类错误数据库不会报，只能自己查
            if use_ratio_guard:
                ratio_problem = check_semantics(clean)
                if ratio_problem:
                    raise RatioError(ratio_problem)

            df = run_query(clean)
            return clean, df, attempt, history

        except UnsafeSQLError:
            raise  # 安全拦截不做自动修正，直接上报

        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            history.append({"try": attempt, "sql": sql, "error": err})
            if attempt == max_fix:
                # 把最后一版 SQL 挂到异常上，方便评估脚本记录失败样本
                e.last_sql = sql
                e.history = history
                raise

            if isinstance(e, RatioError):
                _log(f"  [口径重写 {attempt + 1}/{max_fix}] {str(e)[:70]}")
                prompt = RATIO_FIX_PROMPT.format(
                    question=question, sql=sql, problem=e, schema=schema
                )
            else:
                _log(f"  [自纠错 {attempt + 1}/{max_fix}] {err[:90]}")
                prompt = FIX_PROMPT.format(
                    question=question, sql=sql, error=err, schema=schema
                )
            sql = extract_sql(chat(prompt))

    raise RuntimeError("自纠错流程异常退出")
