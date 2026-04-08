#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tạo Stream_live.m3u từ schedule.json và danh sách M3U.
Sử dụng fuzzy_matcher để khớp kênh, lọc chất lượng >= HD.
"""

import json
import re
import os
import sys
import requests
from collections import OrderedDict
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import fuzzy_matcher (cùng thư mục)
try:
    from fuzzy_matcher import FuzzyMatcher
except ImportError:
    print("Lỗi: Không tìm thấy fuzzy_matcher.py. Hãy đặt cùng thư mục.")
    sys.exit(1)

# ==================== CẤU HÌNH ====================
SCHEDULE_URL = "https://raw.githubusercontent.com/Love4vn/Live-Schedue/refs/heads/1/schedule.json"
M3U_LIST_FILE = "M3U_list.txt"          # mỗi dòng là một URL đến file M3U
OUTPUT_M3U = "Stream_live.m3u"

# Ngưỡng fuzzy matching (0-100)
FUZZY_THRESHOLD = 85

# Các tag cần bỏ qua khi so khớp (có thể thêm)
IGNORE_TAGS = [
    "[Backup]", "(SD)", "[SD]", "SD", "Low", "480p", "576p",
    "┃CANAL+┃", "┃NL┃", "UK:", "US:", "DE:", "FR:",
    "[line.tivi-ott.net]", "[ktkguru.com]"
]

# Mapping từ league (trong schedule) sang group-title mong muốn
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

# Từ khoá chất lượng (cao hơn SD)
HD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:4K|UHD|FHD|Full[ -]?HD|HD|1080|720)(?=\W|$)', re.IGNORECASE)
SD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:SD|480|576|Low|Backup|Dead)(?=\W|$)', re.IGNORECASE)
# =================================================

def download_text(url, timeout=30):
    """Tải nội dung text từ URL, xử lý lỗi."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; M3U-Builder/1.0)'}
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        # Tự động detect encoding từ response hoặc dùng utf-8
        if resp.encoding is None:
            resp.encoding = 'utf-8'
        return resp.text
    except Exception as e:
        print(f"  Lỗi tải {url}: {e}")
        return None

def parse_m3u(content, base_url=None):
    """
    Phân tích nội dung M3U, trả về danh sách dict:
    {
        'name': tên kênh (đã trim),
        'url': link stream,
        'headers': dict từ #EXTVLCOPT,
        'quality': 'HD' / 'FHD' / '4K' / 'SD' (ước lượng)
    }
    """
    streams = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            # Lấy tên kênh (phần sau dấu phẩy cuối cùng)
            parts = line.split(',', 1)
            name = parts[1].strip() if len(parts) > 1 else ""
            # Thu thập các dòng #EXTVLCOPT phía sau
            headers = {}
            j = i + 1
            while j < len(lines) and lines[j].startswith('#EXTVLCOPT'):
                opt_line = lines[j].strip()
                if '=' in opt_line:
                    key, val = opt_line.split('=', 1)
                    key = key.replace('#EXTVLCOPT:', '').strip()
                    headers[key] = val.strip()
                j += 1
            # Dòng URL
            url_line = lines[j].strip() if j < len(lines) else ''
            # Xử lý URL relative nếu có base_url
            if url_line and not url_line.startswith('http') and base_url:
                url_line = requests.compat.urljoin(base_url, url_line)
            if url_line and not url_line.startswith('#'):
                # Ước lượng chất lượng từ tên kênh
                quality = "SD"
                if HD_KEYWORDS.search(name):
                    if re.search(r'(?i)4K|UHD', name):
                        quality = "4K"
                    elif re.search(r'(?i)FHD|Full[ -]?HD|1080', name):
                        quality = "FHD"
                    else:
                        quality = "HD"
                if SD_KEYWORDS.search(name):
                    quality = "SD"   # ghi đè nếu có từ khoá SD
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

def load_all_m3us(m3u_urls, max_workers=5):
    """Tải song song các M3U, trả về list stream (gộp)."""
    all_streams = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(download_text, url): url for url in m3u_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            content = future.result()
            if content:
                # Lấy base_url để giải relative paths
                base = url[:url.rfind('/')+1] if '/' in url else None
                streams = parse_m3u(content, base)
                print(f"  Đã tải {url}: {len(streams)} stream")
                all_streams.extend(streams)
            else:
                print(f"  Bỏ qua {url} (tải thất bại)")
    return all_streams

def collect_channels_from_schedule(json_data):
    """
    Trích xuất các cặp (channel_name, league) từ schedule.json.
    Trả về dict: channel_name -> league (league đầu tiên gặp)
    """
    channel_to_league = {}
    for day_key, day_data in json_data.get('days', {}).items():
        for game in day_data.get('games', []):
            league = game.get('league', '')
            if not league:
                continue
            for tv_item in game.get('tv_channels', []):
                for ch in tv_item.get('channels', []):
                    ch_name = ch.strip()
                    if ch_name and ch_name not in channel_to_league:
                        channel_to_league[ch_name] = league
    return channel_to_league

def filter_hd_streams(streams):
    """Chỉ giữ các stream có quality != 'SD'."""
    return [s for s in streams if s['quality'] != 'SD']

def main():
    print("=== BẮT ĐẦU TẠO Stream_live.m3u ===\n")

    # 1. Tải schedule.json
    print("[1] Đang tải lịch thi đấu...")
    schedule_json_str = download_text(SCHEDULE_URL)
    if not schedule_json_str:
        print("Lỗi: Không thể tải schedule.json. Thoát.")
        sys.exit(1)
    schedule_data = json.loads(schedule_json_str)
    channel_to_league = collect_channels_from_schedule(schedule_data)
    target_channels = list(channel_to_league.keys())
    print(f"  Tìm thấy {len(target_channels)} kênh cần khớp (unique).")

    # 2. Đọc danh sách M3U từ file M3U_list.txt
    print("\n[2] Đọc danh sách URL M3U từ M3U_list.txt...")
    if not os.path.exists(M3U_LIST_FILE):
        print(f"Lỗi: Không tìm thấy {M3U_LIST_FILE}")
        sys.exit(1)
    with open(M3U_LIST_FILE, 'r', encoding='utf-8') as f:
        m3u_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    print(f"  Có {len(m3u_urls)} URL M3U.")

    # 3. Tải tất cả M3U và phân tích
    print("\n[3] Đang tải và phân tích các file M3U...")
    all_streams = load_all_m3us(m3u_urls)
    print(f"  Tổng số stream thô: {len(all_streams)}")

    # 4. Lọc chỉ giữ HD trở lên
    hd_streams = filter_hd_streams(all_streams)
    print(f"  Số stream chất lượng HD+ : {len(hd_streams)}")

    # 5. Khởi tạo FuzzyMatcher và precompute
    print("\n[4] Khởi tạo FuzzyMatcher...")
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

    # 6. Fuzzy matching: với mỗi kênh cần tìm, lấy tất cả stream khớp (có thể nhiều nguồn)
    print("\n[5] Tiến hành fuzzy matching...")
    matched_streams = []  # list các stream đã khớp (có thể trùng tên kênh)
    for ch_name, league in channel_to_league.items():
        # Tìm tất cả stream có điểm >= ngưỡng
        best_matches = []
        for stream in hd_streams:
            # fuzzy_match trả về (match_name, score, type)
            match_name, score, _ = matcher.fuzzy_match(
                query_name=ch_name,
                candidate_names=[stream['name']],
                user_ignored_tags=IGNORE_TAGS,
                ignore_quality=True,
                ignore_regional=True,
                ignore_geographic=True,
                ignore_misc=True
            )
            if match_name and score >= FUZZY_THRESHOLD:
                best_matches.append((stream, score))
        if best_matches:
            # Sắp xếp theo score giảm dần, giữ tất cả
            best_matches.sort(key=lambda x: x[1], reverse=True)
            for stream, score in best_matches:
                matched_streams.append({
                    'stream': stream,
                    'league': league,
                    'channel_name': ch_name,
                    'score': score
                })
            print(f"  ✓ {ch_name} -> {len(best_matches)} nguồn (cao nhất {best_matches[0][1]}%)")
        else:
            print(f"  ✗ {ch_name} -> không khớp")

    print(f"\n  Tổng số stream sau khớp: {len(matched_streams)}")

    # 7. Ghi file M3U đầu ra
    print(f"\n[6] Ghi file {OUTPUT_M3U}...")
    with open(OUTPUT_M3U, 'w', encoding='utf-8') as out:
        out.write('#EXTM3U\n')
        # Để tránh trùng lặp quá nhiều, có thể nhóm theo (tên stream, league) nhưng giữ nguyên
        for item in matched_streams:
            stream = item['stream']
            league = item['league']
            group = LEAGUE_TO_GROUP.get(league, league)  # nếu không có mapping thì dùng tên gốc
            # Dòng EXTINF
            extinf = f'#EXTINF:-1 group-title="{group}"'
            # Giữ lại tvg-id, tvg-name, tvg-logo nếu có (có thể parse từ stream name hoặc dùng mặc định)
            # Ta có thể trích xuất tvg-name từ dòng #EXTINF gốc nếu cần, nhưng ở đây đơn giản
            out.write(f'{extinf},{stream["name"]}\n')
            # Ghi các dòng #EXTVLCOPT
            for key, val in stream['headers'].items():
                out.write(f'#EXTVLCOPT:{key}={val}\n')
            out.write(f'{stream["url"]}\n')
        out.write('\n')

    print(f"✅ Hoàn tất! Đã tạo {OUTPUT_M3U} với {len(matched_streams)} dòng stream.")

if __name__ == '__main__':
    main()
