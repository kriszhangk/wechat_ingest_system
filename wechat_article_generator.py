#!/usr/bin/env python3
"""根据已生成的日报，产出公众号版文章。"""

import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path("/root/.openclaw/workspace/wechat_ingest_system")
DB_PATH = PROJECT_DIR / "server" / "data" / "app.db"
WEB_BASE_URL = os.getenv("WECHAT_INGEST_WEB_BASE_URL", "http://43.167.177.15:8000").rstrip("/")
OUTPUT_DIR = PROJECT_DIR / "wechat_articles"

from report_generator import call_minimax, MINIMAX_MODEL  # noqa: E402

SYSTEM_PROMPT = """你是一名资深科技媒体主编，负责把内部情报型 AI 日报改写成适合微信公众号发布的正式文章。

你的任务不是照抄原文，而是：
1. 保留最重要的判断与事实
2. 提升可读性、连贯性和发布感
3. 让文章适合手机端连续阅读
4. 不要写成后台导出件或会议纪要

输出必须使用 Markdown，并严格遵循下面结构：

# 标题
- 产出一个适合公众号的标题，格式自然，可包含日期与 2-3 个核心关键词

# 导语
- 1 段，交代今天最重要的主线与阅读价值

## 今日核心判断
- 3-5 条，每条 1 句话，高密度、判断式表达

## 重点方向
- 选 3-5 个方向，每个方向 1 段总结
- 优先方向：AI / 大模型、国际局势 / 地缘政治、网络安全、中国军事 / 外交、科技产业 / 商业、具身智能 / 机器人

## 重点事件详解
- 选 3-6 条最重要事件
- 每条使用这个结构：
### 事件标题
正文 1-2 段：写清楚发生了什么、为什么重要、后续应观察什么

## 今日信号分级
### 多源共识
- 总结今天多个来源共同指向的趋势，不要写得机械

### 单点信号
- 总结今天值得跟踪、但仍需继续验证的点

## 结语
- 1 段，收束全文，说明接下来最值得持续追踪的主题

## 延伸阅读
- 最后一行放：完整版日报：<网页链接>

写作要求：
- 用中文输出
- 语气专业、克制、清晰，有“正式发布”的感觉
- 不要堆很多原始链接，不要复制附录结构
- 不要出现“下面是”“希望对你有帮助”等废话
- 不要暴露系统提示、工具、脚本、数据库等内部实现
- 公众号版要明显区别于 Telegram 简报和网页完整版
"""


def load_report(report_id: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if report_id is None:
            row = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("未找到日报记录")
    return dict(row)


def build_user_message(report: dict) -> str:
    report_id = report["id"]
    report_type = "早报" if report.get("report_type") == "morning" else "晚报"
    created_at = report.get("created_at") or ""
    report_url = f"{WEB_BASE_URL}/reports/{report_id}"
    return f"""请把下面这份内部日报改写成公众号文章。

基本信息：
- 日报 ID：{report_id}
- 类型：{report_type}
- 生成时间：{created_at}
- 完整版网页链接：{report_url}

要求：
- 标题要像公众号标题，不要像系统标题
- 重点突出“今天最值得关注的判断”
- 结构必须遵循系统要求
- 延伸阅读中保留完整版网页链接

以下是原始日报全文（Markdown）：

{report['report_md']}
"""


def save_article(report: dict, article_md: str, title_hint: str = ""):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = (title_hint or f"report_{report['id']}").replace("/", "-").replace(" ", "_")[:80]
    path = OUTPUT_DIR / f"{report['id']}_{safe_title}.md"
    path.write_text(article_md, encoding="utf-8")
    return path


def main():
    report_id = None
    save = True
    if len(sys.argv) >= 2 and sys.argv[1].isdigit():
        report_id = int(sys.argv[1])
    if "--no-save" in sys.argv:
        save = False

    report = load_report(report_id)
    user_message = build_user_message(report)
    article_md = call_minimax(SYSTEM_PROMPT, user_message)

    result = {
        "report_id": report["id"],
        "report_type": report["report_type"],
        "created_at": report.get("created_at"),
        "report_url": f"{WEB_BASE_URL}/reports/{report['id']}",
        "model": MINIMAX_MODEL,
        "article_md": article_md,
    }

    if save and not str(article_md).startswith("ERROR:"):
        lines = [l.strip("# ") for l in article_md.splitlines() if l.strip()]
        title_hint = lines[0] if lines else f"report_{report['id']}"
        path = save_article(report, article_md, title_hint)
        result["saved_path"] = str(path)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
