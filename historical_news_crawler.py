"""Crawl Eastmoney historical news pages into SQLite.

Example:
    py -3 historical_news_crawler.py --channel cbkjj --start 2026-04-01 --end 2026-05-21 --max-pages 20

This crawler is intentionally conservative: one request every ~1.5 seconds,
retry on failures, and stop once list pages move older than the requested start
date.  It is for research/backtest data preparation only.
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from news_store import connect, stats, upsert_article


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

CHANNELS = {
    # 用户提到的东方财富「板块聚焦」。
    "cbkjj": {
        "name": "东方财富-板块聚焦",
        "base": "https://stock.eastmoney.com/a/cbkjj",
    },
    # 常用财经焦点页，后续可按同样规则扩展。
    "cjsd": {
        "name": "东方财富-财经视点",
        "base": "https://finance.eastmoney.com/a/cjdd",
    },
    # 东方财富结构化资讯流，比静态分页更适合历史回测采集。
    "np350": {
        "name": "东方财富-财经资讯流",
        "kind": "np_listapi",
        "column": "350",
    },
}


@dataclass
class LinkItem:
    title: str
    url: str


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[LinkItem] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_d = {k.lower(): v for k, v in attrs if v}
        href = attrs_d.get("href", "")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        title = " ".join("".join(self._text).split())
        if title and _is_article_url(self._href):
            self.links.append(LinkItem(title=html.unescape(title), url=self._href))
        self._href = None
        self._text = []


def _is_article_url(url: str) -> bool:
    return bool(re.search(r"/a/\d{12,}\.html", url))


def _list_url(base: str, page: int) -> str:
    if page <= 1:
        return f"{base}.html"
    return f"{base}_{page}.html"


def _decode_response(raw: bytes, headers: Any) -> str:
    content_type = headers.get("Content-Type", "") if headers else ""
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    encodings = [m.group(1)] if m else []
    encodings.extend(["utf-8", "gb18030", "gbk"])
    for enc in encodings:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def fetch_text(url: str, *, retries: int = 3, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _decode_response(resp.read(), resp.headers)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(1.5 + attempt * 1.2)
    raise RuntimeError(f"fetch failed: {url} ({last_error})")


def _np_listapi_url(column: str, page: int, page_size: int) -> str:
    query = urllib.parse.urlencode({
        "client": "web",
        "biz": "web_news_col",
        "column": column,
        "page_index": page,
        "page_size": page_size,
        "req_trace": str(int(time.time() * 1000)),
    })
    return f"https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?{query}"


def _parse_np_listapi_items(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if str(data.get("code")) not in {"0", "1"}:
        raise RuntimeError(f"listapi failed: {data.get('message')}")
    items = ((data.get("data") or {}).get("list") or [])
    out = []
    for item in items:
        title = str(item.get("title") or "").strip()
        url = str(item.get("uniqueUrl") or item.get("url") or "").strip()
        show_time = str(item.get("showTime") or "").strip()
        if not title or not url or not show_time:
            continue
        out.append({
            "url": url,
            "title": title,
            "publish_time": show_time,
            "source": str(item.get("mediaName") or "东方财富").strip(),
            "content": str(item.get("summary") or "").strip(),
        })
    return out


def parse_list_links(list_html: str, list_url: str) -> list[LinkItem]:
    parser = LinkParser()
    parser.feed(list_html)
    out: list[LinkItem] = []
    seen: set[str] = set()
    for item in parser.links:
        url = urllib.parse.urljoin(list_url, item.url)
        if url in seen:
            continue
        seen.add(url)
        out.append(LinkItem(title=item.title, url=url))
    return out


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", fragment, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _paragraph_text(fragment: str) -> str:
    parts = []
    for m in re.finditer(r"<p[^>]*>([\s\S]*?)</p>", fragment, flags=re.I):
        text = _strip_tags(m.group(1))
        if len(text) >= 8:
            parts.append(text)
    return " ".join(parts).strip()


def _clean_content(text: str) -> str:
    stop_phrases = (
        "郑重声明",
        "东方财富网发布此信息",
        "风险自担",
        "文章来源",
        "责任编辑",
    )
    for phrase in stop_phrases:
        idx = text.find(phrase)
        if idx >= 0:
            text = text[:idx]
    return re.sub(r"\s+", " ", text).strip()


def _parse_publish_time(page_html: str) -> str:
    patterns = (
        r"(\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2})",
        r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)",
        r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)",
    )
    for pattern in patterns:
        m = re.search(pattern, page_html)
        if not m:
            continue
        raw = m.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        raw = raw.replace("/", "-")
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                return datetime.strptime(raw, "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    return ""


def _parse_title(page_html: str, fallback: str) -> str:
    for pattern in (r"<h1[^>]*>([\s\S]*?)</h1>", r"<title[^>]*>([\s\S]*?)</title>"):
        m = re.search(pattern, page_html, flags=re.I)
        if m:
            title = _strip_tags(m.group(1))
            title = re.sub(r"[_\-].*东方财富.*$", "", title).strip()
            if title:
                return title
    return fallback


def _parse_source(page_html: str) -> str:
    text = _strip_tags(page_html[:6000])
    m = re.search(r"来源[:：]\s*([^\s]+)", text)
    if m:
        return m.group(1)[:40]
    return "东方财富"


def _parse_content(page_html: str) -> str:
    candidates = []
    for pattern in (
        r"<div[^>]+id=[\"']ContentBody[\"'][^>]*>([\s\S]*?)(?:<div\s+class=[\"']em_media|<div\s+class=[\"']res-edit|</div>\s*</div>\s*</div>)",
        r"<div[^>]+class=[\"'][^\"']*(?:txtinfos|article|content)[^\"']*[\"'][^>]*>([\s\S]*?)(?:<div\s+class=[\"']em_media|<div\s+class=[\"']res-edit|</div>\s*</div>\s*</div>)",
        r"<article[^>]*>([\s\S]*?)</article>",
    ):
        for m in re.finditer(pattern, page_html, flags=re.I):
            body = m.group(1)
            text = _paragraph_text(body) or _strip_tags(body)
            text = _clean_content(text)
            if len(text) > 80:
                candidates.append(text)
    if candidates:
        return max(candidates, key=len)

    # Fallback: use title-level matching rather than polluting the signal with
    # navigation, related links, and disclaimer text from the whole page.
    return ""


def parse_article(page_html: str, *, fallback_title: str, url: str, channel: str) -> dict[str, Any]:
    return {
        "url": url,
        "title": _parse_title(page_html, fallback_title),
        "publish_time": _parse_publish_time(page_html),
        "source": _parse_source(page_html),
        "channel": channel,
        "content": _parse_content(page_html),
    }


def crawl_channel(
    channel: str,
    *,
    start_date: str,
    end_date: str,
    max_pages: int,
    start_page: int,
    sleep_seconds: float,
    page_size: int = 50,
) -> dict[str, int]:
    cfg = CHANNELS[channel]
    if cfg.get("kind") == "np_listapi":
        return crawl_np_listapi(
            cfg,
            start_date=start_date,
            end_date=end_date,
            max_pages=max_pages,
            start_page=start_page,
            sleep_seconds=sleep_seconds,
            page_size=page_size,
        )

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    counters = {"pages": 0, "links": 0, "saved": 0, "skipped_old": 0, "duplicates": 0, "failed": 0}
    seen_urls: set[str] = set()
    seen_page_signatures: set[tuple[str, ...]] = set()
    duplicate_page_streak = 0

    with connect() as conn:
        for page in range(start_page, start_page + max_pages):
            list_url = _list_url(cfg["base"], page)
            print(f"[list] {page}: {list_url}", flush=True)
            try:
                list_html = fetch_text(list_url)
            except Exception as exc:
                counters["failed"] += 1
                print(f"  ! list failed: {exc}", flush=True)
                continue

            counters["pages"] += 1
            links = parse_list_links(list_html, list_url)
            counters["links"] += len(links)
            if not links:
                print("  no links, stop", flush=True)
                break

            page_signature = tuple(sorted(link.url for link in links[:20]))
            if page_signature in seen_page_signatures:
                duplicate_page_streak += 1
            else:
                duplicate_page_streak = 0
                seen_page_signatures.add(page_signature)
            if duplicate_page_streak >= 2:
                print("  repeated list pages detected, stop", flush=True)
                break

            page_old_count = 0
            page_duplicate_count = 0
            for link in links:
                if link.url in seen_urls:
                    page_duplicate_count += 1
                    counters["duplicates"] += 1
                    continue
                seen_urls.add(link.url)

                time.sleep(sleep_seconds + random.random() * 0.6)
                try:
                    page_html = fetch_text(link.url)
                    article = parse_article(page_html, fallback_title=link.title, url=link.url, channel=cfg["name"])
                    published = article.get("publish_time") or ""
                    if published:
                        published_dt = datetime.strptime(published, "%Y-%m-%d %H:%M:%S")
                        if published_dt < start_dt:
                            page_old_count += 1
                            counters["skipped_old"] += 1
                            continue
                        if published_dt > end_dt:
                            continue
                    if upsert_article(conn, article):
                        counters["saved"] += 1
                        print(f"  saved {published or 'unknown'} {article['title'][:42]}", flush=True)
                    else:
                        page_duplicate_count += 1
                        counters["duplicates"] += 1
                    conn.commit()
                except Exception as exc:
                    counters["failed"] += 1
                    print(f"  ! article failed: {link.url} ({exc})", flush=True)

            if page_old_count >= max(3, len(links) // 2):
                print("  list page is mostly older than start date, stop", flush=True)
                break
            if page_duplicate_count >= max(3, len(links) // 2):
                duplicate_page_streak += 1
                if duplicate_page_streak >= 2:
                    print("  list page is mostly duplicate, stop", flush=True)
                    break

            time.sleep(sleep_seconds)

    return counters


def crawl_np_listapi(
    cfg: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
    max_pages: int,
    start_page: int,
    sleep_seconds: float,
    page_size: int,
) -> dict[str, int]:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    counters = {"pages": 0, "links": 0, "saved": 0, "skipped_old": 0, "duplicates": 0, "failed": 0}
    seen_urls: set[str] = set()

    with connect() as conn:
        for page in range(start_page, start_page + max_pages):
            list_url = _np_listapi_url(str(cfg["column"]), page, page_size)
            print(f"[api] {page}: column={cfg['column']}", flush=True)
            try:
                items = _parse_np_listapi_items(fetch_text(list_url))
            except Exception as exc:
                counters["failed"] += 1
                print(f"  ! api failed: {exc}", flush=True)
                time.sleep(sleep_seconds)
                continue

            counters["pages"] += 1
            counters["links"] += len(items)
            if not items:
                print("  no items, stop", flush=True)
                break

            page_old_count = 0
            page_saved_count = 0
            for item in items:
                url = item["url"]
                if url in seen_urls:
                    counters["duplicates"] += 1
                    continue
                seen_urls.add(url)

                try:
                    published_dt = datetime.strptime(item["publish_time"], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    counters["failed"] += 1
                    continue

                if published_dt > end_dt:
                    continue
                if published_dt < start_dt:
                    page_old_count += 1
                    counters["skipped_old"] += 1
                    continue

                article = {
                    **item,
                    "channel": cfg["name"],
                }
                if upsert_article(conn, article):
                    counters["saved"] += 1
                    page_saved_count += 1
                    print(f"  saved {item['publish_time']} {item['title'][:42]}", flush=True)
                else:
                    counters["duplicates"] += 1
                conn.commit()

            if page_old_count >= max(3, len(items) // 2):
                print("  api page is mostly older than start date, stop", flush=True)
                break
            if page_saved_count == 0 and page_old_count == 0:
                print("  no in-range items on this page", flush=True)

            time.sleep(sleep_seconds + random.random() * 0.3)

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl Eastmoney historical news into SQLite.")
    parser.add_argument("--channel", choices=sorted(CHANNELS), default="cbkjj")
    parser.add_argument("--start", required=True, help="Start date, e.g. 2026-04-01")
    parser.add_argument("--end", required=True, help="End date, e.g. 2026-05-21")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--sleep", type=float, default=1.2)
    args = parser.parse_args()

    counters = crawl_channel(
        args.channel,
        start_date=args.start,
        end_date=args.end,
        max_pages=args.max_pages,
        start_page=args.start_page,
        page_size=args.page_size,
        sleep_seconds=args.sleep,
    )
    print("\nCrawl summary:", counters)
    print("DB stats:", stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
