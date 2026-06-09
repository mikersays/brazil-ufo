#!/usr/bin/env python3
"""
Brazilian UFO Archives — post extractor.

Pulls every published post from the WordPress.com public REST API for
brazilianufoarchives.wordpress.com, then writes each post to an organized
folder on disk together with its photos (downloaded at full resolution).

Output layout:

    data/
      index.json                         # catalog of all posts
      posts/
        0001-<slug>/
          post.json                      # raw API object
          content.html                   # cleaned HTML, <img> rewritten to local files
          post.md                        # plain-text / markdown rendering of the body
          images/
            01-<name>.jpg ...            # full-res photos referenced by content.html

Usage:
    python3 tools/extract.py                # full run
    python3 tools/extract.py --limit 10     # quick test run
    python3 tools/extract.py --no-images    # metadata only, skip downloads
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

SITE = "brazilianufoarchives.wordpress.com"
API = f"https://public-api.wordpress.com/rest/v1.1/sites/{SITE}/posts/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
POSTS_DIR = os.path.join(DATA, "posts")

UA = "brazil-ufo-archiver/1.0 (+local extraction tool)"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http_get(url: str, *, binary: bool = False, retries: int = 4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} :: {last}")


def fetch_all_posts(limit: int | None = None) -> list[dict]:
    """Page through the API and return all published posts (newest API order)."""
    posts: list[dict] = []
    page = 1
    per_page = 100
    while True:
        q = urllib.parse.urlencode(
            {"number": per_page, "page": page, "status": "publish", "order_by": "date", "order": "ASC"}
        )
        payload = json.loads(http_get(f"{API}?{q}"))
        batch = payload.get("posts", [])
        if not batch:
            break
        posts.extend(batch)
        found = payload.get("found", 0)
        print(f"  fetched page {page}: +{len(batch)} (total {len(posts)}/{found})", flush=True)
        if limit and len(posts) >= limit:
            posts = posts[:limit]
            break
        if len(posts) >= found:
            break
        page += 1
    return posts


# --------------------------------------------------------------------------- #
# Text / HTML utilities
# --------------------------------------------------------------------------- #
def slugify(text: str, fallback: str = "post") -> str:
    text = html.unescape(text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text or fallback)[:70]


IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*"([^"]+)"[^>]*>', re.IGNORECASE)


def original_image_url(src: str) -> str:
    """Strip resize query (?w=&h=) to obtain the full-resolution original."""
    src = html.unescape(src)
    parsed = urllib.parse.urlsplit(src)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def image_filename(idx: int, url: str) -> str:
    name = os.path.basename(urllib.parse.urlsplit(url).path) or f"image-{idx}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    if "." not in name:
        name += ".jpg"
    return f"{idx:02d}-{name}"


class _TextRender(HTMLParser):
    """Very small HTML -> readable text converter for post.md."""

    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("li",):
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("## ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self.parts))
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(content_html: str) -> str:
    parser = _TextRender()
    try:
        parser.feed(content_html)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", content_html)
    return parser.text()


# --------------------------------------------------------------------------- #
# Per-post processing
# --------------------------------------------------------------------------- #
def process_post(idx: int, post: dict, *, download_images: bool) -> dict | None:
    slug = post.get("slug") or slugify(post.get("title", ""))
    if slug.startswith("__trashed"):
        return None

    folder_name = f"{idx:04d}-{slugify(slug)}"
    folder = os.path.join(POSTS_DIR, folder_name)
    img_dir = os.path.join(folder, "images")
    os.makedirs(img_dir, exist_ok=True)

    content_html = post.get("content", "") or ""

    # Collect unique images (original resolution), preserving order of appearance.
    seen: dict[str, str] = {}      # original_url -> local relative path
    rewrite: dict[str, str] = {}   # raw_src_in_html -> local relative path
    ordered: list[tuple[str, str]] = []  # (original_url, local_rel)
    for raw_src in IMG_SRC_RE.findall(content_html):
        orig = original_image_url(raw_src)
        if not orig.lower().split("?")[0].endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff")
        ):
            # Skip emoji/spacer/svg trackers; still rewrite if already seen.
            if orig in seen:
                rewrite[raw_src] = seen[orig]
            continue
        if orig not in seen:
            local_rel = os.path.join("images", image_filename(len(ordered) + 1, orig))
            seen[orig] = local_rel
            ordered.append((orig, local_rel))
        rewrite[raw_src] = seen[orig]

    # Download images.
    downloaded: list[str] = []
    if download_images:
        for orig, local_rel in ordered:
            dest = os.path.join(folder, local_rel)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                downloaded.append(local_rel)
                continue
            try:
                blob = http_get(orig, binary=True)
                with open(dest, "wb") as fh:
                    fh.write(blob)
                downloaded.append(local_rel)
            except Exception as e:  # noqa: BLE001
                print(f"    [warn] image failed {orig}: {e}", flush=True)

    # Rewrite content so <img src> points at the local files.
    local_html = content_html
    for raw_src, local_rel in rewrite.items():
        local_html = local_html.replace(f'"{raw_src}"', f'"{local_rel}"')

    title = html.unescape(post.get("title", "") or "")
    body_text = html_to_text(content_html)

    record = {
        "index": idx,
        "id": post.get("ID"),
        "folder": folder_name,
        "slug": slug,
        "title": title,
        "date": post.get("date"),
        "modified": post.get("modified"),
        "url": post.get("URL"),
        "categories": sorted((post.get("categories") or {}).keys()),
        "tags": sorted((post.get("tags") or {}).keys()),
        "excerpt": html_to_text(post.get("excerpt", "") or "")[:400],
        "image_count": len(downloaded if download_images else ordered),
        "images": (downloaded if download_images else [r for _, r in ordered]),
        "thumbnail": (downloaded[0] if download_images and downloaded else
                      (ordered[0][1] if ordered else None)),
        "word_count": len(body_text.split()),
        "text_preview": body_text[:600],
    }

    # Persist files.
    with open(os.path.join(folder, "post.json"), "w") as fh:
        json.dump(post, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(folder, "content.html"), "w") as fh:
        fh.write(local_html)
    with open(os.path.join(folder, "post.md"), "w") as fh:
        fh.write(f"# {title}\n\n*{post.get('date','')}*\n\n{body_text}\n")
    with open(os.path.join(folder, "meta.json"), "w") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)

    return record


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Extract Brazilian UFO Archives posts.")
    ap.add_argument("--limit", type=int, default=None, help="only process N posts (test)")
    ap.add_argument("--no-images", action="store_true", help="skip image downloads")
    ap.add_argument("--workers", type=int, default=8, help="parallel post workers")
    args = ap.parse_args()

    os.makedirs(POSTS_DIR, exist_ok=True)
    print(f"Fetching post list from {SITE} ...", flush=True)
    posts = fetch_all_posts(limit=args.limit)
    print(f"Got {len(posts)} posts. Processing (images={'no' if args.no_images else 'yes'}) ...", flush=True)

    records: list[dict] = []
    download = not args.no_images
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_post, i + 1, p, download_images=download): i
            for i, p in enumerate(posts)
        }
        done = 0
        for fut in cf.as_completed(futures):
            rec = fut.result()
            done += 1
            if rec:
                records.append(rec)
            if done % 25 == 0 or done == len(posts):
                print(f"  processed {done}/{len(posts)}", flush=True)

    records.sort(key=lambda r: r["index"])
    total_images = sum(r["image_count"] for r in records)
    index = {
        "site": SITE,
        "source": "https://brazilianufoarchives.com/",
        "generated_with": "tools/extract.py",
        "post_count": len(records),
        "image_count": total_images,
        "posts": [
            {k: r[k] for k in (
                "index", "id", "folder", "slug", "title", "date", "url",
                "categories", "tags", "image_count", "thumbnail",
                "word_count", "text_preview", "excerpt",
            )}
            for r in records
        ],
    }
    with open(os.path.join(DATA, "index.json"), "w") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(records)} posts, {total_images} images.")
    print(f"Index: {os.path.join(DATA, 'index.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
