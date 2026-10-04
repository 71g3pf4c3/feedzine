#!/usr/bin/env python3
"""feedzine — RSS/Atom фиды → периодические «журналы» в EPUB.

Каждый запуск `feedzine issue` собирает выпуск из статей, появившихся
в фидах с прошлого выпуска: сводка из фида или полный текст
(Habr — через kek/v2, произвольные сайты — <article>-extractor),
картинки обрабатываются под e-ink (true grayscale, бокс экрана).
Прочитанное помечается в state — в следующий выпуск попадает только новое.

`feedzine tui` — интерактивно: отметить фиды, подкрутить max/full_text,
собрать выпуск не выходя из терминала.

Стек: typer (CLI), rich (вывод), textual (TUI), httpx (сеть, повторы
на транспортные сбои), pydantic (валидация конфига), Pillow (картинки),
pandoc (HTML→Markdown и сборка EPUB).
"""

import contextlib
import datetime
import hashlib
import html as htmllib
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
import zlib
import zipfile
from collections import deque
from dataclasses import dataclass, replace
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from rich.console import Console
from rich.table import Table
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, RichLog, Static

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
DEFAULT_CONFIG = "~/.config/feedzine/config.toml"

console = Console(highlight=False)


# хвост логов для webui/статуса сборки: log() пишет всегда
LOG_RING = deque(maxlen=400)


def log(s):
    # markup=False: имена фидов с [скобками] не должны парситься как разметка
    LOG_RING.append(s)
    console.print(s, markup=False)


# Пресеты картинок: resize W, gray — настоящий ч/б, box — вписывание (W, H),
# q — JPEG quality, cover — размер генерируемой обложки под экран.
PRESETS = {
    "reader": {"resize": 480, "gray": True, "box": (480, 800), "q": 85, "cover": (600, 800)},
    "eink":   {"resize": 480, "gray": False, "box": (480, 800), "q": 85, "cover": (600, 800)},
    "mini":   {"resize": 360, "gray": True, "box": (360, 600), "q": 80, "cover": (450, 600)},
    "tiny":   {"resize": 240, "gray": True, "box": (240, 400), "q": 78, "cover": (300, 400)},
    "hq":     {"resize": 900, "gray": False, "box": None, "q": 88, "cover": (900, 1200)},
}


# ---------------------------------------------------------------- сеть

_client = None


def _get_client():
    """Ленивый httpx-клиент: создание на импорте модуля падает в песочницах
    без валидного SSL_CERT_FILE (nix-проверки) и не нужно без сети."""
    global _client
    if _client is None:
        _client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True)
    return _client


def http_get(url, timeout=25, binary=False, retries=2):
    """GET с повторами на сетевые сбои (1 + retries попыток, паузы 2с/4с).

    HTTP-ошибки (4xx/5xx) не повторяются — падают сразу.
    Последняя ошибка пробрасывается наверх: вызывающие и так best-effort.
    """
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(2 * attempt)
        try:
            r = _get_client().get(url, timeout=timeout)
            r.raise_for_status()
            return r.content if binary else r.content.decode("utf-8", "replace")
        except httpx.TransportError as e:
            last = e
    raise last


def sanitize(s):
    return re.sub(r"[^\w-]+", "_", s)[:60].strip("_") or "item"


# ---------------------------------------------------------------- RSS / Atom

def parse_feed(xml_text):
    """RSS 2.0 / Atom -> (имя фида, [item]). Поля: title, link, guid, date(datetime|None), html, author, tags."""
    root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    items = []
    if root.tag == "rss" or root.tag.endswith("}rss"):
        ch = root.find("channel")
        name = (ch.findtext("title") or "") if ch is not None else ""
        for it in root.findall(".//item"):
            def g(tag):
                return (it.findtext(tag) or "").strip()
            # теги статьи: <category> RSS + dc:subject
            tags = [(c.text or "").strip() for c in it.findall("category")]
            tags += [(c.text or "").strip()
                     for c in it.findall("{http://purl.org/dc/elements/1.1/}subject")]
            items.append({
                "title": htmllib.unescape(g("title")),
                "link": g("link"),
                "guid": g("guid") or g("link"),
                "date": _parse_date(g("pubDate")),
                "html": g("description"),
                "author": (it.findtext("{http://purl.org/dc/elements/1.1/}creator") or "").strip(),
                "tags": [t for t in tags if t],
            })
    elif root.tag.endswith("}feed"):  # Atom
        ns = {"a": "http://www.w3.org/2005/Atom"}
        name = root.findtext("a:title", namespaces=ns) or ""
        for e in root.findall("a:entry", ns):
            link = next((l.get("href") for l in e.findall("a:link", ns)
                         if l.get("rel") in (None, "alternate")), None)
            content = e.findtext("a:content", namespaces=ns) or e.findtext("a:summary", namespaces=ns) or ""
            items.append({
                "title": htmllib.unescape(e.findtext("a:title", namespaces=ns) or ""),
                "link": link or "",
                "guid": e.findtext("a:id", namespaces=ns) or link or "",
                "date": _parse_date(e.findtext("a:updated", namespaces=ns) or e.findtext("a:published", namespaces=ns)),
                "html": content,
                "author": _tag_text(e, "a:author/a:name", ns),
                "tags": [t for t in (c.get("term", "").strip()
                                     for c in e.findall("a:category", ns)) if t],
            })
    else:
        raise ValueError(f"не RSS и не Atom: корень {root.tag}")
    return name, items


def _tag_text(el, path, ns=None):
    t = el.findtext(path, namespaces=ns) if ns else el.findtext(path)
    return (t or "").strip()


def _parse_date(s):
    """Дата из RSS/Atom -> naive datetime (единый формат для сортировки)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    try:  # ISO без таймзоны
        return datetime.datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


# ---------------------------------------------------------------- state

def load_state(workdir):
    """state.json -> dict. Выпускные поля — по журналам; старый плоский
    формат (issue int / pending list / last_emit str) мигрируется в
    журнал "main" при чтении, файл не трогаем до первого save."""
    p = f"{workdir}/state.json"
    st = None
    if os.path.exists(p):
        try:
            st = json.load(open(p))
        except json.JSONDecodeError:
            pass
    if not isinstance(st, dict):
        st = {}
    if isinstance(st.get("issue"), int):
        st["issue"] = {"main": st["issue"]}
    if isinstance(st.get("pending"), list):
        st["pending"] = {"main": st.pop("pending")}
    if isinstance(st.get("last_emit"), str):
        st["last_emit"] = {"main": st.pop("last_emit")}
    st.setdefault("seen", {})
    st.setdefault("issue", {})
    st.setdefault("pending", {})
    st.setdefault("last_emit", {})
    st.setdefault("down_feeds", {})
    st.setdefault("retry_fulltext", {})
    return st


def save_state(workdir, st):
    with open(f"{workdir}/state.json", "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- полный текст

def habr_id(link):
    m = re.search(r"habr\.com/(?:\w{2}/)?articles/(\d+)", link or "")
    return m.group(1) if m else None


def _page_block(page, tag):
    """Самый мясной <tag>…</tag> страницы (по объёму текста, не разметки)."""
    blocks = re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", page, re.S | re.I)
    if not blocks:
        return None

    def text_len(b):
        return len(re.sub(r"<[^>]+>", " ", b))

    return max(blocks, key=text_len)


def _extract_page(page):
    """Главный контент страницы: article -> main, мусор вырезается.

    На агрегаторах и в комментариях <article> бывает много — берём
    самый большой по тексту блок, а не первый попавшийся.
    """
    for tag in ("article", "main"):
        blk = _page_block(page, tag)
        if blk and len(re.sub(r"<[^>]+>", " ", blk)) > 500:
            return re.sub(r"<(script|style|nav|aside|footer|form)\b[^>]*>.*?</\1>",
                          "", blk, flags=re.S | re.I)
    return None


def fetch_full(link, sniff=None):
    """Полный текст статьи -> (html, author) или None.

    Habr — kek/v2. Прочее — SSR-страница: <article>/<main>. Сниффер: если
    по ссылке контент не взялся (агрегатор: HN-страница обсуждения,
    редирект-хаб), пробуем первую внешнюю ссылку из описания фида —
    у link-фидов (HN, rss-bridge) настоящая статья обычно там.
    """
    hid = habr_id(link)
    if hid:
        d = json.loads(http_get(f"https://habr.com/kek/v2/articles/{hid}/"))
        author = ((d.get("author") or {}).get("fullname")) or ((d.get("author") or {}).get("alias"))
        return d.get("textHtml"), author
    html = _extract_page(http_get(link))
    if html:
        return html, None
    if sniff:
        for u in re.findall(r"https?://[^\s\"'<>]+", sniff):
            u = u.rstrip(").,];")
            if u == link:
                continue
            try:  # ровно одна попытка по сниффнутой ссылке
                html = _extract_page(http_get(u))
            except Exception:  # noqa: BLE001
                break
            if html:
                return html, None
            break
    return None, None


# ---------------------------------------------------------------- картинки

def img_name(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"


def _errcode(e):
    """Короткий код ошибки для текста выпуска: '403', 'таймаут', 'сеть'."""
    s = str(e)
    m = re.search(r"\b(4\d\d|5\d\d)\b", s)
    if m:
        return m.group(1)
    if "timed out" in s or "Timeout" in s:
        return "таймаут"
    return (s[:24] + "…") if len(s) > 24 else (s or "сеть")


def process_image(url, imgdir, opts, base=None, q=None):
    """Скачивает и обрабатывает под e-ink (true gray, бокс, baseline JPEG).

    Относительные URL (/img/x.png) резолвятся от base (origin статьи).
    q — переопределение JPEG quality (None → качество пресета).
    Идемпотентно: готовый файл в кэше не трогаем.
    -> (имя локального файла | None, код ошибки | None).
    """
    if url.startswith("data:"):
        pass
    elif base and not url.startswith(("http://", "https://")):
        # относительные /img/x.png и img/x.png — резолвим от origin
        url = urljoin(base, url)
    name = img_name(url)
    dst = f"{imgdir}/{name}"
    if os.path.exists(dst):
        return name, None
    try:
        raw = http_get(_abs_url(url), timeout=40, binary=True)
    except Exception as e:  # noqa: BLE001
        log(f"  ! картинка не скачалась: {url[:60]}… ({e})")
        return None, _errcode(e)
    if not HAS_PIL:
        os.makedirs(imgdir, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(raw)
        return name, None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None, "битый файл"
    if im.mode in ("P", "LA", "PA"):
        im = im.convert("RGBA")   # палитра с прозрачностью — без варнингов PIL
    pr = PRESETS[opts.preset]
    if pr["gray"]:
        im = im.convert("L")
    if pr["box"]:
        im.thumbnail(pr["box"])
    else:
        w = pr["resize"]
        if im.width > w:
            im = im.resize((w, max(1, round(im.height * w / im.width))))
    os.makedirs(imgdir, exist_ok=True)
    im.save(dst, "JPEG", quality=q or pr["q"], optimize=True)  # baseline, не progressive
    return name, None


def _abs_url(u):
    return "https:" + u if u.startswith("//") else u


def rewrite_images(html, imgdir, opts, rel="../img", base=None, q=None):
    """<img src> → локальный rel/<hash>.jpg; нес скачавшиеся — заглушка
    в тексте. -> (html, [локальные имена], [(url, код ошибки)]).
    """
    done, failed = [], []

    def _sub(m):
        url = m.group(1)
        if url.startswith("data:"):
            return m.group(0)
        # трекинг-пиксели (medium _/stat и подобные) — не контент,
        # выкидываем молча, без заглушки и без 403-шума
        if re.search(r"(/stat\?|/_/stat|pixel|beacon|/collect\?|analytics)",
                     url, re.I):
            return ""
        name, err = process_image(url, imgdir, opts, base, q=q)
        if name:
            done.append(name)
            return m.group(0).replace(url, f"{rel}/{name}")
        # заглушка: читатель видит, что картинка была, но не взялась
        failed.append((url, err))
        return (f'<p>[картинка не скачалась: {err} · {url[:80]}]</p>')

    out = re.sub(r'<img\b[^>]*src="([^"]+)"[^>]*/?>', _sub, html)
    return out, done, failed


# ---------------------------------------------------------------- обложка

def cover_font_path():
    """TTF для генерируемой обложки: env FEEDZINE_FONT, потом рядом со
    скриптом (в nix-пакете — share/feedzine/fonts/cover.ttf). None — дефолт PIL."""
    p = os.environ.get("FEEDZINE_FONT")
    if p and os.path.exists(p):
        return p
    here = os.path.dirname(os.path.abspath(__file__))
    cand = f"{here}/fonts/cover.ttf"
    return cand if os.path.exists(cand) else None


def _escape_art(w, h, cx0, cy0, scale, julia_c, iters):
    """Фрактал (Мандельброт при julia_c=None, иначе Жюлиа): escape-time на
    грубой сетке (блок 4px) с апскейлом NEAREST — пиксель-арт выглядит
    осознанным стилем и не требует numpy."""
    from PIL import Image
    K = 4
    sw, sh = max(2, w // K), max(2, h // K)
    im = Image.new("L", (sw, sh), 255)
    put = im.load()
    ar = sw / sh
    for j in range(sh):
        ci = cy0 + (j / sh - 0.5) * scale
        for i in range(sw):
            cr = cx0 + (i / sw - 0.5) * scale * ar
            if julia_c is None:            # Мандельброт: c = точка, z = 0
                zr, zi, xr, xi = 0.0, 0.0, cr, ci
            else:                           # Жюлиа: z = точка, c фиксирован
                zr, zi, xr, xi = cr, ci, julia_c[0], julia_c[1]
            it = 0
            while it < iters and zr * zr + zi * zi < 4.0:
                zr, zi = zr * zr - zi * zi + xr, 2 * zr * zi + xi
                it += 1
            # вне множества: чем дольше держится, тем темнее; внутри — чёрный
            put[i, j] = 0 if it >= iters else max(70, 250 - it * 9)
    return im.resize((w, h), Image.NEAREST)


def _pat_mandelbrot(rng, w, h, sc):
    """Кроп границы множества Мандельброта в известных «долинах»."""
    spots = [(-0.7435, 0.1314), (-0.16, 1.0405), (0.2925, 0.0149),
             (-1.786, 0.0), (-0.7756, 0.2549), (-0.1011, 0.9563)]
    cx, cy = rng.choice(spots)
    return _escape_art(w, h, cx, cy, rng.uniform(0.004, 0.05), None, 96)


def _pat_julia(rng, w, h, sc):
    """Множество Жюлиа со случайной c у границы кардиоиды."""
    cs = [(-0.8, 0.156), (0.285, 0.01), (-0.4, 0.6), (-0.7269, 0.1889),
          (0.35, 0.35), (-0.54, 0.54)]
    cx, cy = rng.choice(cs)
    j = 0.002
    c = (cx + rng.uniform(-j, j), cy + rng.uniform(-j, j))
    return _escape_art(w, h, 0.0, 0.0, rng.uniform(2.4, 3.2), c, 96)


def _pat_truchet(rng, w, h, sc):
    """Тайлинг Трюше: дуги или диагонали в случайных ориентациях клеток."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    s = max(10, round(44 * sc))
    lw = max(1, round(5 * sc))
    arcs = rng.random() < 0.65
    for gy in range(0, h, s):
        for gx in range(0, w, s):
            if arcs:
                if rng.random() < 0.5:      # дуги: верх-лево + низ-право
                    d.arc([gx, gy, gx + 2 * s, gy + 2 * s], 0, 90, fill=0, width=lw)
                    d.arc([gx - s, gy - s, gx + s, gy + s], 180, 270, fill=0, width=lw)
                else:                       # верх-право + низ-лево
                    d.arc([gx - s, gy, gx + s, gy + 2 * s], 90, 180, fill=0, width=lw)
                    d.arc([gx, gy - s, gx + 2 * s, gy + s], 270, 360, fill=0, width=lw)
            elif rng.random() < 0.5:        # диагональ / антидиагональ
                d.line([(gx, gy), (gx + s, gy + s)], fill=0, width=lw)
            else:
                d.line([(gx + s, gy), (gx, gy + s)], fill=0, width=lw)
    return im


def _pat_phyllo(rng, w, h, sc):
    """Филлотаксис (подсолнух): точки по золотому углу, растут от центра."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    cx, cy = w / 2, h / 2
    rmax = min(w, h) / 2 - 2 * sc
    ga = math.pi * (3 - math.sqrt(5))
    rot = rng.uniform(0, 2 * math.pi)
    n = int(w * h / (900 * sc * sc))
    base = max(1.5, 3.2 * sc)
    for i in range(n):
        r = rmax * math.sqrt((i + 1) / n)
        a = i * ga + rot
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        rr = base * (0.4 + 0.8 * r / rmax)
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=0)
    return im


def _pat_moire(rng, w, h, sc):
    """Муар: два пучка концентрических окружностей с близким шагом."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    lw = max(1, round(2 * sc))
    cy = h / 2
    c1 = (w * rng.uniform(0.15, 0.35), cy + rng.uniform(-0.2, 0.2) * h)
    c2 = (w * rng.uniform(0.65, 0.85), cy + rng.uniform(-0.2, 0.2) * h)
    step = max(4, round(7 * sc))
    step2 = step * rng.uniform(1.03, 1.1)
    rmax = math.hypot(max(w, h), w) / 2
    for r in range(step, int(rmax), step):
        d.ellipse([c1[0] - r, c1[1] - r, c1[0] + r, c1[1] + r], outline=0, width=lw)
    for r in range(round(step2), int(rmax), round(step2)):
        d.ellipse([c2[0] - r, c2[1] - r, c2[0] + r, c2[1] + r], outline=0, width=lw)
    return im


def _pat_tree(rng, w, h, sc):
    """Фрактальное дерево из нижнего центра, случайные углы ветвления."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)

    def branch(x, y, ang, ln, wd, depth):
        x2, y2 = x + math.cos(ang) * ln, y - math.sin(ang) * ln
        d.line([(x, y), (x2, y2)], fill=0, width=wd)
        if depth <= 0 or ln < 3 * sc:
            return
        spread = rng.uniform(0.25, 0.6)
        decay = rng.uniform(0.66, 0.78)
        branch(x2, y2, ang + spread, ln * decay, max(1, wd - round(2 * sc)), depth - 1)
        branch(x2, y2, ang - spread, ln * decay, max(1, wd - round(2 * sc)), depth - 1)
        if rng.random() < 0.3:              # изредка третья ветка
            branch(x2, y2, ang + rng.uniform(-0.12, 0.12), ln * decay,
                   max(1, wd - round(2 * sc)), depth - 1)

    branch(w / 2, h - 2 * sc, math.pi / 2, h * rng.uniform(0.24, 0.32),
           max(2, round(9 * sc)), 9)
    return im


def _pat_ridge(rng, w, h, sc):
    """Риджлайн: стопка «горизонтов» из суммы синусов с центральным пиком."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    rows = max(10, round(h / (16 * sc)))
    rowh = h / (rows + 1)
    amp = rowh * 1.8
    comps = [[(rng.uniform(0.5, 2.2) * math.pi, rng.uniform(0, 2 * math.pi),
               rng.uniform(0.2, 1.0)) for _ in range(4)] for _ in range(rows)]
    bump = rng.uniform(0.6, 1.0)
    for r in range(rows):
        ybase = (r + 1) * rowh + amp / 2
        pts = []
        for px_ in range(0, w + 8, 8):
            t = px_ / w
            y = ybase - sum(a * math.sin(f * t * 2 * math.pi + p) for f, p, a in comps[r])
            y -= amp * bump * math.exp(-((t - 0.5) ** 2) / 0.035)
            pts.append((px_, y))
        # белая заливка под линией прикрывает задние ряды — эффект хребтов
        d.polygon(pts + [(w, h), (0, h)], fill=255)
        d.line(pts, fill=0, width=max(1, round(2 * sc)))
    return im


def _pat_sierpinski(rng, w, h, sc):
    """Треугольник Серпинского: игра хаоса точками."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    m = 4 * sc
    verts = [(w / 2, m), (m, h - m), (w - m, h - m)]
    x, y = (sum(v[0] for v in verts) / 3, sum(v[1] for v in verts) / 3)
    dot = max(1, round(1.2 * sc))
    for _ in range(30000):
        vx, vy = verts[rng.randrange(3)]
        x, y = (x + vx) / 2, (y + vy) / 2
        d.ellipse([x - dot, y - dot, x + dot, y + dot], fill=0)
    return im


PATTERNS = {
    "mandelbrot": _pat_mandelbrot,
    "julia": _pat_julia,
    "truchet": _pat_truchet,
    "phyllotaxis": _pat_phyllo,
    "moire": _pat_moire,
    "tree": _pat_tree,
    "ridge": _pat_ridge,
    "sierpinski": _pat_sierpinski,
}


_COVERED_CACHE = {}


def _covered_codepoints(font_path):
    """Кодпоинты шрифта обложки (cmap из fontTools) или None — не проверяем.

    Шрифт может не знать символ (CJK, эмодзи, № у битмаповых дефолтов):
    FreeType тогда рисует tofu-квадрат. cache — TUI зовёт make_cover
    на каждый пересбор.
    """
    if not font_path:
        return None
    if font_path not in _COVERED_CACHE:
        try:
            from fontTools.ttLib import TTFont
            with open(font_path, "rb") as f:
                _COVERED_CACHE[font_path] = set(TTFont(f).getBestCmap())
        except Exception:  # noqa: BLE001 — нет fontTools/кривой шрифт: не блокируем
            _COVERED_CACHE[font_path] = None
    return _COVERED_CACHE[font_path]


def cover_text_safe(text, covered):
    """Текст без символов, которых нет в шрифте обложки. -> (текст, выброшено).

    Непокрытые символы выкидываются: на e-ink tofu-квадраты хуже
    отсутствующего символа. Пробелы сохраняются всегда.
    """
    if not text or covered is None:
        return text, ""
    out, dropped = [], set()
    for ch in text:
        if ch.isspace() or ord(ch) in covered:
            out.append(ch)
        else:
            dropped.add(ch)
    return "".join(out), "".join(sorted(dropped))


def make_cover(dst, title, n, date_str, sections, size=None, pattern="auto"):
    """Генеративная обложка под e-ink: монохромный узор/фрактал во всю
    ширину, поверх — заголовок, номер на белой плашке, дата, секции.
    Узор выбирается детерминированно по номеру выпуска и заголовку:
    пересборка того же выпуска даёт ту же обложку, новый выпуск — новый
    узор. pattern="auto" — случайный из PATTERNS, иначе фиксированный.
    Размер — от пресета экрана, вся геометрия масштабируется. Без PIL — None.
    """
    if not HAS_PIL:
        return None
    from PIL import Image, ImageDraw, ImageFont

    W, H = size if size else (600, 800)
    sc = W / 600.0  # масштаб под ширину обложки

    def px(v):
        return max(1, round(v * sc))

    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    font_path = cover_font_path()
    # глифы шрифта: неизвестных шрифту символов на обложке не рисуем
    covered = _covered_codepoints(font_path)
    dropped = set()

    def safe(text):
        t, dr = cover_text_safe(text, covered)
        dropped.update(dr)
        return t

    def font(size):
        try:
            if font_path:
                return ImageFont.truetype(font_path, size)
        except Exception:  # noqa: BLE001
            pass
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # старый Pillow без size у load_default
            return ImageFont.load_default()

    def center(y, text, f):
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, y), text, font=f, fill=0)

    def wrap(text, f, max_w):
        lines, cur = [], ""
        for word in text.split():
            t = (cur + " " + word).strip()
            if not cur or d.textlength(t, font=f) <= max_w:
                cur = t
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    # детерминированный seed: тот же выпуск — тот же узор
    rng = random.Random(zlib.crc32(title.encode())
                        ^ zlib.crc32(date_str.encode())
                        ^ (n * 2654435761) & 0xFFFFFFFF)
    name = pattern if pattern != "auto" else rng.choice(list(PATTERNS))
    pat_fn = PATTERNS.get(name, _pat_truchet)

    M = px(48)  # поля
    # двойная рамка — классический титульный лист
    d.rectangle([M, M, W - M, H - M], outline=0, width=max(2, px(3)))
    d.rectangle([M + px(8), M + px(8), W - M - px(8), H - M - px(8)],
                outline=0, width=1)

    # --- раскладка по вертикали: заголовок / узор / дата / секции
    title = safe(title)
    title_f, title_lh = font(px(38)), px(50)
    title_lines = wrap(title, title_f, W - 2 * (M + px(40)))[:3]
    y_title = M + px(44)
    for line in title_lines:
        center(y_title, line, title_f)
        y_title += title_lh
    y_title -= title_lh - px(10)  # низ последней строки

    sec_f, sec_lh = font(px(21)), px(28)
    sections = [safe(s) for s in sections]
    sec_h = H - M - px(14) - y_title - px(30) - px(80)
    sec_count = max(0, min(len(sections), 5, int(sec_h // sec_lh)))
    sec_top = H - M - px(14) - sec_lh * sec_count
    y = sec_top
    for s in sections[:sec_count]:
        if s:
            center(y, s, sec_f)
        y += sec_lh

    date_f = font(px(28))
    date_y = sec_top - px(46)

    # --- полоса узора во всю ширину между заголовком и датой
    bx = M + px(12)
    by0 = y_title + px(26)
    by1 = date_y - px(26)
    if by1 - by0 >= px(100):
        band = pat_fn(rng, W - 2 * bx, by1 - by0, sc)
        im.paste(band, (bx, by0))
        d.rectangle([bx, by0, W - bx - 1, by1 - 1], outline=0, width=1)

    # --- номер выпуска на белой плашке поверх узора
    num_f = font(px(54))
    num = safe(f"№ {n}")
    tw = d.textlength(num, font=num_f) or d.textlength("0", font=num_f)
    ph = px(104)
    pw = tw + px(56)
    pcx, pcy = W / 2, (by0 + by1) / 2
    d.rounded_rectangle([pcx - pw / 2, pcy - ph / 2, pcx + pw / 2, pcy + ph / 2],
                        radius=px(16), fill=255, outline=0, width=max(2, px(2)))
    d.text((pcx - tw / 2, pcy - ph / 2 + (ph - px(64)) / 2), num,
           font=num_f, fill=0)

    center(date_y, safe(date_str), date_f)

    if dropped:
        log(f"  ! обложка: символов нет в шрифте, выкинул: {' '.join(dropped)}")

    im.save(dst, "PNG")
    return dst


# ---------------------------------------------------------------- markdown / EPUB

def html_to_md(html):
    p = subprocess.run(["pandoc", "-f", "html", "-t", "gfm", "--wrap=none"],
                       input=html.encode(), capture_output=True)
    return p.stdout.decode("utf-8", "replace").strip()


def out_name(out, title, n, date_str, fmt):
    """Журнал_007_2026-10-04.<fmt> — единое имя для всех форматов."""
    ext = {"epub": "epub", "html": "html", "md": "md"}[fmt]
    return f"{out}/{sanitize(title)}_{n:03d}_{date_str}.{ext}"


def squeeze_epub(path):
    """Перепаковывает EPUB с максимальным сжатием (deflate level 9).

    По OCF mimetype обязан идти первым и без сжатия — сохраняем это.
    Выигрыш даёт текст/CSS; JPEG уже сжаты, их не трогаем.
    """
    with zipfile.ZipFile(path) as zin:
        data = {it.filename: zin.read(it.filename)
                for it in zin.infolist() if not it.is_dir()}
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w") as zout:
        if "mimetype" in data:
            zout.writestr("mimetype", data.pop("mimetype"),
                          compress_type=zipfile.ZIP_STORED)
        for name, blob in data.items():
            zout.writestr(name, blob,
                          compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.replace(tmp, path)


def build_output(md_file, out_file, fmt, title, workdir, cover=None, date=None, toc_depth=3):
    """Собирает выпуск в epub | html | md из markdown-файла.

    epub: pandoc + eink.css + обложка, потом squeeze (zip level 9).
         Главы режутся по ## (--epub-chapter-level=2): один фид = один
         xhtml-файл — иначе весь журнал ляжет в один ch001.xhtml и
         e-ink читалка его не переживёт.
    html: один самодостаточный файл (картинки зашиты --embed-resources).
    md:  копия markdown (картинки остаются в workdir/img — пути относительные).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.basename(md_file)

    if fmt == "md":
        shutil.copy(md_file, out_file)
        return True

    cmd = ["pandoc", base, "-o", out_file,
           "--metadata", f"title={title}",
           "--metadata", "author=feedzine",
           "--toc", f"--toc-depth={toc_depth}",
           f"--css={here}/eink.css"]
    if fmt == "html":
        cmd += ["--standalone", "--embed-resources"]
    else:
        cmd += ["--split-level=2"]
        if cover:
            cmd += [f"--epub-cover-image={cover}"]
    if date:
        cmd += ["--metadata", f"date={date}"]
    # картинки в md указаны относительно workdir — pandoc резолвит от cwd
    if subprocess.run(cmd, cwd=workdir).returncode != 0:
        return False
    if fmt == "epub":
        squeeze_epub(out_file)
    return True


# ---------------------------------------------------------------- конфиг (pydantic)

class FeedCfg(BaseModel):
    """Один [[feed]] из config.toml."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    url: str
    max: int | None = None
    section: str | None = None
    full_text: bool | str | None = None
    include_tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    journal: str | None = None      # id отдельного журнала (out/<id>/)
    min_article_chars: int | None = None  # None — глобальный; 0 — не фильтровать


class JournalCfg(BaseModel):
    """[journal.<id>] — настройки отдельного журнала.

    Фиды с journal = "<id>" собираются в свои выпуски: свой заголовок,
    свой период, своя нумерация. Пустые значения наследуют глобальные.
    """
    model_config = ConfigDict(extra="forbid")
    title: str = ""             # пусто — id журнала (main: cfg.title)
    period: str = ""            # пусто — глобальный period
    keep_issues: int = 0        # 0 — глобальный keep_issues


class Config(BaseModel):
    """config.toml целиком. extra=forbid: опечатка в ключе — понятная ошибка."""
    model_config = ConfigDict(extra="forbid")
    title: str = "Журнал"
    out: str = "~/Books/feedzine"
    workdir: str = "~/.cache/feedzine"
    preset: str = "reader"
    full_text: str | bool = "auto"
    max_per_feed: int = 10
    text_only: bool = False
    min_article_chars: int = 120   # меньше прозы (без ссылок/картинок) — не статья
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    cover: str = "auto"              # auto | image | generated | off
    cover_pattern: str = "auto"      # узор генерируемой обложки: auto | mandelbrot | ...
    format: str = "epub"             # epub | html | md
    img_quality: int = 0             # 0 = качество пресета; иначе JPEG quality 40..95
    toc_depth: int = 3               # глубина оглавления: 2 — только фиды, 3 — и статьи
    period: str = "day"              # day | week | month: сводки вместо потока
    keep_issues: int = 0             # 0 = хранить все; N = оставить последние N
    calibre_library: str = ""        # непусто — добавлять выпуск через calibredb
    post_issue: str = ""             # shell-хук после сборки, %f = путь к выпуску
    journal: dict[str, JournalCfg] = Field(default_factory=dict)
    feed: list[FeedCfg] = Field(default_factory=list)

    @field_validator("preset")
    @classmethod
    def _check_preset(cls, v):
        if v not in PRESETS:
            raise ValueError(f"preset {v!r} не из {sorted(PRESETS)}")
        return v

    @field_validator("cover")
    @classmethod
    def _check_cover(cls, v):
        if v not in ("auto", "image", "generated", "off"):
            raise ValueError("cover должен быть auto | image | generated | off")
        return v

    @field_validator("min_article_chars")
    @classmethod
    def _check_min_chars(cls, v):
        if v < 0:
            raise ValueError("min_article_chars должен быть >= 0 (0 = не фильтровать)")
        return v

    @field_validator("cover_pattern")
    @classmethod
    def _check_cover_pattern(cls, v):
        if v != "auto" and v not in PATTERNS:
            raise ValueError(f"cover_pattern должен быть auto | {' | '.join(PATTERNS)}")
        return v

    @field_validator("format")
    @classmethod
    def _check_format(cls, v):
        if v not in ("epub", "html", "md"):
            raise ValueError("format должен быть epub | html | md")
        return v

    @field_validator("img_quality")
    @classmethod
    def _check_img_quality(cls, v):
        if v != 0 and not (40 <= v <= 95):
            raise ValueError("img_quality: 0 (качество пресета) или 40..95")
        return v

    @field_validator("toc_depth")
    @classmethod
    def _check_toc_depth(cls, v):
        if not (1 <= v <= 4):
            raise ValueError("toc_depth: 1..4 (2 — только фиды, 3 — и статьи)")
        return v

    @field_validator("period")
    @classmethod
    def _check_period(cls, v):
        if v not in ("day", "week", "month"):
            raise ValueError("period должен быть day | week | month")
        return v

    @field_validator("journal")
    @classmethod
    def _check_journal_periods(cls, v):
        for jid, jc in v.items():
            if jc.period and jc.period not in ("day", "week", "month"):
                raise ValueError(f"journal.{jid}.period должен быть day | week | month")
        return v


def load_config(path):
    """TOML -> Config (pydantic валидирует типы и известность ключей)."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config(**data)


@dataclass
class RunOpts:
    """Параметры одного запуска (не путать с Config — это не desired state)."""
    preset: str = "reader"
    text_only: bool = False
    dry_run: bool = False
    format: str | None = None   # None -> cfg.format
    force: bool = False         # собрать сводку сейчас, не дожидаясь границы


# ---------------------------------------------------------------- тег-фильтры

def tag_match(tags, include, exclude):
    """Проходит ли статья с тегами `tags` через фильтры.

    include: пусто → любой тег подходит; иначе нужен хотя бы один общий тег.
    exclude: пусто → ничего не режем; иначе ни одного общего тега.
    Регистронезависимо. Теги статьи — <category> RSS / category Atom.
    """
    have = {t.lower() for t in tags if t}
    if exclude and have & {t.lower() for t in exclude}:
        return False
    if include and not (have & {t.lower() for t in include}):
        return False
    return True


def collect_items(cfg, opts):
    """Новые статьи всех фидов -> (свежие с капом, все guid'ы фидов, state).

    Помечаются ВСЕ статьи фида (не только попавшие в выпуск): бэклог
    не выдавливается выпусками — новый выпуск собирается из genuinely
    нового. Переполнение сверх max и отфильтрованное тегами молча
    роняется как прочитанное.
    """
    st = load_state(cfg.workdir)
    fresh, all_guids = [], set()
    down = st.setdefault("down_feeds", {})
    for fd in cfg.feed:
        url = fd.url
        try:
            ftitle, items = parse_feed(http_get(url))
        except Exception as e:  # noqa: BLE001
            log(f"  ! фид {fd.name or url} не прочитался: {e}")
            # заглушка: помечаем деградацию, читатель узнает в выпуске;
            # поднялся — запись снимется и статьи приедут сами (unseen)
            down[url] = {"name": fd.name or url, "jid": fd.journal or "main",
                         "error": _errcode(e), "since": datetime.date.today().isoformat()}
            continue
        down.pop(url, None)
        if not fd.name:
            # имя фида по умолчанию — его собственный title
            fd.name = ftitle or re.sub(r"^https?://(?:www\.)?", "", url)[:40]
        for it in items:
            if it["guid"]:
                all_guids.add(it["guid"])
        # тег-фильтры: на фиде переопределяют глобальные
        inc = fd.include_tags if fd.include_tags is not None else cfg.include_tags
        exc = fd.exclude_tags if fd.exclude_tags is not None else cfg.exclude_tags
        # свежие = невидимые, новые сверху (беру хвост — самые новые в RSS идут первыми)
        new = [it for it in items if it["guid"] and it["guid"] not in st["seen"]]
        if inc or exc:
            new = [it for it in new if tag_match(it.get("tags", []), inc, exc)]
        cap = fd.max if fd.max is not None else cfg.max_per_feed
        log(f"  {fd.name}: {len(items)} в фиде, новых {len(new)} (в выпуск {min(cap, len(new))})")
        for it in new[:cap]:
            it["feed"] = fd
            fresh.append(it)
    fresh.sort(key=lambda i: i["date"] or datetime.datetime.min)
    return fresh, all_guids, st


def _section(fd):
    """Секция выпуска: явная или имя фида."""
    return fd.section or fd.name or "RSS"


def demote_headings(md):
    """Демо́тит ATX-заголовки (#…####) на два уровня вниз: внутренние
    заголовки статей не должны конкурировать с секциями (##) и
    заголовками статей (###) в навигации EPUB.

    Внутри fenced-кодов (``` … ```) не трогаем: там `#` — комментарий,
    а не заголовок. Заголовки не выбрасываются — только понижаются.
    """
    out, fence = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if not fence:
            line = re.sub(r"^(#{1,4})(\s)", r"##\1\2", line)
        out.append(line)
    return "\n".join(out)


def _item_text_len(html):
    """Объём текста в html-фрагменте: без тегов, сущностей и пробелов."""
    t = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return len(htmllib.unescape(t).strip())


def _md_prose_len(md):
    """Объём «прозы» в markdown: картинки/ссылки/разметка не считаются.

    Это гейт пустого говна: статья-ссылка без контента даёт ~0. Заглушки
    нес скачавшихся картинок тоже не проза — иначе пачка битых картинок
    прошла бы фильтр как текст.
    """
    t = re.sub(r"\[картинка не скачалась: [^\]]*\]", " ", md)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)          # картинки не текст
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)        # ссылки -> их текст
    t = re.sub(r"```.*?```", " ", t, flags=re.S)          # код не проза
    t = re.sub(r"[#*_>`~|\[\]-]+", " ", t)                # разметка
    return len(" ".join(t.split()))


def _md_img_count(md):
    """Скачавшиеся картинки в markdown-теле (заглушки не в счёт)."""
    return len(re.findall(r"!\[[^\]]*\]\(img/[^)]+\)", md))


def article_md(item, cfg, imgdir, opts, failures=None):
    """Статья -> markdown: шапка с метаданными + сводка/полный текст.

    None — пустое говно: после зачистки ссылок/картинок в теле меньше
    min_article_chars прозы (голая ссылка, заглушка paywall, пустая
    сводка). Вызывавший должен такую статью из выпуска выкинуть.

    failures — список для фиксации деградаций: полный текст не взялся
    (kind=fulltext, уйдёт в пул ретраев), картинки не скачались
    (kind=image). Деградации отмечаются в тексте выпуска, читатель
    видит, что контент неполный.
    """
    fd = item["feed"]
    ft = fd.full_text if fd.full_text is not None else cfg.full_text
    # auto: Habr (надёжный kek/v2) и «огрызки» — фиды-агрегаторы (HN,
    # rss-bridge), у которых сводка — это ссылка, а не текст
    stub = _item_text_len(item["html"]) < 300
    want_full = (ft in (True, "always")
                 or (ft == "auto" and (habr_id(item["link"]) or stub)))
    html, author = item["html"], item["author"]
    notes, ft_err = [], None
    if want_full and item["link"]:
        try:
            full, fauthor = fetch_full(item["link"], sniff=item.get("html"))
            if full:
                html, author = full, author or fauthor
            else:
                ft_err = "контент не извлёкся"
        except Exception as e:  # noqa: BLE001
            ft_err = _errcode(e)
            log(f"  ! полный текст не взялся: {e}")
        if ft_err and failures is not None:
            failures.append({"kind": "fulltext", "guid": item.get("guid"),
                             "title": item["title"], "link": item["link"],
                             "date": (item["date"].isoformat()
                                      if item["date"] else None),
                             "html": item["html"], "author": item["author"],
                             "feed": {"name": fd.name, "full_text": fd.full_text,
                                      "section": fd.section,
                                      "min_article_chars": fd.min_article_chars},
                             "error": ft_err})
    img_fails = []
    if not opts.text_only:
        base = None
        if item["link"]:
            m = re.match(r"https?://[^/]+", item["link"])
            if m:
                base = m.group(0)
        html, _done, img_fails = rewrite_images(html, imgdir, opts,
                                                rel=f"img/{opts.preset}",
                                                base=base, q=cfg.img_quality or None)
    else:
        html = re.sub(r"<img\b[^>]*>", "", html)
    if img_fails and failures is not None:
        failures.append({"kind": "image", "count": len(img_fails),
                         "guid": item.get("guid")})
    body = html_to_md(html) or ""
    # внутренние заголовки статьи демо́тимся (fenced-коды не трогаем)
    body = demote_headings(body)
    min_chars = (fd.min_article_chars if fd.min_article_chars is not None
                 else cfg.min_article_chars)
    # фото-пост («@x posted a photo» с подписью) — картинка и есть контент:
    # гейт действует только на текст без картинок
    if _md_prose_len(body) < min_chars and _md_img_count(body) == 0:
        log(f"  − пустое не пошло в выпуск: {item['title'][:60]}")
        return None
    d = item["date"].strftime("%d.%m.%Y") if item["date"] else ""
    src = fd.name or "RSS"
    head = f"*{author or src}"
    if d:
        head += f" · {d}"
    head += f" · [оригинал]({item['link']})*" if item["link"] else "*"
    # ограничения — в текст выпуска, а не молча
    if ft_err:
        notes.append(f"полный текст не взялся ({ft_err}) — попробую снова "
                     f"в следующем выпуске")
    if img_fails:
        notes.append(f"картинок не скачалось: {len(img_fails)} "
                     f"({_errcode_list(img_fails)})")
    note = ("\n\n" + "\n\n".join(f"> ⚠ {n}" for n in notes)) if notes else ""
    return f"### {item['title']}\n\n{head}{note}\n\n{body}\n"


def _errcode_list(fails):
    """Сводка кодов ошибок картинок: '403 ×3, таймаут'."""
    cnt = {}
    for _url, err in fails:
        cnt[err] = cnt.get(err, 0) + 1
    return ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in cnt.items())


# ---------------------------------------------------------------- поставка

def calibre_add(epub, library):
    """Добавляет EPUB в calibre-библиотеку через calibredb (best effort)."""
    exe = shutil.which("calibredb")
    if not exe:
        log("  ! calibredb не в PATH — в calibre не добавил")
        return False
    r = subprocess.run([exe, "add", "--library", library, epub],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  ! calibredb: {(r.stderr or r.stdout).strip()[:200]}")
        return False
    log("  + добавлено в calibre")
    return True


def run_post_issue(cmd, epub):
    """Хук после сборки: shell-команда, %f -> путь к EPUB, env FEEDZINE_EPUB.

    Универсальный шов доставки: syncthing/rscp/копия на читалку/что угодно.
    """
    if not cmd:
        return
    c = cmd.replace("%f", epub)
    env = dict(os.environ, FEEDZINE_EPUB=epub)
    try:
        r = subprocess.run(c, shell=True, env=env, timeout=600)
    except subprocess.TimeoutExpired:
        log("  ! post_issue: таймаут 600с")
        return
    if r.returncode != 0:
        log(f"  ! post_issue упал ({r.returncode})")


def prune_issues(out, title, keep, ext="epub"):
    """Оставляет последние `keep` выпусков (по номеру в имени), старые удаляет."""
    if keep <= 0:
        return
    prefix = sanitize(title) + "_"

    def num(f):
        m = re.match(rf"{re.escape(prefix)}(\d+)_", f)
        return int(m.group(1)) if m else -1

    files = sorted((f for f in os.listdir(out)
                    if num(f) >= 0 and f.endswith("." + ext)), key=num)
    for f in files[:-keep]:
        os.remove(os.path.join(out, f))
        log(f"  − старый выпуск удалён: {f}")


# ---------------------------------------------------------------- выпуск

# ---------------------------------------------------------------- периоды

def _period_key(period, d):
    """Ключ периода даты: ISO-неделя или месяц."""
    if period == "month":
        return f"{d.year:04d}-{d.month:02d}"
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def period_ready(period, last_emit, now):
    """Наступила ли граница периода — пора собирать сводку.

    day — всегда; week/month — период last_emit отличается от текущего.
    last_emit=None (ни разу не собирали) — копим до первой границы.
    """
    if period == "day":
        return True
    if not last_emit:
        return False
    try:
        le = datetime.datetime.fromisoformat(last_emit)
    except ValueError:
        return True  # кривой last_emit — не блокируем выпуск
    return _period_key(period, le) != _period_key(period, now)


def _pending_add(pending, fresh):
    """Прошедшие фильтры статьи -> накопитель (JSON-сериализуемая форма).

    feed сжимается в снимок {name, full_text, section}: конфиг фида может
    измениться (или фид исчезнуть) до выпуска — сводка не должна зависеть
    от текущего конфига.
    """
    for it in fresh:
        fd = it.pop("feed")
        it["feed"] = {"name": fd.name, "full_text": fd.full_text, "section": fd.section,
                      "min_article_chars": fd.min_article_chars}
        it["date"] = it["date"].isoformat() if it["date"] else None
        pending.append(it)


def _pending_prepare(pending):
    """Накопитель -> статьи к выпуску: datetime обратно, feed -> namespace
    для article_md/_section, сортировка по дате. Копии — pending остаётся
    сериализуемым даже при провале сборки."""
    out = []
    for it in pending:
        it2 = dict(it)
        it2["feed"] = SimpleNamespace(section=it["feed"].get("section"),
                                      name=it["feed"].get("name"),
                                      full_text=it["feed"].get("full_text"),
                                      min_article_chars=it["feed"].get("min_article_chars"))
        it2["date"] = (datetime.datetime.fromisoformat(it["date"])
                       if it["date"] else None)
        out.append(it2)
    out.sort(key=lambda i: i["date"] or datetime.datetime.min)
    return out


def _journal_ids(cfg, st):
    """id журналов по фидам (порядок конфига); main — если есть фиды без
    journal или осел pending после миграции старого state."""
    ids = []
    for fd in cfg.feed:
        j = fd.journal or "main"
        if j not in ids:
            ids.append(j)
    if not ids:
        return ["main"]
    if st["pending"].get("main") and "main" not in ids:
        ids.append("main")
    return ids


def _journal_cfg(cfg, jid):
    """(title, outdir, period, keep_issues) журнала. main живёт в cfg.out
    под cfg.title — конфиг без journal работает как раньше, один в один."""
    jc = cfg.journal.get(jid)
    title = jc.title if jc and jc.title else (cfg.title if jid == "main" else jid)
    outdir = cfg.out if jid == "main" else f"{cfg.out}/{sanitize(jid)}"
    period = (jc.period if jc and jc.period else "") or cfg.period
    keep = (jc.keep_issues if jc and jc.keep_issues else 0) or cfg.keep_issues
    return title, outdir, period, keep


def _emit_journal(cfg, opts, st, jid, imgdir, now):
    """Один журнал: граница периода, сборка, пост-шаги.

    False — сборка не удалась (pending сохранён, граница не сдвинута).
    «Нечего собирать» и «не пора» — это True: журнал в порядке.
    """
    title, outdir, period, keep = _journal_cfg(cfg, jid)
    pending = st["pending"].setdefault(jid, [])
    ready = opts.force or period_ready(period, st["last_emit"].get(jid), now)
    if not pending:
        log(f"{title}: нового нет — выпуск не собираю")
        return True
    if not ready:
        log(f"{title}: накопил {len(pending)} статей — сводка по границе {period}, не сейчас")
        return True

    date_str = now.strftime("%Y-%m-%d")
    articles = _pending_prepare(pending)

    # сборка markdown; пустое говно (голые ссылки/заглушки) отсеивается;
    # деградации (полный текст не взялся, картинки 403) собираются
    parts, first_img, failures = {}, None, []
    with console.status(f"{title}: {len(articles)} статей"):
        for it in articles:
            m = article_md(it, cfg, imgdir, opts, failures=failures)
            if m is None:
                continue
            parts.setdefault(_section(it["feed"]), []).append(m + "\n")
            if first_img is None and not opts.text_only:
                m2 = re.search(r"!\[[^\]]*\]\(img/[^/]+/([^)]+)\)", m)
                if m2:
                    first_img = f"{imgdir}/{m2.group(1)}"
    if not parts:
        log(f"{title}: всё отсеялось пустым — выпуск не собираю")
        pending.clear()
        st["last_emit"][jid] = date_str
        return True

    # пул ретраев: полный текст не взялся — попробуем в следующем выпуске
    pool = st.setdefault("retry_fulltext", {})
    img_total = 0
    for f in failures:
        if f["kind"] == "fulltext" and f.get("guid"):
            old = pool.get(f["guid"]) or {}
            pool[f["guid"]] = {**f, "jid": jid, "kind": None,
                               "tries": old.get("tries", 0),
                               "added": old.get("added") or now.isoformat()}
        elif f["kind"] == "image":
            img_total += f.get("count", 0)

    # отдельный граф: что не попало в выпуск — фиды лежат, полные тексты
    # в ретрае, картинки не скачались. Читатель видит ограничения.
    report = [f"— фид «{d['name']}» не синкался с {d.get('since')}: {d.get('error')}"
              for d in st.get("down_feeds", {}).values() if d.get("jid") == jid]
    report += [f"— «{e['title'][:60]}»: полный текст не взялся ({e.get('error')}); "
               f"попытка {e.get('tries', 0) + 1}/{RETRY_MAX_TRIES}"
               for e in pool.values() if e.get("jid") == jid]
    if img_total:
        report.append(f"— картинок не скачалось: {img_total} (403/таймаут/битые)")
    if report:
        parts["Что не попало в выпуск"] = ["\n".join(report) + "\n"]

    st["issue"][jid] = st["issue"].get(jid, 0) + 1
    n = st["issue"][jid]
    md = [f"# {title} №{n}\n\n*{now.strftime('%d %B %Y')}*\n"]
    for fname, blocks in parts.items():
        md.append(f"\n## {fname}\n")
        md.extend(blocks)
    md_file = f"{cfg.workdir}/issue_{jid}_{n}.md"
    with open(md_file, "w") as f:
        f.write("\n".join(md))

    fmt = opts.format or cfg.format

    def gen_cover():
        return make_cover(f"{cfg.workdir}/cover_{jid}_{n}.png", title, n, date_str,
                          list(parts), size=PRESETS[opts.preset].get("cover"),
                          pattern=cfg.cover_pattern)

    cover = None
    if fmt == "epub" and cfg.cover != "off":
        if cfg.cover == "image":
            cover = first_img
        elif cfg.cover == "generated":
            cover = gen_cover()
        else:  # auto
            cover = first_img or gen_cover()
        if cover and cover is not first_img:
            log(f"обложка: {cover}")

    os.makedirs(outdir, exist_ok=True)
    out_file = out_name(outdir, title, n, date_str, fmt)
    if build_output(md_file, out_file, fmt, f"{title} №{n}",
                    cfg.workdir, cover=cover, date=date_str,
                    toc_depth=cfg.toc_depth):
        log(f"готово: {out_file}  ({os.path.getsize(out_file) / 1e6:.1f} МБ)")
        if cfg.calibre_library:
            calibre_add(out_file, cfg.calibre_library)
        run_post_issue(cfg.post_issue, out_file)
        prune_issues(outdir, title, keep,
                     ext={"epub": "epub", "html": "html", "md": "md"}[fmt])
        # сводка вышла: накопитель пуст, граница зафиксирована
        pending.clear()
        st["last_emit"][jid] = date_str
        return True
    return False


RETRY_MAX_TRIES = 5      # попыток полного текста на статью
RETRY_MAX_DAYS = 14      # после — сдаёмся (в тексте уже есть пометка)


def _retry_fulltext(cfg, st, now):
    """Повторная выгрузка полного текста для провалившихся.

    Успех -> статья уезжает в копилку журнала секцией «Докатнуто · …»
    и выйдет следующим выпуском; неудача -> счётчик попыток. Исчерпал
    попытки/устарел -> выкидываем (заглушка со сводкой уже вышла).
    """
    pool = st.get("retry_fulltext") or {}
    for guid, e in list(pool.items()):
        added = datetime.datetime.fromisoformat(e.get("added") or "1970-01-01")
        if e.get("tries", 0) >= RETRY_MAX_TRIES or (now - added).days > RETRY_MAX_DAYS:
            pool.pop(guid)
            continue
        full = None
        try:
            full, _fa = fetch_full(e["link"], sniff=e.get("html"))
        except Exception:  # noqa: BLE001
            pass
        if not full:
            e["tries"] = e.get("tries", 0) + 1
            continue
        pool.pop(guid)
        fdp = dict(e.get("feed") or {})
        fdp["section"] = f"Докатнуто · {fdp.get('section') or fdp.get('name') or 'RSS'}"
        item = {"title": e["title"], "link": e["link"], "guid": guid,
                "date": (datetime.datetime.fromisoformat(e["date"])
                         if e.get("date") else None),
                "html": full, "author": e.get("author") or "",
                "tags": [], "feed": SimpleNamespace(**fdp)}
        log(f"  + докатка: полный текст взялся — {e['title'][:60]}")
        _pending_add(st["pending"].setdefault(e.get("jid", "main"), []), [item])


def run_issue(cfg, opts):
    """Сборка выпусков по готовому Config (пути уже раскрыты). -> код возврата.

    Фиды группируются по журналам (feed.journal, default "main"): у
    каждого свой заголовок, каталог, период, счётчик и накопитель.
    seen — общий пул на все журналы. Провал сборки одного журнала не
    трогает остальные; статьи проваленного остаются в накопителе.
    """
    os.makedirs(cfg.workdir, exist_ok=True)
    os.makedirs(cfg.out, exist_ok=True)
    imgdir = f"{cfg.workdir}/img/{opts.preset}"

    with console.status("читаю фиды…"):
        fresh, all_guids, st = collect_items(cfg, opts)

    now = datetime.datetime.now()
    # докатка: повторяем полные тексты, провалившиеся в прошлых выпусках
    _retry_fulltext(cfg, st, now)

    # свежие статьи — по журналам
    by_j = {}
    for it in fresh:
        by_j.setdefault(it["feed"].journal or "main", []).append(it)
    for jid, items in by_j.items():
        _pending_add(st["pending"].setdefault(jid, []), items)

    if opts.dry_run:
        pool_n = len(st.get("retry_fulltext") or {})
        down_n = len(st.get("down_feeds") or {})
        extra = (f"; ретраев полного текста {pool_n}"
                 + (f"; фидов лежит {down_n}" if down_n else ""))
        for jid in _journal_ids(cfg, st):
            title, _outdir, period, _keep = _journal_cfg(cfg, jid)
            pend = st["pending"].get(jid, [])
            ready = opts.force or period_ready(period, st["last_emit"].get(jid), now)
            log(f"[dry-run] {title}: в накопителе {len(pend)}; "
                + ("пора собирать сводку" if ready else f"коплю до границы ({period})"))
            for it in pend[:10]:
                fd = it["feed"]
                log(f"  {fd.get('section') or fd.get('name') or 'RSS'} :: {it['title']}")
        log(f"[dry-run] деградации{extra or ': нет'}")
        return 0

    date_str = now.strftime("%Y-%m-%d")
    # весь фид прочитан: и попавшее в выпуск, и отсеянное капом, и старое
    for g in all_guids:
        st["seen"][g] = date_str

    rc = 0
    for jid in _journal_ids(cfg, st):
        try:
            if not _emit_journal(cfg, opts, st, jid, imgdir, now):
                rc = 1
        except Exception as e:  # noqa: BLE001
            log(f"! журнал {jid}: сборка упала ({e}) — статьи сохранены в накопителе")
            rc = 1
    save_state(cfg.workdir, st)
    return rc


# ---------------------------------------------------------------- backfill

def backfill_plan(cfg, weeks, now=None):
    """План восстановления истории: ВСЕ статьи фидов в окне последних
    weeks недель (seen игнорируется), сгруппированные по (журнал,
    ISO-неделя). Сеть, без state.

    -> (план {jid: {week_key: [items]}}, guid'ы окна)
    Без даты или старше окна не попадают: дату не определить.
    Фид отдаёт столько истории, сколько отдаёт — окно режет, но не
    расширяет доступное.
    """
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(weeks=weeks)
    plan, window = {}, set()
    for fd in cfg.feed:
        try:
            _ftitle, items = parse_feed(http_get(fd.url))
        except Exception as e:  # noqa: BLE001
            log(f"  ! фид {fd.name or fd.url} не прочитался: {e}")
            continue
        for it in items:
            if not it["guid"] or it["date"] is None or it["date"] < cutoff:
                continue
            it["feed"] = fd
            window.add(it["guid"])
            jid = fd.journal or "main"
            plan.setdefault(jid, {}).setdefault(_period_key("week", it["date"]), []).append(it)
    return plan, window


def _cap_bucket(items, cfg):
    """Кап фида внутри одной недели (fd.max / max_per_feed), порядок фида."""
    cnt, out = {}, []
    for it in items:
        fd = it["feed"]
        cap = fd.max if fd.max is not None else cfg.max_per_feed
        c = cnt.get(fd.url, 0)
        cnt[fd.url] = c + 1
        if c < cap:
            out.append(it)
    return out


def run_backfill(cfg, opts, weeks):
    """Недельные выпуски за последние weeks недель, без учёта seen.

    История берётся из того, что фиды отдают сейчас; выпуски датируются
    последней статьёй своей недели. Вышедшее помечается прочитанным и
    вычищается из накопителей, чтобы regular-поток не выдал двойное;
    накопленное вне окна (без даты/старое) возвращается на место.
    """
    st = load_state(cfg.workdir)

    plan, window = backfill_plan(cfg, weeks)
    total = sum(len(b) for j in plan.values() for b in j.values())
    if not total:
        log(f"в окне {weeks} нед пусто — фиды не отдают столько истории")
        return 0

    if opts.dry_run:
        for jid, buckets in plan.items():
            title, _o, _p, _k = _journal_cfg(cfg, jid)
            for wk in sorted(buckets):
                log(f"[dry-run] {title} {wk}: {len(_cap_bucket(buckets[wk], cfg))} статей")
        log(f"[dry-run] окно {weeks} нед: {total} статей, {len(window)} guid'ов")
        return 0

    # файловая система трогается только после dry-run-возврата
    os.makedirs(cfg.workdir, exist_ok=True)
    os.makedirs(cfg.out, exist_ok=True)
    imgdir = f"{cfg.workdir}/img/{opts.preset}"

    emit_opts = replace(opts, force=True)
    old_pending = {jid: list(p) for jid, p in st["pending"].items()}
    emitted = set()
    rc = 0
    for jid, buckets in plan.items():
        title, _o, _p, _k = _journal_cfg(cfg, jid)
        for wk in sorted(buckets):
            items = _cap_bucket(buckets[wk], cfg)
            if not items:
                continue
            # выпуск датируется последней статьёй недели, не «сегодня»
            wk_now = max(it["date"] for it in items)
            st["pending"][jid] = []
            _pending_add(st["pending"][jid], items)
            try:
                if not _emit_journal(cfg, emit_opts, st, jid, imgdir, wk_now):
                    rc = 1
            except Exception as e:  # noqa: BLE001
                log(f"! {title} {wk}: сборка упала ({e})")
                rc = 1
            finally:
                st["pending"][jid] = []  # неделя либо вышла, либо нет — целиком
            emitted.update(it["guid"] for it in items)

    # окно прочитано: и вышедшее, и срезанное капом
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    for g in window:
        st["seen"][g] = date_str
    # накопители: вернуть то, что вне окна и не вышло
    st["pending"] = {}
    for jid, pend in old_pending.items():
        rest = [p for p in pend if p.get("guid") not in emitted]
        if rest:
            st["pending"][jid] = rest
    save_state(cfg.workdir, st)
    return rc


# ---------------------------------------------------------------- статистика фидов

@dataclass
class FeedStat:
    name: str
    url: str
    total: int = 0
    new: int = 0
    error: str | None = None


def feed_stats(cfg, seen):
    """Статистика по всем фидам (сеть, per-feed best-effort) — для list и TUI."""
    rows = []
    for fd in cfg.feed:
        r = FeedStat(name=fd.name or fd.url, url=fd.url)
        try:
            name, items = parse_feed(http_get(fd.url))
            if not fd.name:
                r.name = name or fd.url
            r.total = len(items)
            r.new = sum(1 for i in items if i["guid"] and i["guid"] not in seen)
        except Exception as e:  # noqa: BLE001
            r.error = str(e)
        rows.append(r)
    return rows


# ---------------------------------------------------------------- TUI (textual)

def _ft_label(v):
    return {None: "·", True: "full", False: "сводки"}.get(v, str(v))


class FeedzineApp(App):
    """Отметить фиды, подкрутить max/full_text, собрать выпуск в фоне."""

    TITLE = "feedzine"
    BINDINGS = [
        ("space", "toggle", "вкл/выкл"),
        ("f", "fulltext", "full_text"),
        ("plus", "max_up", "+1"),
        ("minus", "max_down", "−1"),
        ("i", "build", "выпуск"),
        ("I", "build_force", "форс-сводка"),
        ("d", "dryrun", "dry-run"),
        ("r", "refresh", "обновить"),
        ("q", "quit", "выйти"),
    ]

    def __init__(self, cfg, opts):
        super().__init__()
        self.cfg = cfg
        self.opts = opts
        self.rows = []       # ephemeral-правки: {include, max, ft}
        self.row_keys = []
        self.col_keys = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="jbar")
        yield DataTable()
        yield RichLog(id="rlog", wrap=True, max_lines=500)
        yield Footer()

    def on_mount(self):
        t = self.query_one(DataTable)
        t.cursor_type = "row"
        self.col_keys = t.add_columns("✓", "фид", "новых", "max", "full_text", "журнал")
        for fd in self.cfg.feed:
            self.rows.append({"include": True, "max": fd.max, "ft": fd.full_text})
            self.row_keys.append(t.add_row(
                "✓", fd.name or fd.url, "…",
                str(fd.max or self.cfg.max_per_feed), _ft_label(fd.full_text),
                fd.journal or "main"))
        self._render_jbar()
        self._refresh_worker()

    # -- данные (в worker-тредах) --

    def _refresh_worker(self):
        self.run_worker(self._refresh, thread=True, exclusive=True)

    def _refresh(self):
        st = load_state(self.cfg.workdir)
        stats = feed_stats(self.cfg, st["seen"])
        self.call_from_thread(self._render_stats, stats)

    def _effective_cfg(self):
        """Конфиг с ephemeral-правками таблицы и только отмеченными фидами."""
        cfg2 = self.cfg.model_copy(deep=True)
        for i, fd in enumerate(cfg2.feed):
            r = self.rows[i]
            fd.max = r["max"]
            fd.full_text = r["ft"]
        cfg2.feed = [fd for i, fd in enumerate(cfg2.feed) if self.rows[i]["include"]]
        return cfg2

    def _run_in_thread(self, opts2):
        """run_issue в фоне; вывод — в RichLog, итог — в notify."""
        def work():
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = run_issue(self._effective_cfg(), opts2)
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self.notify, f"сборка упала: {e}", severity="error")
                return
            self.call_from_thread(self._after_run, rc, buf.getvalue())
        self.run_worker(work, thread=True, exclusive=True)

    # -- UI (в основном потоке) --

    def _render_stats(self, stats):
        t = self.query_one(DataTable)
        for i, s in enumerate(stats):
            t.update_cell(self.row_keys[i], self.col_keys[2],
                          "недоступен" if s.error else str(s.new))

    def _render_jbar(self):
        """Статус журналов: номер выпуска, копилка, период."""
        st = load_state(self.cfg.workdir)
        parts = []
        for jid in _journal_ids(self.cfg, st):
            title, _out, period, _keep = _journal_cfg(self.cfg, jid)
            parts.append(f"{title}: №{st['issue'].get(jid, 0)} · "
                         f"копилка {len(st['pending'].get(jid, []))} · {period}")
        self.query_one("#jbar", Static).update("  │  ".join(parts) or "нет журналов")

    def _after_run(self, rc, out):
        rlog = self.query_one("#rlog", RichLog)
        for line in out.splitlines():
            if line.strip():
                rlog.write(line)
        lines = [l for l in out.splitlines() if l.strip()]
        msg = lines[-1] if lines else ("готово" if rc == 0 else "не собралось")
        self.notify(msg, severity="information" if rc == 0 else "warning")
        self._render_jbar()
        self._refresh_worker()

    def _sel(self):
        t = self.query_one(DataTable)
        return t.cursor_row if t.row_count else None

    def _update_row(self, i):
        t = self.query_one(DataTable)
        r = self.rows[i]
        t.update_cell(self.row_keys[i], self.col_keys[0], "✓" if r["include"] else "·")
        t.update_cell(self.row_keys[i], self.col_keys[3], str(r["max"] or self.cfg.max_per_feed))
        t.update_cell(self.row_keys[i], self.col_keys[4], _ft_label(r["ft"]))

    # -- действия --

    def action_build(self):
        # «i» — обычный прогон: периоды журналов уважаются (недельная
        # сводка не соберётся раньше границы), «I» — форс из копилки
        if not self._effective_cfg().feed:
            self.notify("ничего не отмечено")
            return
        self._run_in_thread(replace(self.opts, force=False))

    def action_build_force(self):
        if not self._effective_cfg().feed:
            self.notify("ничего не отмечено")
            return
        self._run_in_thread(replace(self.opts, force=True))

    def action_dryrun(self):
        if not self._effective_cfg().feed:
            self.notify("ничего не отмечено")
            return
        self._run_in_thread(replace(self.opts, dry_run=True))

    def action_toggle(self):
        i = self._sel()
        if i is None:
            return
        self.rows[i]["include"] = not self.rows[i]["include"]
        self._update_row(i)

    def action_fulltext(self):
        i = self._sel()
        if i is None:
            return
        r = self.rows[i]
        r["ft"] = {None: True, True: False, False: None}.get(r["ft"])
        self._update_row(i)

    def action_max_up(self):
        i = self._sel()
        if i is None:
            return
        r = self.rows[i]
        r["max"] = (r["max"] or self.cfg.max_per_feed) + 1
        self._update_row(i)

    def action_max_down(self):
        i = self._sel()
        if i is None:
            return
        r = self.rows[i]
        r["max"] = max(1, (r["max"] or self.cfg.max_per_feed) - 1)
        self._update_row(i)

    def action_refresh(self):
        self._refresh_worker()


# ---------------------------------------------------------------- webui

WEB_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>feedzine</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,sans-serif;background:#14161a;color:#d7dae0;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#7d8590;font-size:13px;margin-bottom:20px}
h2{font-size:15px;color:#9fb3c8;margin:18px 0 8px;text-transform:uppercase;letter-spacing:.08em}
.cards{display:flex;flex-wrap:wrap;gap:12px}
.card{background:#1d2026;border:1px solid #2a2e36;border-radius:10px;padding:12px 16px;min-width:200px}
.card .t{font-weight:600}
.card .m{color:#7d8590;font-size:13px;margin-top:4px}
.card .big{font-size:26px;font-weight:700;margin:2px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #23262d}
th{color:#7d8590;font-weight:500}
tr:hover td{background:#1a1d22}
button{background:#2c5f8a;border:none;color:#fff;border-radius:8px;padding:9px 16px;
font-size:14px;cursor:pointer;margin-right:8px}
button.sec{background:#2a2e36}
button:disabled{opacity:.45;cursor:default}
label{font-size:14px;color:#9fb3c8;margin-right:16px}
#log{background:#0e1013;border:1px solid #23262d;border-radius:10px;padding:12px;
font:12px/1.55 ui-monospace,monospace;white-space:pre-wrap;max-height:340px;overflow-y:auto;color:#a8b3bf}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.ok{background:#3fb26a}.run{background:#d9a53f}.err{background:#d05c5c}
.row{display:flex;align-items:center;gap:8px;margin:14px 0}
</style></head><body>
<h1>feedzine</h1>
<div class="sub" id="sub">…</div>

<h2>Журналы</h2>
<div class="cards" id="journals"></div>

<h2>Фиды</h2>
<div class="row">
<button id="scan">проверить фиды</button>
<button id="build">собрать выпуск</button>
<label><input type="checkbox" id="force"> форс-сводка</label>
</div>
<table id="feeds"><thead>
<tr><th>фид</th><th>журнал</th><th>в фиде</th><th>новых</th></tr>
</thead><tbody></tbody></table>

<h2>Лог</h2>
<div id="log"></div>

<script>
const $=id=>document.getElementById(id);
let busy=false;
async function api(path,opts){const r=await fetch(path,opts);return r.json()}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

async function overview(){
  const d=await api('/api/overview');
  $('sub').textContent=d.title+' · '+d.out+' · период '+d.period;
  $('journals').innerHTML=d.journals.map(j=>`
    <div class="card"><div class="t">${esc(j.title)}</div>
    <div class="big">№${j.issue}</div>
    <div class="m">копилка: ${j.pending} · ${j.period} · ${j.last_emit?('вышел '+j.last_emit):'ещё не выходил'}</div>
    </div>`).join('')||'<div class="card"><div class="m">журналов нет</div></div>';
  const rows=d.feeds.map(f=>{
    const s=(d.scan&&d.scan.rows[f.name])||null;
    return `<tr><td>${esc(f.name)}</td><td>${esc(f.journal)}</td>
    <td>${s?s.total:'—'}</td><td>${s?(s.error?'<span class="dot err"></span>ошибка':s.new):'—'}</td></tr>`;
  });
  $('feeds').querySelector('tbody').innerHTML=rows.join('');
  if(d.scan)$('scan').textContent='фиды от '+d.scan.ts.slice(11,16);
}

async function build_status(){
  const d=await api('/api/build');
  const dot=d.running?'<span class="dot run"></span>собирается…'
    :(d.rc===null?'':'<span class="dot '+(d.rc?'err':'ok')+'"></span>rc='+d.rc);
  $('build').disabled=d.running;
  $('build').textContent=d.running?'собирается…':'собрать выпуск';
  $('log').textContent=d.log.join('\\n')||'— пусто —';
  if(dot)$('sub').dataset.build=dot;
  $('log').scrollTop=$('log').scrollHeight;
}

$('build').onclick=async()=>{
  const body=JSON.stringify({force:$('force').checked});
  await fetch('/api/build',{method:'POST',body});
  build_status();
};
$('scan').onclick=async()=>{$('scan').disabled=true;$('scan').textContent='проверяю…';
  await fetch('/api/scan',{method:'POST'});setTimeout(overview,1000)};
setInterval(overview,5000);setInterval(build_status,2000);
overview();build_status();
</script></body></html>"""


def _web_overview(cfg, scan_cache):
    """JSON-снимок для webui: журналы, фиды, свежие выпуски."""
    st = load_state(cfg.workdir)
    journals = []
    for jid in _journal_ids(cfg, st):
        title, outdir, period, _keep = _journal_cfg(cfg, jid)
        journals.append({"id": jid, "title": title, "period": period,
                         "issue": st["issue"].get(jid, 0),
                         "pending": len(st["pending"].get(jid, [])),
                         "last_emit": st["last_emit"].get(jid)})
    feeds = [{"name": fd.name or fd.url, "url": fd.url,
              "journal": fd.journal or "main"} for fd in cfg.feed]
    issues = []
    exts = ("epub", "html", "md")
    for jid in _journal_ids(cfg, st):
        _t, outdir, _p, _k = _journal_cfg(cfg, jid)
        if os.path.isdir(outdir):
            for f in os.listdir(outdir):
                if f.endswith(exts):
                    p = os.path.join(outdir, f)
                    issues.append({"file": f, "path": p,
                                   "mtime": os.path.getmtime(p),
                                   "size": os.path.getsize(p)})
    issues.sort(key=lambda i: i["mtime"], reverse=True)
    out = {"title": cfg.title, "out": cfg.out, "period": cfg.period,
           "journals": journals, "feeds": feeds, "issues": issues[:15]}
    if scan_cache:
        out["scan"] = scan_cache
    return out


def make_web_server(cfg, opts, host, port):
    """HTTP-сервер webui (без serve_forever) — для запуска и тестов."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"lock": threading.Lock(),
             "build": {"running": False, "rc": None, "log": []},
             "scan": None}

    def do_build(force):
        def work():
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = run_issue(cfg, replace(opts, force=force))
            except Exception as e:  # noqa: BLE001
                buf.write(f"! сборка упала: {e}\n")
                rc = 1
            with state["lock"]:
                state["build"] = {"running": False, "rc": rc,
                                  "log": buf.getvalue().splitlines()[-200:]}
        with state["lock"]:
            if state["build"]["running"]:
                return False
            state["build"] = {"running": True, "rc": None, "log": ["стартую…"]}
        threading.Thread(target=work, daemon=True).start()
        return True

    def do_scan():
        def work():
            st = load_state(cfg.workdir)
            rows = {}
            for s in feed_stats(cfg, st["seen"]):
                rows[s.name] = {"total": s.total, "new": s.new, "error": s.error}
            with state["lock"]:
                state["scan"] = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                 "rows": rows}
        with state["lock"]:
            if state.get("scan_running"):
                return False
            state["scan_running"] = True
        def run_and_clear():
            try:
                work()
            finally:
                with state["lock"]:
                    state["scan_running"] = False
        threading.Thread(target=run_and_clear, daemon=True).start()
        return True

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/":
                self._send(200, WEB_PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/overview":
                with state["lock"]:
                    scan = dict(state["scan"], rows=dict(state["scan"]["rows"])) if state["scan"] else None
                self._send(200, json.dumps(_web_overview(cfg, scan), ensure_ascii=False))
            elif self.path == "/api/build":
                with state["lock"]:
                    self._send(200, json.dumps(state["build"], ensure_ascii=False))
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/build":
                ok = do_build(bool(body.get("force")))
                self._send(200 if ok else 409,
                           json.dumps({"started": ok}, ensure_ascii=False))
            elif self.path == "/api/scan":
                do_scan()
                self._send(200, '{"started": true}')
            else:
                self._send(404, '{"error": "not found"}')

        def log_message(self, fmt, *args):  # шум в консоль не нужен
            pass

    return ThreadingHTTPServer((host, port), H)


def run_web(cfg, opts, host, port, browser=True):
    """Локальный webui: stdlib http.server, без новых депов.

    Только просмотр и запуск сборки — конфиг web не правит (он может быть
    менеджеримым home-manager'ом). Сборка — в фоне, single-flight; лог
    тянется опросом /api/build. По умолчанию слушаем 127.0.0.1 — наружу
    не выставляй без понимания, что это без авторизации.
    """
    srv = make_web_server(cfg, opts, host, port)
    url = f"http://{host}:{srv.server_address[1]}/"
    log(f"webui: {url}")
    if browser:
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ---------------------------------------------------------------- CLI (typer)

EXAMPLE_CFG = '''# feedzine — пример конфига (~/.config/feedzine/config.toml)
title = "Мой журнал"
out = "~/Books/feedzine"          # куда класть выпуски
preset = "reader"                # reader | eink | mini | tiny | hq
full_text = "auto"               # auto: полный текст для Habr, сводки для остальных
max_per_feed = 10                # статей на фид в выпуске
min_article_chars = 120           # меньше прозы (без ссылок/картинок) — не статья; 0 = не фильтровать
include_tags = []                # теги статьи из фида: ["python", "go"]; [] = без фильтра
exclude_tags = []                # напр. ["из песочницы", "перевод"]
cover = "auto"                   # auto: первая картинка выпуска, нет её — сгенерированный узор
cover_pattern = "auto"            # узор: auto (разный каждый выпуск) | mandelbrot | julia | truchet
                                 # | phyllotaxis | moire | tree | ridge | sierpinski
format = "epub"                  # epub | html (один файл с картинками) | md
img_quality = 0                  # 0 = качество пресета; иначе JPEG quality 40..95
toc_depth = 3                    # оглавление: 2 — только фиды, 3 — и статьи
period = "day"                   # day | week | month: сводки вместо потока (недельные — в пн)
keep_issues = 0                  # 0 = хранить все выпуски; N = оставить последние N
# calibre_library = "~/Calibre"  # добавлять выпуск в calibre (нужен calibredb в PATH)
# post_issue = "rsync -a %f reader:/books/"   # хук доставки после сборки (%f = EPUB)

[[feed]]
name = "Хабр · статьи"
url = "https://habr.com/ru/rss/articles/"

[[feed]]
name = "Хабр · Python"
url = "https://habr.com/ru/rss/hubs/python/"
max = 5

[[feed]]
name = "Hacker News"
url = "https://hnrss.org/frontpage"
full_text = true            # сниффер: вытащить полный текст статьи по ссылке из фида
journal = "hn"              # отдельный журнал — выпуски в out/hn/

[journal.hn]                # настройки отдельного журнала (пусто — глобальные)
title = "HN Дайджест"
period = "week"             # свой период: main — daily, HN — раз в неделю

[[feed]]
name = "DTF · главное"
url = "https://dtf.ru/rss/all"
full_text = false                # только сводки
exclude_tags = ["реклама"]       # тег-фильтры можно и на фиде (переопределяют глобальные)
'''

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="RSS/Atom фиды → периодические «журналы» EPUB")

CONFIG_OPT = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="путь к конфигу")


def _load(path):
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        log(f"нет конфига {p} — feedzine init его создаст")
        raise typer.Exit(2)
    cfg = load_config(p)
    cfg.out = os.path.expanduser(cfg.out)
    cfg.workdir = os.path.expanduser(cfg.workdir)
    cfg.calibre_library = os.path.expanduser(cfg.calibre_library)
    return cfg


@app.command("init")
def init_cmd(config: str = CONFIG_OPT):
    """написать пример конфига"""
    path = os.path.expanduser(config)
    if os.path.exists(path):
        log(f"уже есть: {path}")
        raise typer.Exit(1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(EXAMPLE_CFG)
    log(f"написал пример конфига: {path}")


@app.command("list")
def list_cmd(config: str = CONFIG_OPT,
             as_json: bool = typer.Option(False, "--json", help="машиночитаемо")):
    """фиды и состояние"""
    cfg = _load(config)
    st = load_state(cfg.workdir)
    rows = feed_stats(cfg, st["seen"])
    if as_json:
        print(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=1))
        return
    console.print(f"выпусков собрано: {st['issue']}, "
                  f"статей отмечено прочитанными: {len(st['seen'])}")
    t = Table(box=None, show_header=True)
    t.add_column("фид")
    t.add_column("в фиде", justify="right")
    t.add_column("новых", justify="right", style="bold")
    for r in rows:
        if r.error:
            t.add_row(r.name, "—", "[red]недоступен[/red]")
        else:
            t.add_row(r.name, str(r.total), str(r.new))
    console.print(t)


@app.command("issue")
def issue_cmd(config: str = CONFIG_OPT,
              preset: str = typer.Option("reader", help="пресет картинок"),
              text_only: bool = typer.Option(False, "--text-only", help="без картинок"),
              dry_run: bool = typer.Option(False, "--dry-run",
                                           help="что попадёт в выпуск; state не менять"),
              format: str = typer.Option(None, "--format",
                                         help="epub | html | md (по умолчанию из конфига)"),
              force: bool = typer.Option(False, "--force",
                                         help="собрать сводку сейчас, не дожидаясь границы периода")):
    """собрать выпуск из нового в фидах"""
    if preset not in PRESETS:
        log(f"preset {preset!r} не из {sorted(PRESETS)}")
        raise typer.Exit(2)
    if format is not None and format not in ("epub", "html", "md"):
        log("format должен быть epub | html | md")
        raise typer.Exit(2)
    raise typer.Exit(run_issue(_load(config), RunOpts(preset=preset,
                                                      text_only=text_only,
                                                      dry_run=dry_run,
                                                      format=format,
                                                      force=force)))


@app.command("backfill")
def backfill_cmd(config: str = CONFIG_OPT,
                 weeks: int = typer.Option(12, "--weeks", "-w", min=1, max=260,
                                           help="сколько недель истории собрать"),
                 preset: str = typer.Option("reader", help="пресет картинок"),
                 text_only: bool = typer.Option(False, "--text-only", help="без картинок"),
                 dry_run: bool = typer.Option(False, "--dry-run",
                                               help="план недель; state и файлы не трогать"),
                 format: str = typer.Option(None, "--format",
                                           help="epub | html | md (по умолчанию из конфига)")):
    """пересобрать историю: недельные выпуски за последние N недель"""
    if preset not in PRESETS:
        log(f"preset {preset!r} не из {sorted(PRESETS)}")
        raise typer.Exit(2)
    if format is not None and format not in ("epub", "html", "md"):
        log("format должен быть epub | html | md")
        raise typer.Exit(2)
    raise typer.Exit(run_backfill(_load(config), RunOpts(preset=preset,
                                                         text_only=text_only,
                                                         dry_run=dry_run,
                                                         format=format), weeks))


@app.command("tui")
def tui_cmd(config: str = CONFIG_OPT,
            preset: str = typer.Option("reader", help="пресет картинок")):
    """интерактивно: отметить фиды, собрать выпуск"""
    if preset not in PRESETS:
        log(f"preset {preset!r} не из {sorted(PRESETS)}")
        raise typer.Exit(2)
    FeedzineApp(_load(config), RunOpts(preset=preset)).run()


@app.command("web")
def web_cmd(config: str = CONFIG_OPT,
            preset: str = typer.Option("reader", help="пресет картинок"),
            host: str = typer.Option("127.0.0.1", help="адрес; 0.0.0.0 — слушать все интерфейсы"),
            port: int = typer.Option(8097, help="порт"),
            no_browser: bool = typer.Option(False, "--no-browser", help="не открывать браузер")):
    """webui: журналы, фиды, сборка выпуска из браузера"""
    if preset not in PRESETS:
        log(f"preset {preset!r} не из {sorted(PRESETS)}")
        raise typer.Exit(2)
    run_web(_load(config), RunOpts(preset=preset), host, port, browser=not no_browser)


def main():
    app(prog_name="feedzine")


if __name__ == "__main__":
    main()
