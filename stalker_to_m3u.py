#!/usr/bin/env python3
"""
Stalker to M3U converter – Hoàn chỉnh + kiểm tra stream thực tế + sắp xếp theo số lượng kênh
- Đọc danh sách portal từ Mac_list.txt (url,mac)
- Tự động handshake, lấy token, lấy danh sách kênh
- Kiểm tra stream thử (lấy kênh đầu tiên, gọi create_link và xác thực)
- Lưu tổng số kênh của portal
- Sắp xếp các portal đã qua kiểm tra theo số lượng kênh giảm dần, chọn 4 portal có nhiều kênh nhất
- Lọc kênh thể thao, loại bỏ kênh SD, loại trừ từ khóa (tên và group)
- Sắp xếp toàn bộ kênh từ tất cả portal theo thứ tự ưu tiên: Anh → Mỹ → Úc → New Zealand → khác
- Giữ nguyên group-title gốc, thêm tiền tố [tên_portal]
- Thêm header #EXTVLCOPT (User-Agent, Cookie, Bearer token)
- Xuất M3U playlist
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Union

import requests

# ========== CẤU HÌNH ==========
DETAILED_DEBUG = False   # Bật True để xem response chi tiết (hữu ích khi gỡ lỗi expiry)
MAX_PORTALS = 4          # Số lượng portal tối đa sẽ lấy (dựa trên số kênh nhiều nhất)

# ========== LỚP STALKER LITE ==========
class StalkerLite:
    def __init__(self, url: str, mac: str, handshake_path: str, token: str,
                 model: str = "MAG250", extras: Optional[Dict] = None):
        self.mac = mac.upper().strip()
        self.model = model
        self.token = token
        self.extras = extras or {}
        self.handshake_path = handshake_path

        # Chuẩn hóa URL
        base = url.rstrip('/')
        if base.endswith('/c'):
            base = base[:-2]
        self.base_url = base
        self.server_urls = self._build_server_urls()
        self.portal_base = self.base_url + '/c/' if '/c' in url else self.base_url + '/stalker_portal/c/'

        self.device_info = self._make_device_info()
        self.headers = self._make_headers()
        self.session = requests.Session()

    def _build_server_urls(self) -> List[str]:
        urls = []
        urls.append(self.base_url + self.handshake_path)
        if 'portal.php' in self.handshake_path:
            urls.append(self.base_url + '/server/load.php')
            urls.append(self.base_url + '/stalker_portal/server/load.php')
            urls.append(self.base_url + '/c/server/load.php')
        else:
            urls.append(self.base_url + '/portal.php')
            urls.append(self.base_url + '/stalker_portal/server/load.php')
        return list(dict.fromkeys(urls))

    def _make_device_info(self) -> Dict[str, str]:
        mac = self.mac
        sn = hashlib.md5(mac.encode()).hexdigest().upper()
        sn_cut = self.extras.get("sn_cut", sn[:13])
        device_id = self.extras.get("device_id", hashlib.sha256(mac.encode()).hexdigest().upper())
        device_id2 = self.extras.get("device_id2", device_id)
        signature = self.extras.get("signature", hashlib.sha256((sn_cut + mac).encode()).hexdigest().upper())
        return {
            "mac": mac, "sn": sn, "snCut": sn_cut,
            "deviceId": device_id, "deviceId2": device_id2,
            "signature": signature, "model": self.model,
        }

    def _make_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
            "X-User-Agent": f"Model: {self.model}; Link: WiFi",
            "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
            "Connection": "Keep-Alive",
            "Cookie": f"mac={self.mac}; stb_lang=en; timezone=GMT",
            "Referer": self.portal_base,
        }

    def _auth_headers(self) -> Dict[str, str]:
        h = self.headers.copy()
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, url: str, timeout: int = 20) -> Optional[Union[Dict, List]]:
        headers = self._auth_headers()
        try:
            resp = self.session.get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if isinstance(data, dict) and "js" in data:
                return data["js"]
            return data
        except Exception:
            return None

    def _call_api(self, params: Dict, timeout: int = 20) -> Optional[Union[Dict, List]]:
        for server_url in self.server_urls:
            base = server_url.split('?')[0]
            url = f"{base}?{requests.compat.urlencode(params)}"
            data = self._get(url, timeout)
            if data is not None:
                return data
        return None

    def get_profile(self) -> Dict[str, str]:
        if not self.token:
            return {}
        di = self.device_info
        params = {
            "type": "stb", "action": "get_profile", "hd": "1",
            "sn": di["snCut"], "stb_type": di["model"],
            "device_id": di["deviceId"], "device_id2": di["deviceId2"],
            "signature": di["signature"], "timestamp": int(time.time()),
            "metrics": json.dumps({"mac": di["mac"], "sn": di["sn"], "model": di["model"], "type": "STB"}),
            "JsHttpRequest": "1-xml",
        }
        data = self._call_api(params)
        if DETAILED_DEBUG and data:
            print(f"      [DEBUG] Profile response: {json.dumps(data, indent=2)[:1000]}")
        if not data or not isinstance(data, dict):
            return {}
        # Thử nhiều trường expiry
        expiry = (data.get("expire_billing_date") or data.get("expire_date") or
                  data.get("account_expire") or data.get("expiry") or data.get("phone") or "")
        return {
            "login": data.get("login", ""),
            "id": str(data.get("id", "")),
            "name": data.get("name") or data.get("fname", ""),
            "expiry": expiry,
        }

    def ensure_token(self) -> bool:
        prof = self.get_profile()
        return bool(prof.get("id") or prof.get("login") or prof.get("name"))

    def get_genres(self) -> Dict[str, str]:
        if not self.ensure_token():
            return {}
        params = {"type": "itv", "action": "get_genres", "JsHttpRequest": "1-xml"}
        data = self._call_api(params, timeout=30)
        if data is None:
            params["action"] = "get_all_genres"
            data = self._call_api(params, timeout=30)
        if data is None:
            return {}
        if isinstance(data, list):
            genres_list = data
        elif isinstance(data, dict):
            genres_list = data.get("data") or data
            if not isinstance(genres_list, list):
                return {}
        else:
            return {}
        out = {}
        for g in genres_list:
            if isinstance(g, dict):
                gid = str(g.get("id") or g.get("genre_id", "0"))
                title = g.get("title") or g.get("name", "General")
                out[gid] = title
        return out

    def get_channels(self) -> List[Dict[str, Any]]:
        if not self.ensure_token():
            return []
        genres = self.get_genres()
        params = {"type": "itv", "action": "get_all_channels", "JsHttpRequest": "1-xml"}
        data = self._call_api(params, timeout=120)
        raw_channels = []
        if data is not None:
            if isinstance(data, list):
                raw_channels = data
            elif isinstance(data, dict):
                raw_channels = data.get("data") or next((v for v in data.values() if isinstance(v, list)), [])
        if not raw_channels:
            raw_channels = self._fetch_channels_paginated()
        channels = []
        for i, ch in enumerate(raw_channels):
            if not isinstance(ch, dict):
                continue
            gid = str(ch.get("tv_genre_id") or ch.get("genre_id", "0"))
            channels.append({
                "id": str(ch.get("id") or ch.get("channel_id", i)),
                "name": (ch.get("name") or ch.get("title", f"Channel {i+1}")).strip(),
                "cmd": ch.get("cmd", ""),
                "logo": self._build_logo_url(ch.get("logo", "")),
                "genre_id": gid,
                "genre_name": genres.get(gid, "General"),
                "number": int(ch.get("number", i)),
            })
        return channels

    def _fetch_channels_paginated(self) -> List[Dict]:
        all_channels = []
        page = 0
        while True:
            params = {"type": "itv", "action": "get_ordered_list", "genre": "*",
                      "force_ch_link_check": "", "fav": "0", "sortby": "number",
                      "p": page, "JsHttpRequest": "1-xml"}
            data = self._call_api(params, timeout=60)
            if not data:
                break
            if isinstance(data, list):
                ch_list = data
            elif isinstance(data, dict):
                ch_list = data.get("data", [])
            else:
                break
            if not ch_list:
                break
            for ch in ch_list:
                if isinstance(ch, dict):
                    all_channels.append(ch)
            total = 0
            if isinstance(data, dict):
                total = int(data.get("total_items") or data.get("max_page_items", 0))
            if total and len(all_channels) >= total:
                break
            page += 1
            if page > 100:
                break
        return all_channels

    def create_link(self, cmd: str) -> str:
        cmd = cmd.strip()
        if cmd.lower().startswith("ffmpeg "):
            cmd = cmd[7:].strip()
        if re.match(r"^https?://", cmd, re.I) and not cmd.lower().startswith("ffrt"):
            m = re.search(r"(https?://[^\s\"']+)", cmd, re.I)
            return m.group(1) if m else cmd
        params = {"type": "itv", "action": "create_link", "cmd": cmd,
                  "forced_storage": "undefined", "disable_ad": "1", "JsHttpRequest": "1-xml"}
        data = self._call_api(params, timeout=15)
        if data and isinstance(data, dict):
            stream = data.get("cmd") or data.get("url", "")
            if stream:
                if stream.lower().startswith("ffmpeg "):
                    stream = stream[7:].strip()
                m = re.search(r"(https?://[^\s\"']+)", stream, re.I)
                return m.group(1) if m else stream
        return ""

    def _build_logo_url(self, logo: str) -> str:
        if not logo or re.match(r"^https?://", logo, re.I):
            return logo
        base = re.sub(r"/server/load\.php$", "", self.server_urls[0])
        base = re.sub(r"/portal\.php$", "", base)
        return f"{base}/misc/logos/320/{logo.lstrip('/')}"


# ========== CÁC HÀM HỖ TRỢ ==========
def parse_mac_list(filename: str) -> List[Tuple[str, str]]:
    """Đọc file Mac_list.txt (mỗi dòng: url,mac)"""
    portals = []
    with open(filename, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) >= 2:
                portals.append((parts[0].strip(), parts[1].strip()))
    return portals


def get_expiry_date(profile: dict) -> Optional[datetime]:
    """Chuyển đổi chuỗi expiry thành datetime"""
    expiry_str = profile.get("expiry", "")
    if not expiry_str:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(expiry_str, fmt)
            if dt.year > 1970:
                return dt
        except ValueError:
            continue
    try:
        ts = int(expiry_str)
        if ts > 0:
            return datetime.fromtimestamp(ts)
    except:
        pass
    return None


def try_endpoint(base_url: str, endpoint: str, mac: str, debug: bool = False) -> Optional[Tuple[str, str]]:
    """Thử một endpoint handshake, trả về (token, path) nếu thành công"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3',
        'X-User-Agent': 'Model: MAG250; Link: WiFi',
        'Cookie': f'mac={mac}; stb_lang=en; timezone=Europe/Kiev'
    }
    url = base_url.rstrip('/') + endpoint
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if debug:
            print(f"    Trying {url} -> status {resp.status_code}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        token = None
        if isinstance(data, dict):
            token = data.get('js', {}).get('token') or data.get('token') or data.get('auth_token')
        if token:
            path = endpoint.split('?')[0]
            if debug:
                print(f"      Got token: {token[:20]}... via {path}")
            return token, path
    except Exception:
        pass
    return None


def get_token_path_and_base(url: str, mac: str, debug: bool = False) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Duyệt nhiều endpoint để lấy token và đường dẫn handshake"""
    base = url.rstrip('/')
    endpoints = [
        '/server/load.php?type=stb&action=handshake&prehash=' + mac + '&token=&JsHttpRequest=1-xml',
        '/stalker_portal/server/load.php?type=stb&action=handshake&prehash=' + mac + '&token=&JsHttpRequest=1-xml',
        '/portal.php?type=stb&action=handshake&prehash=' + mac + '&token=&JsHttpRequest=1-xml',
        '/c/server/load.php?type=stb&action=handshake&prehash=' + mac + '&token=&JsHttpRequest=1-xml',
    ]
    for ep in endpoints:
        res = try_endpoint(base, ep, mac, debug)
        if res:
            token, path = res
            return token, path, base
    if base.endswith('/c'):
        base2 = base[:-2]
        for ep in endpoints:
            res = try_endpoint(base2, ep, mac, debug)
            if res:
                token, path = res
                return token, path, base2
    return None, None, None


def check_stream_url(stream_url: str, headers: Dict, debug: bool = False) -> bool:
    """Kiểm tra stream URL có trả về nội dung hợp lệ không (không bị lỗi 458, access denied, ...)"""
    try:
        headers_check = headers.copy()
        headers_check['Range'] = 'bytes=0-1024'
        resp = requests.get(stream_url, headers=headers_check, timeout=10, stream=True)
        if debug:
            print(f"      Stream test response status: {resp.status_code}")
        if resp.status_code in [200, 206]:
            content = b''
            for chunk in resp.iter_content(chunk_size=512):
                content += chunk
                if len(content) > 1024:
                    break
            content_decoded = content.decode('utf-8', errors='ignore').lower()
            error_keywords = ['access denied', 'incorrect key', 'invalid', 'error', 'forbidden', 'unauthorized', '458']
            if any(kw in content_decoded for kw in error_keywords):
                if debug:
                    print(f"      Stream test FAILED: error keyword found in response")
                return False
            if debug:
                print(f"      Stream test OK")
            return True
        else:
            if debug:
                print(f"      Stream test FAILED: HTTP {resp.status_code}")
            return False
    except Exception as e:
        if debug:
            print(f"      Stream test exception: {e}")
        return False


def test_portal(url: str, mac: str, debug: bool = False, check_stream: bool = True) -> Optional[Dict]:
    """Kiểm tra portal: handshake, lấy kênh, kiểm tra stream thử, trả về thông tin kèm số kênh"""
    if debug:
        print(f"  Getting token for {url}")
    token, handshake_path, base = get_token_path_and_base(url, mac, debug)
    if not token:
        if debug:
            print("  No token obtained")
        return None
    if debug:
        print(f"  Token obtained via {handshake_path}, creating StalkerLite")
    stalker = StalkerLite(url, mac, handshake_path, token)
    try:
        if debug:
            print("  Fetching channels...")
        channels = stalker.get_channels()
        if not channels:
            if debug:
                print("  No channels returned")
            return None
        total_channels = len(channels)
        if debug:
            print(f"  Got {total_channels} channels")
        
        # Kiểm tra stream thực tế nếu yêu cầu
        if check_stream and channels:
            test_ch = channels[0]
            stream_url = stalker.create_link(test_ch.get("cmd", ""))
            if stream_url:
                if debug:
                    print(f"  Testing stream URL (first channel): {stream_url[:100]}...")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3',
                    'Cookie': f'mac={mac}; stb_lang=en; timezone=GMT',
                }
                if token:
                    headers['Authorization'] = f'Bearer {token}'
                if check_stream_url(stream_url, headers, debug):
                    if debug:
                        print("  Stream test: OK")
                else:
                    if debug:
                        print("  Stream test: FAILED (error or inaccessible)")
                    return None
            else:
                if debug:
                    print("  Could not create stream URL for test channel")
                return None
                
    except Exception as e:
        print(f"Channel fetch failed for {url}: {e}")
        return None
    profile = stalker.get_profile()
    expiry_dt = get_expiry_date(profile)
    return {
        "url": url,
        "mac": mac,
        "stalker": stalker,
        "profile": profile,
        "expiry_date": expiry_dt,
        "total_channels": total_channels,   # Lưu tổng số kênh
    }


def generate_playlist(portals: List[Dict], output_file: str):
    """Tạo file M3U từ danh sách portal, sắp xếp toàn bộ kênh theo thứ tự ưu tiên quốc gia"""
    # ========== TỪ KHÓA LỌC ==========
    SPORTS_KEYWORDS = [
        "worldcup", "sport", "sports", "Šport", "football", "soccer", "tennis", "golf", "tudn", "telemundo", "usa network", "paramount+",
        "motorsport", "formula 1", "f1", "hub premier", "premier league", "viaplay", "disney+ premium",
        "monomax", "astro arena", "spotv", "epl", "tsn", "la liga", "laliga", "bundesliga",
        "seriea", "serie a", "uefa", "arsenal", "aston villa", "bournemouth",
        "brentford", "brighton", "chelsea", "crystal palace", "everton", "fulham", "leeds united", "liverpool",
        "manchester city", "manchester united", "newcastle", "nottingham forest", "sunderland", "tottenham hotspur",
        "west ham united", "wolverhampton", "bayern", "bayern munich", "borussia dortmund", "bayer leverkusen", "inter milan",
        "ac milan", "napoli", "barcelona", "real madrid", "atlético", "atletico madrid", "psg", "paris saint-germain", "olympique marseille",
        "antena", "bbc1", "bbc one", "bbc 1", "bnt", "hrt", "ictimai", "itv", "m6", "npo", "orf", "rai", "rsi la", "rtbf la", "rtk", "rts", "RTVE", "RTÉ", "Rustavi", "RUV Sjonvarpid", "SABC", "Sigma TV", "Slovenija TV", "STV Scotland", "UTV Northern Ireland", "VRT 1 Belgium", "ZDF Deutschland"
    ]
    EXCLUDE_KEYWORDS = [
        "baseball", "cricket", "nfl", "nhl", "rugby", "basketball", "bóng rổ",
        "handball", "bóng ném", "hockey", "khúc côn cầu", "bóng bầu dục",
        "u23", "u21", "u19", "youth", "junior", "reserve", "mma",
        "second division", "liga 2", "serie b", "2. bundesliga", "championship", "national league", "replay", "film", "movie",
        "kurd", "iran", "iraq", "libya", "egypt", "peru", "afghanistan", "kuwait", "saudi", "oman", "cinema", "entertainment", "horse", "hindi", "azam east africa", "burkina faso", "nigeria", "cineplay", "bangla", "philipine", "caribbean", "caribbiean", "basket", "de efl", "scottish", "de gfl", "de chl", "de free", "de del", "costa rica", "uruguay", "venezuela", "fanduel sports network", "flo college", "flo football", "flow", "azam network", "som iptv", "bolivia", "south korea", "ecuador", "comedy", "reality", "netflix on air", "oneplay", "abc 6", "milb", "nfhs", "24/7", "bbc iplayer", "cameroon", "chile", "jordan", "palestine", "andere", "cineplex", "US - BeIN SPORTS 7 HD", "┃USA┃ BEIN SPORTS 7 HD", "UK - SUPERSPORT FOOTBALL HD", "┃UK┃ SUPERSPORT FOOTBALL HD", "supersport"
    ]
    SD_KEYWORDS = ["sd", "576p", "480p", "360p"]

    def get_country_priority(channel_name: str, group_name: str) -> int:
        """Trả về mức độ ưu tiên: 0 (Anh), 1 (Mỹ), 2 (Úc), 3 (New Zealand), 4 (khác)"""
        text = (channel_name + " " + group_name).lower()
        if any(kw in text for kw in ["uk", "england", "british", "bbc", "itv", "sky sports uk"]):
            return 0
        if any(kw in text for kw in ["us", "usa", "america", "american", "espn", "nbc", "cbs", "abc", "fox sports"]):
            return 1
        if any(kw in text for kw in ["australia", "aussie", "foxtel", "optus"]):
            return 2
        if any(kw in text for kw in ["new zealand", "nz", "spark sport"]):
            return 3
        return 4

    # Danh sách chứa tất cả kênh thể thao từ các portal
    all_sport_items = []

    for portal in portals:
        print(f"Processing portal: {portal['url']} (total channels: {portal.get('total_channels', 0)})")
        stalker = portal["stalker"]
        mac = portal["mac"]
        token = stalker.token
        portal_short = portal['url'].replace('http://', '').replace('https://', '').split('/')[0].replace(':80', '')
        try:
            channels = stalker.get_channels()
            print(f"  Total channels: {len(channels)}")
            sport_channels = []
            for ch in channels:
                if not isinstance(ch, dict):
                    continue
                name = ch.get("name", "")
                name_lower = name.lower()
                group_lower = ch.get("genre_name", "").lower()

                if any(kw.lower() in name_lower for kw in EXCLUDE_KEYWORDS) or any(kw.lower() in group_lower for kw in EXCLUDE_KEYWORDS):
                    continue
                if any(sd in name_lower for sd in SD_KEYWORDS):
                    continue
                if any(kw.lower() in name_lower for kw in SPORTS_KEYWORDS) or any(kw.lower() in group_lower for kw in SPORTS_KEYWORDS):
                    sport_channels.append(ch)

            print(f"  Sport channels (after filtering): {len(sport_channels)}")
            for ch in sport_channels:
                all_sport_items.append({
                    "channel": ch,
                    "stalker": stalker,
                    "mac": mac,
                    "token": token,
                    "portal_short": portal_short
                })
        except Exception as e:
            print(f"  Error while processing portal: {e}")

    # Sắp xếp toàn bộ danh sách theo thứ tự ưu tiên quốc gia
    all_sport_items.sort(key=lambda item: get_country_priority(
        item["channel"].get("name", ""),
        item["channel"].get("genre_name", "")
    ))

    # Ghi file M3U
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"# Generated at {datetime.now().isoformat()}\n\n")

        for item in all_sport_items:
            ch = item["channel"]
            stalker = item["stalker"]
            mac = item["mac"]
            token = item["token"]
            portal_short = item["portal_short"]

            stream_url = stalker.create_link(ch.get("cmd", ""))
            if not stream_url:
                print(f"    Failed to get stream URL for {ch.get('name')}")
                continue

            user_agent = "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
            cookie = f"mac={mac}; stb_lang=en; timezone=GMT"
            auth_header = f"Bearer {token}" if token else ""

            def esc(s: str) -> str:
                return s.replace('"', "&quot;")

            original_group = ch.get("genre_name", "General")
            new_group = f"[{portal_short}] {original_group}"

            f.write(
                f'#EXTINF:-1 tvg-id="{esc(ch.get("id", ""))}" '
                f'tvg-name="{esc(ch.get("name", ""))}" '
                f'tvg-logo="{esc(ch.get("logo", ""))}" '
                f'group-title="{esc(new_group)}" '
                f'tvg-chno="{ch.get("number", 0)}",{esc(ch.get("name", ""))}\n'
            )
            f.write(f'#EXTVLCOPT:http-user-agent={user_agent}\n')
            f.write(f'#EXTVLCOPT:http-cookie={cookie}\n')
            if auth_header:
                f.write(f'#EXTVLCOPT:http-header=Authorization: {auth_header}\n')
            f.write(f"{stream_url}\n")

    print(f"Total sport channels across all portals: {len(all_sport_items)}")


# ========== MAIN ==========
def main():
    global DETAILED_DEBUG
    DETAILED_DEBUG = False   # Đổi thành True nếu muốn xem response profile

    mac_file = sys.argv[1] if len(sys.argv) > 1 else "Mac_list.txt"
    if not os.path.exists(mac_file):
        print(f"Error: {mac_file} not found.")
        sys.exit(1)

    portals = parse_mac_list(mac_file)
    print(f"Found {len(portals)} portals in {mac_file}")

    valid = []
    for url, mac in portals:
        print(f"Testing {url} {mac}")
        res = test_portal(url, mac, debug=True, check_stream=True)
        if res:
            valid.append(res)
            expiry = res["expiry_date"].strftime("%Y-%m-%d") if res["expiry_date"] else "unknown"
            print(f"  -> Active, total channels: {res['total_channels']}, expiry: {expiry}")
        else:
            print("  -> Failed or expired (stream test may have failed)")

    # Sắp xếp theo số lượng kênh giảm dần (portal nào nhiều kênh nhất đứng đầu)
    valid.sort(key=lambda x: x.get("total_channels", 0), reverse=True)
    top = valid[:MAX_PORTALS]   # Lấy tối đa MAX_PORTALS portal có nhiều kênh nhất
    print(f"\nSelected {len(top)} portal(s) with most channels:")
    for p in top:
        expiry_str = p["expiry_date"].strftime("%Y-%m-%d") if p["expiry_date"] else "unknown"
        print(f"  {p['url']} – total channels: {p.get('total_channels', 0)}, expires {expiry_str}")

    if not top:
        print("No active portals found. Exiting.")
        sys.exit(0)

    output = "Mac_playlist.m3u"
    generate_playlist(top, output)
    print(f"\nPlaylist saved to {output}")


if __name__ == "__main__":
    main()
