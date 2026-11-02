import ipaddress
import re
import socket
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


async def fetch(url: str) -> str:
    check_url(url)
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=10, follow_redirects=True
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"เว็บปลายทางตอบ {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ดึงข้อมูลไม่สำเร็จ: {e}")


@app.get("/scrape")
async def scrape(
    url: str = Query(..., description="URL ของเว็บที่ต้องการดึง"),
    selector: Optional[str] = Query(None, description="CSS selector เช่น h2.title"),
):
    """ดึง title, description, หัวข้อ, ลิงก์ หรือข้อความตาม CSS selector"""
    html = await fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    if selector:
        return {
            "url": url,
            "selector": selector,
            "results": [el.get_text(" ", strip=True) for el in soup.select(selector)],
        }

    desc = soup.find("meta", attrs={"name": "description"})
    return {
        "url": url,
        "title": soup.title.string.strip() if soup.title and soup.title.string else None,
        "description": desc.get("content") if desc else None,
        "headings": [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])],
        "images": extract_images(soup, url, html)[:100],
        "links": [
            {"text": a.get_text(strip=True), "href": urljoin(url, a["href"])}
            for a in soup.find_all("a", href=True)
        ][:100],
    }


IMG_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif|bmp)(?:[?#].*)?$", re.I)
# ค้น URL รูปจาก HTML/สคริปต์ดิบ (กรณีรูปถูกใส่ผ่าน JS)
IMG_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()\\]+?\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s"'<>()\\]*)?"""
    r"""|(?<=["'])/?[\w\-./%]+?\.(?:jpe?g|png|gif|webp|avif)(?=["'])""",
    re.I,
)


def extract_images(
    soup: BeautifulSoup, base_url: str, html: str = "", pattern: Optional[str] = None
) -> list[dict]:
    """ดึง URL รูปจาก <img> (รวม lazy-load/srcset), <picture><source> และ og:image"""
    found: dict[str, str] = {}

    def add(src: Optional[str], alt: str = "") -> None:
        if not src or src.startswith("data:"):
            return
        found.setdefault(urljoin(base_url, src.strip()), alt)

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


@app.get("/images")
async def images(
    url: str = Query(..., description="URL ของเว็บที่ต้องการดึงรูป"),
    pattern: Optional[str] = Query(None, description="regex กรอง URL เช่น backcat"),
):
    """ดึง URL รูปภาพทั้งหมดในหน้าเว็บ"""
    html = await fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    imgs = extract_images(soup, url, html, pattern)
    return {"url": url, "count": len(imgs), "images": imgs}


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


@app.get("/reader", include_in_schema=False)
def reader():
    return FileResponse(Path(__file__).parent / "static" / "reader.html")


@app.get("/")
def root():
    return {
        "usage": "GET /scrape?url=https://example.com[&selector=h1]",
        "images": "GET /images?url=https://example.com",
        "docs": "/docs",
    }
