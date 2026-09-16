# -*- coding: utf-8 -*-
"""M7 稳定性跑批: 连续 N 轮全量 run_eval.py --no-report, 每轮立即 rejudge --backfill 留档。

用法:
    cd benchmark && python m7_run_rounds.py --rounds 5

说明: 全量 run_eval 默认会覆盖 docs/评测报告-基线.md, 本脚本固定加 --no-report,
      只落 runs/run_<时间戳>.json, 再对每轮执行 rejudge --backfill 固化最终判定。
纯标准库, 无第三方依赖。
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def newest_run():
    files = sorted((HERE / "runs").glob("run_*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main():
    ap = argparse.ArgumentParser(description="M7 稳定性连续跑批")
    ap.add_argument("--rounds", type=int, default=5, help="连续跑几轮全量(缺省 5)")
    ap.add_argument("--start", type=int, default=1, help="起始轮号标签(续跑时用, 缺省 1)")
    opts = ap.parse_args()

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    for i in range(opts.start, opts.start + opts.rounds):
        print(f"===== M7 ROUND {i} =====", flush=True)
        before = newest_run()
        r = subprocess.run([sys.executable, "run_eval.py", "--no-report"],
                           cwd=HERE, env=env)
        if r.returncode != 0:
            sys.exit(f"round {i} run_eval failed rc={r.returncode}")
        after = newest_run()
        if after is None or after == before:
            sys.exit("round 跑完后未发现新 run JSON")
        print(f"new run: {after.name}", flush=True)
        r2 = subprocess.run([sys.executable, "rejudge.py", "--backfill", str(after)],
                            cwd=HERE, env=env)
        if r2.returncode != 0:
            sys.exit(f"round {i} rejudge failed rc={r2.returncode}")
    print(f"ALL {opts.rounds} ROUNDS DONE", flush=True)


if __name__ == "__main__":
    main()
