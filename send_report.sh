#!/bin/bash
# ============================================================
# 早报 / 晚报发送脚本
# 用法:
#   ./send_report.sh morning   # 生成并发送早报
#   ./send_report.sh evening  # 生成并发送晚报
#
# 依赖:
#   - python3 report_generator.py 已完成
#   - openclaw message 命令可用
#   - Telegram group ID: -5289939595
# ============================================================

set -e

REPORT_TYPE="${1:-morning}"
PROJECT_DIR="/root/.openclaw/workspace/wechat_ingest_system"
GENERATOR="${PROJECT_DIR}/report_generator.py"
OUTPUT_FILE="${PROJECT_DIR}/report_latest.txt"
STATE_FILE="${PROJECT_DIR}/report_state.json"
TARGET_CHAT="${WECHAT_INGEST_REPORT_TARGET_CHAT:--5289939595}"
REPORT_LABEL="早报"

if [ "$REPORT_TYPE" != "morning" ] && [ "$REPORT_TYPE" != "evening" ]; then
    echo "用法: $0 [morning|evening]"
    exit 1
fi

if [ "$REPORT_TYPE" = "evening" ]; then
    REPORT_LABEL="晚报"
fi

# ── 1. 生成报告 ─────────────────────────────────────────────
echo "[$REPORT_TYPE] 生成报告中..."
python3 "$GENERATOR" "$REPORT_TYPE" > "$OUTPUT_FILE"

# ── 2. 读取报告内容 ────────────────────────────────────────
CONTENT=$(cat "$OUTPUT_FILE")
echo "[$REPORT_TYPE] 报告已生成，字符数: $(echo "$CONTENT" | wc -c)"

# ── 3. 发送报告（每段最多 4000 字符） ───────────────────────
# Telegram 消息上限约 4096，保留余量
MAX_LEN=3900

# 3a. 发送标题头
openclaw message send \
    --channel telegram \
    --target "$TARGET_CHAT" \
    --message "🧭 AI 资讯${REPORT_LABEL} | $(date '+%Y-%m-%d')"

# 3b. 分割并发送正文
CURRENT=""
SECTION=""
SEND_COUNT=0

send_chunk() {
    if [ -n "$CURRENT" ]; then
        openclaw message send \
            --channel telegram \
            --target "$TARGET_CHAT" \
            --message "$CURRENT"
        SEND_COUNT=$((SEND_COUNT + 1))
        echo "  -> 已发送第 $SEND_COUNT 段"
    fi
    CURRENT=""
}

# 按行处理
while IFS= read -r line; do
    # 检测到大标题行则另起一段
    if echo "$line" | grep -qE '^## '; then
        send_chunk
        CURRENT="$line"
    elif [ ${#CURRENT} -gt $MAX_LEN ]; then
        send_chunk
        CURRENT="$line"
    else
        CURRENT="${CURRENT}${CURRENT:+$'\n'}${line}"
    fi
done <<< "$CONTENT"

# 最后一段
send_chunk

echo "[$REPORT_TYPE] 发送完成，共 $SEND_COUNT 段"

# ── 4. 更新状态文件（可选） ─────────────────────────────────
# jq 可能有也可能没有，用 python 代替
python3 - "$STATE_FILE" "$REPORT_TYPE" << 'EOF'
import json, sys
state_file, report_type = sys.argv[1], sys.argv[2]
state = json.load(open(state_file))
now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
if report_type == 'morning':
    state['last_morning_report_at'] = now
else:
    state['last_evening_report_at'] = now
json.dump(state, open(state_file, 'w'), indent=2, ensure_ascii=False)
print(f"状态已更新: {report_type} -> {now}")
EOF
