# descargador_yt_qt6_ytdlp_gui.py
# PyQt6 + yt-dlp.exe (ruta fija) + ffmpeg.exe (ruta fija)
# - Cola de URLs (multi-URL pegadas)
# - Al añadir: consulta formatos automáticamente (yt-dlp -J)
# - Combo por selector REAL: format_id o "video_id+audio_id"
# - Default siempre 360p (muxed si existe; si no merge 360+best audio)
# - Renombrado por fila (nombre base)
# - Config JSON: carpeta + cola
# - Descarga secuencial
# - Progreso robusto con --progress-template (NO regex frágil)
# - Log a consola + TXT por INFO y por DESCARGA
# - --no-config para evitar que un yt-dlp.conf te cambie el comportamiento
#
# Requisitos:
#   pip install PyQt6
#   (yt-dlp.exe y ffmpeg.exe en las rutas indicadas abajo)
#
# Ejecuta:
#   python descargador_yt_qt6_ytdlp_gui.py

import datetime
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QComboBox,
    QAbstractItemView,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

# ----------------------------
# RUTAS (AJUSTADAS A TU CASO)
# ----------------------------
YTDLP_EXE = r"C:\Users\Anibal\PycharmProjects\descargadoryt\yt-dlp.exe"
FFMPEG_EXE = r"C:\Users\Anibal\PycharmProjects\descargadoryt\ffmpeg.exe"

YTDLP_CMD = [YTDLP_EXE]

# ----------------------------
# CONFIG & LOGS
# ----------------------------
CONFIG_PATH = Path.home() / ".descargador_yt_qt6_ytdlp_config.json"
LOG_DIR = Path(r"C:\Users\Anibal\PycharmProjects\descargadoryt\logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ----------------------------
# UTILIDADES
# ----------------------------
def now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def safe_filename(name: str) -> str:
    name = (name or "").strip()
    # caracteres inválidos en Windows
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:180] if len(name) > 180 else name


def sanitize_for_path(s: str) -> str:
    s = safe_filename(s)
    return s[:80] if len(s) > 80 else s


def split_urls(text: str) -> List[str]:
    candidates = re.split(r"[\s]+", (text or "").strip())
    urls: List[str] = []
    for c in candidates:
        c = c.strip()
        if not c:
            continue
        if c.lower().startswith("http"):
            urls.append(c)
    return urls


def fmt_size(fs: Any) -> str:
    try:
        v = float(fs)
        if v <= 0:
            return ""
        mb = round(v / (1024 * 1024), 1)
        return f"~{mb}MB"
    except Exception:
        return ""


class DualLogger:
    """Log a consola + fichero txt."""

    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def write(self, msg: str):
        print(msg, flush=True)
        with self.log_file.open("a", encoding="utf-8", errors="replace") as f:
            f.write(msg + "\n")

    def header(self, title: str):
        self.write("=" * 100)
        self.write(title)
        self.write("=" * 100)


# ----------------------------
# MODELO
# ----------------------------
@dataclass
class QueueItem:
    url: str
    title: str = ""
    out_name: str = ""                      # nombre base (sin extensión)
    format_selector: Optional[str] = None   # "18" o "137+140"
    status: str = "Pendiente"
    progress: int = 0


# ----------------------------
# WORKER: INFO (yt-dlp -J)
# ----------------------------
class InfoWorker(QThread):
    info_ready = pyqtSignal(int, str, list, str)   # row, title, options, default_selector
    status_changed = pyqtSignal(int, str)
    error_signal = pyqtSignal(int, str)

    def __init__(self, row: int, url: str):
        super().__init__()
        self.row = row
        self.url = url

    def run(self):
        log_file = LOG_DIR / f"{now_stamp()}_INFO_row{self.row+1}_{sanitize_for_path(self.url)}.txt"
        log = DualLogger(log_file)

        try:
            self.status_changed.emit(self.row, "Consultando...")
            log.header("INFO: INICIO")
            log.write(f"Row: {self.row+1}")
            log.write(f"URL: {self.url}")
            log.write(f"YTDLP_EXE exists: {Path(YTDLP_EXE).exists()} -> {YTDLP_EXE}")

            cmd = YTDLP_CMD + [
                "--no-config",
                "--no-warnings",
                "--skip-download",
                "-J",
                self.url
            ]

            log.write("Command:")
            log.write("  " + " ".join([f'"{c}"' if " " in c else c for c in cmd]))

            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            log.write(f"Return code: {p.returncode}")

            if p.stdout:
                log.write("--- STDOUT (begin) ---")
                log.write(p.stdout.strip())
                log.write("--- STDOUT (end) ---")

            if p.stderr:
                log.write("--- STDERR (begin) ---")
                log.write(p.stderr.strip())
                log.write("--- STDERR (end) ---")

            if p.returncode != 0:
                raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "yt-dlp falló al consultar formatos (-J)")

            data = json.loads(p.stdout)
            title = data.get("title") or ""
            formats = data.get("formats") or []

            video_formats: List[Dict[str, Any]] = []
            audio_only_formats: List[Dict[str, Any]] = []

            for f in formats:
                fid = f.get("format_id")
                if not fid:
                    continue

                vcodec = f.get("vcodec")
                acodec = f.get("acodec")
                ext = (f.get("ext") or "").lower()
                height = f.get("height")
                fps = f.get("fps")
                tbr = f.get("tbr")
                fs = f.get("filesize") or f.get("filesize_approx")

                is_video = bool(vcodec and vcodec != "none")
                is_audio = bool(acodec and acodec != "none")

                entry = {
                    "format_id": str(fid),
                    "ext": ext,
                    "height": height,
                    "fps": fps,
                    "tbr": tbr,
                    "filesize": fs,
                    "vcodec": vcodec or "",
                    "acodec": acodec or "",
                    "is_video": is_video,
                    "is_audio": is_audio,
                }

                if is_video:
                    video_formats.append(entry)
                if is_audio and not is_video:
                    audio_only_formats.append(entry)

            if not video_formats:
                self.status_changed.emit(self.row, "Error")
                self.error_signal.emit(self.row, f"No se encontraron formatos de vídeo.\nLog: {log_file}")
                return

            # Best audio: preferimos m4a, si no mayor bitrate
            def audio_score(a: Dict[str, Any]) -> Tuple[int, float]:
                ext_ = (a.get("ext") or "").lower()
                tbr_ = float(a.get("tbr") or 0.0)
                return (1 if ext_ == "m4a" else 0, tbr_)

            best_audio = max(audio_only_formats, key=audio_score) if audio_only_formats else None

            # Orden para vídeo: height desc, fps desc, tbr desc, prefer mp4
            def v_sort_key(v: Dict[str, Any]):
                h = int(v.get("height") or 0)
                fps_ = int(float(v.get("fps") or 0)) if v.get("fps") else 0
                tbr_ = float(v.get("tbr") or 0.0)
                ext_ = (v.get("ext") or "").lower()
                mp4_bonus = 1 if ext_ == "mp4" else 0
                return (h, fps_, tbr_, mp4_bonus)

            video_sorted = sorted(video_formats, key=v_sort_key, reverse=True)

            options: List[Dict[str, Any]] = []

            def label_for(fmt: Dict[str, Any], kind: str, selector: str) -> str:
                h = fmt.get("height")
                fps_ = fmt.get("fps")
                ext_ = (fmt.get("ext") or "").lower()
                vcodec_ = fmt.get("vcodec") or ""
                acodec_ = fmt.get("acodec") or ""
                tbr_ = fmt.get("tbr")
                fs_ = fmt.get("filesize")

                parts = [f"{kind} | {selector}"]
                if h:
                    parts.append(f"{h}p")
                if fps_:
                    try:
                        parts.append(f"{int(float(fps_))}fps")
                    except Exception:
                        parts.append(f"{fps_}fps")
                if ext_:
                    parts.append(ext_)
                if vcodec_:
                    parts.append(f"v:{vcodec_}")
                if kind == "muxed" and acodec_:
                    parts.append(f"a:{acodec_}")
                if tbr_:
                    try:
                        parts.append(f"{round(float(tbr_), 1)}kbps")
                    except Exception:
                        pass
                s = fmt_size(fs_)
                if s:
                    parts.append(s)

                return "  ".join(parts)

            # Construimos opciones:
            # - muxed (video+audio) => selector = format_id
            # - video-only => selector = vid+best_audio (si hay), si no vid
            for v in video_sorted:
                fid = v["format_id"]
                acodec = (v.get("acodec") or "").lower()
                is_muxed = acodec not in ("", "none")

                if is_muxed:
                    selector = fid
                    options.append({
                        "selector": selector,
                        "label": label_for(v, "muxed", selector),
                        "height": v.get("height"),
                        "kind": "muxed",
                    })
                else:
                    if best_audio:
                        selector = f'{fid}+{best_audio["format_id"]}'
                        options.append({
                            "selector": selector,
                            "label": label_for(v, "video-only+audio", selector),
                            "height": v.get("height"),
                            "kind": "merge",
                        })
                    else:
                        selector = fid
                        options.append({
                            "selector": selector,
                            "label": label_for(v, "video-only", selector),
                            "height": v.get("height"),
                            "kind": "video-only",
                        })

            default_selector = self._pick_default_360(options)

            log.header("INFO: OK")
            log.write(f"Title: {title}")
            log.write(f"Options: {len(options)}")
            log.write(f"Default selector: {default_selector}")

            self.info_ready.emit(self.row, title, options, default_selector)
            self.status_changed.emit(self.row, "Listo")

        except Exception as e:
            self.status_changed.emit(self.row, "Error")
            log.header("INFO: EXCEPCIÓN")
            log.write(f"{type(e).__name__}: {e}")
            log.write(traceback.format_exc())
            self.error_signal.emit(self.row, f"{type(e).__name__}: {e}\nLog: {log_file}")

    @staticmethod
    def _pick_default_360(options: List[Dict[str, Any]]) -> str:
        # 1) 360p muxed si existe
        c360 = [o for o in options if o.get("height") == 360]
        if c360:
            muxed = [o for o in c360 if o.get("kind") == "muxed"]
            return (muxed[0] if muxed else c360[0])["selector"]

        # 2) closest below 360
        heights = sorted({o.get("height") for o in options if isinstance(o.get("height"), int)})
        below = [h for h in heights if h < 360]
        if below:
            h = max(below)
            cand = [o for o in options if o.get("height") == h]
            muxed = [o for o in cand if o.get("kind") == "muxed"]
            return (muxed[0] if muxed else cand[0])["selector"]

        # 3) lowest available
        if heights:
            h = min(heights)
            cand = [o for o in options if o.get("height") == h]
            muxed = [o for o in cand if o.get("kind") == "muxed"]
            return (muxed[0] if muxed else cand[0])["selector"]

        # 4) fallback
        return options[0]["selector"]


# ----------------------------
# WORKER: DOWNLOAD (yt-dlp)
# ----------------------------
class DownloadWorker(QThread):
    progress_changed = pyqtSignal(int, int)  # row, percent
    status_changed = pyqtSignal(int, str)
    error_signal = pyqtSignal(int, str)

    def __init__(self, row: int, item: QueueItem, download_dir: str):
        super().__init__()
        self.row = row
        self.item = item
        self.download_dir = download_dir
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        title_part = sanitize_for_path(self.item.title or "sin_titulo")
        log_file = LOG_DIR / f"{now_stamp()}_DL_row{self.row+1}_{title_part}.txt"
        log = DualLogger(log_file)

        proc: Optional[subprocess.Popen] = None
        try:
            log.header("DESCARGA: INICIO")
            log.write(f"Row: {self.row+1}")
            log.write(f"URL: {self.item.url}")
            log.write(f"Title: {self.item.title}")
            log.write(f"Download dir: {self.download_dir}")
            log.write(f"YTDLP_EXE exists: {Path(YTDLP_EXE).exists()} -> {YTDLP_EXE}")
            log.write(f"FFMPEG_EXE exists: {Path(FFMPEG_EXE).exists()} -> {FFMPEG_EXE}")

            if not os.path.isdir(self.download_dir):
                raise RuntimeError(f"Carpeta de descarga inválida: {self.download_dir}")

            if not self.item.format_selector:
                raise RuntimeError("No hay formato seleccionado (format_selector).")

            base = safe_filename(self.item.out_name) if self.item.out_name else safe_filename(self.item.title)
            if not base:
                base = "video"

            outtmpl = str(Path(self.download_dir) / (base + ".%(ext)s"))
            fmt = self.item.format_selector

            self.status_changed.emit(self.row, "Descargando...")
            self.progress_changed.emit(self.row, 0)

            # Progreso robusto: parseamos una línea machine-readable
            # download:%(progress._percent_str)s|%(progress.status)s|...
            cmd = YTDLP_CMD + [
                "--no-config",
                "--no-warnings",
                "--verbose",
                "--newline",
                "--progress",
                "--progress-template",
                "download:%(progress._percent_str)s|%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.eta)s",
                "--ffmpeg-location", FFMPEG_EXE,
                "-f", fmt,
                "-o", outtmpl,
                self.item.url,
            ]

            log.write("Command:")
            log.write("  " + " ".join([f'"{c}"' if " " in c else c for c in cmd]))
            log.write(f"Format selector (-f): {fmt}")
            log.write(f"Output template (-o): {outtmpl}")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            while True:
                if self._cancel:
                    log.write("Cancel requested -> terminating process")
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    self.status_changed.emit(self.row, "Cancelado")
                    log.header("DESCARGA: CANCELADA")
                    return

                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    break

                line = line.rstrip("\n")
                log.write(line)

                if line.startswith("download:"):
                    payload = line[len("download:"):]
                    parts = payload.split("|")
                    if parts:
                        percent_str = parts[0].strip().replace("%", "").strip()
                        try:
                            pct = int(float(percent_str))
                            pct = max(0, min(100, pct))
                            self.progress_changed.emit(self.row, pct)
                        except Exception:
                            pass

            rc = proc.wait()
            log.write(f"Process return code: {rc}")

            if rc != 0:
                log.header("DESCARGA: ERROR (yt-dlp rc != 0)")
                log.write("Causas típicas: video privado/edad/geo, cookies necesarias, 403/429, formato no disponible, ffmpeg, etc.")
                raise RuntimeError(f"yt-dlp falló (rc={rc}). Revisa el log: {log_file}")

            self.progress_changed.emit(self.row, 100)
            self.status_changed.emit(self.row, "OK")
            log.header("DESCARGA: OK")

        except Exception as e:
            self.status_changed.emit(self.row, "Error")
            log.header("DESCARGA: EXCEPCIÓN PYTHON")
            log.write(f"{type(e).__name__}: {e}")
            log.write(traceback.format_exc())
            self.error_signal.emit(self.row, f"{type(e).__name__}: {e}\n\nLog: {log_file}")

        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass


# ----------------------------
# UI PRINCIPAL
# ----------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Descargador YouTube (PyQt6 + yt-dlp.exe) — format_id real")
        self.resize(1250, 680)

        self.queue: List[QueueItem] = []
        self.download_dir: str = str(Path.home() / "Downloads")

        self.info_workers: List[InfoWorker] = []
        self.download_workers: List[DownloadWorker] = []

        self.downloading = False
        self._download_index = 0

        # UI
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        root.addLayout(top)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Pega 1 o varias URLs (separadas por espacios o saltos de línea)")
        top.addWidget(self.url_input, 1)

        btn_add = QPushButton("Añadir (auto formatos)")
        btn_add.clicked.connect(self.add_urls)
        top.addWidget(btn_add)

        dir_row = QHBoxLayout()
        root.addLayout(dir_row)

        self.dir_label = QLabel("")
        dir_row.addWidget(self.dir_label, 1)

        btn_dir = QPushButton("Cambiar carpeta")
        btn_dir.clicked.connect(self.choose_dir)
        dir_row.addWidget(btn_dir)

        actions = QHBoxLayout()
        root.addLayout(actions)

        btn_remove = QPushButton("Eliminar (seleccionados)")
        btn_remove.clicked.connect(self.remove_selected)
        actions.addWidget(btn_remove)

        btn_clear = QPushButton("Vaciar cola")
        btn_clear.clicked.connect(self.clear_queue)
        actions.addWidget(btn_clear)

        actions.addStretch(1)

        self.btn_start = QPushButton("Descargar cola")
        self.btn_start.clicked.connect(self.start_downloads)
        actions.addWidget(self.btn_start)

        self.btn_stop = QPushButton("Cancelar")
        self.btn_stop.clicked.connect(self.cancel_downloads)
        self.btn_stop.setEnabled(False)
        actions.addWidget(self.btn_stop)

        # Tabla
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["URL", "Título", "Formato (selector real)", "Nombre salida", "Estado", "Progreso"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.SelectedClicked)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)

        # Validaciones de binarios
        if not Path(YTDLP_EXE).exists():
            QMessageBox.critical(self, "yt-dlp.exe no encontrado", f"No existe:\n{YTDLP_EXE}")
        if not Path(FFMPEG_EXE).exists():
            QMessageBox.warning(self, "ffmpeg.exe no encontrado", f"No existe:\n{FFMPEG_EXE}\n\nLos merges pueden fallar sin ffmpeg.")

        self.load_config()
        self.refresh_table()

    # -------------
    # Config
    # -------------
    def load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.download_dir = data.get("download_dir", self.download_dir)
            items = data.get("queue", [])
            self.queue = [QueueItem(**it) for it in items]
        except Exception:
            self.queue = []

    def save_config(self):
        try:
            data = {
                "download_dir": self.download_dir,
                "queue": [asdict(x) for x in self.queue],
            }
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # -------------
    # UI helpers
    # -------------
    def refresh_table(self):
        self.dir_label.setText(f"Carpeta: {self.download_dir}")
        self.table.setRowCount(0)

        for item in self.queue:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(item.url))
            self.table.setItem(row, 1, QTableWidgetItem(item.title))

            combo = QComboBox()
            # si ya hay selector en config, lo pondremos luego (cuando llegue info) o se verá en blanco
            combo.addItem("—", None)
            combo.currentIndexChanged.connect(lambda _idx, r=row: self.on_format_changed(r))
            self.table.setCellWidget(row, 2, combo)

            out_item = QTableWidgetItem(item.out_name)
            out_item.setToolTip("Nombre base sin extensión (yt-dlp asigna el contenedor final)")
            self.table.setItem(row, 3, out_item)

            self.table.setItem(row, 4, QTableWidgetItem(item.status))
            self.table.setItem(row, 5, QTableWidgetItem(f"{item.progress}%"))

        self.save_config()

        # Si hay items pendientes de info (por ejemplo tras abrir el programa)
        for i, it in enumerate(self.queue):
            if it.status in ("Pendiente", "Consultando...") or not it.format_selector:
                # Lanza consulta si falta título o selector
                if not it.title or it.status != "Listo":
                    self.start_info_worker(i)

    def selected_rows(self) -> List[int]:
        rows = set()
        for idx in self.table.selectionModel().selectedRows():
            rows.add(idx.row())
        return sorted(rows)

    def _set_cell_text(self, row: int, col: int, text: str):
        item = self.table.item(row, col)
        if item is None:
            item = QTableWidgetItem(text)
            self.table.setItem(row, col, item)
        else:
            item.setText(text)

    def _set_row_status(self, row: int, status: str):
        self._set_cell_text(row, 4, status)

    def _set_row_progress(self, row: int, pct: int):
        self._set_cell_text(row, 5, f"{pct}%")

    # -------------
    # Actions
    # -------------
    def add_urls(self):
        text = self.url_input.text().strip()
        if not text:
            return

        urls = split_urls(text)
        if not urls:
            QMessageBox.information(self, "Nada añadido", "No detecté URLs válidas.")
            return

        self.url_input.clear()

        start_row = len(self.queue)
        for u in urls:
            self.queue.append(QueueItem(url=u, status="Consultando...", progress=0))

        self.refresh_table()

        for i in range(start_row, len(self.queue)):
            self.start_info_worker(i)

    def start_info_worker(self, row: int):
        if not (0 <= row < len(self.queue)):
            return
        # evitamos arrancar multiples workers si ya está consultando
        self.queue[row].status = "Consultando..."
        self._set_row_status(row, "Consultando...")

        worker = InfoWorker(row=row, url=self.queue[row].url)
        worker.status_changed.connect(self.on_status_changed)
        worker.info_ready.connect(self.on_info_ready)
        worker.error_signal.connect(self.on_error)
        worker.start()
        self.info_workers.append(worker)

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Selecciona carpeta de descarga", self.download_dir)
        if d:
            self.download_dir = d
            self.refresh_table()

    def remove_selected(self):
        if self.downloading:
            QMessageBox.warning(self, "Descargando", "Cancela primero las descargas.")
            return
        rows = self.selected_rows()
        if not rows:
            return
        for r in reversed(rows):
            if 0 <= r < len(self.queue):
                self.queue.pop(r)
        self.refresh_table()

    def clear_queue(self):
        if self.downloading:
            QMessageBox.warning(self, "Descargando", "Cancela primero las descargas.")
            return
        self.queue = []
        self.refresh_table()

    # -------------
    # Combo format changed
    # -------------
    def on_format_changed(self, row: int):
        if not (0 <= row < len(self.queue)):
            return
        combo: QComboBox = self.table.cellWidget(row, 2)  # type: ignore
        if combo is None:
            return
        selector = combo.currentData()
        if not selector:
            return
        self.queue[row].format_selector = str(selector)
        if self.queue[row].status not in ("Descargando...", "OK"):
            self.queue[row].status = "Listo"
            self._set_row_status(row, "Listo")
        self.save_config()

    # -------------
    # Download flow
    # -------------
    def start_downloads(self):
        if self.downloading:
            return
        if not self.queue:
            QMessageBox.information(self, "Cola vacía", "Añade al menos una URL.")
            return

        if not os.path.isdir(self.download_dir):
            QMessageBox.warning(self, "Carpeta inválida", "La carpeta de descarga no existe.")
            return

        # Volcar nombres desde tabla
        for i in range(len(self.queue)):
            cell = self.table.item(i, 3)
            self.queue[i].out_name = cell.text().strip() if cell else ""

        missing = [
            i for i, it in enumerate(self.queue)
            if not it.format_selector or it.status.startswith("Consultando")
        ]
        if missing:
            QMessageBox.warning(
                self,
                "Faltan formatos",
                "Hay vídeos que aún no tienen formato seleccionado o están consultando.\n"
                "Espera a que estén en 'Listo' y revisa el combo."
            )
            return

        self.downloading = True
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._download_index = 0

        self.start_next_download()

    def start_next_download(self):
        while self._download_index < len(self.queue) and self.queue[self._download_index].status == "OK":
            self._download_index += 1

        if self._download_index >= len(self.queue):
            self.downloading = False
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            QMessageBox.information(self, "Terminado", "La cola ha finalizado.")
            self.refresh_table()
            return

        row = self._download_index
        item = self.queue[row]
        item.status = "Descargando..."
        item.progress = 0
        self._set_row_status(row, item.status)
        self._set_row_progress(row, 0)

        w = DownloadWorker(row=row, item=item, download_dir=self.download_dir)
        w.status_changed.connect(self.on_status_changed)
        w.progress_changed.connect(self.on_progress_changed)
        w.error_signal.connect(self.on_error)
        w.finished.connect(self.on_download_finished)
        w.start()
        self.download_workers.append(w)

    def on_download_finished(self):
        self._download_index += 1
        self.start_next_download()

    def cancel_downloads(self):
        for w in self.download_workers:
            try:
                w.cancel()
            except Exception:
                pass
        self.download_workers = []

        self.downloading = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        QMessageBox.information(self, "Cancelado", "Se ha solicitado cancelación (puede tardar unos segundos).")

    # -------------
    # Worker signals
    # -------------
    def on_status_changed(self, row: int, status: str):
        if not (0 <= row < len(self.queue)):
            return
        self.queue[row].status = status
        self._set_row_status(row, status)
        self.save_config()

    def on_progress_changed(self, row: int, pct: int):
        if not (0 <= row < len(self.queue)):
            return
        self.queue[row].progress = pct
        self._set_row_progress(row, pct)
        self.save_config()

    def on_info_ready(self, row: int, title: str, options: List[Dict[str, Any]], default_selector: str):
        if not (0 <= row < len(self.queue)):
            return

        self.queue[row].title = title
        self._set_cell_text(row, 1, title)

        # Suggest name if empty
        out_cell = self.table.item(row, 3)
        if out_cell and not out_cell.text().strip():
            suggested = safe_filename(title)
            out_cell.setText(suggested)
            self.queue[row].out_name = suggested

        combo: QComboBox = self.table.cellWidget(row, 2)  # type: ignore
        if combo is None:
            return

        combo.blockSignals(True)
        combo.clear()

        for opt in options:
            combo.addItem(opt["label"], opt["selector"])

        # default 360p
        idx = combo.findData(default_selector)
        if idx >= 0:
            combo.setCurrentIndex(idx)
            self.queue[row].format_selector = str(combo.currentData())
        else:
            combo.setCurrentIndex(0)
            self.queue[row].format_selector = str(combo.currentData())

        combo.blockSignals(False)

        self.queue[row].status = "Listo"
        self._set_row_status(row, "Listo")
        self.save_config()

    def on_error(self, row: int, message: str):
        if 0 <= row < len(self.queue):
            self.queue[row].status = "Error"
            self._set_row_status(row, "Error")
        QMessageBox.warning(self, "Error", f"Fila {row+1}:\n{message}")
        self.save_config()

    def closeEvent(self, event):
        self.save_config()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()