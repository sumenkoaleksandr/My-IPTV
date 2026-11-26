import asyncio
import aiohttp
import requests
import re
import os
import sys
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

# Базовые самообновляемые источники (без украинских агрегаторов)
SEED_PLAYLISTS = [
    "https://iptv-org.github.io/iptv/languages/rus.m3u",
    "https://iptv-org.github.io/iptv/countries/ru.m3u",
    "https://ngrch.github.io/iptv/ru.m3u",
    "https://raw.githubusercontent.com/Adam-ZS/iptv-ru-ua/main/iptv.m3u",
    "https://raw.githubusercontent.com/slava-67/TV/IPTV/iptv.m3u",
    "https://myplaylists.github.io/iptv/ru.m3u",
    "https://myplaylists.github.io/iptv/sport.m3u",
    "https://myplaylists.github.io/iptv/cinema.m3u",
    "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_russia.m3u8",
    "https://raw.githubusercontent.com/Dimonchik003/iptv/master/iptv.m3u",
    "https://raw.githubusercontent.com/SmolNP/IPTVru/main/IPTVru.m3u",
    "https://raw.githubusercontent.com/ip-tv/iptv/master/ru.m3u",
]

EPG_URLS = "http://epg.it999.ru/epg.xml.gz,https://iptvx.one/epg/epg.xml.gz"
CHANNELS_DB_URL = "https://iptv-org.github.io/api/channels.json"
STREAMS_DB_URL = "https://iptv-org.github.io/api/streams.json"
FEEDS_DB_URL = "https://iptv-org.github.io/api/feeds.json"

# Параметры парсинга и проверки стабильности
DAYS_FRESH = 30                 # Искать репозитории на GitHub, обновленные за последние N дней
MAX_HTTP_CONCURRENT = 45        # Одновременные HTTP- и сегмент-проверки
HTTP_TIMEOUT = 8                # Таймаут HTTP-ответа (сек)
MAX_LATENCY_SEC = 4.5           # Максимально допустимая задержка ответа сервера (сек)
MIN_SPEED_KBPS = 500            # Минимальная скорость отдачи видеосегмента (Кбит/с)
FFPROBE_CONCURRENT = 15         # Одновременные проверки через ffprobe
FFPROBE_TIMEOUT = 10            # Таймаут анализа потока через ffprobe (сек)
MIN_HEIGHT = 360                # Минимальная высота видеокадра (720 — только HD и выше)
MAX_LATIN_CANDIDATES = 350      # Лимит латинских каналов без тега RU для проверки дорожки в ffprobe
MAX_MIRRORS_PER_CHANNEL = 3     # Сколько самых стабильных потоков оставлять на каждый канал (1 = без резерва)
MIN_CHANNELS = 15               # Защита: не перезаписывать файл, если рабочих потоков меньше
OUTPUT_FILE = "playlist.m3u"
CUSTOM_PLAYLIST_FILE = "custom_channels.m3u"

# Поиск сдвига по времени (+1 .. +9) в названии канала
TIMEZONE_SHIFT_PATTERN = re.compile(
    r"(?:[\(\[\s]|^)\+([1-9])(?:\s*(?:ч|h|msk|мск))?(?:[\)\]\s]|$)",
    re.IGNORECASE,
)

# Признаки привязки потока к конкретному IP-адресу, локальному прокси или одноразовой сессии
IP_LOCKED_URL_PATTERN = re.compile(
    r"(^https?://(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.0\.0\.1)|"
    r"/udpxy/|/udp/|wmsAuthSign=|client_ip=|remote_ip=|user_ip=|ip=\d+\.\d+\.\d+\.\d+)",
    re.IGNORECASE,
)

# Стоп-слова для жесткого отсева 18+
ADULT_BLOCK_PATTERN = re.compile(
    r"(18\+|xxx|adult|erotic|эротик|порно|porn|nsfw|brazzers|playboy|hustler|"
    r"penthouse|redlight|private|vivid|babes|nuart|dorcel|venus|sex|секс|клубничка|ночной)",
    re.IGNORECASE,
)

# Стоп-слова и домены с жесткой геоблокировкой
GEOBLOCK_TEXT_PATTERN = re.compile(
    r"(geo-?blocked|not\s*24/7|geoblock|region\s*blocked|запрещен\s*в\s*вашем\s*регионе)",
    re.IGNORECASE,
)
GEOBLOCK_DOMAIN_PATTERN = re.compile(
    r"(vgtrk\.com|smotrim\.ru|1tv\.ru/live|ntv\.ru/air|matchtv\.ru/on-air|"
    r"ctc\.ru/online|tnt-online\.ru|rutube\.ru/api/play|okko\.tv|wink\.ru/live|"
    r"kion\.ru|premier\.one|more\.tv|ivi\.ru|kinopoisk\.ru|megogo\.net)",
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

# Иностранные языковые и региональные маркеры
FOREIGN_LANG_MARKER_PATTERN = re.compile(
    r"(\b(cz|cze|czech|sk|svk|pl|pol|polish|de|ger|deu|german|fr|fra|french|es|spa|spanish|"
    r"it|ita|italian|tr|tur|turkish|ro|rou|romanian|bg|bul|hu|hun|rs|srp|hr|hrv|nl|nld|"
    r"pt|por|ar|ara|hi|hin|ko|kor|ja|jpn|zh|chi|uk|usa|latino|brasil)\b|"
    r"tvg-id=\"[^\"]*\.(cz|sk|pl|de|fr|es|it|tr|ro|bg|hu|uk|us)\")",
    re.IGNORECASE,
)

# Популярные международные бренды для приоритетной проверки русской звуковой дорожки
LATIN_BRAND_PRIORITY = re.compile(
    r"\b(discovery|animal\s*planet|national\s*geographic|nat\s*geo|eurosport|viasat|viju|"
    r"tv\s*1000|tv1000|amedia|nickelodeon|nick\s*jr|cartoon\s*network|boomerang|tiji|gulli|"
    r"disney|mtv|vh1|mezzo|mcm|bridge|europa\s*plus|setanta|ufc|rt|rbc|ctc|tnt|ntv|ren|mir|sts)\b",
    re.IGNORECASE,
)

# Распознавание радиопотоков
RADIO_PATTERN = re.compile(
    r"(radio=\"true\"|\b(радио|radio|fm|фм)\b|маяк|вести\s*фм|эхо\s*москвы|авторадио|"
    r"дорожное|рекорд|record|наше\s*радио|русское\s*радио|европа\s*плюс|dfm|дфм|"
    r"energy|энерджи|релакс|relax\s*fm|монте-карло|monte\s*carlo|серебряный\s*дождь|"
    r"бизнес\s*фм|business\s*fm|коммерсантъ\s*фм|детское\s*радио|юмор\s*fm|камеди\s*радио|"
    r"like\s*fm|лайк\s*фм|маруся\s*фм|казак\s*фм|милицейская\s*волна|радио\s*дача|радио\s*шансон)",
    re.IGNORECASE,
)

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
    "Радио": 9,
    "Общие": 10,
}

CATEGORY_CHANNEL_ORDER = {
    "Общероссийские": {
        "первыйканал": 1, "первый": 1, "россия1": 2, "матч": 3, "нтв": 4,
        "пятыйканал": 5, "5канал": 5, "россияк": 6, "россиякультура": 6, "культура": 6,
        "россия24": 7, "карусель": 8, "отр": 9, "общественноетелевидениероссии": 9,
        "твцентр": 10, "твц": 10, "рен": 11, "рентв": 11, "спас": 12,
        "стс": 13, "домашний": 14, "тв3": 15, "пятница": 16, "звезда": 17,
        "мир": 18, "тнт": 19, "муз": 20, "музтв": 20, "че": 21, "ю": 22,
        "суббота": 23, "солнце": 24, "известия": 25, "iz": 25, "рбк": 26,
        "2x2": 27, "2х2": 27, "победа": 28, "москва24": 29, "360": 30,
    },
    "Кино и сериалы": {
        "кинопремьера": 1, "кинохит": 2, "киномикс": 3, "киносемья": 4,
        "киносвидание": 5, "кинокомедия": 6, "киносерия": 7, "киноужас": 8,
        "мужскоекино": 9, "нашеновоекино": 10, "родноекино": 11, "нашелюбимоекино": 12,
        "домкино": 13, "домкинопремиум": 14, "мосфильмзолотаяколлекция": 15, "мосфильм": 15,
        "tv1000": 16, "тв1000": 16, "viju1000": 16, "tv1000action": 17, "тв1000экшн": 17,
        "tv1000русскоекино": 18, "тв1000русскоекино": 18, "amedia1": 19, "амедиа1": 19,
        "amedia2": 20, "амедиа2": 20, "amediahit": 21, "амедиахит": 21,
        "amediapremium": 22, "амедиапремиум": 22, "русскийроман": 23,
        "русскийбестселлер": 24, "русскийдетектив": 25, "русскийиллюзион": 26,
        "иллюзион": 27, "еврокино": 28, "нст": 29, "шокирующее": 30, "остросюжетное": 31,
    },
    "Познавательные": {
        "discoverychannel": 1, "discovery": 1, "дискавери": 1,
        "discoveryscience": 2, "дискаверисайенс": 2, "nationalgeographic": 3, "natgeo": 3,
        "natgeowild": 4, "animalplanet": 5, "энималплэнет": 5, "мояпланета": 6,
        "живаяпланета": 7, "наука": 8, "наука20": 8, "история": 9,
        "viasathistory": 10, "виасатхистори": 10, "viasatexplore": 11, "виасатэксплорер": 11,
        "viasatnature": 12, "виасатнейчер": 12, "365дней": 13, "ктоестькто": 14,
        "время": 15, "ностальгия": 16, "охотаирыбалка": 17, "диалогиорыбалке": 18,
        "дикий": 19, "дикаяохота": 20, "дикаярыбалка": 21, "вмиреживотных": 22,
        "зоотв": 23, "zoo": 23, "приключения": 24, "глазамитуриста": 25,
        "rtg": 26, "russiantravelguide": 26, "эврика": 27, "galaxy": 28, "гэлакси": 28,
        "техно24": 29, "т24": 29, "оружие": 30, "доктор": 31, "живи": 32,
        "еда": 33, "кухнятв": 34, "кухня": 34, "телекафе": 35,
        "бобер": 36, "бобёр": 36, "усадьба": 37, "загородный": 38,
    },
    "Спорт": {
        "матчпремьер": 1, "матчфутбол1": 2, "матчфутбол2": 3, "матчфутбол3": 4,
        "матчарена": 5, "матчигра": 6, "матчбоец": 7, "матчстрана": 8, "матчпланета": 9,
        "кхл": 10, "телеканалкхл": 10, "кхлprime": 11, "кхлпрайм": 11,
        "eurosport1": 12, "евроспорт1": 12, "евроспорт": 12, "eurosport2": 13, "евроспорт2": 13,
        "setantasports1": 14, "сетантаспорт1": 14, "setantasports2": 15, "сетантаспорт2": 15,
        "viasatsport": 16, "виасатспорт": 16, "футбол": 17, "бокс": 18, "бокств": 18,
        "ufc": 19, "удартв": 20, "удар": 20, "старт": 21, "старттриумф": 22,
        "спортивный": 23, "хоккейный": 24, "футбольный": 25, "драйв": 26,
        "автоплюс": 27, "конныймир": 28, "русскийэкстрим": 29,
    },
    "Детские": {
        "мульт": 1, "мультимузыка": 2, "мультиландия": 3, "союзмультфильм": 4,
        "карусель": 5, "о": 6, "телеканало": 6, "disney": 7, "дисней": 7,
        "nickelodeon": 8, "никелодеон": 8, "nickjr": 9, "никджуниор": 9, "nicktoons": 10,
        "cartoonnetwork": 11, "boomerang": 12, "бумеранг": 12, "tiji": 13, "тижи": 13,
        "gulli": 14, "детскиймир": 15, "детский": 16, "вгостяхусказки": 17,
        "рыжий": 18, "смайл": 19, "малыш": 20, "cartoonclassics": 21,
        "ani": 22, "ани": 22, "fan": 23, "фан": 23, "супергерои": 24,
        "типтоп": 25, "чижик": 26, "стслove": 27, "стслав": 27,
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
    match = TIMEZONE_SHIFT_PATTERN.search(raw_title)
    return int(match.group(1)) if match else 0


def normalize_channel_name(name, keep_shift=False):
    shift = extract_timezone_shift(name)
    clean = name.lower()
    clean = re.sub(r"[\(\[].*?[\)\]]", "", clean)
    clean = re.sub(r"\b(hd|fhd|uhd|4k|sd|hq|ru|rus|tv|тв|плюс|\+\d)\b", "", clean)
    clean = re.sub(r"[^a-zа-яё0-9]", "", clean).strip()

    if keep_shift and shift > 0 and clean:
        return f"{clean}_plus{shift}"
    return clean


def is_ip_independent_url(stream_url):
    return not bool(IP_LOCKED_URL_PATTERN.search(stream_url))


def load_iptv_org_databases():
    print("[*] Загрузка баз метаданных, языков и геоблокировок iptv-org...")
    metadata_map = {}
    geoblocked_urls = set()
    channel_languages = {}

    try:
        f_resp = requests.get(FEEDS_DB_URL, timeout=15)
        if f_resp.status_code == 200:
            for feed in f_resp.json():
                ch_id = feed.get("channel") or ""
                langs = [l.lower() for l in (feed.get("languages") or [])]
                if ch_id and langs:
                    channel_languages.setdefault(ch_id, set()).update(langs)
    except Exception as e:
        print(f"[!] Ошибка загрузки feeds.json: {e}")

    try:
        resp = requests.get(CHANNELS_DB_URL, timeout=15)
        if resp.status_code == 200:
            for ch in resp.json():
                ch_id = ch.get("id", "")
                logo = ch.get("logo", "")
                country = (ch.get("country") or "").upper()
                is_nsfw = bool(ch.get("is_nsfw", False))
                categories = ch.get("categories") or []
                langs = channel_languages.get(ch_id, set())

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
                            "languages": langs,
                        }
    except Exception as e:
        print(f"[!] Ошибка загрузки channels.json: {e}")

    try:
        s_resp = requests.get(STREAMS_DB_URL, timeout=15)
        if s_resp.status_code == 200:
            for st in s_resp.json():
                status = (st.get("status") or "").lower()
                title = (st.get("title") or "").lower()
                url = st.get("url") or ""
                if "blocked" in status or "geo" in status or "geo-blocked" in title:
                    if url:
                        geoblocked_urls.add(url.strip())
    except Exception as e:
        print(f"[!] Ошибка загрузки streams.json: {e}")

    print(f"[+] Загружено метаданных: {len(metadata_map)}, geo-blocked URL: {len(geoblocked_urls)}")
    return metadata_map, geoblocked_urls


def is_radio_stream(extinf_line, stream_url, meta):
    if "radio" in [c.lower() for c in meta.get("categories", [])]:
        return True
    if RADIO_PATTERN.search(extinf_line):
        return True
    if re.search(r"(/radio/|icecast|shoutcast|:8000/|\.mp3$|\.aac$)", stream_url, re.IGNORECASE):
        return True
    return False


def check_channel_eligibility(extinf_line, stream_url, meta, geoblocked_urls):
    title_part = extinf_line.split(",")[-1].strip()

    if not is_ip_independent_url(stream_url):
        return False
    if meta.get("is_nsfw"):
        return False
    if ADULT_BLOCK_PATTERN.search(extinf_line) or ADULT_BLOCK_PATTERN.search(stream_url):
        return False
    if stream_url.strip() in geoblocked_urls:
        return False
    if GEOBLOCK_TEXT_PATTERN.search(extinf_line) or GEOBLOCK_DOMAIN_PATTERN.search(stream_url):
        return False
    if meta.get("country") == "UA" or UKRAINIAN_LETTERS_PATTERN.search(extinf_line):
        return False
    if UKRAINIAN_TAG_PATTERN.search(extinf_line) or UKRAINIAN_TAG_PATTERN.search(stream_url):
        return False
    if UKRAINIAN_CHANNELS_PATTERN.search(title_part):
        return False
    if FOREIGN_LANG_MARKER_PATTERN.search(title_part) or FOREIGN_LANG_MARKER_PATTERN.search(stream_url):
        return False

    has_cyrillic = bool(re.search(r"[а-яА-ЯёЁ]", title_part))
    has_ru_tag = bool(re.search(r"\b(ru|rus|russia|россия|русский|рус)\b", extinf_line, re.IGNORECASE))
    is_ru_country = meta.get("country") == "RU"
    known_langs = meta.get("languages") or set()

    if has_cyrillic or has_ru_tag or is_ru_country or ("rus" in known_langs) or ("ru" in known_langs):
        return True

    return "probe_audio"


def detect_category(extinf_line, stream_url, meta):
    if is_radio_stream(extinf_line, stream_url, meta):
        return "Радио"

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
    header_part, _, channel_title = extinf_line.partition(",")
    channel_title = re.sub(r"\s*\[(Geo-blocked|Not 24/7)\]", "", channel_title, flags=re.IGNORECASE).strip()

    existing_attrs = dict(re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', header_part))
    existing_attrs["group-title"] = category

    if category == "Радио":
        existing_attrs["radio"] = "true"

    if not existing_attrs.get("tvg-id") and meta.get("tvg-id"):
        existing_attrs["tvg-id"] = meta["tvg-id"]
    if not existing_attrs.get("tvg-logo") and meta.get("tvg-logo"):
        existing_attrs["tvg-logo"] = meta["tvg-logo"]

    duration_match = re.match(r"^(#EXTINF:[^\s,]+)", header_part)
    duration_prefix = duration_match.group(1) if duration_match else "#EXTINF:-1"

    attrs_str = " ".join(f'{k}="{v}"' for k, v in existing_attrs.items() if v)
    return f"{duration_prefix} {attrs_str},{channel_title}"


def load_custom_channels(filepath, metadata_map):
    """Читает личный плейлист custom_channels.m3u без проверки с IP GitHub."""
    if not os.path.exists(filepath):
        print(f"[*] Локальный файл {filepath} не найден, пропуск.")
        return []

    print(f"[*] Загрузка пользовательских каналов из {filepath}...")
    custom_items = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        current_extinf = None
        for line in lines:
            if line.startswith("#EXTINF"):
                current_extinf = line
            elif line.startswith(("http://", "https://")) and current_extinf:
                raw_title = current_extinf.split(",")[-1].strip()
                base_norm = normalize_channel_name(raw_title, keep_shift=False)
                unique_norm = normalize_channel_name(raw_title, keep_shift=True)
                meta = metadata_map.get(base_norm, {})

                group_match = re.search(r'group-title="([^"]+)"', current_extinf, re.IGNORECASE)
                category = group_match.group(1).strip() if group_match else "Общероссийские"

                enriched_extinf = enrich_extinf(current_extinf, category, meta)
                custom_items.append((category, enriched_extinf, line, unique_norm, 1080, False, 999999))
                current_extinf = None
    except Exception as e:
        print(f"[!] Ошибка чтения {filepath}: {e}")

    print(f"[+] Загружено пользовательских каналов: {len(custom_items)}")
    return custom_items


def search_fresh_github_playlists(days=30):
    print(f"[*] Поиск свежих плейлистов на GitHub (за последние {days} дн.)...")
    date_since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
    queries = [
        f"iptv+rus+pushed:>{date_since}",
        f"iptv+russia+pushed:>{date_since}",
        f"iptv+m3u+pushed:>{date_since}",
    ]
    headers = {"Accept": "application/vnd.github.v3+json"}
    found_urls = set(SEED_PLAYLISTS)

    for q in queries:
        url = f"https://api.github.com/search/repositories?q={q}&sort=updated&order=desc&per_page=20"
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
            print(f"[!] Ошибка при поиске ({q}): {e}")

    print(f"[+] Найдено источников для проверки: {len(found_urls)}")
    return list(found_urls)


async def fetch_playlist_content(session, url, metadata_map, geoblocked_urls):
    channels = []
    try:
        async with session.get(url, timeout=12) as resp:
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

                        eligibility = check_channel_eligibility(current_extinf, line, meta, geoblocked_urls)
                        if eligibility:
                            category = detect_category(current_extinf, line, meta)
                            enriched_extinf = enrich_extinf(current_extinf, category, meta)
                            require_rus_audio = (eligibility == "probe_audio")
                            channels.append((category, enriched_extinf, line, unique_norm, require_rus_audio))
                        current_extinf = None
    except Exception:
        pass
    return channels


async def test_stream_stability(session, stream_url, is_radio=False, is_federal=False):
    start_time = time.monotonic()
    req_min_speed = 900 if is_federal else MIN_SPEED_KBPS
    req_max_latency = 2.5 if is_federal else MAX_LATENCY_SEC
    segments_to_test = 3 if is_federal else 1

    try:
        async with session.get(stream_url, timeout=HTTP_TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                return 0

            final_url = str(resp.url)
            if not is_ip_independent_url(final_url):
                return 0
            if re.search(r"(geoblock|stub|error|forbidden|deny|not_available)", final_url, re.IGNORECASE):
                return 0

            latency = time.monotonic() - start_time
            if latency > req_max_latency:
                return 0

            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw_bytes = await resp.content.read(65536)
            if not raw_bytes:
                return 0
            if re.search(b"(geoblock|forbidden|access denied)", raw_bytes, re.IGNORECASE):
                return 0

            if is_radio:
                return max(int(3000 - (latency * 300)), 1)

            if ".m3u8" in final_url.lower() or "mpegurl" in content_type:
                text = raw_bytes.decode("utf-8", errors="ignore")
                lines = [l.strip() for l in text.splitlines() if l.strip()]

                sub_playlists = []
                segments = []
                last_bw = 0
                for line in lines:
                    if line.startswith("#EXT-X-STREAM-INF"):
                        bw_match = re.search(r"BANDWIDTH=(\d+)", line)
                        last_bw = int(bw_match.group(1)) if bw_match else 0
                    elif not line.startswith("#"):
                        full_seg_url = urljoin(final_url, line)
                        if ".m3u8" in line.lower():
                            sub_playlists.append((last_bw, full_seg_url))
                        else:
                            segments.append(full_seg_url)

                if sub_playlists:
                    sub_playlists.sort(key=lambda x: x[0], reverse=True)
                    target_sub = sub_playlists[1][1] if (is_federal and len(sub_playlists) > 1) else sub_playlists[0][1]
                    async with session.get(target_sub, timeout=HTTP_TIMEOUT) as sub_resp:
                        if sub_resp.status != 200:
                            return 0
                        sub_text = await sub_resp.text(errors="ignore")
                        sub_base = str(sub_resp.url)
                        segments = [
                            urljoin(sub_base, l.strip())
                            for l in sub_text.splitlines()
                            if l.strip() and not l.strip().startswith("#")
                        ]

                if not segments:
                    return 0

                total_bytes = 0
                total_time = 0.0
                for seg_url in segments[:segments_to_test]:
                    seg_start = time.monotonic()
                    async with session.get(seg_url, timeout=HTTP_TIMEOUT) as seg_resp:
                        if seg_resp.status != 200:
                            return 0
                        seg_data = await seg_resp.content.read(524288)
                        seg_elapsed = max(time.monotonic() - seg_start, 0.01)
                        if len(seg_data) < 8192:
                            return 0
                        total_bytes += len(seg_data)
                        total_time += seg_elapsed

                speed_kbps = (total_bytes * 8 / 1000) / max(total_time, 0.01)
                if speed_kbps < req_min_speed:
                    return 0

                return max(int(speed_kbps - (latency * 300)), 1)

            elapsed = max(time.monotonic() - start_time, 0.01)
            speed_kbps = (len(raw_bytes) * 8 / 1000) / elapsed
            if speed_kbps < req_min_speed:
                return 0
            return max(int(speed_kbps - (latency * 300)), 1)

    except Exception:
        return 0


async def check_stream_alive(session, semaphore, category, extinf, stream_url, norm_name, require_rus_audio):
    async with semaphore:
        is_radio = (category == "Радио")
        is_federal = (category == "Общероссийские")
        score = await test_stream_stability(session, stream_url, is_radio=is_radio, is_federal=is_federal)
        if score > 0:
            return (category, extinf, stream_url, norm_name, require_rus_audio, score)
    return None


def classify_resolution(height):
    if height >= 2160:
        return "4K"
    elif height >= 1080:
        return "FHD"
    elif height >= 720:
        return "HD"
    return "SD"


async def probe_stream_quality(semaphore, category, extinf, stream_url, norm_name, require_rus_audio, stability_score):
    async with semaphore:
        is_radio = (category == "Радио")
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
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

            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

            has_rus_audio = False
            explicit_other_langs = set()

            for a_stream in audio_streams:
                tags = a_stream.get("tags") or {}
                lang = (tags.get("language") or "").strip().lower()
                track_title = (tags.get("title") or "").strip().lower()

                if lang in ("rus", "ru") or re.search(r"\b(rus|russian|рус|русский)\b", track_title):
                    has_rus_audio = True
                elif lang not in ("", "und", "unk", "mul"):
                    explicit_other_langs.add(lang)

            if require_rus_audio and not has_rus_audio:
                return None

            if not has_rus_audio and explicit_other_langs:
                return None

            if is_radio:
                if not audio_streams:
                    return None
                return (category, extinf, stream_url, norm_name, 1, False, stability_score)

            if not video_streams:
                return None

            fmt_duration = float(data.get("format", {}).get("duration") or 0)
            if 0 < fmt_duration < 15.0:
                return None

            height = int(video_streams[0].get("height", 0))
            if height < MIN_HEIGHT:
                return None

            quality_tag = classify_resolution(height)
            is_multi = (len(audio_streams) > 1 and has_rus_audio)

            header, _, title = extinf.partition(",")
            title = title.strip()
            title = re.sub(r"\s*\[(4K|FHD|HD|SD|Multi/RUS)\]", "", title, flags=re.IGNORECASE).strip()

            tags_to_add = [f"[{quality_tag}]"]
            if is_multi:
                tags_to_add.append("[Multi/RUS]")

            title = f"{title} {' '.join(tags_to_add)}"
            updated_extinf = f"{header},{title}"

            return (category, updated_extinf, stream_url, norm_name, height, is_multi, stability_score)

        except Exception:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            return None


async def main():
    metadata_map, geoblocked_urls = load_iptv_org_databases()
    playlist_urls = search_fresh_github_playlists(days=DAYS_FRESH)

    connector = aiohttp.TCPConnector(limit=MAX_HTTP_CONCURRENT, ssl=False)
    headers = {"User-Agent": "VLC/3.0.18 LibVLC/3.0.18"}

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        print("[*] Скачивание и первичный отбор каналов...")
        fetch_tasks = [fetch_playlist_content(session, url, metadata_map, geoblocked_urls) for url in playlist_urls]
        results = await asyncio.gather(*fetch_tasks)

        confirmed_urls = {}
        latin_candidates = []

        for channel_list in results:
            for category, extinf, stream_url, norm_name, req_audio in channel_list:
                if stream_url in confirmed_urls:
                    continue
                if not req_audio:
                    confirmed_urls[stream_url] = (category, extinf, norm_name, False)
                else:
                    latin_candidates.append((category, extinf, stream_url, norm_name, True))

        latin_candidates.sort(key=lambda x: 0 if LATIN_BRAND_PRIORITY.search(x[1]) else 1)
        for cat, ext, url, norm, req_audio in latin_candidates[:MAX_LATIN_CANDIDATES]:
            if url not in confirmed_urls:
                confirmed_urls[url] = (cat, ext, norm, req_audio)

        print(f"[+] Отобрано уникальных ссылок для проверки стабильности: {len(confirmed_urls)}")

        alive_streams = []
        if confirmed_urls:
            print("[*] Этап 1: Замер задержки, скорости сегментов и проверка IP-независимости...")
            http_semaphore = asyncio.Semaphore(MAX_HTTP_CONCURRENT)
            http_tasks = [
                check_stream_alive(session, http_semaphore, cat, ext, url, norm, req_audio)
                for url, (cat, ext, norm, req_audio) in confirmed_urls.items()
            ]
            for future in asyncio.as_completed(http_tasks):
                res = await future
                if res:
                    alive_streams.append(res)

        print(f"[+] Прошли тест скорости и стабильности: {len(alive_streams)} потоков.")

    print("[*] Этап 2: Анализ разрешения видео и звуковых дорожек через ffprobe...")
    probe_semaphore = asyncio.Semaphore(FFPROBE_CONCURRENT)
    probe_tasks = [
        probe_stream_quality(probe_semaphore, cat, ext, url, norm, req_audio, score)
        for cat, ext, url, norm, req_audio, score in alive_streams
    ]

    verified_channels = []
    for future in asyncio.as_completed(probe_tasks):
        res = await future
        if res:
            verified_channels.append(res)
            if len(verified_channels) % 25 == 0:
                print(f"    Подтверждено потоков: {len(verified_channels)}...")

    # Подключаем пользовательские ссылки из custom_channels.m3u с максимальным приоритетом
    custom_channels = load_custom_channels(CUSTOM_PLAYLIST_FILE, metadata_map)
    verified_channels.extend(custom_channels)

    grouped_by_channel = {}
    for item in verified_channels:
        cat, ext, url, norm, height, is_multi, score = item
        key = f"{cat}_{norm}" if norm else url
        grouped_by_channel.setdefault(key, []).append(item)

    final_channels = []
    for key, mirrors in grouped_by_channel.items():
        mirrors.sort(key=lambda x: (x[6], x[4], x[5]), reverse=True)
        limit = MAX_MIRRORS_PER_CHANNEL if mirrors[0][0] != "Радио" else 1
        for idx, m in enumerate(mirrors[:limit]):
            cat, ext, url, norm, height, is_multi, score = m
            if idx > 0:
                header, _, title = ext.partition(",")
                ext = f"{header},{title} (Резерв {idx + 1})"
            final_channels.append((cat, ext, url, norm, height, is_multi, score))

    if len(final_channels) < MIN_CHANNELS:
        print(f"[-] Слишком мало рабочих каналов ({len(final_channels)} < {MIN_CHANNELS}). Старый файл сохранен.")
        sys.exit(1)

    def channel_sort_key(item):
        cat, extinf, url, norm_name, height, is_multi, score = item
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
        for _, extinf, stream_url, _, _, _, _ in final_channels:
            f.write(f"{extinf}\n{stream_url}\n")

    os.replace(temp_file, OUTPUT_FILE)
    print(f"[+] Готово! В плейлист сохранено {len(final_channels)} стабильных потоков.")


if __name__ == "__main__":
    asyncio.run(main())
