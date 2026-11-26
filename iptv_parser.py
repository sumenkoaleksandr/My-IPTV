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
DAYS_FRESH = 14                 # Искать репозитории на GitHub, обновленные за последние N дней
MAX_HTTP_CONCURRENT = 45        # Одновременные HTTP- и сегмент-проверки
HTTP_TIMEOUT = 6                # Таймаут HTTP-ответа (сек)
MAX_LATENCY_SEC = 2.5           # Максимально допустимая задержка ответа сервера (сек)
MIN_SPEED_KBPS = 1200           # Минимальная скорость отдачи видеосегмента (Кбит/с)
FFPROBE_CONCURRENT = 15         # Одновременные проверки через ffprobe
FFPROBE_TIMEOUT = 9             # Таймаут анализа потока через ffprobe (сек)
MIN_HEIGHT = 480                # Минимальная высота видеокадра (720 — только HD и выше)
MAX_LATIN_CANDIDATES = 350      # Лимит латинских каналов без тега RU для проверки дорожки в ffprobe
MAX_MIRRORS_PER_CHANNEL = 2     # Сколько самых стабильных потоков оставлять на каждый канал (1 = без резерва)
MIN_CHANNELS = 30               # Защита: не перезаписывать файл, если рабочих потоков меньше
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
    "Радио": 9,
    "Общие": 10,
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
        r"(discovery|дискавери|animal|history|история|наука|national geographic|nat geo|природа|охота