#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
e-hentai 画廊下载器 —— 图形界面版

使用系统代理（默认），不额外配置。
依赖：PyQt5, requests, curl (系统自带)
"""

import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from PyQt5.QtCore import (
    QThread, pyqtSignal, Qt, QModelIndex, QTimer, QUrl
)
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QDoubleSpinBox, QSpinBox,
    QCheckBox, QGroupBox, QTextEdit, QFileDialog,
    QMessageBox, QProgressBar, QAbstractItemView,
    QMenu, QAction
)
from PyQt5.QtGui import (
    QColor, QTextCursor, QFont, QIcon, QPixmap, QPainter, QPen,
    QDesktopServices
)

# ─── 核心模块 ──────────────────────────────────────────
try:
    import requests
except ImportError:
    QMessageBox.critical(None, "缺少依赖",
                         "请先安装依赖：pip install requests PyQt5")
    raise

# ─── 常量 ──────────────────────────────────────────────────
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
MIN_VALID_BYTES = 5000
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = Path.home() / "Desktop"
APP_NAME = "E-Hentai Downloader"
APP_VERSION = "1.0"


# ─── 工具函数 ──────────────────────────────────────────────

def safe_name(s: str, max_len: int = 100) -> str:
    s = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:max_len].rstrip(" .") or "untitled")


def is_valid_file(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= MIN_VALID_BYTES


# ─── 核心下载函数 ──────────────────────────────────────────

def create_session() -> requests.Session:
    """不设 proxies → 默认使用系统代理"""
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    return session


def search_galleries(session: requests.Session, keyword: str,
                     max_pages: int = 0, log_cb=None) -> list[dict]:
    seen = {}

    def log(msg):
        if log_cb:
            log_cb(msg)

    page = 0
    while True:
        url = f"https://e-hentai.org/?f_search={quote(keyword)}"
        if page > 0:
            url += f"&page={page}"

        for attempt in range(3):
            try:
                resp = session.get(url, timeout=30)
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log(f"[错误] 搜索页面获取失败：{e}")
                    return list(seen.values())

        text = resp.text
        links = re.findall(
            r'href="(https://e-hentai\.org/g/(\d+)/([^/"]+)/?)"', text)
        if not links:
            break

        for full_url, gid, token in links:
            gid = int(gid)
            if gid not in seen:
                seen[gid] = {
                    "gid": gid,
                    "token": token,
                    "url": full_url.rstrip("/"),
                }

        if f"p={page + 1}" not in text:
            break

        page += 1
        if max_pages and page >= max_pages:
            break
        time.sleep(0.3)

    return list(seen.values())


def get_gallery_metadata(session: requests.Session, galleries: list[dict],
                         log_cb=None) -> list[dict]:
    def log(msg):
        if log_cb:
            log_cb(msg)

    for i in range(0, len(galleries), 25):
        batch = galleries[i:i + 25]
        gidlist = [[g["gid"], g["token"]] for g in batch]

        for attempt in range(3):
            try:
                resp = session.post(
                    "https://api.e-hentai.org/api.php",
                    json={
                        "method": "gdata",
                        "gidlist": gidlist,
                        "namespace": 1,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log(f"[错误] API 获取失败：{e}")
                    data = {"gmetadata": []}

        for meta in data.get("gmetadata", []):
            gid = meta["gid"]
            for g in galleries:
                if g["gid"] == gid:
                    g["title"] = meta.get("title", "")
                    g["title_jpn"] = meta.get("title_jpn", "")
                    g["pages"] = int(meta.get("filecount", 0))
                    g["tags"] = meta.get("tags", [])
                    break

        time.sleep(0.3)

    return galleries


def collect_image_page_links(session: requests.Session, gid: int,
                             token: str, total_pages: int = 0,
                             log_cb=None) -> dict[int, str]:
    def log(msg):
        if log_cb:
            log_cb(msg)

    base = f"https://e-hentai.org/g/{gid}/{token}/"
    all_links = {}

    thumb_pages = (total_pages + 19) // 20 if total_pages > 0 else 50

    for page in range(thumb_pages):
        url = base if page == 0 else f"{base}?p={page}"
        for attempt in range(3):
            try:
                resp = session.get(url, timeout=30)
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log(f"  [错误] 缩略图页 {page} 获取失败：{e}")
                    return all_links

        links = re.findall(
            r'href="(https://e-hentai\.org/s/\w+/\w+-(\d+))"', resp.text)
        if not links:
            break

        for link, page_num in links:
            pn = int(page_num)
            if pn not in all_links:
                all_links[pn] = link

        time.sleep(0.3)

    return all_links


def extract_image_url(session: requests.Session,
                      page_url: str) -> Optional[str]:
    for attempt in range(3):
        try:
            resp = session.get(page_url, timeout=30)
            m = re.search(r'id="img"[^>]*src="([^"]+)"', resp.text)
            if m:
                return m.group(1)
        except Exception:
            time.sleep(1)
    return None


def download_image_direct(img_url: str, save_path: str,
                          referer: str) -> bool:
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "-s", "-L", "--insecure",
                 "-H", f"Referer: {referer}",
                 "-H", f"User-Agent: {UA}",
                 "-o", save_path, "--max-time", "120", img_url],
                capture_output=True, text=True, timeout=130)
            if (os.path.exists(save_path)
                    and os.path.getsize(save_path) > MIN_VALID_BYTES):
                return True
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return False


def cbz_album(cbz_path: Path, files: list[Path]) -> None:
    if cbz_path.exists():
        cbz_path.unlink()
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
        for fp in files:
            if is_valid_file(fp):
                zf.write(fp, arcname=fp.name)


def get_folder_name(gal: dict) -> str:
    """优先使用英文标题（鱼王需求 #6）"""
    en = gal.get("title", "").strip()
    if en:
        return safe_name(en)
    jpn = gal.get("title_jpn", "").strip()
    return safe_name(jpn) if jpn else f"gallery_{gal['gid']}"





# ─── 工作线程 ──────────────────────────────────────────────

class WorkerThread(QThread):
    log = pyqtSignal(str)
    search_done = pyqtSignal(list)

    # 画廊进度：gid, 当前图片序号, 总图片数
    gallery_progress = pyqtSignal(int, int, int)
    # 全局进度：当前已处理图片数, 总图片数
    total_progress = pyqtSignal(int, int)
    # 单个画廊完成
    gallery_done = pyqtSignal(int, bool)
    # 全部完成：总失败数
    all_done = pyqtSignal(int)

    error_occurred = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._session = create_session()
        self._task = None
        self._kwargs = {}
        self._running = True

    def stop(self):
        self._running = False

    def start_search(self, keyword: str):
        self._task = "search"
        self._kwargs = {"keyword": keyword}
        self.start()

    def start_download(self, galleries: list[dict], out_dir: Path,
                       delay: float, overwrite: bool, keep_folder: bool):
        self._task = "download"
        self._kwargs = {
            "galleries": galleries,
            "out_dir": out_dir,
            "delay": delay,
            "overwrite": overwrite,
            "keep_folder": keep_folder,
        }
        self.start()

    def run(self):
        try:
            if self._task == "search":
                self._do_search()
            elif self._task == "download":
                self._do_download()
        except Exception as e:
            self.error_occurred.emit(traceback.format_exc())

    # ── 搜索 ──
    def _do_search(self):
        keyword = self._kwargs["keyword"]
        self.log.emit(f"搜索：{keyword}")
        galleries = search_galleries(
            self._session, keyword, log_cb=self._log)
        if not galleries:
            self.log.emit("未找到结果。")
            self.search_done.emit([])
            return

        self.log.emit(
            f"获取元数据（{len(galleries)} 个画廊）...")
        galleries = get_gallery_metadata(
            self._session, galleries, log_cb=self._log)
        self.log.emit(f"搜索完成，找到 {len(galleries)} 个画廊。")
        self.search_done.emit(galleries)

    # ── 下载 ──
    def _do_download(self):
        kw = self._kwargs
        galleries: list[dict] = kw["galleries"]
        out_dir: Path = kw["out_dir"]
        delay: float = kw["delay"]
        overwrite: bool = kw["overwrite"]
        keep_folder: bool = kw["keep_folder"]

        out_dir.mkdir(parents=True, exist_ok=True)

        total = len(galleries)
        failed_count = 0

        # 计算全局总图片数
        grand_total = sum(g.get("pages", 0) for g in galleries)
        grand_current = 0

        for i, gal in enumerate(galleries, 1):
            if not self._running:
                self.log.emit("[中断] 用户停止下载。")
                break

            title = (gal.get("title_jpn")
                     or gal.get("title", f"gallery_{gal['gid']}"))
            self.log.emit(
                f"\n[{i}/{total}] {title}")
            self.log.emit(
                f"  https://e-hentai.org/g/{gal['gid']}/{gal['token']}/"
                f"  ({gal.get('pages', '?')}页)")

            need_download = True
            gal_expected = gal.get("pages", 0)

            try:
                skipped = self._check_skip(gal, out_dir, overwrite)
                if skipped is not None:
                    self.log.emit(
                        f"  [跳过] CBZ 已完整：{skipped[0]} ({skipped[1]}/{skipped[2]})")
                    grand_current += gal_expected
                    self.gallery_progress.emit(
                        gal["gid"], gal_expected, gal_expected)
                    self.total_progress.emit(grand_current, grand_total)
                    self.gallery_done.emit(gal["gid"], True)
                    need_download = False

                if need_download:
                    gid = gal["gid"]
                    last_processed = 0

                    def progress_cb(cur, tot):
                        nonlocal grand_current, last_processed
                        self.gallery_progress.emit(gid, cur, tot)
                        delta = max(cur - last_processed, 0)
                        if delta:
                            grand_current += delta
                            self.total_progress.emit(grand_current, grand_total)
                            last_processed = cur

                    result, processed = self._download_single(
                        gal, out_dir, delay, overwrite, keep_folder,
                        on_progress=progress_cb)

                    if result is None:
                        failed_count += 1
                        self.gallery_done.emit(gal["gid"], False)
                    else:
                        self.gallery_done.emit(gal["gid"], True)

            except Exception as e:
                failed_count += 1
                self.log.emit(f"  [错误] {e}")
                self.gallery_done.emit(gal["gid"], False)

        self.log.emit(f"\n{'=' * 50}")
        if failed_count:
            self.log.emit(
                f"[完成] {total - failed_count} 成功，{failed_count} 失败")
        else:
            self.log.emit(
                f"[完成] 全部 {total} 个画廊下载成功！")
        self.all_done.emit(failed_count)

    def _check_skip(self, gal: dict, out_dir: Path,
                    overwrite: bool) -> Optional[tuple]:
        """检查 CBZ 是否已完整。返回 (name, count, total) 或 None"""
        if overwrite:
            return None
        folder_name = get_folder_name(gal)
        cbz_path = out_dir / f"{folder_name}.cbz"
        total_expected = gal.get("pages", 0)
        if cbz_path.exists():
            try:
                with zipfile.ZipFile(cbz_path, "r") as zf:
                    cbz_count = len([
                        n for n in zf.namelist()
                        if re.search(r"\.(jpe?g|png|webp)$", n, re.I)
                    ])
                if total_expected and cbz_count >= total_expected:
                    return (cbz_path.name, cbz_count, total_expected)
            except Exception:
                pass
        return None

    def _download_single(self, gal: dict, out_dir: Path,
                         delay: float, overwrite: bool,
                         keep_folder: bool,
                         on_progress=None) -> tuple:
        """返回 (cbz_path or None, 实际处理的图片数)"""
        gid = gal["gid"]
        token = gal["token"]
        folder_name = get_folder_name(gal)
        total_expected = gal.get("pages", 0)
        gal_dir = out_dir / folder_name
        cbz_path = out_dir / f"{folder_name}.cbz"

        gal_dir.mkdir(parents=True, exist_ok=True)

        # 收集链接
        self.log.emit("  收集图片页链接...")
        page_links = collect_image_page_links(
            self._session, gid, token, total_expected, log_cb=self._log)
        if not page_links:
            self.log.emit("  [错误] 未找到图片链接")
            return (None, 0)
        self.log.emit(f"  找到 {len(page_links)} 个图片页")

        total_images = len(page_links)
        if on_progress:
            on_progress(0, total_images)

        # 找出缺失
        missing = []
        for pn in range(1, total_images + 1):
            candidates = list(gal_dir.glob(f"{pn:03d}.*"))
            if candidates and any(is_valid_file(c) for c in candidates):
                continue
            missing.append(pn)

        existing_count = total_images - len(missing)
        if on_progress and existing_count > 0 and not overwrite:
            on_progress(existing_count, total_images)

        if not missing and not overwrite:
            self.log.emit("  所有图片已存在")
            processed = total_images
            if on_progress:
                on_progress(processed, total_images)
        else:
            if overwrite:
                missing = list(range(1, total_images + 1))
                existing_count = 0
            self.log.emit(f"  需要下载：{len(missing)} 张")

            success = 0
            fail = 0

            for idx, pn in enumerate(missing):
                if not self._running:
                    return (None, idx)

                if pn not in page_links:
                    fail += 1
                    if on_progress:
                        on_progress(existing_count + success + fail, total_images)
                    continue

                page_url = page_links[pn]
                img_url = extract_image_url(self._session, page_url)
                if not img_url:
                    fail += 1
                    if on_progress:
                        on_progress(existing_count + success + fail, total_images)
                    continue

                ext = ".jpg"
                if ".png" in img_url:
                    ext = ".png"
                elif ".webp" in img_url:
                    ext = ".webp"
                save_path = str(gal_dir / f"{pn:03d}{ext}")

                if download_image_direct(img_url, save_path, page_url):
                    success += 1
                else:
                    fail += 1

                if on_progress:
                    on_progress(existing_count + success + fail, total_images)

                if (idx + 1) % 20 == 0:
                    self.log.emit(
                        f"    进度：{idx + 1}/{len(missing)}"
                        f" ({success} 成功, {fail} 失败)")

                time.sleep(delay)

            self.log.emit(
                f"  下载完成：{success} 成功, {fail} 失败")
            processed = existing_count + success + fail

        # 打包
        all_files = sorted([
            f for f in gal_dir.iterdir()
            if f.is_file()
            and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
        ])

        if all_files:
            cbz_album(cbz_path, all_files)
            size_mb = cbz_path.stat().st_size / 1024 / 1024
            self.log.emit(
                f"  [打包] {cbz_path.name}"
                f" ({len(all_files)} 张, {size_mb:.1f} MB)")

            if not keep_folder:
                shutil.rmtree(gal_dir, ignore_errors=True)
                self.log.emit("  [清理] 已删除临时文件夹")
        else:
            self.log.emit("  [警告] 没有有效图片")
            if not keep_folder:
                shutil.rmtree(gal_dir, ignore_errors=True)

        return (cbz_path, processed)

    def _log(self, msg: str):
        self.log.emit(msg)


# ─── 主窗口 ────────────────────────────────────────────────

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._galleries = []
        self._gallery_map = {}
        self._worker = None
        self._downloading = False
        self._all_checked = False
        self._status_bars = {}
        self._init_ui()
        self._connect_signals()

    def _init_ui(self):
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(960, 680)
        self.resize(1100, 780)

        # ── 全局样式表 ──
        self.setStyleSheet("""
            QWidget { font-size: 13px; }
            QGroupBox {
                font-weight: bold; border: 1px solid #ccc;
                border-radius: 6px; margin-top: 8px; padding-top: 16px;
            }
            QGroupBox::title {
                subcontrol-origin: margin; subcontrol-position: top left;
                padding: 2px 8px;
            }
            QLineEdit {
                padding: 4px 6px; border: 1px solid #bbb;
                border-radius: 4px;
            }
            QLineEdit:focus { border-color: #2196F3; }
            QPushButton {
                padding: 5px 16px; border: 1px solid #bbb;
                border-radius: 4px; background: #f5f5f5;
                font-weight: normal;
            }
            QPushButton:hover { background: #e8e8e8; }
            QPushButton:pressed { background: #d0d0d0; }
            QPushButton:disabled { color: #999; background: #f0f0f0; }
            QTableWidget {
                border: 1px solid #ccc; border-radius: 4px;
                gridline-color: #eee;
                selection-background-color: #bbdefb;
                selection-color: #000;
            }
            QHeaderView::section {
                background: #fafafa; border: none;
                border-bottom: 1px solid #ddd;
                padding: 4px 6px; font-weight: bold;
            }
            QTextEdit {
                border: 1px solid #ccc; border-radius: 4px;
                background: #fafafa;
            }
            QDoubleSpinBox, QSpinBox {
                padding: 3px 4px; border: 1px solid #bbb;
                border-radius: 4px;
            }
            QCheckBox { spacing: 4px; }
            QProgressBar {
                border: 1px solid #ccc; border-radius: 4px;
                text-align: center; height: 18px;
            }
            QProgressBar::chunk { background: #4CAF50; border-radius: 3px; }
            QLabel { font-weight: 500; }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # ── 标题栏 ──
        title_bar = QHBoxLayout()
        title_bar.setContentsMargins(0, 0, 0, 0)

        title = QLabel(f"<b>{APP_NAME}</b>  —  画廊搜索与批量下载")
        title.setStyleSheet("font-size: 15px; color: #333; padding: 2px 0; font-weight: bold;")
        title_bar.addWidget(title)
        title_bar.addStretch()

        # GitHub 图标按钮
        github_btn = QPushButton()
        github_btn.setToolTip("GitHub 仓库 — 查看源代码")
        github_btn.setFixedSize(28, 28)
        github_btn.setCursor(Qt.PointingHandCursor)
        github_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #ccc; border-radius: 14px;
                background: white; padding: 0;
            }
            QPushButton:hover { background: #e8e8e8; }
        """)
        # 绘制 GitHub 风格图标
        gh_pix = QPixmap(18, 18)
        gh_pix.fill(Qt.transparent)
        gh_p = QPainter(gh_pix)
        gh_p.setRenderHint(QPainter.Antialiasing)
        gh_p.setPen(Qt.NoPen)
        gh_p.setBrush(QColor("#333"))
        gh_p.drawRoundedRect(0, 0, 18, 18, 3, 3)
        gh_p.setPen(QPen(Qt.white, 1.8))
        gh_font = QFont("Segoe UI", 9, QFont.Bold)
        gh_p.setFont(gh_font)
        gh_p.drawText(gh_pix.rect(), Qt.AlignCenter, "G")
        gh_p.end()
        github_btn.setIcon(QIcon(gh_pix))
        github_btn.setIconSize(gh_pix.size())
        github_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(
                "https://github.com/OliverArcher/ehentai-downloader-gui")))
        title_bar.addWidget(github_btn)

        main_layout.addLayout(title_bar)

        # ── 输入区 ──
        input_layout = QHBoxLayout()
        input_layout.setSpacing(6)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索关键词")
        self.search_btn = QPushButton("搜索")
        self.search_btn.setMinimumWidth(60)

        sep = QLabel("或")
        sep.setStyleSheet("color: #999; padding: 0 4px; font-weight: normal;")

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("直接输入画廊 URL")
        self.url_btn = QPushButton("下载")
        self.url_btn.setMinimumWidth(60)

        input_layout.addWidget(self.search_input, 2)
        input_layout.addWidget(self.search_btn)
        input_layout.addWidget(sep)
        input_layout.addWidget(self.url_input, 3)
        input_layout.addWidget(self.url_btn)
        main_layout.addLayout(input_layout)

        # ── 结果表格 ──
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["", "序号", "标题", "日文标题", "页数", "状态"])
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 44)
        self.table.setColumnWidth(2, 280)
        self.table.setColumnWidth(3, 200)
        self.table.setColumnWidth(4, 56)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Interactive)

        self.table.setContextMenuPolicy(Qt.CustomContextMenu)

        # 表头全选框：用真实 QCheckBox，和列表里的复选框保持同一套原生外观
        self.header_checkbox = QCheckBox(self.table.horizontalHeader())
        self.header_checkbox.setTristate(False)
        self.header_checkbox.setFocusPolicy(Qt.NoFocus)
        self.header_checkbox.stateChanged.connect(self._on_header_checkbox_changed)
        self.table.horizontalHeader().sectionResized.connect(
            lambda *_: self._position_header_checkbox())
        self.table.horizontalHeader().geometriesChanged.connect(
            self._position_header_checkbox)
        QTimer.singleShot(0, self._position_header_checkbox)

        main_layout.addWidget(self.table, 1)

        # ── 全局进度条 ──
        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)
        self.progress_label.setStyleSheet("font-weight: normal;")
        main_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m   (%p%)")
        main_layout.addWidget(self.progress_bar)

        # ── 选项区 ──
        options_group = QGroupBox("下载选项")
        options_layout = QHBoxLayout(options_group)
        options_layout.setContentsMargins(8, 16, 8, 6)
        options_layout.setSpacing(12)

        options_layout.addWidget(QLabel("延时："))
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setValue(0.3)
        self.delay_spin.setSuffix(" 秒")
        self.delay_spin.setDecimals(1)
        self.delay_spin.setFixedWidth(90)
        options_layout.addWidget(self.delay_spin)

        options_layout.addSpacing(8)

        self.overwrite_cb = QCheckBox("覆盖已有")
        options_layout.addWidget(self.overwrite_cb)

        self.keep_folder_cb = QCheckBox("保留文件夹")
        options_layout.addWidget(self.keep_folder_cb)

        options_layout.addSpacing(8)

        options_layout.addWidget(QLabel("输出："))
        self.out_path_label = QLabel(str(DEFAULT_OUT))
        self.out_path_label.setStyleSheet("color: #555; font-weight: normal;")
        options_layout.addWidget(self.out_path_label, 1)

        self.out_browse_btn = QPushButton("浏览")
        self.out_browse_btn.setMinimumWidth(76)
        self.out_browse_btn.setFixedHeight(28)
        options_layout.addWidget(self.out_browse_btn)

        options_layout.addStretch()
        main_layout.addWidget(options_group)

        # ── 操作按钮栏 ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self.dl_checked_btn = QPushButton("下载勾选")
        self.dl_checked_btn.setStyleSheet(
            "background: #1976D2; color: white; font-weight: normal;"
            " padding: 6px 20px; border: none;")

        self.list_btn = QPushButton("列出到日志")
        self.clear_btn = QPushButton("清空")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            "background: #d32f2f; color: white;"
            " padding: 6px 16px; border: none;")

        btn_layout.addWidget(self.dl_checked_btn)
        btn_layout.addWidget(self.list_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.stop_btn)
        main_layout.addLayout(btn_layout)

        # ── 日志区 ──
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(500)
        self.log_view.setStyleSheet(
            "font-family: 'Cascadia Code', Consolas, monospace;"
            " font-size: 12px; font-weight: normal;")
        self.log_view.setMinimumHeight(120)
        main_layout.addWidget(self.log_view, 0)

    def _connect_signals(self):
        self.search_btn.clicked.connect(self._on_search)
        self.search_input.returnPressed.connect(self._on_search)
        self.url_btn.clicked.connect(self._on_url_download)
        self.url_input.returnPressed.connect(self._on_url_download)
        self.dl_checked_btn.clicked.connect(self._start_download_checked)
        self.list_btn.clicked.connect(self._on_list_only)
        self.clear_btn.clicked.connect(self._on_clear)
        self.stop_btn.clicked.connect(self._on_stop)
        self.out_browse_btn.clicked.connect(self._on_browse_out)
        self.table.customContextMenuRequested.connect(self._on_table_context)

    # ── 事件处理 ──

    def _on_search(self):
        keyword = self.search_input.text().strip()
        if not keyword:
            return
        self._set_ui_busy(True)
        self._log(f"开始搜索：{keyword}")
        self.table.setRowCount(0)
        self._galleries = []
        self._gallery_map = {}

        self._worker = WorkerThread(self)
        self._worker.log.connect(self._log)
        self._worker.search_done.connect(self._on_search_done)
        self._worker.error_occurred.connect(self._on_worker_error)
        self._worker.finished.connect(
            lambda: self._set_ui_busy(False))
        self._worker.start_search(keyword)

    def _on_search_done(self, galleries: list):
        self._galleries = galleries
        self._gallery_map = {
            g["gid"]: i for i, g in enumerate(galleries)}
        self._populate_table()
        if galleries:
            self._log(
                f"共 {len(galleries)} 个画廊，在列表中勾选后点击"
                f"「下载勾选」开始下载。")

    def _on_url_download(self):
        url = self.url_input.text().strip()
        if not url:
            return
        m = re.search(r"e-hentai\.org/g/(\d+)/([^/]+)", url)
        if not m:
            QMessageBox.warning(
                self, "无效 URL", "无法识别画廊 URL，请检查格式。")
            return

        gid, token = int(m.group(1)), m.group(2)
        self._set_ui_busy(True)
        self._log(f"正在获取画廊信息：{url}")

        session = create_session()
        galleries = [{
            "gid": gid, "token": token, "url": url.rstrip("/"),
        }]
        galleries = get_gallery_metadata(
            session, galleries, log_cb=self._log)
        if not galleries or not galleries[0].get("title"):
            self._log("[错误] 无法获取画廊信息")
            self._set_ui_busy(False)
            return

        g = galleries[0]
        self._log(
            f"  标题：{g.get('title', '') or g.get('title_jpn', '')}")
        self._log(f"  页数：{g.get('pages', '?')}")

        self.table.setRowCount(0)
        self._galleries = galleries
        self._gallery_map = {galleries[0]["gid"]: 0}
        self._populate_table()
        self._set_ui_busy(False)

        # 自动勾选并下载
        self.header_checkbox.setChecked(True)
        self._start_download_checked()

    def _on_list_only(self):
        """只列出到日志，不下载"""
        if not self._galleries:
            self._log("当前没有搜索结果。")
            return
        self._log(f"\n列表：共 {len(self._galleries)} 个画廊")
        for i, g in enumerate(self._galleries, 1):
            title = (g.get("title", "")
                     or g.get("title_jpn", "")
                     or "?")
            pages = g.get("pages", "?")
            self._log(f"  {i:02d}. [{pages}页] {title}  {g.get('url','')}")

    def _on_clear(self):
        if self._downloading:
            QMessageBox.warning(self, "正在下载", "请先停止当前下载。")
            return
        self.table.setRowCount(0)
        self._galleries = []
        self._gallery_map = {}
        self._status_bars = {}
        self.log_view.clear()
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)

    def _on_stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._log("\n正在停止下载...")
            self.stop_btn.setEnabled(False)

    def _on_browse_out(self):
        dir_path = QFileDialog.getExistingDirectory(
            self, "选择输出目录",
            str(Path(self.out_path_label.text().strip())
                if self.out_path_label.text().strip()
                else DEFAULT_OUT))
        if dir_path:
            self.out_path_label.setText(dir_path)

    def _on_worker_error(self, err: str):
        self._log(f"[线程错误] {err}")
        self._set_ui_busy(False)

    def _position_header_checkbox(self):
        """把全选框放在第 0 列表头正中央"""
        header = self.table.horizontalHeader()
        x = header.sectionPosition(0)
        w = header.sectionSize(0)
        h = header.height()
        box_w = self.header_checkbox.sizeHint().width()
        box_h = self.header_checkbox.sizeHint().height()
        self.header_checkbox.setGeometry(
            x + (w - box_w) // 2,
            (h - box_h) // 2,
            box_w,
            box_h,
        )
        self.header_checkbox.raise_()

    def _on_header_checkbox_changed(self, state: int):
        """表头 checkbox → 全选/取消全选"""
        checked = state == Qt.Checked
        self._all_checked = checked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _on_table_context(self, pos):
        """右键菜单：复制链接"""
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._galleries):
            return
        gal = self._galleries[row]
        url = gal.get("url", "")
        if not url:
            url = f"https://e-hentai.org/g/{gal['gid']}/{gal['token']}/"

        menu = QMenu(self)
        copy_action = QAction("复制画廊链接", self)
        copy_action.triggered.connect(
            lambda: QApplication.clipboard().setText(url))
        menu.addAction(copy_action)
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    # ── 下载 ──

    def _start_download_checked(self):
        """下载所有勾选的画廊"""
        if not self._galleries:
            self._log("当前没有画廊数据，请先搜索或输入 URL。")
            return

        rows = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.Checked:
                rows.append(r)

        if not rows:
            self._log("请先勾选要下载的画廊（点击每行第 1 列的方框）。")
            return

        selected = []
        for r in rows:
            if 0 <= r < len(self._galleries):
                selected.append(self._galleries[r])

        self._log(f"\n准备下载 {len(selected)} 个勾选的画廊...")

        out_path = self.out_path_label.text().strip()
        out_dir = Path(out_path) if out_path else DEFAULT_OUT
        delay = self.delay_spin.value()
        overwrite = self.overwrite_cb.isChecked()
        keep_folder = self.keep_folder_cb.isChecked()

        # 计算总图片数用于全局进度
        total_images = sum(g.get("pages", 0) for g in selected)
        if total_images <= 0:
            total_images = 100  # fallback

        self._downloading = True
        self._set_ui_busy(True, downloading=True)

        self.progress_label.setVisible(True)
        self.progress_label.setText("全局进度（按图片文件数）")
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(total_images)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat(f"%v / %m 张  (%p%)")

        self._worker = WorkerThread(self)
        self._worker.log.connect(self._log)
        self._worker.gallery_progress.connect(self._on_gallery_progress)
        self._worker.total_progress.connect(self._on_total_progress)
        self._worker.gallery_done.connect(self._on_gallery_done)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.error_occurred.connect(self._on_worker_error)
        self._worker.finished.connect(
            lambda: self._set_ui_busy(False))
        self._worker.start_download(
            selected, out_dir, delay, overwrite, keep_folder)

    def _on_gallery_progress(self, gid: int, current: int, total: int):
        """更新状态列进度条"""
        idx = self._gallery_map.get(gid)
        if idx is None:
            return
        pct = int(current / max(total, 1) * 100)

        # 清掉底层文字，避免“就绪”和进度条重叠
        item = self.table.item(idx, 5)
        if item:
            item.setText("")

        pb = self._status_bars.get(gid)
        if pb is None:
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.setContentsMargins(4, 2, 4, 2)
            layout.setSpacing(0)
            pb = QProgressBar()
            pb.setMinimum(0)
            pb.setMaximum(100)
            pb.setTextVisible(True)
            pb.setFormat("%p%")
            pb.setFixedHeight(18)
            layout.addStretch(1)
            layout.addWidget(pb)
            layout.addStretch(1)
            self.table.setCellWidget(idx, 5, container)
            self._status_bars[gid] = pb
        pb.setValue(pct)

    def _on_total_progress(self, current: int, total: int):
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(current)

    def _on_gallery_done(self, gid: int, success: bool):
        idx = self._gallery_map.get(gid)
        if idx is not None:
            # 移除进度条，换回文字
            self.table.removeCellWidget(idx, 5)
            self._status_bars.pop(gid, None)
            item = QTableWidgetItem("成功" if success else "失败")
            item.setForeground(
                QColor("#4CAF50") if success else QColor("#f44336"))
            self.table.setItem(idx, 5, item)

    def _on_all_done(self, failed: int):
        self._downloading = False
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)

    # ── UI 工具 ──

    def _populate_table(self):
        self._status_bars = {}
        self.header_checkbox.blockSignals(True)
        self.header_checkbox.setChecked(False)
        self.header_checkbox.blockSignals(False)
        self._all_checked = False
        self.table.setRowCount(len(self._galleries))
        for i, g in enumerate(self._galleries):
            cb_item = QTableWidgetItem()
            cb_item.setFlags(
                Qt.ItemIsUserCheckable
                | Qt.ItemIsEnabled
                | Qt.ItemIsSelectable)
            cb_item.setCheckState(Qt.Unchecked)
            self.table.setItem(i, 0, cb_item)

            self.table.setItem(
                i, 1, QTableWidgetItem(str(i + 1)))

            # 英文标题优先展示（需求 #6）
            item_en = QTableWidgetItem(g.get("title", ""))
            item_en.setToolTip(g.get("title_jpn", ""))
            self.table.setItem(i, 2, item_en)

            item_jpn = QTableWidgetItem(g.get("title_jpn", ""))
            self.table.setItem(i, 3, item_jpn)

            self.table.setItem(
                i, 4, QTableWidgetItem(str(g.get("pages", "?"))))

            # 状态列
            status_item = QTableWidgetItem("就绪")
            status_item.setForeground(QColor("#666"))
            self.table.setItem(i, 5, status_item)

        if self._galleries:
            self.table.setRowHeight(len(self._galleries) - 1, 28)
        QTimer.singleShot(0, self._position_header_checkbox)

    def _update_table_row_status(self, row: int, text: str, color: str):
        if 0 <= row < self.table.rowCount():
            item = self.table.item(row, 5)
            if item:
                item.setData(Qt.UserRole, None)
                item.setText(text)
                item.setForeground(QColor(color))

    def _log(self, msg: str):
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(msg + "\n")
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    def _set_ui_busy(self, busy: bool, downloading: bool = False):
        has_data = bool(self._galleries)
        self.search_btn.setEnabled(not busy)
        self.search_input.setEnabled(not busy)
        self.url_btn.setEnabled(not busy)
        self.url_input.setEnabled(not busy)
        self.dl_checked_btn.setEnabled(not busy and has_data)
        self.list_btn.setEnabled(not busy and has_data)
        self.stop_btn.setEnabled(downloading)
        if not busy:
            self.stop_btn.setEnabled(False)


# ─── 入口 ──────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # 设置系统原生字体，改善渲染清晰度
    font = QFont("Microsoft YaHei UI", 9)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
