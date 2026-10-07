#!/usr/bin/env bash
# 拉取数据集附件(Release: dataset-v1)到数据集目录, 并做 sha256 + 库内容双重校验。
#
# 用法:
#   bash scripts/fetch_dataset.sh                # 默认: 拉 4 张大表 CSV + 现成 enterprise.db(共约 151MB), 全部校验
#   bash scripts/fetch_dataset.sh --csv-only     # 只拉 4 张大表 CSV(约 64MB)
#   bash scripts/fetch_dataset.sh --db-only      # 只拉现成 enterprise.db(约 88MB)
#   bash scripts/fetch_dataset.sh --rebuild      # 拉完 CSV 后接着跑 benchmark/build_db.py 重建库(约 6 秒)
#   bash scripts/fetch_dataset.sh --check        # 不下载, 只校验本地已有的附件(一个都没有则报错退出)
#
# 认证: 公开仓库匿名直链即可(无需 gh/token); 私有仓库需要 gh 已登录(gh auth status) 或导出 GH_TOKEN。
#       脚本按 "匿名直链 → gh → GH_TOKEN" 依次回退, 公开/私有都能用。
# 幂等: 重复执行会覆盖同名文件(--clobber); 校验不通过则退出码非 0。
set -euo pipefail

REPO="${REPO:-congxinjie/agentic-text2sql}"
TAG="${TAG:-dataset-v1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D="$ROOT/Agentic智能问数在客户营销场景的应用数据集"
PY="${PYTHON:-python3}"

MODE="all"
for a in "$@"; do
  case "$a" in
    --csv-only) MODE="csv" ;;
    --db-only)  MODE="db" ;;
    --rebuild)  MODE="all+rebuild" ;;
    --check)    MODE="check" ;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "[!] 未知参数: $a (试 --help)" >&2; exit 2 ;;
  esac
done

CSVS=(dim_product_202606021049.csv dwd_cust_hold_d_202606021051.csv \
      dwd_cust_tran_d_202606021051.csv dws_cust_aset_d_202606021051.csv)
DB=enterprise.db

WANT=()
case "$MODE" in
  csv) WANT=("${CSVS[@]}") ;;
  db)  WANT=("$DB") ;;
  *)   WANT=("${CSVS[@]}" "$DB") ;;
esac

mkdir -p "$D"

have_gh() { command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; }

# 公开仓库: 匿名直链下载 release 附件(无需 gh/token); 失败则删除半成品, 返回非 0。
curl_public_asset() {
  local name="$1" out="$2" tmp="$2.part"
  if curl -fsSL -C - --connect-timeout 15 -o "$tmp" "https://github.com/$REPO/releases/download/$TAG/$name"; then
    mv -f "$tmp" "$out"; return 0
  fi
  rm -f "$tmp"; return 1
}

# 私有仓库: 用 API 反查附件 id(需 GH_TOKEN), 再按 id 下载 —— 不依赖 gh。
curl_asset() {
  local name="$1" out="$2" id
  [ -n "${GH_TOKEN:-}" ] || { echo "[!] 私有仓库需要 gh 登录或 GH_TOKEN; 当前两者都没有" >&2; return 1; }
  id=$(curl -sS -H "Authorization: token $GH_TOKEN" \
        "https://api.github.com/repos/$REPO/releases/tags/$TAG" \
       | python3 -c "
import sys, json
d = json.load(sys.stdin)
name = sys.argv[1]
for a in d.get('assets', []):
    if a['name'] == name:
        print(a['id']); break
else:
    sys.exit('asset not found: ' + name)
" "$name")
  # 先试 octet-stream, 部分版本需再跟一次 302 到 objects.githubusercontent.com
  curl -sSL -H "Authorization: token $GH_TOKEN" -H "Accept: application/octet-stream" \
       -o "$out" "https://api.github.com/repos/$REPO/releases/assets/$id"
}

if [ "$MODE" = "check" ]; then
  echo "[·] --check: 只校验, 不下载"
else
  echo "[·] 拉取 ${#WANT[@]} 个附件 ($REPO @ $TAG) → $D"
  for f in "${WANT[@]}"; do
    printf '    ↓ %s ... ' "$f"
    # 1) 公开仓库: 匿名直链(无需任何认证)
    if curl_public_asset "$f" "$D/$f"; then
      echo "ok (匿名直链)"
      continue
    fi
    # 2) 私有仓库 + gh 已登录
    if have_gh && gh release download "$TAG" -R "$REPO" -p "$f" -D "$D" --clobber; then
      echo "ok (gh release download)"
      continue
    fi
    # 3) 私有仓库 + GH_TOKEN
    if [ -n "${GH_TOKEN:-}" ] && curl_asset "$f" "$D/$f"; then
      echo "ok (GH_TOKEN)"
      continue
    fi
    echo "失败" >&2
    echo "    匿名直链失败(仓库可能是 private 或附件不存在); 且 gh 未登录、GH_TOKEN 未设置。" >&2
    echo "    公开仓库: 检查网络/代理; 私有仓库: 先 gh auth login 或 export GH_TOKEN=<token>" >&2
    exit 1
  done
fi

echo
echo "[·] 校验 sha256 (对照仓库根 SHA256SUMS, 缺的文件跳过)"
present=0
for f in "${WANT[@]}"; do [ -f "$D/$f" ] && present=$((present + 1)); done
if [ "$present" = 0 ]; then
  echo "    ✗ 本地没有可校验的附件(检查目录: $D)" >&2
  echo "      先下载: bash scripts/fetch_dataset.sh   (或 --db-only / --csv-only)" >&2
  exit 1
fi
cd "$ROOT"
if sha256sum -c --ignore-missing SHA256SUMS; then
  echo "    ✓ sha256 全部匹配"
else
  echo "    ✗ sha256 不匹配 —— 附件可能损坏或版本不符" >&2
  exit 1
fi

echo
echo "[·] 校验库内容(表/行数/索引)"
"$PY" - "$D/$DB" <<'PY'
import os, sqlite3, sys
path = sys.argv[1]
if not os.path.exists(path):
    print("    (无 enterprise.db, 跳过; 如已拉 CSV 可 bash scripts/fetch_dataset.sh --rebuild 重建)")
    sys.exit(0)
EXPECT = {"dim_branch": 312, "dim_public": 155, "dim_product": 334694,
          "ads_cust_info_d": 500, "dwd_cust_hold_d": 408150, "dwd_cust_tran_d": 39060,
          "dws_cust_aset_d": 43684, "dws_cust_fin_d": 4892}
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
bad = 0
for t, n in EXPECT.items():
    try:
        got = c.execute(f'select count(*) from "{t}"').fetchone()[0]
    except sqlite3.Error as e:
        print(f"    ✗ {t}: 查询失败 {e}"); bad += 1; continue
    print(f"    {'✓' if got == n else '✗'} {t:20} {got:>8} (期望 {n})")
    bad += got != n
idx = [r[0] for r in c.execute("select name from sqlite_master where type='index' and name not like 'sqlite_%'")]
print(f"    索引 {len(idx)} 个 (期望 7), 总行数 {sum(EXPECT.values())} (期望 831447)")
sys.exit(1 if bad else 0)
PY

if [ "$MODE" = "all+rebuild" ]; then
  echo
  echo "[·] 由 4 张大表 CSV 重建 enterprise.db"
  "$PY" "$ROOT/benchmark/build_db.py"
fi

echo
echo "✓ 完成。可直接: python3 benchmark/run_eval.py   或   python3 demo/server.py --db \"$D/$DB\""
