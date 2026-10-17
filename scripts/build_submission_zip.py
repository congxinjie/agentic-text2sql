#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成邮箱/邮件提交用的 zip 包（纯标准库）。

**为什么不用 `git archive`**（实测结论，别改回去）：
    git archive 产出的 zip 在 macOS 自带 `unzip`（Info-ZIP 6.0）下解压会失败：

        checkdir error: cannot create .../Agentic<乱码>
        Illegal byte sequence

    原因是它按 CP437 解码中文文件名，即使 zip 里已设置 UTF-8 标志位（bit 11）。
    同样的内容改用 Python `zipfile` 重写后，`unzip` / `ditto`(Finder 双击) / Python
    三种解压方式都能正确还原中文名 —— 所以打包必须走 Python zipfile。

用法：
    python3 scripts/build_submission_zip.py              # 输出到 <仓库父目录>/提交包/
    python3 scripts/build_submission_zip.py -o /tmp/x.zip

约定：
    只打包 **git 已跟踪** 的文件，因此天然排除 .env.local / *.db / logs / __pycache__
    （这些都在 .gitignore 里）。打包结束会再做一次安全复核并打印 sha256。
"""

import argparse
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "agentic-text2sql/"

# 打包后禁止出现的路径特征（防御性复核：即使将来有人不小心把这类文件加进 git）
FORBIDDEN = (
    ".env.local",
    "/.env",
    "logs/",
    "__pycache__/",
)
FORBIDDEN_SUFFIX = (".db", ".part")


def git_tracked_files():
    """返回 git 已跟踪文件的相对路径列表（按字节-safe 处理中文名）。"""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             stdout=subprocess.PIPE, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit("无法读取 git 文件列表（需要在仓库内且有 git）：{}".format(e))
    return [p for p in out.decode("utf-8").split("\0") if p]


def is_forbidden(rel):
    if rel.endswith(FORBIDDEN_SUFFIX):
        return True
    return any(pat in "/" + rel for pat in FORBIDDEN)


def check_clean_tree():
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                             stdout=subprocess.PIPE, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.decode("utf-8", "replace").strip()


def main():
    ap = argparse.ArgumentParser(description="打包参赛提交 zip（用 Python zipfile，避开 git archive 的中文名缺陷）")
    ap.add_argument("-o", "--output", default=None, help="输出路径（默认 <仓库父目录>/提交包/agentic-text2sql-源代码与文档.zip）")
    ap.add_argument("--allow-dirty", action="store_true", help="工作区有未提交改动时也继续")
    args = ap.parse_args()

    dirty = check_clean_tree()
    if dirty and not args.allow_dirty:
        sys.stderr.write("[警告] 工作区有未提交改动，包里是已提交版本，两者可能不一致：\n")
        for line in dirty.splitlines()[:10]:
            sys.stderr.write("    " + line + "\n")
        sys.stderr.write("继续请加 --allow-dirty\n")
        return 2

    out_path = Path(args.output) if args.output else (ROOT.parent / "提交包" / "agentic-text2sql-源代码与文档.zip")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    files = [f for f in git_tracked_files() if not is_forbidden(f)]
    skipped = [f for f in git_tracked_files() if is_forbidden(f)]
    if not files:
        sys.exit("没有可打包的文件（git ls-files 为空？）")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                sys.stderr.write("[跳过] 已跟踪但本地缺失: {}\n".format(rel))
                continue
            zi = zipfile.ZipInfo(PREFIX + rel)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (0o644 & 0xFFFF) << 16
            with open(src, "rb") as fh:
                z.writestr(zi, fh.read())

    # ---- 复核 ----
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    size = out_path.stat().st_size
    with zipfile.ZipFile(out_path) as z:
        infos = z.infolist()
        names = z.namelist()
        bad = [n for n in names
               if n.startswith(PREFIX) and is_forbidden(n[len(PREFIX):])]
        non_ascii = [i for i in infos if any(ord(c) > 127 for c in i.filename)]
        utf8_ok = sum(1 for i in non_ascii if i.flag_bits & 0x800)
        # 内容级密钥扫描(与 ci_check 的 secret 项同一正则), 只读文本类文件
        import re
        secret_re = re.compile(r"sk-[A-Za-z0-9]{20,}")
        leaked = []
        for i in infos:
            if i.file_size > 2 * 1024 * 1024:
                continue
            blob = z.read(i.filename)
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if secret_re.search(text):
                leaked.append(i.filename)

    print("=" * 66)
    print("输出   : {}".format(out_path))
    print("体积   : {:.2f} MB ({} 字节)".format(size / 1048576.0, size))
    print("文件数 : {}".format(len(names)))
    print("sha256 : {}".format(digest))
    print("-" * 66)
    print("复核   : 禁止项 {} 个 ｜ 内容含疑似密钥 {} 个 ｜ 中文名 {} 个(UTF-8 标志 {} 个)".format(
        len(bad), len(leaked), len(non_ascii), utf8_ok))
    if skipped:
        print("已排除 : {}".format(", ".join(skipped[:5])))
    problems = 0
    if bad:
        problems += 1
        print("  [X] 有禁止项混入: {}".format(bad[:5]))
    if leaked:
        problems += 1
        print("  [X] 有文件内容疑似含 API 密钥: {}".format(leaked[:5]))
    if non_ascii and utf8_ok != len(non_ascii):
        problems += 1
        print("  [X] 有中文文件名未设置 UTF-8 标志位（Windows 解压会乱码）")
    if problems == 0:
        print("  [OK] 安全复核通过")
    print("=" * 66)
    print("提示: 提交前请用 `unzip -q {0} -d /tmp/check` 与 Finder 双击各试一次。".format(out_path.name))
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
