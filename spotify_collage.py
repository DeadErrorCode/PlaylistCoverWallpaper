#!/usr/bin/env python3
"""
Spotify Cover Collage
=====================
Turns the album covers of Spotify playlists, albums or tracks into a desktop
wallpaper. Before the image is built, every cover can be approved and given a
priority (1-5) in a small window. Higher priority -> bigger and placed on top.

Nothing is stored between runs: every run fetches the playlists and asks for
the ratings again. Only the Spotify login is kept (.spotify_cache).

Settings live in config.json (template: config.example.json).

Requirements:
  pip install requests pillow spotipy
  A Spotify app from https://developer.spotify.com/dashboard (see README)

Examples:
  python spotify_collage.py                       # everything from config.json
  python spotify_collage.py https://open.spotify.com/playlist/XXXX
"""

import argparse
import io
import json
import math
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
CONFIG_TEMPLATE = BASE / "config.example.json"
TITLE = "Spotify Collage"
INSTALL = "pip install requests pillow spotipy"


# --------------------------------------------------------------------------
# Messages (pop-up + console)
# --------------------------------------------------------------------------
def _dialog(function, title, text):
    """Shows a tkinter dialog; returns None if no window can be opened."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return getattr(messagebox, function)(title, text, parent=root)
        finally:
            root.destroy()
    except Exception:
        return None


def notify(text, title=TITLE, kind="info"):
    print(f"[{title}] {text}", file=sys.stdout if kind == "info" else sys.stderr)
    _dialog({"info": "showinfo", "warning": "showwarning", "error": "showerror"}[kind], title, text)


def fail(text, title=f"{TITLE} – Error"):
    notify(text, title, "error")
    sys.exit(1)


try:
    import requests
    from PIL import Image, ImageFilter, ImageOps
except ImportError:
    fail(f"Some Python packages are missing. Please run:\n\n{INSTALL}")

API = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?|spotify:)"
    r"(playlist|album|track)[/:]([A-Za-z0-9]{22})"
)

# How often a cover is repeated when there are too few covers
PRIO_WEIGHT = {1: 1.0, 2: 2.0, 3: 3.5, 4: 5.5, 5: 8.0}

# Defaults for entries missing from config.json
DEFAULTS = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "playlists": [],
    "resolution": "auto",
    "rotation": 0,
    "output": "wallpaper.png",
}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        values = json.loads(CONFIG_PATH.read_text("utf-8-sig"))
    except json.JSONDecodeError as ex:
        fail(f"config.json is not valid (line {ex.lineno}, column {ex.colno}):\n{ex.msg}\n\n"
             "Common causes: a missing comma, a comma after the last entry, "
             "single instead of double quotes, \\ instead of / in paths.")
    if not isinstance(values, dict):
        fail("config.json must contain a JSON object { ... }.")
    values = {k: v for k, v in values.items() if not k.startswith("_")}  # "_note" = comment
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        notify(f"Unknown entries in config.json are ignored: {', '.join(sorted(unknown))}", kind="warning")
    cfg = {**DEFAULTS, **values}
    if isinstance(cfg["playlists"], str):
        cfg["playlists"] = [cfg["playlists"]]
    if not isinstance(cfg["rotation"], (int, float)) or cfg["rotation"] < 0:
        fail('"rotation" must be a number of at least 0 (degrees).')
    return cfg


def setup_needed(reason):
    """Creates config.json from the template if needed and explains what to fill in."""
    if not CONFIG_PATH.exists() and CONFIG_TEMPLATE.exists():
        shutil.copy(CONFIG_TEMPLATE, CONFIG_PATH)
        where = "I just created it from the template:\n"
    else:
        where = "File:\n"
    fail(f"{reason}\n\n"
         "Please fill in config.json:\n"
         '• "spotify_client_id" and "spotify_client_secret" (Spotify Developer Dashboard → your app → Settings)\n'
         '• your playlist link under "playlists"\n\n'
         f"{where}{CONFIG_PATH}\n\nInstructions: README.md",
         title=f"{TITLE} – Setup")


def parse_resolution(text):
    if str(text).lower() == "auto":
        detected = screen_resolution()
        if detected:
            print(f"Detected monitor: {detected[0]}x{detected[1]}")
            return detected
        print('Could not detect the monitor – using 2560x1440 (set "resolution" in config.json).')
        return 2560, 1440
    try:
        W, H = (int(v) for v in str(text).lower().split("x"))
        if W > 0 and H > 0:
            return W, H
    except ValueError:
        pass
    fail(f'Invalid resolution: "{text}"\n\nPlease use WIDTHxHEIGHT, e.g. "2560x1440", or "auto".')


# --------------------------------------------------------------------------
# Spotify
# --------------------------------------------------------------------------
class Spotify:
    def __init__(self, cid, sec):
        try:
            from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError
        except ImportError:
            fail(f"The spotipy package is missing. Please run:\n\n{INSTALL}")

        cache = BASE / ".spotify_cache"
        old = BASE / ".cache"  # earlier versions: spotipy's default name
        if old.exists() and not cache.exists():
            old.rename(cache)

        auth = SpotifyOAuth(
            client_id=cid,
            client_secret=sec,
            redirect_uri=REDIRECT_URI,
            scope="playlist-read-private playlist-read-collaborative",
            open_browser=True,
            cache_path=str(cache),
        )
        try:
            token = auth.get_access_token(as_dict=True)
        except SpotifyOauthError as ex:
            fail("Spotify login failed.\n\n"
                 "Please check:\n"
                 "• Are the client ID and client secret in config.json correct?\n"
                 f"• Is the redirect URI in your Spotify app exactly: {REDIRECT_URI}\n"
                 "• Is your Spotify account listed in the app under “User Management”?\n\n"
                 f"Spotify says: {ex}")

        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token['access_token']}"

    def get(self, url, params=None):
        if not url.startswith("http"):
            url = API + url
        for _ in range(6):
            r = self.s.get(url, params=params, timeout=20)
            if r.status_code == 429:  # rate limit -> wait
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
                continue
            if r.status_code in (403, 404):
                raise LookupError(f"{r.status_code} for {url}: {r.text}")
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Too many requests to Spotify (429).")


def album_entry(album):
    images = (album or {}).get("images") or []
    if not images or not album.get("id"):
        return None
    largest = max(images, key=lambda b: b.get("width") or 0)
    return {
        "id": album["id"],
        "name": album.get("name", ""),
        "artists": ", ".join(a.get("name", "") for a in album.get("artists", [])),
        "image_url": largest["url"],
    }


def parse_links(texts):
    refs = []
    for t in texts:
        for m in URL_RE.finditer(t):
            refs.append((m.group(1), m.group(2)))
    return refs


def collect_covers(sp, refs):
    """Returns (covers, failed); links that could not be fetched end up in failed."""
    covers, failed = {}, []
    for kind, sid in refs:
        try:
            if kind == "playlist":
                url = f"/playlists/{sid}/items"
                params = {"limit": 100, "additional_types": "track"}
                count = 0
                while url:
                    data = sp.get(url, params)
                    params = None  # "next" already contains the parameters
                    for item in data.get("items", []):
                        track = item.get("item") or item.get("track") or {}
                        e = album_entry(track.get("album"))
                        if e:
                            covers.setdefault(e["id"], e)
                            count += 1
                    url = data.get("next")
                print(f"  Playlist {sid}: {count} tracks")
            elif kind == "album":
                e = album_entry(sp.get(f"/albums/{sid}"))
                if e:
                    covers.setdefault(e["id"], e)
            elif kind == "track":
                e = album_entry(sp.get(f"/tracks/{sid}").get("album"))
                if e:
                    covers.setdefault(e["id"], e)
        except LookupError as ex:
            print(f"  ! {kind} {sid} could not be fetched ({ex})")
            failed.append(f"{kind} {sid}")
    return covers, failed


def download_covers(covers):
    """Loads every cover into memory (nothing is written to disk)."""
    for c in covers.values():
        r = requests.get(c["image_url"], timeout=30)
        r.raise_for_status()
        c["image"] = Image.open(io.BytesIO(r.content)).convert("RGB")


# --------------------------------------------------------------------------
# Rating window
# --------------------------------------------------------------------------
def rating_window(cover_list):
    """Returns {cover id: priority} for every approved cover."""
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError:
        fail("tkinter is missing – please install Python with “tcl/tk”.")

    ratings = {}
    root = tk.Tk()
    root.title("Cover selection")
    root.configure(bg="#1e1e1e")
    pos = [0]

    image_lbl = tk.Label(root, bg="#1e1e1e")
    image_lbl.pack(padx=16, pady=(16, 8))
    text_lbl = tk.Label(root, bg="#1e1e1e", fg="white", font=("Segoe UI", 12), justify="center")
    text_lbl.pack(padx=16)

    buttons = tk.Frame(root, bg="#1e1e1e")
    buttons.pack(pady=12)

    help_lbl = tk.Label(
        root, bg="#1e1e1e", fg="#9a9a9a", font=("Segoe UI", 9), justify="center",
        text="Keys: 1-5 = use with priority (5 = important)   0 / N = don't use\n"
             "← / Backspace = back   Esc = done (remaining covers are left out)",
    )
    help_lbl.pack(pady=(0, 14))

    def show():
        if pos[0] >= len(cover_list):
            root.destroy()
            return
        c = cover_list[pos[0]]
        img = c["image"].copy()
        img.thumbnail((420, 420))
        tkimg = ImageTk.PhotoImage(img)
        image_lbl.configure(image=tkimg)
        image_lbl.image = tkimg
        before = ratings.get(c["id"])
        extra = f"\nchosen: priority {before}" if before else ""
        text_lbl.configure(text=f"{c['name']}\n{c['artists']}\n({pos[0] + 1}/{len(cover_list)}){extra}")

    def decide(prio):
        c = cover_list[pos[0]]
        if prio:
            ratings[c["id"]] = prio
        else:
            ratings.pop(c["id"], None)
        pos[0] += 1
        show()

    def back(_=None):
        if pos[0] > 0:
            pos[0] -= 1
            show()

    tk.Button(buttons, text="Don't use", width=14, command=lambda: decide(0)).pack(side="left", padx=4)
    for p in range(1, 6):
        tk.Button(buttons, text=f"Prio {p}", width=7, command=lambda p=p: decide(p)).pack(side="left", padx=2)

    for k in "012345":
        root.bind(k, lambda e, p=int(k): decide(p))
    root.bind("n", lambda e: decide(0))
    root.bind("<Left>", back)
    root.bind("<BackSpace>", back)
    root.bind("<Escape>", lambda e: root.destroy())

    show()
    root.mainloop()
    return ratings


def ask_too_few(count, slots, W, H):
    """Asks via pop-up: True = repeat covers, False = leave gaps. Cancel quits the program."""
    answer = _dialog(
        "askyesnocancel", f"{TITLE} – Too few covers",
        f"You approved {count} covers, but {W}x{H} needs {slots} "
        f"({slots - count} missing).\n\n"
        "Yes  =  repeat covers (fills the whole screen)\n"
        "No  =  use every cover only once, leave gaps\n"
        "Cancel  =  don't create anything",
    )
    if answer is None:
        print("Cancelled.")
        sys.exit(0)
    return answer


# --------------------------------------------------------------------------
# Collage
# --------------------------------------------------------------------------
class Tiles:
    """Square covers with their priority; caches scaled versions."""

    def __init__(self, entries):
        self.items = []  # (image, priority)
        for img, prio in entries:
            self.items.append((ImageOps.fit(img, (min(img.size),) * 2), prio))
        self.weights = [PRIO_WEIGHT[p] for _, p in self.items]
        self.cache = {}

    def image(self, k, size):
        key = (k, size)
        if key not in self.cache:
            self.cache[key] = self.items[k][0].resize((size, size), Image.LANCZOS)
        return self.cache[key]


def layout(n, W, H):
    """Grid of cells across the whole canvas (edge cells may stick out).

    Tile size is height / 6.5; if not all n covers fit, the tile gets smaller.
    Returns (tile size, columns, [(x, y) per cell]).
    """
    tile = max(80, round(H / 6.5))  # covers look the same size on every monitor
    while tile > 20 and math.ceil(W / tile) * math.ceil(H / tile) < n:
        tile -= 1
    cols, rows = math.ceil(W / tile), math.ceil(H / tile)
    ox, oy = (W - cols * tile) // 2, (H - rows * tile) // 2
    cells = [(ox + c * tile, oy + r * tile) for r in range(rows) for c in range(cols)]
    return tile, cols, cells


def assign(tiles, slots, cols, repeat, rng):
    """Which cover goes into which cell (None = gap). Every cover appears at least once."""
    n = len(tiles.items)
    grid = list(range(n))
    if slots > n:
        if repeat:
            grid += rng.choices(range(n), tiles.weights, k=slots - n)
        else:
            grid += [None] * (slots - n)

    def neighbours_equal():
        for i, k in enumerate(grid):
            if k is not None and ((i % cols and grid[i - 1] == k) or (i >= cols and grid[i - cols] == k)):
                return True
        return False

    # try to keep identical covers from sitting right next to / below each other
    for _ in range(200):
        rng.shuffle(grid)
        if not repeat or not neighbours_equal():
            break
    return grid


def draw(canvas, tiles, tile, cells, grid, rng, rotation):
    # Every cover gets one cell; size/position/rotation vary slightly.
    spots = [(k, c) for k, c in zip(grid, cells) if k is not None]
    spots.sort(key=lambda s: tiles.items[s[0]][1])  # low priority first -> high ends up on top

    for k, (cx0, cy0) in spots:
        prio = tiles.items[k][1]
        size = max(1, int(tile * rng.uniform(0.95, 1.05) * (0.9 + 0.1 * prio)))
        img = tiles.image(k, size).convert("RGBA")
        if rotation:
            img = img.rotate(rng.uniform(-rotation, rotation), resample=Image.BICUBIC, expand=True)

        # soft shadow
        pad = 18
        shadow = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
        alpha = img.getchannel("A").point(lambda a: int(a * 0.55))
        shadow.paste((0, 0, 0, 255), (pad, pad), alpha)
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))

        cx = cx0 + tile // 2 + rng.randint(-tile // 10, tile // 10)
        cy = cy0 + tile // 2 + rng.randint(-tile // 10, tile // 10)
        x, y = cx - img.width // 2, cy - img.height // 2
        canvas.paste(shadow, (x - pad + 6, y - pad + 8), shadow)
        canvas.paste(img, (x, y), img)


def make_collage(entries, W, H, rotation, rng, ask=ask_too_few):
    """entries: [(image, priority)]. Asks via `ask` if there are too few covers."""
    tile, cols, cells = layout(len(entries), W, H)
    repeat = False
    if len(entries) < len(cells):
        repeat = ask(len(entries), len(cells), W, H)
    print(f"Creating collage ({len(entries)} covers, {len(cells)} slots, tile {tile}px, "
          f"{'with' if repeat else 'without'} repeats, {W}x{H}) ...")
    tiles = Tiles(entries)
    grid = assign(tiles, len(cells), cols, repeat, rng)
    canvas = Image.new("RGB", (W, H), (18, 18, 18))
    draw(canvas, tiles, tile, cells, grid, rng, rotation)
    return canvas


# --------------------------------------------------------------------------
# Screen resolution
# --------------------------------------------------------------------------
def screen_resolution():
    """Real resolution of the main monitor (ignoring Windows scaling)."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        u = ctypes.windll.user32
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        if w and h:
            return w, h
    except Exception:
        pass
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        w, h = r.winfo_screenwidth(), r.winfo_screenheight()
        r.destroy()
        return w, h
    except Exception:
        return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Desktop wallpaper collage from Spotify covers.")
    ap.add_argument("links", nargs="*", help="Spotify links/URIs (playlist, album, track) instead of those in config.json")
    args = ap.parse_args()

    cfg = load_config()

    # 1) collect links and fetch covers
    texts = list(args.links) or list(cfg["playlists"])
    refs = parse_links(texts)
    if not refs and not any("YOUR_PLAYLIST" not in t for t in texts):
        setup_needed("No Spotify playlist has been set up yet.")
    if not refs:
        fail("No valid Spotify links found.\n\n"
             "A link looks like this:\nhttps://open.spotify.com/playlist/<22 characters>\n\n"
             "Found:\n" + "\n".join(texts) + f"\n\nFile: {CONFIG_PATH}")

    cid = cfg["spotify_client_id"] or os.environ.get("SPOTIFY_CLIENT_ID")
    sec = cfg["spotify_client_secret"] or os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not sec:
        setup_needed("Spotify credentials are missing.")
    print("Fetching data from Spotify ...")
    covers, failed = collect_covers(Spotify(cid, sec), refs)
    if failed:
        notify("These links could not be fetched:\n\n• " + "\n• ".join(failed) +
               "\n\nPossible reasons: wrong link, someone else's private playlist, or a playlist "
               "made by Spotify itself (e.g. “Discover Weekly”) – those are blocked for the API. "
               "Fix: copy the songs into a playlist of your own.",
               kind="warning")
    if not covers:
        fail("The playlist(s) contain no tracks with a cover – nothing to create.")
    print(f"  {len(covers)} covers found. Downloading ...")
    download_covers(covers)

    # 2) approval + priority
    cover_list = list(covers.values())
    ratings = rating_window(cover_list)
    entries = [(c["image"], ratings[c["id"]]) for c in cover_list if c["id"] in ratings]
    if not entries:
        fail("No covers approved – nothing to create.")

    # 3) collage
    W, H = parse_resolution(cfg["resolution"])
    canvas = make_collage(entries, W, H, cfg["rotation"], random.Random())
    output = BASE / cfg["output"]  # relative paths are relative to the program folder
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    print(f"Done: {output}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
    except requests.RequestException as ex:
        fail(f"Connection problem with Spotify:\n\n{ex}\n\nCheck your internet connection and try again.")
    except Exception as ex:
        fail(f"Unexpected error ({type(ex).__name__}):\n\n{ex}")
