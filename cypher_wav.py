import json
import math
import os
import random
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QPoint, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    import qtawesome as qta
    _IC = "#f5c400"
except Exception:
    qta = None
    _IC = "#f5c400"

import yt_dlp

# ── persistence ────────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
PLAYLISTS_FILE = os.path.join(_DIR, "playlists.json")

# ── page indices ───────────────────────────────────────────────────────────────
PAGE_HOME    = 0
PAGE_SEARCH  = 1
PAGE_NOWPLAY = 2
PAGE_LIBRARY = 3

LOOP_OFF = 0
LOOP_ONE = 1
LOOP_ALL = 2


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Track:
    title:      str
    artist:     str
    duration:   int
    page_url:   str
    stream_url: str = ""   # empty = not yet resolved


# ─────────────────────────────────────────────────────────────────────────────
# Search engine  (two-phase)
# ─────────────────────────────────────────────────────────────────────────────
class CypherSearchEngine:
    """Phase-1: fast metadata only (~1-2 s).  Phase-2: resolve one URL on demand."""

    @staticmethod
    def _fmt(seconds: int) -> str:
        if seconds <= 0:
            return "--:--"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def search_metadata(self, query: str, limit: int = 12) -> List[Track]:
        """Returns tracks with stream_url="" — very fast."""
        query = query.strip()
        if not query:
            return []
        opts = {
            "quiet": True, "no_warnings": True,
            "skip_download": True, "noplaylist": True,
            "extract_flat": "in_playlist",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        tracks: List[Track] = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            url = e.get("webpage_url") or e.get("url")
            if not url:
                continue
            tracks.append(Track(
                title=e.get("title") or "UNKNOWN",
                artist=e.get("uploader") or e.get("channel") or "UNKNOWN",
                duration=int(e.get("duration") or 0),
                page_url=url,
                stream_url="",
            ))
        return tracks

    def resolve_stream_url(self, page_url: str) -> str:
        """Phase-2: resolve direct audio URL for one track (prefers m4a/AAC)."""
        opts = {
            "quiet": True, "no_warnings": True,
            "skip_download": True, "noplaylist": True,
            "format": "bestaudio[ext=m4a]/bestaudio[acodec=mp4a.40.2]/140/bestaudio/best",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(page_url, download=False)
        if not data:
            return ""
        direct = data.get("url")
        if direct:
            return direct
        for fmt in reversed(data.get("formats") or []):
            if fmt.get("url") and fmt.get("acodec") != "none":
                return fmt["url"]
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Background workers
# ─────────────────────────────────────────────────────────────────────────────
class SearchWorker(QObject):
    finished = pyqtSignal(list)
    failed   = pyqtSignal(str)

    def __init__(self, engine: CypherSearchEngine, query: str) -> None:
        super().__init__()
        self.engine = engine
        self.query  = query

    def run(self) -> None:
        try:
            self.finished.emit(self.engine.search_metadata(self.query))
        except Exception as exc:
            self.failed.emit(str(exc))


class StreamResolveWorker(QObject):
    resolved = pyqtSignal(int, str)   # row, url
    failed   = pyqtSignal(int)

    def __init__(self, engine: CypherSearchEngine, row: int, page_url: str) -> None:
        super().__init__()
        self.engine   = engine
        self.row      = row
        self.page_url = page_url

    def run(self) -> None:
        url = self.engine.resolve_stream_url(self.page_url)
        if url:
            self.resolved.emit(self.row, url)
        else:
            self.failed.emit(self.row)


# ─────────────────────────────────────────────────────────────────────────────
# EQ bars widget
# ─────────────────────────────────────────────────────────────────────────────
class EqBarsWidget(QWidget):
    def __init__(self, num_bars: int = 9, size=(140, 52)) -> None:
        super().__init__()
        self._n = num_bars
        self.setFixedSize(*size)
        self._h = [0.08] * self._n
        self._timer = QTimer(self)
        self._timer.setInterval(110)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None: self._timer.start()
    def stop(self) -> None:
        self._timer.stop()
        self._h = [0.08] * self._n
        self.update()

    def _tick(self) -> None:
        self._h = [random.uniform(0.2, 1.0) for _ in range(self._n)]
        self.update()

    def paintEvent(self, _) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        gap = 3
        bw = (w - gap * (self._n + 1)) // self._n
        for i, r in enumerate(self._h):
            bh = int(h * r)
            x = gap + i * (bw + gap)
            p.fillRect(x, h - bh, bw, bh, QColor(245, 196, 0, int(150 + 105 * r)))


# ─────────────────────────────────────────────────────────────────────────────
# Circular visualizer widget
# ─────────────────────────────────────────────────────────────────────────────
class VisualizerWidget(QWidget):
    NUM = 28

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(420, 420)
        self._bars  = [0.1] * self.NUM
        self._angle = 0.0
        self._t = QTimer(self)
        self._t.setInterval(65)
        self._t.timeout.connect(self._tick)

    def start(self) -> None: self._t.start()
    def stop(self) -> None:
        self._t.stop()
        self._bars = [0.05] * self.NUM
        self.update()

    def _tick(self) -> None:
        self._bars  = [random.uniform(0.12, 1.0) for _ in range(self.NUM)]
        self._angle = (self._angle + 2.2) % 360
        self.update()

    def paintEvent(self, _) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        base_r = min(w, h) * 0.27

        for i, bar in enumerate(self._bars):
            ang = math.radians(self._angle + i * (360 / self.NUM))
            bar_len = base_r * 0.72 * bar
            x1 = cx + base_r * math.cos(ang)
            y1 = cy + base_r * math.sin(ang)
            x2 = cx + (base_r + bar_len) * math.cos(ang)
            y2 = cy + (base_r + bar_len) * math.sin(ang)
            alpha = int(100 + 155 * bar)
            p.setPen(QPen(QColor(245, 196, 0, alpha), max(2, int(4 * bar))))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

        # inner ring
        p.setPen(QPen(QColor(245, 196, 0, 70), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(int(cx - base_r), int(cy - base_r), int(base_r * 2), int(base_r * 2))

        # pulse center circle
        pulse_r = 10 + 6 * (self._bars[0])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#f5c400"))
        p.drawEllipse(int(cx - pulse_r), int(cy - pulse_r), int(pulse_r * 2), int(pulse_r * 2))

        # small rotating dot on ring
        dot_ang = math.radians(self._angle * 3)
        dx = cx + base_r * math.cos(dot_ang)
        dy = cy + base_r * math.sin(dot_ang)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(int(dx - 4), int(dy - 4), 8, 8)


# ─────────────────────────────────────────────────────────────────────────────
# Visualizer popup dialog
# ─────────────────────────────────────────────────────────────────────────────
class VisualizerDialog(QDialog):
    def __init__(self, title: str, artist: str, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(580, 640)
        self._drag_pos = QPoint()
        self.setStyleSheet("""
            QDialog { background:#060400; border:2px solid #f5c400; border-radius:16px; }
            QLabel  { color:#f5e88a; border:none; font-family:Consolas; }
            QPushButton {
                background:#1a1500; border:1px solid #3a2e00;
                border-radius:8px; color:#f5c400; padding:7px 18px; font-family:Consolas;
            }
            QPushButton:hover { border-color:#f5c400; background:#2a2000; }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 20)
        lay.setSpacing(10)

        hdr = QHBoxLayout()
        live_lbl = QLabel("● LIVE VISUALIZATION")
        live_lbl.setStyleSheet("color:#f5c400; font-size:11px; letter-spacing:3px;")
        hdr.addWidget(live_lbl)
        hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(30, 26)
        x_btn.clicked.connect(self.close)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self.viz = VisualizerWidget()
        lay.addWidget(self.viz, 1)

        self.title_lbl = QLabel(title)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("font-size:18px; font-weight:700; color:#f5e48a; padding:4px;")
        self.title_lbl.setWordWrap(True)
        lay.addWidget(self.title_lbl)

        self.artist_lbl = QLabel(artist.upper())
        self.artist_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.artist_lbl.setStyleSheet("font-size:10px; letter-spacing:5px; color:#a08040;")
        lay.addWidget(self.artist_lbl)

    def start(self) -> None: self.viz.start()
    def stop(self) -> None:  self.viz.stop()

    def update_track(self, title: str, artist: str) -> None:
        self.title_lbl.setText(title)
        self.artist_lbl.setText(artist.upper())

    def closeEvent(self, e) -> None:  # type: ignore[override]
        self.viz.stop()
        super().closeEvent(e)

    # make dialog draggable
    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:  # type: ignore[override]
        if e.buttons() & Qt.MouseButton.LeftButton and not self._drag_pos.isNull():
            self.move(e.globalPosition().toPoint() - self._drag_pos)


# ─────────────────────────────────────────────────────────────────────────────
# Helper button
# ─────────────────────────────────────────────────────────────────────────────
class NeonButton(QPushButton):
    def __init__(self, text: str = "", icon_name: Optional[str] = None) -> None:
        super().__init__(text)
        if qta and icon_name:
            try:
                self.setIcon(qta.icon(icon_name, color=_IC))
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.engine = CypherSearchEngine()
        self.results: List[Track] = []
        self._recent: List[Track] = []
        self.current_index = -1
        self.drag_position = QPoint()
        self.user_dragging_slider = False
        self._is_maximized = False

        # Playback modes
        self._shuffle = False
        self._loop    = LOOP_OFF

        # Workers
        self.worker_thread: Optional[QThread] = None
        self.worker: Optional[SearchWorker] = None
        self.resolve_thread: Optional[QThread] = None
        self.resolve_worker: Optional[StreamResolveWorker] = None
        self._pending_play = False

        # Playlists
        self.playlists: Dict[str, List[Track]] = self._load_playlists()
        self._active_playlist: Optional[str] = None
        # "search" | playlist name — controls next/prev scope
        self._playing_from: str = "search"
        self._pl_play_index: int = -1

        # Visualizer
        self._viz: Optional[VisualizerDialog] = None
        self._current_track: Optional[Track] = None

        self.setWindowTitle("CYPHER.WAV")
        self.setMinimumSize(1200, 760)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.65)

        self._build_ui()
        self._connect_signals()
        self._apply_styles()

    # ── Playlist persistence ───────────────────────────────────────────────
    @staticmethod
    def _load_playlists() -> Dict[str, List[Track]]:
        try:
            if os.path.exists(PLAYLISTS_FILE):
                with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return {n: [Track(**t) for t in ts] for n, ts in data.items()}
        except Exception:
            pass
        return {}

    def _save_playlists(self) -> None:
        try:
            with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
                json.dump({n: [t.__dict__ for t in ts]
                           for n, ts in self.playlists.items()}, f, indent=2)
        except Exception:
            pass

    # ── UI construction ────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        self.outer_layout = QVBoxLayout(root)
        self.outer_layout.setContentsMargins(10, 10, 10, 10)
        self.outer_layout.setSpacing(0)

        self.window_shell = QFrame()
        self.window_shell.setObjectName("window_shell")
        shell = QVBoxLayout(self.window_shell)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # ── Title bar ────────────────────────────────────────────────────
        self.top_chrome = QFrame()
        self.top_chrome.setObjectName("top_chrome")
        chrome = QHBoxLayout(self.top_chrome)
        chrome.setContentsMargins(14, 6, 8, 6)
        chrome.setSpacing(8)

        # Logo  ♬ + title
        self.logo_lbl = QLabel("♬")
        self.logo_lbl.setObjectName("logo_icon")
        self.title_lbl = QLabel("CYPHER.WAV")
        self.title_lbl.setObjectName("logo_label")
        self.sub_lbl = QLabel("// CYBER STREAM NODE")
        self.sub_lbl.setObjectName("subtitle_label")
        chrome.addWidget(self.logo_lbl)
        chrome.addWidget(self.title_lbl)
        chrome.addWidget(self.sub_lbl)
        chrome.addStretch()

        for sym, attr in (("─", "min_btn"), ("□", "max_btn"), ("✕", "close_btn")):
            b = QPushButton(sym)
            b.setObjectName("chrome_btn")
            b.setFixedSize(38, 30)
            setattr(self, attr, b)
            chrome.addWidget(b)
        shell.addWidget(self.top_chrome)

        # ── Body ──────────────────────────────────────────────────────────
        self.main_frame = QFrame()
        self.main_frame.setObjectName("main_frame")
        body = QHBoxLayout(self.main_frame)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # ── Sidebar ────────────────────────────────────────────────────
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(214)
        sb = QVBoxLayout(self.sidebar)
        sb.setContentsMargins(14, 18, 14, 14)
        sb.setSpacing(5)

        self.neural_label = QLabel("made by : NAMAN")
        self.neural_label.setObjectName("neural_label")
        sb.addWidget(self.neural_label)
        sb.addSpacing(10)

        self.home_btn    = NeonButton("  Home",    "fa5s.home")
        self.search_btn  = NeonButton("  Search",  "fa5s.search")
        self.library_btn = NeonButton("  Library", "fa5s.compact-disc")
        for b in (self.home_btn, self.search_btn, self.library_btn):
            b.setObjectName("nav_btn")
            sb.addWidget(b)

        sb.addSpacing(14)
        sb.addWidget(self._divider())

        # Playlists sub-section
        pl_row = QHBoxLayout()
        pl_lbl = QLabel("YOUR PLAYLISTS")
        pl_lbl.setObjectName("section_label")
        self.create_pl_btn = QPushButton("+")
        self.create_pl_btn.setObjectName("small_btn")
        self.create_pl_btn.setFixedSize(26, 24)
        self.create_pl_btn.setToolTip("Create playlist")
        pl_row.addWidget(pl_lbl)
        pl_row.addStretch()
        pl_row.addWidget(self.create_pl_btn)
        sb.addLayout(pl_row)

        self.sb_playlist_list = QListWidget()
        self.sb_playlist_list.setObjectName("sb_playlist_list")
        self.sb_playlist_list.setMaximumHeight(200)
        sb.addWidget(self.sb_playlist_list)
        sb.addStretch()
        self._refresh_sb_playlists()

        body.addWidget(self.sidebar)

        # ── Right area ─────────────────────────────────────────────────
        self.right_frame = QFrame()
        self.right_frame.setObjectName("right_frame")
        right = QVBoxLayout(self.right_frame)
        right.setContentsMargins(16, 12, 16, 8)
        right.setSpacing(7)

        # Search bar always visible
        self.search_input   = QLineEdit()
        self.search_input.setPlaceholderText("SEARCH_DATABASE...")
        self.search_input.setClearButtonEnabled(True)
        self.search_trigger = NeonButton("SCAN", "fa5s.bolt")
        sr = QHBoxLayout()
        sr.addWidget(self.search_input)
        sr.addWidget(self.search_trigger)
        right.addLayout(sr)

        self.status_text = QLabel("SYSTEM READY")
        self.status_text.setObjectName("status_text")
        right.addWidget(self.status_text)

        # ── Content stack ────────────────────────────────────────────────
        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("content_stack")

        # PAGE_HOME (0) ──────────────────────────────────────────────────
        home_pg = QWidget()
        home_pg.setObjectName("inner_page")
        hl = QVBoxLayout(home_pg)
        hl.setContentsMargins(8, 4, 8, 4)
        hl.setSpacing(12)

        hw = QLabel("WELCOME BACK, OPERATOR")
        hw.setObjectName("page_title")
        hl.addWidget(hw)

        rl = QLabel("RECENTLY PLAYED")
        rl.setObjectName("section_label")
        hl.addWidget(rl)

        self.recent_table = QTableWidget(0, 3)
        self.recent_table.setHorizontalHeaderLabels(["TRACK", "ARTIST", "DURATION"])
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.recent_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.recent_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.recent_table.horizontalHeader().setStretchLastSection(True)
        self.recent_table.setAlternatingRowColors(True)
        self.recent_table.setMaximumHeight(200)
        hl.addWidget(self.recent_table)
        hl.addStretch()
        self.content_stack.addWidget(home_pg)   # PAGE_HOME = 0

        # PAGE_SEARCH (1) ────────────────────────────────────────────────
        search_pg = QWidget()
        search_pg.setObjectName("inner_page")
        sl2 = QVBoxLayout(search_pg)
        sl2.setContentsMargins(0, 0, 0, 0)
        self.result_table = QTableWidget(0, 3)
        self.result_table.setHorizontalHeaderLabels(["TRACK", "ARTIST", "DURATION"])
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.horizontalHeader().setDefaultSectionSize(320)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        sl2.addWidget(self.result_table)
        self.content_stack.addWidget(search_pg)  # PAGE_SEARCH = 1

        # PAGE_NOWPLAY (2) ───────────────────────────────────────────────
        self.np_page = QWidget()
        self.np_page.setObjectName("now_playing_page")
        nl = QVBoxLayout(self.np_page)
        nl.setContentsMargins(40, 18, 40, 18)
        nl.setSpacing(10)

        np_top = QHBoxLayout()
        self.back_btn     = NeonButton("← RESULTS",   "fa5s.arrow-left")
        self.add_pl_btn   = NeonButton("+ PLAYLIST",  "fa5s.plus-circle")
        self.back_btn.setObjectName("back_btn")
        self.add_pl_btn.setObjectName("back_btn")
        np_top.addWidget(self.back_btn)
        np_top.addStretch()
        np_top.addWidget(self.add_pl_btn)
        nl.addLayout(np_top)
        nl.addStretch()

        self.np_disc = QLabel("◉")
        self.np_disc.setObjectName("np_disc")
        self.np_disc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nl.addWidget(self.np_disc)

        self.np_title = QLabel("---")
        self.np_title.setObjectName("np_track_name")
        self.np_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.np_title.setWordWrap(True)
        nl.addWidget(self.np_title)

        self.np_artist = QLabel("---")
        self.np_artist.setObjectName("np_artist_name")
        self.np_artist.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nl.addWidget(self.np_artist)

        self.eq_bars = EqBarsWidget(num_bars=9, size=(160, 52))
        nl.addWidget(self.eq_bars, 0, Qt.AlignmentFlag.AlignCenter)
        nl.addStretch()
        self.content_stack.addWidget(self.np_page)  # PAGE_NOWPLAY = 2

        # PAGE_LIBRARY (3) ───────────────────────────────────────────────
        lib_pg = QWidget()
        lib_pg.setObjectName("inner_page")
        ll = QVBoxLayout(lib_pg)
        ll.setContentsMargins(8, 4, 8, 4)
        ll.setSpacing(10)

        lib_hdr = QHBoxLayout()
        lib_t = QLabel("YOUR LIBRARY")
        lib_t.setObjectName("page_title")
        self.new_pl_btn = NeonButton("+ NEW PLAYLIST", "fa5s.plus")
        lib_hdr.addWidget(lib_t)
        lib_hdr.addStretch()
        lib_hdr.addWidget(self.new_pl_btn)
        ll.addLayout(lib_hdr)

        self.lib_pl_list = QListWidget()
        self.lib_pl_list.setObjectName("lib_pl_list")
        self.lib_pl_list.setMaximumHeight(160)
        ll.addWidget(self.lib_pl_list)

        pl_tracks_lbl = QLabel("TRACKS IN PLAYLIST")
        pl_tracks_lbl.setObjectName("section_label")
        ll.addWidget(pl_tracks_lbl)

        self.lib_track_table = QTableWidget(0, 3)
        self.lib_track_table.setHorizontalHeaderLabels(["TRACK", "ARTIST", "DURATION"])
        self.lib_track_table.verticalHeader().setVisible(False)
        self.lib_track_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lib_track_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.lib_track_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.lib_track_table.horizontalHeader().setStretchLastSection(True)
        self.lib_track_table.setAlternatingRowColors(True)
        self.lib_track_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        ll.addWidget(self.lib_track_table)
        self.content_stack.addWidget(lib_pg)   # PAGE_LIBRARY = 3

        right.addWidget(self.content_stack)
        body.addWidget(self.right_frame)
        shell.addWidget(self.main_frame)

        # ── Player bar ──────────────────────────────────────────────────
        self.player_bar = QFrame()
        self.player_bar.setObjectName("player_bar")
        pb = QVBoxLayout(self.player_bar)
        pb.setContentsMargins(16, 8, 16, 10)
        pb.setSpacing(5)

        # Clickable now-playing label → opens visualizer
        self.np_label = QLabel("NO ACTIVE SIGNAL")
        self.np_label.setObjectName("now_playing_label")
        self.np_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        pb.addWidget(self.np_label)

        seek_row = QHBoxLayout()
        self.cur_time  = QLabel("00:00")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.tot_time  = QLabel("00:00")
        seek_row.addWidget(self.cur_time)
        seek_row.addWidget(self.seek_slider)
        seek_row.addWidget(self.tot_time)
        pb.addLayout(seek_row)

        ctrl = QHBoxLayout()
        self.shuffle_btn = NeonButton("", "fa5s.random")
        self.prev_btn    = NeonButton("", "fa5s.step-backward")
        self.play_btn    = NeonButton("", "fa5s.play")
        self.play_btn.setObjectName("play_btn")
        self.pause_btn   = NeonButton("", "fa5s.pause")
        self.next_btn    = NeonButton("", "fa5s.step-forward")
        self.loop_btn    = NeonButton("", "fa5s.redo")
        for b in (self.shuffle_btn, self.prev_btn, self.play_btn,
                  self.pause_btn, self.next_btn, self.loop_btn):
            b.setFixedSize(46, 40)
        self.shuffle_btn.setObjectName("mode_btn")
        self.loop_btn.setObjectName("mode_btn")

        ctrl.addWidget(self.shuffle_btn)
        ctrl.addWidget(self.prev_btn)
        ctrl.addWidget(self.play_btn)
        ctrl.addWidget(self.pause_btn)
        ctrl.addWidget(self.next_btn)
        ctrl.addWidget(self.loop_btn)
        ctrl.addStretch()

        self.vol_lbl = QLabel("VOL")
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(65)
        self.vol_slider.setMaximumWidth(150)
        ctrl.addWidget(self.vol_lbl)
        ctrl.addWidget(self.vol_slider)
        pb.addLayout(ctrl)

        shell.addWidget(self.player_bar)
        self.outer_layout.addWidget(self.window_shell)

    @staticmethod
    def _divider() -> QFrame:
        d = QFrame()
        d.setFrameShape(QFrame.Shape.HLine)
        d.setObjectName("hr_line")
        return d

    # ── Signal wiring ──────────────────────────────────────────────────────
    def _connect_signals(self) -> None:
        # Chrome
        self.min_btn.clicked.connect(self.showMinimized)
        self.max_btn.clicked.connect(self._toggle_maximize)
        self.close_btn.clicked.connect(self.close)

        # Navigation
        self.home_btn.clicked.connect(lambda: self._nav(PAGE_HOME))
        self.search_btn.clicked.connect(self._focus_search)
        self.library_btn.clicked.connect(lambda: self._nav(PAGE_LIBRARY))
        self.sb_playlist_list.itemClicked.connect(self._sb_pl_clicked)

        # Search
        self.search_trigger.clicked.connect(self.start_search)
        self.search_input.returnPressed.connect(self.start_search)

        # Results table
        self.result_table.cellClicked.connect(self._preload_row)
        self.result_table.cellDoubleClicked.connect(self._play_result_row)
        self.result_table.customContextMenuRequested.connect(self._results_ctx_menu)

        # Now-playing page
        self.back_btn.clicked.connect(lambda: self._nav(PAGE_SEARCH))
        self.add_pl_btn.clicked.connect(self._add_current_to_pl)

        # Library
        self.new_pl_btn.clicked.connect(self.create_playlist)
        self.create_pl_btn.clicked.connect(self.create_playlist)
        self.lib_pl_list.itemClicked.connect(self._lib_pl_clicked)
        self.lib_track_table.cellDoubleClicked.connect(self._play_lib_row)
        self.lib_track_table.customContextMenuRequested.connect(self._lib_ctx_menu)

        # Recent
        self.recent_table.cellDoubleClicked.connect(self._play_recent_row)

        # Player controls
        self.play_btn.clicked.connect(self.resume_playback)
        self.pause_btn.clicked.connect(self.player.pause)
        self.prev_btn.clicked.connect(self.play_previous)
        self.next_btn.clicked.connect(self.play_next)
        self.shuffle_btn.clicked.connect(self._toggle_shuffle)
        self.loop_btn.clicked.connect(self._toggle_loop)
        self.vol_slider.valueChanged.connect(lambda v: self.audio_output.setVolume(v / 100.0))

        # Seek
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "user_dragging_slider", True))
        self.seek_slider.sliderReleased.connect(self._end_seek)

        # Media
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.playbackStateChanged.connect(self._on_playback_state)

        # Clicking the now-playing label → visualizer
        self.np_label.mousePressEvent = lambda _e: self._open_viz()  # type: ignore

    # ── Styles ────────────────────────────────────────────────────────────
    def _apply_styles(self) -> None:
        QApplication.instance().setFont(QFont("Consolas", 10))
        self.setStyleSheet("""
            QWidget {
                background: #080600;
                color: #f0e6c0;
                font-family: Consolas, 'Share Tech Mono', monospace;
            }
            QFrame  { border: 1px solid #2a2200; border-radius: 8px; }
            QLabel  { border: none; letter-spacing: 1px; }
            QFrame[glitch="true"] { border: 1px solid #ff8c00; }

            #window_shell { border: 1px solid #3a2e00; border-radius: 12px; }

            #top_chrome {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #120e00,stop:1 #1e1600);
                border: none; border-bottom: 1px solid #2e2200;
                border-radius: 0px; min-height: 42px;
            }
            #logo_icon  { color:#f5c400; font-size:22px; border:none; }
            #logo_label { color:#f5c400; font-size:15px; font-weight:700; letter-spacing:3px; border:none; }
            #subtitle_label { color:#4a3c0a; font-size:10px; letter-spacing:2px; border:none; }

            #main_frame  { background:#080600; border:none; border-radius:0px; }
            #right_frame { background:#070500; border:none; border-radius:0px; }
            #inner_page  { background:transparent; border:none; border-radius:0px; }
            #content_stack { background:transparent; border:none; border-radius:0px; }

            #sidebar {
                background:#0d0b00; border:none;
                border-right:1px solid #2e2200; border-radius:0px;
            }
            #player_bar {
                background:#0c0a00; border:none;
                border-top:1px solid #2e2200; border-radius:0px;
            }

            #neural_label {
                color:#f5c400; font-size:12px; font-weight:600;
                letter-spacing:2px; border:none;
            }
            #page_title {
                color:#f5c400; font-size:17px; font-weight:700;
                letter-spacing:2px; border:none;
            }
            #section_label  { color:#6a5018; font-size:9px; letter-spacing:3px; border:none; }
            #status_text    { color:#5a4410; font-size:10px; border:none; }
            #now_playing_label {
                color:#c8aa50; font-size:11px; border:none;
                text-decoration: underline;
            }
            #now_playing_label:hover { color:#f5c400; }

            QPushButton {
                background:#120f00; border:1px solid #3a2e00;
                border-radius:8px; color:#f5c400; padding:7px 13px;
            }
            QPushButton:hover   { border-color:#f5c400; background:#1e1800; }
            QPushButton:pressed { background:#2a2000; }

            #nav_btn { text-align:left; padding-left:8px; }
            #nav_btn[active="true"] { background:#241d00; border-color:#f5c400; }

            #chrome_btn {
                background:transparent; border:none;
                color:#6a5820; border-radius:6px; font-size:13px; padding:2px;
            }
            #chrome_btn:hover { color:#f5c400; background:#1a1500; }

            #play_btn {
                background:#1a1000; border:2px solid #f5c400; border-radius:50%;
            }
            #play_btn:hover { background:#2b1e00; }

            #mode_btn { background:transparent; border:1px solid #2a2000; border-radius:8px; }
            #mode_btn[active="true"] { border:1px solid #f5c400; background:#1a1600; }
            #mode_btn:hover { border-color:#f5c400; }

            #back_btn { background:transparent; border:1px solid #3a2e00; padding:5px 12px; }
            #back_btn:hover { border-color:#f5c400; }
            #small_btn {
                background:transparent; border:1px solid #3a2e00;
                border-radius:5px; color:#f5c400; font-size:12px;
            }
            #small_btn:hover { border-color:#f5c400; }

            QLineEdit {
                border:2px solid #f5c400; border-radius:8px; padding:9px;
                background:#0e0c00; color:#f5e88a;
                selection-background-color:#f5c400; selection-color:#050400;
            }

            QTableWidget {
                border:1px solid #2e2200; background:#080600;
                gridline-color:#1c1600; border-radius:8px;
                alternate-background-color:#0c0900;
            }
            QHeaderView::section {
                background:#100d00; color:#f5c400;
                border:none; border-bottom:1px solid #2e2200;
                padding:7px; font-weight:600; letter-spacing:2px;
            }
            QTableWidget::item { padding:6px; }
            QTableWidget::item:selected { background:#2a2000; color:#f5e88a; }
            QTableWidget::item:hover    { background:#181200; }

            QListWidget {
                background:#0a0800; border:1px solid #2a2200;
                border-radius:6px; color:#c8aa50;
            }
            QListWidget::item          { padding:5px 9px; border:none; }
            QListWidget::item:selected { background:#2a2000; color:#f5e88a; }
            QListWidget::item:hover    { background:#181000; }
            #sb_playlist_list, #lib_pl_list { font-size:11px; }

            QSlider::groove:horizontal { border:none; height:5px; background:#1e1800; border-radius:3px; }
            QSlider::sub-page:horizontal { background:#f5c400; border-radius:3px; }
            QSlider::handle:horizontal {
                background:#f5c400; border:2px solid #f5c400;
                width:14px; margin:-5px 0; border-radius:7px;
            }

            QScrollBar:vertical { background:#0a0800; width:7px; border-radius:4px; border:none; }
            QScrollBar::handle:vertical { background:#3a2e00; border-radius:4px; min-height:20px; }
            QScrollBar::handle:vertical:hover { background:#f5c400; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
            QScrollBar:horizontal { height:0; }

            #hr_line { border:none; border-top:1px solid #2a2200; max-height:1px; }

            #np_disc { color:#f5c400; font-size:80px; border:none; padding:8px; }
            #np_track_name {
                color:#f5e48a; font-size:24px; font-weight:700;
                letter-spacing:2px; border:none; padding:4px;
            }
            #np_artist_name { color:#a08040; font-size:12px; letter-spacing:4px; border:none; }

            #now_playing_page {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #1c1000, stop:0.5 #0a0800, stop:1 #050400);
                border:none; border-radius:0px;
            }

            QMenu {
                background:#0e0c00; border:1px solid #3a2e00;
                border-radius:8px; color:#f0e6c0;
            }
            QMenu::item { padding:8px 22px; }
            QMenu::item:selected { background:#2a2000; color:#f5c400; }
        """)

        glow = QGraphicsDropShadowEffect(self.play_btn)
        glow.setBlurRadius(36)
        glow.setOffset(0, 0)
        glow.setColor(QColor("#f5c400"))
        self.play_btn.setGraphicsEffect(glow)

    # ── Navigation ─────────────────────────────────────────────────────────
    def _nav(self, page: int) -> None:
        self.content_stack.setCurrentIndex(page)
        for p, b in ((PAGE_HOME, self.home_btn),
                     (PAGE_SEARCH, self.search_btn),
                     (PAGE_LIBRARY, self.library_btn)):
            b.setProperty("active", p == page)
            b.style().unpolish(b)
            b.style().polish(b)
        if page == PAGE_LIBRARY:
            self._refresh_lib_list()

    def _focus_search(self) -> None:
        self._nav(PAGE_SEARCH)
        self.search_input.setFocus()

    # ── Search (phase 1 – metadata only) ──────────────────────────────────
    def start_search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            self.status_text.setText("NO QUERY PROVIDED")
            return
        if self.worker_thread and self.worker_thread.isRunning():
            self.status_text.setText("ALREADY SCANNING…")
            return

        self.status_text.setText("SCANNING…")
        self.result_table.setRowCount(0)
        self._nav(PAGE_SEARCH)

        self.worker_thread = QThread(self)
        self.worker = SearchWorker(self.engine, query)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._on_search_done)
        self.worker.failed.connect(lambda m: self.status_text.setText(f"ERROR: {m[:100]}"))
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(lambda: setattr(self, "worker_thread", None))
        self.worker_thread.finished.connect(lambda: setattr(self, "worker", None))
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    def _on_search_done(self, tracks: List[Track]) -> None:
        self.results = tracks
        self.result_table.setRowCount(len(tracks))
        E = self.engine
        for r, t in enumerate(tracks):
            self.result_table.setItem(r, 0, QTableWidgetItem(t.title))
            self.result_table.setItem(r, 1, QTableWidgetItem(t.artist))
            self.result_table.setItem(r, 2, QTableWidgetItem(E._fmt(t.duration) if t.duration else "…"))
        self.status_text.setText(
            f"{len(tracks)} SIGNALS LOCKED  •  double-click to play" if tracks else "NO SIGNALS FOUND")

    # ── Stream URL resolution (phase 2 – lazy, background) ────────────────
    def _preload_row(self, row: int, _c: int) -> None:
        """Single-click: silently resolve URL so double-click plays instantly."""
        if row < 0 or row >= len(self.results): return
        if self.results[row].stream_url: return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState: return
        self._resolve(row, play_after=False)

    def _resolve(self, row: int, play_after: bool) -> None:
        if self.resolve_thread and self.resolve_thread.isRunning():
            self._pending_play = play_after
            return
        self.status_text.setText("RESOLVING STREAM…")
        self._pending_play = play_after

        self.resolve_thread = QThread(self)
        self.resolve_worker = StreamResolveWorker(self.engine, row, self.results[row].page_url)
        self.resolve_worker.moveToThread(self.resolve_thread)
        self.resolve_thread.started.connect(self.resolve_worker.run)
        self.resolve_worker.resolved.connect(self._on_resolved)
        self.resolve_worker.failed.connect(lambda _r: self.status_text.setText("SIGNAL LOSS"))
        self.resolve_worker.resolved.connect(self.resolve_thread.quit)
        self.resolve_worker.failed.connect(self.resolve_thread.quit)
        self.resolve_thread.finished.connect(self.resolve_worker.deleteLater)
        self.resolve_thread.finished.connect(self.resolve_thread.deleteLater)
        self.resolve_thread.finished.connect(lambda: setattr(self, "resolve_thread", None))
        self.resolve_thread.finished.connect(lambda: setattr(self, "resolve_worker", None))
        self.resolve_thread.start()

    def _on_resolved(self, row: int, url: str) -> None:
        if row < len(self.results):
            self.results[row].stream_url = url
        self.status_text.setText("STREAM READY")
        if self._pending_play and row == self.current_index:
            self._play(self.results[row])

    # ── Playback core ──────────────────────────────────────────────────────
    def _play_result_row(self, row: int, _c: int) -> None:
        if row < 0 or row >= len(self.results): return
        self.current_index = row
        self._playing_from = "search"
        t = self.results[row]
        if t.stream_url:
            self._play(t)
        else:
            self._resolve(row, play_after=True)

    def _play_lib_row(self, row: int, _c: int) -> None:
        if not self._active_playlist: return
        tracks = self.playlists.get(self._active_playlist, [])
        if row < 0 or row >= len(tracks): return
        self._playing_from = self._active_playlist
        self._pl_play_index = row
        t = tracks[row]
        if not t.stream_url:
            self.status_text.setText("RESOLVING…")
            url = self.engine.resolve_stream_url(t.page_url)
            if url:
                t.stream_url = url
                self.playlists[self._active_playlist][row].stream_url = url
                self._save_playlists()
            else:
                self.status_text.setText("SIGNAL LOSS")
                return
        self._play(t)

    def _play_recent_row(self, row: int, _c: int) -> None:
        if row < 0 or row >= len(self._recent): return
        t = self._recent[row]
        if t.stream_url:
            self._play(t)

    def _play(self, track: Track) -> None:
        self._current_track = track
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setSource(QUrl(track.stream_url))
        self.player.play()

        short = track.title[:60] + ("…" if len(track.title) > 60 else "")
        self.np_label.setText(f"▶  {short}  //  {track.artist}  ·  click to visualize")
        self.np_title.setText(track.title)
        self.np_artist.setText(track.artist.upper())
        self.status_text.setText("DECRYPTING…")
        self._nav(PAGE_NOWPLAY)
        self._glitch()
        self._push_recent(track)
        if self._viz and self._viz.isVisible():
            self._viz.update_track(track.title, track.artist)

    def resume_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            return
        if self.current_index < 0 and self.results:
            self.current_index = 0
            t = self.results[0]
            if t.stream_url:
                self._play(t)
            else:
                self._resolve(0, play_after=True)
            return
        self.player.play()

    def play_next(self) -> None:
        if self._loop == LOOP_ONE:
            self.player.setPosition(0)
            self.player.play()
            return

        # ── playlist context ──────────────────────────────────────────────
        if self._playing_from != "search" and self._playing_from in self.playlists:
            tracks = self.playlists[self._playing_from]
            if not tracks: return
            if self._shuffle:
                others = [i for i in range(len(tracks)) if i != self._pl_play_index]
                nxt = random.choice(others) if others else self._pl_play_index
            elif self._loop == LOOP_ALL:
                nxt = (self._pl_play_index + 1) % len(tracks)
            else:
                nxt = self._pl_play_index + 1
                if nxt >= len(tracks): return
            self._pl_play_index = nxt
            t = tracks[nxt]
            if not t.stream_url:
                url = self.engine.resolve_stream_url(t.page_url)
                if url:
                    t.stream_url = url
                    self._save_playlists()
                else:
                    self.status_text.setText("SIGNAL LOSS")
                    return
            self._play(t)
            return

        # ── search results context ────────────────────────────────────────
        if not self.results: return
        if self._shuffle:
            others = [i for i in range(len(self.results)) if i != self.current_index]
            nxt = random.choice(others) if others else self.current_index
        elif self._loop == LOOP_ALL:
            nxt = (self.current_index + 1) % len(self.results)
        else:
            nxt = self.current_index + 1
            if nxt >= len(self.results): return
        self.current_index = nxt
        t = self.results[nxt]
        if t.stream_url:
            self._play(t)
        else:
            self._resolve(nxt, play_after=True)

    def play_previous(self) -> None:
        # ── playlist context ──────────────────────────────────────────────
        if self._playing_from != "search" and self._playing_from in self.playlists:
            tracks = self.playlists[self._playing_from]
            if not tracks: return
            self._pl_play_index = (self._pl_play_index - 1) % len(tracks)
            t = tracks[self._pl_play_index]
            if not t.stream_url:
                url = self.engine.resolve_stream_url(t.page_url)
                if url:
                    t.stream_url = url
                    self._save_playlists()
                else:
                    self.status_text.setText("SIGNAL LOSS")
                    return
            self._play(t)
            return

        # ── search results context ────────────────────────────────────────
        if not self.results: return
        self.current_index = (self.current_index - 1) % len(self.results)
        t = self.results[self.current_index]
        if t.stream_url:
            self._play(t)
        else:
            self._resolve(self.current_index, play_after=True)

    # ── Playback modes ─────────────────────────────────────────────────────
    def _toggle_shuffle(self) -> None:
        self._shuffle = not self._shuffle
        self.shuffle_btn.setProperty("active", self._shuffle)
        self.shuffle_btn.style().unpolish(self.shuffle_btn)
        self.shuffle_btn.style().polish(self.shuffle_btn)

    def _toggle_loop(self) -> None:
        self._loop = (self._loop + 1) % 3
        labels = {LOOP_OFF: "", LOOP_ONE: "1", LOOP_ALL: "∞"}
        self.loop_btn.setText(labels[self._loop])
        self.loop_btn.setProperty("active", self._loop != LOOP_OFF)
        self.loop_btn.style().unpolish(self.loop_btn)
        self.loop_btn.style().polish(self.loop_btn)

    # ── Playlists ──────────────────────────────────────────────────────────
    def create_playlist(self) -> None:
        name, ok = QInputDialog.getText(self, "NEW PLAYLIST", "Playlist name:", text="My Playlist")
        if ok and name.strip():
            n = name.strip()
            if n not in self.playlists:
                self.playlists[n] = []
                self._save_playlists()
            self._refresh_sb_playlists()
            self._refresh_lib_list()
            self.status_text.setText(f"PLAYLIST CREATED: {n}")

    def _add_current_to_pl(self) -> None:
        if self._current_track is None: return
        self._add_to_pl(self._current_track)

    def _add_to_pl(self, track: Track) -> None:
        if not self.playlists:
            name, ok = QInputDialog.getText(self, "NEW PLAYLIST", "No playlists. Create one:")
            if ok and name.strip():
                self.playlists[name.strip()] = []
        if not self.playlists: return
        names = list(self.playlists.keys())
        name, ok = QInputDialog.getItem(self, "ADD TO PLAYLIST", "Choose playlist:", names, 0, False)
        if ok and name:
            self.playlists[name].append(track)
            self._save_playlists()
            self._refresh_lib_list()
            self._refresh_sb_playlists()
            # If the user is currently viewing this playlist, refresh its track table
            if self._active_playlist == name:
                self._show_pl_tracks(name)
            self.status_text.setText(f"ADDED TO: {name}  ({len(self.playlists[name])} tracks)")

    def _refresh_sb_playlists(self) -> None:
        self.sb_playlist_list.clear()
        for name in self.playlists:
            it = QListWidgetItem(f"♪  {name}")
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.sb_playlist_list.addItem(it)

    def _refresh_lib_list(self) -> None:
        self.lib_pl_list.clear()
        for name, tracks in self.playlists.items():
            it = QListWidgetItem(f"♪  {name}  ({len(tracks)} tracks)")
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.lib_pl_list.addItem(it)

    def _sb_pl_clicked(self, it: QListWidgetItem) -> None:
        self._show_pl_tracks(it.data(Qt.ItemDataRole.UserRole))
        self._nav(PAGE_LIBRARY)

    def _lib_pl_clicked(self, it: QListWidgetItem) -> None:
        self._show_pl_tracks(it.data(Qt.ItemDataRole.UserRole))

    def _show_pl_tracks(self, name: str) -> None:
        self._active_playlist = name
        tracks = self.playlists.get(name, [])
        self.lib_track_table.setRowCount(len(tracks))
        E = self.engine
        for r, t in enumerate(tracks):
            self.lib_track_table.setItem(r, 0, QTableWidgetItem(t.title))
            self.lib_track_table.setItem(r, 1, QTableWidgetItem(t.artist))
            self.lib_track_table.setItem(r, 2, QTableWidgetItem(E._fmt(t.duration)))

    # ── Context menus ──────────────────────────────────────────────────────
    def _results_ctx_menu(self, pos) -> None:
        row = self.result_table.rowAt(pos.y())
        if row < 0 or row >= len(self.results): return
        t = self.results[row]
        m = QMenu(self)
        m.addAction("▶  Play",              lambda: self._play_result_row(row, 0))
        m.addAction("+ Add to Playlist",    lambda: self._add_to_pl(t))
        m.exec(self.result_table.viewport().mapToGlobal(pos))

    def _lib_ctx_menu(self, pos) -> None:
        row = self.lib_track_table.rowAt(pos.y())
        if not self._active_playlist: return
        tracks = self.playlists.get(self._active_playlist, [])
        if row < 0 or row >= len(tracks): return
        m = QMenu(self)
        m.addAction("▶  Play",                  lambda: self._play_lib_row(row, 0))
        m.addAction("✕  Remove from playlist",  lambda: self._remove_from_pl(row))
        m.exec(self.lib_track_table.viewport().mapToGlobal(pos))

    def _remove_from_pl(self, row: int) -> None:
        if not self._active_playlist: return
        del self.playlists[self._active_playlist][row]
        self._save_playlists()
        self._show_pl_tracks(self._active_playlist)
        self._refresh_lib_list()
        self._refresh_sb_playlists()

    # ── Recent plays ───────────────────────────────────────────────────────
    def _push_recent(self, track: Track) -> None:
        self._recent = [t for t in self._recent if t.page_url != track.page_url]
        self._recent.insert(0, track)
        self._recent = self._recent[:20]
        E = self.engine
        rows = min(len(self._recent), 10)
        self.recent_table.setRowCount(rows)
        for r, t in enumerate(self._recent[:rows]):
            self.recent_table.setItem(r, 0, QTableWidgetItem(t.title))
            self.recent_table.setItem(r, 1, QTableWidgetItem(t.artist))
            self.recent_table.setItem(r, 2, QTableWidgetItem(E._fmt(t.duration)))

    # ── Visualizer popup ───────────────────────────────────────────────────
    def _open_viz(self) -> None:
        title  = self.np_title.text()  if self.np_title.text()  != "---" else ""
        artist = self.np_artist.text() if self.np_artist.text() != "---" else ""
        if self._viz is None or not self._viz.isVisible():
            self._viz = VisualizerDialog(title, artist, self)
        else:
            self._viz.update_track(title, artist)
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._viz.start()
        self._viz.show()
        self._viz.raise_()

    # ── Media callbacks ────────────────────────────────────────────────────
    def _end_seek(self) -> None:
        self.user_dragging_slider = False
        self.player.setPosition(self.seek_slider.value())

    def _on_pos(self, pos: int) -> None:
        if not self.user_dragging_slider:
            self.seek_slider.setValue(pos)
        self.cur_time.setText(self._ms(pos))

    def _on_dur(self, dur: int) -> None:
        self.seek_slider.setRange(0, dur)
        self.tot_time.setText(self._ms(dur))

    def _on_media_status(self, s: QMediaPlayer.MediaStatus) -> None:
        if s == QMediaPlayer.MediaStatus.BufferingMedia:
            self.status_text.setText("DECRYPTING…")
        elif s in (QMediaPlayer.MediaStatus.BufferedMedia, QMediaPlayer.MediaStatus.LoadedMedia):
            self.status_text.setText("STREAM STABLE")
        elif s == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_next()
        elif s == QMediaPlayer.MediaStatus.InvalidMedia:
            self.status_text.setText("SIGNAL LOSS — try another track")

    def _on_playback_state(self, s: QMediaPlayer.PlaybackState) -> None:
        playing = s == QMediaPlayer.PlaybackState.PlayingState
        self.eq_bars.start() if playing else self.eq_bars.stop()
        if self._viz and self._viz.isVisible():
            self._viz.start() if playing else self._viz.stop()

    @staticmethod
    def _ms(ms: int) -> str:
        s = max(ms // 1000, 0)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    # ── Glitch animation ───────────────────────────────────────────────────
    def _glitch(self) -> None:
        for f in (self.sidebar, self.player_bar):
            f.setProperty("glitch", True)
            f.style().unpolish(f)
            f.style().polish(f)
            f.update()
        def _clear():
            for f in (self.sidebar, self.player_bar):
                f.setProperty("glitch", False)
                f.style().unpolish(f)
                f.style().polish(f)
                f.update()
        QTimer.singleShot(100, _clear)

    # ── Window chrome ──────────────────────────────────────────────────────
    def _toggle_maximize(self) -> None:
        if self._is_maximized:
            self.showNormal()
            self.outer_layout.setContentsMargins(10, 10, 10, 10)
            self.max_btn.setText("□")
            self._is_maximized = False
        else:
            self.showMaximized()
            self.outer_layout.setContentsMargins(0, 0, 0, 0)
            self.max_btn.setText("❐")
            self._is_maximized = True

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton and self.top_chrome.geometry().contains(e.pos()):
            self.drag_position = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:  # type: ignore[override]
        if e.buttons() & Qt.MouseButton.LeftButton \
                and not self.drag_position.isNull() \
                and not self._is_maximized:
            self.move(e.globalPosition().toPoint() - self.drag_position)
            e.accept()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self.drag_position = QPoint()
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton \
                and self.top_chrome.geometry().contains(e.pos()):
            self._toggle_maximize()
        else:
            super().mouseDoubleClickEvent(e)


# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
