#!/usr/bin/env python3
"""feedzine — RSS/Atom фиды → периодические «журналы» в EPUB.

Каждый запуск `feedzine issue` собирает выпуск из статей, появившихся
в фидах с прошлого выпуска: сводка из фида или полный текст
(Habr — через kek/v2, произвольные сайты — <article>-extractor),
картинки обрабатываются под e-ink (true grayscale, бокс экрана).
Прочитанное помечается в state — в следующий выпуск попадает только новое.
"""

import argparse
import datetime
import hashlib
import html as htmllib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
import xml.etree.ElementTree as ET

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"

# Пресеты картинок: resize W, gray — настоящий ч/б, box — вписывание (W, H), q — JPEG quality.
PRESETS = {
    "reader": {"resize": 480, "gray": True, "box": (480, 800), "q": 85},
    "eink":   {"resize": 480, "gray": False, "box": (480, 800), "q": 85},
    "mini":   {"resize": 360, "gray": True, "box": (360, 600), "q": 80},
    "tiny":   {"resize": 240, "gray": True, "box": (240, 400), "q": 80},
    "hq":     {"resize": 900, "gray": False, "box": None, "q": 88},
}


def log(s):
    print(s, flush=True)


def http_get(url, timeout=25, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        return b if binary else b.decode("utf-8", "replace")


def sanitize(s):
    return re.sub(r"[^\w-]+", "_", s)[:60].strip("_") or "item"


# ---------------------------------------------------------------- RSS / Atom

def parse_feed(xml_text):
    """RSS 2.0 / Atom -> (имя фида, [item]). Поля: title, link, guid, date(datetime|None), html, author."""
    root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    items = []
    if root.tag == "rss" or root.tag.endswith("}rss"):
        ch = root.find("channel")
        name = (ch.findtext("title") or "") if ch is not None else ""
        for it in root.findall(".//item"):
            def g(tag):
                return (it.findtext(tag) or "").strip()
            items.append({
                "title": htmllib.unescape(g("title")),
                "link": g("link"),
                "guid": g("guid") or g("link"),
                "date": _parse_date(g("pubDate")),
                "html": g("description"),
                "author": (it.findtext("{http://purl.org/dc/elements/1.1/}creator") or "").strip(),
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


def process_image(url, imgdir, args, base=None):
    """Скачивает и обрабатывает под e-ink (true gray, бокс, baseline JPEG).

    Относительные URL (/img/x.png) резолвятся от base (origin статьи).
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
        im = Image.open(__import__("io").BytesIO(raw))
        im.load()
    except Exception:
        return None
    pr = PRESETS[args.preset]
    if pr["gray"]:
        im = im.convert("L")
    if pr["box"]:
        im.thumbnail(pr["box"])
    else:
        w = pr["resize"]
        if im.width > w:
            im = im.resize((w, max(1, round(im.height * w / im.width))))
    os.makedirs(imgdir, exist_ok=True)
    im.save(dst, "JPEG", quality=pr["q"], optimize=True)  # baseline, не progressive
    return name


def _abs_url(u):
    return "https:" + u if u.startswith("//") else u


def rewrite_images(html, imgdir, args, rel="../img", base=None):
    """<img src> → локальный rel/<hash>.jpg. Возвращает (html, [локальные имена])."""
    done = []

    def _sub(m):
        url = m.group(1)
        if url.startswith("data:"):
            return m.group(0)
        name = process_image(url, imgdir, args, base)
        if name:
            done.append(name)
            return m.group(0).replace(url, f"{rel}/{name}")
        return ""  # битая картинка — выкидываем тег

    out = re.sub(r'<img\b[^>]*src="([^"]+)"[^>]*/?>', _sub, html)
    return out, done


# ---------------------------------------------------------------- markdown / EPUB

def html_to_md(html):
    p = subprocess.run(["pandoc", "-f", "html", "-t", "gfm", "--wrap=none"],
                       input=html.encode(), capture_output=True)
    return p.stdout.decode("utf-8", "replace").strip()


def build_epub(md_file, out_file, title, author, workdir, cover=None, date=None, toc_depth=3):
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = ["pandoc", os.path.basename(md_file), "-o", out_file,
           "--metadata", f"title={title}", "--metadata", f"author={author}",
           "--toc", f"--toc-depth={toc_depth}",
           f"--css={here}/eink.css"]
    if cover:
        cmd += [f"--epub-cover-image={cover}"]
    if date:
        cmd += ["--metadata", f"date={date}"]
    # картинки в md указаны относительно workdir — pandoc резолвит от cwd
    if subprocess.run(cmd, cwd=workdir).returncode != 0:
        return False
    return True


# ---------------------------------------------------------------- выпуск

def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("title", "Журнал")
    cfg.setdefault("out", "~/Books/feedzine")
    cfg.setdefault("preset", "reader")
    cfg.setdefault("workdir", "~/.cache/feedzine")
    cfg.setdefault("full_text", "auto")  # auto | true | false
    cfg.setdefault("max_per_feed", 10)
    cfg.setdefault("text_only", False)
    cfg.setdefault("feed", [])
    return cfg


def collect_items(cfg, args):
    """Новые статьи всех фидов -> (свежие с капом, все guid'ы фидов, state).

    Помечаются ВСЕ статьи фида (не только попавшие в выпуск): бэклог
    не выдавливается выпусками — новый выпуск собирается из genuinely
    нового. Переполнение сверх max молча роняется как прочитанное.
    """
    st = load_state(cfg["workdir"])
    fresh, all_guids = [], set()
    for fd in cfg["feed"]:
        url = fd["url"]
        try:
            ftitle, items = parse_feed(http_get(url))
        except Exception as e:  # noqa: BLE001
            log(f"  ! фид {fd.get('name', url)} не прочитался: {e}")
            continue
        if not fd.get("name"):
            # имя фида по умолчанию — его собственный title
            fd["name"] = ftitle or re.sub(r"^https?://(?:www\.)?", "", url)[:40]
        for it in items:
            if it["guid"]:
                all_guids.add(it["guid"])
        cap = fd.get("max", cfg["max_per_feed"])
        # свежие = невидимые, новые сверху (беру хвост — самые новые в RSS идут первыми)
        new = [it for it in items if it["guid"] and it["guid"] not in st["seen"]]
        log(f"  {fd['name']}: {len(items)} в фиде, новых {len(new)} (в выпуск {min(cap, len(new))})")
        for it in new[:cap]:
            it["feed"] = fd
            fresh.append(it)
    fresh.sort(key=lambda i: i["date"] or datetime.datetime.min)
    return fresh, all_guids, st


def _section(fd):
    """Секция выпуска: явная или имя фида."""
    return fd.get("section") or fd.get("name") or "RSS"


def article_md(item, cfg, imgdir, args):
    """Статья -> markdown: шапка с метаданными + сводка/полный текст."""
    fd = item["feed"]
    ft = fd.get("full_text", cfg["full_text"])
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
    if not args.text_only:
        base = None
        if item["link"]:
            m = re.match(r"https?://[^/]+", item["link"])
            if m:
                base = m.group(0)
        html, _ = rewrite_images(html, imgdir, args, rel=f"img/{args.preset}", base=base)
    else:
        html = re.sub(r"<img\b[^>]*>", "", html)
    body = html_to_md(html) or "_(пустая сводка)_"
    # внутренние заголовки статьи демо́тимся: не должны конкурировать
    # с секциями (##) и заголовками статей (###) в навигации EPUB
    body = re.sub(r"^(#{1,4})\s", r"##\1 ", body, flags=re.M)
    d = item["date"].strftime("%d.%m.%Y") if item["date"] else ""
    src = fd.get("name") or "RSS"
    head = f"*{author or src}"
    if d:
        head += f" · {d}"
    head += f" · [оригинал]({item['link']})*" if item["link"] else "*"
    return f"### {item['title']}\n\n{head}\n\n{body}\n"


def cmd_issue(args):
    cfg = load_config(os.path.expanduser(args.config))
    cfg["workdir"] = os.path.expanduser(cfg["workdir"])
    cfg["out"] = os.path.expanduser(cfg["out"])
    os.makedirs(cfg["workdir"], exist_ok=True)
    os.makedirs(cfg["out"], exist_ok=True)
    imgdir = f"{cfg['workdir']}/img/{args.preset}"

    log("читаю фиды…")
    fresh, all_guids, st = collect_items(cfg, args)
    if not fresh:
        log("нового нет — выпуск не собираю")
        for g in all_guids:  # но фид помечаем просмотренным
            st["seen"].setdefault(g, date_now())
        save_state(cfg["workdir"], st)
        return 0

    st["issue"] += 1
    n, now = st["issue"], datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    log(f"собираю выпуск №{n}: {len(fresh)} статей…")

    md = [f"# {cfg['title']} №{n}\n\n*{now.strftime('%d %B %Y')}*\n"]
    by_feed = {}
    for it in fresh:
        by_feed.setdefault(_section(it["feed"]), []).append(it)
    first_img = None
    for fname, items in by_feed.items():
        md.append(f"\n## {fname}\n")
        for it in items:
            m = article_md(it, cfg, imgdir, args)
            md.append(m + "\n")
            if first_img is None and not args.text_only:
                m2 = re.search(r"!\[[^\]]*\]\(img/[^/]+/([^)]+)\)", m)
                if m2:
                    first_img = f"{imgdir}/{m2.group(1)}"
    md_file = f"{cfg['workdir']}/issue_{n}.md"
    with open(md_file, "w") as f:
        f.write("\n".join(md))

    out_file = f"{cfg['out']}/{sanitize(cfg['title'])}_{n:03d}_{date_str}.epub"
    if build_epub(md_file, out_file, f"{cfg['title']} №{n}",
                  "feedzine", cfg["workdir"], cover=first_img, date=date_str):
        log(f"готово: {out_file}  ({os.path.getsize(out_file) / 1e6:.1f} МБ)")
    # весь фид прочитан: и попавшее в выпуск, и отсеянное капом, и старое
    for g in all_guids:
        st["seen"][g] = date_str
    save_state(cfg["workdir"], st)
    return 0


def date_now():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def cmd_list(args):
    cfg = load_config(os.path.expanduser(args.config))
    cfg["workdir"] = os.path.expanduser(cfg["workdir"])
    st = load_state(cfg["workdir"])
    log(f"выпусков собрано: {st['issue']}, статей отмечено прочитанными: {len(st['seen'])}")
    for fd in cfg["feed"]:
        try:
            name, items = parse_feed(http_get(fd["url"]))
            new = sum(1 for i in items if i["guid"] and i["guid"] not in st["seen"])
            print(f"  {fd.get('name') or name or fd['url']}: {len(items)} в фиде, новых {new}")
        except Exception as e:  # noqa: BLE001
            print(f"  {fd['url']}: недоступен ({e})")
    return 0


EXAMPLE_CFG = '''# feedzine — пример конфига (~/.config/feedzine/config.toml)
title = "Мой журнал"
out = "~/Books/feedzine"          # куда класть выпуски
preset = "reader"                # reader | eink | mini | tiny | hq
full_text = "auto"               # auto: полный текст для Habr, сводки для остальных
max_per_feed = 10                # статей на фид в выпуске

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
'''


def cmd_init(args):
    path = os.path.expanduser(args.config)
    if os.path.exists(path):
        log(f"уже есть: {path}")
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(EXAMPLE_CFG)
    log(f"написал пример конфига: {path}")
    return 0


def main():
    p = argparse.ArgumentParser(prog="feedzine", description="RSS/Atom фиды → «журналы» EPUB")
    p.add_argument("-c", "--config", default="~/.config/feedzine/config.toml", help="конфиг (default ~/.config/feedzine/config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("issue", help="собрать выпуск из нового в фидах")
    i.add_argument("--preset", default="reader", choices=list(PRESETS))
    i.add_argument("--text-only", action="store_true", help="без картинок")

    sub.add_parser("list", help="фиды и состояние")
    sub.add_parser("init", help="написать пример конфига")

    args = p.parse_args()
    args.config = os.path.expanduser(args.config)
    if not os.path.exists(args.config) and args.cmd != "init":
        print(f"нет конфига {args.config} — feedzine init его создаст")
        return 2
    return {"issue": cmd_issue, "list": cmd_list, "init": cmd_init}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
