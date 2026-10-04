"""Юнит-тесты feedzine (без сети и pandoc)."""

import datetime
import importlib.util
import os

import pytest

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


# ---------------------------------------------------------------- картинки

def test_img_name_stable():
    a = fz.img_name("https://habrastorage.org/getpro/habr/a.png")
    assert a == fz.img_name("https://habrastorage.org/getpro/habr/a.png")
    assert a != fz.img_name("https://habrastorage.org/getpro/habr/b.png")
    assert a.endswith(".jpg")


def test_rewrite_images_data_uri_kept():
    html = '<img src="data:image/png;base64,xxxx"/>'
    out, done = fz.rewrite_images(html, "/nonexistent", argparse_ns())
    assert done == [] and "data:" in out


def test_rewrite_images_bad_url_dropped():
    html = '<p>до</p><img src="https://invalid.invalid/x.png"/><p>после</p>'
    out, done = fz.rewrite_images(html, "/nonexistent", argparse_ns())
    assert done == []
    assert "<img" not in out
    assert "до" in out and "после" in out


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(preset=kw.get("preset", "tiny"))


# ---------------------------------------------------------------- config

def test_load_config_defaults(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('[[feed]]\nname = "X"\nurl = "https://x/rss"\n')
    cfg = fz.load_config(str(cfgf))
    assert cfg["title"] == "Журнал"
    assert cfg["full_text"] == "auto"
    assert cfg["max_per_feed"] == 10
    assert cfg["preset"] == "reader"
    assert len(cfg["feed"]) == 1


# ---------------------------------------------------------------- collect

def test_collect_items_dedup_and_order(tmp_path):
    cfgf = tmp_path / "c.toml"
    cfgf.write_text('[[feed]]\nname = "X"\nurl = "https://x/rss"\nmax = 1\n')
    cfg = fz.load_config(str(cfgf))
    cfg["workdir"] = str(tmp_path)
    fz.save_state(cfg["workdir"], {"seen": {"https://habr.com/ru/articles/1089999/": "x"},
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
                 fz.load_state(cfg["workdir"])["seen"]][:cfg["feed"][0]["max"]], fz.load_state(cfg["workdir"])
    assert [f["title"] for f in fresh] == ["новая"]
    assert st["issue"] == 0


def test_section():
    assert fz._section({"section": "Хабр", "name": "Python"}) == "Хабр"
    assert fz._section({"name": "Just Feed"}) == "Just Feed"
    assert fz._section({}) == "RSS"


# ---------------------------------------------------------------- sanitize

def test_sanitize():
    assert fz.sanitize("Мой журнал!") == "Мой_журнал"
    assert fz.sanitize("///") == "item"
