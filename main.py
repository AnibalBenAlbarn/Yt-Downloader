import datetime
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)
YTDLP_EXE = str(Path(__file__).parent / "yt-dlp.exe")
FFMPEG_EXE = str(Path(__file__).parent / "ffmpeg.exe")
CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_FORMAT_SPECS: Dict[str, Dict[str, str]] = {
    "mp4 (H.264 + AAC)": {"ext": "mp4", "vcodec": "libx264", "acodec": "aac", "pix_fmt": "yuv420p"},
    "mkv (H.264 + AAC)": {"ext": "mkv", "vcodec": "libx264", "acodec": "aac", "pix_fmt": "yuv420p"},
    "webm (VP9 + Opus)": {"ext": "webm", "vcodec": "libvpx-vp9", "acodec": "libopus", "pix_fmt": "yuv420p"},
    "avi (MPEG4 + MP3)": {"ext": "avi", "vcodec": "mpeg4", "acodec": "libmp3lame", "pix_fmt": "yuv420p"},
}
def now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
def safe_filename(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", (text or "").strip())
    text = re.sub(r"\s+", " ", text)
    return text[:180] or "video"
def split_urls(text: str) -> List[str]:
    return [x.strip() for x in re.split(r"[\s]+", text or "") if x.strip().lower().startswith("http")]
def parse_allowed_video_formats(raw: str) -> List[str]:
    values = [x.strip() for x in (raw or '').split(',') if x.strip()]
    valid = [x for x in values if x in VIDEO_FORMAT_SPECS]
    return valid or ['mp4 (H.264 + AAC)']
def run_ffmpeg_convert(input_path: Path, spec: Dict[str, str], logger: "DualLogger") -> Path:
    target = input_path.with_suffix(f".{spec['ext']}")
    temp_target = target.with_name(f"{target.stem}_convirtiendo{target.suffix}")
    if temp_target.exists():
        temp_target.unlink()
    cmd = [
        FFMPEG_EXE,
        '-y',
        '-i',
        str(input_path),
        '-map',
        '0:v:0',
        '-map',
        '0:a?',
        '-c:v',
        spec['vcodec'],
        '-pix_fmt',
        spec['pix_fmt'],
        '-c:a',
        spec['acodec'],
        '-movflags',
        '+faststart',
        str(temp_target),
    ]
    logger.write('FFMPEG CMD: ' + ' '.join(cmd))
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    if done.returncode != 0:
        raise RuntimeError(f'Falló conversión ffmpeg ({done.returncode})')
    logger.write(done.stdout.strip())
    if target.exists():
        target.unlink()
    temp_target.replace(target)
    if input_path != target and input_path.exists():
        input_path.unlink()
    return target
def run_hyperspin_compat(input_path: Path, logger: "DualLogger") -> Path:
    spec = {'ext': 'mp4', 'vcodec': 'libx264', 'acodec': 'aac', 'pix_fmt': 'yuv420p'}
    logger.write('Aplicando compatibilidad HyperSpin (MP4 H.264 + AAC)...')
    return run_ffmpeg_convert(input_path, spec, logger)
class DualLogger:
    def __init__(self, file_path: Path, ui_log: Optional[QTextEdit] = None):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.ui_log = ui_log
    def write(self, msg: str):
        print(msg, flush=True)
        with self.file_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(msg + "\n")
        if self.ui_log is not None:
            self.ui_log.append(msg)
@dataclass
class AppConfig:
    video_dir: str = str(Path.home() / "Downloads")
    audio_dir: str = str(Path.home() / "Music")
    simultaneous_downloads: int = 1
    default_video_quality: str = "720p"
    default_video_format: str = "mp4 (H.264 + AAC)"
    allowed_video_formats: str = "mp4 (H.264 + AAC), mkv (H.264 + AAC), webm (VP9 + Opus)"
    default_audio_quality: str = "128K"
    hyperspin_enabled: bool = False
@dataclass
class DownloadItem:
    url: str
    title: str = ""
    mode: str = "video"  # video/audio
    quality: str = "720p"
    video_format: str = "mp4 (H.264 + AAC)"
    output_name: str = ""
    format_selector: str = "bestvideo+bestaudio/best"
    status: str = "Pendiente"
    progress: int = 0
class MetadataWorker(QThread):
    done = pyqtSignal(dict)
    error = pyqtSignal(str)
    def __init__(self, url: str, query: bool = False):
        super().__init__()
        self.url = url
        self.query = query
    def run(self):
        try:
            target = self.url if not self.query else f"ytsearch10:{self.url}"
            cmd = [YTDLP_EXE, "--no-config", "--skip-download", "-J", target]
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if p.returncode != 0:
                raise RuntimeError(p.stderr.strip() or "Error consultando metadatos")
            self.done.emit(json.loads(p.stdout))
        except Exception as e:
            self.error.emit(str(e))
class DownloadWorker(QThread):
    progress = pyqtSignal(str, int)  # url, pct
    status = pyqtSignal(str, str)  # url, status
    finished_item = pyqtSignal(str)
    error = pyqtSignal(str, str)
    def __init__(self, item: DownloadItem, cfg: AppConfig, ui_log: Optional[QTextEdit] = None):
        super().__init__()
        self.item = item
        self.cfg = cfg
        self._cancel = False
        self.ui_log = ui_log
    def cancel(self):
        self._cancel = True
    def _selector(self) -> str:
        if self.item.mode == "audio":
            return "bestaudio/best"
        q = self.item.quality.replace("p", "")
        if q.isdigit():
            return f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
        return "bestvideo+bestaudio/best"
    def run(self):
        log = DualLogger(LOG_DIR / f"{now_stamp()}_{safe_filename(self.item.title)}.log", self.ui_log)
        out_dir = self.cfg.video_dir if self.item.mode == "video" else self.cfg.audio_dir
        out_name = safe_filename(self.item.output_name or self.item.title or "video")
        outtmpl = str(Path(out_dir) / f"{out_name}.%(ext)s")
        selector = self._selector()
        cmd = [
            YTDLP_EXE,
            "--no-config",
            "--newline",
            "--progress",
            "--progress-template",
            "download:%(progress._percent_str)s",
            "--print",
            "after_move:FILEPATH:%(filepath)s",
            "--ffmpeg-location",
            FFMPEG_EXE,
            "-f",
            selector,
            "-o",
            outtmpl,
            self.item.url,
        ]
        if self.item.mode == "audio":
            cmd.extend(["-x", "--audio-format", "mp3", "--audio-quality", self.cfg.default_audio_quality])
        self.status.emit(self.item.url, "Descargando")
        log.write("CMD: " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        downloaded_path: Optional[Path] = None
        try:
            while True:
                if self._cancel:
                    proc.terminate()
                    self.status.emit(self.item.url, "Cancelado")
                    return
                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    break
                line = line.rstrip("\n")
                log.write(line)
                if line.startswith("download:"):
                    raw = line.split(":", 1)[1].replace("%", "").strip()
                    try:
                        self.progress.emit(self.item.url, int(float(raw)))
                    except Exception:
                        pass
                if line.startswith("FILEPATH:"):
                    downloaded_path = Path(line.split(":", 1)[1].strip())
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"yt-dlp devolvió código {rc}")
            if self.item.mode == "video" and downloaded_path and downloaded_path.exists():
                fmt_name = self.item.video_format if self.item.video_format in VIDEO_FORMAT_SPECS else self.cfg.default_video_format
                spec = VIDEO_FORMAT_SPECS.get(fmt_name, VIDEO_FORMAT_SPECS["mp4 (H.264 + AAC)"])
                self.status.emit(self.item.url, f"Convirtiendo a {fmt_name}")
                downloaded_path = run_ffmpeg_convert(downloaded_path, spec, log)
                if self.cfg.hyperspin_enabled:
                    self.status.emit(self.item.url, "Aplicando compatibilidad HyperSpin")
                    downloaded_path = run_hyperspin_compat(downloaded_path, log)
            self.progress.emit(self.item.url, 100)
            self.status.emit(self.item.url, "OK")
            self.finished_item.emit(self.item.url)
        except Exception as e:
            self.status.emit(self.item.url, "Error")
            self.error.emit(self.item.url, str(e))
        finally:
            if proc.poll() is None:
                proc.terminate()
class QuickDownloadDialog(QDialog):
    def __init__(self, title: str, url: str, defaults: AppConfig):
        super().__init__()
        self.setWindowTitle("Descarga rápida")
        self.resize(500, 260)
        self.result_item: Optional[DownloadItem] = None
        self.defaults = defaults
        layout = QFormLayout(self)
        self.lbl_title = QLabel(title)
        self.lbl_title.setWordWrap(True)
        layout.addRow("Título", self.lbl_title)
        self.url_edit = QLineEdit(url)
        layout.addRow("URL", self.url_edit)
        self.mode = QComboBox()
        self.mode.addItems(["video", "audio"])
        layout.addRow("Salida", self.mode)
        self.quality = QComboBox()
        self.quality.addItems(["1080p", "720p", "480p", "360p", "240p"])
        self.quality.setCurrentText(defaults.default_video_quality)
        layout.addRow("Calidad vídeo", self.quality)
        self.name = QLineEdit(safe_filename(title))
        layout.addRow("Nombre salida", self.name)
        row = QHBoxLayout()
        btn_ok = QPushButton("Descargar ahora")
        btn_cancel = QPushButton("Cancelar")
        row.addWidget(btn_ok)
        row.addWidget(btn_cancel)
        layout.addRow(row)
        btn_ok.clicked.connect(self.accept_payload)
        btn_cancel.clicked.connect(self.reject)
    def accept_payload(self):
        self.result_item = DownloadItem(
            url=self.url_edit.text().strip(),
            title=self.lbl_title.text().strip(),
            mode=self.mode.currentText(),
            quality=self.quality.currentText(),
            video_format=self.defaults.default_video_format,
            output_name=self.name.text().strip(),
        )
        self.accept()
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YT Downloader - Gestor visual")
        self.resize(1300, 780)
        self.cfg = AppConfig()
        self.basket: List[DownloadItem] = []
        self.downloads: List[DownloadItem] = []
        self.active_workers: Dict[str, DownloadWorker] = {}
        self.metadata_workers: List[MetadataWorker] = []
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tab_config = QWidget()
        self.tab_downloads = QWidget()
        self.tab_search = QWidget()
        self.tab_manager = QWidget()
        self.tabs.addTab(self.tab_config, "Configuración")
        self.tabs.addTab(self.tab_downloads, "Tabla de descargas")
        self.tabs.addTab(self.tab_search, "Búsqueda")
        self.tabs.addTab(self.tab_manager, "Gestor de descargas")
        self._build_config_tab()
        self._build_downloads_tab()
        self._build_search_tab()
        self._build_manager_tab()
        self.load_config()
        self.refresh_all_tables()
        self.log_ui("Aplicación iniciada")
    def _build_config_tab(self):
        layout = QVBoxLayout(self.tab_config)
        box = QGroupBox("Preferencias")
        form = QFormLayout(box)
        self.video_dir = QLineEdit()
        self.audio_dir = QLineEdit()
        btn_video = QPushButton("...")
        btn_audio = QPushButton("...")
        rv = QHBoxLayout(); rv.addWidget(self.video_dir); rv.addWidget(btn_video)
        ra = QHBoxLayout(); ra.addWidget(self.audio_dir); ra.addWidget(btn_audio)
        self.simultaneous = QSpinBox(); self.simultaneous.setRange(1, 8)
        self.default_video_q = QComboBox(); self.default_video_q.addItems(["1080p", "720p", "480p", "360p", "240p"])
        self.default_video_format = QComboBox(); self.default_video_format.addItems(list(VIDEO_FORMAT_SPECS.keys()))
        self.allowed_video_formats = QLineEdit()
        self.allowed_video_formats.setPlaceholderText(", ".join(VIDEO_FORMAT_SPECS.keys()))
        self.default_audio_q = QComboBox(); self.default_audio_q.addItems(["320K", "256K", "192K", "128K", "96K"])
        self.hyperspin_enabled = QCheckBox("Convertir cada vídeo descargado a perfil HyperSpin (MP4 H.264 + AAC)")
        form.addRow("Carpeta vídeo", rv)
        form.addRow("Carpeta audio", ra)
        form.addRow("Descargas simultáneas", self.simultaneous)
        form.addRow("Calidad vídeo por defecto", self.default_video_q)
        form.addRow("Formato vídeo por defecto", self.default_video_format)
        form.addRow("Formatos de vídeo permitidos", self.allowed_video_formats)
        form.addRow("Calidad audio por defecto", self.default_audio_q)
        form.addRow("Compatibilidad HyperSpin", self.hyperspin_enabled)
        btn_save = QPushButton("Guardar en config.json")
        btn_save.clicked.connect(self.save_config)
        btn_video.clicked.connect(lambda: self.pick_dir(self.video_dir))
        btn_audio.clicked.connect(lambda: self.pick_dir(self.audio_dir))
        layout.addWidget(box)
        layout.addWidget(btn_save)
        layout.addStretch(1)
    def _build_downloads_tab(self):
        layout = QVBoxLayout(self.tab_downloads)
        self.downloads_table = QTableWidget(0, 8)
        self.downloads_table.setHorizontalHeaderLabels(["URL", "Título", "Modo", "Calidad", "Formato", "Salida", "Estado", "%"])
        self.downloads_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.current_file_progress = QProgressBar(); self.current_file_progress.setFormat("Archivo actual: %p%")
        self.total_progress = QProgressBar(); self.total_progress.setFormat("Total cola: %p%")
        row = QHBoxLayout()
        btn_start = QPushButton("Iniciar descargas")
        btn_cancel = QPushButton("Cancelar activas")
        btn_start.clicked.connect(self.start_downloads)
        btn_cancel.clicked.connect(self.cancel_downloads)
        row.addWidget(btn_start); row.addWidget(btn_cancel)
        self.logs_box = QTextEdit(); self.logs_box.setReadOnly(True)
        layout.addLayout(row)
        layout.addWidget(self.downloads_table)
        layout.addWidget(self.current_file_progress)
        layout.addWidget(self.total_progress)
        layout.addWidget(QLabel("Logs en vivo"))
        layout.addWidget(self.logs_box)
    def _build_search_tab(self):
        layout = QVBoxLayout(self.tab_search)
        row = QHBoxLayout()
        self.search_text = QLineEdit(); self.search_text.setPlaceholderText("Busca vídeos (ytsearch)")
        btn = QPushButton("Buscar")
        btn.clicked.connect(self.search_videos)
        row.addWidget(self.search_text); row.addWidget(btn)
        self.search_table = QTableWidget(0, 6)
        self.search_table.setHorizontalHeaderLabels(["✔", "Título", "Canal", "Duración", "URL", "Acciones"])
        self.search_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.search_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.search_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        actions = QHBoxLayout()
        add_basket = QPushButton("Añadir seleccionados a cesta")
        add_basket.clicked.connect(self.add_checked_search_to_basket)
        actions.addWidget(add_basket)
        layout.addLayout(row)
        layout.addWidget(self.search_table)
        layout.addLayout(actions)
    def _build_manager_tab(self):
        layout = QVBoxLayout(self.tab_manager)
        row = QHBoxLayout()
        self.manager_url = QLineEdit(); self.manager_url.setPlaceholderText("Pega URL")
        btn_add = QPushButton("Añadir a cesta")
        btn_quick = QPushButton("Descarga rápida")
        btn_add.clicked.connect(self.add_url_to_basket)
        btn_quick.clicked.connect(self.quick_download)
        row.addWidget(self.manager_url); row.addWidget(btn_add); row.addWidget(btn_quick)
        self.basket_table = QTableWidget(0, 7)
        self.basket_table.setHorizontalHeaderLabels(["URL", "Título", "Modo", "Calidad", "Formato", "Salida", "Estado"])
        self.basket_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        actions = QHBoxLayout()
        btn_send = QPushButton("Añadir a descargas")
        btn_remove = QPushButton("Eliminar de cesta")
        btn_send.clicked.connect(self.move_basket_to_downloads)
        btn_remove.clicked.connect(self.remove_basket_selected)
        actions.addWidget(btn_send); actions.addWidget(btn_remove)
        layout.addLayout(row)
        layout.addWidget(self.basket_table)
        layout.addLayout(actions)
    def log_ui(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.logs_box.append(f"[{ts}] {msg}")
    def pick_dir(self, widget: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Selecciona carpeta", widget.text() or str(Path.home()))
        if d:
            widget.setText(d)
    def load_config(self):
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.cfg = AppConfig(**data.get("settings", {}))
            self.basket = [DownloadItem(**x) for x in data.get("basket", [])]
            self.downloads = [DownloadItem(**x) for x in data.get("downloads", [])]
        self.video_dir.setText(self.cfg.video_dir)
        self.audio_dir.setText(self.cfg.audio_dir)
        self.simultaneous.setValue(self.cfg.simultaneous_downloads)
        self.default_video_q.setCurrentText(self.cfg.default_video_quality)
        self.default_video_format.setCurrentText(self.cfg.default_video_format)
        self.allowed_video_formats.setText(self.cfg.allowed_video_formats)
        self.default_audio_q.setCurrentText(self.cfg.default_audio_quality)
        self.hyperspin_enabled.setChecked(self.cfg.hyperspin_enabled)
    def save_config(self):
        self.cfg.video_dir = self.video_dir.text().strip()
        self.cfg.audio_dir = self.audio_dir.text().strip()
        self.cfg.simultaneous_downloads = self.simultaneous.value()
        self.cfg.default_video_quality = self.default_video_q.currentText()
        self.cfg.default_video_format = self.default_video_format.currentText()
        self.cfg.allowed_video_formats = self.allowed_video_formats.text().strip()
        self.cfg.default_audio_quality = self.default_audio_q.currentText()
        self.cfg.hyperspin_enabled = self.hyperspin_enabled.isChecked()
        payload = {
            "settings": asdict(self.cfg),
            "basket": [asdict(x) for x in self.basket],
            "downloads": [asdict(x) for x in self.downloads],
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log_ui("Configuración guardada en config.json")
    def refresh_all_tables(self):
        self._fill_table(self.basket_table, self.basket, include_progress=False)
        self._fill_table(self.downloads_table, self.downloads, include_progress=True)
        self._refresh_total_progress()
    def _fill_table(self, table: QTableWidget, items: List[DownloadItem], include_progress: bool):
        table.setRowCount(0)
        for it in items:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(it.url))
            table.setItem(row, 1, QTableWidgetItem(it.title))
            mode = QComboBox(); mode.addItems(["video", "audio"]); mode.setCurrentText(it.mode)
            quality = QComboBox(); quality.addItems(["1080p", "720p", "480p", "360p", "240p"]); quality.setCurrentText(it.quality)
            fmt = QComboBox(); fmt.addItems(parse_allowed_video_formats(self.cfg.allowed_video_formats)); fmt.setCurrentText(it.video_format)
            if fmt.currentIndex() < 0:
                fmt.setCurrentIndex(0)
            fmt.setEnabled(it.mode == "video")
            mode.currentTextChanged.connect(lambda text, f=fmt: f.setEnabled(text == "video"))
            table.setCellWidget(row, 2, mode)
            table.setCellWidget(row, 3, quality)
            table.setCellWidget(row, 4, fmt)
            table.setItem(row, 5, QTableWidgetItem(it.output_name))
            table.setItem(row, 6, QTableWidgetItem(it.status))
            if include_progress:
                table.setItem(row, 7, QTableWidgetItem(str(it.progress)))
    def _collect_table_edits(self):
        def update_from_table(table: QTableWidget, target: List[DownloadItem], has_progress: bool):
            for i, item in enumerate(target):
                item.mode = table.cellWidget(i, 2).currentText()  # type: ignore
                item.quality = table.cellWidget(i, 3).currentText()  # type: ignore
                item.video_format = table.cellWidget(i, 4).currentText()  # type: ignore
                item.output_name = (table.item(i, 5).text() if table.item(i, 5) else item.output_name).strip()
                if has_progress and table.item(i, 7):
                    try:
                        item.progress = int(table.item(i, 7).text())
                    except Exception:
                        pass
        update_from_table(self.basket_table, self.basket, False)
        update_from_table(self.downloads_table, self.downloads, True)
    def search_videos(self):
        q = self.search_text.text().strip()
        if not q:
            return
        w = MetadataWorker(q, query=True)
        w.done.connect(self.on_search_done)
        w.error.connect(lambda e: QMessageBox.warning(self, "Error búsqueda", e))
        w.start()
        self.metadata_workers.append(w)
        self.log_ui(f"Buscando: {q}")
    def on_search_done(self, data: Dict[str, Any]):
        entries = data.get("entries") or []
        self.search_table.setRowCount(0)
        for e in entries:
            row = self.search_table.rowCount()
            self.search_table.insertRow(row)
            checkbox = QCheckBox()
            checkbox.setStyleSheet("margin-left:8px; margin-right:8px;")
            self.search_table.setCellWidget(row, 0, checkbox)
            title = e.get("title", "")
            url = e.get("webpage_url", "")
            self.search_table.setItem(row, 1, QTableWidgetItem(title))
            self.search_table.setItem(row, 2, QTableWidgetItem(e.get("channel", "")))
            self.search_table.setItem(row, 3, QTableWidgetItem(str(e.get("duration_string", ""))))
            self.search_table.setItem(row, 4, QTableWidgetItem(url))
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(6)
            btn_add = QPushButton("Añadir a cesta de descargas")
            btn_now = QPushButton("Descargar ahora")
            btn_add.clicked.connect(lambda _, r=row: self.add_search_row_to_basket(r))
            btn_now.clicked.connect(lambda _, r=row: self.download_search_row_now(r))
            action_layout.addWidget(btn_add)
            action_layout.addWidget(btn_now)
            self.search_table.setCellWidget(row, 5, action_widget)
    def add_search_row_to_basket(self, row: int, refresh: bool = True):
        if row < 0 or row >= self.search_table.rowCount():
            return
        url_item = self.search_table.item(row, 4)
        title_item = self.search_table.item(row, 1)
        if not url_item or not title_item:
            return
        url = url_item.text().strip()
        title = title_item.text().strip()
        if not url:
            return
        self.basket.append(DownloadItem(url=url, title=title, quality=self.cfg.default_video_quality, video_format=self.cfg.default_video_format, output_name=safe_filename(title)))
        if refresh:
            self.refresh_all_tables()
            self.save_config()
    def add_checked_search_to_basket(self):
        added = 0
        for row in range(self.search_table.rowCount()):
            checkbox = self.search_table.cellWidget(row, 0)
            if isinstance(checkbox, QCheckBox) and checkbox.isChecked():
                self.add_search_row_to_basket(row, refresh=False)
                checkbox.setChecked(False)
                added += 1
        if added == 0:
            QMessageBox.information(self, "Búsqueda", "No hay elementos marcados")
            return
        self.refresh_all_tables()
        self.save_config()
    def download_search_row_now(self, row: int):
        if row < 0 or row >= self.search_table.rowCount():
            return
        url_item = self.search_table.item(row, 4)
        title_item = self.search_table.item(row, 1)
        if not url_item or not title_item:
            return
        url = url_item.text().strip()
        title = title_item.text().strip()
        if not url:
            return
        item = DownloadItem(
            url=url,
            title=title,
            quality=self.cfg.default_video_quality,
            video_format=self.cfg.default_video_format,
            output_name=safe_filename(title),
            status="En cola",
        )
        self.downloads.append(item)
        self.refresh_all_tables()
        self.save_config()
        self.tabs.setCurrentWidget(self.tab_downloads)
    def add_url_to_basket(self):
        urls = split_urls(self.manager_url.text().strip())
        if not urls:
            QMessageBox.information(self, "URL", "Introduce una URL válida")
            return
        for url in urls:
            self.basket.append(DownloadItem(url=url, title=url, quality=self.cfg.default_video_quality, video_format=self.cfg.default_video_format, output_name=""))
        self.manager_url.clear()
        self.refresh_all_tables()
        self.save_config()
    def quick_download(self):
        url = self.manager_url.text().strip()
        if not url:
            QMessageBox.information(self, "URL", "Introduce una URL")
            return
        def open_dialog(data: Dict[str, Any]):
            title = data.get("title", url)
            dlg = QuickDownloadDialog(title=title, url=url, defaults=self.cfg)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
                item = dlg.result_item
                item.status = "En cola"
                self.downloads.append(item)
                self.refresh_all_tables()
                self.save_config()
                self.tabs.setCurrentWidget(self.tab_downloads)
        w = MetadataWorker(url)
        w.done.connect(open_dialog)
        w.error.connect(lambda e: QMessageBox.warning(self, "Error", e))
        w.start()
        self.metadata_workers.append(w)
    def move_basket_to_downloads(self):
        self._collect_table_edits()
        for it in self.basket:
            it.status = "En cola"
            it.progress = 0
            self.downloads.append(it)
        self.basket = []
        self.refresh_all_tables()
        self.save_config()
        self.tabs.setCurrentWidget(self.tab_downloads)
    def remove_basket_selected(self):
        rows = sorted({i.row() for i in self.basket_table.selectionModel().selectedRows()}, reverse=True)
        for r in rows:
            self.basket.pop(r)
        self.refresh_all_tables()
        self.save_config()
    def start_downloads(self):
        self._collect_table_edits()
        pending = [d for d in self.downloads if d.status in ("En cola", "Pendiente", "Error")]
        if not pending:
            self.log_ui("No hay descargas pendientes")
            return
        self.save_config()
        self._schedule_downloads()
    def _schedule_downloads(self):
        cap = self.cfg.simultaneous_downloads
        while len(self.active_workers) < cap:
            nxt = next((x for x in self.downloads if x.status in ("En cola", "Pendiente", "Error")), None)
            if not nxt:
                break
            nxt.status = "Iniciando"
            w = DownloadWorker(nxt, self.cfg, self.logs_box)
            w.progress.connect(self.on_item_progress)
            w.status.connect(self.on_item_status)
            w.finished_item.connect(self.on_item_finished)
            w.error.connect(self.on_item_error)
            self.active_workers[nxt.url] = w
            w.start()
    def on_item_progress(self, url: str, pct: int):
        for d in self.downloads:
            if d.url == url:
                d.progress = pct
                self.current_file_progress.setValue(pct)
                break
        self.refresh_all_tables()
    def on_item_status(self, url: str, status: str):
        for d in self.downloads:
            if d.url == url:
                d.status = status
                break
        self.log_ui(f"{url} -> {status}")
        self.refresh_all_tables()
    def on_item_finished(self, url: str):
        self.active_workers.pop(url, None)
        self._refresh_total_progress()
        self.save_config()
        self._schedule_downloads()
    def on_item_error(self, url: str, err: str):
        self.active_workers.pop(url, None)
        self.log_ui(f"Error en {url}: {err}")
        self._refresh_total_progress()
        self.save_config()
        self._schedule_downloads()
    def _refresh_total_progress(self):
        if not self.downloads:
            self.total_progress.setValue(0)
            return
        ratio = int(sum(d.progress for d in self.downloads) / len(self.downloads))
        self.total_progress.setValue(ratio)
    def cancel_downloads(self):
        for w in list(self.active_workers.values()):
            w.cancel()
        self.active_workers.clear()
        self.log_ui("Cancelación solicitada")
    def closeEvent(self, event):
        self.save_config()
        super().closeEvent(event)
def main():
    if not Path(YTDLP_EXE).exists():
        print(f"No existe {YTDLP_EXE}")
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
if __name__ == "__main__":
    main()
