# feedzine

RSS/Atom фиды → периодические «журналы» в EPUB. Заточено под e-ink читалки:
true-grayscale картинки, вписывание в бокс экрана, baseline JPEG, книжная
типографика. Чистый Python stdlib (+ Pillow в обёртке), pandoc для сборки.

Каждый `feedzine issue` собирает **выпуск** из статей, появившихся в фидах
с прошлого выпуска: сводка из фида или полный текст (Habr — через kek/v2,
остальные сайты — `<article>`-extractor), статьи группируются по фидам,
в навигацию попадают и фиды, и статьи. Прочитанное помечается в state —
дубликатов между выпусками нет.

## Установка

```bash
nix run github:71g3pf4c3/feedzine -- init   # напишет ~/.config/feedzine/config.toml
nix profile install github:71g3pf4c3/feedzine
```

## Использование

```bash
feedzine init                          # пример конфига
feedzine list                          # фиды, сколько нового
feedzine issue                         # собрать выпуск из нового
feedzine issue --preset mini --text-only
```

Конфиг (`~/.config/feedzine/config.toml`):

```toml
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
section = "Хабр"                 # своя секция вместо имени фида

[[feed]]
name = "DTF · главное"
url = "https://dtf.ru/rss/all"
full_text = false                # только сводки
```

- `full_text` глобально (`auto | true | false`) и на фид. `auto`: Habr —
  полный текст (kek/v2 `textHtml`), прочие — сводка из фида; `true` — для
  чужих сайтов пробуется `<article>`-extraction, не вышло — сводка.
- `max` на фид ограничивает выпуск, если фид зашумлён. Сверх капа статьи
  помечаются прочитанными (не переносятся в следующий выпуск).
- секции выпуска: `section` у фида или его имя;
  внутренние заголовки статей демо́тятся и не засоряют навигацию.

Выпуски именуются `Журнал_007_2026-10-04.epub` (номер + дата), обложка —
первая картинка выпуска. `issue` идемпотентен: нового нет — EPUB не
создаётся, state не меняется.

## Пресеты

| Пресет | Картинки |
|---|---|
| `reader` (default) | 480px true grayscale, бокс 480×800, JPEG q85 |
| `eink` | 480px цветной, бокс 480×800 |
| `mini` / `tiny` | 360/240px ч/б — совсем мелко |
| `hq` | 900px цветной |

Кэш картинок в `~/.cache/feedzine/img/<preset>/`, имена — sha1 URL:
повторные выпуски не перекачивают то, что уже лежит.

## Регулярный выпуск

systemd user timer (`~/.config/systemd/user/feedzine.timer`):

```ini
[Unit]
Description=feedzine issue

[Timer]
OnCalendar=daily 7:00
Persistent=true

[Install]
WantedBy=timers.target
```

`~/.config/systemd/user/feedzine.service`:

```ini
[Service]
Type=oneshot
ExecStart=%h/.nix-profile/bin/feedzine issue
```

## Как это работает

- фиды: RSS 2.0 и Atom через `xml.etree` (stdlib), дедуп по `guid`/`id`/`link`
- полный текст Habr: `GET https://habr.com/kek/v2/articles/<id>/` → `textHtml`;
  чужие сайты: первый `<article>…</article>` достаточной длины из SSR-страницы
- HTML → Markdown: pandoc; картинки вырезаются из HTML, качаются, обрабатываются
  (Pillow: grayscale/box/resize, baseline JPEG), ссылки переписываются на локальные
- EPUB: pandoc + книжная типографика (та же eink.css, что у [dtf-dl](../dtf-dl))

## Ограничения

- Habr: полнотекстовый fetcher рассчитан на kek/v2 — если Хабр его прикроет,
  фид продолжит работать в режиме сводок
- `full_text=true` для произвольных сайтов — best effort: нужен внятный
  `<article>` в разметке
- комментарии и закрытые посты не поддерживаются
