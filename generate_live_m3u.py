#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tạo Stream_live.m3u từ schedule.json và danh sách M3U.

Các tính năng:
- Lấy lịch từ GitHub, tải nhiều M3U, phân tích #EXTINF, #EXTVLCOPT.
- Lọc HD+ (4K, UHD, FHD, HD, 1080, 720), bỏ SD.
- Xoá tag quảng cáo: ###, ===, ---, ***, ☰.
- Fuzzy matching tên kênh (có xét số kênh và quốc gia).
- Xử lý stream dạng "NEXT | Tên trận" (ưu tiên match theo tên trận).
- Thêm prefix ngày giờ (từ trường "time" trong schedule, giữ nguyên AM/PM) vào tên hiển thị.
- Tennis: chỉ lấy mỗi kênh một lần cho tất cả các trận.
- Kiểm tra stream sống (HEAD/GET) với cache và song song.
- Sắp xếp kênh trong cùng một trận theo thứ tự ưu tiên: UK > US > CA > AU > NZ > IE > khác.
- Xuất ra file Stream_live.m3u với group-title theo giải đấu (có emoji).
"""

import json
import re
import os
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from fuzzy_matcher import FuzzyMatcher

# ==================== CẤU HÌNH ====================
SCHEDULE_URL = "https://raw.githubusercontent.com/Love4vn/Live-Schedue/refs/heads/1/schedule.json"
M3U_LIST_FILE = "M3U_list.txt"
OUTPUT_M3U = "Stream_live.m3u"

FUZZY_THRESHOLD = 85
MAX_STREAMS_PER_CHANNEL = 300      # tối đa số stream giữ lại cho mỗi kênh trong một trận
CHECK_ALIVE = False                # bật/tắt kiểm tra stream sống (để nhanh hơn nếu tin tưởng nguồn)
ALIVE_CHECK_WORKERS = 20
CHECK_TIMEOUT = 8

IGNORE_TAGS = [
    "[Backup]", "(SD)", "[SD]", "SD", "Low", "480p", "576p",
    "PLAY+:", "VIP:", "NOW:", "RAW", "HEVC", "VIP", "NOW", "FHD", "HD"
]

LEAGUE_TO_GROUP = {
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

# Chất lượng
HD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:4K|UHD|FHD|Full[ -]?HD|HD|1080|720)(?=\W|$)', re.IGNORECASE)
SD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:SD|480|576|Low|Backup|Dead)(?=\W|$)', re.IGNORECASE)

# ========== XỬ LÝ QUỐC GIA ==========
COUNTRY_CODE_MAP = {
    "united states": "US", "usa": "US", "us": "US",
    "united kingdom": "UK", "uk": "UK", "great britain": "UK",
    "viet nam": "VN", "vietnam": "VN",
    "france": "FR", "french": "FR",
    "germany": "DE",
    "italy": "IT",
    "spain": "ES",
    "canada": "CA",
    "australia": "AU",
    "netherlands": "NL",
    "belgium": "BE",
    "brazil": "BR",
    "mexico": "MX",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "peru": "PE",
    "venezuela": "VE",
    "india": "IN",
    "japan": "JP",
    "china": "CN",
    "south africa": "ZA",
    "nigeria": "NG",
    "egypt": "EG",
    "saudi arabia": "SA",
    "uae": "AE",
    "qatar": "QA",
    "kuwait": "KW",
    "oman": "OM",
    "bahrain": "BH",
    "jordan": "JO",
    "lebanon": "LB",
    "iraq": "IQ",
    "iran": "IR",
    "israel": "IL",
    "turkey": "TR",
    "russia": "RU",
    "poland": "PL",
    "czech": "CZ",
    "czechia": "CZ",
    "slovakia": "SK",
    "hungary": "HU",
    "romania": "RO",
    "bulgaria": "BG",
    "serbia": "RS",
    "srbija": "RS",
    "croatia": "HR",
    "hrvatska": "HR",
    "slovenia": "SI",
    "bosnia": "BA",
    "macedonia": "MK",
    "albania": "AL",
    "greece": "GR",
    "hellas": "GR",
    "cyprus": "CY",
    "portugal": "PT",
    "sweden": "SE",
    "sverige": "SE",
    "norway": "NO",
    "denmark": "DK",
    "danmark": "DK",
    "finland": "FI",
    "iceland": "IS",
    "ireland": "IE",
    "switzerland": "CH",
    "austria": "AT",
    "belarus": "BY",
    "ukraine": "UA",
    "kazakhstan": "KZ",
    "uzbekistan": "UZ",
    "thailand": "TH",
    "malaysia": "MY",
    "singapore": "SG",
    "indonesia": "ID",
    "philippines": "PH",
    "pakistan": "PK",
    "bangladesh": "BD",
    "sri lanka": "LK",
    "nepal": "NP",
    "morocco": "MA",
    "algeria": "DZ",
    "tunisia": "TN",
    "libya": "LY",
    "sudan": "SD",
    "ethiopia": "ET",
    "kenya": "KE",
    "tanzania": "TZ",
    "uganda": "UG",
    "rwanda": "RW",
    "ghana": "GH",
    "senegal": "SN",
    "ivory coast": "CI",
    "cameroon": "CM",
    "angola": "AO",
    "mozambique": "MZ",
    "zimbabwe": "ZW",
    "zambia": "ZM",
}

CODE_TO_FULLNAMES = {}
for name, code in COUNTRY_CODE_MAP.items():
    CODE_TO_FULLNAMES.setdefault(code, []).append(name)

# Các nhãn đặc biệt không phải quốc gia (bỏ qua)
SPECIAL_LABELS = {"livesportsontv", "wheresthematch", "ausport"}

def normalize_country(country_name):
    if not country_name:
        return None
    label = country_name.strip().lower()
    if label in SPECIAL_LABELS:
        return None
    if "united states" in label:
        return "US"
    if "united kingdom" in label:
        return "UK"
    if "viet nam" in label or "vietnam" in label:
        return "VN"
    return COUNTRY_CODE_MAP.get(label, None)

def contains_country(stream_name, country_code):
    if not country_code:
        return True
    patterns = [
        rf'\b{country_code}\s*:',
        rf'\b{country_code}\s*-',
        rf'\|{country_code}\|',
        rf'\({country_code}\)',
        rf'\[{country_code}\]',
        rf'\b{country_code}\b',
    ]
    for pat in patterns:
        if re.search(pat, stream_name, re.IGNORECASE):
            return True
    if country_code in CODE_TO_FULLNAMES:
        for full_name in CODE_TO_FULLNAMES[country_code]:
            if re.search(rf'\b{re.escape(full_name)}\b', stream_name, re.IGNORECASE):
                return True
    return False

# ========== TIỀN XỬ LÝ TÊN KÊNH ==========
def extract_channel_number(name):
    match = re.search(r'\b(\d+)\b', name)
    return int(match.group(1)) if match else None

def clean_stream_name(name):
    name = re.sub(r'┃[^┃]+┃', '', name)
    # Xóa tiền tố dạng "VIP:", "NOW:", "UK:", "US:" (2-5 chữ hoa + dấu hai chấm)
    name = re.sub(r'\b[A-Z]{2,5}:\s*', '', name)
    # Xóa tiền tố dạng "UK -", "VIP -" (2-5 chữ hoa + dấu cách + dấu gạch ngang)
    name = re.sub(r'\b[A-Z]{2,5}\s*-\s*', '', name)
    name = re.sub(r'\[[^\]]+\]', '', name)
    # Xóa các tag chất lượng (dạng từ riêng)
    name = re.sub(r'\b(?:FHD|HD|4K|UHD|8K|SD)\b', '', name, flags=re.IGNORECASE)
    return name.strip()

def is_advertisement_stream(name):
    return bool(re.search(r'#{3,}|={3,}|☰{3,}|-{3,}|\*{3,}', name))

# ========== TẢI VÀ PHÂN TÍCH M3U ==========
def download_text(url, timeout=30):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; M3U-Builder/1.0)'}
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        if resp.encoding is None:
            resp.encoding = 'utf-8'
        return resp.text
    except Exception as e:
        print(f"  Lỗi tải {url}: {e}")
        return None

def parse_m3u(content, base_url=None):
    streams = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            parts = line.split(',', 1)
            name = parts[1].strip() if len(parts) > 1 else ""
            if is_advertisement_stream(name):
                i += 1
                continue
            headers = {}
            j = i + 1
            while j < len(lines) and lines[j].startswith('#EXTVLCOPT'):
                opt_line = lines[j].strip()
                if '=' in opt_line:
                    key, val = opt_line.split('=', 1)
                    key = key.replace('#EXTVLCOPT:', '').strip()
                    headers[key] = val.strip()
                j += 1
            url_line = lines[j].strip() if j < len(lines) else ''
            if url_line and not url_line.startswith('http') and base_url:
                url_line = requests.compat.urljoin(base_url, url_line)
            if url_line and not url_line.startswith('#'):
                quality = "SD"
                if HD_KEYWORDS.search(name):
                    if re.search(r'(?i)4K|UHD', name):
                        quality = "4K"
                    elif re.search(r'(?i)FHD|Full[ -]?HD|1080', name):
                        quality = "FHD"
                    else:
                        quality = "HD"
                if SD_KEYWORDS.search(name):
                    quality = "SD"
                clean = clean_stream_name(name)
                num = extract_channel_number(name)
                streams.append({
                    'name': name,
                    'clean_name': clean,
                    'channel_number': num,
                    'url': url_line,
                    'headers': headers,
                    'quality': quality
                })
            i = j + 1
        else:
            i += 1
    return streams

def load_all_m3us(m3u_urls):
    all_streams = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(download_text, url): url for url in m3u_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            content = future.result()
            if content:
                base = url[:url.rfind('/')+1] if '/' in url else None
                streams = parse_m3u(content, base)
                print(f"  Đã tải {url}: {len(streams)} stream")
                all_streams.extend(streams)
            else:
                print(f"  Bỏ qua {url}")
    return all_streams

def filter_hd_streams(streams):
    return [s for s in streams if s['quality'] != 'SD']

# ========== KIỂM TRA STREAM SỐNG ==========
_alive_cache = {}
def check_stream_alive(stream):
    url = stream['url']
    if url in _alive_cache:
        return _alive_cache[url]
    headers = {}
    for k, v in stream['headers'].items():
        if k == 'http-user-agent':
            headers['User-Agent'] = v
        elif k == 'http-cookie':
            headers['Cookie'] = v
        elif k == 'http-header':
            if ': ' in v:
                h_name, h_val = v.split(': ', 1)
                headers[h_name] = h_val
            else:
                headers['Authorization'] = v
        else:
            headers[k] = v
    try:
        resp = requests.head(url, headers=headers, timeout=CHECK_TIMEOUT, allow_redirects=True)
        if resp.status_code in (200, 206, 302, 304):
            _alive_cache[url] = True
            return True
        resp = requests.get(url, headers=headers, timeout=CHECK_TIMEOUT, stream=True)
        if resp.status_code in (200, 206):
            for _ in resp.iter_content(1024):
                break
            _alive_cache[url] = True
            return True
    except Exception:
        pass
    _alive_cache[url] = False
    return False

def check_alive_batch(streams):
    if not CHECK_ALIVE:
        return {s['url'] for s in streams}
    alive_urls = set()
    with ThreadPoolExecutor(max_workers=ALIVE_CHECK_WORKERS) as executor:
        future_to_stream = {executor.submit(check_stream_alive, s): s for s in streams}
        for future in as_completed(future_to_stream):
            if future.result():
                alive_urls.add(future_to_stream[future]['url'])
    return alive_urls

# ========== MATCH THEO TÊN TRẬN ==========
def match_stream_by_match_name(match_name, stream_name):
    def normalize(s):
        s = s.lower()
        s = re.sub(r'[^\w\s]', '', s)
        s = re.sub(r'\s+', ' ', s).strip()
        return s
    norm_match = normalize(match_name)
    norm_stream = normalize(stream_name)
    return norm_match in norm_stream

# ========== HÀM SẮP XẾP ƯU TIÊN ==========
def get_priority_for_stream(stream_name):
    """
    Trả về mức ưu tiên (càng nhỏ càng ưu tiên):
    0: UK
    1: US
    2: Canada
    3: Australia
    4: New Zealand
    5: Ireland
    6: Các nước nói tiếng Anh khác (dựa trên từ khóa English)
    7: Còn lại
    """
    name_upper = stream_name.upper()
    # Kiểm tra các mã quốc gia (ưu tiên theo thứ tự)
    if re.search(r'\b(?:UK|GB|UNITED KINGDOM|GREAT BRITAIN)\b', name_upper):
        return 0
    if re.search(r'\b(?:US|USA|UNITED STATES)\b', name_upper):
        return 1
    if re.search(r'\b(?:CA|CANADA)\b', name_upper):
        return 2
    if re.search(r'\b(?:AU|AUS|AUSTRALIA)\b', name_upper):
        return 3
    if re.search(r'\b(?:NZ|NEW ZEALAND)\b', name_upper):
        return 4
    if re.search(r'\b(?:IE|IRELAND)\b', name_upper):
        return 5
    if re.search(r'\b(?:ENGLISH|ENG)\b', name_upper):
        return 6
    return 7

# ========== HÀM CHÍNH ==========
def main():
    print("=== BẮT ĐẦU TẠO Stream_live.m3u ===\n")

    # 1. Tải schedule
    print("[1] Tải lịch thi đấu...")
    schedule_json_str = download_text(SCHEDULE_URL)
    if not schedule_json_str:
        print("Lỗi: Không thể tải schedule.json")
        sys.exit(1)
    schedule_data = json.loads(schedule_json_str)

    # 2. Đọc danh sách M3U
    print("[2] Đọc M3U_list.txt...")
    if not os.path.exists(M3U_LIST_FILE):
        print(f"Lỗi: Không tìm thấy {M3U_LIST_FILE}")
        sys.exit(1)
    with open(M3U_LIST_FILE, 'r', encoding='utf-8') as f:
        m3u_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    print(f"  Có {len(m3u_urls)} URL M3U.")

    # 3. Tải M3U
    print("[3] Tải M3U...")
    all_streams = load_all_m3us(m3u_urls)
    print(f"  Tổng stream: {len(all_streams)}")
    hd_streams = filter_hd_streams(all_streams)
    print(f"  Stream HD+: {len(hd_streams)}")

    # 4. Chuẩn bị FuzzyMatcher
    print("[4] Khởi tạo FuzzyMatcher...")
    matcher = FuzzyMatcher(plugin_dir=None, match_threshold=FUZZY_THRESHOLD)
    clean_names = [s['clean_name'] for s in hd_streams if s['clean_name']]
    matcher.precompute_normalizations(
        clean_names,
        user_ignored_tags=IGNORE_TAGS,
        ignore_quality=True,
        ignore_regional=True,
        ignore_geographic=True,
        ignore_misc=True
    )

    # 5. Thu thập yêu cầu kênh
    channel_requests = []
    game_info = {}
    for day_data in schedule_data.get('days', {}).values():
        for game in day_data.get('games', []):
            match_name = game.get('match', '').strip()
            league = game.get('league', '')
            time_str = game.get('time', '')
            if not match_name:
                continue
            if match_name not in game_info:
                game_info[match_name] = time_str
            for tv_item in game.get('tv_channels', []):
                country_raw = tv_item.get('country', '')
                country_code = normalize_country(country_raw)
                for ch_name in tv_item.get('channels', []):
                    ch_name = ch_name.strip()
                    if not ch_name:
                        continue
                    if country_code is None:
                        for full_name, code in COUNTRY_CODE_MAP.items():
                            if re.search(rf'\b{re.escape(full_name)}\b', ch_name, re.IGNORECASE):
                                country_code = code
                                break
                    channel_requests.append((ch_name, country_code, league, match_name))

    unique_reqs = {}
    for ch, cc, lg, mn in channel_requests:
        if ch not in unique_reqs:
            unique_reqs[ch] = (cc, lg, mn)
    print(f"  Số kênh cần tìm: {len(unique_reqs)}")

    # 6. Stream đặc biệt (chứa tên trận)
    match_specific_streams = {}
    for stream in hd_streams:
        for match_name in game_info.keys():
            if match_stream_by_match_name(match_name, stream['name']):
                match_specific_streams.setdefault(match_name, []).append(stream)

    # 7. Fuzzy matching
    channel_to_streams = {}
    for ch_name, (country_code, league, match_name) in unique_reqs.items():
        if match_name in match_specific_streams:
            candidates = []
            for stream in match_specific_streams[match_name]:
                if country_code and not contains_country(stream['name'], country_code):
                    continue
                candidates.append((stream, 100))
            if candidates:
                channel_to_streams[ch_name] = candidates[:MAX_STREAMS_PER_CHANNEL]
                print(f"  ✓ {ch_name} (match specific) -> {len(candidates)} stream")
                continue
        best_matches = []
        expected_number = extract_channel_number(ch_name)
        for stream in hd_streams:
            if country_code and not contains_country(stream['name'], country_code):
                continue
            if expected_number is not None and stream['channel_number'] != expected_number:
                continue
            match_name_found, score, _ = matcher.fuzzy_match(
                query_name=ch_name,
                candidate_names=[stream['clean_name']],
                user_ignored_tags=IGNORE_TAGS,
                ignore_quality=True,
                ignore_regional=True,
                ignore_geographic=True,
                ignore_misc=True
            )
            if match_name_found and score >= FUZZY_THRESHOLD:
                best_matches.append((stream, score))
        if best_matches:
            best_matches.sort(key=lambda x: x[1], reverse=True)
            channel_to_streams[ch_name] = best_matches[:MAX_STREAMS_PER_CHANNEL]
            print(f"  ✓ {ch_name} (fuzzy) -> {len(best_matches)} stream")
        else:
            print(f"  ✗ {ch_name} -> không khớp")

    # 8. Kiểm tra sống
    all_candidate_streams = []
    for streams in channel_to_streams.values():
        for stream, _ in streams:
            all_candidate_streams.append(stream)
    print(f"\n[5] Kiểm tra sống {len(all_candidate_streams)} stream...")
    alive_urls = check_alive_batch(all_candidate_streams)
    print(f"  Số stream sống: {len(alive_urls)}")

    # 9. Tạo kết quả với sắp xếp ưu tiên trong từng trận
    print("[6] Tạo file M3U...")
    results = []
    tennis_seen_urls = set()

    for day_data in schedule_data.get('days', {}).values():
        for game in day_data.get('games', []):
            match_name = game.get('match', '').strip()
            league = game.get('league', '')
            if not match_name:
                continue
            prefix = game_info.get(match_name, '')
            if prefix:
                prefix = f"[{prefix}]"
            else:
                prefix = ""
            seen_urls_in_game = set()
            game_items = []   # lưu tạm các item cho trận này kèm priority

            for tv_item in game.get('tv_channels', []):
                for ch_name in tv_item.get('channels', []):
                    ch_name = ch_name.strip()
                    if ch_name not in channel_to_streams:
                        continue
                    for stream, _ in channel_to_streams[ch_name]:
                        if stream['url'] not in alive_urls:
                            continue
                        if league == "Tennis":
                            if stream['url'] in tennis_seen_urls:
                                continue
                            tennis_seen_urls.add(stream['url'])
                        else:
                            if stream['url'] in seen_urls_in_game:
                                continue
                            seen_urls_in_game.add(stream['url'])
                        priority = get_priority_for_stream(stream['name'])
                        game_items.append({
                            'match_name': match_name,
                            'channel_name': stream['name'],
                            'stream_url': stream['url'],
                            'headers': stream['headers'],
                            'league': league,
                            'time_prefix': prefix,
                            'priority': priority
                        })

            # Sắp xếp game_items theo priority tăng dần
            game_items.sort(key=lambda x: x['priority'])
            for item in game_items:
                item.pop('priority', None)
                results.append(item)

    print(f"  Số dòng M3U tạo được: {len(results)}")

    # 10. Ghi file M3U
    with open(OUTPUT_M3U, 'w', encoding='utf-8') as out:
        out.write('#EXTM3U\n')
        for item in results:
            group = LEAGUE_TO_GROUP.get(item['league'], item['league'])
            if item['time_prefix']:
                display = f"{item['time_prefix']} {item['match_name']} ({item['channel_name']})"
            else:
                display = f"{item['match_name']} ({item['channel_name']})"
            out.write(f'#EXTINF:-1 group-title="{group}",{display}\n')
            for key, val in item['headers'].items():
                out.write(f'#EXTVLCOPT:{key}={val}\n')
            out.write(f'{item["stream_url"]}\n')
        out.write('\n')

    print(f"✅ Hoàn tất! Đã tạo {OUTPUT_M3U}.")

if __name__ == '__main__':
    main()
