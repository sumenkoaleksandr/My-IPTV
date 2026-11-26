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
MAX_MIRRORS_PER_CHANNEL = 2     # Сколько самых стабильных потоков оставлять на каждый канал (1 = без резерва)
MIN_CHANNELS = 15               # Защита: не перезаписывать файл, если рабочих потоков меньше
OUTPUT_FILE = "playlist.m3u"

# Поиск сдвига по времени (+1 .. +9) в названии канала
TIMEZONE_SHIFT_PATTERN = re.compile(
    r"(?:[\(\[\s]\vert{}^)\+([1-9])(?:\s*(?:ч\vert{}h\vert{}msk\vert{}мск))?(?:[\)\]\s]|$)",
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
    "Ра