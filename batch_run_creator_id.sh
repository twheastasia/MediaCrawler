#!/bin/bash
set -euo pipefail

# ============================================================
# 配置
# ============================================================
USER_ID_FILE="user_id.json"        # 数据源：JSON 数组文件
PROGRESS_FILE=".batch_progress_creator"  # 记录已完成 user_id 的文件

# ============================================================
# 参数解析：--start N --end N（均为 1-based，包含两端）
# ============================================================
START_IDX=0   # 0 表示从第 1 条开始
END_IDX=0     # 0 表示到最后一条结束

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)
      START_IDX="$2"
      shift 2
      ;;
    --end)
      END_IDX="$2"
      shift 2
      ;;
    *)
      echo "未知参数: $1" >&2
      echo "用法: $0 [--start N] [--end N]" >&2
      exit 1
      ;;
  esac
done

# ============================================================
# 读取 user_id 列表（解析 JSON 数组）
# ============================================================
if [ ! -f "$USER_ID_FILE" ]; then
  echo "错误：找不到数据源文件 $USER_ID_FILE" >&2
  exit 1
fi

# 用 jq 或 python3 解析 JSON，输出到临时文件（兼容 macOS bash 3.x，避免 mapfile）
TMP_ALL=$(mktemp)
TMP_IDS=$(mktemp)
CMD_OUTPUT=$(mktemp)
trap 'rm -f "$TMP_ALL" "$TMP_IDS" "$CMD_OUTPUT"' EXIT

if command -v jq >/dev/null 2>&1; then
  jq -r '.[]' "$USER_ID_FILE" > "$TMP_ALL"
else
  python3 -c "
import json
with open('$USER_ID_FILE') as f:
    ids = json.load(f)
for uid in ids:
    print(uid)
" > "$TMP_ALL"
fi

ALL_TOTAL=$(wc -l < "$TMP_ALL" | tr -d ' ')

# 计算实际起止行号（1-based）
[ "$START_IDX" -eq 0 ] && START_IDX=1
[ "$END_IDX" -eq 0 ] && END_IDX="$ALL_TOTAL"

if [ "$START_IDX" -lt 1 ] || [ "$END_IDX" -gt "$ALL_TOTAL" ] || [ "$START_IDX" -gt "$END_IDX" ]; then
  echo "错误：--start/$START_IDX 或 --end/$END_IDX 超出范围（共 $ALL_TOTAL 条）" >&2
  exit 1
fi

# 切片到 TMP_IDS
sed -n "${START_IDX},${END_IDX}p" "$TMP_ALL" > "$TMP_IDS"
TOTAL=$(wc -l < "$TMP_IDS" | tr -d ' ')

# ============================================================
# 工具函数
# ============================================================
is_done() {
  [ -f "$PROGRESS_FILE" ] && grep -qxF "$1" "$PROGRESS_FILE"
}

mark_done() {
  echo "$1" >> "$PROGRESS_FILE"
}

# ============================================================
# 主循环
# ============================================================
echo "========================================================"
echo "全部 $ALL_TOTAL 个 user_id，本机处理第 $START_IDX ~ $END_IDX 条（共 $TOTAL 个）"
echo "进度文件: $PROGRESS_FILE"
echo "========================================================"

DONE_COUNT=0
SKIP_COUNT=0
INDEX=0

while IFS= read -r user_id; do
  INDEX=$((INDEX + 1))

  if is_done "$user_id"; then
    echo "[SKIP] ($INDEX/$TOTAL) 已完成，跳过: $user_id"
    SKIP_COUNT=$((SKIP_COUNT + 1))
    continue
  fi

  echo "------------------------------------------------------------"
  echo "[RUN ] ($INDEX/$TOTAL) 执行 creator_id: $user_id"
  echo "------------------------------------------------------------"

  if XHS_CREATOR_START="$START_IDX" XHS_CREATOR_END="$END_IDX" \
     uv run main.py --platform xhs --lt qrcode --type creator --get_comment false --headless false 2>&1 | tee "$CMD_OUTPUT"; then
    if grep -qE "Login xiaohongshu failed by qrcode login method" "$CMD_OUTPUT"; then
      echo "[ABORT] 检测到登录已过期，需要重新扫码登录，整批任务终止。" >&2
      exit 2
    fi
    mark_done "$user_id"
    DONE_COUNT=$((DONE_COUNT + 1))
    echo "[OK  ] 完成: $user_id"
  else
    EXIT_CODE=$?
    if grep -qE "Login xiaohongshu failed by qrcode login method" "$CMD_OUTPUT"; then
      echo "[ABORT] 检测到登录已过期，需要重新扫码登录，整批任务终止。" >&2
      exit 2
    fi
    echo "[FAIL] 执行失败 (exit=$EXIT_CODE)，creator_id: $user_id" >&2
    echo "程序终止。修复问题后重新运行脚本，将从此 user_id 继续。" >&2
    exit $EXIT_CODE
  fi
done < "$TMP_IDS"

echo "========================================================"
echo "全部完成！本次新执行 $DONE_COUNT 个，跳过 $SKIP_COUNT 个。"
echo "========================================================"
