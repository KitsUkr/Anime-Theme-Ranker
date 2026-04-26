# Anime Theme Ranker

CLI-утиліта, що збирає всі OP/ED заданого аніме-сезону з [AnimeThemes API](https://api-docs.animethemes.moe), знаходить кожен трек на YouTube через `yt-dlp` і ранжує за переглядами. На виході — інтерактивний HTML зі сортуванням, пошуком і фільтрами.

## Встановлення

Потрібен Python 3.9+.

```bash
pip install requests yt-dlp rich
```

`rich` опційний — без нього просто не буде кольорового прогрес-бару.

## Використання

```bash
python anime_theme_ranker.py [--year YEAR] [--season SEASON] [--type {OP,ED}]
                             [--delay SEC] [--top N] [--out FILE]
```

| Аргумент   | Дефолт   | Опис                                                                |
|------------|----------|---------------------------------------------------------------------|
| `--year`   | `2026`   | Рік сезону                                                          |
| `--season` | `Spring` | `Winter` / `Spring` / `Summer` / `Fall`                             |
| `--type`   | —        | Фільтр `OP` або `ED`; без аргументу — обидва                        |
| `--delay`  | `1.5`    | Пауза між запитами до YouTube (сек)                                 |
| `--top`    | `20`     | Кількість рядків у консольному топі                                 |
| `--out`    | авто     | Шлях до HTML; за замовчуванням `anime_themes_<сезон>_<рік>.html`    |

Приклади:

```bash
python anime_theme_ranker.py
python anime_theme_ranker.py --year 2025 --season Fall --type OP
python anime_theme_ranker.py --season Winter --out winter.html
```

HTML відкриється в браузері автоматично.

## Як це працює

Для кожної теми, отриманої з API, будується кілька пошукових запитів — від `<song> <artist>` (найточніший, добре працює з японськими офіційними каналами) до `<anime> opening`. Кандидати з YouTube оцінюються за збігом назви пісні / виконавця в заголовку, тривалістю біля TV-size (~90 с), наявністю `official` тощо. Перемагає кандидат з найвищим скором; при рівності — той, у кого більше переглядів.

Якщо YouTube починає різати частоту запитів — підвищ `--delay`.

## Залежності

- [animethemes.moe](https://animethemes.moe) — API і дані тем
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — пошук на YouTube
- [rich](https://github.com/Textualize/rich) — TUI

## Ліцензія

MIT
