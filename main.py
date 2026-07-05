import os
import json
import hashlib
import requests
import time
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.id3 import TIT2, TPE1, TALB, APIC, COMM, TXXX, TDOR, TCOM
from mutagen.flac import Picture

# ===================== 用户可自定义配置区 =====================
NAME_TEMPLATE = "{artist} - {title}"
CACHE_API_INFO = True                # 开启元数据缓存（强烈建议开启）
API_CACHE_FILE = ".netease_api_cache.json"
TASK_CACHE_FILE = ".netease_task_cache.json"  # 断点续传任务缓存文件
COVER_CACHE_DIR = ".netease_cover_cache"     # 封面图片缓存目录
REQUEST_TIMEOUT = 10
MAX_FILENAME_LEN = 120
API_CALL_INTERVAL = 1.5              # API调用间隔（秒），0表示无限制
SHOW_PROGRESS_BAR = True             # 是否显示进度条
DEBUG_MODE = False                   # 调试模式，显示详细信息（缓存命中、加入队列等）
SKIP_SCAN_AND_VALIDATE = False       # 跳过文件扫描和验证，直接处理延迟API任务
METADATA_RETRY_COUNT = 3            # 元数据写入失败时的重试次数
API_RETRY_COUNT = 3                 # API调用失败时的重试次数
UNAVAILABLE_DIR_NAME = "unavailable_songs"  # 疑似下架歌曲存放目录
SKIP_DUPLICATE_FILES = True         # 跳过已存在的重复文件
METADATA_ERROR_DIR_PREFIX = "metadata_error_"  # 元数据写入错误文件夹前缀
BACKUP_MD5_MISMATCH = False          # MD5校验失败全套备份开关（音频+切片+配置）
BACKUP_BROKEN_SLICE = False          # 残缺切片备份开关
BACKUP_UNAVAILABLE = True            # 下架歌曲备份开关
ENABLE_LOG = True                    # 是否启用日志文件输出
LOG_FILE_NAME = "decrypt_log.txt"    # 日志文件名（保存在处理根目录下）
# ==============================================================

mem_cache = {}
new_meta_count = 0
last_api_call_time = 0.0
_log_file = None

# 模拟浏览器请求头
REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://music.163.com/"
}

def init_log(root_folder: str):
    """初始化日志文件"""
    global _log_file
    if not ENABLE_LOG:
        return
    log_path = os.path.join(root_folder, LOG_FILE_NAME)
    try:
        _log_file = open(log_path, "a", encoding="utf-8")
        log_write("=" * 60)
        log_write(f"程序启动 - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        log_write("=" * 60)
    except Exception as e:
        print(f"[日志初始化失败] {str(e)}")
        _log_file = None

def log_write(message: str):
    """写入一条日志"""
    global _log_file
    if not ENABLE_LOG or _log_file is None:
        return
    try:
        timestamp = time.strftime("%H:%M:%S")
        _log_file.write(f"[{timestamp}] {message}\n")
        _log_file.flush()
    except Exception:
        pass

def close_log():
    """关闭日志文件"""
    global _log_file
    if _log_file is not None:
        try:
            log_write(f"程序结束 - {time.strftime('%Y-%m-%d %H:%M:%S')}")
            log_write("=" * 60 + "\n")
            _log_file.close()
        except Exception:
            pass
        _log_file = None

def load_api_cache_once():
    """程序启动仅执行一次加载全量缓存到内存"""
    global mem_cache
    if not CACHE_API_INFO:
        return
    if os.path.exists(API_CACHE_FILE):
        try:
            with open(API_CACHE_FILE, "r", encoding="utf-8") as f:
                mem_cache = json.load(f)
        except Exception:
            mem_cache = {}

def save_single_meta_to_cache(meta: dict):
    """获取到元数据后立即写入缓存文件，防止任务中断导致数据丢失"""
    global mem_cache, new_meta_count
    if not CACHE_API_INFO:
        return
    sid = meta["id"]
    mem_cache[sid] = meta
    new_meta_count += 1
    try:
        with open(API_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(mem_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[缓存写入失败 songID:{sid}] {str(e)}")


def load_task_cache() -> list:
    """加载断点续传任务缓存"""
    if os.path.exists(TASK_CACHE_FILE):
        try:
            with open(TASK_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_task_cache(tasks: list):
    """保存断点续传任务缓存"""
    try:
        with open(TASK_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[任务缓存写入失败] {str(e)}")


def clear_task_cache():
    """清空任务缓存"""
    if os.path.exists(TASK_CACHE_FILE):
        os.remove(TASK_CACHE_FILE)

def get_bytes_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def decrypt_slice(raw_data: bytes) -> bytes:
    xor_key = 0xA3
    return bytes(byte ^ xor_key for byte in raw_data)

def stream_decrypt_and_hash(local_slices: list, output_path: str) -> str:
    """流式读取切片，边解密边写入输出文件，同时计算MD5。
    返回最终MD5值。内存仅占用单个切片大小。"""
    md5_hasher = hashlib.md5()
    with open(output_path, "wb") as out_f:
        for _, slice_path in local_slices:
            with open(slice_path, "rb") as in_f:
                raw = in_f.read()
            decrypted = decrypt_slice(raw)
            md5_hasher.update(decrypted)
            out_f.write(decrypted)
    return md5_hasher.hexdigest()

def wait_api_interval():
    """等待API调用间隔，不阻塞不需要调用API的过程"""
    global last_api_call_time
    if API_CALL_INTERVAL <= 0:
        return
    elapsed = time.time() - last_api_call_time
    if elapsed < API_CALL_INTERVAL:
        sleep_time = API_CALL_INTERVAL - elapsed
        print(f"[等待API间隔] 等待 {sleep_time:.2f} 秒")
        time.sleep(sleep_time)

def fetch_song_meta(song_id: str) -> dict | None:
    global mem_cache, last_api_call_time
    if CACHE_API_INFO and song_id in mem_cache:
        if DEBUG_MODE:
            print(f"[缓存命中] {song_id}")
        return mem_cache[song_id]

    wait_api_interval()

    try:
        detail_url = f"https://music.163.com/api/song/detail?ids=[{song_id}]"
        resp_detail = requests.get(detail_url, headers=REQ_HEADERS, timeout=REQUEST_TIMEOUT)
        resp_detail.raise_for_status()
        last_api_call_time = time.time()
        detail_json = resp_detail.json()
        if detail_json.get("code") != 200 or len(detail_json.get("songs", [])) == 0:
            raise Exception("接口无歌曲数据")
        song_raw = detail_json["songs"][0]

        artist_names = [ar["name"] for ar in song_raw["artists"]]
        artist_str = " / ".join(artist_names)
        album_info = song_raw["album"]
        album_name = album_info["name"]
        cover_url = album_info["picUrl"]
        company = album_info.get("company", "")
        publish_ts = album_info.get("publishingTime", 0)
        song_title = song_raw["name"]
        duration_ms = song_raw["duration"]
        duration_sec = duration_ms // 1000
        h, m = divmod(duration_sec, 3600)
        m, s = divmod(m, 60)
        duration_str = f"{h:02d}:{m:02d}:{s:02d}"

        lyric_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
        resp_lyric = requests.get(lyric_url, headers=REQ_HEADERS, timeout=REQUEST_TIMEOUT)
        resp_lyric.raise_for_status()
        lyric_json = resp_lyric.json()
        lyric_text = lyric_json.get("lrc", {}).get("lyric", "")

        meta_data = {
            "id": song_id,
            "title": song_title,
            "artist": artist_str,
            "album": album_name,
            "cover_url": cover_url,
            "company": company,
            "publish_ts": publish_ts,
            "duration_ms": duration_ms,
            "duration_str": duration_str,
            "lyric": lyric_text
        }
        save_single_meta_to_cache(meta_data)
        get_cover_bin(song_id, cover_url)
        return meta_data
    except Exception as e:
        last_api_call_time = time.time()
        print(f"[API拉取失败 songID:{song_id}] {str(e)}")
        return None

def get_cover_bin(song_id: str, cover_url: str) -> bytes | None:
    if not cover_url:
        return None
    os.makedirs(COVER_CACHE_DIR, exist_ok=True)
    local_path = os.path.join(COVER_CACHE_DIR, f"{song_id}.jpg")
    if os.path.exists(local_path):
        try:
            with open(local_path, "rb") as f:
                return f.read()
        except Exception:
            pass
    try:
        resp = requests.get(cover_url, headers=REQ_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            f.write(resp.content)
        return resp.content
    except Exception:
        return None

def audio_has_existing_tag(audio_obj, tag_key: str) -> bool:
    if isinstance(audio_obj, MP3):
        return tag_key in audio_obj
    elif isinstance(audio_obj, FLAC):
        return tag_key in audio_obj
    elif isinstance(audio_obj, MP4):
        return tag_key in audio_obj
    return False

def write_audio_metadata(audio_path: str, meta: dict):
    if meta is None:
        return False
    ext = os.path.splitext(audio_path)[1].lower()
    cover_bin = get_cover_bin(meta["id"], meta["cover_url"])
    publish_year = ""
    if meta.get("publish_ts", 0) > 0:
        publish_year = str(int(meta["publish_ts"] / 1000 // 31536000 + 1970))
    try:
        if ext == ".mp3":
            audio = MP3(audio_path)
            if not audio_has_existing_tag(audio, "TIT2"):
                audio["TIT2"] = TIT2(encoding=3, text=[meta["title"]])
            if not audio_has_existing_tag(audio, "TPE1"):
                audio["TPE1"] = TPE1(encoding=3, text=[meta["artist"]])
            if not audio_has_existing_tag(audio, "TALB"):
                audio["TALB"] = TALB(encoding=3, text=[meta["album"]])
            if publish_year and not audio_has_existing_tag(audio, "TDOR"):
                audio["TDOR"] = TDOR(encoding=3, text=[publish_year])
            if meta["company"] and not audio_has_existing_tag(audio, "TCOM"):
                audio["TCOM"] = TCOM(encoding=3, text=[meta["company"]])
            if not audio_has_existing_tag(audio, "TXXX:netease_song_id"):
                audio["TXXX:netease_song_id"] = TXXX(encoding=3, desc="netease_song_id", text=[meta["id"]])
            if meta["lyric"] and not audio_has_existing_tag(audio, "COMM::zho"):
                audio["COMM::zho"] = COMM(encoding=3, lang="zho", desc="lyric", text=[meta["lyric"]])
            if cover_bin and not audio_has_existing_tag(audio, "APIC:"):
                audio["APIC:"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=cover_bin)
            audio.save()
        elif ext == ".flac":
            audio = FLAC(audio_path)
            if not audio_has_existing_tag(audio, "title"):
                audio["title"] = meta["title"]
            if not audio_has_existing_tag(audio, "artist"):
                audio["artist"] = meta["artist"]
            if not audio_has_existing_tag(audio, "album"):
                audio["album"] = meta["album"]
            if publish_year and not audio_has_existing_tag(audio, "date"):
                audio["date"] = publish_year
            if meta["company"] and not audio_has_existing_tag(audio, "publisher"):
                audio["publisher"] = meta["company"]
            if not audio_has_existing_tag(audio, "netease_song_id"):
                audio["netease_song_id"] = meta["id"]
            if meta["lyric"] and not audio_has_existing_tag(audio, "lyric"):
                audio["lyric"] = meta["lyric"]
            if cover_bin:
                pic = Picture()
                pic.data = cover_bin
                pic.mime = "image/jpeg"
                pic.type = 3
                audio.add_picture(pic)
            audio.save()
        elif ext == ".m4a":
            audio = MP4(audio_path)
            if not audio_has_existing_tag(audio, "\xa9nam"):
                audio["\xa9nam"] = meta["title"]
            if not audio_has_existing_tag(audio, "\xa9ART"):
                audio["\xa9ART"] = meta["artist"]
            if not audio_has_existing_tag(audio, "\xa9alb"):
                audio["\xa9alb"] = meta["album"]
            if publish_year and not audio_has_existing_tag(audio, "\xa9day"):
                audio["\xa9day"] = publish_year
            if meta["company"] and not audio_has_existing_tag(audio, "\xa9pub"):
                audio["\xa9pub"] = meta["company"]
            if not audio_has_existing_tag(audio, "netease_song_id"):
                audio["netease_song_id"] = meta["id"]
            if meta["lyric"] and not audio_has_existing_tag(audio, "\xa9cmt"):
                audio["\xa9cmt"] = meta["lyric"]
            audio.save()
    except Exception:
        raise
    return True

def write_audio_metadata_with_retry(audio_path: str, meta: dict, base_name: str = ""):
    """带重试的元数据写入，失败时自动重试 METADATA_RETRY_COUNT 次。
    成功返回True，失败返回错误类型字符串（异常类名）。"""
    last_error_type = None
    for attempt in range(1, METADATA_RETRY_COUNT + 1):
        try:
            write_audio_metadata(audio_path, meta)
            return True
        except Exception as e:
            last_error_type = type(e).__name__
            if attempt < METADATA_RETRY_COUNT:
                print(f"\n[元数据写入重试 {attempt}/{METADATA_RETRY_COUNT}] {base_name} [{last_error_type}] {str(e)}")
                time.sleep(1)
            else:
                print(f"\n[元数据写入失败] {base_name} 已重试 {METADATA_RETRY_COUNT} 次仍失败 [{last_error_type}] {str(e)}")
    return last_error_type


def move_to_metadata_error_dir(audio_path: str, error_type: str, root_folder: str):
    """将元数据写入失败的文件移动到以错误类型命名的文件夹"""
    error_dir = os.path.join(root_folder, METADATA_ERROR_DIR_PREFIX + error_type)
    os.makedirs(error_dir, exist_ok=True)
    error_path = os.path.join(error_dir, os.path.basename(audio_path))
    if os.path.exists(audio_path):
        os.replace(audio_path, error_path)
    return error_dir

def format_filename(template: str, meta: dict, ext_plain: str) -> str:
    illegal_chars = r'\/:*?"<>|'
    data_map = {
        "id": meta["id"],
        "title": meta["title"],
        "artist": meta["artist"],
        "album": meta["album"],
        "duration": meta["duration_ms"],
        "ext": ext_plain
    }
    raw_name = template.format(**data_map)
    for char in illegal_chars:
        raw_name = raw_name.replace(char, "")
    max_body = MAX_FILENAME_LEN - len(f".{ext_plain}") - 5
    if len(raw_name) > max_body:
        raw_name = raw_name[:max_body] + "..."
    full_name = f"{raw_name}.{ext_plain}"
    if len(full_name) > MAX_FILENAME_LEN:
        full_name = f"song_{meta['id']}.{ext_plain}"
    return full_name

def get_audio_ext(base_name: str, folder: str) -> str:
    cfg_candidates = [
        os.path.join(folder, f"{base_name}.config"),
        os.path.join(folder, f"{base_name}.txt")
    ]
    default_fmt = "mp3"
    for cfg_path in cfg_candidates:
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg_json = json.load(f)
                fmt_str = cfg_json.get("audioFormat", default_fmt).strip().lower()
                return f".{fmt_str}"
            except Exception:
                continue
    return f".{default_fmt}"

def get_all_slice_files(folder: str, base_name: str) -> list:
    slice_list = []
    for fname in os.listdir(folder):
        if fname.startswith(f"{base_name}_") and fname.endswith(".nmsf"):
            num_part = fname.replace(f"{base_name}_", "").replace(".nmsf", "")
            if num_part.isdigit():
                slice_idx = int(num_part)
                full_slice_path = os.path.join(folder, fname)
                slice_list.append((slice_idx, full_slice_path))
    slice_list.sort(key=lambda x: x[0])
    return slice_list

def scan_all_nmsfi_recursive(root_dir: str) -> list:
    result = []
    skip_dirs = {"decrypt_audio", "broken_files", "md5_mismatch_files", ".netease_cover_cache"}
    for cur_dir, _, file_list in os.walk(root_dir):
        if os.path.basename(cur_dir) in skip_dirs:
            continue
        for fn in file_list:
            if fn.endswith(".nmsfi"):
                full_path = os.path.join(cur_dir, fn)
                result.append((full_path, cur_dir))
    return result

import sys

def print_progress(current: int, total: int, stats: dict, current_song: str = ""):
    if not SHOW_PROGRESS_BAR:
        return
    bar_width = 40
    progress = current / total if total > 0 else 0
    filled = int(bar_width * progress)
    bar = "█" * filled + "░" * (bar_width - filled)
    status = f"[{bar}] {current}/{total}"
    if current_song:
        status += f" | {current_song[:30]}"
    status += f" | 成功:{stats['full_valid']}"
    status += f" | 切片失败:{stats['slice_broken']}"
    status += f" | MD5失败:{stats['md5_mismatch']}"
    status += f" | 等待API:{stats['deferred_count']}"
    
    sys.stdout.write("\r" + status + " " * 30)
    sys.stdout.flush()


def batch_decrypt_sliced_nmsf(root_folder: str):
    load_api_cache_once()
    init_log(root_folder)

    if not os.path.isdir(root_folder):
        print(f"错误：缓存根目录不存在 -> {root_folder}")
        close_log()
        return

    valid_dir = os.path.join(root_folder, "decrypt_audio")
    broken_slice_dir = os.path.join(root_folder, "broken_files")
    md5_fail_dir = os.path.join(root_folder, "md5_mismatch_files")
    os.makedirs(valid_dir, exist_ok=True)
    if BACKUP_BROKEN_SLICE:
        os.makedirs(broken_slice_dir, exist_ok=True)
    if BACKUP_MD5_MISMATCH:
        os.makedirs(md5_fail_dir, exist_ok=True)

    stats = {
        "full_valid": 0,
        "slice_broken": 0,
        "md5_mismatch": 0,
        "cfg_error": 0,
        "deferred_count": 0,
        "skipped": 0
    }

    deferred_api_tasks = []

    task_cache = load_task_cache()
    task_cache_basenames = set()
    if task_cache:
        print(f"[发现断点任务] 共 {len(task_cache)} 个待处理")
        stats["deferred_count"] = len(task_cache)
        deferred_api_tasks = task_cache
        for task in task_cache:
            task_cache_basenames.add(task["base_name"])

    if SKIP_SCAN_AND_VALIDATE:
        all_nmsfi = []
        total_count = 0
        if deferred_api_tasks:
            print(f"\n[跳过扫描验证] 直接处理 {len(deferred_api_tasks)} 个延迟API任务")
        else:
            print("[跳过扫描验证] 没有延迟API任务需要处理")
            close_log()
            return
    else:
        all_nmsfi = scan_all_nmsfi_recursive(root_folder)
        total_count = len(all_nmsfi)

        if not deferred_api_tasks:
            print(f"\n[开始处理] 共 {total_count} 个文件")
        else:
            print(f"\n[开始处理] 共 {total_count} 个文件（含 {len(task_cache)} 个断点任务）")

    for idx, (nmsfi_full_path, file_dir) in enumerate(all_nmsfi):
        file_name = os.path.basename(nmsfi_full_path)
        base_name = file_name[:-6]
        name_segments = base_name.split("_")

        if base_name in task_cache_basenames:
            continue

        local_slices = get_all_slice_files(file_dir, base_name)

        if len(name_segments) < 3 or not name_segments[0].isdigit():
            stats["cfg_error"] += 1
            print_progress(idx + 1, total_count, stats, base_name)
            print(f"\n[文件名格式异常] {file_name}")
            log_write(f"[配置错误] {base_name} - 文件名格式异常")
            if BACKUP_BROKEN_SLICE:
                for _, slice_p in local_slices:
                    dst = os.path.join(broken_slice_dir, os.path.basename(slice_p))
                    with open(slice_p, "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
            continue
        song_id = name_segments[0]
        expect_md5 = name_segments[-1]

        audio_suffix = get_audio_ext(base_name, file_dir)

        slice_config = None
        try:
            with open(nmsfi_full_path, "r", encoding="utf-8") as f:
                slice_config = json.load(f).get("slices", [])
        except Exception:
            stats["cfg_error"] += 1
            print_progress(idx + 1, total_count, stats, base_name)
            print(f"\n[nmsfi配置损坏] {file_name}")
            log_write(f"[配置错误] {base_name} - nmsfi配置损坏")
            if BACKUP_BROKEN_SLICE:
                for _, slice_p in local_slices:
                    dst = os.path.join(broken_slice_dir, os.path.basename(slice_p))
                    with open(slice_p, "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
            continue

        cfg_slice_count = len(slice_config)
        local_slice_count = len(local_slices)

        if local_slice_count != cfg_slice_count:
            stats["slice_broken"] += 1
            print_progress(idx + 1, total_count, stats, base_name)
            print(f"\n[切片数量不匹配] {base_name} 配置{cfg_slice_count}片，本地{local_slice_count}片")
            log_write(f"[切片失败] {base_name} - 数量不匹配(配置{cfg_slice_count}/本地{local_slice_count})")
            if BACKUP_BROKEN_SLICE:
                for _, slice_p in local_slices:
                    dst = os.path.join(broken_slice_dir, os.path.basename(slice_p))
                    with open(slice_p, "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
            continue

        all_slice_ok = True
        bad_slice_indices = []
        for cfg_idx, slice_cfg in enumerate(slice_config):
            std_size = slice_cfg["size"]
            slice_idx, slice_path = local_slices[cfg_idx]
            real_size = os.path.getsize(slice_path)
            if real_size != std_size:
                all_slice_ok = False
                bad_slice_indices.append((slice_idx, slice_path, std_size, real_size))
        if not all_slice_ok:
            stats["slice_broken"] += 1
            print_progress(idx + 1, total_count, stats, base_name)
            bad_slices_info = []
            for slice_idx, slice_path, std_size, real_size in bad_slice_indices:
                print(f"\n[切片{slice_idx}尺寸异常] {os.path.basename(slice_path)}")
                bad_slices_info.append(f"切片{slice_idx}({real_size}/{std_size})")
            log_write(f"[切片失败] {base_name} - 尺寸异常: {', '.join(bad_slices_info)}")
            if BACKUP_BROKEN_SLICE:
                for _, slice_p in local_slices:
                    dst = os.path.join(broken_slice_dir, os.path.basename(slice_p))
                    with open(slice_p, "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
            continue

        temp_output_path = os.path.join(valid_dir, f"{base_name}{audio_suffix}")
        real_md5 = stream_decrypt_and_hash(local_slices, temp_output_path)

        if real_md5.lower() != expect_md5.lower():
            stats["md5_mismatch"] += 1
            print_progress(idx + 1, total_count, stats, base_name)
            print(f"\n[成品MD5校验失败] {base_name}")
            log_write(f"[MD5失败] {base_name} - 预期:{expect_md5[:8]}... 实际:{real_md5[:8]}...")
            if BACKUP_MD5_MISMATCH:
                md5_fail_audio = os.path.join(md5_fail_dir, f"{base_name}{audio_suffix}")
                os.replace(temp_output_path, md5_fail_audio)
                for _, slice_p in local_slices:
                    dst = os.path.join(md5_fail_dir, os.path.basename(slice_p))
                    with open(slice_p, "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
                nmsfi_dst = os.path.join(md5_fail_dir, os.path.basename(nmsfi_full_path))
                with open(nmsfi_full_path, "rb") as fi, open(nmsfi_dst, "wb") as fo:
                    fo.write(fi.read())
                for cfg_name in [f"{base_name}.config", f"{base_name}.txt"]:
                    cfg_path = os.path.join(file_dir, cfg_name)
                    if os.path.exists(cfg_path):
                        dst = os.path.join(md5_fail_dir, cfg_name)
                        with open(cfg_path, "rb") as fi, open(dst, "wb") as fo:
                            fo.write(fi.read())
            else:
                if os.path.exists(temp_output_path):
                    os.remove(temp_output_path)
            continue

        if CACHE_API_INFO and song_id in mem_cache:
            if DEBUG_MODE:
                print(f"[缓存命中] {song_id}")
            song_meta = mem_cache[song_id]
            plain_ext = audio_suffix.lstrip(".")
            safe_filename = format_filename(NAME_TEMPLATE, song_meta, plain_ext)
            output_path = os.path.join(valid_dir, safe_filename)
            if SKIP_DUPLICATE_FILES and os.path.exists(output_path):
                if DEBUG_MODE:
                    print(f"[已跳过重复] {safe_filename}")
                stats["skipped"] += 1
                log_write(f"[已跳过] {base_name} - 目标文件已存在: {safe_filename}")
                if os.path.exists(temp_output_path):
                    os.remove(temp_output_path)
                print_progress(idx + 1, total_count, stats, base_name)
                continue
            if temp_output_path != output_path:
                os.replace(temp_output_path, output_path)
            result = write_audio_metadata_with_retry(output_path, song_meta, base_name)
            if result is not True:
                error_dir = move_to_metadata_error_dir(output_path, result, root_folder)
                print(f"\n[元数据错误] {base_name} → {METADATA_ERROR_DIR_PREFIX}{result}/")
                log_write(f"[元数据错误] {base_name} - {result}")
                stats["skipped"] += 1
                print_progress(idx + 1, total_count, stats, base_name)
                continue
            if DEBUG_MODE:
                print(f"[全部校验通过] {base_name} → {safe_filename}")
            log_write(f"[成功] {base_name} → {safe_filename} | 歌手:{song_meta['artist']} | 专辑:{song_meta['album']} | 时长:{song_meta['duration_str']}")
            stats["full_valid"] += 1
            print_progress(idx + 1, total_count, stats, base_name)
        else:
            if DEBUG_MODE:
                print(f"[加入等待队列] {base_name}")
            log_write(f"[等待API] {base_name} (song_id:{song_id})")
            stats["deferred_count"] += 1
            stats["full_valid"] += 1
            print_progress(idx + 1, total_count, stats, base_name)
            deferred_api_tasks.append({
                "song_id": song_id,
                "base_name": base_name,
                "audio_suffix": audio_suffix,
                "temp_path": temp_output_path
            })
            save_task_cache(deferred_api_tasks)

    if deferred_api_tasks:
        print(f"\n[开始处理延迟API任务] 共 {len(deferred_api_tasks)} 个")
        completed_tasks = []
        for api_idx, task in enumerate(deferred_api_tasks):
            song_id = task["song_id"]
            base_name = task["base_name"]
            audio_suffix = task["audio_suffix"]
            temp_path = task["temp_path"]

            current_progress = total_count + api_idx + 1
            total_progress = total_count + len(deferred_api_tasks)
            
            # API调用重试
            song_meta = None
            for retry_attempt in range(1, API_RETRY_COUNT + 1):
                song_meta = fetch_song_meta(song_id)
                if song_meta is not None:
                    break
                if retry_attempt < API_RETRY_COUNT:
                    print(f"\n[API重试 {retry_attempt}/{API_RETRY_COUNT}] {base_name}")
                    if API_CALL_INTERVAL > 0:
                        time.sleep(API_CALL_INTERVAL)
                else:
                    print(f"\n[API失败] {base_name} 已重试 {API_RETRY_COUNT} 次，疑似已下架")
            
            if song_meta is None:
                # 多次重试失败，处理下架歌曲
                if BACKUP_UNAVAILABLE:
                    unavailable_dir = os.path.join(root_folder, UNAVAILABLE_DIR_NAME)
                    os.makedirs(unavailable_dir, exist_ok=True)
                    unavailable_path = os.path.join(unavailable_dir, os.path.basename(temp_path))
                    if os.path.exists(temp_path):
                        os.replace(temp_path, unavailable_path)
                    print(f"\n[疑似下架] {base_name} → {UNAVAILABLE_DIR_NAME}/")
                else:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    print(f"\n[疑似下架] {base_name}（已删除）")
                log_write(f"[疑似下架] {base_name} (song_id:{song_id}) - API重试{API_RETRY_COUNT}次失败")
                print_progress(current_progress, total_progress, stats, base_name)
                completed_tasks.append(task)
            else:
                plain_ext = audio_suffix.lstrip(".")
                safe_filename = format_filename(NAME_TEMPLATE, song_meta, plain_ext)
                final_output_path = os.path.join(valid_dir, safe_filename)
                if SKIP_DUPLICATE_FILES and os.path.exists(final_output_path) and temp_path != final_output_path:
                    # 目标文件已存在，删除临时文件
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    if DEBUG_MODE:
                        print(f"\n[已跳过重复] {safe_filename}")
                    log_write(f"[已跳过] {base_name} - 目标文件已存在: {safe_filename}")
                else:
                    result = write_audio_metadata_with_retry(temp_path, song_meta, base_name)
                    if result is not True:
                        # 元数据写入失败，移动到错误文件夹
                        error_dir = move_to_metadata_error_dir(temp_path, result, root_folder)
                        print(f"\n[元数据错误] {base_name} → {METADATA_ERROR_DIR_PREFIX}{result}/")
                        log_write(f"[元数据错误] {base_name} - {result}")
                    else:
                        if temp_path != final_output_path:
                            os.replace(temp_path, final_output_path)
                            # 确保临时文件已被清理（os.replace是移动操作，正常情况不会残留）
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                        if DEBUG_MODE:
                            print(f"\n[全部校验通过] {base_name} → {safe_filename}")
                        log_write(f"[成功] {base_name} → {safe_filename} | 歌手:{song_meta['artist']} | 专辑:{song_meta['album']} | 时长:{song_meta['duration_str']}")
                stats["deferred_count"] -= 1
                print_progress(current_progress, total_progress, stats, base_name)
                completed_tasks.append(task)

        deferred_api_tasks = [t for t in deferred_api_tasks if t not in completed_tasks]
        if deferred_api_tasks:
            save_task_cache(deferred_api_tasks)
        else:
            clear_task_cache()

        print("\n\n==================== 处理完成汇总 ====================")
        print(f"共读取 .nmsfi 配置文件总数：{total_count}")
        print(f"切片完整+MD5匹配，成功导出音频：{stats['full_valid']}")
        print(f"切片数量/尺寸不匹配：{stats['slice_broken']}")
        print(f"切片完整但MD5哈希不匹配：{stats['md5_mismatch']}")
        print(f"文件名/配置文件损坏：{stats['cfg_error']}")
        print(f"已跳过（重复处理）：{stats['skipped']}")
        if BACKUP_UNAVAILABLE:
            print(f"疑似下架歌曲目录：{os.path.join(root_folder, UNAVAILABLE_DIR_NAME)}")
        else:
            print("疑似下架歌曲：已删除（备份开关关闭）")
        print(f"正常音频输出目录：{valid_dir}")
        if BACKUP_BROKEN_SLICE:
            print(f"残缺切片备份目录：{broken_slice_dir}")
        else:
            print("残缺切片备份：已关闭")
        if BACKUP_MD5_MISMATCH:
            print(f"MD5校验失败全套文件目录：{md5_fail_dir}")
        else:
            print("MD5校验失败备份：已关闭")
        if CACHE_API_INFO:
            print(f"内存缓存歌曲总数：{len(mem_cache)}，本次新增：{new_meta_count}")
        log_write("---- 处理完成汇总 ----")
        log_write(f"总文件数: {total_count} | 成功: {stats['full_valid']} | 切片失败: {stats['slice_broken']} | MD5失败: {stats['md5_mismatch']} | 配置错误: {stats['cfg_error']} | 跳过: {stats['skipped']}")
        close_log()
        return

    print("\n\n==================== 处理完成汇总 ====================")
    print(f"共读取 .nmsfi 配置文件总数：{total_count}")
    print(f"切片完整+MD5匹配，成功导出音频：{stats['full_valid']}")
    print(f"切片数量/尺寸不匹配：{stats['slice_broken']}")
    print(f"切片完整但MD5哈希不匹配：{stats['md5_mismatch']}")
    print(f"文件名/配置文件损坏：{stats['cfg_error']}")
    print(f"已跳过（重复处理）：{stats['skipped']}")
    print(f"正常音频输出目录：{valid_dir}")
    if BACKUP_BROKEN_SLICE:
        print(f"残缺切片备份目录：{broken_slice_dir}")
    else:
        print("残缺切片备份：已关闭")
    if BACKUP_MD5_MISMATCH:
        print(f"MD5校验失败全套文件目录：{md5_fail_dir}")
    else:
        print("MD5校验失败备份：已关闭")
    if CACHE_API_INFO:
        print(f"内存缓存歌曲总数：{len(mem_cache)}，本次新增：{new_meta_count}")
    log_write("---- 处理完成汇总 ----")
    log_write(f"总文件数: {total_count} | 成功: {stats['full_valid']} | 切片失败: {stats['slice_broken']} | MD5失败: {stats['md5_mismatch']} | 配置错误: {stats['cfg_error']} | 跳过: {stats['skipped']}")
    close_log()

if __name__ == "__main__":
    cache_root = r"C:\Users\yaop\Desktop\test"
    batch_decrypt_sliced_nmsf(cache_root)