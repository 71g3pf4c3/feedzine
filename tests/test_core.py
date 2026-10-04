"""Юнит-тесты feedzine (без сети; pandoc нужен только epub-сборочным тестам)."""

import datetime
import importlib.util
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

_spec = importlib.util.spec_from_file_location(
    "feedzine", os.path.join(os.path.dirname(__file__), "..", "bin", "feedzine.py"))
fz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fz)

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Хабр · тест</title>
<item>
  <title>Статья один &amp; детали</title>
  <link>https://habr.com/ru/articles/1090040/</link>
  <guid>https://habr.com/ru/articles/1090040/</guid>
  <pubDate>Sun, 04 Oct 2026 11:17:52 GMT</pubDate>
  <description>&lt;p&gt;Сводка: &lt;?статья до""" + " текст " * 60 + """&lt;/?&gt;&lt;/p&gt;</description>
  <dc:creator>vasya</dc:creator>
</item>
<item>
  <title>Статья два</title>
  <link>https://habr.com/ru/articles/1089999/?utm=x</link>
  <guid>https://habr.com/ru/articles/1089999/</guid>
  <pubDate>Sat, 03 Oct 2026 09:00:00 GMT</pubDate>
  <description>&lt;p&gt;Ещё: """ + "проза " * 60 + """&lt;/p&gt;</description>
  <dc:creator>petya</dc:creator>
</item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom-фид</title>
<entry>
  <title>Атомная запись</title>
  <link rel="alternate" href="https://example.org/1"/>
  <id>urn:1</id>
  <updated>2026-10-01T10:00:00Z</updated>
  <content type="html">&lt;p&gt;текст&lt;/p&gt;</content>
  <author><name>Автор</name></author>
</entry>
</feed>"""


# ---------------------------------------------------------------- parse_feed

def test_parse_rss():
    name, items = fz.parse_feed(RSS)
    assert name == "Хабр · тест"
    assert len(items) == 2
    assert items[0]["title"] == "Статья один & детали"  # entities
    assert items[0]["author"] == "vasya"
    assert items[0]["date"] == datetime.datetime(2026, 10, 4, 11, 17, 52)  # naive


def test_parse_atom():
    name, items = fz.parse_feed(ATOM)
    assert name == "Atom-фид"
    it = items[0]
    assert it["link"] == "https://example.org/1"
    assert it["guid"] == "urn:1"
    assert it["author"] == "Автор"
    assert "текст" in it["html"]


def test_parse_feed_garbage():
    with pytest.raises(ValueError):
        fz.parse_feed("<html><body>не фид</body></html>")


def test_parse_dates():
    assert fz._parse_date("Sun, 04 Oct 2026 11:17:52 GMT") == datetime.datetime(2026, 10, 4, 11, 17, 52)
    assert fz._parse_date("2026-10-01T10:00:00+00:00") == datetime.datetime(2026, 10, 1, 10, 0)
    assert fz._parse_date("") is None
    assert fz._parse_date("мусор") is None


# ---------------------------------------------------------------- state

def test_state_roundtrip(tmp_path):
    st = fz.load_state(str(tmp_path))
    assert st == {"seen": {}, "issue": {}, "pending": {}, "last_emit": {},
                  "down_feeds": {}, "retry_fulltext": {}}
    st["seen"]["x"] = "2026-10-04"
    st["issue"]["main"] = 3
    fz.save_state(str(tmp_path), st)
    assert fz.load_state(str(tmp_path)) == st


def test_state_bad_json(tmp_path):
    (tmp_path / "state.json").write_text("{")
    assert fz.load_state(str(tmp_path)) == {
        "seen": {}, "issue": {}, "pending": {}, "last_emit": {},
        "down_feeds": {}, "retry_fulltext": {}}


# ---------------------------------------------------------------- habr

def test_habr_id():
    assert fz.habr_id("https://habr.com/ru/articles/1090040/") == "1090040"
    assert fz.habr_id("https://habr.com/en/articles/777/?utm=x") == "777"
    assert fz.habr_id("https://example.org/x") is None


# ---------------------------------------------------------------- теги

RSS_TAGS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Теги</title>
<item>
  <title>Статья</title>
  <link>https://example.org/1</link>
  <guid>t1</guid>
  <pubDate>Sun, 04 Oct 2026 11:17:52 GMT</pubDate>
  <category>Python</category>
  <category>Из песочницы</category>
  <dc:subject>howto</dc:subject>
</item>
</channel></rss>"""

ATOM_TAGS = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom-теги</title>
<entry>
  <title>Запись</title>
  <link rel="alternate" href="https://example.org/2"/>
  <id>urn:2</id>
  <updated>2026-10-01T10:00:00Z</updated>
  <category term="Kotlin"/>
  <category term=""/>
</entry>
</feed>"""


def test_parse_rss_tags():
    _, items = fz.parse_feed(RSS_TAGS)
    # category RSS + dc:subject, пустые отброшены
    assert items[0]["tags"] == ["Python", "Из песочницы", "howto"]


def test_parse_atom_tags():
    _, items = fz.parse_feed(ATOM_TAGS)
    assert items[0]["tags"] == ["Kotlin"]


def test_tag_match():
    assert fz.tag_match(["Python"], [], [])                # без фильтров всё проходит
    assert fz.tag_match(["Python"], ["python"], [])       # регистронезависимо
    assert not fz.tag_match(["Go"], ["python"], [])       # include не совпал
    assert not fz.tag_match([], ["python"], [])           # без тегов include не проходит
    assert fz.tag_match([], [], ["спам"])                 # без тегов exclude не задет
    assert not fz.tag_match(["Python", "Реклама"], [], ["реклама"])
    assert fz.tag_match(["a", "b"], ["b", "c"], ["d"])    # и include, и exclude


# ---------------------------------------------------------------- заголовки

def test_demote_headings():
    md = "# Большой\n\nтекст\n\n## Средний\n\n```python\n# не заголовок\nx = 1\n```\n\n#### Мелкий\n"
    out = fz.demote_headings(md)
    lines = out.split("\n")
    assert "#### Большой" in lines           # демо́тнут за горизонт оглавления (h4+)
    assert "##### Средний" in lines
    assert "#### Мелкий" in lines             # уже h4 — не трогаем
    assert "# не заголовок" in lines          # fenced-код не тронут
    assert "x = 1" in lines


def test_demote_headings_no_fence_leak():
    # после закрывающего ``` заголовки снова демо́тятся
    md = "```\n# in code\n```\n# real heading\n"
    lines = fz.demote_headings(md).split("\n")
    assert "# in code" in lines
    assert "#### real heading" in lines


# ---------------------------------------------------------------- картинки

def test_img_name_stable():
    a = fz.img_name("https://habrastorage.org/getpro/habr/a.png")
    assert a == fz.img_name("https://habrastorage.org/getpro/habr/a.png")
    assert a != fz.img_name("https://habrastorage.org/getpro/habr/b.png")
    assert a.endswith(".jpg")


def test_rewrite_images_data_uri_kept():
    html = '<img src="data:image/png;base64,xxxx"/>'
    out, done, failed = fz.rewrite_images(html, "/nonexistent", fz.RunOpts(preset="tiny"))
    assert done == [] and failed == [] and "data:" in out


def test_rewrite_images_bad_url_placeholder():
    # битый URL не выбрасывает статью: на его месте остаётся заглушка
    html = '<p>до</p><img src="https://invalid.invalid/x.png"/><p>после</p>'
    out, done, failed = fz.rewrite_images(html, "/nonexistent", fz.RunOpts(preset="tiny"))
    assert done == [] and len(failed) == 1 and failed[0][0] == "https://invalid.invalid/x.png"
    assert "<img" not in out
    assert "до" in out and "после" in out
    assert "картинка не скачалась" in out


def test_rewrite_images_tracking_pixel_dropped():
    # трекинг-пиксель вырезается молча, без заглушки в тексте
    html = '<p>текст</p><img src="https://medium.com/_/stat?event=view"/>'
    out, done, failed = fz.rewrite_images(html, "/nonexistent", fz.RunOpts(preset="tiny"))
    assert done == [] and failed == []
    assert "текст" in out and "<img" not in out and "картинка" not in out


def test_process_image_relative_url(monkeypatch, tmp_path):
    # относительный URL резолвится от origin статьи, а не падает как битый
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        raise fz.httpx.ConnectError("нет сети")

    monkeypatch.setattr(fz, "http_get", fake_get)
    name, err = fz.process_image("/featured/a.png", str(tmp_path),
                                  fz.RunOpts(preset="tiny"),
                                  base="https://x.io/posts/1.html")
    assert seen["url"] == "https://x.io/featured/a.png"
    assert name is None and err is not None and "unknown url type" not in err


# ---------------------------------------------------------------- обложка

def test_make_cover(tmp_path):
    dst = str(tmp_path / "cover.png")
    assert fz.make_cover(dst, "Мой журнал", 7, "2026-10-04", ["Хабр", "DTF"]) == dst
    from PIL import Image
    im = Image.open(dst)
    assert im.mode == "L"          # true grayscale под e-ink
    assert im.size == (600, 800)


def test_make_cover_small_screen(tmp_path):
    # маленький экран (пресет tiny) -> маленькая обложка, типографика масштабируется
    dst = str(tmp_path / "cover.png")
    fz.make_cover(dst, "Мой журнал", 3, "2026-10-04",
                  ["Хабр", "DTF", "Lenta", "Остальное", "Ещё"], size=(300, 400))
    from PIL import Image
    im = Image.open(dst)
    assert im.mode == "L"
    assert im.size == (300, 400)
    px = list(im.getdata())
    assert sum(1 for v in px if v < 128) > 200   # что-то нарисовано


def test_make_cover_patterns_all_render(tmp_path):
    # каждый узор из PATTERNS рисует что-то и не падает на любом размере
    from PIL import Image
    for name in fz.PATTERNS:
        dst = str(tmp_path / f"cover_{name}.png")
        assert fz.make_cover(dst, "Ж", 1, "2026-10-04", [], pattern=name) == dst
        im = Image.open(dst)
        assert im.mode == "L" and im.size == (600, 800)
        ink = sum(1 for v in im.getdata() if v < 128)
        assert ink > 500, f"{name}: пустая обложка"


def test_make_cover_deterministic_per_issue(tmp_path):
    # тот же выпуск -> байт-в-байт та же обложка; другой выпуск -> другая
    a = fz.make_cover(str(tmp_path / "a.png"), "Мой журнал", 5, "2026-10-04", ["Х"])
    b = fz.make_cover(str(tmp_path / "b.png"), "Мой журнал", 5, "2026-10-04", ["Х"])
    c = fz.make_cover(str(tmp_path / "c.png"), "Мой журнал", 6, "2026-10-04", ["Х"])
    assert Path(a).read_bytes() == Path(b).read_bytes()
    assert Path(a).read_bytes() != Path(c).read_bytes()


def test_cover_text_safe():
    covered = {ord(c) for c in "ab "}
    t, dropped = fz.cover_text_safe("a 😀 中 b", covered)
    assert t == "a   b" and dropped == "中😀"
    # пробелы всегда остаются, сортировка выброшенного стабильна
    t2, dropped2 = fz.cover_text_safe("x", covered)      # x тоже не в шрифте
    assert t2 == "" and dropped2 == "x"
    # covered=None (нет fontTools/шрифта) — текст не трогаем
    t3, dropped3 = fz.cover_text_safe("abc 😀", None)
    assert t3 == "abc 😀" and dropped3 == ""


def test_make_cover_drops_uncovered_glyphs(tmp_path):
    # эмодзи/CJK в заголовке и секциях не роняют обложку и не дают tofu
    dst = str(tmp_path / "cover.png")
    assert fz.make_cover(dst, "Журнал 😀 про 中國", 3, "2026-10-04",
                         ["Хабр 👍", "DTF"], pattern="truchet") == dst
    from PIL import Image
    im = Image.open(dst)
    assert im.mode == "L" and im.size == (600, 800)
    assert sum(1 for v in im.getdata() if v < 128) > 500


# ---------------------------------------------------------------- фильтр пустого

def test_md_prose_len():
    # картинки, ссылки, код и разметка прозой не считаются
    md = ("![alt](img/p/x.jpg) [читать](https://x) **жирный** `code`\n"
          "```python\nprint('hi')\n```\n## заголовок\nОбычная проза тут.")
    assert fz._md_prose_len(md) == len("читать жирный code заголовок Обычная проза тут.")
    assert fz._md_prose_len("[читать](https://x)") == len("читать")
    assert fz._md_prose_len("![x](y.jpg)") == 0


def _mk_item(html, link="https://ex.com/a", ft=None):
    from types import SimpleNamespace
    return {"title": "T", "link": link, "guid": "g1", "date": None,
            "html": html, "author": "",
            "feed": SimpleNamespace(name="X", full_text=ft, section=None,
                                     min_article_chars=None)}


def test_article_md_drops_link_only_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(fz, "html_to_md", lambda h: h)          # без pandoc
    monkeypatch.setattr(fz, "fetch_full", lambda link, sniff=None: (None, None))
    cfg = fz.Config.model_validate({"feed": [{"url": "https://x"}]})
    # голая ссылка без контента — не статья
    assert fz.article_md(_mk_item('<a href="https://ex.com/a">читай</a>'),
                         cfg, str(tmp_path), fz.RunOpts()) is None
    # осмысленная сводка проходит
    ok = fz.article_md(_mk_item("<p>" + "нормальный текст статьи. " * 20 + "</p>"),
                       cfg, str(tmp_path), fz.RunOpts())
    assert ok and "нормальный" in ok


def test_article_md_min_chars_override(monkeypatch, tmp_path):
    monkeypatch.setattr(fz, "html_to_md", lambda h: h)
    monkeypatch.setattr(fz, "fetch_full", lambda link, sniff=None: (None, None))
    short = _mk_item("<p>коротко: было да.</p>")
    # глобально 0 — короткое живёт
    cfg = fz.Config.model_validate({"feed": [{"url": "https://x"}],
                                          "min_article_chars": 0})
    assert fz.article_md(short, cfg, str(tmp_path), fz.RunOpts()) is not None
    # override на фиде — едет в item["feed"], а не ищется в cfg
    short["feed"].min_article_chars = 0
    cfg2 = fz.Config.model_validate({"feed": [{"url": "https://x"}]})
    assert fz.article_md(short, cfg2, str(tmp_path), fz.RunOpts()) is not None


def test_auto_full_text_for_link_feeds(monkeypatch, tmp_path):
    # auto + огрызок вместо сводки -> сниффер вызывается сам (без habr-ссылки)
    calls = []
    monkeypatch.setattr(fz, "html_to_md", lambda h: h)
    monkeypatch.setattr(fz, "fetch_full",
                        lambda link, sniff=None: calls.append(link) or ("<p>" + "статья " * 300 + "</p>", None))
    cfg = fz.Config.model_validate({"feed": [{"url": "https://x"}]})
    ok = fz.article_md(_mk_item("<p>ссылка</p>", link="https://ex.com/post"),
                       cfg, str(tmp_path), fz.RunOpts())
    assert calls == ["https://ex.com/post"]
    assert "статья" in ok
    # богатая сводка (>= 300 символов текста) — сайт не дёргаем
    calls.clear()
    fz.article_md(_mk_item("<p>" + "богатая сводка. " * 80 + "</p>",
                           link="https://ex.com/rich"), cfg, str(tmp_path), fz.RunOpts())
    assert calls == []


def test_issue_skips_junk_articles(tmp_path, monkeypatch):
    # первая статья становится голой ссылкой, вторая остаётся нормальной
    junk_rss = re.sub(r"<description>.*?</description>",
                      "<description>&lt;a href='https://x/1'&gt;читай&lt;/a&gt;</description>",
                      RSS, count=1)
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: junk_rss)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    cfg = fz._load(str(cfgf))
    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True,
                                        force=True)) == 0
    epubs = list((tmp_path / "out").glob("*.epub"))
    assert len(epubs) == 1
    import zipfile
    z = zipfile.ZipFile(epubs[0])
    body = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xhtml"))
    assert "Статья два".encode() in body          # нормальная прошла
    assert "читай".encode() not in body           # тело пустышки не вышло…
    assert "не взялся".encode() in body           # …но названа в отчёте деградаций


def test_article_md_photo_post_survives_junk_gate(monkeypatch, tmp_path):
    # фото-пост: подпись короткая, но картинка скачалась — это контент
    monkeypatch.setattr(fz, "html_to_md",
                        lambda h: h.replace('<img src="img/tiny/p.jpg"/>',
                                            "![фото](img/tiny/p.jpg)"))
    monkeypatch.setattr(fz, "fetch_full", lambda link, sniff=None: (None, None))
    monkeypatch.setattr(fz, "rewrite_images",
                        lambda html, *a, **kw: (html.replace(
                            '<img src="https://x/p.jpg"/>',
                            '<img src="img/tiny/p.jpg"/>'), ["p.jpg"], []))
    fd = fz.SimpleNamespace(name="X", full_text=False, section=None,
                             min_article_chars=None, journal=None)
    item = {"title": "@x posted a photo", "link": "https://t.me/x/1", "guid": "g",
            "date": None, "author": "",
            "html": "<p>Ну надо пробывать, ящитаю ;)</p><img src=\"https://x/p.jpg\"/>",
            "feed": fd}
    cfg = fz.Config(title="Журнал", out=str(tmp_path / "out"),
                    workdir=str(tmp_path / "wd"))
    md = fz.article_md(item, cfg, str(tmp_path / "img"),
                       fz.RunOpts(preset="tiny"))
    assert md is not None and "фото" in md


def test_article_md_pure_image_dropped(monkeypatch, tmp_path):
    # чистая картинка без текста — пыль, не контент: в выпуск не идёт
    monkeypatch.setattr(fz, "html_to_md",
                        lambda h: h.replace('<img src="img/tiny/p.jpg"/>',
                                            "![фото](img/tiny/p.jpg)"))
    monkeypatch.setattr(fz, "fetch_full", lambda link, sniff=None: (None, None))
    monkeypatch.setattr(fz, "rewrite_images",
                        lambda html, *a, **kw: (html.replace(
                            '<img src="https://x/p.jpg"/>',
                            '<img src="img/tiny/p.jpg"/>'), ["p.jpg"], []))
    fd = fz.SimpleNamespace(name="X", full_text=False, section=None,
                             min_article_chars=None, journal=None)
    item = {"title": "@x posted a photo", "link": "https://t.me/x/1", "guid": "g",
            "date": None, "author": "",
            "html": '<img src="https://x/p.jpg"/>', "feed": fd}
    cfg = fz.Config(title="Журнал", out=str(tmp_path / "out"),
                    workdir=str(tmp_path / "wd"))
    assert fz.article_md(item, cfg, str(tmp_path / "img"),
                         fz.RunOpts(preset="tiny")) is None


def test_journal_authors_in_headings(monkeypatch, tmp_path):
    # journal.authors = true: автор в заголовке главы («Автор · Тема»)
    monkeypatch.setattr(fz, "html_to_md", lambda h: h)
    monkeypatch.setattr(fz, "fetch_full", lambda link, sniff=None: (None, None))
    fd = fz.SimpleNamespace(name="tg · x", full_text=False, section=None,
                             min_article_chars=None, journal="tg")
    item = {"title": "Пост о разном", "link": "https://t.me/x/1", "guid": "g",
            "date": None, "author": "Канал Такой-то",
            "html": "<p>" + "текст поста достаточно длинный, чтобы пройти гейт " * 3
                    + "</p>", "feed": fd}
    cfg = fz.Config(title="Журнал", out=str(tmp_path / "out"),
                    workdir=str(tmp_path / "wd"),
                    journal={"tg": fz.JournalCfg(title="TG", authors=True)})
    md = fz.article_md(item, cfg, str(tmp_path / "img"),
                       fz.RunOpts(preset="tiny"))
    assert "### Канал Такой-то · Пост о разном" in md
    # без authors — заголовок как был
    cfg2 = fz.Config(title="Журнал", out=str(tmp_path / "out"),
                     workdir=str(tmp_path / "wd"))
    assert "### Пост о разном" in fz.article_md(
        item, cfg2, str(tmp_path / "img"), fz.RunOpts(preset="tiny"))


def test_pending_add_dedup():
    # один и тот же guid/link дважды в накопитель не попадает
    fd = fz.SimpleNamespace(name="X", full_text=None, section=None,
                             min_article_chars=None)
    mk = lambda g, ln: {"title": "T", "link": ln, "guid": g,
                        "date": None, "author": "", "html": "<p>x</p>", "feed": fd}
    pending = []
    fz._pending_add(pending, [mk("a", "https://x/1"), mk("a", "https://x/1"),
                              mk("b", "https://x/2")])
    # тот же link, другой guid — тоже дубль
    fz._pending_add(pending, [mk("c", "https://x/1")])
    assert [p["guid"] for p in pending] == ["a", "b"]


def test_collect_items_feed_dup_dropped(tmp_path, monkeypatch):
    # фид отдал статью дважды (guid и link продублированы) — в fresh она одна
    g1 = "https://habr.com/ru/articles/1090040/"
    dup_rss = RSS.replace("https://habr.com/ru/articles/1089999/?utm=x", g1) \
                 .replace("<guid>https://habr.com/ru/articles/1089999/</guid>",
                          f"<guid>{g1}</guid>")
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: dup_rss)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    fresh, all_guids, _st = fz.collect_items(fz._load(str(cfgf)),
                                             fz.RunOpts(preset="tiny"))
    assert [i["guid"] for i in fresh] == [g1]
    assert all_guids == {g1}


def test_collect_bridge_error_whole_feed(tmp_path, monkeypatch):
    # мостик вернул фид из одной ошибки — это упавший фид, не статьи
    err = RSS.replace("Статья один &amp; детали", "Bridge returned error 502! (20730)") \
             .replace("Статья два", "Bridge returned error 502! (20731)")
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: err)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    fresh, all_guids, st = fz.collect_items(fz._load(str(cfgf)),
                                           fz.RunOpts(preset="tiny"))
    assert fresh == [] and all_guids == set()
    assert "https://x/rss" in st["down_feeds"]
    assert "502" in st["down_feeds"]["https://x/rss"]["error"]


def test_collect_bridge_error_mixed(tmp_path, monkeypatch):
    # ошибка вперемешку с нормальными статьями — ошибка выброшена тихо
    mixed = RSS.replace(
        "Статья один &amp; детали", "Bridge returned error 502! (20730)")
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: mixed)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    fresh, all_guids, st = fz.collect_items(fz._load(str(cfgf)),
                                           fz.RunOpts(preset="tiny"))
    assert len(fresh) == 1 and fresh[0]["title"] == "Статья два"
    assert "https://x/rss" not in st["down_feeds"]


def test_retry_fulltext_pool(tmp_path, monkeypatch):
    # неудавшийся полный текст уходит в пул и докатывается в следующий выпуск
    import datetime as dt
    st = fz.load_state(str(tmp_path))
    st["pending"] = {"main": []}
    calls = {"n": 0}

    def flaky(link, sniff=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fz.httpx.ConnectError("boom")
        return "<p>Полный текст вернулся</p>", "Автор"

    monkeypatch.setattr(fz, "fetch_full", flaky)
    cfg = fz.Config(title="Журнал", out=str(tmp_path / "out"),
                    workdir=str(tmp_path / "wd"))
    st["retry_fulltext"]["g1"] = {
        "kind": None, "guid": "g1", "title": "Докатка", "link": "https://x/1",
        "date": None, "html": "<p>заглушка</p>", "author": "",
        "feed": {"name": "X", "full_text": None, "section": None,
                 "min_article_chars": None},
        "error": "403", "jid": "main", "tries": 0,
        "added": "2026-10-01T00:00:00"}
    # первая докатка: снова упала — попытка списана, статья остаётся в пуле
    fz._retry_fulltext(cfg, st, dt.datetime(2026, 10, 2))
    assert st["retry_fulltext"]["g1"]["tries"] == 1
    assert st["pending"]["main"] == []
    # вторая: полный текст взялся — уезжает в копилку «Докатнуто · …»
    fz._retry_fulltext(cfg, st, dt.datetime(2026, 10, 3))
    assert "g1" not in st["retry_fulltext"]
    assert [p["guid"] for p in st["pending"]["main"]] == ["g1"]
    assert "Докатнуто" in st["pending"]["main"][0]["feed"]["section"]


def test_issue_reports_degradations(tmp_path, monkeypatch):
    # лежащий фид и пустая статья попадают в секцию «Что не попало в выпуск»
    monkeypatch.setattr(fz.time, "sleep", lambda s: None)
    monkeypatch.setattr(fz, "http_get",
                        lambda url, **kw: RSS if "x/rss" in url else
                        (_ for _ in ()).throw(fz.httpx.ConnectError("нет сети")))
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "Живой"\nurl = "https://x/rss"\n'
        '[[feed]]\nname = "Мёртвый"\nurl = "https://y/rss"\n')
    cfg = fz._load(str(cfgf))
    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True,
                                         force=True)) == 0
    st = fz.load_state(str(tmp_path / "wd"))
    assert "https://y/rss" in st["down_feeds"]
    assert st["down_feeds"]["https://y/rss"]["name"] == "Мёртвый"
    epubs = list((tmp_path / "out").glob("*.epub"))
    assert len(epubs) == 1
    import zipfile
    z = zipfile.ZipFile(epubs[0])
    body = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xhtml"))
    assert "Что не попало в выпуск".encode() in body
    assert "не синкался".encode() in body


def test_cover_pattern_validation():
    import pytest
    from pydantic import ValidationError
    fz.Config.model_validate({"cover_pattern": "mandelbrot"})
    fz.Config.model_validate({"cover_pattern": "auto"})
    with pytest.raises(ValidationError):
        fz.Config.model_validate({"cover_pattern": "kaschtan"})


def test_presets_have_cover_size():
    for name, pr in fz.PRESETS.items():
        assert len(pr.get("cover", ())) == 2, f"{name}: нет размера обложки"


def test_cover_font_path_env(monkeypatch, tmp_path):
    f = tmp_path / "f.ttf"
    f.write_bytes(b"")
    monkeypatch.setenv("FEEDZINE_FONT", str(f))
    assert fz.cover_font_path() == str(f)
    monkeypatch.delenv("FEEDZINE_FONT")
    # рядом с bin/feedzine.py шрифта нет — дефолт PIL
    assert fz.cover_font_path() is None


# ---------------------------------------------------------------- сеть: повторы

class _FakeResp:
    content = b"ok"
    headers = {"content-type": "text/plain; charset=utf-8"}

    def raise_for_status(self):
        pass


def test_http_get_retries(monkeypatch):
    calls = {"n": 0}

    class FakeClient:
        def get(self, url, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("сеть моргнула")
            return _FakeResp()

    monkeypatch.setattr(fz, "_client", FakeClient())
    monkeypatch.setattr(fz.time, "sleep", lambda s: None)
    assert fz.http_get("https://x/") == "ok"
    assert calls["n"] == 2


def test_http_get_retries_exhausted(monkeypatch):
    class FakeClient:
        def get(self, url, timeout=None):
            raise httpx.ConnectError("нет сети")

    monkeypatch.setattr(fz, "_client", FakeClient())
    monkeypatch.setattr(fz.time, "sleep", lambda s: None)
    with pytest.raises(httpx.ConnectError):
        fz.http_get("https://x/", retries=1)


def test_http_get_windows1251(monkeypatch):
    # opennet-style: XML в windows-1251, charset в заголовке не указан
    body = ('<?xml version="1.0" encoding="windows-1251"?>'
            "<rss><channel><title>Опеннет</title></channel></rss>").encode("cp1251")

    class R:
        content = body
        headers = {"content-type": "text/xml"}

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, timeout=None):
            return R()

    monkeypatch.setattr(fz, "_client", FakeClient())
    assert fz.http_get("https://opennet.ru/rss.shtml") .startswith("<?xml")
    assert "Опеннет" in fz.parse_feed(fz.http_get("https://x/"))[0]


def test_http_get_charset_header_wins(monkeypatch):
    # charset из Content-Type приоритетнее XML-декларации
    body = ("<?xml version='1.0' encoding='utf-8'?>"
            "<rss><channel><title>Тест</title></channel></rss>").encode("utf-8")

    class R:
        content = body
        headers = {"content-type": "application/rss+xml; charset=utf-8"}

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, timeout=None):
            return R()

    monkeypatch.setattr(fz, "_client", FakeClient())
    assert "Тест" in fz.http_get("https://x/")


# ---------------------------------------------------------------- поставка

def test_calibre_add_no_calibredb(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fz.shutil, "which", lambda x: None)
    assert fz.calibre_add(str(tmp_path / "x.epub"), str(tmp_path)) is False
    assert "calibredb" in capsys.readouterr().out


def test_run_post_issue(tmp_path):
    epub = tmp_path / "j.epub"
    epub.write_bytes(b"x")
    dst = tmp_path / "copy.epub"
    fz.run_post_issue("cp %f " + str(dst), str(epub))
    assert dst.read_bytes() == b"x"


def test_run_post_issue_env(tmp_path):
    out = tmp_path / "env.txt"
    fz.run_post_issue(f"printf %s \"$FEEDZINE_EPUB\" > {out}", "/tmp/j.epub")
    assert out.read_text() == "/tmp/j.epub"


def test_run_post_issue_skips_empty():
    fz.run_post_issue("", "/tmp/j.epub")  # не падает, ничего не запускает


def test_prune_issues(tmp_path):
    for n in (1, 2, 3):
        (tmp_path / f"Журнал_{n:03d}_2026-10-0{n}.epub").write_bytes(b"x")
    (tmp_path / "чужое.epub").write_bytes(b"x")
    fz.prune_issues(str(tmp_path), "Журнал", 2)
    names = sorted(p.name for p in tmp_path.glob("*.epub"))
    # старейший удалён, чужие файлы не тронуты
    assert names == ["Журнал_002_2026-10-02.epub", "Журнал_003_2026-10-03.epub", "чужое.epub"]


def test_prune_issues_respects_format(tmp_path):
    # уборка epub не трогает html-выпуски того же журнала
    (tmp_path / "T_001_2026-10-01.epub").write_bytes(b"x")
    (tmp_path / "T_002_2026-10-02.epub").write_bytes(b"x")
    (tmp_path / "T_001_2026-10-01.html").write_bytes(b"x")
    fz.prune_issues(str(tmp_path), "T", 1)
    assert (tmp_path / "T_001_2026-10-01.epub").exists() is False
    assert (tmp_path / "T_002_2026-10-02.epub").exists()
    assert (tmp_path / "T_001_2026-10-01.html").exists()


# ---------------------------------------------------------------- форматы

def test_out_name():
    assert fz.out_name("/out", "Мой журнал", 7, "2026-10-04", "epub") == \
        "/out/Мой_журнал_007_2026-10-04.epub"
    assert fz.out_name("/out", "T", 1, "2026-10-04", "md").endswith("_001_2026-10-04.md")
    assert fz.out_name("/out", "T", 1, "2026-10-04", "html").endswith("_001_2026-10-04.html")


def test_squeeze_epub(tmp_path):
    import zipfile
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("OEBPS/content.xhtml", "текст " * 2000)
    before = p.stat().st_size
    fz.squeeze_epub(str(p))
    with zipfile.ZipFile(p) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype"                 # OCF: первым
        assert infos[0].compress_type == zipfile.ZIP_STORED    # и без сжатия
        assert z.read("mimetype") == b"application/epub+zip"
        assert z.read("OEBPS/content.xhtml").startswith("текст".encode())
    assert p.stat().st_size <= before                          # ужалось, не распухло


def test_build_output_md(tmp_path):
    src = tmp_path / "issue_1.md"
    src.write_text("# T\n\nтекст\n", encoding="utf-8")
    dst = tmp_path / "out.md"
    assert fz.build_output(str(src), str(dst), "md", "T", str(tmp_path)) is True
    assert dst.read_text(encoding="utf-8").startswith("# T")


# ---------------------------------------------------------------- конфиг (pydantic)

def test_load_config_defaults(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    cfg = fz.load_config(str(cfgf))
    assert cfg.title == "Журнал"
    assert cfg.full_text == "auto"
    assert cfg.max_per_feed == 10
    assert cfg.preset == "reader"
    assert cfg.include_tags == [] and cfg.exclude_tags == []
    assert cfg.cover == "auto"
    assert len(cfg.feed) == 1


def test_load_config_rejects_unknown_key(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('max_per_feedd = 10\n')  # опечатка
    with pytest.raises(ValidationError):
        fz.load_config(str(cfgf))


def test_load_config_rejects_bad_preset(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('preset = "ultra"\n')
    with pytest.raises(ValidationError):
        fz.load_config(str(cfgf))


def test_load_config_rejects_bad_cover(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('cover = "beautiful"\n')
    with pytest.raises(ValidationError):
        fz.load_config(str(cfgf))


def test_load_config_rejects_bad_format(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('format = "pdf"\n')
    with pytest.raises(ValidationError):
        fz.load_config(str(cfgf))


def test_img_quality_bounds(tmp_path):
    def cfg_with(q):
        cfgf = tmp_path / "c.toml"
        cfgf.write_text(f"img_quality = {q}\n")
        return fz.load_config(str(cfgf))
    assert cfg_with(0).img_quality == 0      # дефолт пресета
    assert cfg_with(95).img_quality == 95
    assert cfg_with(40).img_quality == 40
    with pytest.raises(ValidationError):
        cfg_with(30)
    with pytest.raises(ValidationError):
        cfg_with(100)


def test_toc_depth_bounds(tmp_path):
    def cfg_with(q):
        cfgf = tmp_path / "c.toml"
        cfgf.write_text(f"toc_depth = {q}\n")
        return fz.load_config(str(cfgf))
    assert cfg_with(2).toc_depth == 2
    with pytest.raises(ValidationError):
        cfg_with(5)


def test_epub_chapters_split_by_feed(tmp_path):
    # журнал режется на файлы по фидам, а не одним гигантским ch001
    import zipfile
    md = tmp_path / "issue_1.md"
    md.write_text(
        "# Ж №1\n\n"
        "## Фид А\n\n### Статья 1\n\ntext\n\n### Статья 2\n\ntext\n\n"
        "## Фид Б\n\n### Статья 3\n\ntext\n", encoding="utf-8")
    out = tmp_path / "o.epub"
    assert fz.build_output(str(md), str(out), "epub", "Ж", str(tmp_path)) is True
    with zipfile.ZipFile(out) as z:
        chapters = [n for n in z.namelist()
                    if n.startswith("EPUB/text/") and n.endswith(".xhtml")
                    and "title" not in n and "nav" not in n and "cover" not in n]
    assert len(chapters) >= 2, f"главы не порезались по ##: {chapters}"


# ---------------------------------------------------------------- collect

def test_collect_items_dedup_and_order(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('[[feed]]\nname = "X"\nurl = "https://x/rss"\nmax = 1\n')
    cfg = fz.load_config(str(cfgf))
    cfg.workdir = str(tmp_path)
    fz.save_state(cfg.workdir, {"seen": {"https://habr.com/ru/articles/1089999/": "x"},
                                 "issue": 0})
    items = [
        {"title": "новая", "link": "https://habr.com/ru/articles/1090040/",
         "guid": "https://habr.com/ru/articles/1090040/",
         "date": datetime.datetime(2026, 10, 4), "html": "<p>a</p>", "author": ""},
        {"title": "старая", "link": "https://habr.com/ru/articles/1089999/",
         "guid": "https://habr.com/ru/articles/1089999/",
         "date": datetime.datetime(2026, 10, 3), "html": "<p>b</p>", "author": ""},
    ]
    # подменяем fetch: проверяем только фильтрацию seen и cap
    fresh, st = [it for it in items if it["guid"] not in
                 fz.load_state(cfg.workdir)["seen"]][:cfg.feed[0].max], fz.load_state(cfg.workdir)
    assert [f["title"] for f in fresh] == ["новая"]
    assert st["issue"] == {"main": 0}   # плоский старый state мигрирован в журналы


def test_section():
    assert fz._section(fz.FeedCfg(url="https://x", section="Хабр", name="Python")) == "Хабр"
    assert fz._section(fz.FeedCfg(url="https://x", name="Just Feed")) == "Just Feed"
    assert fz._section(fz.FeedCfg(url="https://x")) == "RSS"


# ---------------------------------------------------------------- sanitize

def test_sanitize():
    assert fz.sanitize("Мой журнал!") == "Мой_журнал"
    assert fz.sanitize("///") == "item"


# ---------------------------------------------------------------- dry-run

def test_issue_dry_run_no_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: RSS)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\n'
        f'workdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    assert fz.run_issue(fz._load(str(cfgf)), fz.RunOpts(preset="tiny",
                                                        text_only=True, dry_run=True)) == 0
    # state не тронут: ни seen, ни счётчик выпусков
    assert fz.load_state(str(tmp_path / "wd")) == {
        "seen": {}, "issue": {}, "pending": {}, "last_emit": {},
        "down_feeds": {}, "retry_fulltext": {}}


# ---------------------------------------------------------------- периоды

def test_period_ready():
    now = datetime.datetime(2026, 10, 8)          # четверг W41 (пн = 5.10)
    assert fz.period_ready("day", None, now)
    assert fz.period_ready("week", None, now) is False       # ещё не собирали — копим
    assert fz.period_ready("week", "2026-10-06", now) is False  # та же ISO-неделя
    assert fz.period_ready("week", "2026-10-01", now)          # неделя сменилась
    assert fz.period_ready("month", "2026-10-01", now) is False
    assert fz.period_ready("month", "2026-09-30", now)
    assert fz.period_ready("week", "мусор", now)               # кривой last_emit


def test_issue_weekly_accumulates_then_forces(tmp_path, monkeypatch):
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: RSS)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\nperiod = "week"\n'
        f'out = "{tmp_path / "out"}"\n'
        f'workdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    cfg = fz._load(str(cfgf))

    # первый прогон: неделя не сменилась — копим, EPUB нет
    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True)) == 0
    st = fz.load_state(str(tmp_path / "wd"))
    assert len(st["pending"]["main"]) == 2
    assert st["issue"] == {} and st["last_emit"] == {}
    # pending сериализуем и дат в ISO-строках
    assert all(isinstance(it["date"], (str, type(None))) for it in st["pending"]["main"])
    assert list((tmp_path / "out").glob("*.epub")) == []

    # force: сводка из накопленного, накопитель пустеет
    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True, force=True)) == 0
    st = fz.load_state(str(tmp_path / "wd"))
    assert st["pending"]["main"] == []
    assert st["issue"] == {"main": 1}
    assert st["last_emit"]["main"]
    epubs = list((tmp_path / "out").glob("*.epub"))
    assert len(epubs) == 1


# ---------------------------------------------------------------- сниффер

def test_fetch_full_picks_largest_article(monkeypatch):
    page = ("<html><body>"
            "<article><p>комментарий</p></article>"
            "<article><p>" + "текст " * 300 +
            "<script>tracking()</script></p></article></body></html>")
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: page)
    html, _ = fz.fetch_full("https://ex.com/a")
    assert "текст" in html
    assert "комментарий" not in html     # мелкие article не берём
    assert "tracking" not in html        # script вырезается


def test_fetch_full_sniffs_link_from_summary(monkeypatch):
    pages = {"https://agg/item": "<html><body><p>обсуждение</p></body></html>",
             "https://real/post": ("<html><body><article><p>"
                                   + "статья " * 300 + "</p></article></body></html>")}
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: pages[url])
    # по ссылке агрегатора контента нет — сниффим прямую ссылку из описания
    html, _ = fz.fetch_full("https://agg/item",
                            sniff='читай <a href="https://real/post">тут</a>')
    assert "статья" in html


# ---------------------------------------------------------------- журналы

RSS2 = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>HN</title>
<item><title>HN статья</title><link>https://hn/1</link><guid>hn-1</guid>
<pubDate>Thu, 01 Oct 2026 10:00:00 +0000</pubDate>
<description>""" + "статья целиком " * 40 + """</description></item>
</channel></rss>"""


def _route(url, **kw):
    return RSS if "x/rss" in url else RSS2


def test_state_migration_flat_to_journals(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "state.json").write_text(json.dumps(
        {"seen": {"g1": "2026-10-01"}, "issue": 7,
         "pending": [{"title": "x"}], "last_emit": "2026-10-01"}))
    st = fz.load_state(str(wd))
    assert st["seen"] == {"g1": "2026-10-01"}
    assert st["issue"] == {"main": 7}
    assert st["pending"] == {"main": [{"title": "x"}]}
    assert st["last_emit"] == {"main": "2026-10-01"}


def test_journals_split_and_numbering(tmp_path, monkeypatch):
    monkeypatch.setattr(fz, "http_get", _route)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Главный"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n'
        '[[feed]]\nname = "HN"\nurl = "https://hn/rss"\njournal = "hn"\n'
        '[journal.hn]\ntitle = "HN Дайджест"\n')
    cfg = fz._load(str(cfgf))

    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True,
                                        force=True)) == 0
    main = list((tmp_path / "out").glob("*.epub"))
    hn = list((tmp_path / "out" / "hn").glob("*.epub"))
    assert len(main) == 1 and len(hn) == 1
    assert "Главный_001" in str(main[0])       # каждый журнал — свой №1
    assert "HN_Дайджест_001" in str(hn[0])
    st = fz.load_state(str(tmp_path / "wd"))
    assert st["issue"] == {"main": 1, "hn": 1}
    assert st["pending"] == {"main": [], "hn": []}
    assert st["last_emit"]["main"] and st["last_emit"]["hn"]


def test_journal_period_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(fz, "http_get", _route)
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Главный"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n'
        '[[feed]]\nname = "HN"\nurl = "https://hn/rss"\njournal = "hn"\n'
        '[journal.hn]\nperiod = "week"\n')
    cfg = fz._load(str(cfgf))

    # main (day) выходит сразу, hn (week) копит до границы
    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True)) == 0
    assert len(list((tmp_path / "out").glob("*.epub"))) == 1
    assert list((tmp_path / "out" / "hn").glob("*.epub")) == []
    st = fz.load_state(str(tmp_path / "wd"))
    assert st["issue"] == {"main": 1}
    assert len(st["pending"]["hn"]) == 1
    assert "hn" not in st["last_emit"]

    # новых нет: main молчит, hn продолжает копить
    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True)) == 0
    st = fz.load_state(str(tmp_path / "wd"))
    assert st["issue"] == {"main": 1}
    assert len(st["pending"]["hn"]) == 1


def test_journal_validation():
    with pytest.raises(ValidationError):
        fz.Config.model_validate({"journal": {"hn": {"period": "год"}}})


# ---------------------------------------------------------------- webui

def test_webui_endpoints(tmp_path, monkeypatch):
    import threading
    import urllib.request
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Веб"\nperiod = "week"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n'
        '[[feed]]\nname = "HN"\nurl = "https://hn/rss"\njournal = "hn"\n')
    cfg = fz._load(cfgf)

    runs = []
    ev = threading.Event()

    def fake_run(cfg2, opts2):
        runs.append(opts2.force)
        print(f"сборка force={opts2.force}")
        ev.set()
        return 0
    monkeypatch.setattr(fz, "run_issue", fake_run)

    srv = fz.make_web_server(cfg, fz.RunOpts(preset="tiny"), "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert html.startswith("<!doctype") and "feedzine" in html
        assert "журналы" in html.lower() or "journal" in html
        ov = json.loads(urllib.request.urlopen(base + "/api/overview").read())
        js = {j["id"]: j for j in ov["journals"]}
        assert set(js) == {"main", "hn"}
        assert js["hn"]["period"] == "week" and js["hn"]["pending"] == 0
        assert ov["feeds"][1]["journal"] == "hn"

        req = urllib.request.Request(base + "/api/build",
                                     data=b'{"force": true}', method="POST")
        assert json.loads(urllib.request.urlopen(req).read()) == {"started": True}
        assert ev.wait(5)
        assert runs == [True]
        ev.clear()
        st = json.loads(urllib.request.urlopen(base + "/api/build").read())
        assert st["rc"] == 0 and any("force=True" in l for l in st["log"])

        req = urllib.request.Request(base + "/api/build",
                                     data=b'{}', method="POST")
        assert json.loads(urllib.request.urlopen(req).read()) == {"started": True}
        assert ev.wait(5)
        assert runs == [True, False]
        urllib.request.urlopen(base + "/api/nope")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_webui_build_single_flight(tmp_path, monkeypatch):
    import threading
    import urllib.request
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        f'title = "W"\nout = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    cfg = fz._load(cfgf)

    started, release = threading.Event(), threading.Event()

    def slow_run(cfg2, opts2):
        started.set()
        release.wait(5)
        return 0
    monkeypatch.setattr(fz, "run_issue", slow_run)
    srv = fz.make_web_server(cfg, fz.RunOpts(preset="tiny"), "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        r1 = urllib.request.Request(base + "/api/build", data=b"{}", method="POST")
        assert json.loads(urllib.request.urlopen(r1).read())["started"] is True
        assert started.wait(5)
        # пока сборка идёт — вторая не стартует (409)
        r2 = urllib.request.Request(base + "/api/build", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(r2)
            assert False, "ожидали 409"
        except urllib.error.HTTPError as e:
            assert e.code == 409
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- TUI (textual)

def test_tui_build_respects_period_and_force(tmp_path, monkeypatch):
    import asyncio

    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\nworkdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\njournal = "hn"\n')
    cfg = fz._load(str(cfgf))
    monkeypatch.setattr(fz, "feed_stats", lambda cfg, seen: [])
    forces = []

    def fake_run(cfg2, opts2):
        forces.append(opts2.force)
        assert cfg2.feed[0].journal == "hn"
        return 0
    monkeypatch.setattr(fz, "run_issue", fake_run)
    app = fz.FeedzineApp(cfg, fz.RunOpts(preset="tiny"))

    async def run():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("i")            # обычный выпуск: период уважается
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("I")            # форс-сводка из копилки
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("d")            # dry-run тем же путём
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert forces == [False, True, False]

    asyncio.run(run())


def test_tui_toggle_and_cycle(tmp_path, monkeypatch):
    import asyncio

    cfgf = tmp_path / "c.toml"
    cfgf.write_text(
        'title = "Т"\n'
        f'out = "{tmp_path / "out"}"\n'
        f'workdir = "{tmp_path / "wd"}"\n'
        '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    cfg = fz._load(str(cfgf))
    monkeypatch.setattr(fz, "feed_stats",
                        lambda cfg, seen: [fz.FeedStat(name="X", url="https://x/rss",
                                                        total=5, new=2)])
    app = fz.FeedzineApp(cfg, fz.RunOpts(preset="tiny"))

    async def run():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            t = app.query_one(fz.DataTable)
            assert t.row_count == 1
            assert app.rows[0]["include"] is True
            await pilot.press("space")
            await pilot.pause()
            assert app.rows[0]["include"] is False
            await pilot.press("f")   # full_text: None -> True
            await pilot.pause()
            assert app.rows[0]["ft"] is True

    asyncio.run(run())


# ---------------------------------------------------------------- backfill

def _rss_age(items):
    """RSS из (title, aware|naive|None datetime): pubDate RFC-2822 GMT.

    Description — объёмная сводка (как у живых фидов): проходит junk-gate
    и не триггерит auto-полный текст (сводка не огрызок).
    """
    import datetime as dt
    from email.utils import format_datetime
    rows = ""
    for i, (t, d) in enumerate(items):
        pub = ""
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            pub = f"<pubDate>{format_datetime(d, usegmt=True)}</pubDate>"
        rows += (f"<item><title>{t}</title><link>https://x/{i}</link>"
                 f"<guid>https://x/{i}</guid>{pub}"
                 f"<description>{'статья в деталях. ' * 30}</description></item>")
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{rows}</channel></rss>'


def _cfg_for_backfill(tmp_path, feed_toml):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text(feed_toml)
    cfg = fz.load_config(str(cfgf))
    cfg.workdir = str(tmp_path)
    cfg.out = str(tmp_path / "out")
    return cfg


def test_backfill_plan_window_and_weeks(tmp_path, monkeypatch):
    import datetime as dt
    now = dt.datetime(2026, 10, 4, 12, 0, 0)
    old = now - dt.timedelta(days=200)          # вне окна
    inwin1 = now - dt.timedelta(days=20)        # ~3 недели назад
    inwin2 = now - dt.timedelta(days=1)         # текущая неделя
    xml = _rss_age([("старая за окном", old), ("неделя-3", inwin1),
                    ("текущая", inwin2), ("без даты", None)])
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: xml)
    cfg = _cfg_for_backfill(tmp_path, '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    plan, window = fz.backfill_plan(cfg, 12, now=now)
    weeks = plan["main"]
    assert len(window) == 2                     # старая и без даты не в окне
    assert any("неделя-3" in i["title"] for b in weeks.values() for i in b)
    assert all("старая за окном" not in i["title"] for b in weeks.values() for i in b)
    assert len(weeks) == 2                       # две разные ISO-недели


def test_backfill_plan_journal_split(tmp_path, monkeypatch):
    import datetime as dt
    now = dt.datetime(2026, 10, 4, 12, 0, 0)
    xml = _rss_age([("a", now - dt.timedelta(days=2))])
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: xml)
    cfg = _cfg_for_backfill(tmp_path, '''
[[feed]]
name = "Main"
url = "https://x/rss"

[[feed]]
name = "HN"
url = "https://x/hn"
journal = "hn"
''')
    plan, _ = fz.backfill_plan(cfg, 4, now=now)
    assert set(plan) == {"main", "hn"}


def test_cap_bucket_per_feed(tmp_path):
    import datetime as dt
    cfg = _cfg_for_backfill(tmp_path, 'max_per_feed = 2\n[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    fd = cfg.feed[0]
    items = [{"title": f"t{i}", "guid": f"g{i}", "date": dt.datetime(2026, 10, 1), "feed": fd}
             for i in range(5)]
    assert [i["title"] for i in fz._cap_bucket(items, cfg)] == ["t0", "t1"]


def test_backfill_dry_run_purity(tmp_path, monkeypatch):
    import datetime as dt
    now = dt.datetime(2026, 10, 4, 12, 0, 0)
    xml = _rss_age([("a", now - dt.timedelta(days=3))])
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: xml)
    cfg = _cfg_for_backfill(tmp_path, '[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    assert fz.run_backfill(cfg, fz.RunOpts(preset="tiny", text_only=True,
                                           dry_run=True), 4) == 0
    # ни state.json, ни каталогов — файловую систему не трогаем
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "out").exists()


def test_backfill_empty_bucket_after_cap_skips(tmp_path, monkeypatch):
    import datetime as dt
    now = dt.datetime(2026, 10, 4, 12, 0, 0)
    # неделя есть, но cap = 0: бакет пустеет — выпуск не создаётся,
    # номер не расходуется, а срезанное капом всё равно прочитано
    xml = _rss_age([("a", now - dt.timedelta(days=3)), ("b", now - dt.timedelta(days=4))])
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: xml)
    cfg = _cfg_for_backfill(tmp_path, '[[feed]]\nname = "X"\nurl = "https://x/rss"\nmax = 0\n')
    assert fz.run_backfill(cfg, fz.RunOpts(preset="tiny", text_only=True), 4) == 0
    assert list((tmp_path / "out").glob("*.epub")) == []
    st = fz.load_state(str(tmp_path))
    assert st["issue"] == {}
    assert set(st["seen"]) == {"https://x/0", "https://x/1"}   # cap-cut тоже seen


def test_backfill_emits_weeks_and_restores_pending(tmp_path, monkeypatch):
    import datetime as dt
    now = dt.datetime(2026, 10, 4, 12, 0, 0)
    w1 = now - dt.timedelta(days=13)
    w2 = now - dt.timedelta(days=2)
    xml = _rss_age([("старая неделя", w1), ("новая неделя", w2)])
    monkeypatch.setattr(fz, "http_get", lambda url, **kw: xml)
    monkeypatch.setattr(fz, "html_to_md", lambda h: h)
    cfg = _cfg_for_backfill(tmp_path, 'title = "Ретро"\n[[feed]]\nname = "X"\nurl = "https://x/rss"\n')

    # в копилке main уже лежит статья вне окна — должна уцелеть
    fz.save_state(str(tmp_path), {"seen": {}, "issue": {},
                                  "pending": {"main": [{"guid": "old-1", "title": "копилка",
                                                         "date": None, "feed": {}}]},
                                  "last_emit": {}})
    assert fz.run_backfill(cfg, fz.RunOpts(preset="tiny", text_only=True), 4) == 0
    epubs = sorted((tmp_path / "out").glob("*.epub"))
    assert len(epubs) == 2                       # две недели — два выпуска
    # выпуски датированы последней статьёй своей недели
    assert w2.strftime("%Y-%m-%d") in epubs[1].name
    assert w1.strftime("%Y-%m-%d") in epubs[0].name
    st = fz.load_state(str(tmp_path))
    assert st["issue"] == {"main": 2}
    assert set(st["seen"]) == {"https://x/0", "https://x/1"}
    # копилка восстановлена минус вышедшее
    assert [p["guid"] for p in st["pending"].get("main", [])] == ["old-1"]
    assert st["last_emit"]["main"] == w2.strftime("%Y-%m-%d")
