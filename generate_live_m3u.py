#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tạo Stream_live.m3u với:
- Xử lý quốc gia: chỉ match nếu stream có chứa country code (nếu schedule có country)
- Bỏ qua stream chứa ### (quảng cáo)
- Tách số kênh: số phải khớp chính xác
- Tennis: deduplicate toàn bộ (chỉ lấy mỗi kênh một lần cho tất cả trận)
"""

import json
import re
import os
import sys
import requests
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from fuzzy_matcher import FuzzyMatcher

# ==================== CẤU HÌNH ====================
SCHEDULE_URL = "https://raw.githubusercontent.com/Love4vn/Live-Schedue/refs/heads/1/schedule.json"
M3U_LIST_FILE = "M3U_list.txt"
OUTPUT_M3U = "Stream_live.m3u"

FUZZY_THRESHOLD = 85
MAX_STREAMS_PER_CHANNEL = 300          # tối đa số stream giữ lại cho mỗi kênh trong một trận
CHECK_ALIVE = False                   # bật kiểm tra stream sống
ALIVE_CHECK_WORKERS = 20
CHECK_TIMEOUT = 8

IGNORE_TAGS = ["[Backup]", "(SD)", "[SD]", "SD", "Low", "480p", "576p", "PLAY+:"]

LEAGUE_TO_GROUP = {
    "Premier League": "Live Premier League",
    "Serie A": "Live Serie A",
    "Bundesliga": "Live Bundesliga",
    "La Liga": "Live La Liga",
    "Ligue 1": "Live Ligue 1",
    "UEFA Champions League": "Live UEFA Champions League",
    "UEFA Europa League": "Live UEFA Europa League",
    "UEFA Europa Conference League": "Live UEFA Conference League",
    "UEFA Euro": "Live Euro",
    "FA Cup": "Live FA, League Cup",
    "League Cup": "Live FA, League Cup",
    "Tennis": "Live Tennis",
    "FIFA World Cup": "Live Fifa World Cup",
    "International Friendly": "Live International Friendly"
}

HD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:4K|UHD|FHD|Full[ -]?HD|HD|1080|720)(?=\W|$)', re.IGNORECASE)
SD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:SD|480|576|Low|Backup|Dead)(?=\W|$)', re.IGNORECASE)

# ========== Mapping quốc gia ==========
# Ánh xạ tên quốc gia (tiếng Anh) sang mã 2 ký tự (ưu tiên viết hoa)
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
    "slovakia": "SK",
    "hungary": "HU",
    "romania": "RO",
    "bulgaria": "BG",
    "serbia": "RS",
    "croatia": "HR",
    "slovenia": "SI",
    "bosnia": "BA",
    "macedonia": "MK",
    "albania": "AL",
    "greece": "GR",
    "cyprus": "CY",
    "portugal": "PT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
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
    "nigeria": "NG",
    "senegal": "SN",
    "ivory coast": "CI",
    "cameroon": "CM",
    "angola": "AO",
    "mozambique": "MZ",
    "zimbabwe": "ZW",
    "zambia": "ZM",
}

def normalize_country(country_name):
    """Chuyển tên quốc gia (có thể viết hoa, viết thường) thành mã 2 chữ cái."""
    if not country_name:
        return None
    key = country_name.strip().lower()
    # Xử lý trường hợp "United States of America"
    if "united states" in key:
        return "US"
    if "united kingdom" in key:
        return "UK"
    if "viet nam" in key or "vietnam" in key:
        return "VN"
    return COUNTRY_CODE_MAP.get(key, None)

# ========== HÀM XỬ LÝ TÊN KÊNH ==========
def extract_channel_number(name):
    """Lấy số cuối cùng trong tên kênh (ví dụ 'beIN SPORTS 1' -> 1, 'BBC One' -> None)."""
    # Tìm số đứng một mình ở cuối hoặc sau chữ 'SPORTS', 'CHANNEL', ...
    # Pattern: số nguyên (1-999) có thể có khoảng trắng xung quanh
    match = re.search(r'\b(\d+)\b', name)
    if match:
        return int(match.group(1))
    return None

def contains_country(stream_name, country_code):
    """Kiểm tra stream_name có chứa country_code dưới dạng tiền tố/hậu tố hay không."""
    if not country_code:
        return True  # không yêu cầu quốc gia -> luôn đúng
    # Các pattern thường gặp: US:, US -, |US|, (US), [US], US space
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
    return False

def clean_stream_name(name):
    """Loại bỏ các tag phổ biến (┃...┃, tiền tố 2 chữ cái + : hoặc -, ...)."""
    # Giữ lại các ký tự đặc biệt cần thiết nhưng loại bỏ các tiền tố
    name = re.sub(r'┃[^┃]+┃', '', name)
    name = re.sub(r'\b[A-Z]{2}:\s*', '', name)
    name = re.sub(r'\b[A-Z]{2}\s*-\s*', '', name)
    name = re.sub(r'\[[^\]]+\]', '', name)
    # Không loại bỏ ngoặc tròn vì có thể chứa regional (East/West)
    return name.strip()

# ========== CÁC HÀM TIỆN ÍCH ==========
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
            # Bỏ qua nếu tên chứa ### (3 dấu # trở lên)
            if re.search(r'#{3,}', name):
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
                # Lưu thêm clean_name và channel_number
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

    # 4. Chuẩn bị fuzzy matcher
    print("[4] Khởi tạo FuzzyMatcher...")
    matcher = FuzzyMatcher(plugin_dir=None, match_threshold=FUZZY_THRESHOLD)
    # Dùng clean_name để precompute
    clean_names = [s['clean_name'] for s in hd_streams if s['clean_name']]
    matcher.precompute_normalizations(
        clean_names,
        user_ignored_tags=IGNORE_TAGS,
        ignore_quality=True,
        ignore_regional=True,
        ignore_geographic=True,
        ignore_misc=True
    )

    # 5. Xây dựng danh sách các kênh từ schedule (kèm country)
    # Mỗi mục: (channel_name, country_code, league)
    channel_requests = []
    for day_data in schedule_data.get('days', {}).values():
        for game in day_data.get('games', []):
            league = game.get('league', '')
            for tv_item in game.get('tv_channels', []):
                country_raw = tv_item.get('country', '')
                country_code = normalize_country(country_raw)
                for ch_name in tv_item.get('channels', []):
                    ch_name = ch_name.strip()
                    if ch_name:
                        channel_requests.append((ch_name, country_code, league))
    # Loại bỏ trùng lặp để tối ưu (giữ lại country đầu tiên gặp)
    unique_reqs = {}
    for ch, cc, lg in channel_requests:
        if ch not in unique_reqs:
            unique_reqs[ch] = (cc, lg)
    print(f"  Số kênh cần tìm: {len(unique_reqs)}")

    # 6. Fuzzy matching có xét số kênh và quốc gia
    channel_to_streams = {}  # channel_name -> list of (stream, score)
    for ch_name, (country_code, league) in unique_reqs.items():
        best_matches = []
        # Lấy số từ channel_name trong schedule
        expected_number = extract_channel_number(ch_name)
        for stream in hd_streams:
            # Kiểm tra quốc gia
            if country_code and not contains_country(stream['name'], country_code):
                continue
            # Kiểm tra số kênh
            if expected_number is not None and stream['channel_number'] != expected_number:
                continue
            # Fuzzy match trên clean_name
            match_name, score, _ = matcher.fuzzy_match(
                query_name=ch_name,
                candidate_names=[stream['clean_name']],
                user_ignored_tags=IGNORE_TAGS,
                ignore_quality=True,
                ignore_regional=True,
                ignore_geographic=True,
                ignore_misc=True
            )
            if match_name and score >= FUZZY_THRESHOLD:
                best_matches.append((stream, score))
        if best_matches:
            best_matches.sort(key=lambda x: x[1], reverse=True)
            channel_to_streams[ch_name] = best_matches[:MAX_STREAMS_PER_CHANNEL]
            print(f"  ✓ {ch_name} (country={country_code}) -> {len(best_matches)} stream")
        else:
            print(f"  ✗ {ch_name} (country={country_code}) -> không khớp")

    # 7. Kiểm tra sống hàng loạt
    all_candidate_streams = []
    for streams in channel_to_streams.values():
        for stream, _ in streams:
            all_candidate_streams.append(stream)
    print(f"\n[5] Kiểm tra sống {len(all_candidate_streams)} stream...")
    alive_urls = check_alive_batch(all_candidate_streams)
    print(f"  Số stream sống: {len(alive_urls)}")

    # 8. Tạo kết quả theo từng trận (riêng Tennis deduplicate toàn bộ)
    print("[6] Tạo file M3U...")
    results = []          # list các dòng output
    tennis_seen_urls = set()   # dùng cho Tennis để deduplicate toàn bộ

    for day_data in schedule_data.get('days', {}).values():
        for game in day_data.get('games', []):
            match_name = game.get('match', '').strip()
            league = game.get('league', '')
            if not match_name:
                continue
            seen_urls_in_game = set()
            for tv_item in game.get('tv_channels', []):
                country_raw = tv_item.get('country', '')
                country_code = normalize_country(country_raw)
                for ch_name in tv_item.get('channels', []):
                    ch_name = ch_name.strip()
                    if ch_name not in channel_to_streams:
                        continue
                    for stream, score in channel_to_streams[ch_name]:
                        if stream['url'] not in alive_urls:
                            continue
                        # Xử lý Tennis: nếu đã có URL trong toàn bộ thì bỏ qua
                        if league == "Tennis":
                            if stream['url'] in tennis_seen_urls:
                                continue
                            tennis_seen_urls.add(stream['url'])
                        else:
                            if stream['url'] in seen_urls_in_game:
                                continue
                            seen_urls_in_game.add(stream['url'])
                        results.append({
                            'match_name': match_name,
                            'channel_name': stream['name'],
                            'stream_url': stream['url'],
                            'headers': stream['headers'],
                            'league': league,
                            'quality': stream['quality']
                        })
    print(f"  Số dòng M3U tạo được: {len(results)}")

    # 9. Ghi file
    with open(OUTPUT_M3U, 'w', encoding='utf-8') as out:
        out.write('#EXTM3U\n')
        for item in results:
            group = LEAGUE_TO_GROUP.get(item['league'], item['league'])
            display_title = f"{item['match_name']} ({item['channel_name']})"
            out.write(f'#EXTINF:-1 group-title="{group}",{display_title}\n')
            for key, val in item['headers'].items():
                out.write(f'#EXTVLCOPT:{key}={val}\n')
            out.write(f'{item["stream_url"]}\n')
        out.write('\n')

    print(f"✅ Hoàn tất! Đã tạo {OUTPUT_M3U}.")

if __name__ == '__main__':
    main()
