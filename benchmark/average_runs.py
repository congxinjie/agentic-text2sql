# -*- coding: utf-8 -*-
"""双跑平均: 加载两个 run JSON, 输出平均指标与逐题平均判定。"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_eval  # noqa: E402


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def main():
    files = [Path(a) for a in sys.argv[1:]]
    runs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    recs = [r["records"] for r in runs]
    ids = [r["id"] for r in recs[0]]
    print("run files:", [f.name for f in files])

    def avg_stats(diff=None):
        rows = []
        for rec_list in recs:
            sel = [r for r in rec_list if (diff is None or r["difficulty"] == diff)]
            n = len(sel)
            ex = sum(1 for r in sel if r["exec_ok"])
            rr = sum(1 for r in sel if r["exec_ok"] and r["result_ok"])
            cr = sum(1 for r in sel if r["exec_ok"] and r["caliber_ok"])
            e2 = sum(1 for r in sel if r["exec_ok"] and r["result_ok"] and r["caliber_ok"])
            hal = sum(1 for r in sel if r["hallucination"])
            times = sorted(r["elapsed_ms"] for r in sel)
            rows.append({"n": n, "exec": pct(ex, n), "res": pct(rr, n), "cal": pct(cr, n),
                         "e2e": pct(e2, n), "hal": pct(hal, n),
                         "p50": run_eval.percentile(times, 0.5),
                         "p90": run_eval.percentile(times, 0.9)})
        out = {k: round(sum(x[k] for x in rows) / len(rows), 1) for k in ("exec", "res", "cal", "e2e", "hal")}
        out["p50_ms"] = int(sum(x["p50"] for x in rows) / len(rows))
        out["p90_ms"] = int(sum(x["p90"] for x in rows) / len(rows))
        out["n"] = rows[0]["n"]
        return out

    for d in ("simple", "medium", "complex"):
        s = avg_stats(d)
        print(f"{d}: n={s['n']} exec={s['exec']} res={s['res']} cal={s['cal']} e2e={s['e2e']} hal={s['hal']} p50={s['p50_ms']}ms p90={s['p90_ms']}ms")
    s = avg_stats(None)
    print(f"ALL: exec={s['exec']} res={s['res']} cal={s['cal']} e2e={s['e2e']} hal={s['hal']} p50={s['p50_ms']}ms p90={s['p90_ms']}ms")

    # 逐题: 两轮均达标视为稳定达标
    stable_ok = 0
    for qid in ids:
        oks = []
        for rec_list in recs:
            r = next(x for x in rec_list if x["id"] == qid)
            oks.append(r["exec_ok"] and r["result_ok"] and r["caliber_ok"] and not r["hallucination"])
        if all(oks):
            stable_ok += 1
        elif any(oks):
            print(f"  波动题 {qid}: {oks}")
    print(f"双轮均达标: {stable_ok}/{len(ids)}")


if __name__ == "__main__":
    main()
