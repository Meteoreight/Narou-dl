#!/usr/bin/env python3
import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from tqdm import tqdm

NCODE_RE = re.compile(r"/(n\d{4,}[a-z]{1,2})(?:/|$)", re.IGNORECASE)
EP_PATH_RE = re.compile(r"^/(n\d{4,}[a-z]{1,2})/(\d+)/?$", re.IGNORECASE)

DEFAULT_UA = "narou-dl/0.1 (personal-use)"
API_ENDPOINT = "https://api.syosetu.com/novelapi/api/"


@dataclass
class Episode:
    index: int
    title: str
    url: str
    html_body: str


class HttpClient:
    def __init__(self, delay: float, timeout: int, retries: int, user_agent: str) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.retries = max(1, retries)
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _throttle(self) -> None:
        if self.delay <= 0:
            return
        elapsed = time.time() - self._last_request
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def get(self, url: str) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                backoff = min(5.0, 0.5 * (2 ** (attempt - 1)))
                time.sleep(backoff)
        assert last_error is not None
        raise last_error


def extract_ncode(url_or_ncode: str) -> str:
    raw = url_or_ncode.strip()
    if re.fullmatch(r"n\d{4,}[a-z]{1,2}", raw, re.IGNORECASE):
        return raw.lower()
    path = urlparse(raw).path or raw
    m = NCODE_RE.search(path + "/")
    if not m:
        raise ValueError(f"Unable to extract ncode from: {url_or_ncode}")
    return m.group(1).lower()


def normalize_index_url(ncode: str) -> str:
    return f"https://ncode.syosetu.com/{ncode}/"


def get_soup(client: HttpClient, url: str) -> BeautifulSoup:
    resp = client.get(url)
    return BeautifulSoup(resp.text, "lxml")


def parse_episode_urls(index_soup: BeautifulSoup, ncode: str, base_url: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    for a in index_soup.find_all("a", href=True):
        href = a["href"].strip()
        path = urlparse(href).path if "://" in href else href
        m = EP_PATH_RE.match(path)
        if not m or m.group(1).lower() != ncode.lower():
            continue
        abs_url = urljoin(base_url, path)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        urls.append(abs_url)

    def ep_no(u: str) -> int:
        m2 = re.search(rf"/{re.escape(ncode)}/(\d+)/", u, re.IGNORECASE)
        return int(m2.group(1)) if m2 else 10**18

    urls.sort(key=ep_no)
    return urls


def extract_episode(
    client: HttpClient,
    url: str,
    include_preface: bool,
    include_afterword: bool,
    default_title: str,
) -> Episode:
    soup = get_soup(client, url)

    subtitle = soup.select_one(".novel_subtitle")
    title = subtitle.get_text(strip=True) if subtitle else default_title

    parts: list[str] = []
    if include_preface:
        node = soup.select_one("#novel_p")
        if node:
            parts.append(node.decode_contents())
    node = soup.select_one("#novel_honbun")
    if node:
        parts.append(node.decode_contents())
    if include_afterword:
        node = soup.select_one("#novel_a")
        if node:
            parts.append(node.decode_contents())

    if parts:
        body_html = "\n<hr/>\n".join(parts)
    else:
        body_html = "<p>(no body found)</p>"

    m = re.search(r"/(\d+)/?$", url)
    idx = int(m.group(1)) if m else 1
    return Episode(index=idx, title=title, url=url, html_body=body_html)


def fetch_general_all_no(client: HttpClient, ncode: str) -> Optional[int]:
    params = {"out": "json", "ncode": ncode, "of": "ga"}
    try:
        resp = client.get(API_ENDPOINT + "?" + "&".join(f"{k}={v}" for k, v in params.items()))
        data = json.loads(resp.text)
    except Exception:
        return None
    if not isinstance(data, list) or len(data) < 2:
        return None
    record = data[1]
    if not isinstance(record, dict):
        return None
    value = record.get("general_all_no")
    if isinstance(value, int):
        return value
    return None


def build_css(vertical: bool) -> bytes:
    base = [
        "body { line-height: 1.8; }",
        "h1 { font-size: 1.2em; margin: 0 0 1em 0; }",
        "hr { border: none; border-top: 1px solid #ccc; margin: 1em 0; }",
        "ruby { ruby-position: over; }",
    ]
    if vertical:
        base.extend(
            [
                "body { writing-mode: vertical-rl; }",
                "body { text-orientation: mixed; }",
            ]
        )
    return ("\n".join(base) + "\n").encode("utf-8")


def build_epub(
    out_path: Path,
    identifier: str,
    book_title: str,
    author: str,
    episodes: Iterable[Episode],
    vertical: bool,
    language: str = "ja",
) -> None:
    book = epub.EpubBook()
    book.set_identifier(identifier)
    book.set_title(book_title)
    book.set_language(language)
    if author:
        book.add_author(author)

    css = epub.EpubItem(
        uid="style_base",
        file_name="style/style.css",
        media_type="text/css",
        content=build_css(vertical),
    )
    book.add_item(css)

    spine = ["nav"]
    toc = []

    for ep in episodes:
        chapter = epub.EpubHtml(
            title=f"{ep.index}. {ep.title}",
            file_name=f"chap_{ep.index:05d}.xhtml",
            lang=language,
        )
        chapter.content = (
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{language}">'
            f"<head><title>{ep.index}. {ep.title}</title>"
            f'<link rel="stylesheet" type="text/css" href="style/style.css" />'
            f"</head><body>"
            f"<h1>{ep.index}. {ep.title}</h1>"
            f"{ep.html_body}"
            f"</body></html>"
        ).encode("utf-8")

        book.add_item(chapter)
        toc.append(chapter)
        spine.append(chapter)

    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book, {})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a Narou novel and build an EPUB (personal use only)."
    )
    parser.add_argument("url", help="Work top page URL or ncode.")
    parser.add_argument("-o", "--out", default="out", help="Output directory.")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout (seconds).")
    parser.add_argument("--from-ep", type=int, default=None, help="Start episode number.")
    parser.add_argument("--to-ep", type=int, default=None, help="End episode number.")
    parser.add_argument("--retry", type=int, default=3, help="Retry count for fetch errors.")
    parser.add_argument("--user-agent", default=DEFAULT_UA, help="User-Agent string.")
    parser.add_argument("--vertical", action="store_true", help="Enable vertical writing CSS.")
    parser.add_argument("--no-preface", action="store_true", help="Exclude preface.")
    parser.add_argument("--no-afterword", action="store_true", help="Exclude afterword.")
    args = parser.parse_args()

    ncode = extract_ncode(args.url)
    index_url = normalize_index_url(ncode)
    base_url = "https://ncode.syosetu.com/"

    client = HttpClient(
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retry,
        user_agent=args.user_agent,
    )

    general_all_no = fetch_general_all_no(client, ncode)

    index_soup = get_soup(client, index_url)
    title_node = index_soup.select_one(".novel_title")
    author_node = index_soup.select_one(".novel_writername a")
    book_title = title_node.get_text(strip=True) if title_node else ncode
    author = author_node.get_text(strip=True) if author_node else ""

    episode_urls = parse_episode_urls(index_soup, ncode=ncode, base_url=base_url)
    if not episode_urls:
        episode_urls = [index_url]

    filtered_urls: list[str] = []
    for url in episode_urls:
        ep_no_m = re.search(rf"/{re.escape(ncode)}/(\d+)/", url, re.IGNORECASE)
        ep_no = int(ep_no_m.group(1)) if ep_no_m else 1
        if args.from_ep is not None and ep_no < args.from_ep:
            continue
        if args.to_ep is not None and ep_no > args.to_ep:
            continue
        filtered_urls.append(url)

    episodes: list[Episode] = []
    for url in tqdm(filtered_urls, unit="ep", desc="Fetching"):
        ep_no_m = re.search(rf"/{re.escape(ncode)}/(\d+)/", url, re.IGNORECASE)
        ep_no = int(ep_no_m.group(1)) if ep_no_m else 1
        episode = extract_episode(
            client,
            url,
            include_preface=not args.no_preface,
            include_afterword=not args.no_afterword,
            default_title=book_title,
        )
        episodes.append(episode)
        tqdm.write(f"fetched: {ep_no} {url}")

    if not episodes:
        raise SystemExit("No episodes matched the requested range.")

    out_dir = Path(args.out)
    out_file = out_dir / f"{ncode}.epub"
    build_epub(
        out_file,
        identifier=f"narou:{ncode}",
        book_title=book_title,
        author=author,
        episodes=episodes,
        vertical=args.vertical,
    )
    if general_all_no:
        print(f"episode count (API): {general_all_no}")
    print(f"written: {out_file}")


if __name__ == "__main__":
    main()
