"""批量评估：对 20 道题跑指定版本的提示词，输出明细 CSV 与执行成功率。

两种指标要分清：
- 执行成功率：SQL 能不能跑出结果（脚本自动统计）
- 口径正确率：结果对不对（人工判定，在 correct 列填 1/0 后 --score 统计）

准确率需要人工判定：跑完后打开 eval_result_*.csv，在 correct 列填 1 或 0，
再执行 `python evaluate.py --score eval_result_v4_xxx.csv` 统计。
"""
from __future__ import annotations

import argparse
import csv
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from prompt_templates import VERSIONS
from schema_context import build_schema_context
from selfcorrect import generate_and_run
from sql_guard import UnsafeSQLError

HERE = Path(__file__).parent


def run_eval(version: str, limit: int | None = None, max_fix: int = 2) -> Path:
    questions = pd.read_csv(HERE / "eval_questions.csv")
    if limit:
        questions = questions.head(limit)

    schema = build_schema_context()
    rows, ok_exec = [], 0

    for _, q in questions.iterrows():
        qid, question = int(q["id"]), str(q["question"])
        record = {"id": qid, "question": question, "version": version,
                  "sql": "", "status": "", "rows": 0, "fixes": 0,
                  "elapsed_s": 0.0, "correct": ""}
        t0 = time.time()
        try:
            sql, df, fixes, _ = generate_and_run(question, schema, version, max_fix)
            record.update(sql=sql, status="ok", rows=len(df), fixes=fixes)
            ok_exec += 1
        except UnsafeSQLError as e:
            record.update(status=f"unsafe: {e}")
        except Exception as e:
            record.update(status=f"error: {type(e).__name__}: {str(e)[:120]}")
            if getattr(e, "last_sql", ""):
                record.update(sql=e.last_sql, fixes=len(getattr(e, "history", [])))
        record["elapsed_s"] = round(time.time() - t0, 1)

        rows.append(record)
        mark = f" (纠错{record['fixes']})" if record["fixes"] else ""
        print(f"[{qid:>2}] {record['status'][:12]:<12}{mark} {question}")

    out = HERE / f"eval_result_{version}_{datetime.now():%m%d_%H%M}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    fixed = sum(1 for r in rows if r["fixes"] > 0)
    print(f"\n版本 {version}：执行成功 {ok_exec}/{total} = {ok_exec / total * 100:.0f}%"
          f"（其中 {fixed} 题靠自动纠错救回）")
    print(f"明细已保存：{out.name}")
    print("下一步：打开该文件，在 correct 列给每题填 1（口径正确）或 0（错误），"
          f"然后运行 python evaluate.py --score {out.name}")
    return out


def score(path: str) -> None:
    df = pd.read_csv(HERE / path)
    marked = df[df["correct"].notna() & (df["correct"].astype(str).str.strip() != "")]
    if marked.empty:
        print("correct 列还没填，无法统计准确率")
        return
    acc = (marked["correct"].astype(float) == 1).mean() * 100
    print(f"版本 {df['version'].iloc[0]}：已标注 {len(marked)}/{len(df)} 题，"
          f"口径正确率 {acc:.0f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v5", choices=VERSIONS)
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题（调试用）")
    ap.add_argument("--max-fix", type=int, default=2, help="每题自动纠错次数，0 表示关闭")
    ap.add_argument("--score", help="对已有结果文件统计准确率")
    args = ap.parse_args()

    if args.score:
        score(args.score)
        return
    config.check()
    run_eval(args.version, args.limit, args.max_fix)


if __name__ == "__main__":
    main()
