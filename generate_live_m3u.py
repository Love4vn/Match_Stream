import requests
import re
import json
import subprocess
import os
from datetime import datetime
from fuzzy_matcher import FuzzyMatcher   # Giữ nguyên file bạn có

# ================== CẤU HÌNH ==================
SCHEDULE_URL = "https://raw.githubusercontent.com/Love4vn/Live-Schedue/refs/heads/1/schedule.json"
OUTPUT_FILE = "Stream_live.m3u"
FFPROBE_PATH = "/usr/bin/ffprobe"        # GitHub Actions dùng đường dẫn này
TIMEOUT = 8                              # Giây kiểm tra mỗi stream
PROBE_DURATION = 4                       # Giây phân tích stream

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
    "International Friendly": "Live International Friendly",
}

def load_schedule():
    r = requests.get(SCHEDULE_URL, timeout=15)
    r.raise_for_status()
    return r.json()

def load_all_m3u_streams():
    with open("M3U_list.txt", "r", encoding="utf-8") as f:
        m3u_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    all_streams = []
    matcher = FuzzyMatcher()

    for url in m3u_urls:
        try:
            print(f"Đang tải: {url}")
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            content = r.text

            lines = content.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("#EXTINF"):
                    name_match = re.search(r',(.+)$', line)
                    name = name_match.group(1).strip() if name_match else "Unknown"

                    i += 1
                    if i < len(lines) and lines[i].startswith("http"):
                        url_stream = lines[i].strip()
                        all_streams.append({"name": name, "url": url_stream, "raw_line": line})
                i += 1
        except Exception as e:
            print(f"Lỗi tải {url}: {e}")

    print(f"Đã tải {len(all_streams)} stream từ tất cả M3U")
    return all_streams

def check_stream_alive(stream_url, timeout=TIMEOUT, probe_duration=PROBE_DURATION):
    """Kiểm tra stream sống bằng ffprobe (giống IPTV Checker plugin)"""
    cmd = [
        FFPROBE_PATH,
        '-v', 'quiet',
        '-print_format', 'json',
        '-timeout', str(timeout * 1000000),
        '-analyzeduration', str(probe_duration * 1000000),
        '-probesize', '10000000',
        '-show_streams',
        stream_url
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + probe_duration + 3)
        if result.returncode != 0:
            return False, None

        data = json.loads(result.stdout)
        video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
        if not video or not video.get('width'):
            return False, None

        width = video.get('width', 0)
        height = video.get('height', 0)
        fps_str = video.get('r_frame_rate', '0/1')
        try:
            num, den = map(int, fps_str.split('/'))
            fps = num / den if den else 0
        except:
            fps = 0

        if width >= 3840:
            quality = "4K"
        elif width >= 1920:
            quality = "FHD"
        elif width >= 1280:
            quality = "HD"
        else:
            quality = "SD"

        return True, {"quality": quality, "width": width, "height": height, "fps": round(fps, 1)}

    except Exception:
        return False, None

def main():
    print("=== BẮT ĐẦU TẠO Stream_live.m3u + IPTV Checker ===")
    
    schedule = load_schedule()
    all_streams = load_all_m3u_streams()
    matcher = FuzzyMatcher()

    m3u_content = "#EXTM3U\n"
    added = 0

    for match in schedule:
        league = match.get("league", "")
        match_name = match.get("match", "")
        tv_list = match.get("tv_channels", [])

        group_title = LEAGUE_TO_GROUP.get(league, f"Live {league}")

        for item in tv_list:
            for ch_name in item.get("channels", []):
                # Fuzzy match
                best_match = None
                best_score = 0

                for stream in all_streams:
                    score = matcher.calculate_similarity(ch_name, stream["name"])
                    if score > best_score:
                        best_score = score
                        best_match = stream

                if best_match and best_score >= 75:
                    # Kiểm tra stream sống
                    is_alive, info = check_stream_alive(best_match["url"])
                    if not is_alive:
                        continue  # Bỏ stream chết

                    # Tạo tên đẹp
                    quality = info["quality"] if info else ""
                    display_name = f"{match_name} ({ch_name})"
                    if quality:
                        display_name += f" [{quality}]"

                    m3u_content += f'#EXTINF:-1 group-title="{group_title}",{display_name}\n'
                    m3u_content += f'{best_match["url"]}\n'
                    added += 1

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(m3u_content)

    print(f"✅ HOÀN THÀNH! Đã tạo {added} stream sống trong {OUTPUT_FILE}")
    print("   Đã lọc dead streams + thêm quality tag.")

if __name__ == "__main__":
    main()
