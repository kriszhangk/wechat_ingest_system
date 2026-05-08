import json
from pathlib import Path
from app.config import EXPORT_DIR
from app.utils.time_util import filename_ts, now_str


class ArticleService:
    def export_markdown(self, article: dict):
        safe_title = "".join(c for c in article["title"] if c not in r'\\/:*?"<>|').strip()
        safe_title = safe_title[:80] if safe_title else "untitled"
        filename = f"{filename_ts()}_{safe_title}.md"
        path = Path(EXPORT_DIR) / filename
        image_urls = article.get('image_urls') or []
        video_urls = article.get('video_urls') or []
        content = f"""---
title: {article.get('title', '')}
publish_time: {article.get('publish_time', '')}
source_url: {article.get('article_url', '')}
exported_at: {now_str()}
image_count: {len(image_urls)}
video_count: {len(video_urls)}
image_urls_json: {json.dumps(image_urls, ensure_ascii=False)}
video_urls_json: {json.dumps(video_urls, ensure_ascii=False)}
---

# {article.get('title', '')}

> 原文链接: {article.get('article_url', '')}

---

{article.get('content_md', '')}
"""
        path.write_text(content, encoding="utf-8")
        return str(path)
