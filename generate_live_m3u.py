#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tạo Stream_live.m3u với tốc độ tối ưu (kiểm tra sống song song, cache)
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
# Tối đa số stream sẽ lưu cho mỗi kênh (trong cùng một trận)
MAX_STREAMS_PER_CHANNEL = 300

# Bật/tắt kiểm tra stream sống (nếu tắt sẽ nhanh hơn rất nhiều)
CHECK_ALIVE = True
# Số luồng kiểm tra song song
ALIVE_CHECK_WORKERS = 20
# Timeout kiểm tra (giây)
CHECK_TIMEOUT = 8

# Các tag bỏ qua (chỉ thêm những tag đặc biệt chưa có trong fuzzy_matcher)
IGNORE_TAGS = [
    "[Backup]", "(SD)", "[SD]", "SD", "Low", "480p", "576p",
    "PLAY+:",   # đặc thù
]

# Mapping giải đấu -> group-title
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

# Chất lượng (giữ lại HD+)
HD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:4K|UHD|FHD|Full[ -]?HD|HD|1080|720)(?=\W|$)', re.IGNORECASE)
SD_KEYWORDS = re.compile(r'(?i)(?:^|\W)(?:SD|480|576|Low|Backup|Dead)(?=\W|$)', re.IGNORECASE)

# ========== HÀM TIỆN ÍCH ==========
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

# Cache kiểm tra sống: dict url -> bool
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
        # Thử GET stream nhẹ
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
    """Kiểm tra hàng loạt stream song song, trả về set các url sống."""
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
    stream_names = [s['name'] for s in hd_streams]
    matcher.precompute_normalizations(
        stream_names,
        user_ignored_tags=IGNORE_TAGS,
        ignore_quality=True,
        ignore_regional=True,
        ignore_geographic=True,
        ignore_misc=True
    )

    # 5. Xây dựng mapping channel -> list stream (chưa kiểm tra sống)
    print("[5] Fuzzy matching...")
    channel_to_streams = {}  # channel_name -> list of (stream, score)
    # Duyệt qua từng kênh duy nhất trong schedule (lấy từ tất cả các trận)
    all_channel_names = set()
    for day_data in schedule_data.get('days', {}).values():
        for game in day_data.get('games', []):
            for tv_item in game.get('tv_channels', []):
                for ch in tv_item.get('channels', []):
                    all_channel_names.add(ch.strip())
    print(f"  Số kênh cần tìm: {len(all_channel_names)}")
    for ch_name in all_channel_names:
        best_matches = []
        for stream in hd_streams:
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
            # Sắp xếp theo score giảm, lấy tối đa MAX_STREAMS_PER_CHANNEL
            best_matches.sort(key=lambda x: x[1], reverse=True)
            channel_to_streams[ch_name] = best_matches[:MAX_STREAMS_PER_CHANNEL]
            print(f"  ✓ {ch_name}: {len(best_matches)} stream (top {len(channel_to_streams[ch_name])})")
        else:
            print(f"  ✗ {ch_name}: không khớp")

    # 6. Kiểm tra sống hàng loạt (nếu bật)
    all_candidate_streams = []
    for streams in channel_to_streams.values():
        for stream, _ in streams:
            all_candidate_streams.append(stream)
    print(f"\n[6] Kiểm tra sống {len(all_candidate_streams)} stream (có thể mất vài phút)...")
    alive_urls = check_alive_batch(all_candidate_streams)
    print(f"  Số stream sống: {len(alive_urls)}")

    # 7. Duyệt từng trận để tạo kết quả (giữ thứ tự trận đấu)
    print("[7] Tạo file M3U...")
    results = []  # mỗi phần tử là dict cho một dòng
    for day_data in schedule_data.get('days', {}).values():
        for game in day_data.get('games', []):
            match_name = game.get('match', '').strip()
            league = game.get('league', '')
            if not match_name:
                continue
            seen_urls = set()
            for tv_item in game.get('tv_channels', []):
                for ch_name in tv_item.get('channels', []):
                    ch_name = ch_name.strip()
                    if ch_name not in channel_to_streams:
                        continue
                    for stream, score in channel_to_streams[ch_name]:
                        if stream['url'] not in alive_urls:
                            continue
                        if stream['url'] in seen_urls:
                            continue
                        seen_urls.add(stream['url'])
                        results.append({
                            'match_name': match_name,
                            'channel_name': stream['name'],
                            'stream_url': stream['url'],
                            'headers': stream['headers'],
                            'league': league,
                            'quality': stream['quality']
                        })
    print(f"  Số dòng M3U tạo được: {len(results)}")

    # 8. Ghi file
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
