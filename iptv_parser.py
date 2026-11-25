import asyncio
import aiohttp
import requests
import re
import os
import sys
import json
from datetime import datetime, timedelta

# Базовые самообновляемые источники (без украинских агрегаторов)
SEED_PLAYLISTS = [
    "https://iptv-org.github.io/iptv/languages/rus.m3u",
    "https://iptv-org.github.io/iptv/countries/ru.m3u",
    "https://raw.githubusercontent.com/slava-67/TV/IPTV/iptv.m3u",
    "https://myplaylists.github.io/iptv/ru.m3u",
    "https://myplaylists.github.io/iptv/sport.m3u",
    "https://myplaylists.github.io/iptv/cinema.m3u",
]

# Ссылки на EPG (телепрограмму) и справочник метаданных каналов
EPG_URLS = "http://epg.it999.ru/epg.xml.gz,https://iptvx.one/epg/epg.xml.gz"
CHANNELS_DB_URL = "https://iptv-org.github.io/api/channels.json"

# Параметры парсинга и проверки
DAYS_FRESH = 7              # Искать репозитории на GitHub, обновленные за последние N дней
MAX_HTTP_CONCURRENT = 50    # Одновременные HTTP-проверки
HTTP_TIMEOUT = 5            # Таймаут HTTP-ответа (сек)
FFPROBE_CONCURRENT = 15     # Одновременные проверки видеопотока через ffprobe
FFPROBE_TIMEOUT = 8         # Таймаут анализа потока через ffprobe (сек)
MIN_HEIGHT = 480            # Минимальная высота кадра (поставьте 720, чтобы отсеять SD)
DEDUPLICATE_BY_NAME = True  # Оставлять только одну лучшую версию каждого канала
MIN_CHANNELS = 30           # Защита: не перезаписывать файл, если рабочих каналов меньше
OUTPUT_FILE = "playlist.m3u"

# Поиск сдвига по времени (+1 .. +9) в названии канала
TIMEZONE_SHIFT_PATTERN = re.compile(
    r"(?:[\(\[\s]|^)\+([1-9])(?:\s*(?:ч|h|msk|мск))?(?:[\)\]\s]|$)",
    re.IGNORECASE,
)

# Стоп-слова для жесткого отсева 18+
ADULT_BLOCK_PATTERN = re.compile(
    r"(18\+|xxx|adult|erotic|эротик|порно|porn|nsfw|brazzers|playboy|hustler|"
    r"penthouse|redlight|private|vivid|babes|nuart|dorcel|venus|sex|секс|клубничка|ночной)",
    re.IGNORECASE,
)

# Стоп-слова и паттерны для отсева украинских каналов
UKRAINIAN_LETTERS_PATTERN = re.compile(r"[іїєґІЇЄҐ]")
UKRAINIAN_TAG_PATTERN = re.compile(
    r"(\b(ua|ukr|ukraine|украина|україна)\b|\.ua\b|tvg-id=\"[^\"]*\.ua\")",
    re.IGNORECASE,
)
UKRAINIAN_CHANNELS_PATTERN = re.compile(
    r"\b(1\+1|2\+2|интер|інтер|стб|ictv|новый канал|новий канал|тет|прямий|прямой|"
    r"еспресо|эспрессо|5 канал|уніан|униан|бігуді|бигуди|квартал|рада|суспільне|суспильне|"
    r"мега|піксель|пиксель|плюсплюс|плюс плюс|к1|к2|нтн|ентер-фільм|энтер-фильм|мій|твой|дом|дім|"
    r"freeдом|freedom|україна 24|украина 24|громадське|atr|м1|м2|music box ua|оце|индиго|індиго)\b",
    re.IGNORECASE,
)

# Порядок отображения групп в итоговом плейлисте
CATEGORY_ORDER = {
    "Общероссийские": 0,
    "Кино и сериалы": 1,
    "Развлекательные": 2,
    "Познавательные": 3,
    "Спорт": 4,
    "Детские": 5,
    "Новости": 6,
    "Музыка": 7,
    "Часовые пояса": 8,
    "Общие": 9,
}

# Ручной порядок главных каналов внутри категорий
CATEGORY_CHANNEL_ORDER = {
    "Общероссийские": {
        "первыйканал": 1, "первый": 1,
        "россия1": 2,
        "матч": 3,
        "нтв": 4,
        "пятыйканал": 5, "5канал": 5,
        "россияк": 6, "россиякультура": 6, "культура": 6,
        "россия24": 7,
        "карусель": 8,
        "отр": 9, "общественноетелевидениероссии": 9,
        "твцентр": 10, "твц": 10,
        "рен": 11, "рентв": 11,
        "спас": 12,
        "стс": 13,
        "домашний": 14,
        "тв3": 15,
        "пятница": 16,
        "звезда": 17,
        "мир": 18,
        "тнт": 19,
        "муз": 20, "музтв": 20,
        "че": 21,
        "ю": 22,
        "суббота": 23,
        "солнце": 24,
        "известия": 25, "iz": 25,
        "рбк": 26,
        "2x2": 27, "2х2": 27,
        "победа": 28,
        "москва24": 29,
        "360": 30,
    },
    "Кино и сериалы": {
        "кинопремьера": 1,
        "кинохит": 2,
        "киномикс": 3,
        "киносемья": 4,
        "киносвидание": 5,
        "кинокомедия": 6,
        "киносерия": 7,
        "киноужас": 8,
        "мужскоекино": 9,
        "нашеновоекино": 10,
        "родноекино": 11,
        "нашелюбимоекино": 12,
        "домкино": 13,
        "домкинопремиум": 14,
        "мосфильмзолотаяколлекция": 15, "мосфильм": 15,
        "tv1000": 16, "тв1000": 16, "viju1000": 16,
        "tv1000action": 17, "тв1000экшн": 17, "viju1000action": 17,
        "tv1000русскоекино": 18, "тв1000русскоекино": 18, "viju1000русское": 18,
        "amedia1": 19, "амедиа1": 19,
        "amedia2": 20, "амедиа2": 20,
        "amediahit": 21, "амедиахит": 21,
        "amediapremium": 22, "амедиапремиум": 22,
        "русскийроман": 23,
        "русскийбестселлер": 24,
        "русскийдетектив": 25,
        "русскийиллюзион": 26,
        "иллюзион": 27,
        "еврокино": 28,
        "нст": 29, "настоящеестрашноетелевидение": 29,
        "шокирующее": 30,
        "остросюжетное": 31,
    },
    "Познавательные": {
        "discoverychannel": 1, "discovery": 1, "дискавери": 1,
        "discoveryscience": 2, "дискаверисайенс": 2,
        "nationalgeographic": 3, "natgeo": 3,
        "natgeowild": 4,
        "animalplanet": 5, "энималплэнет": 5,
        "мояпланета": 6,
        "живаяпланета": 7,
        "наука": 8, "наука20": 8,
        "история": 9,
        "viasathistory": 10, "виасатхистори": 10, "vijuhistory": 10,
        "viasatexplore": 11, "виасатэксплорер": 11, "vijuexplore": 11,
        "viasatnature": 12, "виасатнейчер": 12, "vijunature": 12,
        "365дней": 13, "365днейтв": 13,
        "ктоестькто": 14,
        "время": 15,
        "ностальгия": 16,
        "охотаирыбалка": 17,
        "диалогиорыбалке": 18,
        "дикий": 19,
        "дикаяохота": 20,
        "дикаярыбалка": 21,
        "вмиреживотных": 22,
        "зоотв": 23, "zoo": 23,
        "приключения": 24,
        "глазамитуриста": 25,
        "rtg": 26, "russiantravelguide": 26,
        "эврика": 27,
        "galaxy": 28, "гэлакси": 28,
        "техно24": 29, "т24": 29,
        "оружие": 30,
        "доктор": 31,
        "живи": 32,
        "еда": 33, "едапремиум": 33,
        "кухнятв": 34, "кухня": 34,
        "телекафе": 35,
        "бобер": 36, "бобёр": 36,
        "усадьба": 37,
        "загородный": 38,
    },
    "Спорт": {
        "матчпремьер": 1,
        "матчфутбол1": 2,
        "матчфутбол2": 3,
        "матчфутбол3": 4,
        "матчарена": 5,
        "матчигра": 6,
        "матчбоец": 7,
        "матчстрана": 8,
        "матчпланета": 9,
        "кхл": 10, "телеканалкхл": 10,
        "кхлprime": 11, "кхлпрайм": 11,
        "eurosport1": 12, "евроспорт1": 12, "евроспорт": 12,
        "eurosport2": 13, "евроспорт2": 13,
        "setantasports1": 14, "сетантаспорт1": 14, "сетантаспорт": 14,
        "setantasports2": 15, "сетантаспорт2": 15,
        "viasatsport": 16, "виасатспорт": 16, "vijusport": 16,
        "футбол": 17, "телеканалфутбол": 17,
        "бокс": 18, "бокств": 18,
        "ufc": 19, "ufctv": 19,
        "удартв": 20, "удар": 20,
        "старт": 21, "твстарт": 21,
        "старттриумф": 22,
        "спортивный": 23,
        "хоккейный": 24,
        "футбольный": 25,
        "драйв": 26,
        "автоплюс": 27,
        "конныймир": 28,
        "русскийэкстрим": 29,
    },
    "Детские": {
        "мульт": 1,
        "мультимузыка": 2,
        "мультиландия": 3,
        "союзмультфильм": 4,
        "карусель": 5,
        "о": 6, "телеканало": 6,
        "disney": 7, "каналdisney": 7, "дисней": 7,
        "nickelodeon": 8, "никелодеон": 8,
        "nickjr": 9, "никджуниор": 9,
        "nicktoons": 10,
        "cartoonnetwork": 11,
        "boomerang": 12, "бумеранг": 12,
        "tiji": 13, "тижи": 13,
        "gulli": 14, "gulligirl": 14,
        "детскиймир": 15,
        "детский": 16,
        "вгостяхусказки": 17,
        "рыжий": 18,
        "смайл": 19, "смайлтв": 19,
        "малыш": 20, "малыштв": 20,
        "cartoonclassics": 21,
        "ani": 22, "ани": 22,
        "fan": 23, "фан": 23,
        "супергерои": 24,
        "типтоп": 25,
        "чижик": 26,
        "стслove": 27, "стслав": 27,
    },
}

IPTV_ORG_CATEGORY_MAP = {
    "movies": "Кино и сериалы",
    "series": "Кино и сериалы",
    "entertainment": "Развлекательные",
    "comedy": "Развлекательные",
    "lifestyle": "Развлекательные",
    "kids": "Детские",
    "animation": "Детские",
    "family": "Детские",
    "sports": "Спорт",
    "documentary": "Познавательные",
    "science": "Познавательные",
    "education": "Познавательные",
    "nature": "Познавательные",
    "travel": "Познавательные",
    "history": "Познавательные",
    "outdoor": "Познавательные",
    "music": "Музыка",
    "news": "Новости",
    "business": "Новости",
}

CATEGORY_RULES = [
    (
        "Детские",
        r"(детск|мульт|карусель|disney|дисней|nickelodeon|nick\b|малыш|cartoon|мультик|"
        r"солнце|tiji|gulli|\bо!|смайл|рыжий|в гостях у сказки|ландия|тип-топ|супергерои|kids|children|animation)",
    ),
    (
        "Кино и сериалы",
        r"(фильм|кино|cinema|movie|сериал|дом кино|тв-1000|tv1000|tv 1000|sci-fi|комедия|боевик|ужасы|"
        r"amedia|амедиа|paramount|fox|синема|иллюзион|мосфильм|победа|наше новое|родное|русский роман|"
        r"русский бестселлер|русский детектив|драма|триллер|фантастика|hollywood|голливуд|шокирующее|"
        r"остросюжетное|премьера|хит|блокбастер|любовь|семейное|viju|вижу|start|старт|kion|кион|premier|премьер|"
        r"dorama|дорама|индийское|bollywood|болливуд|series|films)",
    ),
    (
        "Спорт",
        r"(спорт|sport|матч|match|футбол|football|хоккей|khl|кхл|ufc|бокс|евроспорт|eurosport|"
        r"арена|arena|боец|драйв|авто плюс|auto|мотор|теннис|баскетбол|viasat sport|setanta|сетанта|старт триумф)",
    ),
    (
        "Познавательные",
        r"(discovery|дискавери|animal|history|история|наука|national geographic|nat geo|природа|охота|рыбалка|"
        r"путешествия|моя планета|живая планета|эврика|galaxy|гэлакси|365 дней|кто есть кто|время|ностальгия|"
        r"усадьба|загородный|бобер|бобёр|кухня|еда|телекафе|доктор|здоровье|живи|психология|оружие|техно 24|"
        r"т24|nanо|нано|ocean|океан|в мире животных|диалоги о рыбалке|дикий|zoo|зоо|documentary|science|travel)",
    ),
    (
        "Музыка",
        r"(муз|music|mtv|ru\.tv|ru tv|жара|шансон|bridge|бридж|europa plus|европа плюс|хит|тнт music|"
        r"ля-минор|ля минор|russian musicbox|music box|mcm|mezzo|классика|vh1|1hd music)",
    ),
    (
        "Новости",
        r"(новост|news|россия 24|рбк|rbc|мир 24|известия|рен тв|рен-тв|звезда|rt\b|russia today|"
        r"москва 24|360|соловьев|вместе-рф|дума тв|инфо|24)",
    ),
    (
        "Развлекательные",
        r"(стс|тнт|пятница|суббота|че\b|ю\b|тв-3|тв3|перец|2x2|2х2|квн|сарафан|аншлаг|юмор|"
        r"комеди|comedy|развлек|шоу|entertainment|калейдоскоп|точка тв|мир сериала|супер)",
    ),
]


def extract_timezone_shift(raw_title):
    """Возвращает номер часового сдвига (1..9) или 0, если это основной пояс."""
    match = TIMEZONE_SHIFT_PATTERN.search(raw_title)
    return int(match.group(1)) if match else 0


def normalize_channel_name(name, keep_shift=False):
    """Очищает название канала от тегов качества и региона."""
    shift = extract_timezone_shift(name)
    clean = name.lower()
    clean = re.sub(r"[\(\[].*?[\)\]]", "", clean)
    clean = re.sub(r"\b(hd|fhd|uhd|4k|sd|hq|ru|rus|tv|тв|плюс|\+\d)\b", "", clean)
    clean = re.sub(r"[^a-zа-яё0-9]", "", clean).strip()

    if keep_shift and shift > 0 and clean:
        return f"{clean}_plus{shift}"
    return clean


def load_channels_metadata():
    """Загружает базу логотипов, tvg-id, категорий, страны и флага is_nsfw из iptv-org."""
    print("[*] Загрузка справочника метаданных iptv-org...")
    metadata_map = {}
    try:
        resp = requests.get(CHANNELS_DB_URL, timeout=15)
        if resp.status_code == 200:
            for ch in resp.json():
                ch_id = ch.get("id", "")
                logo = ch.get("logo", "")
                country = (ch.get("country") or "").upper()
                is_nsfw = bool(ch.get("is_nsfw", False))
                categories = ch.get("categories") or []

                names_to_index = [ch.get("name", "")] + (ch.get("alt_names") or [])
                for raw_name in names_to_index:
                    norm = normalize_channel_name(raw_name, keep_shift=False)
                    if not norm:
                        continue
                    if norm not in metadata_map or (country == "RU" and metadata_map[norm]["country"] != "RU"):
                        metadata_map[norm] = {
                            "tvg-id": ch_id,
                            "tvg-logo": logo,
                            "country": country,
                            "is_nsfw": is_nsfw,
                            "categories": categories,
                        }
        print(f"[+] Загружено записей в справочник метаданных: {len(metadata_map)}")
    except Exception as e:
        print(f"[!] Не удалось загрузить базу метаданных: {e}")
    return metadata_map


def is_allowed_channel(extinf_line, stream_url, meta):
    """Проверяет, что канал русскоязычный, не 18+ и не украинский."""
    title_part = extinf_line.split(",")[-1].strip()

    if meta.get("is_nsfw"):
        return False
    if ADULT_BLOCK_PATTERN.search(extinf_line) or ADULT_BLOCK_PATTERN.search(stream_url):
        return False

    if meta.get("country") == "UA":
        return False
    if UKRAINIAN_LETTERS_PATTERN.search(extinf_line):
        return False
    if UKRAINIAN_TAG_PATTERN.search(extinf_line) or UKRAINIAN_TAG_PATTERN.search(stream_url):
        return False
    if UKRAINIAN_CHANNELS_PATTERN.search(title_part):
        return False

    has_cyrillic = bool(re.search(r"[а-яА-ЯёЁ]", title_part))
    has_ru_tag = bool(re.search(r"\b(ru|rus|russia|россия)\b", extinf_line, re.IGNORECASE))
    is_ru_country = meta.get("country") == "RU"

    return has_cyrillic or has_ru_tag or is_ru_country


def detect_category(extinf_line, meta):
    """Строгое определение категории: Часовые пояса -> Общероссийские -> iptv-org -> правила."""
    raw_title = extinf_line.split(",")[-1].strip()
    shift = extract_timezone_shift(raw_title)

    if shift > 0:
        return "Часовые пояса"

    base_norm_title = normalize_channel_name(raw_title, keep_shift=False)

    if base_norm_title in CATEGORY_CHANNEL_ORDER["Общероссийские"]:
        return "Общероссийские"

    for raw_cat in meta.get("categories", []):
        mapped = IPTV_ORG_CATEGORY_MAP.get(raw_cat.lower())
        if mapped:
            return mapped

    group_match = re.search(r'group-title="([^"]+)"', extinf_line, re.IGNORECASE)
    existing_group = group_match.group(1).strip().lower() if group_match else ""
    title_part = raw_title.lower()

    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, title_part, re.IGNORECASE) or re.search(pattern, existing_group, re.IGNORECASE):
            return category

    return "Общие"


def enrich_extinf(extinf_line, category, meta):
    """Формирует итоговую строку #EXTINF с категорией, логотипом и tvg-id."""
    header_part, _, channel_title = extinf_line.partition(",")
    channel_title = channel_title.strip()

    existing_attrs = dict(re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', header_part))
    existing_attrs["group-title"] = category

    if not existing_attrs.get("tvg-id") and meta.get("tvg-id"):
        existing_attrs["tvg-id"] = meta["tvg-id"]
    if not existing_attrs.get("tvg-logo") and meta.get("tvg-logo"):
        existing_attrs["tvg-logo"] = meta["tvg-logo"]

    duration_match = re.match(r"^(#EXTINF:[^\s,]+)", header_part)
    duration_prefix = duration_match.group(1) if duration_match else "#EXTINF:-1"

    attrs_str = " ".join(f'{k}="{v}"' for k, v in existing_attrs.items() if v)
    return f"{duration_prefix} {attrs_str},{channel_title}"


def search_fresh_github_playlists(days=7):
    """Ищет свежие плейлисты на GitHub через Search API."""
    print(f"[*] Поиск свежих плейлистов на GitHub (за последние {days} дн.)...")
    date_since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
    url = f"https://api.github.com/search/repositories?q=iptv+m3u+pushed:>{date_since}&sort=updated&order=desc&per_page=30"
    headers = {"Accept": "application/vnd.github.v3+json"}

    found_urls = set(SEED_PLAYLISTS)
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for repo in resp.json().get("items", []):
                full_name = repo["full_name"]
                default_branch = repo["default_branch"]
                tree_url = f"https://api.github.com/repos/{full_name}/git/trees/{default_branch}?recursive=1"
                tree_resp = requests.get(tree_url, headers=headers, timeout=10)
                if tree_resp.status_code == 200:
                    for file_info in tree_resp.json().get("tree", []):
                        path = file_info.get("path", "")
                        if path.endswith((".m3u", ".m3u8")):
                            found_urls.add(f"https://raw.githubusercontent.com/{full_name}/{default_branch}/{path}")
    except Exception as e:
        print(f"[!] Ошибка при поиске на GitHub: {e}")

    print(f"[+] Найдено источников для проверки: {len(found_urls)}")
    return list(found_urls)


async def fetch_playlist_content(session, url, metadata_map):
    """Скачивает плейлист, применяет строгие фильтры и классифицирует каналы."""
    channels = []
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text(errors="ignore")
                lines = [line.strip() for line in text.splitlines() if line.strip()]

                current_extinf = None
                for line in lines:
                    if line.startswith("#EXTINF"):
                        current_extinf = line
                    elif line.startswith("http") and current_extinf:
                        title_part = current_extinf.split(",")[-1].strip()
                        base_norm = normalize_channel_name(title_part, keep_shift=False)
                        unique_norm = normalize_channel_name(title_part, keep_shift=True)
                        meta = metadata_map.get(base_norm, {})

                        if is_allowed_channel(current_extinf, line, meta):
                            category = detect_category(current_extinf, meta)
                            enriched_extinf = enrich_extinf(current_extinf, category, meta)
                            channels.append((category, enriched_extinf, line, unique_norm))
                        current_extinf = None
    except Exception:
        pass
    return channels


async def check_stream_alive(session, semaphore, category, extinf, stream_url, norm_name):
    """Этап 1: Быстрая HTTP-проверка доступности потока."""
    async with semaphore:
        try:
            async with session.get(stream_url, timeout=HTTP_TIMEOUT, allow_redirects=True) as resp:
                if resp.status == 200:
                    chunk = await resp.content.read(128)
                    if chunk:
                        return (category, extinf, stream_url, norm_name)
        except Exception:
            pass
    return None


def classify_resolution(height):
    if height >= 2160:
        return "4K"
    elif height >= 1080:
        return "FHD"
    elif height >= 720:
        return "HD"
    return "SD"


async def probe_stream_quality(semaphore, category, extinf, stream_url, norm_name):
    """Этап 2: Проверка реального видеопотока и разрешения через ffprobe."""
    async with semaphore:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "v:0",
            "-user_agent", "VLC/3.0.18 LibVLC/3.0.18",
            "-rw_timeout", str(FFPROBE_TIMEOUT * 1_000_000),
            stream_url,
        ]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=FFPROBE_TIMEOUT + 2)

            if proc.returncode != 0 or not stdout:
                return None

            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            streams = data.get("streams", [])
            if not streams:
                return None

            height = int(streams[0].get("height", 0))
            if height < MIN_HEIGHT:
                return None

            quality_tag = classify_resolution(height)
            header, _, title = extinf.partition(",")
            title = title.strip()
            title = re.sub(r"\s*\[(4K|FHD|HD|SD)\]", "", title, flags=re.IGNORECASE).strip()
            title = f"{title} [{quality_tag}]"

            updated_extinf = f"{header},{title}"
            return (category, updated_extinf, stream_url, norm_name, height)

        except Exception:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            return None


async def main():
    metadata_map = load_channels_metadata()
    playlist_urls = search_fresh_github_playlists(days=DAYS_FRESH)

    connector = aiohttp.TCPConnector(limit=MAX_HTTP_CONCURRENT, ssl=False)
    headers = {"User-Agent": "VLC/3.0.18 LibVLC/3.0.18"}

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        print("[*] Скачивание и фильтрация плейлистов...")
        fetch_tasks = [fetch_playlist_content(session, url, metadata_map) for url in playlist_urls]
        results = await asyncio.gather(*fetch_tasks)

        unique_urls = {}
        for channel_list in results:
            for category, extinf, stream_url, norm_name in channel_list:
                if stream_url not in unique_urls:
                    unique_urls[stream_url] = (category, extinf, norm_name)

        print(f"[+] Отобрано уникальных ссылок после фильтрации: {len(unique_urls)}")
        if not unique_urls:
            print("[-] Каналы не найдены. Завершение.")
            sys.exit(1)

        print("[*] Этап 1: Проверка HTTP-доступности потоков...")
        http_semaphore = asyncio.Semaphore(MAX_HTTP_CONCURRENT)
        http_tasks = [
            check_stream_alive(session, http_semaphore, cat, ext, url, norm)
            for url, (cat, ext, norm) in unique_urls.items()
        ]

        alive_streams = []
        for future in asyncio.as_completed(http_tasks):
            res = await future
            if res:
                alive_streams.append(res)

        print(f"[+] Прошли HTTP-проверку: {len(alive_streams)} потоков.")

    print("[*] Этап 2: Анализ разрешения видео через ffprobe...")
    probe_semaphore = asyncio.Semaphore(FFPROBE_CONCURRENT)
    probe_tasks = [
        probe_stream_quality(probe_semaphore, cat, ext, url, norm)
        for cat, ext, url, norm in alive_streams
    ]

    verified_channels = []
    for future in asyncio.as_completed(probe_tasks):
        res = await future
        if res:
            verified_channels.append(res)
            if len(verified_channels) % 25 == 0:
                print(f"    Подтверждено видеопотоков: {len(verified_channels)}...")

    if DEDUPLICATE_BY_NAME:
        best_channels = {}
        for item in verified_channels:
            cat, ext, url, norm, height = item
            key = norm if norm else url
            if key not in best_channels or height > best_channels[key][4]:
                best_channels[key] = item
        final_channels = list(best_channels.values())
    else:
        final_channels = verified_channels

    if len(final_channels) < MIN_CHANNELS:
        print(f"[-] Слишком мало рабочих каналов ({len(final_channels)} < {MIN_CHANNELS}). Старый файл сохранен.")
        sys.exit(1)

    def channel_sort_key(item):
        cat, extinf, url, norm_name, height = item
        cat_rank = CATEGORY_ORDER.get(cat, 99)
        raw_title = extinf.split(",")[-1].strip()
        base_name = normalize_channel_name(raw_title, keep_shift=False)
        shift = extract_timezone_shift(raw_title)

        if cat == "Часовые пояса":
            channel_rank = CATEGORY_CHANNEL_ORDER["Общероссийские"].get(base_name, 999)
            return (cat_rank, channel_rank, shift, raw_title.lower())

        cat_order_map = CATEGORY_CHANNEL_ORDER.get(cat, {})
        channel_rank = cat_order_map.get(base_name, 999)
        return (cat_rank, channel_rank, 0, raw_title.lower())

    final_channels.sort(key=channel_sort_key)

    temp_file = f"{OUTPUT_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{EPG_URLS}" x-tvg-url="{EPG_URLS}"\n')
        for _, extinf, stream_url, _, _ in final_channels:
            f.write(f"{extinf}\n{stream_url}\n")

    os.replace(temp_file, OUTPUT_FILE)
    print(f"[+] Готово! В плейлист сохранено {len(final_channels)} каналов.")


if __name__ == "__main__":
    asyncio.run(main())
