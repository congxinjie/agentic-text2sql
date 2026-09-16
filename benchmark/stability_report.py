# -*- coding: utf-8 -*-
"""M7 稳定性复算工具: 读 N 个 run JSON(建议 rejudge --backfill 后的), 输出:
  1) 每轮 e2e 指标 + 时延 P50/P90;
  2) 40 题 × N 轮的逐题稳定率表(非 N/N 的题附失败形态);
  3) 非 N/N 题详情(判定位/表头/行数/值/SQL/错误)。

用法:
    cd benchmark && python stability_report.py runs/run_<a>.json runs/run_<b>.json ...

口径: 每题每轮通过 = exec_ok ∧ result_ok ∧ caliber_ok ∧ ¬hallucination
      (与 run_eval.py 运行时逐题 OK 口径、average_runs.py 双轮均达标口径一致)。
纯标准库, 无第三方依赖。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_eval as E  # noqa: E402


def ok(rec):
    return bool(rec.get("exec_ok")) and bool(rec.get("result_ok")) \
        and bool(rec.get("caliber_ok")) and not bool(rec.get("hallucination"))


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def short_sql(sql, n=180):
    s = (sql or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def fail_bits(rec):
    bits = []
    if not rec.get("exec_ok"):
        bits.append("exec✗")
    elif not rec.get("result_ok"):
        bits.append("result✗")
    elif not rec.get("caliber_ok"):
        bits.append("caliber✗")
    if rec.get("hallucination"):
        bits.append("幻觉✗")
    return "+".join(bits) or "?"


def fail_form(rec):
    """失败形态: 判定位 + 表头/行数/值/SQL 摘要, 供归属判断。"""
    parts = [fail_bits(rec)]
    if not rec.get("exec_ok"):
        parts.append(f"error={str(rec.get('error') or '')[:120] or '无SQL/未执行'}")
    else:
        g = rec.get("gold_answer") or {}
        gh, gr = g.get("headers", []), g.get("rows", [])
        ah, ar = rec.get("headers", []), rec.get("rows") or []
        if not rec.get("result_ok"):
            parts.append(f"表头 got={ah} gold={gh}")
            parts.append(f"行数 got={len(ar)} gold={len(gr)}")
            parts.append(f"got行0={ar[0] if ar else None}")
            parts.append(f"gold行0={gr[0] if gr else None}")
        if not rec.get("caliber_ok"):
            parts.append("缺期望表/时间")
        if rec.get("hallucination"):
            parts.append(";".join(rec.get("halluc_detail", []))[:160])
    parts.append(f"SQL={short_sql(rec.get('sql'))}")
    return " | ".join(parts)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    files = [Path(a) for a in sys.argv[1:]]
    runs = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        data["_file"] = f.name
        runs.append(data)

    print(f"# M7 稳定性复算: {len(runs)} 轮 × {len(runs[0]['records'])} 题")
    print()
    print("run files:", ", ".join(d["_file"] for d in runs))
    print()

    # 每轮指标
    print("## 每轮指标")
    print("| 轮次 | run 文件 | e2e | exec | result | caliber | 幻觉 | P50 | P90 |")
    print("|---|---|---|---|---|---|---|---|---|")
    all_times = []
    for i, d in enumerate(runs, 1):
        recs = d["records"]
        n = len(recs)
        ex = sum(1 for r in recs if r["exec_ok"])
        rr = sum(1 for r in recs if r["exec_ok"] and r["result_ok"])
        cr = sum(1 for r in recs if r["exec_ok"] and r["caliber_ok"])
        e2 = sum(1 for r in recs if ok(r))
        hal = sum(1 for r in recs if r["hallucination"])
        times = sorted(r["elapsed_ms"] for r in recs)
        all_times.extend(times)
        p50 = E.percentile(times, 0.5)
        p90 = E.percentile(times, 0.9)
        print(f"| R{i} | {d['_file']} | {pct(e2, n)}% | {pct(ex, n)}% | {pct(rr, n)}% | "
              f"{pct(cr, n)}% | {pct(hal, n)}% | {p50/1000:.1f}s | {p90/1000:.1f}s |")
    print()

    # 汇总时延(池化所有调用)
    all_times.sort()
    print(f"## 时延汇总(池化 {len(all_times)} 次问答调用 = 40 题 × {len(runs)} 轮)")
    print(f"- P50: {E.percentile(all_times, 0.5)/1000:.1f}s")
    print(f"- P90: {E.percentile(all_times, 0.9)/1000:.1f}s")
    print(f"- MAX: {max(all_times)/1000:.1f}s")
    print()

    # 逐题稳定率
    ids = [r["id"] for r in runs[0]["records"]]
    print("## 逐题稳定率表")
    header = "| ID | 难度 | 问题 | 通过 | " + " | ".join(f"R{i}" for i in range(1, len(runs) + 1)) + " | 失败形态(非全过) |"
    print(header)
    print("|" + "---|" * (len(runs) + 5) + "")
    non_full = []
    stable_count = 0
    for qid in ids:
        oks = []
        for d in runs:
            rec = next(r for r in d["records"] if r["id"] == qid)
            oks.append(ok(rec))
        passed = sum(oks)
        if passed == len(runs):
            stable_count += 1
        q = next(r for r in runs[0]["records"] if r["id"] == qid)
        short_q = q["question"][:24].replace("|", "/")
        cells = " | ".join("✓" if x else "✗" for x in oks)
        if passed == len(runs):
            print(f"| {qid} | {q['difficulty']} | {short_q} | {passed}/{len(runs)} | {cells} | — |")
        else:
            fail_idx = [i for i, x in enumerate(oks) if not x]
            forms = []
            for i in fail_idx:
                d = runs[i]
                rec = next(r for r in d["records"] if r["id"] == qid)
                forms.append(f"R{i+1}:{fail_form(rec)}")
            non_full.append((qid, passed, oks, forms))
            print(f"| {qid} | {q['difficulty']} | {short_q} | {passed}/{len(runs)} | {cells} | "
                  f"{'<br>'.join(forms)[:300]} |")
    print()
    print(f"## 稳定率(每轮全过题数): {stable_count}/{len(ids)} = {pct(stable_count, len(ids))}%")
    print()

    if non_full:
        print("## 非全过题详情")
        for qid, passed, oks, forms in non_full:
            print(f"### {qid} — {passed}/{len(runs)}")
            for f in forms:
                print(f"- {f}")
            print()
    else:
        print("## 非全过题详情: 无(全部题目每轮通过)")


if __name__ == "__main__":
    main()
