#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tạo Stream_live.m3u từ schedule.json và danh sách M3U.
- Hiển thị tên trận đấu kèm tên kênh trong ngoặc.
- Cho phép cùng kênh xuất hiện ở nhiều trận khác nhau.
- Trong cùng một trận, loại bỏ các stream trùng URL.
- Kiểm tra stream sống trước khi thêm.
"""

import json
import re
import os
import sys
import requests
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

# Import fuzzy_matcher
try:
    from fuzzy_matcher import FuzzyMatcher
except ImportError:
    print("Lỗi: Không tìm thấy fuzzy_matcher.py. Hãy đặt cùng thư mục.")
    sys.exit(1)

# ==================== CẤU HÌNH ====================
SCHEDULE_URL = "https://raw.githubusercontent.com/Love4vn/Live-Schedue/refs/heads/1/schedule.json"
M3U_LIST_FILE = "M3U_list.txt"
OUTPUT_M3U = "Stream_live.m3u"

FUZZY_THRESHOLD = 85
IGNORE_TAGS = [
    "[Backup]", "(SD)", "[SD]", "SD", "Low", "480p", "576p",
    "┃CANAL+┃", "┃NL┃", "UK:", "US:", "DE:", "FR:",
    "[line.tivi-ott.net]", "[ktkguru.com]"
]

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

# Chất lượng
HD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:4K|UHD|FHD|Full[ -]?HD|HD|1080|720)(?=\W|$)', re.IGNORECASE)
SD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:SD|480|576|Low|Backup|Dead)(?=\W|$)', re.IGNORECASE)

# Timeout kiểm tra stream (giây)
CHECK_TIMEOUT = 10
# Số luồng tải M3U và kiểm tra
MAX_WORKERS = 10
# =================================================

def download_text(url, timeout=30):
    """Tải nội dung text từ URL."""
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
    """Phân tích M3U, trả về danh sách stream dict."""
    streams = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            parts = line.split(',', 1)
            name = parts[1].strip() if len(parts) > 1 else ""
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
                # Ước lượng chất lượng
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
                streams.append({
                    'name': name,
                    'url': url_line,
                    'headers': headers,
                    'quality': quality
                })
            i = j + 1
        else:
            i += 1
    return streams

def load_all_m3us(m3u_urls):
    """Tải song song các M3U, gộp stream."""
    all_streams = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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
    """Chỉ giữ chất lượng HD+."""
    return [s for s in streams if s['quality'] != 'SD']

def check_stream_alive(stream):
    """Kiểm tra stream có sống không bằng HEAD request với headers."""
    url = stream['url']
    headers = {}
    # Chuyển đổi headers từ dict
    for k, v in stream['headers'].items():
        if k == 'http-user-agent':
            headers['User-Agent'] = v
        elif k == 'http-cookie':
            headers['Cookie'] = v
        elif k == 'http-header':
            # Dạng "Authorization: Bearer xxx"
            if ': ' in v:
                h_name, h_val = v.split(': ', 1)
                headers[h_name] = h_val
            else:
                headers['Authorization'] = v
        else:
            headers[k] = v
    try:
        # Dùng HEAD để tiết kiệm băng thông
        resp = requests.head(url, headers=headers, timeout=CHECK_TIMEOUT, allow_redirects=True)
        if resp.status_code in (200, 206, 302, 304):
            return True
        # Thử GET với stream=True nếu HEAD không hỗ trợ
        resp = requests.get(url, headers=headers, timeout=CHECK_TIMEOUT, stream=True)
        if resp.status_code in (200, 206):
            # Đọc một phần nhỏ để xác nhận
            for _ in resp.iter_content(1024):
                break
            return True
    except Exception:
        pass
    return False

def main():
    print("=== BẮT ĐẦU TẠO Stream_live.m3u ===\n")

    # 1. Tải schedule.json
    print("[1] Tải lịch thi đấu...")
    schedule_json_str = download_text(SCHEDULE_URL)
    if not schedule_json_str:
        print("Lỗi: Không thể tải schedule.json. Thoát.")
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

    # 3. Tải và phân tích M3U
    print("[3] Tải M3U...")
    all_streams = load_all_m3us(m3u_urls)
    print(f"  Tổng stream: {len(all_streams)}")
    hd_streams = filter_hd_streams(all_streams)
    print(f"  Stream HD+: {len(hd_streams)}")

    # 4. Khởi tạo FuzzyMatcher
    print("[4] Khởi tạo FuzzyMatcher...")
    matcher = FuzzyMatcher(plugin_dir=None, match_threshold=FUZZY_THRESHOLD)
    stream_names = [s['name'] for s in hd_streams]
    matcher.precompute_normalizations(
        stream_names,
        user_ignored_tags=IGNORE_TAGS,
        ignore_quality=True,
        ignore_regional=True,
        ignore_geographic=True,
        ignore_misc=True
    )

    # 5. Duyệt từng trận đấu, tìm stream cho từng kênh
    print("[5] Xử lý từng trận đấu...")
    results = []  # list các dict cho mỗi dòng M3U
    game_counter = 0
    for day_key, day_data in schedule_data.get('days', {}).items():
        for game in day_data.get('games', []):
            game_counter += 1
            match_name = game.get('match', '').strip()
            league = game.get('league', '')
            if not match_name:
                continue
            # Dùng set để deduplicate URL trong cùng trận
            seen_urls = set()
            tv_channels_list = game.get('tv_channels', [])
            for tv_item in tv_channels_list:
                for channel_name in tv_item.get('channels', []):
                    channel_name = channel_name.strip()
                    if not channel_name:
                        continue
                    # Tìm tất cả stream khớp với channel_name
                    matched_streams = []
                    for stream in hd_streams:
                        match_name_found, score, _ = matcher.fuzzy_match(
                            query_name=channel_name,
                            candidate_names=[stream['name']],
                            user_ignored_tags=IGNORE_TAGS,
                            ignore_quality=True,
                            ignore_regional=True,
                            ignore_geographic=True,
                            ignore_misc=True
                        )
                        if match_name_found and score >= FUZZY_THRESHOLD:
                            matched_streams.append((stream, score))
                    if not matched_streams:
                        continue
                    # Sắp xếp theo score giảm dần
                    matched_streams.sort(key=lambda x: x[1], reverse=True)
                    for stream, score in matched_streams:
                        # Kiểm tra trùng URL trong cùng trận
                        if stream['url'] in seen_urls:
                            continue
                        # Kiểm tra stream sống
                        print(f"    Kiểm tra {match_name} - {channel_name} ...", end=' ')
                        if not check_stream_alive(stream):
                            print("CHẾT")
                            continue
                        print("SỐNG")
                        seen_urls.add(stream['url'])
                        results.append({
                            'match_name': match_name,
                            'channel_name': stream['name'],  # tên kênh gốc
                            'stream_url': stream['url'],
                            'headers': stream['headers'],
                            'league': league,
                            'quality': stream['quality']
                        })
            if game_counter % 5 == 0:
                print(f"  Đã xử lý {game_counter} trận, tìm được {len(results)} luồng")

    print(f"\n  Tổng số luồng sau khi kiểm tra sống: {len(results)}")

    # 6. Ghi M3U
    print(f"[6] Ghi file {OUTPUT_M3U}...")
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

    print(f"✅ Hoàn tất! Đã tạo {OUTPUT_M3U} với {len(results)} luồng.")

if __name__ == '__main__':
    main()
