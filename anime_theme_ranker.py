import argparse
import time
import sys
import os
import webbrowser
import html as html_lib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("Встанови requests:  pip install requests")

try:
    import yt_dlp
except ImportError:
    sys.exit("Встанови yt-dlp:    pip install yt-dlp")

try:
    from rich.console import Console
    from rich.progress import (
        Progress, SpinnerColumn, BarColumn,
        TextColumn, TaskProgressColumn, TimeRemainingColumn,
    )
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.columns import Columns
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

console = Console(highlight=False) if HAS_RICH else None

ANIMETHEMES_API = "https://api.animethemes.moe"
HEADERS = {"User-Agent": "AnimeThemeRanker/1.0", "Accept": "application/json"}


# ─── Структури даних ─────────────────────────────────────────────────────────

@dataclass
class Theme:
    anime_name: str
    anime_slug: str
    theme_type: str        # OP / ED
    sequence: int          # 1, 2, 3 ...
    song_title: str
    artists: list[str] = field(default_factory=list)
    yt_url: Optional[str] = None
    yt_views: Optional[int] = None
    yt_title: Optional[str] = None

    @property
    def label(self) -> str:
        seq = str(self.sequence) if self.sequence else ""
        return f"{self.theme_type}{seq}"

    @property
    def search_queries(self) -> list[str]:
        """Декілька стратегій пошуку — від найточнішої до загальної.

        Перший запит (пісня + виконавець) добре працює для японських офіційних
        каналів, де назва аніме часто пишеться лише ієрогліфами, а пісня та
        артист — латиницею. Подальші запити — fallback'и.
        """
        type_word = "opening" if self.theme_type == "OP" else "ending"
        artist = self.artists[0] if self.artists else ""
        has_song = self.song_title and self.song_title != "Unknown"
        seq = self.sequence or 1

        queries: list[str] = []
        seen: set[str] = set()

        def add(q: str) -> None:
            q = " ".join(q.split())
            if q and q.lower() not in seen:
                seen.add(q.lower())
                queries.append(q)

        # 1. Пісня + виконавець — найнадійніше для офіційних релізів
        if has_song and artist:
            add(f"{self.song_title} {artist}")
        # 2. Аніме + пісня — коли немає артиста або як підстраховка
        if has_song:
            add(f"{self.anime_name} {self.song_title}")
        # 3. Аніме + OP1/ED2 — типове позначення на фанатських каналах
        add(f"{self.anime_name} {self.theme_type}{seq}")
        # 4. Загальний запит — остання спроба
        add(f"{self.anime_name} {type_word}")
        return queries

    @property
    def animethemes_url(self) -> str:
        return f"https://animethemes.moe/anime/{self.anime_slug}"


# ─── Логування ───────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    if HAS_RICH:
        console.print(msg)
    else:
        import re
        print(re.sub(r"\[/?[a-zA-Z0-9_ #]+\]", "", msg))


# ─── AnimeThemes API ─────────────────────────────────────────────────────────

def fetch_season(year: int, season: str, theme_filter: Optional[str]) -> list[Theme]:
    themes: list[Theme] = []
    page = 1
    page_size = 100

    log(f"\n[bold cyan]📡 Завантажую аніме сезону {season} {year}...[/bold cyan]")

    while True:
        params = {
            "filter[year]": year,
            "filter[season]": season.capitalize(),
            "include": "animethemes.song.artists,animethemes.animethemeentries",
            "page[size]": page_size,
            "page[number]": page,
        }
        if theme_filter:
            params["filter[animethemes.type]"] = theme_filter.upper()

        try:
            r = requests.get(
                f"{ANIMETHEMES_API}/anime", params=params, headers=HEADERS, timeout=20
            )
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"[red]❌ Помилка AnimeThemes API: {e}[/red]")
            sys.exit(1)

        data = r.json()
        anime_list = data.get("anime", [])

        if not anime_list:
            break

        for anime in anime_list:
            for theme in anime.get("animethemes", []):
                song = theme.get("song") or {}
                song_title = song.get("title") or "Unknown"
                artists = [a["name"] for a in (song.get("artists") or []) if a.get("name")]
                themes.append(Theme(
                    anime_name=anime["name"],
                    anime_slug=anime["slug"],
                    theme_type=theme.get("type", "OP"),
                    sequence=theme.get("sequence") or 0,
                    song_title=song_title,
                    artists=artists,
                ))

        meta  = data.get("meta", {})
        total = meta.get("total", 0)
        loaded = min(page * page_size, total)
        log(f"   [dim]Сторінка {page}: завантажено аніме [white]{loaded}[/white]/{total}[/dim]")
        if page * page_size >= total:
            break
        page += 1
        time.sleep(0.3)

    log(f"[green]✅ Знайдено тем:[/green] [bold white]{len(themes)}[/bold white]")
    return themes


# ─── YouTube пошук ───────────────────────────────────────────────────────────

YDL_OPTS_SEARCH = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,       # швидкий список без деталей
    "skip_download": True,
}

YDL_OPTS_FULL = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,      # повна інфа — для отримання duration
}

def search_youtube(theme: Theme, delay: float = 1.5) -> None:
    # Тривалість TV-size кліпів варіюється сильніше, ніж здається:
    # короткі прев'ю ~60s, типове TV-size ~88-92s, з кредитами/лого каналу до ~120s.
    DURATION_MIN  = 60   # 1:00 — мінімум розумного кліпу
    DURATION_IDEAL_LO = 80   # ідеальне вікно TV-size
    DURATION_IDEAL_HI = 105
    DURATION_MAX  = 135  # 2:15 — далі майже завжди повна версія
    type_word = "opening" if theme.theme_type == "OP" else "ending"

    def is_bad_title(title: str) -> bool:
        # Тільки явні маркери "не той кліп". `lyrics`, `music video` —
        # прибрав, бо багато офіційних відео саме так і називаються.
        t = title.lower()
        return any(w in t for w in (
            "full ver", "full version", "full song", "full audio",
            "フル", "tv size cover", "cover by", "piano cover",
            "guitar cover", "karaoke",
        ))

    try:
        # 1. Збираємо кандидатів з кількох пошукових запитів (fallback-стратегія)
        all_entries: list[dict] = []
        seen_ids: set[str] = set()

        with yt_dlp.YoutubeDL(YDL_OPTS_SEARCH) as ydl:
            for query in theme.search_queries:
                try:
                    info = ydl.extract_info(f"ytsearch10:{query}", download=False)
                except Exception:
                    continue
                for e in (info or {}).get("entries", []):
                    if e and e.get("id") and e["id"] not in seen_ids:
                        seen_ids.add(e["id"])
                        all_entries.append(e)
                # Достатньо кандидатів — наступні запити вже не потрібні
                if len(all_entries) >= 12:
                    break

        if not all_entries:
            return

        # Відкидаємо очевидно небажані за назвою; якщо нічого не лишилось —
        # повертаємо повний список як fallback
        candidates = [e for e in all_entries if not is_bad_title(e.get("title") or "")]
        if not candidates:
            candidates = all_entries

        # 2. Для кожного кандидата отримуємо повні дані та оцінюємо
        best = None
        best_score: tuple = (-999, 0)

        song_l   = (theme.song_title or "").lower()
        artists_l = [a.lower() for a in theme.artists]
        anime_l  = (theme.anime_name or "").lower()
        has_song = song_l and song_l != "unknown"

        with yt_dlp.YoutubeDL(YDL_OPTS_FULL) as ydl:
            # Беремо до 8 кандидатів — більше кандидатів = вища ймовірність знайти точний кліп
            for entry in candidates[:8]:
                try:
                    full = ydl.extract_info(
                        f"https://youtube.com/watch?v={entry['id']}", download=False
                    )
                except Exception:
                    continue
                if not full:
                    continue

                duration = full.get("duration") or 0
                title    = (full.get("title") or "").lower()
                channel  = (full.get("channel") or "").lower()
                views    = full.get("view_count") or 0

                # Жорстке відсікання — лише за межами реалістичних рамок.
                # Все, що між ними, отримує штраф/бонус через scoring.
                if duration and (duration < DURATION_MIN or duration > DURATION_MAX):
                    continue

                # ── Підрахунок скору ───────────────────────────────────────
                # Тривалість: бонус за ідеальне вікно, 0 за допустиме
                if duration and DURATION_IDEAL_LO <= duration <= DURATION_IDEAL_HI:
                    dur_bonus = 2
                elif duration:
                    dur_bonus = 0
                else:
                    dur_bonus = 0

                # Найсильніший сигнал: назва пісні в заголовку відео
                song_bonus = 3 if (has_song and song_l in title) else 0

                # Артист у назві або в каналі — теж дуже сильний сигнал
                artist_bonus = 2 if any(
                    a and (a in title or a in channel) for a in artists_l
                ) else 0

                # Слово opening/ending у назві
                clip_bonus = 1 if (type_word in title or theme.theme_type.lower() in title) else 0

                # Назва аніме у заголовку (перші 10 символів — щоб працювало
                # і для довгих назв з підзаголовками)
                anime_bonus = 1 if (anime_l[:10] in title) else 0

                # Офіційний канал
                off_bonus = 1 if ("official" in title or "official" in channel) else 0

                score = (
                    song_bonus + artist_bonus + dur_bonus
                    + clip_bonus + anime_bonus + off_bonus,
                    views,
                )

                if score > best_score:
                    best_score = score
                    best = full

        if best:
            theme.yt_url   = f"https://youtube.com/watch?v={best.get('id', '')}"
            theme.yt_views = best.get("view_count")
            theme.yt_title = best.get("title")

    except Exception:
        pass

    time.sleep(delay)


# ─── HTML генератор ──────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="https://cdn.datatables.net/1.13.8/css/jquery.dataTables.min.css">
  <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
  <script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>
  <style>
    :root {{
      --bg:      #0f1117;
      --surface: #1a1d27;
      --border:  #2a2d3a;
      --accent:  #e63946;
      --gold:    #f4c542;
      --text:    #e8eaf0;
      --muted:   #8891a8;
      --op:      #4fc3f7;
      --ed:      #ce93d8;
      --link:    #64b5f6;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg); color: var(--text);
      font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
      font-size: 14px; padding: 32px 24px 64px;
    }}
    header {{
      display: flex; align-items: baseline; gap: 16px;
      margin-bottom: 28px; border-bottom: 2px solid var(--accent);
      padding-bottom: 14px;
    }}
    header h1 {{ font-size: 22px; font-weight: 700; letter-spacing: .02em; }}
    header h1 .flag {{ font-size: 26px; }}
    header .meta {{ color: var(--muted); font-size: 13px; }}
    .stats {{
      display: flex; gap: 20px; margin-bottom: 22px; flex-wrap: wrap;
    }}
    .stat-box {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; padding: 10px 18px; min-width: 120px;
    }}
    .stat-box .val {{ font-size: 20px; font-weight: 700; color: var(--gold); }}
    .stat-box .lbl {{
      font-size: 11px; color: var(--muted);
      text-transform: uppercase; letter-spacing: .06em;
    }}
    /* DataTables overrides */
    #rankTable_wrapper .dataTables_filter input {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 6px; color: var(--text); padding: 5px 10px; outline: none;
    }}
    #rankTable_wrapper .dataTables_length select {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 6px; color: var(--text); padding: 4px 8px;
    }}
    #rankTable_wrapper .dataTables_info,
    #rankTable_wrapper .dataTables_length label,
    #rankTable_wrapper .dataTables_filter label {{ color: var(--muted); }}
    #rankTable_wrapper .dataTables_paginate .paginate_button {{
      color: var(--muted) !important; border-radius: 4px;
    }}
    #rankTable_wrapper .dataTables_paginate .paginate_button.current,
    #rankTable_wrapper .dataTables_paginate .paginate_button:hover {{
      background: var(--accent) !important; color: #fff !important; border: none !important;
    }}
    table#rankTable {{
      width: 100% !important; border-collapse: collapse;
      background: var(--surface); border-radius: 10px;
      overflow: hidden; border: 1px solid var(--border);
    }}
    table#rankTable thead th {{
      background: #13161f; color: var(--muted);
      font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .07em;
      padding: 12px 14px; border-bottom: 2px solid var(--border);
      white-space: nowrap;
    }}
    table#rankTable thead th.sorting_asc,
    table#rankTable thead th.sorting_desc {{ color: var(--accent); }}
    table#rankTable tbody tr {{
      border-bottom: 1px solid var(--border); transition: background .15s;
    }}
    table#rankTable tbody tr:hover {{ background: #22263a; }}
    table#rankTable tbody td {{ padding: 10px 14px; vertical-align: middle; }}
    .rank {{
      font-weight: 700; color: var(--muted);
      text-align: right; font-variant-numeric: tabular-nums;
    }}
    .rank-1 {{ color: #FFD700; }}
    .rank-2 {{ color: #C0C0C0; }}
    .rank-3 {{ color: #CD7F32; }}
    .badge {{
      display: inline-block; padding: 2px 8px; border-radius: 4px;
      font-size: 11px; font-weight: 700; letter-spacing: .05em;
    }}
    .badge-op {{ background: rgba(79,195,247,.15); color: var(--op); }}
    .badge-ed {{ background: rgba(206,147,216,.15); color: var(--ed); }}
    .views {{
      font-variant-numeric: tabular-nums; font-weight: 600;
      color: #81c784; text-align: right; white-space: nowrap;
    }}
    .views.no-data {{ color: var(--muted); font-weight: 400; }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; color: #90caf9; }}
    .artists {{ color: var(--muted); font-size: 13px; }}
    .no-yt {{ color: var(--muted); font-size: 12px; }}
    footer {{
      margin-top: 40px; color: var(--muted);
      font-size: 12px; text-align: center;
    }}
  </style>
</head>
<body>

<header>
  <h1><span class="flag">🎌</span> Anime Theme Ranker</h1>
  <div class="meta">
    Сезон: <strong>{season} {year}</strong>{type_label}
    &nbsp;·&nbsp; Згенеровано {generated}
  </div>
</header>

<div class="stats">
  <div class="stat-box"><div class="val">{total_themes}</div><div class="lbl">Всього тем</div></div>
  <div class="stat-box"><div class="val">{found_yt}</div><div class="lbl">Знайдено на YouTube</div></div>
  <div class="stat-box"><div class="val">{top_views}</div><div class="lbl">Макс. переглядів</div></div>
  <div class="stat-box"><div class="val">{total_views}</div><div class="lbl">Всього переглядів</div></div>
</div>

<table id="rankTable" class="display">
  <thead>
    <tr>
      <th>#</th>
      <th>Аніме</th>
      <th>Тема</th>
      <th>Пісня</th>
      <th>Виконавець</th>
      <th>Перегляди</th>
      <th>YouTube</th>
      <th>AnimeThemes</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>

<footer>
  Дані: <a href="https://animethemes.moe" target="_blank">AnimeThemes.moe</a>
  &nbsp;·&nbsp; Перегляди: YouTube (yt-dlp)
  &nbsp;·&nbsp; Дані актуальні на момент запуску скрипту
</footer>

<script>
$(document).ready(function() {{
  $('#rankTable').DataTable({{
    order: [[5, 'desc']],
    pageLength: 50,
    lengthMenu: [25, 50, 100, 9999],
    autoWidth: true,
    columnDefs: [
      {{ targets: 0, width: '44px' }},
      {{ targets: 2, width: '60px' }},
      {{ targets: 5, type: 'num-fmt', width: '120px' }},
      {{ targets: [6, 7], orderable: false }},
    ],
    language: {{
      search:     "🔍 Пошук:",
      lengthMenu: "Показати _MENU_ рядків",
      info:       "Рядки _START_–_END_ з _TOTAL_",
      infoEmpty:  "Немає даних",
      zeroRecords:"Нічого не знайдено",
      paginate:   {{ previous: "←", next: "→" }}
    }}
  }});
}});
</script>

</body>
</html>
"""


def _fmt_views(v: Optional[int]) -> str:
    if v is None:
        return "—"
    return f"{v:,}".replace(",", "\u202f")


def generate_html(
    themes: list[Theme],
    year: int,
    season: str,
    theme_filter: Optional[str],
    output_path: str,
) -> None:
    ranked  = sorted([t for t in themes if t.yt_views is not None],
                     key=lambda t: t.yt_views or 0, reverse=True)
    no_data = [t for t in themes if t.yt_views is None]

    rows_html = []

    # Спочатку з даними (пронумеровані), потім без
    for i, t in enumerate(ranked, 1):
        rank_cls  = f"rank rank-{i}" if i <= 3 else "rank"
        badge_cls = "badge-op" if t.theme_type == "OP" else "badge-ed"
        yt_label  = html_lib.escape((t.yt_title or "")[:55] + ("…" if len(t.yt_title or "") > 55 else ""))

        rows_html.append(
            f'    <tr>'
            f'<td class="{rank_cls}">{i}</td>'
            f'<td><a href="{t.animethemes_url}" target="_blank">{html_lib.escape(t.anime_name)}</a></td>'
            f'<td><span class="badge {badge_cls}">{html_lib.escape(t.label)}</span></td>'
            f'<td>{html_lib.escape(t.song_title)}</td>'
            f'<td class="artists">{html_lib.escape(", ".join(t.artists) or "—")}</td>'
            f'<td class="views" data-order="{t.yt_views}">{_fmt_views(t.yt_views)}</td>'
            f'<td><a href="{t.yt_url}" target="_blank">▶ {yt_label or "YouTube"}</a></td>'
            f'<td><a href="{t.animethemes_url}" target="_blank">AnimeThemes</a></td>'
            f'</tr>'
        )

    for t in no_data:
        badge_cls = "badge-op" if t.theme_type == "OP" else "badge-ed"
        rows_html.append(
            f'    <tr>'
            f'<td class="rank" style="color:#444">—</td>'
            f'<td><a href="{t.animethemes_url}" target="_blank">{html_lib.escape(t.anime_name)}</a></td>'
            f'<td><span class="badge {badge_cls}">{html_lib.escape(t.label)}</span></td>'
            f'<td>{html_lib.escape(t.song_title)}</td>'
            f'<td class="artists">{html_lib.escape(", ".join(t.artists) or "—")}</td>'
            f'<td class="views no-data" data-order="-1">—</td>'
            f'<td class="no-yt">не знайдено</td>'
            f'<td><a href="{t.animethemes_url}" target="_blank">AnimeThemes</a></td>'
            f'</tr>'
        )

    total_v    = sum(t.yt_views for t in ranked if t.yt_views)
    type_label = f" [{theme_filter}]" if theme_filter else ""

    html = HTML_TEMPLATE.format(
        title        = f"Anime Theme Ranker — {season} {year}",
        season       = season,
        year         = year,
        type_label   = type_label,
        generated    = datetime.now().strftime("%d.%m.%Y %H:%M"),
        total_themes = len(themes),
        found_yt     = len(ranked),
        top_views    = _fmt_views(ranked[0].yt_views if ranked else None),
        total_views  = _fmt_views(total_v),
        rows         = "\n".join(rows_html),
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    abs_path = os.path.abspath(output_path)
    log(f"\n[green]✅ HTML збережено:[/green] [bold]{abs_path}[/bold]")
    log("[cyan]🌐 Відкриваю у браузері...[/cyan]")
    webbrowser.open(f"file://{abs_path}")


# ─── Rich: підсумкова таблиця в консолі ──────────────────────────────────────

def print_console_summary(themes: list[Theme], top_n: int = 20) -> None:
    """Виводить красивий топ прямо в термінал після завершення пошуку."""
    ranked = sorted(
        [t for t in themes if t.yt_views is not None],
        key=lambda t: t.yt_views or 0,
        reverse=True,
    )
    no_data_count = sum(1 for t in themes if t.yt_views is None)

    if not ranked:
        log("[yellow]⚠ Нічого не знайдено на YouTube.[/yellow]")
        return

    if HAS_RICH:
        # ── Статистика ─────────────────────────────────────────────────────
        total_views = sum(t.yt_views for t in ranked if t.yt_views)

        stats_table = Table.grid(padding=(0, 3))
        stats_table.add_column(justify="center")
        stats_table.add_column(justify="center")
        stats_table.add_column(justify="center")
        stats_table.add_column(justify="center")

        def stat(val, label):
            return Text.assemble(
                (str(val), "bold yellow"),
                "\n",
                (label, "dim"),
            )

        stats_table.add_row(
            stat(len(themes),     "всього тем"),
            stat(len(ranked),     "знайдено на YT"),
            stat(_fmt_views(ranked[0].yt_views), "макс. переглядів"),
            stat(_fmt_views(total_views), "разом переглядів"),
        )
        console.print(Panel(stats_table, border_style="dim", padding=(0, 2)))

        # ── Топ таблиця ────────────────────────────────────────────────────
        table = Table(
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold dim",
            border_style="dim",
            row_styles=["", "on grey7"],
            pad_edge=False,
            expand=True,
        )
        table.add_column("#",           width=4,  justify="right",  style="bold")
        table.add_column("Аніме",       min_width=22, max_width=34)
        table.add_column("Тема",        width=5,  justify="center")
        table.add_column("Пісня",       min_width=20, max_width=32)
        table.add_column("Виконавець",  min_width=16, max_width=26, style="dim")
        table.add_column("Перегляди",   width=14, justify="right",  style="bold green")

        MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

        for i, t in enumerate(ranked[:top_n], 1):
            rank_str  = MEDALS.get(i, str(i))
            views_str = _fmt_views(t.yt_views)
            badge     = Text(t.label, style="bold cyan" if t.theme_type == "OP" else "bold magenta")
            table.add_row(rank_str, t.anime_name, badge, t.song_title,
                          ", ".join(t.artists) or "—", views_str)

        title_str = (
            f"🏆  Топ {min(top_n, len(ranked))} OP/ED"
            f" · {args_cache['season']} {args_cache['year']}"
            + (f" [{args_cache['type']}]" if args_cache.get('type') else "")
        )
        console.print()
        console.print(Panel(table, title=title_str, border_style="bright_black", padding=(0, 1)))

        if no_data_count:
            log(f"[dim]  Без даних YouTube: {no_data_count} тем[/dim]")

    else:
        # Простий текстовий fallback
        print(f"\n{'#':>3}  {'Аніме':<32} {'Тема':<5}  {'Перегляди':>14}")
        print("─" * 60)
        for i, t in enumerate(ranked[:top_n], 1):
            print(f"{i:>3}  {t.anime_name[:31]:<32} {t.label:<5}  {_fmt_views(t.yt_views):>14}")


# глобальний кеш аргументів для print_console_summary
args_cache: dict = {}


# ─── Точка входу ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ранжує OP/ED сезону за переглядами на YouTube")
    parser.add_argument("--year",   type=int, default=2026, help="Рік (за замовчуванням 2026)")
    parser.add_argument("--season", type=str, default="Spring",
                        choices=["Winter", "Spring", "Summer", "Fall"],
                        help="Сезон (за замовчуванням Spring)")
    parser.add_argument("--type",   type=str, default=None,
                        choices=["OP", "ED"], help="Тільки OP або тільки ED")
    parser.add_argument("--delay",  type=float, default=1.5,
                        help="Пауза між запитами YouTube (сек)")
    parser.add_argument("--top",    type=int, default=20,
                        help="Скільки рядків показати в консольному топі (за замовчуванням 20)")
    parser.add_argument("--out",    type=str, default=None,
                        help="Ім'я HTML файлу (за замовчуванням авто)")
    args = parser.parse_args()

    args_cache.update(vars(args))

    label    = f"{args.season}_{args.year}" + (f"_{args.type}" if args.type else "")
    out_path = args.out or f"anime_themes_{label}.html"

    # ── Шапка ──────────────────────────────────────────────────────────────
    type_str = f" [bold magenta][{args.type}][/bold magenta]" if args.type else ""
    if HAS_RICH:
        console.print()
        console.rule(
            f"[bold white]🎌 Anime Theme Ranker[/bold white]"
            f"  [dim]{args.season} {args.year}[/dim]{type_str}",
            style="bright_black",
        )
    else:
        print(f"\n{'─'*60}")
        print(f"  🎌 Anime Theme Ranker  —  {args.season} {args.year}"
              + (f" [{args.type}]" if args.type else ""))
        print(f"{'─'*60}")

    # 1. Отримуємо теми
    themes = fetch_season(args.year, args.season, args.type)
    if not themes:
        log("[red]❌ Теми не знайдено. Перевір рік/сезон.[/red]")
        return

    # 2. Шукаємо на YouTube з rich progress
    eta = len(themes) * args.delay
    log(f"\n[bold]🔍 Шукаю на YouTube[/bold] [dim]({len(themes)} тем, ≈{eta:.0f} сек)[/dim]\n")

    if HAS_RICH:
        with Progress(
            SpinnerColumn(spinner_name="dots", style="cyan"),
            TextColumn("[progress.description]{task.description}", justify="left"),
            BarColumn(bar_width=28, style="cyan", complete_style="bright_cyan"),
            TaskProgressColumn(),
            TextColumn("[dim]залишилось[/dim]"),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("", total=len(themes))
            for theme in themes:
                progress.update(
                    task,
                    description=(
                        f"[cyan]{theme.anime_name[:28]}[/cyan]"
                        f"[dim] — [/dim]"
                        f"[{'bright_cyan' if theme.theme_type == 'OP' else 'magenta'}]{theme.label}[/]"
                    ),
                )
                search_youtube(theme, delay=args.delay)
                progress.advance(task)
    else:
        for i, theme in enumerate(themes, 1):
            print(f"  [{i}/{len(themes)}] {theme.anime_name} — {theme.label}")
            search_youtube(theme, delay=args.delay)

    # 3. Консольний топ
    print_console_summary(themes, top_n=args.top)

    # 4. HTML файл
    console.print() if HAS_RICH else print()
    console.rule("[dim]HTML[/dim]", style="bright_black") if HAS_RICH else None
    generate_html(themes, args.year, args.season, args.type, out_path)


if __name__ == "__main__":
    main()