import html
import re
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md


class ArticleFetchError(Exception):
    pass


class ArticleDeletedError(ArticleFetchError):
    def __init__(self, message: str, status: str = "deleted"):
        super().__init__(message)
        self.status = status


class ArticleFetcher:
    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }

    def fetch(self, url: str) -> dict:
        resp = requests.get(url, headers=self.headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        deleted_reason, deleted_status = self._detect_deleted_reason(soup)
        if deleted_reason:
            raise ArticleDeletedError(deleted_reason, deleted_status)
        title = self._get_meta(soup, "og:title") or self._safe_text(soup.select_one("title"))
        content_node = soup.select_one("#js_content")
        content_html = str(content_node) if content_node else ""
        raw_content_md = md(content_html) if content_html else ""
        image_urls = self._extract_image_urls(content_node)
        video_urls = self._extract_video_urls(content_node, content_html)
        content_md = self._repair_markdown_media(raw_content_md, image_urls, video_urls)
        if not content_html.strip() and not content_md.strip():
            raise ArticleFetchError("正文抽取失败：未找到文章正文节点")
        return {
            "title": title or "",
            "content_html": content_html,
            "content_md": content_md,
            "image_urls": image_urls,
            "video_urls": video_urls,
        }

    def _detect_deleted_reason(self, soup):
        text = soup.get_text("\n", strip=True)
        markers = [
            ("deleted_author", "该内容已被发布者删除"),
            ("deleted_author", "此内容已被发布者删除"),
            ("deleted_author", "内容已被发布者删除"),
            ("deleted_violation", "该内容因违规无法查看"),
            ("deleted_violation", "此内容因违规无法查看"),
            ("deleted_complaint", "此内容被投诉且经审核涉嫌侵权，无法查看"),
            ("deleted_unavailable", "此内容已无法查看"),
        ]
        for status, marker in markers:
            if marker in text:
                return marker, status
        return "", ""

    def _normalize_url(self, value: str):
        raw = (value or "").strip()
        if not raw:
            return ""
        raw = html.unescape(raw)
        raw = raw.replace("\\/", "/").strip("'\" ")
        if "%3A%2F%2F" in raw or "%3a%2f%2f" in raw:
            raw = unquote(raw)
        if raw.startswith("//"):
            raw = f"https:{raw}"
        if raw.startswith(("http://", "https://")):
            return raw
        return ""

    def _push_media_item(self, bucket: list, seen: set, item: dict):
        key = item.get("url") or item.get("vid") or item.get("id")
        if not key or key in seen:
            return
        seen.add(key)
        bucket.append(item)

    def _extract_image_urls(self, content_node):
        if not content_node:
            return []
        results = []
        seen = set()
        for node in content_node.find_all(True):
            candidates = []
            if node.name == "img":
                candidates.extend([
                    ("data-src", node.get("data-src")),
                    ("src", node.get("src")),
                    ("data-croporisrc", node.get("data-croporisrc")),
                    ("data-backsrc", node.get("data-backsrc")),
                ])
            candidates.extend([
                ("data-miniprogram-imageurl", node.get("data-miniprogram-imageurl")),
                ("data-cover", node.get("data-cover")),
                ("poster", node.get("poster")),
            ])
            for source_attr, raw_url in candidates:
                url = self._normalize_url(raw_url)
                if not url:
                    continue
                item = {
                    "url": url,
                    "tag": node.name,
                    "source_attr": source_attr,
                }
                alt = node.get("alt") or node.get("data-alt") or ""
                if alt:
                    item["alt"] = alt.strip()
                width = node.get("data-w") or node.get("width") or ""
                if width:
                    item["width"] = str(width)
                media_type = node.get("data-type") or ""
                if media_type:
                    item["type"] = str(media_type)
                self._push_media_item(results, seen, item)
                break
        return results

    def _extract_video_urls(self, content_node, content_html: str):
        results = []
        seen = set()
        if content_node:
            for node in content_node.find_all(True):
                tag_name = (node.name or "").lower()
                cls_text = " ".join(node.get("class", [])).lower() if node.get("class") else ""
                looks_video = (
                    tag_name == "video"
                    or "video" in tag_name
                    or "video" in cls_text
                    or (node.get("data-type") or "").lower() == "video"
                    or node.get("data-url")
                )
                if not looks_video:
                    continue
                candidates = [
                    ("data-url", node.get("data-url")),
                    ("src", node.get("src")),
                    ("data-src", node.get("data-src")),
                    ("href", node.get("href")),
                ]
                item = {
                    "tag": tag_name,
                    "source": "tag",
                }
                for source_attr, raw_url in candidates:
                    url = self._normalize_url(raw_url)
                    if url:
                        item["url"] = url
                        item["source_attr"] = source_attr
                        break
                vid = node.get("vid") or node.get("data-vid") or ""
                if vid:
                    item["vid"] = vid
                node_id = node.get("data-id") or node.get("id") or ""
                if node_id:
                    item["id"] = node_id
                desc = node.get("data-desc") or node.get("title") or node.get("alt") or ""
                if desc:
                    item["title"] = desc.strip()
                cover = self._normalize_url(node.get("poster") or node.get("data-cover") or node.get("data-poster") or "")
                if cover:
                    item["cover_url"] = cover
                if item.get("url") or item.get("vid") or item.get("id"):
                    self._push_media_item(results, seen, item)

        for match in re.finditer(r'vid["\']?\s*[:=]\s*["\'](?P<vid>wxv_[A-Za-z0-9]+)["\']', content_html or "", re.I):
            self._push_media_item(results, seen, {
                "vid": match.group("vid"),
                "source": "regex",
                "source_attr": "vid",
            })

        for match in re.finditer(r'data-url="(?P<url>https?://[^"]+)"', content_html or "", re.I):
            url = self._normalize_url(match.group("url"))
            if not url or ("video" not in url and "mp4" not in url and "findermp.video.qq.com" not in url):
                continue
            self._push_media_item(results, seen, {
                "url": url,
                "source": "regex",
                "source_attr": "data-url",
            })

        for match in re.finditer(r'src="(?P<url>https?://[^"<>]+(?:\.mp4|video[^"<>]*|findermp\.video\.qq\.com[^"<>]*|v\.qq\.com[^"<>]*))"', content_html or "", re.I):
            url = self._normalize_url(match.group("url"))
            if not url:
                continue
            self._push_media_item(results, seen, {
                "url": url,
                "source": "regex",
                "source_attr": "src",
            })

        return results

    def _repair_markdown_media(self, content_md: str, image_urls, video_urls):
        text = content_md or ""
        images = image_urls or []
        videos = video_urls or []
        image_index = 0

        def repl(match):
            nonlocal image_index
            original_alt = (match.group(1) or "").strip()
            if image_index >= len(images):
                return match.group(0)
            item = images[image_index]
            image_index += 1
            alt = original_alt if original_alt and original_alt.lower() not in {"img", "image"} else (item.get("alt") or "图片")
            return f"![{alt}]({item['url']})"

        text = re.sub(r'(?m)^\s*图片\s*\n[=-]{2,}\s*$', '![图片]()', text)
        text = re.sub(r'(?m)^(\s*)图片\s*$', r'\1![图片]()', text)
        text = re.sub(r'!\[([^\]]*)\]\(\s*\)', repl, text)

        video_links = []
        seen = set()
        for item in videos:
            url = (item or {}).get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            if url in text:
                continue
            title = (item or {}).get("title") or (item or {}).get("vid") or "视频"
            video_links.append(f"- {title}：{url}")

        if video_links:
            suffix = "\n\n---\n\n## 视频链接\n" + "\n".join(video_links)
            if "## 视频链接" not in text:
                text = (text.rstrip() + suffix).strip() + "\n"

        return text

    def _get_meta(self, soup, prop_name: str):
        node = soup.find("meta", attrs={"property": prop_name}) or soup.find("meta", attrs={"name": prop_name})
        return node.get("content") if node else None

    def _safe_text(self, node):
        return node.get_text(strip=True) if node else ""
