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
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, replace
from types import SimpleNamespace

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from rich.console import Console
from rich.table import Table
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
DEFAULT_CONFIG = "~/.config/feedzine/config.toml"

console = Console(highlight=False)


def log(s):
    # markup=False: имена фидов с [скобками] не должны парситься как разметка
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
    p = f"{workdir}/state.json"
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except json.JSONDecodeError:
            pass
    return {"seen": {}, "issue": 0}


def save_state(workdir, st):
    with open(f"{workdir}/state.json", "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- полный текст

def habr_id(link):
    m = re.search(r"habr\.com/(?:\w{2}/)?articles/(\d+)", link or "")
    return m.group(1) if m else None


def fetch_full(link):
    """Полный текст статьи -> (html, author) или None. Habr — kek/v2, прочее — <article>."""
    hid = habr_id(link)
    if hid:
        d = json.loads(http_get(f"https://habr.com/kek/v2/articles/{hid}/"))
        author = ((d.get("author") or {}).get("fullname")) or ((d.get("author") or {}).get("alias"))
        return d.get("textHtml"), author
    # generic: <article>…</article> из SSR-страницы
    page = http_get(link)
    m = re.search(r"<article\b[^>]*>(.*?)</article>", page, re.S | re.I)
    if m and len(m.group(1)) > 500:
        return m.group(1), None
    return None, None


# ---------------------------------------------------------------- картинки

def img_name(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"


def process_image(url, imgdir, opts, base=None, q=None):
    """Скачивает и обрабатывает под e-ink (true gray, бокс, baseline JPEG).

    Относительные URL (/img/x.png) резолвятся от base (origin статьи).
    q — переопределение JPEG quality (None → качество пресета).
    Идемпотентно: готовый файл в кэше не трогаем. -> имя локального файла или None.
    """
    if url.startswith("/") and base:
        url = base.rstrip("/") + url
    name = img_name(url)
    dst = f"{imgdir}/{name}"
    if os.path.exists(dst):
        return name
    try:
        raw = http_get(_abs_url(url), timeout=40, binary=True)
    except Exception as e:  # noqa: BLE001
        log(f"  ! картинка не скачалась: {url[:60]}… ({e})")
        return None
    if not HAS_PIL:
        os.makedirs(imgdir, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(raw)
        return name
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
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
    return name


def _abs_url(u):
    return "https:" + u if u.startswith("//") else u


def rewrite_images(html, imgdir, opts, rel="../img", base=None, q=None):
    """<img src> → локальный rel/<hash>.jpg. Возвращает (html, [локальные имена])."""
    done = []

    def _sub(m):
        url = m.group(1)
        if url.startswith("data:"):
            return m.group(0)
        name = process_image(url, imgdir, opts, base, q=q)
        if name:
            done.append(name)
            return m.group(0).replace(url, f"{rel}/{name}")
        return ""  # битая картинка — выкидываем тег

    out = re.sub(r'<img\b[^>]*src="([^"]+)"[^>]*/?>', _sub, html)
    return out, done


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


def make_cover(dst, title, n, date_str, sections, size=None):
    """Типографская обложка под e-ink: grayscale, двойная рамка, заголовок,
    номер выпуска, дата, секции. Размер — от пресета экрана (маленькие
    читалки получают маленькую обложку, типографика масштабируется).
    Без PIL — None. Чистый ч/б без полутонов: на e-ink серые плашки
    превращаются в шум.
    """
    if not HAS_PIL:
        return None
    from PIL import ImageDraw, ImageFont

    W, H = size if size else (600, 800)
    sc = W / 600.0  # масштаб типографики под ширину обложки

    def px(v):
        return max(1, round(v * sc))

    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    font_path = cover_font_path()

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

    M = px(48)  # поля
    # двойная рамка — классический титульный лист
    d.rectangle([M, M, W - M, H - M], outline=0, width=max(2, px(3)))
    d.rectangle([M + px(8), M + px(8), W - M - px(8), H - M - px(8)], outline=0, width=1)

    y = M + px(70)
    for line in wrap(title, font(px(40)), W - 2 * (M + px(40))):
        center(y, line, font(px(40)))
        y += px(52)

    # номер выпуска — центр страницы
    center(H / 2 - px(90), "№", font(px(36)))
    center(H / 2 - px(20), str(n), font(px(150)))
    center(H / 2 + px(160), date_str, font(px(30)))

    # секции внизу колонкой — сколько влезает без наезда на дату
    top = H / 2 + px(200)
    count = max(0, min(len(sections), 8,
                       int((H - M - px(16) - top) // px(30))))
    y = H - M - px(16) - px(30) * count
    for s in sections[:count]:
        center(y, s, font(px(22)))
        y += px(30)

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
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    cover: str = "auto"              # auto | image | generated | off
    format: str = "epub"             # epub | html | md
    img_quality: int = 0             # 0 = качество пресета; иначе JPEG quality 40..95
    toc_depth: int = 3               # глубина оглавления: 2 — только фиды, 3 — и статьи
    period: str = "day"              # day | week | month: сводки вместо потока
    keep_issues: int = 0             # 0 = хранить все; N = оставить последние N
    calibre_library: str = ""        # непусто — добавлять выпуск через calibredb
    post_issue: str = ""             # shell-хук после сборки, %f = путь к выпуску
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
    for fd in cfg.feed:
        url = fd.url
        try:
            ftitle, items = parse_feed(http_get(url))
        except Exception as e:  # noqa: BLE001
            log(f"  ! фид {fd.name or url} не прочитался: {e}")
            continue
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


def article_md(item, cfg, imgdir, opts):
    """Статья -> markdown: шапка с метаданными + сводка/полный текст."""
    fd = item["feed"]
    ft = fd.full_text if fd.full_text is not None else cfg.full_text
    # auto: полный текст только там, где умеем надёжно (Habr); true/always: пробуем везде
    want_full = ft in (True, "always") or (ft == "auto" and habr_id(item["link"]))
    html, author = item["html"], item["author"]
    if want_full and item["link"]:
        try:
            full, fauthor = fetch_full(item["link"])
            if full:
                html, author = full, author or fauthor
        except Exception as e:  # noqa: BLE001
            log(f"  ! полный текст не взялся: {e}")
    if not opts.text_only:
        base = None
        if item["link"]:
            m = re.match(r"https?://[^/]+", item["link"])
            if m:
                base = m.group(0)
        html, _ = rewrite_images(html, imgdir, opts, rel=f"img/{opts.preset}",
                                 base=base, q=cfg.img_quality or None)
    else:
        html = re.sub(r"<img\b[^>]*>", "", html)
    body = html_to_md(html) or "_(пустая сводка)_"
    # внутренние заголовки статьи демо́тимся (fenced-коды не трогаем)
    body = demote_headings(body)
    d = item["date"].strftime("%d.%m.%Y") if item["date"] else ""
    src = fd.name or "RSS"
    head = f"*{author or src}"
    if d:
        head += f" · {d}"
    head += f" · [оригинал]({item['link']})*" if item["link"] else "*"
    return f"### {item['title']}\n\n{head}\n\n{body}\n"


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
        it["feed"] = {"name": fd.name, "full_text": fd.full_text, "section": fd.section}
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
                                      full_text=it["feed"].get("full_text"))
        it2["date"] = (datetime.datetime.fromisoformat(it["date"])
                       if it["date"] else None)
        out.append(it2)
    out.sort(key=lambda i: i["date"] or datetime.datetime.min)
    return out


def run_issue(cfg, opts):
    """Сборка выпуска по готовому Config (пути уже раскрыты). -> код возврата.

    Статьи накапливаются в state.pending (дедуп по seen — как раньше);
    сводка собирается на границе периода period=day|week|month либо по
    --force. Провал сборки статьи не теряет — они остаются в накопителе.
    """
    os.makedirs(cfg.workdir, exist_ok=True)
    os.makedirs(cfg.out, exist_ok=True)
    imgdir = f"{cfg.workdir}/img/{opts.preset}"

    with console.status("читаю фиды…"):
        fresh, all_guids, st = collect_items(cfg, opts)

    pending = st.setdefault("pending", [])
    _pending_add(pending, fresh)

    now = datetime.datetime.now()
    ready = opts.force or period_ready(cfg.period, st.get("last_emit"), now)

    if opts.dry_run:
        log(f"[dry-run] новых {len(fresh)}, в накопителе {len(pending)}; "
            + ("пора собирать сводку" if ready else f"коплю до границы ({cfg.period})"))
        for it in pending[:20]:
            fd = it["feed"]
            log(f"  {fd.get('section') or fd.get('name') or 'RSS'} :: {it['title']}")
        return 0

    date_str = now.strftime("%Y-%m-%d")
    # весь фид прочитан: и попавшее в выпуск, и отсеянное капом, и старое
    for g in all_guids:
        st["seen"][g] = date_str

    if not pending:
        log("нового нет — выпуск не собираю")
        save_state(cfg.workdir, st)
        return 0

    if not ready:
        log(f"накопил {len(pending)} статей — сводка по границе {cfg.period}, не сейчас")
        save_state(cfg.workdir, st)
        return 0

    st["issue"] += 1
    n = st["issue"]
    articles = _pending_prepare(pending)
    by_feed = {}
    for it in articles:
        by_feed.setdefault(_section(it["feed"]), []).append(it)

    md = [f"# {cfg.title} №{n}\n\n*{now.strftime('%d %B %Y')}*\n"]
    first_img = None
    with console.status(f"выпуск №{n}: {len(articles)} статей"):
        for fname, items in by_feed.items():
            md.append(f"\n## {fname}\n")
            for it in items:
                m = article_md(it, cfg, imgdir, opts)
                md.append(m + "\n")
                if first_img is None and not opts.text_only:
                    m2 = re.search(r"!\[[^\]]*\]\(img/[^/]+/([^)]+)\)", m)
                    if m2:
                        first_img = f"{imgdir}/{m2.group(1)}"
    md_file = f"{cfg.workdir}/issue_{n}.md"
    with open(md_file, "w") as f:
        f.write("\n".join(md))

    # обложка (только для epub): auto — первая картинка выпуска, нет её —
    # типографская размером с экран пресета
    fmt = opts.format or cfg.format

    def gen_cover():
        return make_cover(f"{cfg.workdir}/cover_{n}.png", cfg.title, n, date_str,
                          list(by_feed), size=PRESETS[opts.preset].get("cover"))

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

    out_file = out_name(cfg.out, cfg.title, n, date_str, fmt)
    if build_output(md_file, out_file, fmt, f"{cfg.title} №{n}",
                    cfg.workdir, cover=cover, date=date_str,
                    toc_depth=cfg.toc_depth):
        log(f"готово: {out_file}  ({os.path.getsize(out_file) / 1e6:.1f} МБ)")
        if cfg.calibre_library:
            calibre_add(out_file, cfg.calibre_library)
        run_post_issue(cfg.post_issue, out_file)
        prune_issues(cfg.out, cfg.title, cfg.keep_issues,
                     ext={"epub": "epub", "html": "html", "md": "md"}[fmt])
        # сводка вышла: накопитель пуст, граница зафиксирована
        pending.clear()
        st["last_emit"] = now.strftime("%Y-%m-%d")
    save_state(cfg.workdir, st)
    return 0


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
        yield DataTable()
        yield Footer()

    def on_mount(self):
        t = self.query_one(DataTable)
        t.cursor_type = "row"
        self.col_keys = t.add_columns("✓", "фид", "новых", "max", "full_text")
        for fd in self.cfg.feed:
            self.rows.append({"include": True, "max": fd.max, "ft": fd.full_text})
            self.row_keys.append(t.add_row(
                "✓", fd.name or fd.url, "…",
                str(fd.max or self.cfg.max_per_feed), _ft_label(fd.full_text)))
        self._refresh_worker()

    # -- данные (в worker-тредах) --

    def _refresh_worker(self):
        self.run_worker(self._refresh, thread=True, exclusive=True)

    def _refresh(self):
        st = load_state(self.cfg.workdir)
        stats = feed_stats(self.cfg, st["seen"])
        self.call_from_thread(self._render_stats, stats)

    def _build(self):
        cfg2 = self.cfg.model_copy(deep=True)
        for i, fd in enumerate(cfg2.feed):
            r = self.rows[i]
            fd.max = r["max"]
            fd.full_text = r["ft"]
        cfg2.feed = [fd for i, fd in enumerate(cfg2.feed) if self.rows[i]["include"]]
        if not cfg2.feed:
            self.call_from_thread(self.notify, "ничего не отмечено")
            return
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                # TUI: «i» = собрать сводку немедленно из накопленного
                rc = run_issue(cfg2, replace(self.opts, force=True))
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"сборка упала: {e}", severity="error")
            return
        self.call_from_thread(self._after_build, rc, buf.getvalue())

    # -- UI (в основном потоке) --

    def _render_stats(self, stats):
        t = self.query_one(DataTable)
        for i, s in enumerate(stats):
            t.update_cell(self.row_keys[i], self.col_keys[2],
                          "недоступен" if s.error else str(s.new))

    def _after_build(self, rc, out):
        lines = [l for l in out.splitlines() if l.strip()]
        msg = lines[-1] if lines else ("готово" if rc == 0 else "не собралось")
        self.notify(msg, severity="information" if rc == 0 else "warning")
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

    def action_build(self):
        self.run_worker(self._build, thread=True, exclusive=True)


# ---------------------------------------------------------------- CLI (typer)

EXAMPLE_CFG = '''# feedzine — пример конфига (~/.config/feedzine/config.toml)
title = "Мой журнал"
out = "~/Books/feedzine"          # куда класть выпуски
preset = "reader"                # reader | eink | mini | tiny | hq
full_text = "auto"               # auto: полный текст для Habr, сводки для остальных
max_per_feed = 10                # статей на фид в выпуске
include_tags = []                # теги статьи из фида: ["python", "go"]; [] = без фильтра
exclude_tags = []                # напр. ["из песочницы", "перевод"]
cover = "auto"                   # auto: первая картинка выпуска, нет её — типографская
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


@app.command("tui")
def tui_cmd(config: str = CONFIG_OPT,
            preset: str = typer.Option("reader", help="пресет картинок")):
    """интерактивно: отметить фиды, собрать выпуск"""
    if preset not in PRESETS:
        log(f"preset {preset!r} не из {sorted(PRESETS)}")
        raise typer.Exit(2)
    FeedzineApp(_load(config), RunOpts(preset=preset)).run()


def main():
    app(prog_name="feedzine")


if __name__ == "__main__":
    main()
