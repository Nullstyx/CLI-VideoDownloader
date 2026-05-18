import os
import re
import sys
import time
from pathlib import Path

import questionary
import yt_dlp
from yt_dlp.utils import DownloadError

CONFIG_PATH = Path(__file__).resolve().parent / "config.env"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOADS = str(SCRIPT_DIR)
OUTTMPL = "%(title)s.%(ext)s"
STANDARD_RESOLUTIONS = [4320, 2160, 1440, 1080, 720, 480, 360, 240, 144]

YOUTUBE_OPTS = {
    "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
}


def load_config():
    cfg = {
        "PROXY_ENABLED": "0",
        "PROXY_URL": "",
        "DOWNLOAD_PATH": "",
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key in cfg:
                    cfg[key] = value
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        for k, v in cfg.items():
            f.write(f'{k}={v}\n')


def ensure_config():
    if not CONFIG_PATH.exists():
        save_config(load_config())


def get_proxy_url(cfg):
    if cfg.get("PROXY_ENABLED") != "1":
        return None
    raw = (cfg.get("PROXY_URL") or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def get_download_dir(cfg):
    path = (cfg.get("DOWNLOAD_PATH") or "").strip() or DEFAULT_DOWNLOADS
    os.makedirs(path, exist_ok=True)
    return path


def _ydl_opts(proxy_url, **kwargs):
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


def download_audio(url, out_dir, proxy_url):
    opts = _ydl_opts(
        proxy_url,
        format="bestaudio/best",
        outtmpl=os.path.join(out_dir, OUTTMPL),
        postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
        quiet=False,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _parse_height(f):
    h = f.get("height")
    if h is not None:
        try:
            return int(h)
        except (TypeError, ValueError):
            pass
    res = f.get("resolution") or f.get("format_note") or ""
    if isinstance(res, str):
        m = re.search(r"(\d+)\s*[x×]\s*(\d+)", res)
        if m:
            return int(m.group(2))
        m = re.search(r"(\d{3,4})\s*[pP]", res)
        if m:
            return int(m.group(1))
    return None


def _format_height(f):
    return _parse_height(f) or 0


def _collect_heights(info):
    out = set()
    for f in info.get("formats") or []:
        if f.get("vcodec") in (None, "none"):
            continue
        h = _parse_height(f)
        if h and 72 <= h <= 4320:
            out.add(h)
    return out


def get_available_heights(url, proxy_url):
    for opts_extra in (YOUTUBE_OPTS, {}):
        opts = _ydl_opts(proxy_url, quiet=True, no_warnings=True, **opts_extra)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    heights = _collect_heights(info)
                    if heights:
                        return heights
        except Exception:
            pass
    return set()


def _pick_best_stream(streams, max_height):
    eligible = [f for f in streams if _format_height(f) <= max_height]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda f: (_format_height(f), (f.get("vcodec") or "").startswith("avc")),
    )


def _pick_best_audio(audio_only):
    preferred = next(
        (
            f
            for f in audio_only
            if f.get("ext") in ("m4a", "mp4") or (f.get("acodec") or "").startswith("mp4a")
        ),
        None,
    )
    return preferred or next(iter(audio_only), None)


def _merge_streams(video, audio):
    return {
        "format_id": f"{video['format_id']}+{audio['format_id']}",
        "ext": video.get("ext") or "mp4",
        "requested_formats": [video, audio],
        "protocol": f"{video.get('protocol', 'unknown')}+{audio.get('protocol', 'unknown')}",
    }


def _make_format_selector(max_height):
    def format_selector(ctx):
        formats = ctx.get("formats", [])[::-1]
        video_only = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none"]
        audio_only = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"]
        combined = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]

        best_video = _pick_best_stream(video_only, max_height)
        best_audio = _pick_best_audio(audio_only)
        if best_video and best_audio:
            yield _merge_streams(best_video, best_audio)
            return

        combined_pick = _pick_best_stream(combined, max_height)
        if combined_pick:
            yield combined_pick
            return

        fallback = next((f for f in combined if _format_height(f) <= max_height), None)
        if not fallback:
            fallback = next((f for f in formats if f.get("vcodec") != "none"), None)
        if fallback:
            yield fallback

    return format_selector


def download_video(url, out_dir, height, proxy_url):
    opts = _ydl_opts(
        proxy_url,
        format=_make_format_selector(height),
        merge_output_format="mp4",
        outtmpl=os.path.join(out_dir, OUTTMPL),
        quiet=False,
        **YOUTUBE_OPTS,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _download_with_feedback(download_fn, url, out_dir, proxy_url):
    try:
        download_fn(url, out_dir, proxy_url)
        print("Download completed successfully.")
    except DownloadError as e:
        print(f"Download failed: {e}")
    time.sleep(3)


def run_start(cfg):
    url = questionary.text("Enter YouTube URL (Ctrl+C to return to menu):").ask()
    if not url or not url.strip():
        return
    url = url.strip()
    proxy_url = get_proxy_url(cfg)
    choice = questionary.select(
        "Download (Ctrl+C to return to menu)",
        choices=["Download full video", "Download audio only"],
    ).ask()
    if choice is None:
        return
    out_dir = get_download_dir(cfg)
    if choice == "Download audio only":
        _download_with_feedback(download_audio, url, out_dir, proxy_url)
        return
    available = get_available_heights(url, proxy_url)
    if available:
        res_choices = [f"{r}p" for r in STANDARD_RESOLUTIONS if any(h >= r for h in available)]
        res_prompt = "Select resolution (Ctrl+C to return to menu)"
    else:
        res_choices = [f"{r}p" for r in STANDARD_RESOLUTIONS]
        res_prompt = (
            "Select resolution (Ctrl+C to return to menu)\n\n"
            "Note: actual resolutions could not be retrieved; this may cause an error."
        )
    if not res_choices:
        print("Could not get available resolutions for this video.")
        time.sleep(3)
        return
    while True:
        res_choice = questionary.select(res_prompt, choices=res_choices).ask()
        if res_choice is None:
            return
        height = int(res_choice.replace("p", ""))
        try:
            download_video(url, out_dir, height, proxy_url)
            print("Download completed successfully.")
            time.sleep(3)
            return
        except DownloadError:
            print("This resolution is not available for this video. Please choose another.")


def run_settings(cfg):
    while True:
        proxy_status = "ON" if cfg.get("PROXY_ENABLED") == "1" else "OFF"
        options = [
            f"Toggle proxy (current: {proxy_status})",
            "Set proxy (login:password@ip:port)",
            "Change download folder",
            "Reset download folder to default",
            "Back",
        ]
        choice = questionary.select("Settings", choices=options).ask()
        if choice is None or choice == "Back":
            return
        if choice.startswith("Toggle"):
            cfg["PROXY_ENABLED"] = "1" if cfg.get("PROXY_ENABLED") != "1" else "0"
            save_config(cfg)
        elif choice.startswith("Set proxy"):
            raw = questionary.text("Proxy (login:password@ip:port) (Ctrl+C to return to menu):").ask()
            if raw and raw.strip():
                cfg["PROXY_URL"] = raw.strip()
                save_config(cfg)
        elif choice == "Change download folder":
            path = questionary.text("Download folder path (Ctrl+C to return to menu):").ask()
            if path and path.strip():
                cfg["DOWNLOAD_PATH"] = os.path.expanduser(path.strip())
                save_config(cfg)
        elif choice == "Reset download folder to default":
            cfg["DOWNLOAD_PATH"] = ""
            save_config(cfg)


def main():
    ensure_config()
    cfg = load_config()
    while True:
        try:
            choice = questionary.select(
                "Main menu",
                choices=["Start", "Settings", "Exit"],
            ).ask()
            if choice is None or choice == "Exit":
                break
            if choice == "Start":
                run_start(cfg)
            else:
                run_settings(cfg)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
