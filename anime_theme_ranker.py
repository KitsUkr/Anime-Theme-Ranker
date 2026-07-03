import argparse
import time
import sys
import os
import re
import unicodedata
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
    from rapidfuzz import fuzz
except ImportError:
    sys.exit("Встанови rapidfuzz: pip install rapidfuzz")

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


# ─── Нормалізація і метчинг ──────────────────────────────────────────────────

def norm(s: str) -> str:
    """Нормалізує рядок для порівняння: NFKC, lower, уніфікує лапки/тире/пробіли."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[‘’ʼ`']", "'", s)   # різні апострофи → '
    s = re.sub(r"[—–−]", "-", s)     # em/en dash, мінус → -
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# Японські муз. лейбли + офіційні аніме-канали — оригінальні релізи
TRUSTED_CHANNELS = (
    "aniplex", "toho animation", "kadokawa anime", "avex",
    "sony music", "universal music japan", "warner music japan",
    "lantis", "pony canyon", "flyingdog", "victor entertainment",
    "king records", "nbcuniversal entertainment japan", "bandai namco music",
)

# Міжнародні передавачі (Crunchyroll, азійський релізери) — це не оригінал,
# а ліцензований реап, часто з англомовним інтерфейсом. Якщо є японський
# варіант — він має перемагати; якщо немає — реап показуємо з пенальті.
REUPLOADER_CHANNELS = (
    "crunchyroll", "muse asia", "ani-one", "ani-one asia",
    "medialink", "bilibili", "iqiyi",
)

# Маркери в назві відео, які майже завжди стоять на офіційних японських
# creditless-релізах OP/ED. Сильний позитивний сигнал.
JP_PATTERNS = ("ノンクレジット", "tvアニメ", "creditless", "non-credit", "non credit")

# Японські лапки 「...」 — стандартна позначка назви пісні в назвах
# офіційних кліпів. Якщо назва там НЕ наша — це інша секвенція/чуже аніме.
SONG_BRACKETS_RE = re.compile(r"「([^」]+)」")


def _is_jp_script(s: str) -> bool:
    """True якщо рядок переважно не-ASCII (kanji/hiragana/katakana).
    Тоді fuzzy-порівняння з romaji-формою ненадійне (завжди ~0), і ми
    не можемо обґрунтовано стверджувати "пісня інша".
    """
    if not s:
        return False
    non_ascii = sum(1 for c in s if ord(c) > 127)
    return non_ascii > len(s) / 2

# Маркери типу теми в назві відео. Якщо в кліпі стоїть протилежна
# до нашої теми позначка (OP при ED або навпаки) — це не той кліп.
OP_TYPE_MARKERS = ("オープニング", "ノンクレジットop", "op映像",
                   "opテーマ", "opムービー", "opening")
ED_TYPE_MARKERS = ("エンディング", "ノンクレジットed", "ed映像",
                   "edテーマ", "edムービー", "ending")

# Поріг якості: якщо найкращий кандидат набрав менше — краще "не знайдено",
# ніж випадковий чужий кліп.
SCORE_FLOOR = 4

# Спецсимволи в іменах виконавців, що збивають YouTube-пошук:
# SUPER★DRAGON, *Luna, ☆moefuwa☆ тощо. У нелапкованих запитах замінюємо на пробіл.
_SPECIAL_CHARS_RE = re.compile(r"[★☆♪♥♡・×÷●○◆◇■□▲△▼▽※*]")

# Маркери, що майже гарантовано означають "не той кліп"
_BAD_PATTERNS = [
    re.compile(r"\bfull\s+(?:ver|version|song|audio)\b"),
    re.compile(r"フル"),                         # フル
    re.compile(r"\bcover\b(?!\s+art)"),
    re.compile(r"\b(?:amv|mad)\b"),
    re.compile(r"\bcompilation\b"),
    re.compile(r"\bmashup\b"),
    re.compile(r"\bremix\b"),
    re.compile(r"\breaction\b"),
    re.compile(r"\bnightcore\b"),
    re.compile(r"\b(?:sped\s*up|slowed|8d\s+audio|karaoke)\b"),
    re.compile(r"\b(?:piano|guitar|violin)\s+cover\b"),
]


def is_bad_title(title: str) -> bool:
    """True якщо назва відео містить маркери не-OP/ED-кліпу (cover, full ver, amv, ...)."""
    t = norm(title)
    return any(p.search(t) for p in _BAD_PATTERNS)


# ─── Структури даних ─────────────────────────────────────────────────────────

@dataclass
class Theme:
    anime_name: str
    anime_slug: str
    theme_type: str        # OP / ED
    sequence: int          # 1, 2, 3 ...
    song_title: str
    artists: list[str] = field(default_factory=list)
    year: int = 0
    at_duration: Optional[float] = None   # тривалість офіційного кліпу з AnimeThemes
    anime_name_jp: Optional[str] = None   # японська назва (з animesynonyms)
    yt_url: Optional[str] = None
    yt_views: Optional[int] = None
    yt_title: Optional[str] = None

    @property
    def label(self) -> str:
        seq = str(self.sequence) if self.sequence else ""
        return f"{self.theme_type}{seq}"

    @property
    def short_name(self) -> str:
        """Скорочена назва аніме — до першого роздільника (двокрапки/тире/тильди).

        Довгі назви на кшталт "Kizoku Tensei: Megumareta Umare kara..." забивають
        YouTube-пошук шумом. Береться частина до роздільника, якщо вона не надто
        коротка; інакше повертається оригінал.
        """
        for sep in (": ", " - ", " ~", " ―"):
            if sep in self.anime_name:
                short = self.anime_name.split(sep, 1)[0].strip()
                if len(short) >= 3:
                    return short
        return self.anime_name

    @property
    def search_queries(self) -> list[tuple[str, int]]:
        """Стратегії пошуку: (запит, скільки результатів брати).

        Порядок важливий: ліміт у `search_youtube` зупиняє наступні запити,
        коли вже зібрано достатньо кандидатів. Тому першим — точний пошук
        пісня+виконавець у лапках: він мовно-нейтральний (знаходить і
        японські, і будь-які інші офіційні релізи з цією піснею). Далі JP-
        запити підкріплюють для випадків коли пошук скоріш мовозалежний.
        """
        type_word = "opening" if self.theme_type == "OP" else "ending"
        artist = self.artists[0] if self.artists else ""
        artist_clean = _SPECIAL_CHARS_RE.sub(" ", artist).strip() if artist else ""
        has_song = self.song_title and self.song_title != "Unknown"
        seq = self.sequence or 1
        short = self.short_name

        def _q(s: str) -> str:
            return s.replace('"', "").strip()

        queries: list[tuple[str, int]] = []
        seen: set[str] = set()

        def add(q: str, count: int) -> None:
            q = " ".join(q.split())
            if q and q.lower() not in seen:
                seen.add(q.lower())
                queries.append((q, count))

        # 1. Точний пошук "пісня" "виконавець" — найточніший варіант
        if has_song and artist:
            add(f'"{_q(self.song_title)}" "{_q(artist)}"', 15)
        # 2. JP-назва + пісня — ловить японські канали з ієрогліфічними назвами
        if self.anime_name_jp and has_song:
            add(f"{self.anime_name_jp} {self.song_title}", 5)
        # 3. JP-назва + ノンクレジット — generic fallback для JP-каналів коли
        #    точний запит не дав результатів (рідкі/нові пісні)
        if self.anime_name_jp:
            add(f"{self.anime_name_jp} ノンクレジット {self.theme_type}", 10)
        # 4. Без лапок (з очищеним артистом — ★ і подібне ламає YT-пошук)
        if has_song and artist_clean:
            add(f"{self.song_title} {artist_clean}", 5)
        # 5. Скорочена назва аніме + пісня
        if has_song:
            add(f"{short} {self.song_title}", 5)
        # 6. Скорочена назва + OP1/ED2 — типове позначення на каналах
        add(f"{short} {self.theme_type}{seq}", 5)
        # 7. Загальний запит з роком — відсікає старі компіляції
        if self.year:
            add(f"{short} {type_word} {self.year}", 5)
        else:
            add(f"{short} {type_word}", 5)
        return queries

    @property
    def animethemes_url(self) -> str:
        return f"https://animethemes.moe/anime/{self.anime_slug}"


# ─── Логування ───────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    if HAS_RICH:
        console.print(msg)
    else:
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
            "include": "animethemes.song.artists,animethemes.animethemeentries.videos,animesynonyms",
            "page[size]": page_size,
            "page[number]": page,
        }
        if theme_filter:
            params["filter[animetheme][type]"] = theme_filter.upper()

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
            # ── Японський синонім назви ─────────────────────────────────────
            # animesynonyms містить альтернативні назви; нас цікавить запис
            # типу "Native"/"Japanese" з ієрогліфами. Якщо API не вернув type —
            # беремо перший синонім, що містить не-ASCII символи.
            anime_name_jp: Optional[str] = None
            synonyms = anime.get("animesynonyms") or []
            for syn in synonyms:
                stype = (syn.get("type") or "").lower()
                text = syn.get("text") or ""
                if stype in ("native", "japanese") and any(ord(c) > 127 for c in text):
                    anime_name_jp = text
                    break
            if not anime_name_jp:
                for syn in synonyms:
                    text = syn.get("text") or ""
                    if any(ord(c) > 127 for c in text):
                        anime_name_jp = text
                        break

            for theme in anime.get("animethemes", []):
                song = theme.get("song") or {}
                song_title = song.get("title") or "Unknown"
                artists = [a["name"] for a in (song.get("artists") or []) if a.get("name")]

                # AnimeThemes іноді віддає duration на video — використаємо як
                # ground-truth для звіряння з YouTube-кандидатом. Якщо API його
                # не повернув — at_duration залишиться None і логіка падає на
                # стару евристику з вікном тривалості.
                at_duration: Optional[float] = None
                for entry in theme.get("animethemeentries", []):
                    for video in entry.get("videos", []):
                        d = video.get("duration")
                        if d:
                            at_duration = float(d)
                            break
                    if at_duration:
                        break

                themes.append(Theme(
                    anime_name=anime["name"],
                    anime_slug=anime["slug"],
                    theme_type=theme.get("type", "OP"),
                    sequence=theme.get("sequence") or 0,
                    song_title=song_title,
                    artists=artists,
                    year=year,
                    at_duration=at_duration,
                    anime_name_jp=anime_name_jp,
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

    # Debug-режим: збираємо всі скоринги для виведення в кінці.
    # `--debug "X"` друкує для конкретного аніме завжди.
    # `--debug-missing` друкує для будь-якої теми, що в кінці не знайшлась.
    debug_filter = (args_cache.get("debug") or "").strip()
    debug_active = bool(debug_filter) and debug_filter.lower() in (theme.anime_name or "").lower()
    debug_missing = bool(args_cache.get("debug_missing"))
    collect_debug = debug_active or debug_missing
    debug_rows: list[dict] = []

    try:
        # 1. Збираємо кандидатів з кількох пошукових запитів (fallback-стратегія)
        all_entries: list[dict] = []
        seen_ids: set[str] = set()

        with yt_dlp.YoutubeDL(YDL_OPTS_SEARCH) as ydl:
            for query, count in theme.search_queries:
                try:
                    info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
                except Exception:
                    continue
                for e in (info or {}).get("entries", []):
                    if e and e.get("id") and e["id"] not in seen_ids:
                        seen_ids.add(e["id"])
                        all_entries.append(e)
                # Достатньо кандидатів — наступні запити вже не потрібні.
                # 25 покриває: 15 з точного quoted + ~10 з JP-запитів —
                # обидва типи джерел потрапляють у пул, навіть якщо точний
                # дав 15 одразу.
                if len(all_entries) >= 25:
                    break

        if not all_entries:
            if collect_debug:
                _print_debug_no_results(theme)
            return

        # Відкидаємо очевидно небажані за назвою; якщо нічого не лишилось —
        # повертаємо повний список як fallback
        candidates = [e for e in all_entries if not is_bad_title(e.get("title") or "")]
        if not candidates:
            candidates = all_entries

        # 2. Для кожного кандидата отримуємо повні дані та оцінюємо
        best = None
        best_score: tuple = (-999, 0)

        song_n   = norm(theme.song_title)
        artists_n = [norm(a) for a in theme.artists if a and len(norm(a)) >= 2]
        anime_n  = norm(theme.anime_name)
        has_song = bool(song_n) and song_n != "unknown" and len(song_n) >= 2

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
                title_n  = norm(full.get("title") or "")
                channel_n = norm(full.get("channel") or "")
                views    = full.get("view_count") or 0
                upload_date = full.get("upload_date") or ""    # "YYYYMMDD"

                # Жорстке відсікання — лише за межами реалістичних рамок.
                if duration and (duration < DURATION_MIN or duration > DURATION_MAX):
                    continue

                # ── Тривалість ─────────────────────────────────────────────
                # Якщо AnimeThemes дав ground-truth duration — звіряємо ±5s.
                # Інакше — стара евристика з ідеальним вікном.
                if theme.at_duration and duration:
                    delta = abs(duration - theme.at_duration)
                    if delta <= 5:
                        dur_bonus = 3
                    elif delta <= 12:
                        dur_bonus = 1
                    else:
                        dur_bonus = -2     # сильно схоже на не той кліп
                elif duration and DURATION_IDEAL_LO <= duration <= DURATION_IDEAL_HI:
                    dur_bonus = 2
                else:
                    dur_bonus = 0

                # ── Назва пісні (fuzzy partial_ratio) ──────────────────────
                if has_song:
                    s_score = fuzz.partial_ratio(song_n, title_n)
                    if s_score >= 90:
                        song_bonus = 3
                    elif s_score >= 75:
                        song_bonus = 2
                    elif s_score >= 60:
                        song_bonus = 1
                    else:
                        song_bonus = 0
                else:
                    song_bonus = 0

                # ── Виконавець (fuzzy у заголовку АБО в каналі) ────────────
                artist_bonus = 0
                if artists_n:
                    a_score = max(
                        max(fuzz.partial_ratio(a, title_n),
                            fuzz.partial_ratio(a, channel_n))
                        for a in artists_n
                    )
                    if a_score >= 90:
                        artist_bonus = 2
                    elif a_score >= 75:
                        artist_bonus = 1

                # ── Слово opening/ending/OP/ED у назві ─────────────────────
                clip_bonus = 1 if (type_word in title_n or theme.theme_type.lower() in title_n) else 0

                # ── Назва аніме у заголовку ────────────────────────────────
                # JP-форма (якщо є): беремо "ядро" назви до японських
                # роздільників (для "貴族転生〜...〜" це "貴族転生" — унікальний
                # ідентифікатор аніме). Це найточніший збіг для японських каналів.
                anime_bonus = 0
                if theme.anime_name_jp:
                    jp_core = theme.anime_name_jp
                    for sep in ("〜", "～", "：", " "):
                        if sep in jp_core:
                            jp_core = jp_core.split(sep, 1)[0].strip()
                            break
                    raw_title = full.get("title") or ""
                    if jp_core and len(jp_core) >= 2 and jp_core in raw_title:
                        anime_bonus = 2
                # Романізована форма — підкріплення або fallback
                # Для коротких назв (≤5 симв.) — потрібен word boundary, щоб
                # "Go" не матчилось у "Dragon Ball". Для довших — fuzzy ОК.
                if anime_bonus == 0 and anime_n and len(anime_n) >= 3:
                    needle = anime_n[:14]
                    if len(needle) <= 5:
                        if re.search(rf"\b{re.escape(needle)}\b", title_n):
                            anime_bonus = 1
                    else:
                        if needle in title_n or fuzz.partial_ratio(needle, title_n) >= 85:
                            anime_bonus = 1

                # ── Канал: trusted / reuploader / official ─────────────────
                # Reuploader (Crunchyroll, Muse Asia, Ani-One) — м'який штраф:
                # якщо це єдиний кандидат, він все одно перемагає, але справжній
                # японський оригінал його обходить.
                trusted = any(tc in channel_n for tc in TRUSTED_CHANNELS)
                reuploader = any(rc in channel_n for rc in REUPLOADER_CHANNELS)
                if reuploader:
                    off_bonus = -3
                elif trusted:
                    off_bonus = 3
                elif "official" in channel_n or "official" in title_n:
                    off_bonus = 1
                else:
                    off_bonus = 0

                raw_title    = full.get("title") or ""
                raw_title_l  = raw_title.lower()

                # ── Назва пісні в японських лапках 「曲名」 у заголовку ──────
                # Якщо в дужках стоїть пісня, відмінна від нашої — це ІНША
                # секвенція OP/ED того ж аніме (або взагалі чуже аніме).
                # АЛЕ: AnimeThemes завжди дає romaji ("Wakaba no Koro"), а
                # японські канали часто пишуть пісню kanji/kana («若葉のころ»).
                # У такому разі fuzzy-порівняння завжди ~0 — ми не маємо
                # права штрафувати, бо це може бути та ж пісня в іншому
                # скрипті. Скидаємо penalty якщо ВСІ дужки — JP-script.
                bracket_songs = SONG_BRACKETS_RE.findall(raw_title)
                song_brackets_present = bool(bracket_songs) and has_song
                song_brackets_match = song_brackets_present and any(
                    fuzz.partial_ratio(norm(bs), song_n) >= 75 for bs in bracket_songs
                )
                song_brackets_unverifiable = song_brackets_present and all(
                    _is_jp_script(bs) for bs in bracket_songs
                )
                song_mismatch_penalty = (
                    -4 if (song_brackets_present
                           and not song_brackets_match
                           and not song_brackets_unverifiable) else 0
                )

                # ── Тип теми (OP vs ED) у назві ─────────────────────────────
                # Якщо наша тема ED, а в назві стоїть オープニング/OP映像 — це
                # явно OP-кліп. Штрафуємо. (І навпаки.)
                opp_markers  = ED_TYPE_MARKERS if theme.theme_type == "OP" else OP_TYPE_MARKERS
                same_markers = OP_TYPE_MARKERS if theme.theme_type == "OP" else ED_TYPE_MARKERS
                opposite_present = any(m in raw_title_l for m in opp_markers)
                same_present     = any(m in raw_title_l for m in same_markers)
                type_mismatch_penalty = -5 if (opposite_present and not same_present) else 0

                # ── JP-патерн у назві (creditless / ノンクレジット / TVアニメ) ─
                # Цей патерн є на ВСІХ офіційних creditless-кліпах, тому сам
                # по собі він не підтверджує "це наше аніме". Бонус даємо лише
                # якщо назва нашого аніме теж у заголовку, І пісня (якщо
                # вказана в дужках) — наша. Інакше штрафуємо.
                jp_pattern_present = any(p in raw_title_l for p in JP_PATTERNS)
                if not jp_pattern_present:
                    jp_pattern_bonus = 0
                elif anime_bonus == 0:
                    jp_pattern_bonus = -5    # creditless ЧУЖОГО аніме
                elif (song_brackets_present and not song_brackets_match
                      and not song_brackets_unverifiable):
                    jp_pattern_bonus = -3    # creditless нашого аніме, ІНША пісня
                else:
                    jp_pattern_bonus = 3     # creditless нашого аніме, наша пісня

                # ── Свіжість завантаження ──────────────────────────────────
                # OP-кліп сезону завантажують ~у вікні самого сезону. Якщо
                # відео старше 2+ років за сезон — це майже завжди оригінал
                # пісні/чужий MV/реліз попередніх років, а не кліп OP.
                recency_bonus = 0
                if upload_date and theme.year and len(upload_date) >= 4:
                    try:
                        diff = theme.year - int(upload_date[:4])
                    except ValueError:
                        diff = 0
                    else:
                        if -1 <= diff <= 1:
                            recency_bonus = 2     # сезонне вікно
                        elif diff == 2:
                            recency_bonus = 0
                        else:
                            recency_bonus = -5    # >2 років — точно не той кліп

                total = (song_bonus + artist_bonus + dur_bonus
                         + clip_bonus + anime_bonus + off_bonus
                         + jp_pattern_bonus + recency_bonus
                         + song_mismatch_penalty + type_mismatch_penalty)

                # Якщо score низький — views не повинні витягувати кандидата
                # вгору (вірусний кавер не має обігнати тихий офіційний кліп).
                tiebreaker = views if total >= SCORE_FLOOR else 0
                score = (total, tiebreaker)

                if collect_debug:
                    debug_rows.append({
                        "id": full.get("id"),
                        "title": raw_title[:55],
                        "channel": (full.get("channel") or "")[:22],
                        "duration": duration,
                        "views": views,
                        "song": song_bonus,
                        "artist": artist_bonus,
                        "dur": dur_bonus,
                        "clip": clip_bonus,
                        "anime": anime_bonus,
                        "off": off_bonus,
                        "jp": jp_pattern_bonus,
                        "rec": recency_bonus,
                        "smis": song_mismatch_penalty,
                        "tmis": type_mismatch_penalty,
                        "total": total,
                    })

                if score > best_score:
                    best_score = score
                    best = full

        # Поріг якості: якщо найкращий кандидат не набрав мінімум — краще
        # лишити "не знайдено", ніж призначити випадковий чужий кліп.
        if best and best_score[0] >= SCORE_FLOOR:
            theme.yt_url   = f"https://youtube.com/watch?v={best.get('id', '')}"
            theme.yt_views = best.get("view_count")
            theme.yt_title = best.get("title")
        else:
            best = None  # для debug-таблиці: позначити, що ніхто не пройшов

        # Друкуємо таблицю якщо: 1) явно вказано це аніме через --debug;
        # 2) увімкнено --debug-missing і саме ця тема не знайшлась.
        if debug_active or (debug_missing and best is None):
            _print_debug_table(theme, debug_rows, best.get("id") if best else None)

    except Exception:
        pass

    time.sleep(delay)


def _print_debug_no_results(theme: Theme) -> None:
    """Друкує повідомлення для теми, де YouTube-пошук не повернув жодного відео.
    Показує які саме запити пробували — щоб видно було чому пусто.
    """
    queries = theme.search_queries
    if HAS_RICH:
        console.print(
            f"\n[yellow]🔧 debug · {theme.anime_name} · {theme.label}: "
            f"YouTube не повернув жодного відео на жоден запит[/yellow]"
        )
        console.print("[dim]Спробовані запити:[/dim]")
        for q, n in queries:
            console.print(f"  [dim]• ytsearch{n}: {q}[/dim]")
    else:
        print(f"\n[debug] {theme.anime_name} {theme.label}: YT нічого не повернув")
        for q, n in queries:
            print(f"  • ytsearch{n}: {q}")


def _print_debug_table(theme: Theme, rows: list[dict], chosen_id: Optional[str]) -> None:
    """Виводить таблицю кандидатів зі скорами для одної теми (--debug режим)."""
    if not rows:
        if HAS_RICH:
            console.print(f"\n[yellow]🔧 debug · {theme.anime_name} {theme.label}: кандидатів немає[/yellow]")
        else:
            print(f"\n[debug] {theme.anime_name} {theme.label}: кандидатів немає")
        return

    rows_sorted = sorted(rows, key=lambda r: r["total"], reverse=True)

    if HAS_RICH:
        t = Table(
            title=f"🔧 debug · {theme.anime_name} · {theme.label}",
            box=box.SIMPLE_HEAVY,
            header_style="bold dim",
            border_style="dim",
            show_lines=False,
        )
        t.add_column("✓", width=1)
        t.add_column("Канал", min_width=14, max_width=22, style="cyan")
        t.add_column("Назва", min_width=24, max_width=50)
        t.add_column("song", justify="right", width=4)
        t.add_column("art", justify="right", width=3)
        t.add_column("dur±", justify="right", width=4)
        t.add_column("clip", justify="right", width=4)
        t.add_column("ani", justify="right", width=3)
        t.add_column("off", justify="right", width=3)
        t.add_column("jp", justify="right", width=3)
        t.add_column("rec", justify="right", width=3)
        t.add_column("smis", justify="right", width=4)
        t.add_column("tmis", justify="right", width=4)
        t.add_column("Σ", justify="right", style="bold yellow", width=4)
        t.add_column("Перегл.", justify="right", style="dim", width=10)

        for r in rows_sorted:
            mark = "✓" if r["id"] == chosen_id else " "
            row_style = "bold green" if r["id"] == chosen_id else None
            t.add_row(
                mark,
                r["channel"], r["title"],
                str(r["song"]), str(r["artist"]), str(r["dur"]),
                str(r["clip"]), str(r["anime"]), str(r["off"]),
                str(r["jp"]), str(r["rec"]),
                str(r["smis"]), str(r["tmis"]),
                str(r["total"]),
                _fmt_views(r["views"]),
                style=row_style,
            )
        console.print(t)
    else:
        print(f"\n[debug] {theme.anime_name} {theme.label}")
        for r in rows_sorted:
            mark = "✓" if r["id"] == chosen_id else " "
            print(f"  {mark} total={r['total']:>3} song={r['song']} art={r['artist']} "
                  f"dur={r['dur']} clip={r['clip']} ani={r['anime']} off={r['off']} "
                  f"jp={r['jp']} rec={r['rec']} smis={r['smis']} tmis={r['tmis']} | "
                  f"{r['channel'][:20]} · {r['title'][:50]}")


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
    parser.add_argument("--debug",  type=str, default=None,
                        help="Друкувати таблицю кандидатів зі скорами для аніме, "
                             "чия назва містить цей рядок (case-insensitive). "
                             "Наприклад: --debug \"Kizoku Tensei\"")
    parser.add_argument("--debug-missing", action="store_true",
                        help="Друкувати таблицю кандидатів для всіх тем, "
                             "які не знайшлись на YouTube (best score < поріг "
                             "або нічого не повернулось). Корисно щоб зрозуміти, "
                             "ЧОМУ саме не знайшлось.")
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