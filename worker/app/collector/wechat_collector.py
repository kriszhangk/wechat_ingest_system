import json
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from app.config import PERSISTENT_PROFILE_DIR, HEADLESS, DEBUG_SAVE_ARTIFACTS, PARAM_CACHE_TTL_SECONDS


class WechatCollector:
    def __init__(self):
        self.debug_dir = Path("./data/debug")
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.debug_save_artifacts = DEBUG_SAVE_ARTIFACTS
        self.profile_dir = Path(PERSISTENT_PROFILE_DIR)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.request_urls = []
        self.interesting_requests = []
        self.interesting_responses = []
        self.discovered_token = None
        self.discovered_fingerprint = None
        self.playwright = None
        self.context = None
        self.page = None
        self.log_file = self.debug_dir / "worker.log"
        self.cached_token = None
        self.cached_fingerprint = None
        self.cached_lang = None
        self.cached_params_at = None
        self.last_resolved = {}
        self.last_fetch_stats = {}
        self.bound_page_ids = set()

    def log(self, message: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [collector] {message}"
        print(line, flush=True)
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def start(self):
        if self.context is not None:
            self.log("复用已存在的 persistent browser context")
            return
        self.log(f"启动 persistent browser: profile_dir={self.profile_dir}, headless={HEADLESS}")
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=HEADLESS,
            viewport={"width": 1440, "height": 960},
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._bind_page(self.page)
        self.log(f"browser 已启动，当前 pages={len(self.context.pages)}，url={self.page.url}")

    def ensure_browser_alive(self):
        try:
            if self.context is None or self.page is None:
                self.log("browser/page 不存在，重新启动")
                self._reset_browser_state()
                self.start()
                return
            _ = self.page.url
            self.page.set_default_timeout(15000)
        except Exception as e:
            self.log(f"检测到 browser/page 已失效，准备重建: err={e}")
            self._reset_browser_state()
            self.start()

    def _reset_browser_state(self):
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright is not None:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None
        self.context = None
        self.page = None
        self.cached_params_at = None
        self.bound_page_ids = set()

    def _bind_page(self, page):
        if page is None:
            return None
        try:
            page.set_default_timeout(15000)
        except Exception:
            pass
        page_id = id(page)
        if page_id not in self.bound_page_ids:
            try:
                page.on("request", self._on_request)
                page.on("response", self._on_response)
                self.bound_page_ids.add(page_id)
                self.log(f"已绑定页面事件: page_id={page_id}, url={getattr(page, 'url', '')}")
            except Exception as e:
                self.log(f"绑定页面事件失败: page_id={page_id}, err={e}")
        return page

    def _set_active_page(self, page, reason: str = ""):
        page = self._bind_page(page)
        self.page = page
        if page is not None:
            self.log(f"切换当前页面: reason={reason or 'unspecified'}, pages={len(self.context.pages) if self.context else 0}, url={page.url}")
        return page

    def _close_page_safely(self, page, reason: str = ""):
        if page is None:
            return
        try:
            if page.is_closed():
                return
        except Exception:
            return
        try:
            url = page.url
        except Exception:
            url = ""
        try:
            page.close()
            self.log(f"已关闭多余标签页: reason={reason or 'cleanup'}, url={url}")
        except Exception as e:
            self.log(f"关闭标签页失败: reason={reason or 'cleanup'}, url={url}, err={e}")

    def _close_extra_pages(self, keep_page=None, reason: str = ""):
        if self.context is None:
            return keep_page
        pages = list(self.context.pages)
        if not pages:
            page = self.context.new_page()
            return self._set_active_page(page, reason=f"{reason or 'cleanup'}_new_page")
        if keep_page is None:
            keep_page = next((p for p in pages if not p.is_closed()), None)
        if keep_page is None:
            keep_page = self.context.new_page()
        for p in pages:
            if p != keep_page:
                self._close_page_safely(p, reason=reason or "cleanup_extra_pages")
        try:
            keep_page.bring_to_front()
        except Exception:
            pass
        return self._set_active_page(keep_page, reason=reason or "cleanup_extra_pages")

    def _prepare_refresh_page(self, page, reason: str = ""):
        if self.context is None:
            self.start()
            page = self.page
        if page is None:
            page = self.page or (self.context.new_page() if self.context else None)
        page = self._close_extra_pages(keep_page=page, reason=reason or "prepare_refresh")
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page

    def close(self):
        self.log("close() 被调用；当前配置为调试模式，不主动关闭浏览器")

    def refresh_homepage_session(self, reason: str = "scheduled_cookie_keepalive"):
        """Refresh mp.weixin.qq.com in the persistent browser context.

        This is intentionally separate from task fetching so the runner can pause
        polling while the page refreshes and resume only after a successful load.
        """
        self.ensure_browser_alive()
        self.log(f"开始刷新公众号首页以保持 cookie 活跃: reason={reason}")
        page = self._prepare_refresh_page(self.page, reason=reason)
        try:
            page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            self._set_active_page(page, reason=reason)
            self.log(f"公众号首页刷新成功: url={page.url}")
            self._dump(page, "scheduled_homepage_refresh")
            return True
        except Exception as e:
            self.log(f"公众号首页刷新失败: err={e}")
            raise

    def fetch_articles(self, keyword: str, resolved_fakeid: str | None = None, min_publish_time: int | None = None, max_items: int | None = None):
        self.ensure_browser_alive()
        self.request_urls = []
        self.interesting_requests = []
        self.interesting_responses = []
        self.discovered_token = None
        self.discovered_fingerprint = None
        self.last_fetch_stats = {
            "min_publish_time": self._to_int_ts(min_publish_time),
            "max_items": self._to_int_ts(max_items),
            "params_source": None,
            "params_refreshed": False,
            "retry_after_refresh": False,
            "pages_fetched": 0,
            "raw_item_count": 0,
            "accepted_item_count": 0,
            "filtered_by_min_publish_time": 0,
            "skipped_missing_title_or_url": 0,
            "skipped_missing_publish_time": 0,
            "duplicate_count": 0,
            "oldest_ts_seen": None,
        }

        page = self.page
        self.log(f"开始抓取 keyword={keyword}")

        try:
            token, fingerprint, lang = self._ensure_params(page)
            page = self.page or page
            self.log(f"参数就绪: token={token}, fingerprint={fingerprint}, lang={lang}")
            self.last_resolved = {}
            try:
                if resolved_fakeid:
                    fakeid = resolved_fakeid
                    self.last_resolved = {"nickname": keyword, "alias": None, "fakeid": fakeid}
                    self.log(f"优先使用服务端已缓存 fakeid={fakeid}")
                else:
                    fakeid = self._search_fakeid(page, keyword, token, fingerprint, lang)
                    self.log(f"searchbiz 命中 fakeid={fakeid}")
                items = self._fetch_appmsg_list(page, fakeid, token, fingerprint, lang, min_publish_time=min_publish_time, max_items=max_items)
            except Exception as first_error:
                if not self._should_refresh_params(first_error):
                    raise
                self.last_fetch_stats["retry_after_refresh"] = True
                self.log(f"检测到参数/会话类异常，准备刷新参数后重试一次: err={first_error}")
                token, fingerprint, lang = self._refresh_params(page, reason="retry_after_error")
                page = self.page or page
                self.log(f"参数刷新完成: token={token}, fingerprint={fingerprint}, lang={lang}")
                fakeid = self._search_fakeid(page, keyword, token, fingerprint, lang)
                self.log(f"searchbiz 重试命中 fakeid={fakeid}")
                items = self._fetch_appmsg_list(page, fakeid, token, fingerprint, lang, min_publish_time=min_publish_time, max_items=max_items)
            self.log(f"appmsgpublish 解析完成，文章数={len(items)}")
            self._flush_debug()
            self._dump(page, f"done_{self._safe(keyword)}")
            return items, self.last_resolved, self.last_fetch_stats
        except Exception as e:
            self.log(f"抓取失败: {e}")
            self.log(traceback.format_exc())
            self._flush_debug()
            self._dump(page, f"fail_{self._safe(keyword)}")
            raise

    def _ensure_params(self, page):
        if self.cached_token and self.cached_fingerprint and self.cached_lang:
            if self.cached_params_at and datetime.now() - self.cached_params_at > timedelta(seconds=PARAM_CACHE_TTL_SECONDS):
                self.log(
                    f"缓存参数已过期，准备主动刷新: cached_at={self.cached_params_at.strftime('%Y-%m-%d %H:%M:%S')}, ttl_seconds={PARAM_CACHE_TTL_SECONDS}"
                )
                return self._refresh_params(page, reason="ttl_expired")
            self.last_fetch_stats["params_source"] = "cache"
            self.log(
                f"复用缓存参数: token={self.cached_token}, fingerprint={self.cached_fingerprint}, lang={self.cached_lang}"
            )
            return self.cached_token, self.cached_fingerprint, self.cached_lang
        self.log("当前无可用缓存参数，开始首次获取 token/fingerprint")
        return self._refresh_params(page, reason="cold_start")

    def _refresh_params(self, page, reason: str = "manual_refresh"):
        self.last_fetch_stats["params_source"] = reason
        self.last_fetch_stats["params_refreshed"] = True
        self.discovered_token = None
        self.discovered_fingerprint = None
        self.log(f"开始刷新 token/fingerprint 缓存: reason={reason}")
        page = self._prepare_refresh_page(page, reason=f"refresh_params_{reason}")
        self.log(f"刷新前已收敛标签页，当前 pages={len(self.context.pages) if self.context else 0}，url={page.url if page else ''}")
        self.log("步骤1：进入首页")
        self._step_home(page)

        self.log("步骤2：点击草稿箱，并在必要时切换到新页面")
        page = self._step_draft(page)
        self.log(f"草稿箱步骤完成，当前页面 url={page.url}")

        self.log("步骤3：先 hover 到“新的创作”，再点击写新文章，并在必要时切换到新页面")
        page = self._step_new_article(page)
        self.log(f"写新文章步骤完成，当前页面 url={page.url}")

        self.log("步骤4：点击超链接")
        self._step_link(page)

        self.log("步骤5：点击选择其他账号 / 公众号文章")
        self._step_other_account(page)
        self._flush_debug()

        self.log("步骤6：提取 token / fingerprint / lang")
        token = self._extract_token(page)
        fingerprint = self._extract_fingerprint(page)
        lang = self._extract_lang(page)

        if not token:
            raise RuntimeError("未能提取 token")
        if not fingerprint:
            raise RuntimeError("未能提取 fingerprint")

        self.cached_token = token
        self.cached_fingerprint = fingerprint
        self.cached_lang = lang
        self.cached_params_at = datetime.now()
        self.log(
            f"缓存参数已更新: token={self.cached_token}, fingerprint={self.cached_fingerprint}, lang={self.cached_lang}, cached_at={self.cached_params_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return token, fingerprint, lang

    def _search_fakeid(self, page, keyword: str, token: str, fingerprint: str, lang: str):
        params = {
            "action": "search_biz",
            "begin": 0,
            "count": 5,
            "query": keyword,
            "fingerprint": fingerprint,
            "token": token,
            "lang": lang,
            "f": "json",
            "ajax": 1,
        }
        url = f"https://mp.weixin.qq.com/cgi-bin/searchbiz?{urlencode(params)}"
        self.log(f"调用 searchbiz: {url}")
        data = self._browser_fetch_json(page, url)
        self._write_json_debug(f"searchbiz_{self._safe(keyword)}.json", data)

        if data.get("base_resp", {}).get("ret") != 0:
            raise RuntimeError(f"searchbiz 返回异常: {data}")

        items = data.get("list", [])
        self.log(f"searchbiz 返回公众号数={len(items)}")
        if not items:
            raise RuntimeError(f"searchbiz 未找到公众号: {keyword}")

        exact = next((x for x in items if x.get("nickname") == keyword or x.get("alias") == keyword), None)
        chosen = exact or items[0]
        self.last_resolved = {
            "nickname": chosen.get("nickname"),
            "alias": chosen.get("alias"),
            "fakeid": chosen.get("fakeid"),
        }
        self.log(f"searchbiz 选中: nickname={chosen.get('nickname')}, alias={chosen.get('alias')}, fakeid={chosen.get('fakeid')}")
        fakeid = chosen.get("fakeid")
        if not fakeid:
            raise RuntimeError(f"searchbiz 未返回 fakeid: {chosen}")
        return fakeid

    def _fetch_appmsg_list(self, page, fakeid: str, token: str, fingerprint: str, lang: str, min_publish_time: int | None = None, max_items: int | None = None):
        all_items = []
        begin = 0
        count = 10
        max_pages = 10
        max_items_limit = self._to_int_ts(max_items)
        raw_item_count = 0
        filtered_by_min_publish_time = 0
        skipped_missing_title_or_url = 0
        skipped_missing_publish_time = 0
        oldest_ts_seen = None
        pages_fetched = 0

        for page_no in range(max_pages):
            pages_fetched += 1
            params = {
                "sub": "list",
                "search_field": "null",
                "begin": begin,
                "count": count,
                "query": "",
                "fakeid": fakeid,
                "type": "101_1",
                "free_publish_type": 1,
                "sub_action": "list_ex",
                "fingerprint": fingerprint,
                "token": token,
                "lang": lang,
                "f": "json",
                "ajax": 1,
            }
            url = f"https://mp.weixin.qq.com/cgi-bin/appmsgpublish?{urlencode(params)}"
            self.log(f"调用 appmsgpublish 第{page_no + 1}页: {url}")
            data = self._browser_fetch_json(page, url)
            if begin == 0:
                self._write_json_debug(f"appmsgpublish_{self._safe(fakeid)}.json", data)

            if data.get("base_resp", {}).get("ret") != 0:
                raise RuntimeError(f"appmsgpublish 返回异常: {data}")

            parsed = self._extract_appmsg_items(data)
            raw_item_count += len(parsed)
            self.log(f"appmsgpublish 第{page_no + 1}页原始解析条数={len(parsed)}")
            if not parsed:
                if page_no == 0:
                    raise RuntimeError("appmsgpublish 返回成功但未解析到文章列表")
                break

            filtered_page_items = []
            oldest_ts_in_page = None
            for item in parsed:
                if not item.get("title") or not item.get("url"):
                    skipped_missing_title_or_url += 1
                    continue
                publish_time = item.get("publish_time")
                ts = self._to_int_ts(publish_time)
                if ts is not None:
                    oldest_ts_in_page = ts if oldest_ts_in_page is None else min(oldest_ts_in_page, ts)
                    oldest_ts_seen = ts if oldest_ts_seen is None else min(oldest_ts_seen, ts)
                if min_publish_time is not None and (ts is None or ts <= int(min_publish_time)):
                    if ts is None:
                        skipped_missing_publish_time += 1
                    else:
                        filtered_by_min_publish_time += 1
                    continue
                filtered_page_items.append({
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "publish_time": str(publish_time) if publish_time is not None else None,
                })

            self.log(f"appmsgpublish 第{page_no + 1}页过滤后条数={len(filtered_page_items)}，min_publish_time={min_publish_time}")
            all_items.extend(filtered_page_items)

            if max_items_limit is not None and len(all_items) >= max_items_limit:
                all_items = all_items[:max_items_limit]
                self.log(f"已达到 max_items 限制={max_items_limit}，停止继续抓取")
                break

            if min_publish_time is not None and oldest_ts_in_page is not None and oldest_ts_in_page <= int(min_publish_time):
                self.log(f"第{page_no + 1}页已触达时间边界 oldest_ts_in_page={oldest_ts_in_page}，停止翻页")
                break

            if len(parsed) < count:
                self.log("当前页返回数量不足一页，停止翻页")
                break

            begin += count

        deduped = []
        seen = set()
        duplicate_count = 0
        for item in all_items:
            if item["url"] in seen:
                duplicate_count += 1
                continue
            seen.add(item["url"])
            deduped.append(item)
            if max_items_limit is not None and len(deduped) >= max_items_limit:
                break

        self.last_fetch_stats.update({
            "pages_fetched": pages_fetched,
            "raw_item_count": raw_item_count,
            "accepted_item_count": len(deduped),
            "filtered_by_min_publish_time": filtered_by_min_publish_time,
            "skipped_missing_title_or_url": skipped_missing_title_or_url,
            "skipped_missing_publish_time": skipped_missing_publish_time,
            "duplicate_count": duplicate_count,
            "oldest_ts_seen": oldest_ts_seen,
        })

        if not deduped:
            self.log("appmsgpublish 返回成功，但按时间基线过滤后无新增文章，本次按成功处理")
            return []

        for idx, item in enumerate(deduped[:10], start=1):
            self.log(f"文章{idx}: title={item['title']} | url={item['url']}")
        return deduped

    def _to_int_ts(self, value):
        if value is None or value == "":
            return None
        try:
            return int(str(value))
        except Exception:
            return None

    def _extract_appmsg_items(self, data: dict):
        results = []

        publish_page = data.get("publish_page")
        if isinstance(publish_page, str) and publish_page.strip():
            try:
                publish_page = json.loads(publish_page)
                self.log("已解析顶层 publish_page JSON 字符串")
            except Exception as e:
                self.log(f"解析 publish_page 失败，回退通用递归: err={e}")
                publish_page = None

        if isinstance(publish_page, dict):
            publish_list = publish_page.get("publish_list", [])
            self.log(f"publish_page.publish_list 条数={len(publish_list)}")
            for entry in publish_list:
                publish_info = entry.get("publish_info")
                if isinstance(publish_info, str) and publish_info.strip():
                    try:
                        publish_info = json.loads(publish_info)
                    except Exception as e:
                        self.log(f"解析 publish_info 失败: err={e}")
                        continue
                if not isinstance(publish_info, dict):
                    continue
                for article in publish_info.get("appmsgex", []) or []:
                    results.append({
                        "title": article.get("title"),
                        "url": article.get("link") or article.get("url"),
                        "publish_time": article.get("update_time") or article.get("publish_time") or article.get("create_time"),
                    })

        if not results:
            def walk(obj):
                if isinstance(obj, dict):
                    if "title" in obj and ("link" in obj or "url" in obj):
                        results.append({
                            "title": obj.get("title"),
                            "url": obj.get("link") or obj.get("url"),
                            "publish_time": obj.get("update_time") or obj.get("publish_time") or obj.get("create_time"),
                        })
                    for v in obj.values():
                        walk(v)
                elif isinstance(obj, list):
                    for x in obj:
                        walk(x)
            walk(data)

        dedup = []
        seen = set()
        for item in results:
            url = item.get("url")
            if url and url not in seen and "mp.weixin.qq.com" in url:
                seen.add(url)
                dedup.append(item)
        return dedup

    def _should_refresh_params(self, error: Exception):
        text = str(error)
        refresh_signals = [
            "未能提取 token",
            "未能提取 fingerprint",
            "token",
            "fingerprint",
            "登录",
            "登录态",
            "relogin",
            "not logged",
            "invalid session",
            "session expired",
            "200003",
            "403",
            "401",
            "fetch 失败 status=401",
            "fetch 失败 status=403",
        ]
        if "appmsgpublish 返回成功但未解析到文章列表" in text:
            return False
        return any(signal in text for signal in refresh_signals)

    def _browser_fetch_json(self, page, url: str):
        script = """
        async (targetUrl) => {
          const resp = await fetch(targetUrl, {
            method: 'GET',
            credentials: 'include',
            headers: {
              'x-requested-with': 'XMLHttpRequest'
            }
          });
          const text = await resp.text();
          return { status: resp.status, text };
        }
        """
        self.ensure_browser_alive()
        page = self.page
        result = page.evaluate(script, url)
        status = result.get("status")
        text = result.get("text", "")
        self.log(f"fetch 返回: status={status}, body_preview={text[:200].replace(chr(10), ' ')}")
        if status != 200:
            raise RuntimeError(f"fetch 失败 status={status}, url={url}, body={text[:500]}")
        try:
            return json.loads(text)
        except Exception as e:
            raise RuntimeError(f"返回不是合法 JSON: {e}; body={text[:500]}")

    def _step_home(self, page):
        page = self._set_active_page(page, reason="step_home")
        page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        self.log(f"首页加载完成: url={page.url}")
        self._dump(page, "step_01_home")

    def _click_and_maybe_switch_page(self, page, selectors, dump_name: str, fail_message: str):
        self.log(f"尝试点击 selectors={selectors}")
        popup_page = None
        for sel in selectors:
            try:
                self.log(f"尝试 popup 方式点击: {sel}")
                with page.expect_popup(timeout=3000) as popup_info:
                    page.locator(sel).first.click(timeout=2500)
                popup_page = popup_info.value
                self.log(f"捕获到 popup，新页面 url={popup_page.url}")
                break
            except Exception as e:
                self.log(f"popup 点击失败: selector={sel}, err={e}")
                continue

        if popup_page is None:
            before_pages = list(page.context.pages)
            self.log(f"未捕获 popup，fallback 到普通点击，点击前 pages={len(before_pages)}")
            if not self._click_any(page, selectors):
                raise RuntimeError(fail_message)
            page.wait_for_timeout(2500)
            after_pages = list(page.context.pages)
            self.log(f"普通点击后 pages={len(after_pages)}")
            if len(after_pages) > len(before_pages):
                popup_page = after_pages[-1]
                self.log(f"检测到新增 page，切到最新 page: url={popup_page.url}")

        target_page = popup_page or page
        target_page = self._close_extra_pages(keep_page=target_page, reason=f"after_{dump_name}")
        target_page.wait_for_timeout(2500)
        self._dump(target_page, dump_name)
        return target_page

    def _step_draft(self, page):
        return self._click_and_maybe_switch_page(page, [
            "text=草稿箱",
            "a:has-text('草稿箱')",
            "text=内容管理",
        ], "step_02_draft", "未找到草稿箱入口")

    def _step_new_article(self, page):
        button_like_selectors = [
            "button:has-text('新的创作')",
            "[role='button']:has-text('新的创作')",
        ]
        generic_create_selectors = [
            "text=新的创作",
            "a:has-text('新的创作')",
            "[class*='create']:has-text('新的创作')",
            "[class*='menu']:has-text('新的创作')",
        ]

        is_button_mode = False
        for sel in button_like_selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    self.log(f"判断结果：新的创作是按钮模式（selector={sel}）")
                    is_button_mode = True
                    break
            except Exception:
                continue

        if is_button_mode:
            create_selectors = button_like_selectors + generic_create_selectors
            clicked_create = False
            for sel in create_selectors:
                try:
                    self.log(f"按钮模式：尝试点击新的创作: {sel}")
                    loc = page.locator(sel).first
                    loc.wait_for(state='visible', timeout=3000)
                    loc.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    clicked_create = True
                    self.log(f"按钮模式：点击新的创作成功: {sel}")
                    break
                except Exception as e:
                    self.log(f"按钮模式：点击新的创作失败: {sel}, err={e}")
                    continue
            if not clicked_create:
                raise RuntimeError("按钮模式下未能点击新的创作")
            self.log("按钮模式：开始点击精确的“文章”菜单项，避免误入文章模板")
            target_page = self._click_and_maybe_switch_page(page, [
                "[role='menuitem'] >> text=/^文章$/",
                "[role='menu'] >> text=/^文章$/",
                "text=/^文章$/",
                ":text-is('文章')",
            ], "step_03_new_article", "按钮模式下未找到文章入口")
            if self._is_article_template_page(target_page):
                raise RuntimeError(f"按钮模式误入文章模板页: url={target_page.url}")
            return target_page

        self.log("判断结果：新的创作不是按钮，走悬停模式")
        hover_selectors = generic_create_selectors + button_like_selectors
        hovered = False
        for sel in hover_selectors:
            try:
                self.log(f"悬停模式：尝试 hover 新的创作: {sel}")
                loc = page.locator(sel).first
                loc.wait_for(state='visible', timeout=3000)
                loc.hover(timeout=3000)
                page.wait_for_timeout(1200)
                hovered = True
                self.log(f"悬停模式：hover 新的创作成功: {sel}")
                break
            except Exception as e:
                self.log(f"悬停模式：hover 新的创作失败: {sel}, err={e}")
                continue
        if not hovered:
            raise RuntimeError("悬停模式下未能定位新的创作")

        self.log("悬停模式：开始点击精确的“文章”入口")
        target_page = self._click_and_maybe_switch_page(page, [
            "[role='menuitem'] >> text=/^文章$/",
            "[role='menu'] >> text=/^文章$/",
            "text=/^文章$/",
            ":text-is('文章')",
            "text=写新文章",
            "button:has-text('写新文章')",
            "a:has-text('写新文章')",
            "li:has-text('写新文章')",
            "div:has-text('写新文章')",
        ], "step_03_new_article", "悬停模式下未找到文章入口")
        if self._is_article_template_page(target_page):
            raise RuntimeError(f"悬停模式误入文章模板页: url={target_page.url}")
        return target_page

    def _step_link(self, page):
        if self._is_link_editor_open(page):
            self.log("检测到当前已处于“编辑超链接”状态，无需重复点击")
            self._dump(page, "step_04_link")
            return

        clicked = self._click_any(page, [
            "text=超链接",
            "text=插入链接",
            "i[title='超链接']",
            "button[title='超链接']",
            "[aria-label='超链接']",
            "[class*='toolbar'] [class*='link']",
            "[class*='icon']:has-text('超链接')",
        ])
        page.wait_for_timeout(2000)

        if not clicked and not self._is_link_editor_open(page):
            raise RuntimeError("未找到超链接入口")
        if not self._is_link_editor_open(page):
            raise RuntimeError("点击超链接后未进入编辑超链接状态")

        self.log("超链接入口点击完成，已进入编辑超链接状态")
        self._dump(page, "step_04_link")

    def _is_article_template_page(self, page):
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if "appmsgtemplate" in url:
            return True
        try:
            body = self._body(page)
        except Exception:
            body = ""
        return "文章模板" in body and "模板示例" in body

    def _is_link_editor_open(self, page):
        checks = [
            "text=编辑超链接",
            "text=选择账号文章",
            "text=输入链接",
            "text=选择其他账号",
        ]
        hits = 0
        for sel in checks:
            try:
                if page.locator(sel).first.is_visible(timeout=800):
                    hits += 1
            except Exception:
                continue
        return hits >= 2

    def _step_other_account(self, page):
        if not self._click_any(page, [
            "text=选择其他账号",
            "text=其他账号",
            "text=账号",
            "text=公众号文章",
            ".tab:has-text('公众号文章')",
            ".weui-desktop-tab__item:has-text('公众号文章')",
            ".weui-desktop-dialog [class*='tab']:has-text('公众号文章')",
        ]):
            raise RuntimeError("未找到选择其他账号入口")
        page.wait_for_timeout(2500)
        self.log("选择其他账号 / 公众号文章 点击完成")
        self._dump(page, "step_05_other_account")

    def _on_request(self, request):
        try:
            url = request.url
            self.request_urls.append(url)
            if any(x in url for x in ["fingerprint=", "searchbiz", "appmsgpublish", "fakeid="]):
                self.interesting_requests.append(url)
                self.log(f"捕获关键请求: {url}")
            if "token=" in url and not self.discovered_token:
                m = re.search(r'token=(\d+)', url)
                if m:
                    self.discovered_token = m.group(1)
                    self.log(f"从请求中发现 token={self.discovered_token}")
            if "fingerprint=" in url and not self.discovered_fingerprint:
                m = re.search(r'fingerprint=([a-f0-9]{16,64})', url)
                if m:
                    self.discovered_fingerprint = m.group(1)
                    self.log(f"从请求中发现 fingerprint={self.discovered_fingerprint}")
        except Exception:
            pass

    def _on_response(self, response):
        try:
            url = response.url
            if any(x in url for x in ["searchbiz", "appmsgpublish", "fingerprint=", "fakeid="]):
                text = response.text()
                self.interesting_responses.append({
                    "url": url,
                    "status": response.status,
                    "body": text[:4000],
                })
                self.log(f"捕获关键响应: status={response.status}, url={url}")
        except Exception:
            pass

    def _extract_token(self, page):
        if self.discovered_token:
            self.log(f"直接使用已发现 token={self.discovered_token}")
            return self.discovered_token
        try:
            qs = parse_qs(urlparse(page.url).query)
            token = qs.get("token", [None])[0]
            if token:
                self.log(f"从 page.url 提取 token={token}")
                return token
        except Exception:
            pass
        html = page.content()
        m = re.search(r'"token"\s*:\s*"?(\d+)"?', html)
        if m:
            self.log(f"从 html 提取 token={m.group(1)}")
            return m.group(1)
        return None

    def _extract_fingerprint(self, page):
        if self.discovered_fingerprint:
            self.log(f"直接使用已发现 fingerprint={self.discovered_fingerprint}")
            return self.discovered_fingerprint
        html = page.content()
        m = re.search(r'fingerprint["\']?\s*[:=]\s*["\']([a-f0-9]{16,64})["\']', html)
        if m:
            self.log(f"从 html 变量提取 fingerprint={m.group(1)}")
            return m.group(1)
        m2 = re.search(r'fingerprint=([a-f0-9]{16,64})', html)
        if m2:
            self.log(f"从 html 文本提取 fingerprint={m2.group(1)}")
            return m2.group(1)
        return None

    def _extract_lang(self, page):
        try:
            qs = parse_qs(urlparse(page.url).query)
            lang = qs.get("lang", [None])[0]
            if lang:
                self.log(f"从 page.url 提取 lang={lang}")
                return lang
        except Exception:
            pass
        self.log("lang 未显式提取到，回退 zh_CN")
        return "zh_CN"

    def _click_any(self, page, selectors):
        for sel in selectors:
            try:
                self.log(f"普通点击尝试: {sel}")
                page.locator(sel).first.click(timeout=2500)
                self.log(f"普通点击成功: {sel}")
                return True
            except Exception as e:
                self.log(f"普通点击失败: {sel}, err={e}")
                continue
        return False

    def _body(self, page):
        try:
            return page.locator("body").inner_text(timeout=5000)
        except PlaywrightTimeoutError:
            return ""

    def _dump(self, page, name: str):
        if not self.debug_save_artifacts:
            return
        png = self.debug_dir / f"{name}.png"
        txt = self.debug_dir / f"{name}.txt"
        html = self.debug_dir / f"{name}.html"
        try:
            page.screenshot(path=str(png), full_page=True)
            self.log(f"已写截图: {png}")
        except Exception as e:
            self.log(f"截图失败: {name}, err={e}")
        try:
            txt.write_text(self._body(page)[:5000], encoding="utf-8")
            self.log(f"已写文本快照: {txt}")
        except Exception as e:
            self.log(f"文本快照失败: {name}, err={e}")
        try:
            html.write_text(page.content(), encoding="utf-8")
            self.log(f"已写 HTML 快照: {html}")
        except Exception as e:
            self.log(f"HTML 快照失败: {name}, err={e}")

    def _write_json_debug(self, filename: str, data: dict):
        if not self.debug_save_artifacts:
            return
        path = self.debug_dir / filename
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"已写 JSON 调试文件: {path}")

    def _flush_debug(self):
        if not self.debug_save_artifacts:
            return
        (self.debug_dir / "requests.log").write_text("\n".join(self.request_urls), encoding="utf-8")
        (self.debug_dir / "interesting_requests.log").write_text("\n".join(self.interesting_requests), encoding="utf-8")
        (self.debug_dir / "interesting_responses.json").write_text(
            json.dumps(self.interesting_responses, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.log(
            f"已刷新调试日志: requests={len(self.request_urls)}, interesting_requests={len(self.interesting_requests)}, interesting_responses={len(self.interesting_responses)}"
        )

    def _safe(self, text: str):
        return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in text)
