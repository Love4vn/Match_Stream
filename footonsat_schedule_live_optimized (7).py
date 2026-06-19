"""
footonsat_schedule_live_optimized.py - ULTRA OPTIMIZED VERSION
- Gộp các trận bóng đá trùng lặp (cùng giờ, cùng cặp đội bất kể thứ tự).
- Giữ tất cả kênh M3U khớp (có URL khác nhau) cho mỗi yêu cầu từ lịch trận.
- Chống trùng link:
  + Bóng đá: trong cùng một trận không trùng URL; các trận khác nhau được phép trùng.
  + Tennis: coi toàn bộ tennis là một trận, không trùng URL.
- Khớp chính xác số kênh hai chiều, phân biệt vị trí 4K.
- Country code nghiêm ngặt: nếu target có code, M3U bắt buộc cùng code.
- Tiền xử lý tên kênh từ JSON: MENA→Arabia (trừ khi có English), Arena Sport SRB→Serbia,...
- Tennis: beIN Sports → beIN Sports 7.
- Bỏ qua kênh quảng cáo.
- Sắp xếp kênh: Hub Sports trước, sau đó Now Sports, UK, English, còn lại.
- Xử lý tiền tố "NOWTV" (có dấu |) trong tên kênh M3U.
"""
import asyncio
import json
import re
import unicodedata
import urllib.request
import urllib.error
import time
import sys
import os
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import List, Dict, Optional, Tuple, Set

# ================== CONFIG ==================
TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
M3U_LIST_FILE = "M3U_list.txt"
LIVE_M3U = "live_schedule_Optimize.m3u"
CACHE_FILE = ".m3u_cache.json"
CACHE_EXPIRY = 3600

VALIDATION_CONCURRENT = 50
VALIDATION_TIMEOUT = 2
USER_AGENT = "Mozilla/5.0"
SKIP_VALIDATION = "--skip-validation" in sys.argv
M3U_FETCH_WORKERS = 40

# === TẮT NGUỒN FOOTONSAT ===
ENABLE_FOOTONSAT = False   # Đặt True để bật lại

# ================== PRE-COMPILED PATTERNS ==================
PATTERN_COUNTRY_CODE_PREFIX = [
    re.compile(r'^\|\s*([a-z]{2,3})\s*\|\s*', re.I),
    re.compile(r'^([a-z]{2,3})\:\s*', re.I),
    re.compile(r'^([a-z]{2,3})\s*-\s*', re.I),
    re.compile(r'^([a-z]{2,3})\|\s*', re.I),
    re.compile(r'^\[([a-z]{2,3})\]\s*', re.I),
    re.compile(r'^\(([a-z]{2,3})\)\s*', re.I),
    re.compile(r'^' + re.escape('┃') + r'([a-z]{2,3})' + re.escape('┃') + r'\s*', re.I),
    re.compile(r'^([a-z]{2,3})\s+', re.I),
]
PATTERN_COUNTRY_CODE_SUFFIX = re.compile(r'\s+([a-z]{2,3})$', re.I)
PATTERN_QUALITY = re.compile(
    r'\b(hd|uhd|8k|4k|fhd|sd|channel|fibra|stream|online|vip|ppv|hevc|full hd|ultra hd|raw|3840p|30fps|60fps|50fps|ᴴᴰ|ᵁᴴᴰ|⁵⁰ᶠᵖˢ|⁶⁰ᶠᵖˢ|³⁸⁴⁰ᴾ|◉|hdr)\b',
    re.I
)
PATTERN_LOW_RES = re.compile(r'(sd|360p|480p|576p|low res|low quality)', re.I)
PATTERN_SPECIAL_TAGS = re.compile(r'[ⱽᴵᴾᴿᴬᵂʰᵉᵛᶜᵗᵛᴴᴰᵁᴴᴰ³⁸⁴⁰ᴾ⁵⁰ᶠᵖˢ◉┃]')
PATTERN_AD_CHANNEL = re.compile(r'[#=☰]')
PATTERN_GENERIC_PREFIX = re.compile(r'^([a-z]{2,3})\:\s*', re.I)
PATTERN_SPORTS_PREFIX = re.compile(r'^SPORTS\s*[-:]\s*', re.I)
PATTERN_NOW_HK_PREFIX = re.compile(r'^NOW\s+HK\s+', re.I)
PATTERN_NOWTV_PREFIX = re.compile(r'^NOWTV[\|\s]*', re.I)   # NEW

# ================== CONSTANTS ==================
ALLOWED_FOOTBALL_LEAGUES = {
    "Premier League", "Serie A", "La Liga", "Bundesliga", "Ligue 1",
    "UEFA Champions League", "UEFA Europa League", "UEFA Europa Conference League",
    "UEFA Euro", "FA Cup", "League Cup",
    "International Friendly",            # Đã thêm
    "FIFA World Cup"                     # THÊM DÒNG NÀY
}

COUNTRY_CODES: Set[str] = {
    "uk", "us", "fr", "de", "it", "es", "pt", "nl", "be", "ch", "at", "ba",
    "se", "no", "dk", "fi", "pl", "cz", "hu", "ro", "bg", "gr", "tr", "al", "in", "sg", "th", "id", "my",
    "il", "au", "ca", "nz", "ie", "gb", "en", "vn", "kr", "jp", "cn", "arg", "irl",
    "br", "ar", "mx", "za", "ru", "ua", "rs", "hr", "si", "sk", "am", "hk",
    "ukr", "lt"
}

COUNTRY_NAME_TO_CODE = {
    "united states": "us", "usa": "us", "uk": "uk", "united kingdom": "uk",
    "viet nam": "vn", "vietnam": "vn", "korea": "kr", "south korea": "kr",
    "japan": "jp", "china": "cn", "brazil": "br", "argentina": "arg", "mexico": "mx",
    "india": "in", "south africa": "za", "russia": "ru", "ukraine": "ukr",
    "serbia": "rs", "srbija": "rs", "croatia": "hr", "hrvatska": "hr",
    "slovenia": "si", "slovenija": "si", "slo": "si", "slovakia": "sk", "arabia": "ar",
    "bosnia and herzegovina": "ba", "bih": "ba", "roireland": "irl",
    "france": "fr", "french": "fr", "germany": "de", "deutsch": "de", "deutschland": "de",
    "italy": "it", "italia": "it", "spain": "es", "espana": "es", "portugal": "pt",
    "netherlands": "nl", "nederland": "nl", "belgium": "be", "belgie": "be",
    "switzerland": "ch", "austria": "at", "österreich": "at",
    "sweden": "se", "sverige": "se", "norway": "no", "norge": "no",
    "denmark": "dk", "dansk": "dk", "danmark": "dk", "finland": "fi", "suomi": "fi",
    "poland": "pl", "polska": "pl", "czech": "cz", "czech republic": "cz", "czechia": "cz",
    "hungary": "hu", "romania": "ro", "bulgaria": "bg", "greece": "gr", "hellas": "gr",
    "turkey": "tr", "türkiye": "tr", "israel": "il", "australia": "au",
    "canada": "ca", "new zealand": "nz", "ireland": "ie",
    "indonesia": "id", "malaysia": "my", "singapore": "sg", "thailand": "th",
    "egypt": "eg", "morocco": "ma", "algeria": "dz", "tunisia": "tn", "libya": "ly",
    "sudan": "sd", "ethiopia": "et", "kenya": "ke", "nigeria": "ng", "ghana": "gh",
    "senegal": "sn", "côte d'ivoire": "ci", "cameroon": "cm", "angola": "ao",
    "albania": "al", "great britain": "gb", "england": "gb", "scotland": "gb", "wales": "gb",
    "chile": "cl", "suriname": "sr", "armenia": "am", "georgia": "ge", "azerbaijan": "az", "kazakhstan": "kz",
    "hong kong": "hk", "lithuania": "lt", "eurasia": "lt"
}

LEAGUE_MAPPING = {
    "english premier league": "Premier League",
    "serie a": "Serie A",
    "la liga": "La Liga",
    "bundesliga": "Bundesliga",
    "ligue 1": "Ligue 1",
    "uefa champions league": "UEFA Champions League",
    "uefa europa league": "UEFA Europa League",
    "uefa conference league": "UEFA Europa Conference League",
}

PREMIER_LEAGUE_TEAMS = {
    "arsenal", "aston villa", "bournemouth", "brentford", "brighton", "chelsea",
    "crystal palace", "everton", "fulham", "leeds united", "liverpool", "manchester city",
    "manchester united", "newcastle", "nottingham forest", "sunderland", "tottenham hotspur",
    "west ham united", "wolverhampton"
}

ALLOWED_TEAMS_PER_LEAGUE = {
    "Premier League": PREMIER_LEAGUE_TEAMS,
    "Serie A": {"inter milan", "ac milan", "napoli", "juventus", "roma", "atalanta", "lazio"},
    "La Liga": {"barcelona", "real madrid", "atletico madrid"},
    "Bundesliga": {"bayern munich", "borussia dortmund", "bayer leverkusen"},
    "Ligue 1": {"psg", "paris saint-germain", "olympique marseille", "marseille"},
}

FOOTONSAT_URLS = [
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/premierleague.json",
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/seriea.json",
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/laliga.json",
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/bundesliga.json",
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/ligue1.json",
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/championsleague.json",
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/europaleague.json",
    "https://raw.githubusercontent.com/fairbird/footonsat-api/refs/heads/main/ConferenceLeague.json",
]

LOVE4VN_URL = "https://raw.githubusercontent.com/Love4vn/Live-Schedue/refs/heads/1/schedule.json"

# ================== GROUP TITLES ==================
LEAGUE_GROUP_NAME = {
    "Premier League": "⚽️🏴󠁧󠁢󠁥󠁮󠁧󠁿|Live Premier League",
    "Serie A": "⚽️🇮🇹|Live Serie A",
    "Bundesliga": "⚽️🇩🇪|Live Bundesliga",
    "La Liga": "⚽️🇪🇦|Live La Liga",
    "Ligue 1": "⚽️🇨🇵|Live Ligue 1",
    "UEFA Champions League": "Live UEFA Champions League",
    "UEFA Europa League": "Live UEFA Europa League",
    "UEFA Europa Conference League": "Live UEFA Conference League",
    "UEFA Euro": "Live Euro",
    "FA Cup": "Live FA, League Cup",
    "League Cup": "Live FA, League Cup",
    "Tennis": "🎾|Live Tennis",
    "FIFA World Cup": "Live Fifa World Cup",
    "International Friendly": "Live International Friendly"
}

# ================== CACHE ==================
class CacheManager:
    @staticmethod
    def get_cache() -> Optional[List[Dict]]:
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if time.time() - data.get('timestamp', 0) < CACHE_EXPIRY:
                        return data.get('channels', [])
        except:
            pass
        return None

    @staticmethod
    def save_cache(channels: List[Dict]):
        try:
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'timestamp': time.time(),
                    'channels': channels
                }, f)
        except:
            pass

# ================== HELPERS ==================
@lru_cache(maxsize=10000)
def normalize(s: str) -> str:
    s_lower = s.lower()
    s_nfd = unicodedata.normalize("NFD", s_lower)
    return "".join(c for c in s_nfd if unicodedata.category(c) != "Mn")

def vn_time(timestamp: int) -> str:
    dt = datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC")).astimezone(TIMEZONE)
    return dt.strftime("%d/%m %I:%M %p")

@lru_cache(maxsize=5000)
def similar(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > max(len_a, len_b) * 0.3:
        return 0.0

    dp = list(range(len_b + 1))
    for i in range(1, len_a + 1):
        new_dp = [i]
        for j in range(1, len_b + 1):
            cost = 0 if a[i-1] == b[j-1] else 1
            new_dp.append(min(dp[j] + 1, new_dp[j-1] + 1, dp[j-1] + cost))
        dp = new_dp

    return 1 - (dp[-1] / max(len_a, len_b))

def extract_country_code_from_name(name: str) -> Tuple[Optional[str], Optional[str]]:
    name_lower = name.lower()
    for country_name, code in COUNTRY_NAME_TO_CODE.items():
        if country_name in name_lower:
            return code, country_name
    return None, None

def extract_prefix_and_name(name: str) -> Tuple[Optional[str], str]:
    """
    Extract country code from beginning, end, or inside the channel name.
    Returns (country_code, cleaned_name_without_code).
    Also removes generic prefixes like "GO:", "VIP:", "NOW HK", "NOWTV|", etc.
    """
    name_lower = name.lower().strip()
    
    # 1. Check prefix patterns that include country codes
    for pat in PATTERN_COUNTRY_CODE_PREFIX:
        m = pat.match(name_lower)
        if m:
            code = m.group(1)
            if code in COUNTRY_CODES:
                remaining = name_lower[m.end():].lstrip('|:-\\s ┃')
                # Sau khi lấy country code, loại bỏ NOWTV nếu có
                remaining = PATTERN_NOWTV_PREFIX.sub('', remaining).strip()
                return code, remaining.strip()
    
    # 2. Check suffix pattern
    m_suffix = PATTERN_COUNTRY_CODE_SUFFIX.search(name_lower)
    if m_suffix:
        code = m_suffix.group(1)
        if code in COUNTRY_CODES:
            remaining = name_lower[:m_suffix.start()].strip()
            return code, remaining
        mapped = COUNTRY_NAME_TO_CODE.get(code, None)
        if mapped:
            remaining = name_lower[:m_suffix.start()].strip()
            return mapped, remaining
    
    # 3. Try to extract country name and map to code, then remove the country name
    code_from_name, country_name = extract_country_code_from_name(name_lower)
    if code_from_name and country_name:
        pattern = re.compile(r'\b' + re.escape(country_name) + r'\b', re.I)
        cleaned = pattern.sub('', name_lower).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        return code_from_name, cleaned
    
    # 4. Remove generic prefixes like "GO:", "VIP:", "NOW HK"
    m_generic = PATTERN_GENERIC_PREFIX.match(name_lower)
    if m_generic:
        remaining = name_lower[m_generic.end():].strip()
        return None, remaining
    
    m_now_hk = PATTERN_NOW_HK_PREFIX.match(name_lower)
    if m_now_hk:
        remaining = name_lower[m_now_hk.end():].strip()
        return None, remaining

    # 5. Remove NOWTV prefix (có thể không có country code)
    name_lower = PATTERN_NOWTV_PREFIX.sub('', name_lower).strip()
    
    # 6. Remove leading punctuation/spaces if nothing else matched
    cleaned = re.sub(r'^[\|\s\:\-┃]+', '', name_lower)
    return None, cleaned.strip()

def extract_channel_info(name: str) -> Tuple[Optional[str], bool]:
    _, clean = extract_prefix_and_name(name)
    name_lower = clean.lower()
    
    match_4k_before = re.search(r'4k\s*[-:\s]?\s*(\d+)', name_lower)
    if match_4k_before:
        return match_4k_before.group(1), True

    name_clean = re.sub(r'\b(?:hd|fhd|uhd|4k|8k|hevc|sd|full hd|ultra hd|hdr|raw)\b', '', name_lower, flags=re.I)
    name_clean = re.sub(r'[^\w\s]', ' ', name_clean)
    name_clean = ' '.join(name_clean.split())
    tokens = name_clean.split()
    if tokens:
        last_token = tokens[-1]
        match = re.search(r'(\d+)$', last_token)
        if match:
            return match.group(1), False
        if last_token.isdigit():
            return last_token, False
    for token in reversed(tokens):
        if token.isdigit():
            return token, False
    return None, False

def normalize_channel_name(name: str) -> str:
    _, name = extract_prefix_and_name(name)
    name = PATTERN_SPORTS_PREFIX.sub('', name)
    name = PATTERN_SPECIAL_TAGS.sub(' ', name)
    name = PATTERN_QUALITY.sub('', name)
    name = name.replace('plus', '+').replace(' and ', ' & ')
    name = re.sub(r'[^\w\s]', ' ', name)
    name = ' '.join(name.split())
    name = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('ascii')
    return name.strip()

def is_low_resolution(name: str) -> bool:
    return bool(PATTERN_LOW_RES.search(name))

def is_channel_match(ch_name: str, m3u_name: str, league: str = None) -> bool:
    if not ch_name or not m3u_name:
        return False

    ch_code, ch_clean = extract_prefix_and_name(ch_name)
    m3u_code, m3u_clean = extract_prefix_and_name(m3u_name)

    ch_num, ch_4k_before = extract_channel_info(ch_clean)
    m3u_num, m3u_4k_before = extract_channel_info(m3u_clean)

    if (ch_num is None) != (m3u_num is None):
        return False
    if ch_num is not None:
        if ch_num != m3u_num:
            return False
        if ch_4k_before != m3u_4k_before:
            return False

    if ch_code is not None:
        if m3u_code is None or ch_code != m3u_code:
            return False

    ch_norm = normalize_channel_name(ch_clean)
    m3u_norm = normalize_channel_name(m3u_clean)

    if ch_norm == m3u_norm:
        return True
    if ch_norm.replace(' ', '') == m3u_norm.replace(' ', ''):
        return True

    # Xử lý đặc biệt cho Now Sports Premier League: cho phép khớp với NOW SPORTS 4K X (4K trước số)
    if 'now sports premier league' in ch_norm:
        if 'now sports 4k' in m3u_norm and ch_num is not None and m3u_num == ch_num and m3u_4k_before:
            return True
        if 'now sports premier league' in m3u_norm:
            return True

    if 'now sports' in ch_norm:
        if 'now hk now sports' in m3u_norm:
            m3u_rest = m3u_norm.replace('now hk now sports', '').strip()
            ch_rest = ch_norm.replace('now sports', '').strip()
            if ch_rest == m3u_rest:
                return True

    if 'now sports premier league tv' in ch_norm:
        if 'now sports epl' in m3u_norm or 'now sports premier league' in m3u_norm:
            return True

    if len(ch_norm) <= 3 or len(m3u_norm) <= 3:
        return ch_norm == m3u_norm

    threshold = 0.85 if league == "Tennis" else 0.91
    return similar(ch_norm, m3u_norm) >= threshold

def is_football_allowed(league: str, match_name: str) -> bool:
    if league not in ALLOWED_FOOTBALL_LEAGUES:
        return False
    if league in ALLOWED_TEAMS_PER_LEAGUE:
        allowed_teams = ALLOWED_TEAMS_PER_LEAGUE[league]
        match_lower = match_name.lower()
        return any(team in match_lower for team in allowed_teams)
    return True

def preprocess_target_channel(name: str) -> str:
    if not name:
        return name
    name_clean = name.strip()
    
    name_clean = re.sub(r'\bART Motion Sport\b', 'ART Sport', name_clean, flags=re.I)
    name_clean = re.sub(r'M\+\s*Liga\s+de\s+Campeones', 'M+ LaLiga de Campeones', name_clean, flags=re.I)
    name_clean = re.sub(r'\bPrima Sport RO\b', 'Prima Sport Romania', name_clean, flags=re.I)
    name_clean = re.sub(r'SportKlub', 'Sport Klub', name_clean, flags=re.I)
    name_clean = re.sub(r'\s+SLO$', ' Slovenia', name_clean, flags=re.I)
    name_clean = re.sub(r'\bSlovenija\b', 'Slovenia', name_clean, flags=re.I)
    name_clean = re.sub(r'\b(Arena Sport \d+) SRB\b', r'\1 Serbia', name_clean, flags=re.I)
    name_clean = re.sub(r'\bPremier Sports ROI\b', 'Premier Sports RoIreland', name_clean, flags=re.I)

    if not re.search(r'\benglish\b', name_clean, re.I):
        name_clean = re.sub(r'\b(beIN\s*Sports?)\s+MENA\b', r'\1 Arabia', name_clean, flags=re.I)

    name_clean = re.sub(r'\bSky Sport Premier League DE\b', 'Sky Sport Premier League deutsch', name_clean, flags=re.I)
    
    return name_clean

# ================== HTTP ==================
async def fetch_json_async(url: str) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_json_sync, url)

def fetch_json_sync(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))
    except:
        return None

def fetch_text_sync(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode('utf-8', errors='ignore')
    except:
        return None

# ================== M3U PARSER ==================
def parse_m3u_fast(content: str) -> List[Dict]:
    channels = []
    lines = content.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('#EXTINF'):
            if '###' in line:
                i += 1
                continue

            params = {}
            for k, v in re.findall(r'([a-zA-Z-]+)="([^"]*)"', line):
                params[k.lower()] = v

            parts = line.split(',')
            name = parts[-1].strip() if len(parts) > 1 else "Unknown"

            if PATTERN_AD_CHANNEL.search(name):
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('http'):
                    i += 1
                i += 1
                continue

            if '###' in name:
                i += 1
                continue

            extra = []
            i += 1

            while i < len(lines) and not lines[i].strip().startswith('http'):
                extra_line = lines[i].strip()
                if extra_line.startswith('#EXTVLCOPT') or extra_line.startswith('#'):
                    extra.append(extra_line)
                i += 1

            if i < len(lines):
                url = lines[i].strip()
                if url.startswith('http'):
                    channels.append({
                        'name': name,
                        'url': url,
                        'params': params,
                        'extra': extra if extra else None
                    })
            i += 1
        else:
            i += 1

    return channels

# ================== FOOTONSAT PARSER ==================
def parse_footonsat_data(data: dict, start_ts: int, end_ts: int) -> List[Dict]:
    games = []
    if not data or "footonsat" not in data or not isinstance(data["footonsat"], list):
        return games

    items = data["footonsat"]
    i = 0

    while i < len(items):
        item = items[i]
        if not isinstance(item, dict) or "match" not in item:
            i += 1
            continue

        compet = (item.get("compet") or "").lower()
        league = None
        for key, val in LEAGUE_MAPPING.items():
            if key in compet:
                league = val
                break

        if not league:
            i += 1
            continue

        try:
            date_str = item.get("date")
            time_str = item.get("time")
            if not date_str or not time_str:
                i += 1
                continue

            dt_utc = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
            kick_utc = int(dt_utc.timestamp())

            if kick_utc < start_ts or kick_utc > end_ts:
                i += 1
                continue

            match_name = item.get("match", "").strip()
            if not is_football_allowed(league, match_name):
                i += 1
                continue

            channels = []
            j = i + 1
            while j < len(items):
                next_item = items[j]
                if isinstance(next_item, dict) and "match" in next_item:
                    break
                if isinstance(next_item, dict) and "channel" in next_item:
                    ch_name = next_item.get("channel", "").replace('📺', '').strip()
                    if ch_name:
                        channels.append({
                            "country_code": None,
                            "channel_name": ch_name
                        })
                j += 1

            if channels:
                games.append({
                    "league": league,
                    "match": match_name,
                    "kick_utc": kick_utc,
                    "time": vn_time(kick_utc),
                    "channels": channels,
                    "source": "footonsat"
                })
            i = j
        except:
            i += 1

    return games

# ================== LOVE4VN PARSER ==================
def parse_love4vn_data(data: dict, start_ts: int, end_ts: int) -> List[Dict]:
    games = []
    if not data or "days" not in data:
        return games

    for day_info in data["days"].values():
        for game in day_info.get("games", []):
            kick_utc = game.get("kick_utc")
            if not kick_utc or kick_utc < start_ts or kick_utc > end_ts:
                continue

            league = game.get("league", "")
            match_name = game.get("match", "").strip()

            if league != "Tennis" and not is_football_allowed(league, match_name):
                continue

            channels = []
            for entry in game.get("tv_channels", []):
                for ch_name in entry.get("channels", []):
                    if ch_name:
                        channels.append({
                            "country_code": None,
                            "channel_name": ch_name
                        })

            if channels:
                games.append({
                    "league": league,
                    "match": match_name,
                    "kick_utc": kick_utc,
                    "time": game.get("time", vn_time(kick_utc)),
                    "channels": channels,
                    "source": "love4vn"
                })

    return games

# ================== VALIDATION ==================
def validate_url_sync(url: str) -> Tuple[bool, Optional[str]]:
    if url.startswith("udp://"):
        return True, None

    url_lower = url.lower()
    if "cinehub24.com" in url_lower or url_lower.endswith(".mp4"):
        return False, "Blacklisted"

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', USER_AGENT)
        req.add_header('Range', 'bytes=0-1024')

        with urllib.request.urlopen(req, timeout=VALIDATION_TIMEOUT) as resp:
            if resp.getcode() not in (200, 206):
                return False, f"HTTP {resp.getcode()}"

            if '.m3u8' in url:
                body = resp.read(5000).decode('utf-8', errors='ignore')
                return "#EXTM3U" in body or "#EXTINF" in body, "Invalid HLS"

            return True, None
    except:
        return False, "Error"

async def validate_events_batch(events: List[Dict]) -> List[Dict]:
    if not events:
        return []

    print(f"\n🔬 Kiểm tra {len(events)} kênh (concurrent={VALIDATION_CONCURRENT})...")

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=VALIDATION_CONCURRENT) as executor:
        tasks = [
            loop.run_in_executor(executor, validate_url_sync, ev['channel']['url'])
            for ev in events
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid_events = [
            ev for ev, result in zip(events, results)
            if result and result[0] is True
        ]

    print(f"   ✅ {len(valid_events)}/{len(events)} hợp lệ")
    return valid_events

# ================== SORTING HELPER ==================
def channel_priority(channel_name: str, code: Optional[str]) -> int:
    name_lower = channel_name.lower()
    if 'hub sports' in name_lower:
        return 0
    if 'now sports' in name_lower or 'now premier sports' in name_lower:
        return 1
    if code == 'uk' or name_lower.startswith('uk ') or name_lower.startswith('uk:'):
        return 2
    english_codes = {'us', 'ca', 'au', 'nz', 'ie', 'gb', 'en'}
    if code in english_codes:
        return 3
    if 'english' in name_lower:
        return 3
    return 4

# ================== MAIN ==================
async def main():
    start = time.time()

    now_utc = datetime.now(ZoneInfo("UTC"))
    now_ts = int(now_utc.timestamp())
    start_ts = now_ts - 7200
    end_ts = now_ts + 86400

    print("🔄 Bắt đầu...")

    print("📡 Tải APIs...")
    tasks = []
    if ENABLE_FOOTONSAT:
        tasks.extend([fetch_json_async(url) for url in FOOTONSAT_URLS])
    love4vn_task = fetch_json_async(LOVE4VN_URL)

    if ENABLE_FOOTONSAT:
        footonsat_results = await asyncio.gather(*tasks)
    else:
        footonsat_results = []
    love4vn_data = await love4vn_task

    all_games = []
    if ENABLE_FOOTONSAT:
        for data in footonsat_results:
            if data:
                all_games.extend(parse_footonsat_data(data, start_ts, end_ts))

    if love4vn_data:
        all_games.extend(parse_love4vn_data(love4vn_data, start_ts, end_ts))

    # ================== GỘP TRẬN BÓNG ĐÁ TRÙNG LẶP ==================
    def extract_teams(match_name):
        parts = re.split(r'\s+vs\.?\s+|\s+v\s+', match_name, flags=re.I)
        if len(parts) == 2:
            return {parts[0].strip().lower(), parts[1].strip().lower()}
        return {match_name.strip().lower()}

    merged_games = {}
    for game in all_games:
        league = game['league']
        kick_utc = game['kick_utc']
        if league in ALLOWED_FOOTBALL_LEAGUES:
            team_set = frozenset(extract_teams(game['match']))
            key = (league, kick_utc, team_set)
        else:
            key = (league, kick_utc, game['match'].strip().lower())
        
        if key not in merged_games:
            merged_games[key] = game.copy()
            merged_games[key]['channels'] = list(game['channels'])
        else:
            existing_names = {ch['channel_name'] for ch in merged_games[key]['channels']}
            for ch in game['channels']:
                if ch['channel_name'] not in existing_names:
                    merged_games[key]['channels'].append(ch)
                    existing_names.add(ch['channel_name'])
    
    all_games = list(merged_games.values())
    # =============================================================

    print(f"✅ Tổng: {len(all_games)} trận")
    if not all_games:
        print("⚠️ Không có trận nào.")
        return

    print("📥 Đọc M3U từ file local...")
    cached = CacheManager.get_cache()

    if cached:
        print(f"   Từ cache: {len(cached)} kênh")
        channels = cached
    else:
        # Đọc danh sách file .m3u cần dùng (từ M3U_list.txt)
        m3u_files = []
        try:
            with open(M3U_LIST_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # Nếu là URL, trích xuất tên file cuối cùng
                    if line.startswith('http'):
                        filename = line.split('/')[-1].split('?')[0]
                        if filename.endswith('.m3u'):
                            m3u_files.append(filename)
                    else:
                        # Nếu là đường dẫn tương đối, giữ nguyên
                        if line.endswith('.m3u'):
                            m3u_files.append(line)
        except Exception as e:
            print(f"   ⚠️ Không thể đọc {M3U_LIST_FILE}: {e}")
            m3u_files = []

        # Nếu không có file nào, thử tìm tất cả file .m3u trong thư mục hiện tại
        if not m3u_files:
            import glob
            m3u_files = glob.glob("*.m3u")
            print(f"   🔍 Tự động tìm thấy {len(m3u_files)} file .m3u: {m3u_files}")

        print(f"   📄 Số file M3U cần đọc: {len(m3u_files)}")

        all_channels = []
        for filename in m3u_files:
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    content = f.read()
                if content:
                    parsed = parse_m3u_fast(content)
                    # Lọc bỏ kênh có độ phân giải thấp (nếu cần)
                    filtered = [ch for ch in parsed if not is_low_resolution(ch.get('name', ''))]
                    all_channels.extend(filtered)
                    print(f"      ✅ Đọc {filename}: {len(filtered)} kênh (bỏ qua {len(parsed)-len(filtered)} kênh low-res)")
                else:
                    print(f"      ⚠️ {filename} rỗng")
            except Exception as e:
                print(f"      ❌ Lỗi đọc {filename}: {e}")

        # Loại bỏ trùng lặp theo URL
        channels = list({ch['url']: ch for ch in all_channels}.values())
        CacheManager.save_cache(channels)
        print(f"   ✅ Tổng cộng {len(channels)} kênh hợp lệ (sau khi lọc trùng)")

    for ch in channels:
        code, clean = extract_prefix_and_name(ch['name'])
        ch['_code'] = code
        ch['_clean'] = clean
        ch['_norm'] = normalize_channel_name(clean)
        ch['_num'], ch['_4k_before'] = extract_channel_info(clean)

    print("\n🔍 QUÁ TRÌNH MATCH KÊNH (giữ tất cả kênh khớp, chống trùng theo quy tắc):")

    used_urls_per_match = defaultdict(set)
    used_urls_tennis = set()

    live_events = []
    total_requested_channels = 0
    total_matched_entries = 0

    for game in all_games:
        league = game['league']
        match_name = game['match']
        kick_utc = game['kick_utc']
        kick_time = game['time']

        if league == "Tennis":
            match_key = "TENNIS_ALL"
        else:
            match_key = (league, match_name, kick_utc)

        print(f"\n🏆 [{league}] {match_name} | {kick_time} (UTC {kick_utc})")

        channels_from_json = game.get('channels', [])
        if not channels_from_json:
            print("   ⚠️  Không có kênh nào từ JSON")
            continue

        for ch_info in channels_from_json:
            target_name_raw = ch_info.get('channel_name')
            if not target_name_raw:
                continue

            original_name = target_name_raw

            if league == "Tennis":
                if re.search(r'\bbein\s*sports?\b', target_name_raw, re.I):
                    target_name_raw = re.sub(
                        r'\b(beIN\s*Sports?)\s*(\d+|connect)\b',
                        r'\1 7',
                        target_name_raw,
                        flags=re.I
                    )
                    if not re.search(r'\bbeIN\s*Sports?\s+\d+\b', target_name_raw, re.I):
                        target_name_raw = re.sub(
                            r'\b(beIN\s*Sports?)\b',
                            r'\1 7',
                            target_name_raw,
                            flags=re.I
                        )

            target_name = preprocess_target_channel(target_name_raw)

            total_requested_channels += 1
            if target_name != target_name_raw:
                print(f"   📡 Yêu cầu: {target_name_raw} -> đã đổi thành: {target_name}")
            else:
                print(f"   📡 Yêu cầu: {target_name}")

            target_code, target_clean = extract_prefix_and_name(target_name)
            target_num, target_4k_before = extract_channel_info(target_clean)
            target_norm = normalize_channel_name(target_clean)

            is_bein_english_ar = (
                target_code == 'ar' and
                re.search(r'\bbein\s*sports?\b', original_name, re.I) and
                re.search(r'\benglish\b', original_name, re.I)
            )

            matching = []
            for ch in channels:
                if target_code is not None:
                    if ch['_code'] is None or target_code != ch['_code']:
                        continue

                if (target_num is None) != (ch['_num'] is None):
                    continue
                if target_num is not None:
                    if target_num != ch['_num']:
                        continue
                    if target_4k_before != ch['_4k_before']:
                        continue

                if is_bein_english_ar:
                    if 'english' not in ch['_norm']:
                        continue

                if is_channel_match(target_name, ch['name'], league):
                    if ch['_norm'] == target_norm or ch['_norm'].replace(' ', '') == target_norm.replace(' ', ''):
                        score = 1.0
                    else:
                        score = similar(ch['_norm'], target_norm)
                    matching.append((score, ch))

            if matching:
                matching.sort(key=lambda x: x[0], reverse=True)
                print(f"      🔍 Tìm thấy {len(matching)} kênh M3U khớp")

                for score, ch in matching:
                    url = ch['url']
                    if league == "Tennis":
                        if url in used_urls_tennis:
                            print(f"         ⚠️ Bỏ qua {ch['name']} (score={score:.3f}) - URL đã dùng trong tennis")
                            continue
                        used_urls_tennis.add(url)
                        used_urls_per_match[match_key].add(url)
                    else:
                        if url in used_urls_per_match[match_key]:
                            print(f"         ⚠️ Bỏ qua {ch['name']} (score={score:.3f}) - URL đã dùng trong trận này")
                            continue
                        used_urls_per_match[match_key].add(url)

                    total_matched_entries += 1
                    live_events.append({
                        "datetime": datetime.fromtimestamp(kick_utc).astimezone(TIMEZONE),
                        "name": f"{kick_time} | {match_name} ({ch['name']})",
                        "channel": ch,
                        "league": league,
                        "match_key": match_key
                    })
                    print(f"         ✅ Thêm kênh: {ch['name']} (score={score:.3f})")
            else:
                print("      ❌ Không tìm thấy kênh M3U phù hợp")

    print(f"\n📊 TỔNG KẾT MATCH: {total_matched_entries} kênh được thêm từ {total_requested_channels} yêu cầu")

    events_by_match = defaultdict(list)
    for ev in live_events:
        events_by_match[ev["match_key"]].append(ev)

    sorted_match_keys = sorted(events_by_match.keys(),
                               key=lambda k: min(ev["datetime"] for ev in events_by_match[k]))
    final_events = []
    for mk in sorted_match_keys:
        evs = events_by_match[mk]
        evs.sort(key=lambda x: channel_priority(x["channel"]["name"], x["channel"]["_code"]))
        final_events.extend(evs)

    live_events = final_events

    if not SKIP_VALIDATION:
        live_events = await validate_events_batch(live_events)

    with open(LIVE_M3U, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ev in live_events:
            ch = ev["channel"]
            league = ev["league"]
            group = LEAGUE_GROUP_NAME.get(league, "Live Football")
            extinf = f'#EXTINF:-1 tvg-id="{ch["params"].get("tvg-id","")}" group-title="{group}"'
            if ch["params"].get("tvg-logo"):
                extinf += f' tvg-logo="{ch["params"]["tvg-logo"]}"'
            extinf += f',{ev["name"]}'
            f.write(extinf + "\n")
            if ch.get('extra'):
                f.write('\n'.join(ch['extra']) + "\n")
            f.write(ch['url'] + "\n")

    elapsed = time.time() - start
    print(f"\n🎉 HOÀN THÀNH! {len(live_events)} kênh trong {elapsed:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())
