# -*- coding: utf-8 -*-
"""M10 SSE 流式接口验收(真实 HTTP + 真实 LLM, 纯标准库)。

用法:
    python benchmark/sse_stream_check.py [--db <库路径>] [--biz-context <JSON>]
                                         [--port 0] [--question "..."]

做四件事并打印:
1. 用 Python 逐行读 SSE 流, 打印每条事件的到达时刻(相对首字节), 断言:
   首条事件远早于总耗时, 且阶段事件数 >= 3;
2. 取出终止事件的 result, 与 POST /api/ask 对同一问题的响应逐字段对比
   (字段集合必须一致; 值差异逐项列出 —— LLM 两次调用本身非严格确定, 如实报告);
3. 把整段 SSE 原文 grep `sk-[A-Za-z0-9]{20,}` 与 `C0000000` → 必须无命中;
4. POST /api/ask 仍可用(既有行为未变)。
输出: benchmark/runs/sse_stream_check_<ts>.json
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUNS = HERE / "runs"
DEFAULT_DB = ROOT / "Agentic智能问数在客户营销场景的应用数据集" / "enterprise.db"
DEFAULT_BIZ = ROOT / "demo" / "enterprise_biz.json"
DEFAULT_Q = "各客户等级分别有多少客户?"
SECRET_PATTERNS = [r"sk-[A-Za-z0-9]{20,}", r"C0000000"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(db: Path, biz: Path | None, port: int):
    cmd = [sys.executable, str(ROOT / "demo" / "server.py"), "--db", str(db), "--port", str(port)]
    if biz:
        cmd += ["--biz-context", str(biz)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=5)
            raise RuntimeError(f"服务启动失败:\n{out}\n{err}")
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return proc, base
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("服务 15s 内未就绪")


def post_ask(base: str, question: str, clarify: bool = False) -> dict:
    body = json.dumps({"question": question, "clarify": clarify}).encode("utf-8")
    req = urllib.request.Request(base + "/api/ask", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_sse(base: str, question: str, clarify: bool = False, timeout: float = 180.0):
    """逐行读 SSE, 返回 (事件列表, 每条事件到达时刻, 原始文本, 总耗时秒)。"""
    from urllib.parse import quote
    url = f"{base}/api/ask/stream?q={quote(question)}&clarify={'true' if clarify else 'false'}"
    events, times, raw_lines = [], [], []
    t0 = time.time()
    resp = urllib.request.urlopen(url, timeout=timeout)
    try:
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            raw_lines.append(text)
            if text.startswith("data:"):
                payload = text[5:].strip()
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                events.append(ev)
                times.append(round(time.time() - t0, 3))
                if ev.get("type") == "done":
                    break
    finally:
        resp.close()
    total = round(time.time() - t0, 3)
    return events, times, "\n".join(raw_lines), total


def main():
    ap = argparse.ArgumentParser(description="M10 SSE 流式接口验收")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--biz-context", default=str(DEFAULT_BIZ))
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--question", default=DEFAULT_Q)
    args = ap.parse_args()

    db = Path(args.db)
    biz = Path(args.biz_context) if args.biz_context else None
    port = args.port or free_port()
    fails = []

    proc, base = start_server(db, biz, port)
    try:
        print(f"== M10 SSE 流式接口验收 ==\n库: {db.name}  业务口径: {biz.name if biz else '未注入'}")
        print(f"问题: {args.question}\n服务: {base}\n")

        # 1) 逐行读 SSE, 打印每条事件的到达时刻
        events, times, raw, sse_total = read_sse(base, args.question)
        stage_events = [e for e in events if e.get("type") == "stage"]
        done_events = [e for e in events if e.get("type") == "done"]
        print(f"[1] SSE 逐行读取: 共 {len(events)} 条事件(阶段 {len(stage_events)} + 终止 {len(done_events)}), "
              f"总耗时 {sse_total}s")
        for i, (ev, t) in enumerate(zip(events, times), 1):
            if ev.get("type") == "stage":
                print(f"    [+{t:6.3f}s] #{ev.get('seq'):>2} stage={ev.get('stage')} "
                      f"status={ev.get('status')} ts={ev.get('ts')} elapsed_s={ev.get('elapsed_s')}")
            else:
                print(f"    [+{t:6.3f}s] #{ev.get('seq'):>2} type=done (result 字段: "
                      f"{', '.join(sorted((ev.get('result') or {}).keys()))})")
        first = times[0] if times else None
        # 首条事件必须远早于总耗时(小于总耗时的 1/2), 且阶段事件 >= 3
        ok1 = bool(first is not None and stage_events and len(stage_events) >= 3
                   and first <= max(0.5, sse_total / 2))
        print(f"    {'PASS' if ok1 else 'FAIL'} 首条事件 +{first}s, 总耗时 {sse_total}s "
              f"(首条 <= max(0.5, 总耗时/2)); 阶段事件数 {len(stage_events)} >= 3")
        if not ok1:
            fails.append("首条事件不够早或阶段事件不足 3")
        if done_events:
            done_result = done_events[-1].get("result") or {}
        else:
            done_result = {}
            fails.append("未收到终止事件")

        # 2) 终止事件 result 与 POST /api/ask 对比
        post = post_ask(base, args.question)
        keys_sse, keys_post = set(done_result), set(post)
        print(f"\n[2] 终止事件 result vs POST /api/ask")
        print(f"    SSE result 字段: {sorted(keys_sse)}")
        print(f"    POST   字段:     {sorted(keys_post)}")
        same_keys = keys_sse == keys_post
        print(f"    {'PASS' if same_keys else 'FAIL'} 字段集合完全一致"
              + ("" if same_keys else f"; 差集 SSE-POST={sorted(keys_sse - keys_post)} POST-SSE={sorted(keys_post - keys_sse)}"))
        if not same_keys:
            fails.append("终止事件字段集合与 POST 不一致")
        diff = []
        for k in sorted(keys_sse & keys_post):
            if k == "elapsed_s":
                continue  # 两次独立运行各自计时, 天然不同
            if done_result[k] != post[k]:
                diff.append(k)
                print(f"    [差异] {k}:\n      SSE : {json.dumps(done_result[k], ensure_ascii=False)[:300]}"
                      f"\n      POST: {json.dumps(post[k], ensure_ascii=False)[:300]}")
        print(f"    {'PASS' if not diff else '注意'} 除 elapsed_s 外的字段: "
              + ("全部逐字段一致" if not diff else f"有差异 {diff}(两次独立 LLM 调用所致, 需人工判断)"))
        print(f"    两次运行耗时: SSE elapsed_s={done_result.get('elapsed_s')}s / POST elapsed_s={post.get('elapsed_s')}s")

        # 3) 安全 grep: 整段 SSE 不得含密钥与客户号前缀
        print(f"\n[3] SSE 原文安全 grep (长度 {len(raw)} 字符)")
        for pat in SECRET_PATTERNS:
            hits = re.findall(pat, raw)
            print(f"    {'PASS' if not hits else 'FAIL'} /{pat}/ 命中 {len(hits)} 处")
            if hits:
                fails.append(f"SSE 输出命中 {pat}")

        # 4) POST /api/ask 仍可用
        print(f"\n[4] POST /api/ask 仍可用: ok={post.get('ok')} 可答={post.get('answerable')} "
              f"结果行数={len(post.get('rows') or [])}")
        if not post.get("ok"):
            fails.append("POST /api/ask 不可用")

        RUNS.mkdir(exist_ok=True)
        out = RUNS / f"sse_stream_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "question": args.question, "db": db.name,
            "sse_events": events, "sse_event_times": times, "sse_total_s": sse_total,
            "post_result_keys": sorted(keys_post), "sse_result_keys": sorted(keys_sse),
            "value_diffs_excluding_elapsed": diff,
            "fails": fails,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n明细已写: {out}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("")
    if fails:
        print(f"验收未通过: {fails}")
        sys.exit(1)
    print("验收通过。")


if __name__ == "__main__":
    main()
