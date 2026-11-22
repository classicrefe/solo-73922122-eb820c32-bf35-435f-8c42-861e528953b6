#!/bin/sh
# 演示：同一规则（seed=42, quota=1）的两张抽检单，回报受理范围不同。
# 用法：先启动服务（docker compose up --build），再执行 scripts/demo.sh
# 注意：演示假定全新数据；重复演示前请先重置（docker compose down -v 后重新 up），
# 否则删除重录会推进修订号，脚本里固定的 revision=1 会被拒。
set -eu

BASE="${QC_URL:-http://localhost:8000}"

# 从 JSON 输出中取字段（依赖 python3；也可用 /docs 手工对照）
json_get() {
    python3 -c "import sys, json; print(json.load(sys.stdin)$1)"
}

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 换版 v1：A 馆 B001/B002/B003，B 馆 B101"
curl -sS -X POST "$BASE/catalog/versions" -H 'Content-Type: application/json' -d '{
  "entries": [
    {"bib_no": "B001", "branch": "A", "title": "书名一"},
    {"bib_no": "B002", "branch": "A", "title": "书名二"},
    {"bib_no": "B003", "branch": "A", "title": "书名三"},
    {"bib_no": "B101", "branch": "B", "title": "书名十"}
  ]}'
echo

step "2. 开单 1（seed=42, quota=1）"
O1=$(curl -sS -X POST "$BASE/orders" -H 'Content-Type: application/json' \
    -d '{"seed": 42, "quota": 1}' | json_get "['order_id']")
echo "order_id=$O1"
echo "当前名单（A 馆选中 B002）："
curl -sS "$BASE/selection?order_id=$O1" | json_get "['items']"
echo

step "3. 换版 v2：A 馆加入 N000（seed 42 下优先级更高）-> B002 落选但仍在目录中"
curl -sS -X POST "$BASE/catalog/versions" -H 'Content-Type: application/json' -d '{
  "entries": [
    {"bib_no": "B001", "branch": "A", "title": "书名一"},
    {"bib_no": "B002", "branch": "A", "title": "书名二"},
    {"bib_no": "B003", "branch": "A", "title": "书名三"},
    {"bib_no": "N000", "branch": "A", "title": "新书"},
    {"bib_no": "B101", "branch": "B", "title": "书名十"}
  ]}'
echo

step "4. 开单 2（同规则 seed=42, quota=1）"
O2=$(curl -sS -X POST "$BASE/orders" -H 'Content-Type: application/json' \
    -d '{"seed": 42, "quota": 1}' | json_get "['order_id']")
echo "order_id=$O2"
echo "当前名单（A 馆改选 N000）："
curl -sS "$BASE/selection?order_id=$O2" | json_get "['items']"
echo

report() { # order_id bib_no 期望说明
    code=$(curl -sS -o /tmp/qc_report_resp.json -w '%{http_code}' \
        -X POST "$BASE/reports?order_id=$1" -H 'Content-Type: application/json' \
        -d "{\"bib_no\": \"$2\", \"revision\": 1, \"match\": true}")
    echo "  单 $1 回报 $2 -> HTTP $code（$3）$(cat /tmp/qc_report_resp.json)"
}

step "5. 同规则两张单的回报差异"
report "$O1" B002 "单 1 开出时 B002 在名单中 -> 201 受理"
report "$O1" N000 "单 1 开出后换版入选 -> 201 受理"
report "$O2" B002 "单 2 开出时 B002 已落选 -> 409 退回"
report "$O2" N000 "单 2 现行名单 -> 201 受理"

step "6. 存储对比（容器内 sqlite3 可选查看）"
echo "  rules：同规则只存一套；rule_bibs：按曾入选的不同书目数增长（本例 3 行：B002/N000/B101）；"
echo "  orders：每单只加固定大小的 (rule_id, base_seq)；issued（旧版逐单表）：新单不再写入。"
