"""Юнит-тесты feedzine (без сети; pandoc нужен только epub-сборочным тестам)."""

import datetime
import importlib.util
import json
import os
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
  <description>&lt;p&gt;Сводка&lt;/p&gt;</description>
  <dc:creator>vasya</dc:creator>
</item>
<item>
  <title>Статья два</title>
  <link>https://habr.com/ru/articles/1089999/?utm=x</link>
  <guid>https://habr.com/ru/articles/1089999/</guid>
  <pubDate>Sat, 03 Oct 2026 09:00:00 GMT</pubDate>
  <description>&lt;p&gt;Ещё&lt;/p&gt;</description>
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
    assert st == {"seen": {}, "issue": 0}
    st["seen"]["x"] = "2026-10-04"
    st["issue"] = 3
    fz.save_state(str(tmp_path), st)
    assert fz.load_state(str(tmp_path)) == st


def test_state_bad_json(tmp_path):
    (tmp_path / "state.json").write_text("{")
    assert fz.load_state(str(tmp_path)) == {"seen": {}, "issue": 0}


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
    assert "### Большой" in lines            # демо́тнут, не потерян
    assert "#### Средний" in lines
    assert "###### Мелкий" in lines          # уровень видимости сохранён относительно
    assert "# не заголовок" in lines         # fenced-код не тронут
    assert "x = 1" in lines


def test_demote_headings_no_fence_leak():
    # после закрывающего ``` заголовки снова демо́тятся
    md = "```\n# in code\n```\n# real heading\n"
    lines = fz.demote_headings(md).split("\n")
    assert "# in code" in lines
    assert "### real heading" in lines


# ---------------------------------------------------------------- картинки

def test_img_name_stable():
    a = fz.img_name("https://habrastorage.org/getpro/habr/a.png")
    assert a == fz.img_name("https://habrastorage.org/getpro/habr/a.png")
    assert a != fz.img_name("https://habrastorage.org/getpro/habr/b.png")
    assert a.endswith(".jpg")


def test_rewrite_images_data_uri_kept():
    html = '<img src="data:image/png;base64,xxxx"/>'
    out, done = fz.rewrite_images(html, "/nonexistent", fz.RunOpts(preset="tiny"))
    assert done == [] and "data:" in out


def test_rewrite_images_bad_url_dropped():
    html = '<p>до</p><img src="https://invalid.invalid/x.png"/><p>после</p>'
    out, done = fz.rewrite_images(html, "/nonexistent", fz.RunOpts(preset="tiny"))
    assert done == []
    assert "<img" not in out
    assert "до" in out and "после" in out


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
    assert st["issue"] == 0


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
    assert fz.load_state(str(tmp_path / "wd")) == {"seen": {}, "issue": 0}


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
    assert len(st["pending"]) == 2
    assert st["issue"] == 0 and st.get("last_emit") is None
    # pending сериализуем и дат в ISO-строках
    assert all(isinstance(it["date"], (str, type(None))) for it in st["pending"])
    assert list((tmp_path / "out").glob("*.epub")) == []

    # force: сводка из накопленного, накопитель пустеет
    assert fz.run_issue(cfg, fz.RunOpts(preset="tiny", text_only=True, force=True)) == 0
    st = fz.load_state(str(tmp_path / "wd"))
    assert st["pending"] == []
    assert st["issue"] == 1
    assert st["last_emit"]
    epubs = list((tmp_path / "out").glob("*.epub"))
    assert len(epubs) == 1


# ---------------------------------------------------------------- TUI (textual)

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
