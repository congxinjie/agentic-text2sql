# -*- coding: utf-8 -*-
"""M7 终检工具: 判定器函数体是否与 HEAD 逐字符一致 + 引擎是否出现券商域词。

用法:
    cd benchmark && python m7_selfcheck.py

检查项:
  1) benchmark/run_eval.py 中 def judge( 到 def pct( 区间, 与 HEAD 逐字符一致;
     期间允许修改 run_eval.py 其它区域(如 --no-report 开关), 判定器冻结不受影响。
  2) text2sql_demo/text2sql.py 不出现券商域词(与 M6.1 自检命令同一词表)。
纯标准库, 无第三方依赖。
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUN_EVAL = HERE / "run_eval.py"
ENGINE = ROOT / "text2sql_demo" / "text2sql.py"
DOMAIN_RE = re.compile(
    r"客户等级|性别|学历|职业|一级分类|二级分类|总资产|交易额|持仓市值|比亚迪|科创板")

MARK_BEGIN = "def judge("
MARK_END = "def pct("


def region(text):
    a = text.index(MARK_BEGIN)
    b = text.index(MARK_END, a)
    return text[a:b]


def main():
    rc = 0

    # 1) 判定器冻结
    head = subprocess.run(["git", "show", f"HEAD:{RUN_EVAL.relative_to(ROOT).as_posix()}"],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout
    work = RUN_EVAL.read_text(encoding="utf-8")
    if region(head) == region(work):
        print("JUDGE_REGION_IDENTICAL=YES")
    else:
        print("JUDGE_REGION_IDENTICAL=NO")
        rc = 1

    # 2) 引擎域词自检(输出同 grep -nE)
    hits = []
    for i, line in enumerate(ENGINE.read_text(encoding="utf-8").splitlines(), 1):
        if DOMAIN_RE.search(line):
            hits.append(f"{i}:{line}")
    if hits:
        print("ENGINE_DOMAIN_WORDS=HIT")
        print("\n".join(hits))
        rc = 1
    else:
        print("ENGINE_DOMAIN_WORDS=EMPTY")
    sys.exit(rc)


if __name__ == "__main__":
    main()
