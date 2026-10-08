#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8 本地演示服务(纯标准库 http.server)。

启动:
    python demo/server.py --db <库路径> [--biz-context demo/enterprise_biz.json] [--port 8000]
                          [--sample-rows 2] [--log-level INFO] [--offline]

--offline: 离线演示模式 —— 没有 LLM_API_KEY 也能起服务(界面与 /api/health 可用),
           提问会立即返回"未配置密钥"的明确错误, 不发起任何真实网络调用。

--sample-rows 控制每表拼进 schema 的样例数据行数: 默认 2(与引擎一致); 0 = 完全不把样例数据
发给外部 LLM。/api/health 会回显实际生效值。

约定:
- 只绑 127.0.0.1, 不对外网开放。
- 问答链路完全复用 text2sql_demo/text2sql.py 的 QueryAgent(演示层不新造链路)。
- 业务口径只经 --biz-context 指向的 JSON 文件注入, 演示层不内嵌任何业务口径。
- API key 只在服务端经 text2sql.load_api_key() 读取, 绝不进入前端响应。
- 数据库只读由引擎保证(mode=ro + PRAGMA query_only=ON)。

接口:
- GET /api/ask?q=...&clarify=false 与 POST /api/ask: 一次性返回完整结果(既有行为, 未变)。
- GET /api/ask/stream?q=...&clarify=false (M10): text/event-stream 流式返回, 引擎每进入/结束
  一个阶段就推一条 data: {...}(只含阶段名/状态/序号/时间戳/耗时, 不含 SQL 片段与数据行),
  最后推一条 {"type":"done","result":{...}}, 其 result 与 /api/ask 的响应逐字段相同。
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEXT2SQL_DIR = ROOT / "text2sql_demo"
sys.path.insert(0, str(TEXT2SQL_DIR))

import text2sql  # noqa: E402
from text2sql import QueryAgent  # noqa: E402

# M9: 演示层自己的轮转文件日志(与引擎同目录、不同文件, 避免多进程争抢同一个轮转文件)。
# 红线: 只记 长度/行数/表名/阶段名/耗时/异常, 不记 API 密钥与数据行内容。
LOG = text2sql.setup_logging(log_file=ROOT / "logs" / "demo_server.log",
                             level=os.environ.get("TEXT2SQL_LOG_LEVEL", "INFO"),
                             name="demo_server")


class DemoQueryAgent(QueryAgent):
    """包装 QueryAgent: 仅捕获引擎内部生成的查询计划供前端展示, 不改动任何阶段逻辑。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_plan = None

    def _plan(self, u, r):
        plan = super()._plan(u, r)
        self.last_plan = plan
        return plan

    def _replan(self, u, r, plan, issues):
        new_plan = super()._replan(u, r, plan, issues)
        self.last_plan = new_plan
        return new_plan


def resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw).expanduser()
    if not p.exists():
        alt = (base / raw).resolve()
        if alt.exists():
            return alt
    return p.resolve()


def load_biz_context_file(path: str | None):
    """从 JSON 文件注入业务口径; 不传则走引擎默认语义。

    返回 (biz_context, sql_hints, caliber_assertions, sensitive_columns, entities)。
    sensitive_columns 与 entities 由注入侧给出库内真实列名, 引擎只认机制(不硬编码券商列名/实体名)。
    """
    if not path:
        return None, None, None, None, None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return (data.get("biz_context"),
            data.get("sql_hints") or {},
            data.get("caliber_assertions") or [],
            data.get("sensitive_columns") or [],
            data.get("entities") or {})


def matched_assertions(question: str, plan: dict | None, rules: list) -> list:
    """选择与本次问答相关的口径断言(仅用于前端展示; 判定机制仍在引擎内)。"""
    text = " ".join([question or "", json.dumps(plan or {}, ensure_ascii=False)])
    hits = []
    for rule in rules:
        when = rule.get("when") or []
        if when and all(str(k) in text for k in when):
            hits.append(rule)
        oc = rule.get("output_contract")
        if isinstance(oc, dict):
            when_any = [str(k) for k in (oc.get("when_any") or [])]
            if when_any and any(k in (question or "") for k in when_any):
                hits.append(rule)
    seen, out = set(), []
    for r in hits:
        key = json.dumps(r, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(chr(34), "&quot;"))


def make_csv(headers, rows):
    def cell(v):
        s = "" if v is None else str(v)
        if any(ch in s for ch in (",", chr(34), "\n", "\r")):
            s = chr(34) + s.replace(chr(34), chr(34) * 2) + chr(34)
        return s
    lines = [",".join(cell(h) for h in headers)]
    for r in rows:
        lines.append(",".join(cell(v) for v in r))
    return "\r\n".join(lines)


def make_chart_svg(headers, rows, max_rows=20):
    try:
        if not headers or len(headers) < 2 or not rows:
            return ""
        def num(v):
            try:
                return float(str(v).replace(",", ""))
            except (TypeError, ValueError):
                return None
        idx = None
        for j in range(1, len(headers)):
            if any(num(r[j]) is not None for r in rows):
                idx = j
                break
        if idx is None:
            return ""
        data = []
        for r in rows[:max_rows]:
            v = num(r[idx])
            if v is not None:
                data.append((str(r[0]), v))
        if len(data) < 2:
            return ""
        mx = max(abs(v) for _, v in data) or 1.0
        lh = 20
        H = 8 + lh * len(data)
        parts = [f"<svg viewBox=\"0 0 540 {H}\" width=\"100%\" role=\"img\">"]
        for i, (label, v) in enumerate(data):
            y = 4 + i * lh
            w = max(2.0, 300.0 * abs(v) / mx)
            lab = label if len(label) <= 10 else label[:10] + "…"
            parts.append(f"<text x=\"0\" y=\"{y + 14}\" font-size=\"11\" fill=\"#6b7280\">{_esc(lab)}</text>")
            parts.append(f"<rect x=\"120\" y=\"{y + 3}\" width=\"{w:.1f}\" height=\"13\" rx=\"2\" fill=\"#2f54eb\" opacity=\"0.85\"/>")
            parts.append(f"<text x=\"{120 + w + 6:.1f}\" y=\"{y + 14}\" font-size=\"11\" fill=\"#1f2430\">{v:,.2f}</text>")
        parts.append("</svg>")
        return "".join(parts)
    except Exception:
        return ""


def run_ask(agent: QueryAgent, question: str, clarify: bool, on_stage=None, context=None) -> dict:
    t0 = time.time()
    orig_question = question
    if context and isinstance(context, dict) and context.get("question"):
        question = ("【上一轮问题】" + str(context.get("question")) + "\n"
                    + "【上一轮 SQL】" + str(context.get("sql") or "") + "\n"
                    + "【上一轮结论】" + str(context.get("explanation") or "") + "\n"
                    + "【本轮】" + question)
    ans = agent.run(question, clarify=clarify, on_stage=on_stage)
    elapsed_s = round(time.time() - t0, 2)
    plan = getattr(agent, "last_plan", None)
    return {
        "question": orig_question,
        "answerable": ans.answerable,
        "reject_reason": ans.reject_reason,
        "needs_clarification": ans.needs_clarification,
        "sql": ans.sql,
        "headers": ans.headers,
        "rows": ans.rows,
        "truncated": ans.truncated,
        "explanation": ans.explanation,
        "error": ans.error,
        "plan": plan,
        "intent": getattr(ans, "intent", ""),
        "entities": list(getattr(ans, "entities", []) or []),
        "query_plan": getattr(ans, "query_plan", {}) or {},
        "decomposed": bool(getattr(ans, "decomposed", False)),
        "sub_answers": list(getattr(ans, "sub_answers", []) or []),
        "entities_covered": bool(getattr(ans, "entities_covered", False)),
        "caliber_assertions": matched_assertions(
            question, plan, getattr(agent, "caliber_assertions", []) or []),
        "trace": [{"stage": e.stage, "status": e.status,
                   "detail": e.detail, "elapsed": e.elapsed}
                  for e in ans.trace.entries],
        "elapsed_s": elapsed_s,
        "model": text2sql.MODEL,
        "chart_svg": make_chart_svg(ans.headers, ans.rows),
        "csv_url": ("data:text/csv;charset=utf-8," + quote("\ufeff" + make_csv(ans.headers, ans.rows))) if ans.headers else "",
        # M10: 让流式终止事件的 result 与 POST /api/ask 的响应逐字段一致(含 ok)
        "ok": True,
    }


def run_ask_stream(agent: QueryAgent, question: str, clarify: bool, emit, context=None) -> dict:
    """M10: 跑一次问答, 边跑边把阶段进度交给 emit(纯标准库, 不加线程/队列)。

    - 每条阶段事件只含 type/seq/stage/status/ts/elapsed_s; **不带 detail**
      (detail 可能含 SQL 片段与结果值), 更不含 API 密钥与任何数据行。
    - 终止事件 {"type": "done", "result": payload} 中的 payload 由同一个 run_ask 产出,
      因此与 POST /api/ask 的响应逐字段相同。
    """
    t0 = time.time()
    seq = 0

    def _on_stage(stage: str, status: str, detail: str = ""):
        nonlocal seq
        seq += 1
        emit({"type": "stage", "seq": seq, "stage": stage, "status": status,
              "ts": round(time.time(), 3), "elapsed_s": round(time.time() - t0, 2)})

    payload = run_ask(agent, question, clarify, on_stage=_on_stage, context=context)
    done = {"type": "done", "seq": seq + 1, "ts": round(time.time(), 3), "result": payload}
    emit(done)
    return done


class Handler(BaseHTTPRequestHandler):
    server_version = "M8Demo/1.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_index(self):
        body = self.server.index_html
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ask(self, question: str, clarify: bool, context=None) -> dict:
        with self.server.agent_lock:
            try:
                payload = run_ask(self.server.agent, question, clarify, context=context)
                payload["ok"] = True
                # M9: 只记长度/行数/耗时/状态, 不记问题正文与结果数据
                LOG.info("[演示问答] 问题长度=%d clarify=%s 耗时=%.2fs 可答=%s 结果行数=%d 有错误=%s",
                         len(question), clarify, payload["elapsed_s"], payload["answerable"],
                         len(payload.get("rows") or []), bool(payload.get("error")))
                return payload
            except Exception as e:  # 服务端兜底: 前端只拿到可读错误, 不泄漏密钥
                LOG.error("[演示问答异常] 问题长度=%d 类型=%s 消息=%s",
                          len(question), type(e).__name__, str(e)[:160])
                traceback.print_exc(file=sys.stderr)
                return {"ok": False, "question": question,
                        "error": f"{type(e).__name__}: {e}"}

    # ---- M10: SSE 流式问答(text/event-stream, 纯标准库) ----
    def _sse_open(self):
        """发送 text/event-stream 响应头(HTTP/1.1 + chunked, 才能边跑边推)。"""
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # 万一流被中断, 也别让浏览器自动重发(重发会再花一次 LLM 调用); 前端收到 done 后主动 close()
        self._sse_write_text("retry: 3600000\n\n")

    def _sse_write_text(self, text: str):
        """按 chunked 编码写一段 SSE 文本并立即 flush(保证"边跑边推")。"""
        data = text.encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _sse_send(self, ev: dict):
        self._sse_write_text("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n")

    def _ask_stream(self, question: str, clarify: bool, context=None):
        """流式问答: 每个阶段到达即推一条事件, 最后推终止事件(内含与 /api/ask 相同的完整结果)。"""
        self._sse_open()
        try:
            with self.server.agent_lock:
                done = run_ask_stream(self.server.agent, question, clarify, self._sse_send, context=context)
            payload = done["result"]
            # M9 红线: 只记长度/行数/耗时/状态, 不记问题正文与结果数据
            LOG.info("[演示流式问答] 问题长度=%d clarify=%s 耗时=%.2fs 阶段事件数=%d 可答=%s 结果行数=%d 有错误=%s",
                     len(question), clarify, payload.get("elapsed_s") or 0, done["seq"] - 1,
                     payload.get("answerable"), len(payload.get("rows") or []),
                     bool(payload.get("error")))
        except Exception as e:  # 服务端兜底: 前端只拿到可读错误, 不泄漏密钥
            LOG.error("[演示流式异常] 问题长度=%d 类型=%s 消息=%s",
                      len(question), type(e).__name__, str(e)[:160])
            traceback.print_exc(file=sys.stderr)
            try:
                self._sse_send({"type": "done", "ts": round(time.time(), 3),
                                "result": {"ok": False, "question": question,
                                           "error": f"{type(e).__name__}: {e}"}})
            except Exception:
                pass
        # 不写 chunked 终止符: 连接留给前端收到 done 后 close(), 避免 EventSource 自动重连触发二次问答

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send_index()
        if parsed.path == "/api/health":
            return self._send_json({
                "ok": True,
                "model": text2sql.MODEL,
                "db": str(self.server.db_path),
                "biz_context_file": self.server.biz_context_file,
                "readonly": True,
                "sample_rows": self.server.agent.sample_rows,
                "sensitive_columns": self.server.agent.sensitive_columns,
            })
        if parsed.path in ("/api/ask", "/api/ask/stream"):
            qs = parse_qs(parsed.query)
            question = (qs.get("q") or [""])[0].strip()
            if not question:
                return self._send_json({"ok": False, "error": "缺少问题参数 q"}, 400)
            clarify = (qs.get("clarify") or ["false"])[0].lower() == "true"
            ctx_raw = (qs.get("context") or [""])[0]
            context = None
            if ctx_raw:
                try:
                    context = json.loads(ctx_raw)
                except ValueError:
                    context = None
            if parsed.path == "/api/ask/stream":
                return self._ask_stream(question, clarify, context)
            return self._send_json(self._ask(question, clarify, context))
        return self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/ask":
            return self._send_json({"ok": False, "error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                return self._send_json({"ok": False, "error": "请求体为空或过大"}, 400)
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            question = str(data.get("question") or "").strip()
            if not question:
                return self._send_json({"ok": False, "error": "缺少 question 字段"}, 400)
            clarify = bool(data.get("clarify", False))
            context = data.get("context") if isinstance(data.get("context"), dict) else None
        except (ValueError, UnicodeDecodeError) as e:
            return self._send_json({"ok": False, "error": f"请求解析失败: {e}"}, 400)
        return self._send_json(self._ask(question, clarify, context))

    def log_message(self, fmt, *args):
        sys.stderr.write("[demo] " + fmt % args + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="M8 本地演示服务(纯标准库, 只绑 127.0.0.1, 只读库)")
    ap.add_argument("--db", required=True, help="SQLite 库路径")
    ap.add_argument("--biz-context", default=None,
                    help="业务口径 JSON 文件(如 demo/enterprise_biz.json), 不传走引擎默认语义")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sample-rows", type=int, default=2,
                    help="每表拼进 schema 的样例数据行数(默认 2; 0 = 完全不把样例数据发给外部 LLM)")
    ap.add_argument("--offline", action="store_true",
                    help="离线演示模式: 无 LLM_API_KEY 也能起服务(仅界面/健康检查; 提问立即报错)")
    ap.add_argument("--log-level", default=os.environ.get("TEXT2SQL_LOG_LEVEL", "INFO"),
                    help="日志级别 DEBUG/INFO/WARNING/ERROR(默认 INFO)")
    args = ap.parse_args(argv)
    if args.sample_rows < 0:
        ap.error("--sample-rows 不能为负数(0 = 不发送样例数据)")
    # M9: 按命令行级别重启演示层日志 handler
    text2sql.setup_logging(log_file=ROOT / "logs" / "demo_server.log",
                           level=args.log_level, name="demo_server")

    db_path = resolve_path(args.db, ROOT)
    if not db_path.exists():
        ap.error(f"数据库不存在: {args.db}")
    db_path = db_path.resolve()

    biz_file = None
    if args.biz_context:
        biz_file = str(resolve_path(args.biz_context, ROOT).resolve())
    biz_context, sql_hints, caliber_assertions, sensitive_columns, entities = load_biz_context_file(biz_file)

    try:
        api_key = text2sql.load_api_key()
    except SystemExit:
        if not args.offline:
            raise
        api_key = "offline-demo-key"
        # 离线演示: 不发起任何真实 LLM 调用, 提问时立即返回明确错误(不联网、不重试)。
        def _offline_llm(*_a, **_k):
            raise RuntimeError("离线演示模式未配置 LLM_API_KEY, 无法生成 SQL; "
                               "请设置 LLM_API_KEY 后重启服务。")
        text2sql.llm_chat = _offline_llm
        text2sql.llm_json = _offline_llm
        print("[离线演示] 未配置 LLM_API_KEY: 仅提供界面与 /api/health, 提问会明确报错。", flush=True)
    agent = DemoQueryAgent(str(db_path), api_key, verbose=False,
                           biz_context=biz_context,
                           sql_hints=sql_hints,
                           caliber_assertions=caliber_assertions,
                           sensitive_columns=sensitive_columns,
                           entities=entities,
                           sample_rows=args.sample_rows)

    index_html = (HERE / "index.html").read_bytes()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.agent = agent
    server.agent_lock = threading.Lock()
    server.db_path = db_path
    server.biz_context_file = biz_file
    server.index_html = index_html

    print(f"智能问数演示服务已启动: http://127.0.0.1:{args.port}", flush=True)
    print(f"库: {db_path} (只读, mode=ro + query_only)", flush=True)
    print(f"模型: {text2sql.MODEL}", flush=True)
    print(f"业务口径: {biz_file or '未注入(引擎默认语义)'}", flush=True)
    print(f"样例数据行数: {args.sample_rows}(0 = 不把样例数据发给外部 LLM)", flush=True)
    print(f"受限字段: {sensitive_columns or '无'}(样例值不外发/查询结果掩码)", flush=True)
    print(f"日志: {ROOT / 'logs' / 'demo_server.log'}(级别 {args.log_level})", flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    LOG.info("演示服务启动: 库=%s 端口=%d 模型=%s 样例数据行数=%d 业务口径=%s",
             db_path.name, args.port, text2sql.MODEL, args.sample_rows,
             biz_file or "未注入(引擎默认语义)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止服务。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
