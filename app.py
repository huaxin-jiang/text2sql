"""Streamlit 界面：把命令行版包一层网页，面试时可当场演示。

启动：
    streamlit run app.py

设计原则：界面只做展示，不掺业务逻辑。
SQL 生成、字段校验、语义检查、自动纠错全部复用 selfcorrect.generate_and_run，
这样网页和命令行的结果永远一致 —— 面试官让你换个方式跑，不会出现两套答案。
"""
from __future__ import annotations

import time
from decimal import Decimal

import pandas as pd
import streamlit as st

import config
from prompt_templates import VERSIONS
from schema_context import build_schema_context
from selfcorrect import generate_and_run
from sql_guard import UnsafeSQLError

st.set_page_config(
    page_title="Olist 数据问答 · Text-to-SQL",
    layout="wide",
    initial_sidebar_state="expanded",
)

EXAMPLES = [
    "2017 年每个月的延迟率是多少",
    "销售额最高的 10 个商品类目",
    "复购客户有多少",
    "各州的平均客单价排名前 5",
    "2017 年各月订单量的环比增长率",
    "延迟订单的平均延迟天数是多少",
]

TIME_HINTS = ("year", "month", "date", "week", "quarter", "年", "月", "日", "周", "季")


# ---------------------------------------------------------------- 工具函数
@st.cache_data(ttl=3600, show_spinner=False)
def load_schema() -> str:
    """表结构 + 业务口径。连库查 INFORMATION_SCHEMA 比较慢，缓存一小时。"""
    return build_schema_context()


def coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """MySQL 的 DECIMAL 会变成 Python Decimal，pandas 认成 object，
    不转的话图表画不出来、排序也会按字符串排。"""
    if df.empty:
        return df
    for col in df.columns:
        if df[col].dtype != object:
            continue
        vals = df[col].dropna()
        if len(vals) == 0:
            continue
        if all(isinstance(v, Decimal) for v in vals):
            df[col] = df[col].astype(float)
    return df


def looks_temporal(series: pd.Series, name: str) -> bool:
    """判断某列是否适合做折线的横轴。"""
    low = str(name).lower()
    if any(h in low for h in TIME_HINTS):
        return True
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    # 纯数字且升序，多半是月份 / 年份 / 排名
    if pd.api.types.is_numeric_dtype(series) and series.is_monotonic_increasing:
        return series.nunique() == len(series) and len(series) <= 60
    return False


def numeric_columns(df: pd.DataFrame) -> list[str]:
    return df.select_dtypes(include="number").columns.tolist()


def render_chart(df: pd.DataFrame) -> None:
    """能画就画一张，画不了就安静跳过，不硬凑。"""
    nums = numeric_columns(df)
    cats = [c for c in df.columns if c not in nums]
    if not nums or len(df) < 2 or len(df) > 100:
        return

    if cats:
        x = cats[0]
        y = nums[:3]
    else:
        x = None
        y = nums[:3]

    st.markdown("**自动图表**")
    try:
        if x is not None and looks_temporal(df[x], x):
            data = df.set_index(x)[y]
            st.line_chart(data, height=320)
        elif x is not None:
            top = df.head(15)
            st.bar_chart(top.set_index(x)[y], height=320)
        else:
            st.line_chart(df[y], height=320)
    except Exception:
        st.caption("这张结果不适合自动出图，看表格即可。")


def run_query_ui(question: str, version: str, max_fix: int, use_guard: bool) -> dict:
    t0 = time.time()
    try:
        sql, df, fixes, history = generate_and_run(
            question,
            load_schema(),
            version,
            max_fix,
            use_ratio_guard=use_guard,
            verbose=False,
        )
        return {
            "ok": True,
            "question": question,
            "sql": sql,
            "df": coerce_types(df),
            "fixes": fixes,
            "history": history,
            "elapsed": time.time() - t0,
            "version": version,
        }
    except UnsafeSQLError as e:
        return {"ok": False, "question": question, "kind": "unsafe",
                "message": str(e), "elapsed": time.time() - t0, "version": version}
    except Exception as e:
        return {
            "ok": False,
            "question": question,
            "kind": "error",
            "message": f"{type(e).__name__}: {e}",
            "last_sql": getattr(e, "last_sql", None),
            "history": getattr(e, "history", []),
            "elapsed": time.time() - t0,
            "version": version,
        }


# ---------------------------------------------------------------- 侧边栏
with st.sidebar:
    st.markdown("### 运行设置")
    version = st.selectbox(
        "提示词版本",
        VERSIONS,
        index=VERSIONS.index(config.PROMPT_VERSION) if config.PROMPT_VERSION in VERSIONS else len(VERSIONS) - 1,
        help="v1 只有基础规则，v7 加了口径约束与中位数模板。切换版本可以现场对比准确率差异。",
    )
    max_fix = st.slider("自动重写上限", 0, 3, 2,
                        help="生成失败后带着错误信息重新生成的次数。设 0 可以看到模型的原始表现。")
    use_guard = st.toggle("启用语义校验", value=True,
                          help="拦截「能跑但算错」的 SQL，例如分子分母粒度不一致。")

    st.divider()
    st.markdown("### 环境")
    st.caption(f"模型 · `{config.LLM_MODEL}`")
    st.caption(f"数据库 · `{config.DB_NAME}`")
    st.caption(f"连接 · `{config.DB_HOST}:{config.DB_PORT}`")
    st.caption(f"单次返回上限 · {config.MAX_ROWS} 行")


# ---------------------------------------------------------------- 主区
st.title("Olist 电商数据问答")
st.caption("用中文提问，自动生成 SQL 并执行。生成的 SQL 全程可见，可逐行核对。")

if "q_input" not in st.session_state:
    st.session_state.q_input = ""
if "result" not in st.session_state:
    st.session_state.result = None


def pick_example(text: str) -> None:
    st.session_state.q_input = text
    st.session_state.auto_run = True


question = st.text_input(
    "你的问题",
    key="q_input",
    placeholder="例如：2017 年每个月的延迟率是多少",
    label_visibility="collapsed",
)

st.caption("示例问题")
ex_cols = st.columns(3)
for i, ex in enumerate(EXAMPLES):
    ex_cols[i % 3].button(
        ex, key=f"ex_{i}", on_click=pick_example, args=(ex,),
        width="stretch",
    )

col_run, col_hint = st.columns([1, 4])
submitted = col_run.button("查询", type="primary", width="stretch")

auto_run = st.session_state.pop("auto_run", False)

if (submitted or auto_run) and question.strip():
    with st.spinner("正在生成 SQL 并执行…"):
        st.session_state.result = run_query_ui(question.strip(), version, max_fix, use_guard)

# ---------------------------------------------------------------- 结果
res = st.session_state.result

if res:
    st.divider()
    st.markdown(f"#### {res['question']}")

    if not res["ok"]:
        if res["kind"] == "unsafe":
            st.error(f"**安全拦截** — {res['message']}")
            st.caption("写操作、多语句、无 LIMIT 的危险查询会被直接拒绝，不参与自动重写。")
        else:
            st.error(f"**未能生成可用的 SQL** — {res['message']}")
            st.caption("系统选择报错而不是给一个看着正常的数字。失败原因见下方过程记录。")

        if res.get("history"):
            with st.expander(f"重写过程（{len(res['history'])} 次尝试）", expanded=True):
                for h in res["history"]:
                    st.markdown(f"**第 {h['try'] + 1} 次** · {h['error'][:200]}")
                    if h.get("sql"):
                        st.code(h["sql"].strip(), language="sql")
        if res.get("last_sql"):
            with st.expander("最后一版 SQL（未通过校验）"):
                st.code(res["last_sql"].strip(), language="sql")
        st.stop()

    df = res["df"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("耗时", f"{res['elapsed']:.1f} s")
    m2.metric("自动重写", f"{res['fixes']} 次" if res["fixes"] else "一次通过")
    m3.metric("返回行数", f"{len(df)}")
    m4.metric("提示词版本", res["version"])

    st.markdown("**生成的 SQL**")
    st.code(res["sql"].strip(), language="sql")

    if df.empty:
        st.warning("查询执行成功，但没有返回数据。可能是过滤条件过严。")
    else:
        st.markdown("**查询结果**")
        st.dataframe(df, width="stretch", height=min(420, 56 + 35 * len(df)))
        render_chart(df)

    if res["history"]:
        with st.expander(f"重写过程（{len(res['history'])} 次拦截）"):
            st.caption("这些是模型第一次没写对的地方，被拦下后带着原因重新生成。")
            for h in res["history"]:
                st.markdown(f"**第 {h['try'] + 1} 次** · {h['error'][:220]}")
                if h.get("sql"):
                    st.code(h["sql"].strip(), language="sql")
    else:
        st.caption("本次一次生成通过，没有触发任何校验拦截。")
