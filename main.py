import ipaddress
import re
import socket
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

app = FastAPI(title="my-reader")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; my-reader/1.0)"}


def check_url(url: str) -> None:
    """ป้องกัน SSRF: อนุญาตเฉพาะ http/https และไม่ยิงเข้า IP ภายใน"""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "URL ต้องขึ้นต้นด้วย http:// หรือ https://")
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(400, "ไม่อนุญาตให้เข้าถึง IP ภายใน")
    except socket.gaierror:
        raise HTTPException(400, "ไม่พบโดเมนนี้")


async def fetch(url: str, timeout: float = 10) -> str:
    check_url(url)
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=timeout, follow_redirects=True
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"เว็บปลายทางตอบ {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ดึงข้อมูลไม่สำเร็จ: {e}")


IMG_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif|bmp)(?:[?#].*)?$", re.I)
# ค้น URL รูปจาก HTML/สคริปต์ดิบ (กรณีรูปถูกใส่ผ่าน JS)
IMG_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()\\]+?\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s"'<>()\\]*)?"""
    r"""|(?<=["'])/?[\w\-./%]+?\.(?:jpe?g|png|gif|webp|avif)(?=["'])""",
    re.I,
)


def natural_sort_key(s: str) -> list:
    """แปลงข้อความเป็นคีย์สำหรับเรียงลำดับแบบตัวเลขธรรมชาติ (natural sort: เช่น 2 มาก่อน 10)"""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def get_image_group_info(url: str) -> tuple[str, str, str]:
    """วิเคราะห์ URL รูปภาพเพื่อหากลุ่ม
    คืนค่า (key, display_name, pattern)
    """
    p = urlparse(url)
    path = p.path
    # หาตัวเลขก่อนนามสกุลไฟล์ เช่น ...-055520-001.jpg หรือ /01.png
    m = re.search(r"^(.*?)([-_./]?\d+)(\.[a-zA-Z0-9]+(?:[?#].*)?)$", path)
    if m:
        prefix = m.group(1)
        ext = m.group(3)
        name_part = prefix.rstrip("/").split("/")[-1] if prefix.rstrip("/") else path
        clean_name = name_part.strip("-_") or name_part
        key = f"{p.scheme}://{p.netloc}{prefix}*{ext}"
        return key, clean_name, clean_name

    dir_path = path.rsplit("/", 1)[0] if "/" in path else path
    dir_name = dir_path.rstrip("/").split("/")[-1] if dir_path.rstrip("/") else "other"
    key = f"{p.scheme}://{p.netloc}{dir_path}/"
    return key, dir_name, dir_name


def group_and_sort_images(
    images: list[dict], pattern: Optional[str] = None, group: Optional[str] = None
) -> tuple[list[dict], list[dict]]:
    """จัดกลุ่มรูปภาพตาม URL pattern/prefix พร้อมเรียงลำดับ natural sort
    คืนค่า (sorted_images, groups_summary)
    """
    grouped_map: dict[str, dict] = {}
    for item in images:
        u = item["url"]
        key, name, pat = get_image_group_info(u)
        if key not in grouped_map:
            grouped_map[key] = {
                "name": name,
                "pattern": pat,
                "key": key,
                "sample": u,
                "images": [],
            }
        grouped_map[key]["images"].append(item)

    # เรียงลำดับรูปภาพในแต่ละกลุ่มตาม natural sort
    group_list = []
    for g in grouped_map.values():
        g["images"].sort(key=lambda x: natural_sort_key(x["url"]))
        g["count"] = len(g["images"])
        group_list.append(g)

    # เรียงกลุ่มตามจำนวนรูปมาก -> น้อย (กลุ่มหลักที่มีรูปเยอะสุดจะอยู่แรก)
    group_list.sort(key=lambda g: g["count"], reverse=True)

    groups_summary = [
        {"name": g["name"], "pattern": g["pattern"], "count": g["count"], "sample": g["sample"]}
        for g in group_list
    ]

    # กรณีระบุ pattern (regex)
    if pattern:
        filtered = [img for img in images if re.search(pattern, img["url"])]
        filtered.sort(key=lambda x: natural_sort_key(x["url"]))
        return filtered, groups_summary

    # กรณีระบุ group (เช่น group=main หรือระบุชื่อกลุ่ม)
    if group:
        if group.lower() in ("main", "primary", "largest") and group_list:
            return group_list[0]["images"], groups_summary
        for g in group_list:
            if group.lower() in g["name"].lower() or group.lower() in g["pattern"].lower():
                return g["images"], groups_summary

    # ค่าเริ่มต้น: รวมทุกกลุ่มโดยนำกลุ่มหลักขึ้นก่อน และในแต่ละกลุ่มเรียงเลขหน้า
    all_sorted = []
    for g in group_list:
        all_sorted.extend(g["images"])

    return all_sorted, groups_summary


def extract_images(
    soup: BeautifulSoup, base_url: str, html: str = "", pattern: Optional[str] = None
) -> list[dict]:
    """ดึง URL รูปจาก <img> (รวม lazy-load/srcset), <picture><source> และ og:image"""
    found: dict[str, str] = {}

    def add(src: Optional[str], alt: str = "") -> None:
        if not src or src.startswith("data:"):
            return
        full = urljoin(base_url, src.strip())
        alt_str = alt.strip() if alt else ""
        if full not in found or (found[full] in ("", "og:image") and alt_str and alt_str != "og:image"):
            found[full] = alt_str

    for meta in soup.find_all("meta", attrs={"property": "og:image"}):
        add(meta.get("content"), "og:image")

    for img in soup.find_all("img"):
        alt = img.get("alt", "")
        # lazy-load แต่ละเว็บใช้ชื่อ attribute ต่างกัน: เช็คทุก attribute ที่ดูเป็น URL รูป
        for attr, val in img.attrs.items():
            if isinstance(val, str) and (
                attr in ("src", "data-src", "data-original", "data-lazy-src")
                or (IMG_RE.search(val.strip()) and "," not in val)
            ):
                add(val, alt)
        for attr in ("srcset", "data-srcset"):
            for part in (img.get(attr) or "").split(","):
                if part.strip():
                    add(part.strip().split()[0], alt)

    for source in soup.select("picture source"):
        for part in (source.get("srcset") or "").split(","):
            if part.strip():
                add(part.strip().split()[0])

    for m in IMG_URL_RE.finditer(html.replace("\\/", "/")):
        add(m.group(0))

    result = [{"url": u, "alt": a} for u, a in found.items()]
    if pattern:
        result = [r for r in result if re.search(pattern, r["url"])]
    return result


@app.get("/scrape")
async def scrape(
    url: str = Query(..., description="URL ของเว็บที่ต้องการดึง"),
    selector: Optional[str] = Query(None, description="CSS selector เช่น h2.title"),
    pattern: Optional[str] = Query(None, description="regex กรอง URL รูปภาพ เช่น caisson"),
    group: Optional[str] = Query(None, description="กรองตามชื่อกลุ่ม หรือใส่ 'main' เพื่อเลือกกลุ่มหลัก"),
):
    """ดึง title, description, หัวข้อ, ลิงก์ และรูปภาพที่จัดกลุ่มและเรียงลำดับแล้ว"""
    html = await fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    if selector:
        return {
            "url": url,
            "selector": selector,
            "results": [el.get_text(" ", strip=True) for el in soup.select(selector)],
        }

    raw_images = extract_images(soup, url, html)
    sorted_images, groups = group_and_sort_images(raw_images, pattern=pattern, group=group)

    desc = soup.find("meta", attrs={"name": "description"})
    return {
        "url": url,
        "title": soup.title.string.strip() if soup.title and soup.title.string else None,
        "description": desc.get("content") if desc else None,
        "headings": [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])],
        "groups": groups,
        "images": sorted_images[:100],
        "links": [
            {"text": a.get_text(strip=True), "href": urljoin(url, a["href"])}
            for a in soup.find_all("a", href=True)
        ][:100],
    }


@app.get("/images")
async def images(
    url: str = Query(..., description="URL ของเว็บที่ต้องการดึงรูป"),
    pattern: Optional[str] = Query(None, description="regex กรอง URL เช่น caisson"),
    group: Optional[str] = Query(None, description="กรองตามชื่อกลุ่ม หรือใส่ 'main' เพื่อเลือกกลุ่มหลัก"),
):
    """ดึง URL รูปภาพทั้งหมดในหน้าเว็บ พร้อมจัดกลุ่มและเรียงลำดับหน้า"""
    html = await fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    raw_imgs = extract_images(soup, url, html)
    sorted_imgs, groups = group_and_sort_images(raw_imgs, pattern=pattern, group=group)
    return {"url": url, "count": len(sorted_imgs), "groups": groups, "images": sorted_imgs}


@app.get("/proxy")
async def proxy(url: str = Query(..., description="URL รูปที่ต้องการโหลดผ่านเซิร์ฟเวอร์")):
    """โหลดรูปผ่านเซิร์ฟเวอร์ พร้อมใส่ Referer (เลี่ยงเว็บที่บล็อก hotlink)"""
    check_url(url)
    p = urlparse(url)
    headers = {**HEADERS, "Referer": f"{p.scheme}://{p.netloc}/"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=20) as client:
            r = await client.get(url)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"โหลดรูปไม่สำเร็จ: {e}")
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/"):
        raise HTTPException(400, "URL นี้ไม่ใช่รูปภาพ")
    return Response(
        r.content, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"}
    )


NEXT_TEXT = re.compile(
    r"next|ตอนถัดไป|ตอนต่อไป|ถัดไป|ต่อไป|^\s*[>›»→▶]+\s*$", re.I
)


@app.get("/next")
async def next_chapter(url: str = Query(..., description="URL หน้าตอนปัจจุบัน")):
    """หา URL ตอนถัดไป: rel=next → ลิงก์ที่ข้อความเป็น next/ถัดไป → เลขท้าย URL +1"""
    soup = BeautifulSoup(await fetch(url), "html.parser")

    def absolute(href: str) -> str:
        return urljoin(url, href)

    def valid(href: Optional[str]) -> bool:
        return bool(href) and not href.startswith(("#", "javascript:")) and absolute(href) != url

    for tag in soup.find_all(["a", "link"], rel=True, href=True):
        if "next" in [r.lower() for r in tag["rel"]] and valid(tag["href"]):
            return {"next": absolute(tag["href"]), "method": "rel=next"}

    for a in soup.find_all("a", href=True):
        label = " ".join(
            [a.get_text(" ", strip=True), a.get("title", ""), a.get("aria-label", "")]
            + a.get("class", [])
        )
        if NEXT_TEXT.search(label) and valid(a["href"]):
            return {"next": absolute(a["href"]), "method": "link text"}

    # สำรอง: เพิ่มเลขตอนท้าย URL เช่น .../bleach-232 → .../bleach-233
    m = list(re.finditer(r"\d+", urlparse(url).path))
    if m:
        last = m[-1]
        p = urlparse(url)
        num = str(int(last.group()) + 1).zfill(len(last.group()))
        path = p.path[: last.start()] + num + p.path[last.end() :]
        return {"next": p._replace(path=path).geturl(), "method": "url number +1"}

    raise HTTPException(404, "ไม่พบตอนถัดไป")


PREV_TEXT = re.compile(
    r"prev|previous|\u0e01\u0e48\u0e2d\u0e19\u0e2b\u0e19\u0e49\u0e32|\u0e22\u0e49\u0e2d\u0e19\u0e01\u0e25\u0e31\u0e1a|^\s*[<\u00ab\u2039]+\s*$",
    re.I,
)


@app.get("/prev")
async def prev_chapter(url: str = Query(..., description="URL of current chapter")):
    """Find previous chapter URL: rel=prev, then link text, then URL number -1."""
    soup = BeautifulSoup(await fetch(url), "html.parser")

    def absolute(href: str) -> str:
        return urljoin(url, href)

    def valid(href: Optional[str]) -> bool:
        return bool(href) and not href.startswith(("#", "javascript:")) and absolute(href) != url

    for tag in soup.find_all(["a", "link"], rel=True, href=True):
        if {"prev", "previous"} & {r.lower() for r in tag["rel"]} and valid(tag["href"]):
            return {"prev": absolute(tag["href"]), "method": "rel=prev"}

    for a in soup.find_all("a", href=True):
        label = " ".join(
            [a.get_text(" ", strip=True), a.get("title", ""), a.get("aria-label", "")]
            + a.get("class", [])
        )
        if PREV_TEXT.search(label) and valid(a["href"]):
            return {"prev": absolute(a["href"]), "method": "link text"}

    m = list(re.finditer(r"\d+", urlparse(url).path))
    if m:
        last = m[-1]
        if int(last.group()) > 0:
            p = urlparse(url)
            num = str(int(last.group()) - 1).zfill(len(last.group()))
            path = p.path[: last.start()] + num + p.path[last.end() :]
            return {"prev": p._replace(path=path).geturl(), "method": "url number -1"}

    raise HTTPException(404, "Previous chapter not found")


SITE_PROFILES = {
    "asianovel.net": {
        "content": "#chapter-content .chapter-formatting",
        "next": "a._navigation._next",
        "prev": "a._navigation._prev",
    },
}
JUNK_TAGS = ["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "iframe", "button"]
TEXT_CACHE: dict[str, tuple[float, dict]] = {}
TEXT_CACHE_TTL = 600


def get_profile(url: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    for domain, prof in SITE_PROFILES.items():
        if host == domain or host.endswith("." + domain):
            return prof
    return {}


def guess_content(soup: BeautifulSoup):
    """หา element ที่มีข้อความย่อหน้ามากที่สุด (heuristic แบบ readability)"""
    for t in soup(JUNK_TAGS):
        t.decompose()
    scores: dict[int, tuple[int, object]] = {}
    for p in soup.find_all("p"):
        n = len(p.get_text(strip=True))
        parent = p.parent
        if parent is None:
            continue
        cur = scores.get(id(parent), (0, parent))
        scores[id(parent)] = (cur[0] + n, parent)
    return max(scores.values(), key=lambda x: x[0])[1] if scores else None


def nav_by_selector(soup: BeautifulSoup, url: str, selector: Optional[str]) -> Optional[str]:
    if not selector:
        return None
    el = soup.select_one(selector)
    href = el.get("href") if el else None
    if href and not href.startswith(("#", "javascript:")):
        return urljoin(url, href)
    return None


def nav_generic(soup: BeautifulSoup, url: str, rels: set, pattern: re.Pattern) -> Optional[str]:
    for tag in soup.find_all(["a", "link"], rel=True, href=True):
        if rels & {r.lower() for r in tag["rel"]} and urljoin(url, tag["href"]) != url:
            return urljoin(url, tag["href"])
    for a in soup.find_all("a", href=True):
        label = " ".join([a.get_text(" ", strip=True), a.get("title", "")] + a.get("class", []))
        href = a["href"]
        if pattern.search(label) and not href.startswith(("#", "javascript:")) and urljoin(url, href) != url:
            return urljoin(url, href)
    return None


@app.get("/text")
async def text(
    url: str = Query(..., description="URL หน้าตอนนิยาย"),
    selector: Optional[str] = Query(None, description="CSS selector ของเนื้อหา (ไม่ใส่ = อัตโนมัติ)"),
):
    """ดึงเนื้อหานิยาย: title, ย่อหน้า, ลิงก์ตอนก่อนหน้า/ถัดไป"""
    key = f"{url}|{selector or ''}"
    hit = TEXT_CACHE.get(key)
    if hit and time.time() - hit[0] < TEXT_CACHE_TTL:
        return hit[1]

    html = await fetch(url, timeout=40)
    soup = BeautifulSoup(html, "html.parser")
    prof = get_profile(url)

    nxt = nav_by_selector(soup, url, prof.get("next")) or nav_generic(soup, url, {"next"}, NEXT_TEXT)
    prv = nav_by_selector(soup, url, prof.get("prev")) or nav_generic(
        soup, url, {"prev", "previous"}, PREV_TEXT
    )

    story_title = None
    meta = soup.find(id="chapter-bookmark-data")
    title = None
    if meta:
        title = meta.get("data-title")
        story_title = meta.get("data-story-title")
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = og.get("content") if og else (soup.title.get_text(strip=True) if soup.title else None)

    content = soup.select_one(selector or prof.get("content", "")) if (selector or prof.get("content")) else None
    source = "selector" if selector else "profile"
    if content is None:
        content = guess_content(soup)
        source = "auto"
    if content is None:
        raise HTTPException(404, "ไม่พบเนื้อหาข้อความในหน้านี้")

    for t in content(JUNK_TAGS):
        t.decompose()
    paragraphs: list[str] = []
    ps = content.find_all("p")
    if ps:
        chunks = [p.get_text("\n", strip=True) for p in ps]
    else:
        chunks = content.get_text("\n", strip=True).split("\n")
    for c in chunks:
        for line in c.split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if line and (not paragraphs or paragraphs[-1] != line):
                paragraphs.append(line)
    if not paragraphs:
        raise HTTPException(404, "ไม่พบเนื้อหาข้อความในหน้านี้")

    data = {
        "url": url,
        "title": title,
        "story_title": story_title,
        "paragraphs": paragraphs,
        "next": nxt,
        "prev": prv,
        "source": source,
    }
    TEXT_CACHE[key] = (time.time(), data)
    if len(TEXT_CACHE) > 100:
        TEXT_CACHE.pop(next(iter(TEXT_CACHE)))
    return data


STATIC = Path(__file__).parent / "static"


@app.get("/manga", include_in_schema=False)
@app.get("/reader", include_in_schema=False)
def reader():
    return FileResponse(STATIC / "reader.html")


@app.get("/novels", include_in_schema=False)
def novels():
    return FileResponse(STATIC / "novel.html")


@app.get("/")
def root():
    return {
        "usage": "GET /scrape?url=https://example.com[&selector=h1][&pattern=...][&group=main]",
        "images": "GET /images?url=https://example.com[&pattern=...][&group=main]",
        "text": "GET /text?url=https://example.com/chapter[&selector=...]",
        "pages": "/manga (อ่านมังงะ), /novels (อ่านนิยาย)",
        "docs": "/docs",
    }
