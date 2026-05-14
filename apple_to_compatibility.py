import sys
import os
import re
import subprocess
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QComboBox,
    QProgressBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QFrame, QSizePolicy
)
from PyQt5.QtCore import QThread, pyqtSignal, QTimer, Qt
from PyQt5.QtGui import QFont, QColor, QBrush, QIcon


# ================================================================
# 狀態常數：每個檔案在佇列中的生命週期
# ================================================================
S_PENDING    = "⏳ 待處理"
S_PROCESSING = "⚙️  處理中"
S_DONE       = "✅ 完成"
S_ERROR      = "❌ 失敗"
S_CANCELLED  = "🚫 已取消"

# 對應的顏色 (背景, 前景)
STATUS_COLORS = {
    S_PENDING:    ("#F5F5F5", "#888888"),
    S_PROCESSING: ("#E3F2FD", "#1565C0"),
    S_DONE:       ("#E8F5E9", "#2E7D32"),
    S_ERROR:      ("#FFEBEE", "#C62828"),
    S_CANCELLED:  ("#FFF3E0", "#E65100"),
}

# 表格欄位
COL_NUM      = 0
COL_FILENAME = 1
COL_STATUS   = 2
COL_DURATION = 3
COL_ELAPSED  = 4
COL_OUTPUT   = 5


# ================================================================
# 工具函式
# ================================================================
def get_video_duration(filepath: str) -> float:
    """用 ffprobe 取得影片真實總秒數，失敗回傳 0.0"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1',
             filepath],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', errors='ignore', timeout=10
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def seconds_to_mmss(s: float) -> str:
    """把浮點秒數轉成 MM:SS 字串，用於顯示影片時長"""
    if s <= 0:
        return "—"
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


# ================================================================
# FFmpegWorker：在背景執行單一檔案的 FFmpeg 工作
# ================================================================
class FFmpegWorker(QThread):
    progress_signal = pyqtSignal(int)       # 目前這個檔案的進度 0~99
    finished_signal = pyqtSignal(bool, str) # (成功?, 輸出路徑或錯誤訊息)

    def __init__(self, command: list, output_file: str, total_seconds: float):
        super().__init__()
        self.command = command
        self.output_file = output_file
        self.total_seconds = total_seconds
        self._process = None
        self._stop_flag = False

    def stop(self):
        """外部呼叫：強制終止正在執行的 FFmpeg process"""
        self._stop_flag = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def run(self):
        time_pattern = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})")
        try:
            self._process = subprocess.Popen(
                self.command,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                universal_newlines=True,
                encoding='utf-8',
                errors='ignore'
            )
            for line in self._process.stdout:
                if self._stop_flag:
                    break
                match = time_pattern.search(line)
                if match and self.total_seconds > 0:
                    h, m, s = match.groups()
                    cur = int(h) * 3600 + int(m) * 60 + float(s)
                    pct = int(min(cur / self.total_seconds * 100, 99))
                    self.progress_signal.emit(pct)

            self._process.wait()

            if self._stop_flag:
                self.finished_signal.emit(False, "__cancelled__")
            elif self._process.returncode == 0:
                self.finished_signal.emit(True, self.output_file)
            else:
                self.finished_signal.emit(False, "FFmpeg 回傳錯誤，請確認格式是否支援")

        except FileNotFoundError:
            self.finished_signal.emit(False, "找不到 ffmpeg，請確認已安裝並加入 PATH")
        except Exception as e:
            self.finished_signal.emit(False, f"未知錯誤：{e}")


# ================================================================
# BatchConverterUI：主視窗
# ================================================================
class BatchConverterUI(QWidget):

    def __init__(self):
        super().__init__()
        # 佇列：list of dict，每個 dict 存這個檔案的所有狀態
        self._queue: list[dict] = []
        self._current_idx = -1      # 正在處理的是第幾個
        self._worker: FFmpegWorker | None = None
        self._stop_all = False      # 使用者按下停止時設為 True
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._initUI()

    # ────────────────────────────────────────────────────────────
    # UI 建構
    # ────────────────────────────────────────────────────────────
    def _initUI(self):
        self.setWindowIcon(QIcon("icon.ico"))
        self.setWindowTitle("Apple格式 FFmpeg 批次轉通用格式工具")
        self.resize(760, 560)

        root = QVBoxLayout()
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)
        self.setLayout(root)

        # ── 標題 ───────────────────────────────────────────────
        title = QLabel("🎬  Apple格式 FFmpeg 批次轉通用格式工具")
        title.setFont(QFont("", 16, QFont.Bold))
        root.addWidget(title)

        # ── 操作按鈕列（加入 / 移除 / 清空）──────────────────
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("➕ 加入檔案")
        self.btn_add.setStyleSheet("padding: 7px 14px; font-size: 13px;")
        self.btn_add.clicked.connect(self._add_files)
        btn_row.addWidget(self.btn_add)

        self.btn_remove = QPushButton("➖ 移除選取")
        self.btn_remove.setStyleSheet("padding: 7px 14px; font-size: 13px;")
        self.btn_remove.clicked.connect(self._remove_selected)
        btn_row.addWidget(self.btn_remove)

        self.btn_clear = QPushButton("🗑  清空佇列")
        self.btn_clear.setStyleSheet("padding: 7px 14px; font-size: 13px;")
        self.btn_clear.clicked.connect(self._clear_queue)
        btn_row.addWidget(self.btn_clear)

        btn_row.addStretch()

        self.queue_count_label = QLabel("佇列：0 個檔案")
        self.queue_count_label.setStyleSheet("color: #666; font-size: 13px;")
        btn_row.addWidget(self.queue_count_label)

        root.addLayout(btn_row)

        # ── 檔案佇列表格 ──────────────────────────────────────
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "檔案名稱", "狀態", "時長", "耗時", "輸出路徑"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setStyleSheet("""
            QTableWidget { border: 1px solid #E0E0E0; border-radius: 6px; font-size: 13px; }
            QHeaderView::section { background: #F5F5F5; padding: 6px; border: none;
                                   border-bottom: 1px solid #E0E0E0; font-weight: bold; }
            QTableWidget::item { padding: 4px 8px; }
        """)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_NUM,      QHeaderView.Fixed);       self.table.setColumnWidth(COL_NUM, 36)
        hdr.setSectionResizeMode(COL_FILENAME, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_STATUS,   QHeaderView.Fixed);       self.table.setColumnWidth(COL_STATUS, 100)
        hdr.setSectionResizeMode(COL_DURATION, QHeaderView.Fixed);       self.table.setColumnWidth(COL_DURATION, 72)
        hdr.setSectionResizeMode(COL_ELAPSED,  QHeaderView.Fixed);       self.table.setColumnWidth(COL_ELAPSED, 64)
        hdr.setSectionResizeMode(COL_OUTPUT,   QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(200)
        root.addWidget(self.table)

        # ── 設定區（分隔線 + 選項）───────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #E0E0E0;")
        root.addWidget(sep)

        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("⚡ 轉檔速度："))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems([
            "ultrafast  ── 最快，檔案較大",
            "medium     ── 速度與體積平衡（推薦）",
            "veryslow   ── 最慢，體積最小",
        ])
        self.preset_combo.setCurrentIndex(1)
        self.preset_combo.setFixedWidth(240)
        settings_row.addWidget(self.preset_combo)
        settings_row.addSpacing(24)

        settings_row.addWidget(QLabel("📝 輸出後綴："))
        self.suffix_combo = QComboBox()
        self.suffix_combo.addItems(["_converted", "_fixed", "_batch", "（原檔名）"])
        self.suffix_combo.setFixedWidth(130)
        settings_row.addWidget(self.suffix_combo)
        settings_row.addStretch()
        root.addLayout(settings_row)

        # ── 目前檔案進度 ──────────────────────────────────────
        self.cur_file_label = QLabel("目前進度：—")
        self.cur_file_label.setStyleSheet("font-size: 12px; color: #555;")
        root.addWidget(self.cur_file_label)

        cur_prog_row = QHBoxLayout()
        self.cur_progress = QProgressBar()
        self.cur_progress.setValue(0)
        self.cur_progress.setTextVisible(False)
        self.cur_progress.setFixedHeight(14)
        self.cur_progress.setStyleSheet("""
            QProgressBar { border-radius: 7px; background: #E0E0E0; }
            QProgressBar::chunk { border-radius: 7px; background: #2196F3; }
        """)
        cur_prog_row.addWidget(self.cur_progress)
        self.cur_pct_label = QLabel("—")
        self.cur_pct_label.setFixedWidth(38)
        self.cur_pct_label.setStyleSheet("font-size: 12px; color: #555;")
        cur_prog_row.addWidget(self.cur_pct_label)
        root.addLayout(cur_prog_row)

        # ── 整體進度 ──────────────────────────────────────────
        self.overall_label = QLabel("整體進度：0 / 0")
        self.overall_label.setStyleSheet("font-size: 12px; color: #555;")
        root.addWidget(self.overall_label)

        overall_row = QHBoxLayout()
        self.overall_progress = QProgressBar()
        self.overall_progress.setValue(0)
        self.overall_progress.setTextVisible(False)
        self.overall_progress.setFixedHeight(14)
        self.overall_progress.setStyleSheet("""
            QProgressBar { border-radius: 7px; background: #E0E0E0; }
            QProgressBar::chunk { border-radius: 7px; background: #4CAF50; }
        """)
        overall_row.addWidget(self.overall_progress)
        self.overall_pct_label = QLabel("0 / 0")
        self.overall_pct_label.setFixedWidth(48)
        self.overall_pct_label.setStyleSheet("font-size: 12px; color: #555;")
        overall_row.addWidget(self.overall_pct_label)
        root.addLayout(overall_row)

        # ── 計時 + 狀態訊息 ───────────────────────────────────
        bottom_row = QHBoxLayout()
        self.status_label = QLabel("就緒。請加入影片後按下「開始全部轉換」。")
        self.status_label.setStyleSheet("font-size: 12px; color: #666;")
        bottom_row.addWidget(self.status_label)
        bottom_row.addStretch()
        self.time_label = QLabel("")
        self.time_label.setStyleSheet("font-size: 12px; color: #999;")
        bottom_row.addWidget(self.time_label)
        root.addLayout(bottom_row)

        # ── 主要操作按鈕 ──────────────────────────────────────
        action_row = QHBoxLayout()
        self.btn_start = QPushButton("🚀  開始全部轉換")
        self.btn_start.setStyleSheet("""
            QPushButton { background:#2196F3; color:white; font-weight:bold;
                          padding:11px 20px; font-size:14px; border-radius:6px; }
            QPushButton:hover { background:#1976D2; }
            QPushButton:disabled { background:#B0BEC5; }
        """)
        self.btn_start.clicked.connect(self._start_all)
        action_row.addWidget(self.btn_start)

        self.btn_stop = QPushButton("⏹  停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("""
            QPushButton { background:#F44336; color:white; font-weight:bold;
                          padding:11px 20px; font-size:14px; border-radius:6px; }
            QPushButton:hover { background:#D32F2F; }
            QPushButton:disabled { background:#B0BEC5; }
        """)
        self.btn_stop.clicked.connect(self._stop_all_jobs)
        action_row.addWidget(self.btn_stop)

        root.addLayout(action_row)

    # ────────────────────────────────────────────────────────────
    # 佇列管理
    # ────────────────────────────────────────────────────────────
    def _add_files(self):
        fnames, _ = QFileDialog.getOpenFileNames(
            self, "選擇影片檔案（可多選）", "",
            "Video Files (*.mov *.mp4 *.mkv *.avi *.flv *.webm *.wmv *.m4v)"
        )
        existing = {item["path"] for item in self._queue}
        added = 0
        for f in fnames:
            if f in existing:
                continue  # 跳過重複的
            duration = get_video_duration(f)
            item = {
                "path": f,
                "status": S_PENDING,
                "duration": duration,
                "elapsed": 0,
                "output": "",
                "error": "",
            }
            self._queue.append(item)
            self._add_table_row(item)
            added += 1
        if added:
            self._refresh_queue_count()
            self._refresh_overall_progress()

    def _add_table_row(self, item: dict):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 30)

        def cell(text, align=Qt.AlignLeft | Qt.AlignVCenter):
            c = QTableWidgetItem(text)
            c.setTextAlignment(align)
            return c

        self.table.setItem(row, COL_NUM,      cell(str(row + 1), Qt.AlignCenter | Qt.AlignVCenter))
        self.table.setItem(row, COL_FILENAME, cell(os.path.basename(item["path"])))
        self.table.setItem(row, COL_DURATION, cell(seconds_to_mmss(item["duration"]), Qt.AlignCenter | Qt.AlignVCenter))
        self.table.setItem(row, COL_ELAPSED,  cell("—", Qt.AlignCenter | Qt.AlignVCenter))
        self.table.setItem(row, COL_OUTPUT,   cell(""))
        self._set_row_status(row, S_PENDING)

    def _set_row_status(self, row: int, status: str):
        c = QTableWidgetItem(status)
        c.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        bg, fg = STATUS_COLORS.get(status, ("#FFF", "#000"))
        c.setBackground(QBrush(QColor(bg)))
        c.setForeground(QBrush(QColor(fg)))
        self.table.setItem(row, COL_STATUS, c)

    def _remove_selected(self):
        if self._worker and self._worker.isRunning():
            self.status_label.setText("⚠️  轉檔進行中，無法移除檔案。請先停止。")
            return
        rows = sorted(set(i.row() for i in self.table.selectedItems()), reverse=True)
        for row in rows:
            self.table.removeRow(row)
            self._queue.pop(row)
        # 重新編號
        for i in range(self.table.rowCount()):
            self.table.item(i, COL_NUM).setText(str(i + 1))
            self._queue[i]  # 已同步
        self._refresh_queue_count()
        self._refresh_overall_progress()

    def _clear_queue(self):
        if self._worker and self._worker.isRunning():
            self.status_label.setText("⚠️  轉檔進行中，請先停止再清空佇列。")
            return
        self.table.setRowCount(0)
        self._queue.clear()
        self._refresh_queue_count()
        self._refresh_overall_progress()

    def _refresh_queue_count(self):
        n = len(self._queue)
        self.queue_count_label.setText(f"佇列：{n} 個檔案")

    # ────────────────────────────────────────────────────────────
    # 批次轉換控制
    # ────────────────────────────────────────────────────────────
    def _start_all(self):
        pending = [i for i, item in enumerate(self._queue) if item["status"] == S_PENDING]
        if not pending:
            self.status_label.setText("⚠️  沒有待處理的檔案（已全部完成或佇列是空的）。")
            return

        self._stop_all = False
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_add.setEnabled(False)
        self.btn_remove.setEnabled(False)
        self.btn_clear.setEnabled(False)

        self._elapsed = 0
        self._timer.start(1000)
        self._process_next()

    def _process_next(self):
        """找到下一個待處理的項目並開始轉換"""
        if self._stop_all:
            self._on_all_done()
            return

        next_idx = next(
            (i for i, item in enumerate(self._queue) if item["status"] == S_PENDING),
            None
        )
        if next_idx is None:
            self._on_all_done()
            return

        self._current_idx = next_idx
        item = self._queue[next_idx]
        item["status"] = S_PROCESSING

        # 更新表格狀態
        self._set_row_status(next_idx, S_PROCESSING)
        self.table.scrollToItem(self.table.item(next_idx, 0))

        # 目前進度條重置
        if item["duration"] > 0:
            self.cur_progress.setMaximum(100)
            self.cur_progress.setValue(0)
        else:
            self.cur_progress.setMaximum(0)  # 跑馬燈
        self.cur_pct_label.setText("0%")
        self.cur_file_label.setText(
            f"目前進度：[{next_idx + 1}/{len(self._queue)}]  {os.path.basename(item['path'])}"
        )

        # 決定輸出路徑
        suffix_text = self.suffix_combo.currentText()
        base, ext = os.path.splitext(item["path"])
        if "原檔名" in suffix_text:
            output_file = base + "_out.mp4"
        else:
            suffix = suffix_text.split()[0]
            output_file = base + suffix + ".mp4"

        preset_text = self.preset_combo.currentText().split()[0]
        command = [
            "ffmpeg", "-i", item["path"],
            "-c:v", "libx264", "-preset", preset_text,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-y", output_file
        ]

        self._item_start_time = self._elapsed  # 記錄這個檔案開始時的總計時

        self._worker = FFmpegWorker(command, output_file, item["duration"])
        self._worker.progress_signal.connect(self._on_file_progress)
        self._worker.finished_signal.connect(self._on_file_finished)
        self._worker.start()

        self._refresh_overall_progress()
        self.status_label.setText(
            f"⚙️  轉換中：{os.path.basename(item['path'])}"
        )

    def _on_file_progress(self, pct: int):
        self.cur_progress.setValue(pct)
        self.cur_pct_label.setText(f"{pct}%")

    def _on_file_finished(self, success: bool, message: str):
        idx = self._current_idx
        item = self._queue[idx]
        item_elapsed = self._elapsed - self._item_start_time

        if message == "__cancelled__":
            item["status"] = S_CANCELLED
            self._set_row_status(idx, S_CANCELLED)
            self.table.item(idx, COL_ELAPSED).setText("—")
        elif success:
            item["status"] = S_DONE
            item["output"] = message
            item["elapsed"] = item_elapsed
            self._set_row_status(idx, S_DONE)
            self.table.item(idx, COL_ELAPSED).setText(f"{item_elapsed}s")
            self.table.item(idx, COL_OUTPUT).setText(os.path.basename(message))
            # 目前進度條推到 100%
            self.cur_progress.setMaximum(100)
            self.cur_progress.setValue(100)
            self.cur_pct_label.setText("100%")
        else:
            item["status"] = S_ERROR
            item["error"] = message
            self._set_row_status(idx, S_ERROR)
            self.table.item(idx, COL_ELAPSED).setText("—")
            self.table.item(idx, COL_OUTPUT).setText(message)

        self._refresh_overall_progress()

        # 如果有已取消則標記剩餘全部
        if self._stop_all:
            for i, q in enumerate(self._queue):
                if q["status"] == S_PENDING:
                    q["status"] = S_CANCELLED
                    self._set_row_status(i, S_CANCELLED)
            self._on_all_done()
        else:
            self._process_next()

    def _stop_all_jobs(self):
        self._stop_all = True
        if self._worker and self._worker.isRunning():
            self._worker.stop()
        self.btn_stop.setEnabled(False)
        self.status_label.setText("🛑  正在停止，等待目前檔案終止...")

    def _on_all_done(self):
        self._timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_add.setEnabled(True)
        self.btn_remove.setEnabled(True)
        self.btn_clear.setEnabled(True)

        done    = sum(1 for item in self._queue if item["status"] == S_DONE)
        error   = sum(1 for item in self._queue if item["status"] == S_ERROR)
        cancelled = sum(1 for item in self._queue if item["status"] == S_CANCELLED)
        total   = len(self._queue)

        self.cur_file_label.setText("目前進度：已結束")
        self.cur_progress.setMaximum(100)
        self.cur_progress.setValue(0)
        self.cur_pct_label.setText("—")

        parts = [f"✅ {done} 成功"]
        if error:     parts.append(f"❌ {error} 失敗")
        if cancelled: parts.append(f"🚫 {cancelled} 取消")
        self.status_label.setText(f"全部完成！共 {total} 個：{'　'.join(parts)}　總耗時 {self._elapsed} 秒")
        self.time_label.setText("")

    # ────────────────────────────────────────────────────────────
    # 進度 / 計時 更新
    # ────────────────────────────────────────────────────────────
    def _refresh_overall_progress(self):
        total = len(self._queue)
        if total == 0:
            self.overall_progress.setMaximum(1)
            self.overall_progress.setValue(0)
            self.overall_label.setText("整體進度：0 / 0")
            self.overall_pct_label.setText("0 / 0")
            return
        done = sum(1 for item in self._queue
                   if item["status"] in (S_DONE, S_ERROR, S_CANCELLED))
        self.overall_progress.setMaximum(total)
        self.overall_progress.setValue(done)
        self.overall_label.setText(f"整體進度：{done} / {total} 完成")
        self.overall_pct_label.setText(f"{done} / {total}")

    def _tick(self):
        self._elapsed += 1
        self.time_label.setText(f"已耗時：{self._elapsed} 秒")


# ================================================================
# 入口
# ================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = BatchConverterUI()
    window.show()
    sys.exit(app.exec_())