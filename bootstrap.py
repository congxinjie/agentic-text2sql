#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评委一键引导: 环境自检 -> 自动补全 -> 工程自证 -> 输入并验证 API Key -> 打开演示页面。

用法(任选其一):
    双击 bootstrap.command (macOS) / bootstrap.cmd (Windows) / bootstrap.sh (Linux)
    终端: python3 bootstrap.py            # 旧解释器也能跑, 它会自己去找 3.10+

设计约束:
  * 只用标准库(项目铁律: 引擎零第三方依赖);
  * 本文件刻意保持 Python 3.8 兼容 —— 它必须在"版本不对的旧解释器"上也能跑起来,
    才能给出可读提示、自动寻找 3.10+, 并在你同意后引导安装;
    真正干活的子进程(ci_check / create_db / demo server)一律用找到的 3.10+ 解释器启动;
  * 绝不打印、绝不写日志地保存 API Key —— 只写入 text2sql_demo/.env.local(已被 .gitignore 排除)。
"""

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ============================ 常量 ============================
REPO_ROOT = Path(__file__).resolve().parent
DATASET_DIR = REPO_ROOT / "Agentic智能问数在客户营销场景的应用数据集"
DB_MAIN = DATASET_DIR / "enterprise.db"
DB_DEMO = REPO_ROOT / "customer_marketing_db" / "marketing.db"
ENV_FILE = REPO_ROOT / "text2sql_demo" / ".env.local"
BIZ_CONTEXT = "demo/enterprise_biz.json"
SHA256SUMS = REPO_ROOT / "SHA256SUMS"

GH_REPO = "congxinjie/agentic-text2sql"
GH_TAG = "dataset-v1"
DB_ASSET = "enterprise.db"
DB_SIZE_HINT = "约 88MB"
DB_URL = "https://github.com/{repo}/releases/download/{tag}/{name}".format(
    repo=GH_REPO, tag=GH_TAG, name=DB_ASSET)

MIN_PY = (3, 10)
PREFERRED_PY = (3, 12)

# enterprise.db 内容校验(与 scripts/fetch_dataset.sh 同源): 表名 -> 期望行数
DB_EXPECT = {
    "dim_branch": 312,
    "dim_public": 155,
    "dim_product": 334694,
    "ads_cust_info_d": 500,
    "dwd_cust_hold_d": 408150,
    "dwd_cust_tran_d": 39060,
    "dws_cust_aset_d": 43684,
    "dws_cust_fin_d": 4892,
}
DB_EXPECT_INDEXES = 7

SMOKE_QUESTION = "各客户等级分别有多少客户?"


# ============================ 终端输出 ============================
def _enable_windows_ansi():
    """Windows 10+ 打开 VT 转义支持, 让颜色可用; 失败就静默降级为无色。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def _setup_stdout():
    """统一 UTF-8 输出, 避免 Windows cp936 控制台打印中文/符号时报 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


COLOR = False


def _c(code, text):
    if not COLOR:
        return text
    return "\033[" + code + "m" + text + "\033[0m"


def say(msg=""):
    print(msg, flush=True)


def rule(char="-", width=68):
    say(_c("90", char * width))


def title(text):
    say("")
    say(_c("1;36", text))
    rule("=")


def step(idx, total, text):
    say("")
    say(_c("1;36", "[{}/{}] {}".format(idx, total, text)))
    rule()


def ok(msg):
    say("  " + _c("32", "[OK]") + " " + msg)


def warn(msg):
    say("  " + _c("33", "[!] ") + " " + msg)


def bad(msg):
    say("  " + _c("31", "[X] ") + " " + msg)


def info(msg):
    say("  " + _c("90", "[-] ") + msg)


def ask(prompt, default=True, auto=False):
    if auto:
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            raw = input(_c("1", prompt) + suffix).strip().lower()
        except EOFError:
            say("")
            return default
        if not raw:
            return default
        if raw in ("y", "yes", "是", "好", "1"):
            return True
        if raw in ("n", "no", "否", "不", "0"):
            return False


# ============================ 子进程 ============================
def run(cmd, cwd=None, timeout=None, quiet=False):
    """跑一个命令, 返回 (returncode, stdout+stderr 文本)。不做 shell 展开。"""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        text = proc.stdout.decode("utf-8", "replace")
        if not quiet and text.strip():
            say(text.rstrip())
        return proc.returncode, text
    except FileNotFoundError:
        return 127, "未找到命令: {}".format(cmd[0])
    except subprocess.TimeoutExpired:
        return 124, "命令超时({}s): {}".format(timeout, " ".join(cmd))


# ============================ 1. Python 环境 ============================
def _probe_python(cmd):
    """探测一个解释器候选, 通过则返回 (cmd列表, 版本元组, 版本字符串)。"""
    code = "import sys;print('%d %d %d' % sys.version_info[:3])"
    try:
        proc = subprocess.run(cmd + ["-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        parts = proc.stdout.decode("utf-8", "replace").strip().split()
        ver = (int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None
    return (cmd, ver, ".".join(str(x) for x in ver))


def _python_candidates():
    cands = []
    override = os.environ.get("TEXT2SQL_PYTHON")
    if override:
        cands.append([override])
    if sys.executable:
        cands.append([sys.executable])

    if os.name == "nt":
        for v in ("3.14", "3.13", "3.12", "3.11", "3.10"):
            cands.append(["py", "-" + v])
        cands.append(["py", "-3"])
        for v in ("314", "313", "312", "311", "310"):
            cands.append(["python" + v])
        cands.append(["python3"])
        cands.append(["python"])
        # PATH 里没有时, 再扫常见安装目录(Windows 安装器默认勾选"不加入 PATH")
        roots = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "Programs" / "Python")
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                roots.append(Path(base))
        for root in roots:
            if not root.exists():
                continue
            try:
                for child in sorted(root.iterdir(), reverse=True):
                    exe = child / "python.exe"
                    if exe.exists():
                        cands.append([str(exe)])
            except OSError:
                pass
    else:
        for v in ("3.14", "3.13", "3.12", "3.11", "3.10"):
            cands.append(["python" + v])
        cands.append(["python3"])
        cands.append(["python"])
        for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                  "/usr/bin/python3"):
            if os.path.exists(p):
                cands.append([p])

    seen = set()
    uniq = []
    for c in cands:
        key = tuple(c)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def _print_install_guide():
    system = platform.system()
    say("")
    warn("没有找到 Python {}+ 。请先安装, 然后重新运行本向导:".format(
        ".".join(str(x) for x in MIN_PY)))
    say("")
    if system == "Darwin":
        say("    macOS  (任选其一)")
        say("      1) 官网安装包: https://www.python.org/downloads/macos/")
        say("      2) 已装 Homebrew:  brew install python@3.12")
    elif system == "Windows":
        say("    Windows(任选其一)")
        say("      1) 官网安装包: https://www.python.org/downloads/windows/")
        say("         ★ 安装时务必勾选 “Add python.exe to PATH”")
        say("      2) 已装 winget:   winget install -e --id Python.Python.3.12")
    else:
        say("    Linux(任选其一)")
        say("      Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv")
        say("      Fedora/RHEL:    sudo dnf install python3.12")
        say("      Arch:           sudo pacman -S python")
    say("")
    info("macOS 自带 python3 通常是 3.9, 不满足要求, 需要单独安装 3.10+ 。")


def _try_auto_install(auto):
    """在你同意后, 尝试用系统包管理器装 Python。返回新解释器候选列表。"""
    system = platform.system()
    if system == "Darwin" and shutil.which("brew"):
        if ask("检测到 Homebrew, 是否现在执行 `brew install python@3.12` ?", default=True, auto=auto):
            info("正在执行 brew install python@3.12 (可能需要几分钟) ...")
            run(["brew", "install", "python@3.12"], timeout=1800)
            return _find_python()
    if system == "Windows" and shutil.which("winget"):
        if ask("检测到 winget, 是否现在执行 `winget install Python.Python.3.12` ?", default=True, auto=auto):
            info("正在执行 winget install (可能需要几分钟) ...")
            run(["winget", "install", "-e", "--id", "Python.Python.3.12",
                 "--accept-package-agreements", "--accept-source-agreements"],
                timeout=1800)
            return _find_python()
    if system == "Linux":
        info("Linux 发行版差异较大, 为避免误改系统, 不自动安装; 请按上面的命令手动安装。")
    return None


def _find_python():
    for cand in _python_candidates():
        found = _probe_python(cand)
        if found and found[1] >= MIN_PY:
            return found
    return None


def detect_python(auto):
    step(1, 6, "环境检测与补全 —— Python 解释器")
    info("当前向导运行在: Python {} ({})".format(
        sys.version.split()[0], sys.executable or "未知"))

    found = _find_python()
    if found is None:
        # 退而求其次: 报告"找得到但版本太低"的, 好让用户知道问题出在哪
        seen_old = []
        for cand in _python_candidates():
            got = _probe_python(cand)
            if got and got[1] < MIN_PY:
                seen_old.append("{} -> {}".format(" ".join(got[0]), got[2]))
        if seen_old:
            warn("找到的解释器版本都太低: " + "; ".join(sorted(set(seen_old))[:4]))
        _print_install_guide()
        if auto:
            bad("自动模式下不安装环境; 请手动安装后重试。")
            return None
        found = _try_auto_install(auto)
        if found is None:
            bad("仍未找到可用的 Python {}+, 向导退出。".format(
                ".".join(str(x) for x in MIN_PY)))
            return None

    cmd, ver, ver_str = found
    ok("使用解释器: {} (Python {})".format(" ".join(cmd), ver_str))
    if ver < PREFERRED_PY:
        info("提示: Python {}+ 会更快; 当前 {} 在支持范围内, 可以继续。".format(
            ".".join(str(x) for x in PREFERRED_PY), ver_str))
    return cmd


# ============================ 2. 数据与演示库 ============================
def _sha256_of(path, block=1024 * 1024):
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        while True:
            chunk = fh.read(block)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _expected_sha(rel_path):
    """从仓库根 SHA256SUMS 里取期望 sha256(不依赖 sed/sha256sum, Windows 也能用)。"""
    if not SHA256SUMS.exists():
        return None
    try:
        for line in SHA256SUMS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or "  " not in line:
                continue
            digest, name = line.split("  ", 1)
            if name.strip() == rel_path:
                return digest.strip().lower()
    except Exception:
        return None
    return None


def validate_main_db(path):
    """校验 enterprise.db: 8 张表行数 + 索引数。返回 (ok, 摘要文本)。"""
    import sqlite3
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    except sqlite3.Error as e:
        return False, "无法打开: {}".format(e)
    try:
        bad_tables = []
        total = 0
        for table, want in sorted(DB_EXPECT.items()):
            try:
                got = conn.execute(
                    'select count(*) from "{}"'.format(table)).fetchone()[0]
            except sqlite3.Error as e:
                bad_tables.append("{}: 查询失败 {}".format(table, e))
                continue
            total += got
            if got != want:
                bad_tables.append("{}: {} (期望 {})".format(table, got, want))
        idx = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='index' "
            "and name not like 'sqlite_%'")]
        if bad_tables:
            return False, "行数不符 -> " + "; ".join(bad_tables[:4])
        if len(idx) != DB_EXPECT_INDEXES:
            return False, "索引 {} 个 (期望 {})".format(len(idx), DB_EXPECT_INDEXES)
        return True, "8 张表共 {} 行, {} 个索引, 与期望完全一致".format(total, len(idx))
    finally:
        conn.close()


def download_file(url, dest, expected_sha):
    """带断点续传 + 进度显示 + sha256 校验的下载。返回 (ok, 消息)。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dest) + ".part")
    have = part.stat().st_size if part.exists() else 0

    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", "bytes={}-".format(have))
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "附件不存在(HTTP 404), 可能 Release 尚未发布"
        return False, "HTTP {}: {}".format(e.code, e.reason)
    except urllib.error.URLError as e:
        return False, "网络不可达: {} (国内网络访问 GitHub 可能较慢, 可稍后重试)".format(e.reason)
    except Exception as e:
        return False, "{}: {}".format(type(e).__name__, e)

    status = getattr(resp, "status", 200)
    if status == 206 and have:
        mode = "ab"
        done = have
        total = have + int(resp.headers.get("Content-Length") or 0)
    else:
        # 服务器不支持续传(或被要求整份重下): 从头来
        mode = "wb"
        done = 0
        total = int(resp.headers.get("Content-Length") or 0)

    info("下载 {} {} ...".format(DB_ASSET, DB_SIZE_HINT))
    start = time.time()
    tty = sys.stdout.isatty()
    next_tick = 10.0
    with open(str(part), mode) as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100.0 / total
                speed = done / max(time.time() - start, 0.001) / 1048576.0
                line = "    {:5.1f}%  {:>6.1f}/{:>6.1f} MB  {:>5.1f} MB/s".format(
                    pct, done / 1048576.0, total / 1048576.0, speed)
            else:
                pct = 0.0
                line = "    {:>6.1f} MB".format(done / 1048576.0)
            if tty:
                # 交互终端: 原地刷新同一行
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
            elif pct >= next_tick:
                # 管道/日志: 每 10% 打一行, 避免把日志刷成一片
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
                next_tick = pct + 10.0
    resp.close()
    if tty:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    if total and done < total:
        return False, "下载不完整({}/{} 字节), 已保留断点, 可重跑本向导续传".format(done, total)

    if expected_sha:
        info("校验 sha256 ...")
        got = _sha256_of(part)
        if got != expected_sha:
            part.unlink()
            return False, "sha256 不匹配(附件可能损坏或版本不符), 已删除半成品"
        ok("sha256 与仓库 SHA256SUMS 一致")

    if dest.exists():
        dest.unlink()
    part.rename(dest)
    return True, "已下载并校验通过"


def ensure_demo_db(py, auto):
    """演示小库 marketing.db: 本地生成, 零下载, 永远可用(兜底方案)。"""
    if DB_DEMO.exists():
        info("演示小库 marketing.db 已就绪")
        return True
    info("生成演示小库 marketing.db (纯虚构数据, 固定随机种子, 不需要联网) ...")
    script = REPO_ROOT / "customer_marketing_db" / "create_db.py"
    code, text = run(py + [str(script)], cwd=script.parent, timeout=300, quiet=True)
    if code != 0 or not DB_DEMO.exists():
        bad("生成 marketing.db 失败(退出码 {})".format(code))
        if text.strip():
            say(text.rstrip())
        return False
    ok("演示小库已生成: {}".format(DB_DEMO.relative_to(REPO_ROOT)))
    return True


def ensure_main_db(py, auto, no_download):
    """主库 enterprise.db: 需要从 GitHub Release 下载 88MB。失败则回退演示小库。"""
    if DB_MAIN.exists():
        good, summary = validate_main_db(DB_MAIN)
        if good:
            ok("主库 enterprise.db 已就绪: {}".format(summary))
            return True
        warn("主库 enterprise.db 存在但校验未过: {}".format(summary))
        if no_download or not ask("是否删除并按 Release 重新下载?", default=True, auto=auto):
            return False
        DB_MAIN.unlink()

    if no_download:
        warn("已按 --no-download 跳过主库下载, 将使用演示小库")
        return False

    say("")
    say("主库 enterprise.db 不在仓库里(88MB, 有意不入 git), 需要从 GitHub Release 拉取。")
    if not ask("现在下载吗? 约 88MB, 支持断点续传 (国内网络可能较慢)",
               default=True, auto=auto):
        warn("已跳过, 将使用演示小库 marketing.db")
        return False

    rel = "Agentic智能问数在客户营销场景的应用数据集/enterprise.db"
    okk, msg = download_file(DB_URL, DB_MAIN, _expected_sha(rel))
    if not okk:
        bad("下载失败: {}".format(msg))
        warn("将回退使用演示小库 marketing.db(不影响工程自证与界面演示)")
        return False

    good, summary = validate_main_db(DB_MAIN)
    if not good:
        bad("库内容校验未过: {}".format(summary))
        return False
    ok("主库校验通过: {}".format(summary))
    return True


# ============================ 3. 工程自证 ============================
def run_ci_check(py):
    """跑仓库自带的 15 项工程自证(零密钥/零数据/零联网)。"""
    info("执行 python ci_check.py (15 项, 纯标准库, 不联网、不调用 LLM) ...")
    say("")
    code, text = run(py + ["ci_check.py"], cwd=REPO_ROOT, timeout=900)
    say("")
    if code == 0:
        ok("工程自证 15 项全部通过")
        return True
    bad("工程自证未通过(退出码 {})".format(code))
    return False


def run_selfcheck(py):
    """演示层无头自检 —— 信息项。下载 ZIP(无 .git)时其中一项会失败, 不影响使用。"""
    info("执行 python demo/selfcheck.py (演示层无头自检) ...")
    code, text = run(py + ["demo/selfcheck.py"], cwd=REPO_ROOT, timeout=300)
    if code == 0:
        ok("演示层自检通过")
        return True
    if not (REPO_ROOT / ".git").exists():
        warn("自检未全过: 你似乎是下载 ZIP 而非 git clone, "
             "其中『judge 与 git HEAD 一致』一项需要完整 git 历史, 可忽略。")
    else:
        warn("演示层自检未全过(退出码 {}), 详见上方输出; 不阻断启动。".format(code))
    return False


# ============================ 4. API Key ============================
def _read_existing_key():
    env = os.environ.get("LLM_API_KEY")
    if env and env.strip():
        return env.strip(), "环境变量 LLM_API_KEY"
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("LLM_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val, str(ENV_FILE.relative_to(REPO_ROOT))
        except Exception:
            pass
    return None, None


def _mask(key):
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * 8 + key[-4:]


def validate_key(key, base_url, model, timeout=40):
    """用 max_tokens=1 的最小真实请求验证密钥。返回 (ok, 人类可读消息)。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "temperature": 0,
        "max_tokens": 1,
        "stream": False,
    }
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            json.load(resp)
        return True, "API 返回 200, 密钥可用"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code == 401:
            return False, "密钥无效或已被吊销 (HTTP 401)"
        if e.code == 402:
            return False, "账户余额不足 (HTTP 402), 请先充值"
        if e.code == 429:
            return False, "触发限流 (HTTP 429), 请稍后重试"
        return False, "HTTP {}: {}".format(e.code, detail or e.reason)
    except urllib.error.URLError as e:
        return False, "网络不可达: {} (请检查网络/代理)".format(e.reason)
    except Exception as e:
        return False, "{}: {}".format(type(e).__name__, e)


def write_env_local(key):
    lines = []
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("LLM_API_KEY="):
                    continue
                lines.append(line)
        except Exception:
            lines = []
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("LLM_API_KEY=" + key)
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(str(ENV_FILE), 0o600)
    except Exception:
        pass


def ensure_api_key(auto, no_key):
    """返回可用的 API Key, 或 None(表示走离线模式)。"""
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")

    if no_key:
        warn("已按 --no-key 跳过密钥配置, 将以离线模式启动(仅界面, 提问会明确报错)")
        return None

    info("接口: {}    模型: {}".format(base_url, model))
    existing, where = _read_existing_key()
    if existing:
        info("已检测到密钥({}, {}), 正在验证 ...".format(_mask(existing), where))
        good, msg = validate_key(existing, base_url, model)
        if good:
            ok("已验证通过: {}".format(msg))
            return existing
        warn("现有密钥不可用: {}".format(msg))
        if not ask("是否重新输入一个密钥?", default=True, auto=auto):
            return None

    if auto:
        warn("自动模式(--yes)下不交互输入密钥, 将以离线模式启动(仅界面)")
        return None

    say("")
    say("完整问答需要 DeepSeek API Key。密钥只写入 text2sql_demo/.env.local,")
    say("该文件已被 .gitignore 排除, 不会入库、不会被本向导打印。")
    info("还没有密钥? 到 https://platform.deepseek.com 注册后在『API Keys』里创建。")

    import getpass
    for attempt in range(1, 4):
        say("")
        try:
            key = getpass.getpass(_c("1", "请粘贴 API Key") + " (输入不显示, 直接回车跳过): ").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            return None
        if not key:
            warn("未输入密钥, 将以离线模式启动")
            return None
        info("正在向 {} 发起一次最小请求验证 (max_tokens=1) ...".format(base_url))
        good, msg = validate_key(key, base_url, model)
        if good:
            ok("密钥验证通过: {}".format(msg))
            write_env_local(key)
            ok("已写入 {} (权限 600)".format(ENV_FILE.relative_to(REPO_ROOT)))
            return key
        bad("第 {}/3 次验证失败: {}".format(attempt, msg))
        if attempt < 3 and not ask("重试输入?", default=True, auto=auto):
            break

    warn("未获得可用密钥, 将以离线模式启动(界面可用, 提问会明确报错)")
    return None


# ============================ 5. 启动服务 ============================
def find_free_port(start=8000, tries=30):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def wait_health(port, timeout=40):
    url = "http://127.0.0.1:{}/api/health".format(port)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return json.load(resp)
        except Exception:
            time.sleep(0.4)
    return None


def smoke_ask(port, question, timeout=180):
    """通过演示服务真实问一句, 验证『密钥 -> 引擎 -> SQL -> 结果』整条链路。"""
    payload = json.dumps({"question": question, "clarify": False}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:{}/api/ask".format(port),
        data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as e:
        return {"error": "{}: {}".format(type(e).__name__, e)}


def start_server(py, db_path, biz, offline, port, auto, no_smoke, no_browser):
    step(5, 6, "启动本地演示服务" + (" (并做一次真实问答冒烟)" if not no_smoke and not offline else ""))
    cmd = py + ["demo/server.py", "--db", str(db_path), "--port", str(port)]
    if biz:
        cmd += ["--biz-context", BIZ_CONTEXT]
    if offline:
        cmd += ["--offline"]
    info("启动命令: " + " ".join(cmd))
    say("")
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT))
    try:
        health = wait_health(port, timeout=45)
        if health is None:
            bad("服务在 45 秒内没有就绪, 请看上方输出排查。")
            proc.terminate()
            return 1
        ok("服务已就绪: http://127.0.0.1:{}".format(port))
        info("模型: {}    只读: {}".format(health.get("model"), health.get("readonly")))

        if offline:
            warn("离线模式: 页面可以打开、界面要素可看, 但提问会提示缺少密钥。")
            warn("配置密钥后重跑本向导即可获得完整问答。")
        elif not no_smoke:
            say("")
            info("正在做一次真实问答冒烟(会消耗少量 token): {}".format(SMOKE_QUESTION))
            t0 = time.time()
            result = smoke_ask(port, SMOKE_QUESTION)
            elapsed = time.time() - t0
            if result.get("ok") and result.get("answerable"):
                headers = result.get("headers") or []
                rows = result.get("rows") or []
                ok("端到端链路打通: {:.1f}s 返回 {} 列 / {} 行".format(
                    elapsed, len(headers), len(rows)))
                if headers:
                    info("列: " + " | ".join(str(h) for h in headers))
                for row in rows[:3]:
                    info("   " + " | ".join(str(v) for v in row))
                if len(rows) > 3:
                    info("   ... 其余 {} 行请在页面上查看".format(len(rows) - 3))
            else:
                bad("冒烟问答未成功: {}".format(
                    result.get("error") or result.get("reject_reason") or result))
                warn("服务仍在运行, 你可以打开页面自行排查; 常见原因是余额/网络/模型名。")

        step(6, 6, "打开演示页面")
        url = "http://127.0.0.1:{}".format(port)
        say("")
        say(_c("1;32", "  请在浏览器打开:  " + url))
        say("")
        if not no_browser:
            try:
                import webbrowser
                webbrowser.open(url)
                ok("已尝试自动打开浏览器(若没弹出, 请手动复制上面的地址)")
            except Exception as e:
                warn("自动打开浏览器失败({}), 请手动复制地址".format(e))
        else:
            info("已按 --no-browser 跳过自动打开")

        say("")
        rule()
        say("服务正在运行。停止: 在本窗口按 Ctrl+C")
        say("日志: {}".format((REPO_ROOT / "logs" / "demo_server.log").relative_to(REPO_ROOT)))
        say("重新运行本向导可再次自检; 数据与密钥都已就绪时它会直接启动。")
        rule()
        try:
            proc.wait()
        except KeyboardInterrupt:
            say("")
            info("正在停止服务 ...")
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
            ok("已停止。")
        return 0
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


# ============================ 主流程 ============================
def parse_args(argv):
    opts = {
        "auto": False, "no_download": False, "no_key": False,
        "no_browser": False, "no_smoke": False, "no_serve": False,
        "port": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a in ("-h", "--help"):
            print(__doc__)
            print("选项:")
            print("  --yes            全自动: 所有询问取默认值(已装环境时无人值守)")
            print("  --no-download    不下载 88MB 主库, 直接用演示小库 marketing.db")
            print("  --no-key         不配置密钥, 以离线模式启动(仅界面)")
            print("  --no-smoke       跳过启动后的真实问答冒烟(省 token)")
            print("  --no-browser     不自动打开浏览器")
            print("  --no-serve       只做检测与自检, 不启动服务(适合批量验收)")
            print("  --port N         指定端口(默认从 8000 起自动找空闲端口)")
            sys.exit(0)
        elif a == "--yes":
            opts["auto"] = True
        elif a == "--no-download":
            opts["no_download"] = True
        elif a == "--no-key":
            opts["no_key"] = True
        elif a == "--no-browser":
            opts["no_browser"] = True
        elif a == "--no-smoke":
            opts["no_smoke"] = True
        elif a == "--no-serve":
            opts["no_serve"] = True
        elif a == "--port":
            if i >= len(argv):
                print("[错误] --port 后面需要一个整数")
                sys.exit(2)
            raw = argv[i]
            i += 1
            try:
                opts["port"] = int(raw)
            except ValueError:
                print("[错误] --port 需要整数, 收到: {!r}".format(raw))
                sys.exit(2)
        elif a.startswith("--port="):
            try:
                opts["port"] = int(a.split("=", 1)[1])
            except ValueError:
                print("[错误] --port 需要整数, 收到: {!r}".format(a))
                sys.exit(2)
        else:
            print("[错误] 未知参数: {} (用 --help 查看可用选项)".format(a))
            sys.exit(2)
    return opts


def main():
    global COLOR
    _setup_stdout()
    _enable_windows_ansi()
    COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    opts = parse_args(sys.argv[1:])
    auto = opts["auto"]

    say("")
    title("Agentic 智能问数 —— 评委一键环境向导")
    say("  本向导会自动完成: 环境检测 -> 数据补全 -> 工程自证 -> 密钥配置 -> 启动演示")
    say("  预期耗时: 首次约 3-8 分钟(含 88MB 数据下载); 环境齐备时约 30 秒")
    say("")
    say("  仓库: {}".format(REPO_ROOT))

    if not (REPO_ROOT / "ci_check.py").exists() or not (REPO_ROOT / "demo" / "server.py").exists():
        bad("当前目录不像本项目根目录(缺 ci_check.py 或 demo/server.py)。")
        bad("请把本文件放在仓库根目录再运行。")
        return 2

    py = detect_python(auto)
    if py is None:
        return 2

    step(2, 6, "数据与演示库")
    demo_ok = ensure_demo_db(py, auto)
    main_ok = ensure_main_db(py, auto, opts["no_download"])
    if not main_ok and not demo_ok:
        bad("两个库都不可用, 无法继续。")
        return 2
    if main_ok:
        db_path = DB_MAIN
        biz = True
        info("将使用主库 enterprise.db + 券商口径注入 ({} 为演示脚本指定的口径文件)".format(BIZ_CONTEXT))
    else:
        db_path = DB_DEMO
        biz = False
        info("将使用演示小库 marketing.db(引擎默认银行语义, 无需下载)")

    step(3, 6, "工程自证(零密钥、零联网)")
    ci_ok = run_ci_check(py)
    run_selfcheck(py)
    if not ci_ok:
        bad("工程自证未通过。请把上方输出反馈给作者; 向导在此停下以免误导。")
        return 1

    step(4, 6, "配置 API Key")
    api_key = ensure_api_key(auto, opts["no_key"])

    if opts["no_serve"]:
        step(5, 6, "检测完成(--no-serve, 不启动服务)")
        ok("环境已就绪。去掉 --no-serve 即可启动演示页面。")
        say("")
        say("  手动启动命令:")
        say("    {} demo/server.py --db \"{}\"{} --port 8000".format(
            " ".join(py), db_path,
            "" if not biz else " --biz-context " + BIZ_CONTEXT))
        return 0

    port = opts["port"] or find_free_port()
    if port is None:
        bad("找不到空闲端口(8000-8029 都被占用), 请用 --port 指定。")
        return 2
    return start_server(py, db_path, biz, api_key is None, port, auto,
                        opts["no_smoke"], opts["no_browser"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("")
        say("已中断。")
        sys.exit(130)
