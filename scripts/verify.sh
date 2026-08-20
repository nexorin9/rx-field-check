#!/usr/bin/env bash
#
# scripts/verify.sh —— 用药处方字段对照校验系统 一键验证脚本。
#
# 目的（与 task 15 / spec.md 输出闭环表对齐）：
#   1) 安装依赖（pip install -e .[dev]）
#   2) check 4 张脱敏样例 → runs/<rx_no>/ 落盘
#   3) 后台启动 rxchk view（带 --serve-seconds 自动停）
#   4) 解析启动链接 → 拿到 host:port + token
#   5) curl 验证三类端点：
#        GET  /api/runs                   （runs 列表按 mtime 倒序）
#        GET  /api/run/<rx>/findings      （字段证据卡 JSON）
#        POST /api/run/<rx>/confirm       （确认 / 备注 / 驳回）
#        GET  /api/run/<rx>/audit         （审计日志含本条）
#   6) viewer 自停（--serve-seconds 到时）；脚本 exit 0 表示全过。
#
# 使用：
#   bash scripts/verify.sh                 # 默认 30 秒 viewer 自动停
#   RXCHK_VIEW_SECONDS=10 bash scripts/verify.sh
#   RXCHK_RUNS_DIR=/tmp/x bash scripts/verify.sh
#
# 退出码：
#   0  —— 全部 curl 断言通过
#   非 0 —— 任意一步失败；具体哪一步见 stderr 输出

set -euo pipefail

# --------------------------------------------------------------------------- #
# 路径与参数                                                                    #
# --------------------------------------------------------------------------- #

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 默认把 runs 落到项目内的 /tmp 子目录，避免与既往 round 产物混淆；
# 用 RXCHK_RUNS_DIR 覆盖。
RXCHK_RUNS_DIR="${RXCHK_RUNS_DIR:-${PROJECT_ROOT}/runs/verify_$$}"
RXCHK_INBOX_DIR="${RXCHK_INBOX_DIR:-${PROJECT_ROOT}/inbox}"
RXCHK_VIEW_SECONDS="${RXCHK_VIEW_SECONDS:-30}"
RXCHK_SAMPLES_DIR="${RXCHK_SAMPLES_DIR:-${PROJECT_ROOT}/src/rxchk/data/sample_rx}"

# 4 张样例：rx_basic / rx_overdose / rx_allergy / rx_complex
# 这些是处方入参 JSON 文件里的 prescription_no 字段值；
# rxchk check 落盘目录 runs/<prescription_no>/ 直接用它。
SAMPLE_RX_BASIC="RX-2026-<EXAMPLE_BASIC>"
SAMPLE_RX_OVERDOSE="RX-2026-<EXAMPLE_OVERDOSE>"
SAMPLE_RX_ALLERGY="RX-2026-<EXAMPLE_ALLERGY>"
SAMPLE_RX_COMPLEX="RX-2026-<EXAMPLE_COMPLEX>"

# 入参 JSON 文件名（与 prescription_no 解耦，便于阅读）
SAMPLE_FILE_BASIC="rx_basic.json"
SAMPLE_FILE_OVERDOSE="rx_overdose.json"
SAMPLE_FILE_ALLERGY="rx_allergy.json"
SAMPLE_FILE_COMPLEX="rx_complex.json"

PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONPATH

mkdir -p "${RXCHK_RUNS_DIR}" "${RXCHK_INBOX_DIR}"

# 日志落盘：默认到项目根的 logs/（与 round 根 logs/ 区分；本脚本是项目级验收）
PROJECT_LOG_DIR="${PROJECT_ROOT}/logs"
mkdir -p "${PROJECT_LOG_DIR}"
VERIFY_LOG="${PROJECT_LOG_DIR}/verify-$$.log"
VIEWER_LOG="${PROJECT_LOG_DIR}/verify-viewer-$$.log"

# --------------------------------------------------------------------------- #
# 工具函数                                                                      #
# --------------------------------------------------------------------------- #

log() { printf '[verify] %s\n' "$*" | tee -a "${VERIFY_LOG}" >&2; }
fail() { printf '[verify][FAIL] %s\n' "$*" | tee -a "${VERIFY_LOG}" >&2; exit 1; }

# 等到 stdout 文件里出现匹配 pattern 的行（最长 N 秒），返回匹配行原文
wait_for_line() {
    local pattern="$1" file="$2" timeout="${3:-15}"
    local waited=0
    while [ "${waited}" -lt "${timeout}" ]; do
        if grep -E "${pattern}" "${file}" >/dev/null 2>&1; then
            grep -E "${pattern}" "${file}" | head -n1
            return 0
        fi
        sleep 0.2
        waited=$(awk -v w="${waited}" -v s="0.2" 'BEGIN{printf "%d", w+s}')
    done
    return 1
}

# 从 viewer 启动行抽取 host:port 与 token
# 输入形如: [rxchk] Viewer 启动：http://127.0.0.1:54321/?token=<secret>
parse_viewer_url() {
    local line="$1"
    local url
    url="$(printf '%s' "${line}" | grep -oE 'http://[^[:space:]]+' | head -n1)"
    if [ -z "${url}" ]; then
        fail "无法从 viewer 启动行抽取 URL：${line}"
    fi
    HOST_PORT="$(printf '%s' "${url}" | sed -E 's|http://([^/]+).*|\1|')"
    TOKEN="$(printf '%s' "${url}" | sed -E 's|.*token=([^&[:space:]]+).*|\1|')"
    if [ -z "${HOST_PORT}" ] || [ -z "${TOKEN}" ]; then
        fail "URL 解析失败：HOST_PORT='${HOST_PORT}' TOKEN='${TOKEN}'"
    fi
}

# 用 python 校验 JSON 含必填字段；缺则 fail
assert_json_has() {
    local label="$1" jq_expr="$2" expected="$3" body_file="$4"
    python - "${label}" "${jq_expr}" "${expected}" "${body_file}" <<'PY'
import json
import re
import sys

label, jq_expr, expected, body_file = sys.argv[1:5]
with open(body_file, encoding="utf-8") as fh:
    body = json.load(fh)

# 极简 jq_expr：
#   "key"               字典取键
#   "key[i]"            列表按下标取
#   "key[i].subkey"     链式：先按下标再取子键（点号可省略在 key 后）
#   "key[*].subkey"     列表展开为各元素子键列表
def get(obj, expr):
    """把表达式切成 key/idx/sub 段递归走。"""
    if not expr:
        return obj
    # 切首段：要么是 'key' 要么是 '[idx]'
    if expr.startswith("["):
        m = re.match(r"^\[(\d+|\*)\](.*)$", expr)
        if not m:
            raise ValueError(f"无法解析的 jq 表达式：{expr!r}")
        idx_or_star = m.group(1)
        rest = m.group(2)
        if idx_or_star == "*":
            sub = rest.lstrip(".") if rest else ""
            return [get(item, sub) if sub else item for item in obj]
        obj = obj[int(idx_or_star)]
    else:
        m = re.match(r"^([^\[\.]+)(.*)$", expr)
        if not m:
            raise ValueError(f"无法解析的 jq 表达式：{expr!r}")
        key = m.group(1)
        rest = m.group(2)
        obj = obj[key]
    rest = rest.lstrip(".")
    if rest:
        return get(obj, rest)
    return obj

try:
    value = get(body, jq_expr)
except (KeyError, IndexError, TypeError, ValueError) as exc:
    print(f"[verify][FAIL] {label}: {jq_expr} 解析失败：{exc}", file=sys.stderr)
    print(json.dumps(body, ensure_ascii=False, indent=2)[:1200], file=sys.stderr)
    sys.exit(1)

if expected.startswith("len:"):
    want = int(expected[4:])
    if not isinstance(value, list) or len(value) != want:
        actual = len(value) if hasattr(value, '__len__') else '?'
        print(f"[verify][FAIL] {label}: {jq_expr} len={actual} expected={want}", file=sys.stderr)
        print(json.dumps(body, ensure_ascii=False, indent=2)[:1200], file=sys.stderr)
        sys.exit(1)
elif expected.startswith("contains:"):
    needle = expected[len("contains:"):]
    if needle not in str(value):
        print(f"[verify][FAIL] {label}: {jq_expr}={value!r} does not contain {needle!r}", file=sys.stderr)
        sys.exit(1)
elif expected.startswith(">="):
    want = int(expected[2:])
    if not (isinstance(value, (int, float)) and value >= want):
        print(f"[verify][FAIL] {label}: {jq_expr}={value} < {want}", file=sys.stderr)
        sys.exit(1)
elif str(value) != expected:
    print(f"[verify][FAIL] {label}: {jq_expr}={value!r} expected={expected!r}", file=sys.stderr)
    sys.exit(1)
print(f"[verify][OK]   {label}: {jq_expr}={value!r}")
PY
}

# --------------------------------------------------------------------------- #
# Step 1: 安装依赖                                                              #
# --------------------------------------------------------------------------- #

log "项目根: ${PROJECT_ROOT}"
log "Step 1/6: 安装依赖（pip install -e .[dev]）"
if [ -d "${PROJECT_ROOT}/.venv" ]; then
    log "  检测到 .venv，跳过安装（视为已安装）"
else
    pip install -e "${PROJECT_ROOT}[dev]" >>"${VERIFY_LOG}" 2>&1 || \
        fail "pip install -e .[dev] 失败；日志见 ${VERIFY_LOG}"
fi

# --------------------------------------------------------------------------- #
# Step 2: check 4 张样例                                                        #
# --------------------------------------------------------------------------- #

log "Step 2/6: rxchk check 4 张样例 → ${RXCHK_RUNS_DIR}"
for sample_file in "${SAMPLE_FILE_BASIC}" "${SAMPLE_FILE_OVERDOSE}" "${SAMPLE_FILE_ALLERGY}" "${SAMPLE_FILE_COMPLEX}"; do
    sample_path="${RXCHK_SAMPLES_DIR}/${sample_file}"
    if [ ! -f "${sample_path}" ]; then
        fail "样例处方缺失：${sample_path}"
    fi
    log "  check ${sample_path}"
    set +e
    python -m rxchk.cli check "${sample_path}" \
        --data-root "${PROJECT_ROOT}/src/rxchk/data" \
        --run-root "${RXCHK_RUNS_DIR}" \
        >>"${VERIFY_LOG}" 2>&1
    rc=$?
    set -e
    # 退出码语义：0=无 finding / 2=有 finding / 其它=失败
    if [ "${rc}" -ne 0 ] && [ "${rc}" -ne 2 ]; then
        fail "rxchk check ${sample_file} 退出码=${rc}（预期 0 或 2）"
    fi
done

# 验证 4 个 run 目录都齐全（按 spec.md 输出闭环表：input/transcript/findings/run）
log "  验证 runs/<rx>/{input.json,transcript.jsonl,findings.json,run.json} 齐全"
for rx in "${SAMPLE_RX_BASIC}" "${SAMPLE_RX_OVERDOSE}" "${SAMPLE_RX_ALLERGY}" "${SAMPLE_RX_COMPLEX}"; do
    run_dir="${RXCHK_RUNS_DIR}/${rx}"
    for f in input.json transcript.jsonl findings.json run.json; do
        [ -f "${run_dir}/${f}" ] || fail "缺失 ${run_dir}/${f}"
    done
done

# --------------------------------------------------------------------------- #
# Step 3: 后台启动 viewer                                                       #
# --------------------------------------------------------------------------- #

log "Step 3/6: 后台启动 rxchk view --serve-seconds=${RXCHK_VIEW_SECONDS}"
: >"${VIEWER_LOG}"
python -m rxchk.cli view \
    --host 127.0.0.1 \
    --port 0 \
    --no-open \
    --serve-seconds "${RXCHK_VIEW_SECONDS}" \
    --run-root "${RXCHK_RUNS_DIR}" \
    >>"${VIEWER_LOG}" 2>&1 &
VIEWER_PID=$!

# 兜底：脚本退出时强制杀 viewer（即便断言失败也不留进程）
trap 'if kill -0 "${VIEWER_PID}" 2>/dev/null; then kill -TERM "${VIEWER_PID}" 2>/dev/null || true; fi' EXIT

if ! wait_for_line "Viewer 启动" "${VIEWER_LOG}" 15; then
    fail "viewer 启动超时；日志：${VIEWER_LOG}"
fi
LOG_LINE="$(grep -E "Viewer 启动" "${VIEWER_LOG}" | head -n1)"
log "  viewer 启动：${LOG_LINE}"
parse_viewer_url "${LOG_LINE}"
BASE_URL="http://${HOST_PORT}"
log "  base_url=${BASE_URL}  token=${TOKEN:0:8}…"

# --------------------------------------------------------------------------- #
# Step 4: curl 断言 /api/runs                                                  #
# --------------------------------------------------------------------------- #

log "Step 4/6: curl GET /api/runs → 期望 count=4"
RUNS_BODY="$(mktemp -t rxchk_verify_runs.XXXXXX.json)"
HTTP_CODE="$(curl -sS -o "${RUNS_BODY}" -w '%{http_code}' "${BASE_URL}/api/runs?token=${TOKEN}")"
[ "${HTTP_CODE}" = "200" ] || fail "GET /api/runs 状态码=${HTTP_CODE}（预期 200）；body=${RUNS_BODY}"
assert_json_has "runs count" "count" "4" "${RUNS_BODY}"
# 4 张样例都在 runs 列表里（run_name 取的是 rx 编号）
assert_json_has "runs 长度" "runs" "len:4" "${RUNS_BODY}"
# 任意一条 run 应带 status=awaiting_review（首次核对后状态）
assert_json_has "run status" "runs.[0].status" "awaiting_review" "${RUNS_BODY}"
assert_json_has "run prescription_no 非空" "runs.[0].prescription_no" "contains:RX-2026-" "${RUNS_BODY}"

# 错误路径：无 token → 401
log "  验证无 token → 401"
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "${BASE_URL}/api/runs")"
[ "${HTTP_CODE}" = "401" ] || fail "无 token 期望 401，实际 ${HTTP_CODE}"

# --------------------------------------------------------------------------- #
# Step 5: curl 断言 /api/run/<rx>/findings + confirm                            #
# --------------------------------------------------------------------------- #

log "Step 5/6: curl GET /api/run/<rx>/findings + POST /api/run/<rx>/confirm"
# 选 rx_complex（必命中 ≥ 2 finding）做端到端断言
RX_FOR_ASSERT="${SAMPLE_RX_COMPLEX}"

FINDINGS_BODY="$(mktemp -t rxchk_verify_findings.XXXXXX.json)"
HTTP_CODE="$(curl -sS -o "${FINDINGS_BODY}" -w '%{http_code}' \
    "${BASE_URL}/api/run/${RX_FOR_ASSERT}/findings?token=${TOKEN}")"
[ "${HTTP_CODE}" = "200" ] || fail "GET /api/run/<rx>/findings 状态码=${HTTP_CODE}"
assert_json_has "findings 非空" "count" ">=2" "${FINDINGS_BODY}"
# rx_complex 必含 family=interaction / family=allergy 至少其一（数据驱动断言）
FAMILIES="$(python -c "
import json
with open('${FINDINGS_BODY}', encoding='utf-8') as f:
    b = json.load(f)
print(','.join(sorted({it['family'] for it in b.get('findings', [])})))
")"
log "  findings.families=${FAMILIES}"
case "${FAMILIES}" in
    *interaction*|*allergy*) ;;
    *) fail "rx_complex 期望命中 interaction/allergy，实际=${FAMILIES}" ;;
esac

# POST /confirm：以 override 操作确认一条 finding（不强制指定 finding_id；
# append-only 审计只要新增一行即可）
CONFIRM_BODY="$(mktemp -t rxchk_verify_confirm.XXXXXX.json)"
HTTP_CODE="$(curl -sS -X POST -H 'Content-Type: application/json' \
    -o "${CONFIRM_BODY}" -w '%{http_code}' \
    -d "$(cat <<JSON
{"action": "override", "operator": "<EXAMPLE_PHARMACIST>", "note": "verify.sh 自动化测试 - 与医师沟通后维持原剂量"}
JSON
)" \
    "${BASE_URL}/api/run/${RX_FOR_ASSERT}/confirm?token=${TOKEN}")"
[ "${HTTP_CODE}" = "200" ] || fail "POST /confirm 状态码=${HTTP_CODE}（预期 200）；body=${CONFIRM_BODY}"
assert_json_has "confirm ok" "ok" "True" "${CONFIRM_BODY}"
assert_json_has "confirm status" "status" "overridden" "${CONFIRM_BODY}"

# 再 GET /audit → 必含刚才那条 override
AUDIT_BODY="$(mktemp -t rxchk_verify_audit.XXXXXX.json)"
HTTP_CODE="$(curl -sS -o "${AUDIT_BODY}" -w '%{http_code}' \
    "${BASE_URL}/api/run/${RX_FOR_ASSERT}/audit?token=${TOKEN}")"
[ "${HTTP_CODE}" = "200" ] || fail "GET /audit 状态码=${HTTP_CODE}"
assert_json_has "audit 长度" "entries" "len:1" "${AUDIT_BODY}"
assert_json_has "audit 含 override" "entries.[0].action" "override" "${AUDIT_BODY}"
assert_json_has "audit 备注" "entries.[0].note" "contains:verify.sh" "${AUDIT_BODY}"

# 清理临时文件
rm -f "${RUNS_BODY}" "${FINDINGS_BODY}" "${CONFIRM_BODY}" "${AUDIT_BODY}"

# --------------------------------------------------------------------------- #
# Step 6: viewer 自停                                                            #
# --------------------------------------------------------------------------- #

log "Step 6/6: 等 viewer 自停（--serve-seconds=${RXCHK_VIEW_SECONDS}）"
# 等待最多 RXCHK_VIEW_SECONDS + 5 秒让进程优雅退出
deadline=$(($(date +%s) + RXCHK_VIEW_SECONDS + 5))
while kill -0 "${VIEWER_PID}" 2>/dev/null; do
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        fail "viewer 未在 ${RXCHK_VIEW_SECONDS}+5 秒内自停"
    fi
    sleep 0.5
done
wait "${VIEWER_PID}" 2>/dev/null || true
trap - EXIT

log "✓ verify.sh 全过；完整日志：${VERIFY_LOG} / viewer 日志：${VIEWER_LOG}"
exit 0
