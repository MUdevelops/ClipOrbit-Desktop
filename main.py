"""
ClipOrbit Desktop  -  A MUdevelops Project  -  v1.0.0
Python 3.11.9 | PySide6 | yt-dlp | FFmpeg (optional) | SQLite

Everything lives in this single file:
  1. Constants, helpers, settings + history (SQLite)
  2. Graphics (logo, icons) painted with Qt
  3. Workers (init, analyze, download) + job manager
  4. Full-screen splash
  5. Main window and pages
  6. Entry point

Only download content you own or are authorized to download.
"""
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import (Qt, QObject, QThread, Signal, QTimer, QElapsedTimer, QPointF,
                            QRectF, QSize, QUrl, QStandardPaths, QCoreApplication, QPropertyAnimation,
                            QEasingCurve)
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QRadialGradient, QLinearGradient,
                           QFont, QFontMetrics, QPixmap, QPainterPath, QDesktopServices, QIcon,
                           QGuiApplication, QCursor, QPolygonF)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
                               QLineEdit, QComboBox, QSpinBox, QCheckBox, QProgressBar,
                               QStackedWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QScrollArea, QTableWidget, QTableWidgetItem, QHeaderView,
                               QAbstractItemView, QFileDialog, QMessageBox, QPlainTextEdit,
                               QButtonGroup, QSystemTrayIcon, QSizePolicy)

# ----------------------------------------------------------------------------------------
# 1. CONSTANTS, HELPERS, SETTINGS, HISTORY
# ----------------------------------------------------------------------------------------
APP_NAME = "ClipOrbit Desktop"
APP_VERSION = "1.0.0"
TAGLINE = "One Link. One Hub. Your Media."

C = dict(bg="#070B16", navy="#0B1020", surface="#111827", cyan="#22D3EE", blue="#3B82F6",
         text="#F8FAFC", muted="#94A3B8", border="#1E293B", danger="#F87171",
         ok="#34D399", warn="#FBBF24")

DEFAULTS = {
    "output_dir": str(Path.home() / "Downloads" / "ClipOrbit"),
    "quality": "Best Available",
    "max_concurrent": "2",
    "container": "auto",
    "mp3": "0",
    "splash": "1",
}
LADDER = [("8K / 4320p", 4320), ("4K / 2160p", 2160), ("1440p", 1440), ("1080p", 1080),
          ("720p", 720), ("480p", 480), ("360p", 360)]
QUALITIES = ["Best Available"] + [n for n, _ in LADDER] + ["Audio Only"]
CONTAINERS = ["auto", "mp4", "mkv", "webm"]
ACTIVE = ("Downloading", "Processing")
FINISHED = ("Completed", "Failed", "Cancelled")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Filled in during startup by InitWorker
STATE = {"ffmpeg": None, "ffmpeg_version": "", "ytdlp_version": "", "ytdlp_error": ""}


def app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def data_dir():
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    path = Path(base) if base else Path.home() / ".cliporbit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sql(query, args=()):
    """Run one SQL statement on a fresh connection (thread-safe, closes immediately)."""
    con = sqlite3.connect(str(data_dir() / "cliporbit.db"), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(query, args)
        rows = cur.fetchall() if cur.description else []
        con.commit()
        return rows, cur.lastrowid
    finally:
        con.close()


def init_db():
    sql("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    sql("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT, platform TEXT,
            quality TEXT, path TEXT, status TEXT, date TEXT)""")
    # Anything left unfinished by a previous run was interrupted
    sql("UPDATE history SET status='Interrupted' WHERE status IN ('Queued','Downloading','Processing')")


def get_setting(key):
    rows, _ = sql("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else DEFAULTS[key]


def set_setting(key, value):
    sql("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, str(value)))


def safe_outdir(text):
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = Path(DEFAULTS["output_dir"])
    return p.resolve()


PLATFORMS = [("YouTube", ("youtube.com", "youtu.be")), ("TikTok", ("tiktok.com",)),
             ("Instagram", ("instagram.com",)), ("Facebook", ("facebook.com", "fb.watch", "fb.com")),
             ("X / Twitter", ("twitter.com", "x.com")), ("Snapchat", ("snapchat.com",))]


def detect_platform(url, fallback=""):
    host = (urlparse(url).hostname or "").lower()
    for name, domains in PLATFORMS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return fallback or "Web"


def valid_url(text):
    text = text.strip()
    p = urlparse(text)
    return p.scheme in ("http", "https") and bool(p.hostname) and not re.search(r"\s", text)


def fmt_duration(sec):
    if not sec:
        return "Unknown"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n):
    if not n:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_speed(bps):
    return f"{fmt_size(bps)}/s" if bps else ""


def fmt_eta(sec):
    return fmt_duration(sec) if sec else ""


def find_ffmpeg():
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for cand in (shutil.which("ffmpeg"), str(app_dir() / exe), str(app_dir() / "ffmpeg" / "bin" / exe)):
        if cand and os.path.isfile(cand):
            return cand
    return None


def ffmpeg_version(path):
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=8,
                             creationflags=NO_WINDOW).stdout.splitlines()
        parts = out[0].split() if out else []
        return parts[2] if len(parts) > 2 else "unknown"
    except Exception:
        return "unknown"


def friendly_error(msg):
    low = msg.lower()
    rules = [
        (("unsupported url",), "This link isn't supported by yt-dlp, or it doesn't point to a media page."),
        (("private", "login", "sign in", "log in", "cookies", "authentication", "members-only"),
         "This media needs a login or is private. ClipOrbit doesn't bypass access controls."),
        (("drm",), "This media is DRM-protected and can't be downloaded."),
        (("429", "too many requests"), "The site is rate-limiting requests. Please wait a while and try again."),
        (("urlopen error", "getaddrinfo", "timed out", "connection", "network"),
         "A network problem occurred. Check your internet connection and try again."),
        (("unavailable", "removed", "404", "expired", "does not exist", "not available"),
         "This media is unavailable. It may have been removed, expired, or be region-locked."),
        (("requested format is not available",), "The requested quality/format isn't available for this media."),
        (("ffmpeg", "ffprobe"), "FFmpeg is required for this operation but wasn't found. See Settings."),
    ]
    for keys, text in rules:
        if any(k in low for k in keys):
            return text
    return "Something went wrong while processing this link."


def clean_err(e):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()


def quality_limit(label):
    """Short-side pixel limit for a quality label (6% tolerance for 1088p etc.)."""
    for name, n in LADDER:
        if name == label:
            return int(n * 1.06)
    return None


def build_format(label, has_ffmpeg, portrait):
    if label == "Audio Only":
        return "ba/b"
    lim = quality_limit(label)
    dim = "width" if portrait else "height"
    if has_ffmpeg:
        return "bv*+ba/b" if lim is None else f"bv*[{dim}<={lim}]+ba/b[{dim}<={lim}]"
    return "b" if lim is None else f"b[{dim}<={lim}]"


# ----------------------------------------------------------------------------------------
# 2. GRAPHICS  (logo + icons painted with Qt, no image files)
# ----------------------------------------------------------------------------------------
def draw_logo(p, cx, cy, r, t, alpha=1.0):
    p.save()
    p.setOpacity(p.opacity() * alpha)
    cyan, blue = QColor(C["cyan"]), QColor(C["blue"])
    glow = QRadialGradient(cx, cy, r * 1.7)
    glow.setColorAt(0, QColor(34, 211, 238, 70))
    glow.setColorAt(1, QColor(34, 211, 238, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(glow)
    p.drawEllipse(QPointF(cx, cy), r * 1.7, r * 1.7)
    p.setBrush(QColor(C["surface"]))
    p.setPen(QPen(QColor(255, 255, 255, 18), 1))
    p.drawEllipse(QPointF(cx, cy), r * 0.82, r * 0.82)

    ring = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    grad = QLinearGradient(cx - r, cy - r, cx + r, cy + r)
    grad.setColorAt(0, cyan)
    grad.setColorAt(1, blue)
    faint = QPen(QColor(34, 211, 238, 45), max(1.5, r * 0.035))
    p.setPen(faint)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(ring)
    pen = QPen(QBrush(grad), max(2.0, r * 0.07))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    spin = (t * 45) % 360
    p.drawArc(ring, int(spin * 16), 110 * 16)
    p.drawArc(ring, int((spin + 180) * 16), 70 * 16)

    tri = QPolygonF([QPointF(cx - 0.2 * r, cy - 0.34 * r), QPointF(cx - 0.2 * r, cy + 0.34 * r),
                     QPointF(cx + 0.38 * r, cy)])
    fill = QLinearGradient(cx - r * 0.2, cy - r * 0.3, cx + r * 0.4, cy + r * 0.3)
    fill.setColorAt(0, cyan)
    fill.setColorAt(1, blue)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(fill))
    p.drawPolygon(tri)

    for rad, speed, size in ((r, 1.4, 0.07), (r * 1.32, -0.7, 0.045)):
        ang = t * speed
        pos = QPointF(cx + rad * math.cos(ang), cy + rad * math.sin(ang))
        halo = QRadialGradient(pos, r * size * 3)
        halo.setColorAt(0, QColor(34, 211, 238, 160))
        halo.setColorAt(1, QColor(34, 211, 238, 0))
        p.setBrush(halo)
        p.drawEllipse(pos, r * size * 3, r * size * 3)
        p.setBrush(QColor("#E0FBFF"))
        p.drawEllipse(pos, r * size, r * size)
    p.restore()


def logo_pixmap(size):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    draw_logo(p, size / 2, size / 2, size * 0.34, 0.6)
    p.end()
    return pm


def make_icon(name, color, size=22):
    s = size * 2
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), s * 0.085)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    u = s / 24.0
    pt = lambda x, y: QPointF(x * u, y * u)
    if name == "home":
        path = QPainterPath()
        path.moveTo(pt(3, 11.5)); path.lineTo(pt(12, 4)); path.lineTo(pt(21, 11.5))
        p.drawPath(path)
        body = QPainterPath()
        body.moveTo(pt(5.5, 10)); body.lineTo(pt(5.5, 20)); body.lineTo(pt(18.5, 20)); body.lineTo(pt(18.5, 10))
        p.drawPath(body)
    elif name == "download":
        p.drawLine(pt(12, 4), pt(12, 15)); p.drawLine(pt(7, 10.5), pt(12, 15.5))
        p.drawLine(pt(17, 10.5), pt(12, 15.5)); p.drawLine(pt(4.5, 20), pt(19.5, 20))
    elif name == "queue":
        for y in (6, 12, 18):
            p.drawLine(pt(9, y), pt(21, y)); p.drawPoint(pt(4.5, y))
    elif name == "history":
        p.drawEllipse(pt(12, 12), 8.5 * u, 8.5 * u)
        p.drawLine(pt(12, 7), pt(12, 12)); p.drawLine(pt(12, 12), pt(15.5, 14))
    elif name == "settings":
        p.drawEllipse(pt(12, 12), 3.2 * u, 3.2 * u)
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(pt(12 + 6 * math.cos(a), 12 + 6 * math.sin(a)),
                       pt(12 + 9 * math.cos(a), 12 + 9 * math.sin(a)))
    elif name == "about":
        p.drawEllipse(pt(12, 12), 8.5 * u, 8.5 * u)
        p.drawLine(pt(12, 11), pt(12, 16.5)); p.drawPoint(pt(12, 7.8))
    p.end()
    return pm


# ----------------------------------------------------------------------------------------
# 3. WORKERS + JOB MANAGER
# ----------------------------------------------------------------------------------------
class InitWorker(QThread):
    """Real startup work: load yt-dlp, detect FFmpeg, load preferences."""
    status = Signal(str)
    done = Signal()

    def run(self):
        self.status.emit("Preparing download engine")
        try:
            import yt_dlp
            STATE["ytdlp_version"] = yt_dlp.version.__version__
        except Exception as e:
            STATE["ytdlp_error"] = clean_err(e) or "yt-dlp could not be imported"
        self.status.emit("Checking multimedia tools")
        STATE["ffmpeg"] = find_ffmpeg()
        STATE["ffmpeg_version"] = ffmpeg_version(STATE["ffmpeg"]) if STATE["ffmpeg"] else ""
        self.status.emit("Loading preferences")
        try:
            safe_outdir(get_setting("output_dir")).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.done.emit()


class AnalyzeWorker(QThread):
    done = Signal(dict)
    failed = Signal(str, str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            import yt_dlp
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                    "socket_timeout": 20}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
            if not info:
                raise RuntimeError("No information was returned for this link.")
            if info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    raise RuntimeError("This link contains no downloadable entries.")
                info = entries[0]
            fmts = []
            for f in info.get("formats") or []:
                vc, ac = f.get("vcodec") or "none", f.get("acodec") or "none"
                if vc == "none" and ac == "none":
                    continue  # storyboards etc.
                w, h = f.get("width"), f.get("height")
                fmts.append({"ext": f.get("ext") or "?", "vcodec": vc, "acodec": ac, "w": w, "h": h,
                             "s": (min(w, h) if w and h else h) if vc != "none" else None,
                             "fps": f.get("fps"), "tbr": f.get("tbr") or 0, "abr": f.get("abr") or 0,
                             "size": f.get("filesize") or f.get("filesize_approx")})
            thumb = None
            if info.get("thumbnail"):
                try:
                    req = Request(info["thumbnail"], headers={"User-Agent": "Mozilla/5.0"})
                    thumb = urlopen(req, timeout=10).read()
                except Exception:
                    thumb = None
            vids = [f for f in fmts if f["vcodec"] != "none" and f["w"] and f["h"]]
            portrait = bool(vids) and max(vids, key=lambda f: f["h"])["h"] > max(vids, key=lambda f: f["h"])["w"]
            self.done.emit({"url": self.url, "title": info.get("title") or "Untitled",
                            "platform": detect_platform(self.url, info.get("extractor_key") or ""),
                            "duration": info.get("duration"), "thumb": thumb, "formats": fmts,
                            "portrait": portrait})
        except Exception as e:
            self.failed.emit(friendly_error(clean_err(e)), clean_err(e))


class Job:
    _counter = 0

    def __init__(self, url, title, platform, quality, portrait=False):
        Job._counter += 1
        self.id = Job._counter
        self.url, self.title, self.platform = url, title, platform
        self.quality, self.portrait = quality, portrait
        self.status, self.pct = "Queued", 0.0
        self.speed = self.eta = self.path = self.error = self.actual = ""
        self.row = 0
        self.cancelling = False


class DownloadWorker(QThread):
    progress = Signal(int, float, str, str)
    stage = Signal(int, str)
    result = Signal(int, str, str, str, str)  # id, status, path, error, actual quality

    def __init__(self, job, outdir, container, mp3):
        super().__init__()
        self.job, self.outdir, self.container, self.mp3 = job, outdir, container, mp3
        self._cancel = False
        self._streams_done = 0

    def cancel(self):
        self._cancel = True

    def run(self):
        job = self.job
        try:
            import yt_dlp
            try:
                Cancelled = yt_dlp.utils.DownloadCancelled
            except AttributeError:
                Cancelled = type("Cancelled", (Exception,), {})
            ffmpeg = STATE["ffmpeg"]
            audio_only = job.quality == "Audio Only"
            selector = build_format(job.quality, bool(ffmpeg), job.portrait)
            try:  # validate selector against the installed yt-dlp
                yt_dlp.YoutubeDL({"quiet": True}).build_format_selector(selector)
            except Exception:
                selector = "ba/b" if audio_only else "b"
            self.outdir.mkdir(parents=True, exist_ok=True)

            def hook(d):
                if self._cancel:
                    raise Cancelled("Cancelled by user")
                if d.get("status") == "downloading":
                    done = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    n = len((d.get("info_dict") or {}).get("requested_formats") or []) or 1
                    pct = -1.0
                    if total:
                        pct = min(100.0, (self._streams_done + done / total) / n * 100)
                    self.progress.emit(job.id, pct, fmt_speed(d.get("speed")), fmt_eta(d.get("eta")))
                elif d.get("status") == "finished":
                    self._streams_done += 1

            def pp_hook(d):
                if d.get("status") == "started":
                    self.stage.emit(job.id, "Processing")

            opts = {"format": selector, "outtmpl": {"default": "%(title).150s [%(id)s].%(ext)s"},
                    "paths": {"home": str(self.outdir)}, "noplaylist": True, "quiet": True,
                    "no_warnings": True, "noprogress": True, "windowsfilenames": True,
                    "retries": 5, "fragment_retries": 5, "socket_timeout": 30,
                    "concurrent_fragment_downloads": 4, "continuedl": True,
                    "progress_hooks": [hook], "postprocessor_hooks": [pp_hook]}
            if ffmpeg:
                opts["ffmpeg_location"] = ffmpeg
                if audio_only and self.mp3:
                    opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                               "preferredquality": "0"}]
                elif not audio_only and self.container != "auto":
                    opts["merge_output_format"] = self.container
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=True)
                rd = (info or {}).get("requested_downloads") or []
                path = (rd[0].get("filepath") if rd else None) or ydl.prepare_filename(info)
                actual = ""
                if not audio_only and info:
                    src = rd[0] if rd else info
                    actual = src.get("resolution") or (f"{src.get('height')}p" if src.get("height") else "")
                    if src.get("fps"):
                        actual += f" {src['fps']:.0f}fps"
            if not os.path.isfile(path):
                path = ""
            self.result.emit(job.id, "Completed", path, "", actual.strip())
        except Exception as e:
            if self._cancel or type(e).__name__ == "DownloadCancelled":
                self.result.emit(job.id, "Cancelled", "", "", "")
            else:
                self.result.emit(job.id, "Failed", "", f"{friendly_error(clean_err(e))}\n\n{clean_err(e)}", "")


class JobManager(QObject):
    changed = Signal()
    job_done = Signal(object)

    def __init__(self):
        super().__init__()
        self.jobs, self.workers, self.dirty = [], {}, False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)

    def _tick(self):
        for jid, w in list(self.workers.items()):
            if w.isFinished():
                del self.workers[jid]
        if self.dirty:
            self.dirty = False
            self.changed.emit()

    def add(self, url, title, platform, quality, portrait=False):
        job = Job(url, title, platform, quality, portrait)
        _, job.row = sql("INSERT INTO history(title,url,platform,quality,path,status,date) VALUES(?,?,?,?,?,?,?)",
                         (title, url, platform, quality, "", "Queued",
                          datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
        self.jobs.append(job)
        self.pump()
        self.dirty = True
        return job

    def pump(self):
        limit = max(1, int(get_setting("max_concurrent")))
        running = sum(1 for j in self.jobs if j.status in ACTIVE)
        for j in self.jobs:
            if running >= limit:
                break
            if j.status == "Queued":
                self._start(j)
                running += 1

    def _start(self, j):
        j.status, j.pct, j.speed, j.eta, j.error = "Downloading", 0.0, "", "", ""
        w = DownloadWorker(j, safe_outdir(get_setting("output_dir")), get_setting("container"),
                           get_setting("mp3") == "1")
        w.progress.connect(self._on_progress)
        w.stage.connect(self._on_stage)
        w.result.connect(self._on_result)
        self.workers[j.id] = w
        sql("UPDATE history SET status=? WHERE id=?", ("Downloading", j.row))
        w.start()

    def _find(self, jid):
        return next((j for j in self.jobs if j.id == jid), None)

    def _on_progress(self, jid, pct, speed, eta):
        j = self._find(jid)
        if j and j.status == "Downloading":
            j.pct, j.speed, j.eta = pct, speed, eta
            self.dirty = True

    def _on_stage(self, jid, stage):
        j = self._find(jid)
        if j and j.status in ACTIVE:
            j.status, j.pct, j.speed, j.eta = stage, 100.0, "", ""
            self.dirty = True

    def _on_result(self, jid, status, path, error, actual):
        j = self._find(jid)
        if not j:
            return
        j.status, j.path, j.error, j.actual, j.speed, j.eta = status, path, error, actual, "", ""
        j.cancelling = False
        if status == "Completed":
            j.pct = 100.0
        qual = j.quality + (f" ({actual})" if actual else "")
        sql("UPDATE history SET status=?, path=?, quality=? WHERE id=?", (status, path, qual, j.row))
        self.job_done.emit(j)
        self.pump()
        self.dirty = True

    def cancel(self, j):
        if j.status == "Queued":
            j.status = "Cancelled"
            sql("UPDATE history SET status='Cancelled' WHERE id=?", (j.row,))
        elif j.status in ACTIVE and j.id in self.workers:
            j.cancelling = True
            self.workers[j.id].cancel()
        self.dirty = True

    def retry(self, j):
        if j.status in ("Failed", "Cancelled"):
            j.status, j.pct, j.error, j.cancelling = "Queued", 0.0, "", False
            sql("UPDATE history SET status='Queued' WHERE id=?", (j.row,))
            self.pump()
            self.dirty = True

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.status not in FINISHED]
        self.dirty = True

    def shutdown(self):
        for w in self.workers.values():
            w.cancel()
        for w in self.workers.values():
            w.wait(8000)
        sql("UPDATE history SET status='Interrupted' WHERE status IN ('Queued','Downloading','Processing')")


# ----------------------------------------------------------------------------------------
# 4. FULL-SCREEN SPLASH
# ----------------------------------------------------------------------------------------
def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


class Splash(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setWindowTitle(APP_NAME)
        self.status = "Initializing interface"
        rnd = random.Random(11)
        self.parts = [(rnd.random(), rnd.random(), rnd.uniform(0.004, 0.014), rnd.uniform(1.0, 2.8),
                       rnd.uniform(0, 6.28)) for _ in range(46)]
        self.clock = QElapsedTimer()
        self.clock.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(16)
        self.anim = None

    def set_status(self, text):
        self.status = text
        self.update()

    def fade_out(self):
        self.anim = QPropertyAnimation(self, b"windowOpacity")
        self.anim.setDuration(500)
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self.anim.setEasingCurve(QEasingCurve.InOutQuad)
        self.anim.finished.connect(self._finish)
        self.anim.start()

    def _finish(self):
        self.timer.stop()
        self.close()

    def _font(self, px, weight=QFont.Weight.Normal, spacing=0.0):
        f = QFont("Segoe UI")
        f.setPixelSize(int(px))
        f.setWeight(weight)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        return f

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        t = self.clock.elapsed() / 1000.0
        unit = min(w, h)
        cx, cy = w / 2, h * 0.385
        r = max(56, min(150, unit * 0.11))
        p.fillRect(self.rect(), QColor(C["bg"]))

        glow = QRadialGradient(cx, cy, unit * 0.8)
        glow.setColorAt(0, QColor(34, 211, 238, 52))
        glow.setColorAt(0.45, QColor(59, 130, 246, 22))
        glow.setColorAt(1, QColor(7, 11, 22, 0))
        p.fillRect(self.rect(), glow)

        p.setPen(QPen(QColor(148, 163, 184, 12), 1))  # fine geometric grid
        step = max(56, int(unit / 14))
        for x in range(0, w, step):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            p.drawLine(0, y, w, y)

        p.save()  # faint orbital paths
        p.translate(cx, cy)
        p.setBrush(Qt.NoBrush)
        for i, (rx, ry, ang) in enumerate(((0.55, 0.20, 24), (0.42, 0.16, -38), (0.70, 0.27, 70))):
            p.save()
            p.rotate(ang + math.sin(t * 0.15 + i) * 4)
            p.setPen(QPen(QColor(34, 211, 238, 24 - i * 5), 1.2))
            p.drawEllipse(QPointF(0, 0), unit * rx, unit * ry)
            p.restore()
        p.restore()

        p.setPen(Qt.NoPen)  # restrained particles
        for x, y, speed, size, phase in self.parts:
            py = (y - t * speed) % 1.0
            twinkle = 0.35 + 0.35 * math.sin(t * size + phase)
            p.setBrush(QColor(34, 211, 238, int(255 * 0.55 * twinkle)))
            p.drawEllipse(QPointF(x * w, py * h), size, size)

        draw_logo(p, cx, cy, r, t, ease(t / 0.9))  # logo fade in

        # Title
        te = ease((t - 0.55) / 0.9)
        tpx = max(30, min(76, h * 0.06))
        f_title = self._font(tpx, QFont.Weight.Bold, tpx * 0.01)
        p.setFont(f_title)
        fm = QFontMetrics(f_title)
        w1, w2 = fm.horizontalAdvance("ClipOrbit"), fm.horizontalAdvance(" Desktop")
        x0 = cx - (w1 + w2) / 2
        ty = cy + r * 1.6 + tpx + (1 - te) * 18
        p.setOpacity(te)
        p.setPen(QColor(C["text"]))
        p.drawText(QPointF(x0, ty), "ClipOrbit")
        p.setPen(QColor(C["cyan"]))
        p.drawText(QPointF(x0 + w1, ty), " Desktop")

        # Tagline
        ge = ease((t - 1.0) / 0.8)
        gpx = max(14, min(26, h * 0.022))
        p.setOpacity(ge)
        p.setFont(self._font(gpx, QFont.Weight.Normal, 2.0))
        p.setPen(QColor(C["muted"]))
        p.drawText(QRectF(0, ty + gpx * 0.9 + (1 - ge) * 10, w, gpx * 2), Qt.AlignHCenter | Qt.AlignTop, TAGLINE)

        # Status + indeterminate indicator (no fake percentage)
        p.setOpacity(ease((t - 0.8) / 0.6))
        spx = max(13, min(20, h * 0.017))
        sy = h * 0.82
        p.setFont(self._font(spx, QFont.Weight.Normal, 0.6))
        p.setPen(QColor(C["text"]))
        p.drawText(QRectF(0, sy, w, spx * 2), Qt.AlignHCenter | Qt.AlignTop, self.status + "…")
        bw = max(200, min(420, w * 0.18))
        bx, by = cx - bw / 2, sy + spx * 2.2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(148, 163, 184, 40))
        p.drawRoundedRect(QRectF(bx, by, bw, 3), 1.5, 1.5)
        seg = bw * 0.28
        pos = ((t * 0.85) % 1.4) * (bw + seg) / 1.4 - seg
        a, b = max(0.0, pos), min(bw, pos + seg)
        if b > a:
            grad = QLinearGradient(bx + a, 0, bx + b, 0)
            grad.setColorAt(0, QColor(C["cyan"]))
            grad.setColorAt(1, QColor(C["blue"]))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(bx + a, by, b - a, 3), 1.5, 1.5)

        # Footer
        p.setOpacity(ease((t - 1.2) / 0.8))
        fpx = max(12, min(17, h * 0.015))
        p.setFont(self._font(fpx, QFont.Weight.DemiBold, 1.5))
        p.setPen(QColor(C["muted"]))
        p.drawText(QRectF(0, h - fpx * 4.6, w, fpx * 1.8), Qt.AlignHCenter, "A MUdevelops Project")
        p.setFont(self._font(fpx, QFont.Weight.Normal, 1.0))
        p.setPen(QColor(100, 116, 139))
        p.drawText(QRectF(0, h - fpx * 2.8, w, fpx * 1.8), Qt.AlignHCenter, f"Version {APP_VERSION}")
        p.end()


# ----------------------------------------------------------------------------------------
# 5. MAIN WINDOW + PAGES
# ----------------------------------------------------------------------------------------
QSS = f"""
* {{ font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 14px; color: {C['text']}; }}
QMainWindow, QWidget#page, QWidget#root {{ background: {C['bg']}; }}
QScrollArea {{ background: transparent; border: none; }}
QFrame#sidebar {{ background: {C['navy']}; border-right: 1px solid {C['border']}; }}
QFrame#card {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 12px; }}
QLabel#h1 {{ font-size: 34px; font-weight: 700; }}
QLabel#h2 {{ font-size: 20px; font-weight: 600; }}
QLabel#muted {{ color: {C['muted']}; }}
QLabel#banner {{ background: rgba(251,191,36,0.10); border: 1px solid rgba(251,191,36,0.45);
                 border-radius: 8px; padding: 10px 14px; color: {C['warn']}; }}
QLabel#chip {{ color: {C['muted']}; font-size: 12px; }}
QPushButton {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 8px;
               padding: 9px 16px; }}
QPushButton:hover {{ border-color: {C['cyan']}; }}
QPushButton:disabled {{ color: #475569; border-color: #172033; }}
QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {C['cyan']}, stop:1 {C['blue']});
                       border: none; color: #04121C; font-weight: 700; padding: 10px 22px; }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #67E8F9, stop:1 #60A5FA); }}
QPushButton#primary:disabled {{ background: #1E293B; color: #475569; }}
QPushButton#nav {{ text-align: left; background: transparent; border: none; border-radius: 8px;
                   padding: 11px 14px; color: {C['muted']}; }}
QPushButton#nav:hover {{ background: rgba(255,255,255,0.04); }}
QPushButton#nav:checked {{ background: rgba(34,211,238,0.12); color: {C['cyan']}; font-weight: 600; }}
QLineEdit, QComboBox, QSpinBox {{ background: {C['navy']}; border: 1px solid {C['border']};
                                  border-radius: 8px; padding: 9px 12px; selection-background-color: {C['blue']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {C['cyan']}; }}
QComboBox QAbstractItemView {{ background: {C['surface']}; border: 1px solid {C['border']};
                               selection-background-color: {C['blue']}; }}
QPlainTextEdit {{ background: {C['navy']}; border: 1px solid {C['border']}; border-radius: 8px;
                  font-family: Consolas, monospace; font-size: 12px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 1px solid {C['border']};
                        background: {C['navy']}; }}
QCheckBox::indicator:checked {{ background: {C['cyan']}; border-color: {C['cyan']}; }}
QProgressBar {{ background: {C['navy']}; border: none; border-radius: 6px; text-align: center;
                height: 14px; font-size: 11px; }}
QProgressBar::chunk {{ border-radius: 6px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                       stop:0 {C['cyan']}, stop:1 {C['blue']}); }}
QTableWidget {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 10px;
                gridline-color: transparent; alternate-background-color: #0F1729; }}
QTableWidget::item:selected {{ background: rgba(59,130,246,0.30); }}
QHeaderView::section {{ background: {C['navy']}; color: {C['muted']}; border: none; padding: 8px;
                        font-weight: 600; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #25324A; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: {C['surface']}; color: {C['text']}; border: 1px solid {C['border']}; }}
QMessageBox {{ background: {C['surface']}; }}
"""


def mklabel(text, name=None, wrap=False):
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def card():
    f = QFrame()
    f.setObjectName("card")
    return f


def page_wrapper(inner_layout):
    inner = QWidget()
    inner.setObjectName("page")
    inner_layout.setContentsMargins(40, 34, 40, 34)
    inner.setLayout(inner_layout)
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setWidget(inner)
    return sa


def open_path(path):
    if path and os.path.exists(path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return True
    return False


def open_folder_of(path):
    if path and os.path.exists(path):
        return open_path(str(Path(path).parent))
    return False


class HomePage(QWidget):
    queued = Signal()

    def __init__(self, mgr):
        super().__init__()
        self.mgr, self.info, self.worker = mgr, None, None
        lay = QVBoxLayout()
        lay.setSpacing(16)
        lay.addWidget(mklabel("Your Media, Within Reach.", "h1"))
        lay.addWidget(mklabel("Paste a link, analyze it, and download in the best quality the source offers.", "muted"))
        self.banner = mklabel("", "banner", True)
        self.banner.hide()
        lay.addWidget(self.banner)

        box = card()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(20, 20, 20, 20)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("Paste a video or audio link (YouTube, Instagram, Facebook…)")
        self.url.returnPressed.connect(self.analyze)
        self.paste_btn, self.clear_btn = QPushButton("Paste"), QPushButton("Clear")
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.setObjectName("primary")
        self.paste_btn.clicked.connect(lambda: self.url.setText(QGuiApplication.clipboard().text().strip()))
        self.clear_btn.clicked.connect(self.clear_all)
        self.analyze_btn.clicked.connect(self.analyze)
        row.addWidget(self.url, 1)
        for b in (self.paste_btn, self.clear_btn, self.analyze_btn):
            row.addWidget(b)
        bl.addLayout(row)
        self.status = mklabel("", "muted", True)
        self.details_btn = QPushButton("Show technical details")
        self.details_btn.setCursor(Qt.PointingHandCursor)
        self.details_btn.clicked.connect(self.toggle_details)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        self.details_btn.hide()
        self.details.hide()
        for wdg in (self.status, self.details_btn, self.details):
            bl.addWidget(wdg)
        self.status.hide()
        lay.addWidget(box)

        self.result = card()
        rl = QHBoxLayout(self.result)
        rl.setContentsMargins(20, 20, 20, 20)
        rl.setSpacing(22)
        self.thumb = QLabel()
        self.thumb.setFixedSize(320, 180)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet(f"background:{C['navy']}; border-radius:10px; color:{C['muted']};")
        rl.addWidget(self.thumb, 0, Qt.AlignTop)
        right = QVBoxLayout()
        right.setSpacing(10)
        self.title_lbl = mklabel("", "h2", True)
        right.addWidget(self.title_lbl)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        self.meta = {}
        for i, key in enumerate(("Platform", "Duration", "Resolutions", "Frame rates", "Formats", "Estimated size")):
            grid.addWidget(mklabel(key, "muted"), i, 0, Qt.AlignTop)
            self.meta[key] = mklabel("", None, True)
            grid.addWidget(self.meta[key], i, 1)
        grid.setColumnStretch(1, 1)
        right.addLayout(grid)
        qrow = QHBoxLayout()
        qrow.addWidget(mklabel("Quality", "muted"))
        self.quality = QComboBox()
        self.quality.setMinimumWidth(180)
        self.quality.currentIndexChanged.connect(self.on_quality)
        qrow.addWidget(self.quality)
        qrow.addStretch(1)
        right.addLayout(qrow)
        self.q_note = mklabel("", "muted", True)
        right.addWidget(self.q_note)
        orow = QHBoxLayout()
        self.outdir_lbl = mklabel("", "muted", True)
        browse, openf = QPushButton("Choose folder…"), QPushButton("Open folder")
        browse.clicked.connect(self.pick_dir)
        openf.clicked.connect(self.open_dir)
        orow.addWidget(self.outdir_lbl, 1)
        orow.addWidget(browse)
        orow.addWidget(openf)
        right.addLayout(orow)
        self.dl_btn = QPushButton("Download Now")
        self.dl_btn.setObjectName("primary")
        self.dl_btn.clicked.connect(self.download)
        right.addWidget(self.dl_btn, 0, Qt.AlignLeft)
        rl.addLayout(right, 1)
        self.result.hide()
        lay.addWidget(self.result)
        lay.addStretch(1)
        self.refresh_settings()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page_wrapper(lay))

    # --- state helpers
    def refresh_settings(self):
        self.outdir_lbl.setText(f"Save to: {safe_outdir(get_setting('output_dir'))}")

    def refresh_deps(self):
        msgs = []
        if STATE["ytdlp_error"]:
            msgs.append(f"yt-dlp could not be loaded ({STATE['ytdlp_error']}). Run: pip install -U yt-dlp")
        if not STATE["ffmpeg"]:
            msgs.append("FFmpeg was not found. Best Available falls back to a single pre-merged stream "
                        "(often lower than the maximum resolution) and MP3 conversion is disabled. "
                        "See Settings → Dependencies for setup help.")
        self.banner.setText("\n".join(msgs))
        self.banner.setVisible(bool(msgs))
        self.analyze_btn.setEnabled(not STATE["ytdlp_error"])

    def set_status(self, text, kind="muted"):
        color = {"muted": C["muted"], "warn": C["warn"], "error": C["danger"], "ok": C["ok"]}[kind]
        self.status.setStyleSheet(f"color:{color};")
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def toggle_details(self):
        show = not self.details.isVisible()
        self.details.setVisible(show)
        self.details_btn.setText("Hide technical details" if show else "Show technical details")

    def clear_all(self):
        self.url.clear()
        self.result.hide()
        self.set_status("")
        self.details_btn.hide()
        self.details.hide()
        self.info = None

    # --- analyze
    def analyze(self):
        url = self.url.text().strip()
        if not valid_url(url):
            self.set_status("Please enter a valid link starting with http:// or https://", "warn")
            return
        if self.worker and self.worker.isRunning():
            return
        self.result.hide()
        self.details_btn.hide()
        self.details.hide()
        self.analyze_btn.setEnabled(False)
        self.set_status("Analyzing link…")
        self.worker = AnalyzeWorker(url)
        self.worker.done.connect(self.on_info)
        self.worker.failed.connect(self.on_fail)
        self.worker.start()

    def on_fail(self, friendly, tech):
        self.analyze_btn.setEnabled(not STATE["ytdlp_error"])
        self.set_status(friendly, "error")
        self.details.setPlainText(tech)
        self.details_btn.setText("Show technical details")
        self.details_btn.show()

    def on_info(self, info):
        self.analyze_btn.setEnabled(not STATE["ytdlp_error"])
        self.info = info
        self.set_status("Analysis complete.", "ok")
        self.title_lbl.setText(info["title"])
        fm = info["formats"]
        shorts = sorted({f["s"] for f in fm if f["s"]}, reverse=True)
        fps = sorted({round(f["fps"]) for f in fm if f["fps"] and f["vcodec"] != "none"}, reverse=True)
        self.meta["Platform"].setText(info["platform"])
        self.meta["Duration"].setText(fmt_duration(info["duration"]))
        self.meta["Resolutions"].setText(" · ".join(f"{s}p" for s in shorts[:10]) or "Audio only / unknown")
        self.meta["Frame rates"].setText(" · ".join(f"{x} fps" for x in fps[:6]) or "Not reported")
        self.meta["Formats"].setText(", ".join(sorted({f["ext"] for f in fm})) or "Unknown")
        pm = QPixmap()
        if info["thumb"] and pm.loadFromData(info["thumb"]):
            self.thumb.setPixmap(pm.scaled(320, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("No thumbnail")
        # quality choices based on formats that actually exist
        self.quality.blockSignals(True)
        self.quality.clear()
        self.quality.addItems(QUALITIES)
        model = self.quality.model()
        has_audio = any(f["acodec"] != "none" for f in fm)
        for i, name in enumerate(QUALITIES):
            ok = True
            if name == "Audio Only":
                ok = has_audio
            elif name != "Best Available":
                rungs = [x for _, x in LADDER]
                n = dict(LADDER)[name]
                lower = rungs[rungs.index(n) + 1] if rungs.index(n) + 1 < len(rungs) else 0
                ok = any(lower < s / 1.06 <= n for s in shorts)
            if not ok:
                item = model.item(i)
                item.setEnabled(False)
                item.setForeground(QColor("#475569"))
                item.setToolTip("Not available for this media")
        wanted = get_setting("quality")
        idx = QUALITIES.index(wanted) if wanted in QUALITIES else 0
        if not model.item(idx).isEnabled():
            idx = 0
        self.quality.setCurrentIndex(idx)
        self.quality.blockSignals(False)
        self.on_quality()
        self.result.show()

    def estimate(self, label):
        if not self.info:
            return None
        fm = self.info["formats"]
        auds = [f for f in fm if f["vcodec"] == "none" and f["acodec"] != "none"]
        best_a = max(auds, key=lambda f: f["abr"] or f["tbr"], default=None)
        if label == "Audio Only":
            return (best_a or {}).get("size")
        lim = quality_limit(label)
        vids = [f for f in fm if f["vcodec"] != "none" and f["s"] and (lim is None or f["s"] <= lim)]
        if not STATE["ffmpeg"]:
            vids = [f for f in vids if f["acodec"] != "none"]
        if not vids:
            return None
        v = max(vids, key=lambda f: (f["s"], f["fps"] or 0, f["tbr"]))
        if v["acodec"] != "none" or not STATE["ffmpeg"]:
            return v["size"]
        return v["size"] + best_a["size"] if v["size"] and best_a and best_a["size"] else None

    def on_quality(self):
        if not self.info:
            return
        label = self.quality.currentText()
        lim = quality_limit(label)
        shorts = [f["s"] for f in self.info["formats"] if f["s"]]
        note = ""
        if label == "Audio Only":
            note = ("Best available audio stream" +
                    (", converted to MP3." if get_setting("mp3") == "1" and STATE["ffmpeg"] else "."))
        elif shorts:
            cands = [s for s in shorts if lim is None or s <= lim]
            top = max(cands) if cands else None
            if lim is None:
                note = f"Highest available: {top}p."
            elif top:
                note = f"Closest available within {label}: {top}p."
                if top < dict(LADDER)[label] * 0.9:
                    note += f" No exact {label} stream exists for this media."
        if not STATE["ffmpeg"] and label != "Audio Only":
            note += " FFmpeg missing: only pre-merged streams can be used."
        est = self.estimate(label)
        self.q_note.setText(note.strip())
        self.meta["Estimated size"].setText(f"≈ {fmt_size(est)} (estimate)" if est else "Unknown")

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose download folder", get_setting("output_dir"))
        if d:
            set_setting("output_dir", d)
            self.refresh_settings()
            self.window().settings_changed.emit()

    def open_dir(self):
        d = safe_outdir(get_setting("output_dir"))
        d.mkdir(parents=True, exist_ok=True)
        open_path(str(d))

    def download(self):
        if not self.info:
            return
        self.mgr.add(self.info["url"], self.info["title"], self.info["platform"],
                     self.quality.currentText(), self.info["portrait"])
        self.set_status("Added to downloads.", "ok")
        self.queued.emit()


class JobsPage(QWidget):
    """Shows jobs of the current session. mode: 'downloads' (not queued) or 'queue' (waiting)."""

    def __init__(self, mgr, mode, title, subtitle):
        super().__init__()
        self.mgr, self.mode, self._key, self.bars = mgr, mode, None, {}
        lay = QVBoxLayout()
        lay.addWidget(mklabel(title, "h1"))
        lay.addWidget(mklabel(subtitle, "muted"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Title", "Platform", "Quality", "Progress", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(44)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 200)
        for c, wd in ((1, 110), (2, 150), (4, 230)):
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            self.table.setColumnWidth(c, wd)
        self.table.setMinimumHeight(360)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        specs = [("Cancel", self.cancel), ("Retry", self.retry), ("Open file", self.open_file),
                 ("Open folder", self.open_folder), ("Clear finished", self.clear)]
        for text, fn in specs:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page_wrapper(lay))
        mgr.changed.connect(self.refresh)
        self.refresh()

    def visible_jobs(self):
        if self.mode == "queue":
            return [j for j in self.mgr.jobs if j.status == "Queued"]
        return [j for j in self.mgr.jobs if j.status != "Queued"]

    def selected(self):
        r = self.table.currentRow()
        if r < 0 or not self.table.item(r, 0):
            return None
        jid = self.table.item(r, 0).data(Qt.UserRole)
        return next((j for j in self.mgr.jobs if j.id == jid), None)

    @staticmethod
    def status_text(j):
        if j.cancelling and j.status in ACTIVE:
            return "Cancelling…"
        if j.status == "Downloading":
            bits = [f"{j.pct:.0f}%" if j.pct >= 0 else "Downloading"]
            if j.speed:
                bits.append(j.speed)
            if j.eta:
                bits.append(f"ETA {j.eta}")
            return " · ".join(bits)
        if j.status == "Processing":
            return "Processing (merging/converting)…"
        if j.status == "Failed":
            return "Failed: " + j.error.split("\n")[0]
        return j.status

    def refresh(self):
        jobs = self.visible_jobs()
        key = [(j.id, j.status) for j in jobs]
        if key != self._key:
            keep = self.selected()
            self._key, self.bars = key, {}
            self.table.setRowCount(len(jobs))
            for r, j in enumerate(jobs):
                for c, text in enumerate((j.title, j.platform, j.quality)):
                    it = QTableWidgetItem(text)
                    it.setToolTip(text)
                    it.setData(Qt.UserRole, j.id)
                    self.table.setItem(r, c, it)
                bar = QProgressBar()
                bar.setRange(0, 100)
                self.bars[j.id] = bar
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(8, 0, 8, 0)
                hl.addWidget(bar)
                self.table.setCellWidget(r, 3, holder)
                self.table.setItem(r, 4, QTableWidgetItem())
                if keep and keep.id == j.id:
                    self.table.selectRow(r)
        for r, j in enumerate(jobs):
            bar = self.bars.get(j.id)
            if bar:
                bar.setRange(0, 0) if (j.status == "Downloading" and j.pct < 0) else bar.setRange(0, 100)
                if j.pct >= 0:
                    bar.setValue(int(j.pct))
            it = self.table.item(r, 4)
            if it:
                it.setText(self.status_text(j))
                it.setToolTip(j.error or "")
                color = {"Completed": C["ok"], "Failed": C["danger"], "Cancelled": C["muted"]}.get(j.status, C["text"])
                it.setForeground(QColor(color))
            q = self.table.item(r, 2)
            if q and j.actual:
                q.setText(f"{j.quality} → {j.actual}")

    def cancel(self):
        j = self.selected()
        if j:
            self.mgr.cancel(j)

    def retry(self):
        j = self.selected()
        if j:
            self.mgr.retry(j)

    def open_file(self):
        j = self.selected()
        if j and not open_path(j.path):
            QMessageBox.information(self, "Open file", "This job has no finished file to open yet.")

    def open_folder(self):
        j = self.selected()
        if j and j.path and open_folder_of(j.path):
            return
        d = safe_outdir(get_setting("output_dir"))
        d.mkdir(parents=True, exist_ok=True)
        open_path(str(d))

    def clear(self):
        self.mgr.clear_finished()


class HistoryPage(QWidget):
    def __init__(self, mgr, redownload):
        super().__init__()
        self.redownload, self.rows = redownload, []
        lay = QVBoxLayout()
        lay.addWidget(mklabel("History", "h1"))
        lay.addWidget(mklabel("Records are kept between sessions. Removing a record never deletes the media file.", "muted"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, link, or platform…")
        self.search.textChanged.connect(self.refresh)
        lay.addWidget(self.search)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Date", "Title", "Platform", "Quality", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(360)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c, wd in ((0, 140), (2, 110), (3, 170), (4, 120)):
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            self.table.setColumnWidth(c, wd)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in (("Open file", self.open_file), ("Open folder", self.open_folder),
                         ("Download again", self.again), ("Remove record", self.remove),
                         ("Delete file…", self.delete_file), ("Clear history…", self.clear)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page_wrapper(lay))
        self.refresh()

    def refresh(self):
        term = f"%{self.search.text().strip()}%"
        self.rows, _ = sql("SELECT * FROM history WHERE title LIKE ? OR url LIKE ? OR platform LIKE ? "
                           "ORDER BY id DESC LIMIT 500", (term, term, term))
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            for c, key in enumerate(("date", "title", "platform", "quality", "status")):
                it = QTableWidgetItem(row[key] or "")
                it.setToolTip(row["path"] or row["url"] or "")
                if key == "status":
                    color = {"Completed": C["ok"], "Failed": C["danger"]}.get(row["status"], C["muted"])
                    it.setForeground(QColor(color))
                self.table.setItem(r, c, it)

    def current(self):
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def open_file(self):
        row = self.current()
        if row and not open_path(row["path"]):
            QMessageBox.information(self, "Open file", "The file no longer exists or was never completed.")

    def open_folder(self):
        row = self.current()
        if row and not open_folder_of(row["path"]):
            QMessageBox.information(self, "Open folder", "The folder for this item could not be found.")

    def again(self):
        row = self.current()
        if row:
            quality = (row["quality"] or "Best Available").split(" (")[0]
            self.redownload(row["url"], row["title"], row["platform"],
                            quality if quality in QUALITIES else "Best Available")

    def remove(self):
        row = self.current()
        if row and row["status"] not in ACTIVE + ("Queued",):
            sql("DELETE FROM history WHERE id=?", (row["id"],))
            self.refresh()

    def delete_file(self):
        row = self.current()
        if not row or not row["path"] or not os.path.isfile(row["path"]):
            QMessageBox.information(self, "Delete file", "No existing file is recorded for this item.")
            return
        ok = QMessageBox.warning(self, "Delete file permanently?",
                                 f"This permanently deletes the media file from disk:\n\n{row['path']}\n\n"
                                 "This cannot be undone.", QMessageBox.Yes | QMessageBox.Cancel,
                                 QMessageBox.Cancel)
        if ok == QMessageBox.Yes:
            try:
                os.remove(row["path"])
                sql("UPDATE history SET status='File deleted' WHERE id=?", (row["id"],))
            except OSError as e:
                QMessageBox.critical(self, "Delete failed", str(e))
            self.refresh()

    def clear(self):
        ok = QMessageBox.question(self, "Clear history?",
                                  "This removes all finished history records. Downloaded files are NOT deleted.",
                                  QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if ok == QMessageBox.Yes:
            sql("DELETE FROM history WHERE status NOT IN ('Queued','Downloading','Processing')")
            self.refresh()


class SettingsPage(QWidget):
    def __init__(self, on_change, recheck):
        super().__init__()
        self.on_change, self.recheck = on_change, recheck
        lay = QVBoxLayout()
        lay.setSpacing(16)
        lay.addWidget(mklabel("Settings", "h1"))
        box = card()
        g = QGridLayout(box)
        g.setContentsMargins(22, 20, 22, 20)
        g.setVerticalSpacing(14)
        g.setHorizontalSpacing(20)
        self.outdir = QLineEdit(get_setting("output_dir"))
        self.outdir.editingFinished.connect(lambda: self.save("output_dir", self.outdir.text().strip() or DEFAULTS["output_dir"]))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        self.quality = QComboBox()
        self.quality.addItems(QUALITIES)
        self.quality.setCurrentText(get_setting("quality"))
        self.quality.currentTextChanged.connect(lambda v: self.save("quality", v))
        self.conc = QSpinBox()
        self.conc.setRange(1, 5)
        self.conc.setValue(int(get_setting("max_concurrent")))
        self.conc.valueChanged.connect(lambda v: self.save("max_concurrent", v))
        self.container = QComboBox()
        self.container.addItems(CONTAINERS)
        self.container.setCurrentText(get_setting("container"))
        self.container.currentTextChanged.connect(lambda v: self.save("container", v))
        self.mp3 = QCheckBox("Convert audio-only downloads to MP3 (requires FFmpeg)")
        self.mp3.setChecked(get_setting("mp3") == "1")
        self.mp3.toggled.connect(lambda v: self.save("mp3", "1" if v else "0"))
        self.splash = QCheckBox("Show the full-screen splash at startup")
        self.splash.setChecked(get_setting("splash") == "1")
        self.splash.toggled.connect(lambda v: self.save("splash", "1" if v else "0"))
        rows = [("Download folder", self.outdir, browse), ("Default quality", self.quality, None),
                ("Max concurrent downloads", self.conc, None),
                ("Preferred container", self.container, None)]
        for i, (name, wdg, extra) in enumerate(rows):
            g.addWidget(mklabel(name, "muted"), i, 0)
            g.addWidget(wdg, i, 1)
            if extra:
                g.addWidget(extra, i, 2)
        g.addWidget(mklabel("“auto” lets yt-dlp choose; mp4/mkv/webm force the merged container when FFmpeg is available.", "muted", True), len(rows), 1, 1, 2)
        g.addWidget(self.mp3, len(rows) + 1, 1, 1, 2)
        g.addWidget(self.splash, len(rows) + 2, 1, 1, 2)
        g.setColumnStretch(1, 1)
        lay.addWidget(box)

        lay.addWidget(mklabel("Dependencies", "h2"))
        dep = card()
        dl = QVBoxLayout(dep)
        dl.setContentsMargins(22, 20, 22, 20)
        self.dep_lbl = mklabel("", None, True)
        self.dep_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dl.addWidget(self.dep_lbl)
        btn = QPushButton("Re-check FFmpeg")
        btn.clicked.connect(self.recheck)
        dl.addWidget(btn, 0, Qt.AlignLeft)
        lay.addWidget(dep)
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page_wrapper(lay))
        self.refresh_deps()

    def refresh_deps(self):
        py = sys.version.split()[0]
        yt = STATE["ytdlp_version"] or f"not available ({STATE['ytdlp_error']})"
        if STATE["ffmpeg"]:
            ff = f"found, version {STATE['ffmpeg_version']}\n{STATE['ffmpeg']}"
            help_txt = ""
        else:
            ff = "NOT found"
            help_txt = ("\n\nWithout FFmpeg: Best Available can only use pre-merged streams (often lower resolution), "
                        "the container setting is ignored, and MP3 conversion is disabled.\n"
                        "Install it with:  winget install Gyan.FFmpeg   (then restart ClipOrbit)\n"
                        "or download a build from ffmpeg.org and either add its bin folder to PATH or place "
                        "ffmpeg.exe next to ClipOrbit.")
        self.dep_lbl.setText(f"Python: {py}\nyt-dlp: {yt}\nFFmpeg: {ff}{help_txt}")

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose download folder", get_setting("output_dir"))
        if d:
            self.outdir.setText(d)
            self.save("output_dir", d)

    def save(self, key, value):
        set_setting(key, value)
        self.on_change()


class AboutPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout()
        lay.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(150))
        lay.addWidget(logo, 0, Qt.AlignLeft)
        lay.addWidget(mklabel("ClipOrbit Desktop", "h1"))
        lay.addWidget(mklabel(TAGLINE, "h2"))
        lay.addWidget(mklabel(f"Version {APP_VERSION}  ·  A MUdevelops Project", "muted"))
        lay.addWidget(mklabel(
            "ClipOrbit Desktop is a local media downloader built on yt-dlp and FFmpeg. Everything runs on your "
            "computer: no accounts, no telemetry, no cloud services. Settings and history are stored in a local "
            "SQLite database.", None, True))
        lay.addWidget(mklabel(
            "Use it only for content you own or are authorized to download, and respect the terms of each "
            "platform. ClipOrbit does not bypass DRM, private-content controls, or logins. Availability varies "
            "by platform and by post.", "muted", True))
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page_wrapper(lay))


class MainWindow(QMainWindow):
    settings_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(logo_pixmap(256)))
        self.resize(1280, 800)
        self.mgr = JobManager()
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(230)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 20, 14, 16)
        sl.setSpacing(4)
        brand = QHBoxLayout()
        lg = QLabel()
        lg.setPixmap(logo_pixmap(44))
        brand.addWidget(lg)
        brand.addWidget(mklabel("ClipOrbit", "h2"))
        brand.addStretch(1)
        sl.addLayout(brand)
        sl.addSpacing(18)

        self.stack = QStackedWidget()
        self.home = HomePage(self.mgr)
        self.downloads = JobsPage(self.mgr, "downloads", "Downloads", "Active and recently finished downloads in this session.")
        self.queue = JobsPage(self.mgr, "queue", "Queue", "Jobs waiting for a free download slot (limit set in Settings).")
        self.history = HistoryPage(self.mgr, self.add_again)
        self.settings = SettingsPage(self.settings_changed.emit, self.recheck_ffmpeg)
        self.about = AboutPage()
        names = [("Home", "home", self.home), ("Downloads", "download", self.downloads),
                 ("Queue", "queue", self.queue), ("History", "history", self.history),
                 ("Settings", "settings", self.settings), ("About", "about", self.about)]
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.nav = []
        for i, (label, icon, page) in enumerate(names):
            self.stack.addWidget(page)
            b = QPushButton("  " + label)
            b.setObjectName("nav")
            b.setCheckable(True)
            ic = QIcon()
            ic.addPixmap(make_icon(icon, C["muted"]), QIcon.Normal, QIcon.Off)
            ic.addPixmap(make_icon(icon, C["cyan"]), QIcon.Normal, QIcon.On)
            b.setIcon(ic)
            b.setIconSize(QSize(22, 22))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self.go(idx))
            self.group.addButton(b)
            sl.addWidget(b)
            self.nav.append((b, label))
        sl.addStretch(1)
        self.chip = mklabel("", "chip", True)
        sl.addWidget(self.chip)
        h.addWidget(side)
        h.addWidget(self.stack, 1)

        self.home.queued.connect(lambda: self.go(1))
        self.settings_changed.connect(self.home.refresh_settings)
        self.settings_changed.connect(self.home.on_quality)
        self.mgr.changed.connect(self.update_counts)
        self.mgr.job_done.connect(self.notify)
        self.mgr.job_done.connect(lambda _j: self.history.refresh())
        self.tray = QSystemTrayIcon(self.windowIcon(), self) if QSystemTrayIcon.isSystemTrayAvailable() else None
        if self.tray:
            self.tray.show()
        self.go(0)
        self.refresh_deps()

    def go(self, idx):
        self.stack.setCurrentIndex(idx)
        self.nav[idx][0].setChecked(True)
        if idx == 3:
            self.history.refresh()

    def update_counts(self):
        active = sum(1 for j in self.mgr.jobs if j.status in ACTIVE)
        queued = sum(1 for j in self.mgr.jobs if j.status == "Queued")
        for idx, count in ((1, active), (2, queued)):
            b, label = self.nav[idx]
            b.setText(f"  {label}" + (f"  ({count})" if count else ""))

    def refresh_deps(self):
        self.home.refresh_deps()
        self.settings.refresh_deps()
        ok = bool(STATE["ffmpeg"])
        self.chip.setText(("● FFmpeg ready" if ok else "● FFmpeg missing") +
                          f"\nyt-dlp {STATE['ytdlp_version'] or 'unavailable'}")
        self.chip.setStyleSheet(f"color:{C['ok'] if ok else C['warn']};")

    def recheck_ffmpeg(self):
        STATE["ffmpeg"] = find_ffmpeg()
        STATE["ffmpeg_version"] = ffmpeg_version(STATE["ffmpeg"]) if STATE["ffmpeg"] else ""
        self.refresh_deps()
        self.home.on_quality()

    def add_again(self, url, title, platform, quality):
        self.mgr.add(url, title, platform, quality)
        self.go(1)

    def notify(self, job):
        msg = {"Completed": f"Download complete: {job.title}", "Failed": f"Download failed: {job.title}",
               "Cancelled": f"Download cancelled: {job.title}"}.get(job.status)
        if msg:
            self.statusBar().showMessage(msg, 8000)
            if self.tray and job.status != "Cancelled":
                self.tray.showMessage(APP_NAME, msg, QSystemTrayIcon.Information, 5000)

    def closeEvent(self, event):
        if any(j.status in ACTIVE or j.status == "Queued" for j in self.mgr.jobs):
            ok = QMessageBox.question(self, "Quit ClipOrbit?", "Downloads are still in progress. Cancel them and quit?",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                event.ignore()
                return
        self.mgr.shutdown()
        if self.tray:
            self.tray.hide()
        event.accept()


# ----------------------------------------------------------------------------------------
# 6. ENTRY POINT
# ----------------------------------------------------------------------------------------
def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MUdevelops.ClipOrbit.Desktop.1")
        except Exception:
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QCoreApplication.setOrganizationName("MUdevelops")
    QCoreApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setWindowIcon(QIcon(logo_pixmap(256)))
    init_db()

    ctx = {}
    splash = Splash() if get_setting("splash") == "1" else None
    if splash:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        splash.setScreen(screen)
        splash.setGeometry(screen.geometry())
        splash.showFullScreen()

    def on_ready():
        if splash:
            splash.set_status("Opening ClipOrbit")
        QTimer.singleShot(30, open_main)

    def open_main():
        ctx["win"] = MainWindow()
        ctx["win"].showMaximized()
        if splash:
            splash.fade_out()

    worker = InitWorker()
    ctx["worker"] = worker
    if splash:
        worker.status.connect(splash.set_status)
    worker.done.connect(on_ready)
    worker.start()
    code = app.exec()
    worker.wait(2000)
    sys.exit(code)


if __name__ == "__main__":
    main()
