import os
import sys
import ctypes
from ctypes import wintypes
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QListWidget, QListWidgetItem, QLabel, QSystemTrayIcon,
    QMenu, QGraphicsDropShadowEffect, QFrame
)
from PySide6.QtCore import Qt, QEvent, Slot, QSize
from PySide6.QtGui import QIcon, QPainter, QColor, QPen, QPixmap, QFont, QKeySequence

from src.config import SUPPORTED_EXTENSIONS, get_default_watch_dirs
from src.database import query_similar_documents, get_monitored_dirs, save_monitored_dirs
from src.watcher import IndexingWorker, WatcherManager

# Windows API constants for hotkey
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_WIN = 0x0008
VK_F = 0x46 # Virtual key for 'F'
HOTKEY_ID = 42

def create_app_icon(color_hex="#8b5cf6"):
    """Creates a beautiful vector magnifying glass icon dynamically."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    
    # Outer glowing circle (subtle)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(139, 92, 246, 30)) # purple with low alpha
    painter.drawEllipse(4, 4, 56, 56)
    
    # Magnifying glass lens
    painter.setPen(QPen(QColor(color_hex), 5))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(16, 16, 24, 24)
    
    # Magnifying glass handle
    painter.drawLine(36, 36, 50, 50)
    painter.end()
    
    return QIcon(pixmap)

class SearchResultWidget(QWidget):
    """Custom widget for rendering a single search result with rich styling."""
    def __init__(self, item_data, parent=None):
        super().__init__(parent)
        self.item_data = item_data
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 8)
        main_layout.setSpacing(4)
        
        # Row 1: Icon, Filename, Similarity Badge
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        
        # File extension icon/badge
        ext = item_data['file_extension'].upper().replace(".", "")
        self.badge_lbl = QLabel(ext)
        self.badge_lbl.setFixedSize(36, 18)
        self.badge_lbl.setAlignment(Qt.AlignCenter)
        self.badge_lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        
        # Set badge colors based on extension
        badge_style = "border-radius: 4px; color: white;"
        if ext in ["PDF"]:
            badge_style += "background-color: #ef4444;" # Red
        elif ext in ["DOCX"]:
            badge_style += "background-color: #3b82f6;" # Blue
        elif ext in ["XLSX"]:
            badge_style += "background-color: #10b981;" # Green
        elif ext in ["TXT", "MD"]:
            badge_style += "background-color: #6b7280;" # Gray
        else:
            badge_style += "background-color: #8b5cf6;" # Purple
        self.badge_lbl.setStyleSheet(badge_style)
        row1.addWidget(self.badge_lbl)
        
        # Filename
        self.name_lbl = QLabel(item_data['file_name'])
        self.name_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.name_lbl.setStyleSheet("color: #f3f4f6;")
        row1.addWidget(self.name_lbl)
        
        row1.addStretch()
        
        # Similarity score badge
        sim_percentage = int(item_data['similarity'] * 100)
        self.sim_lbl = QLabel(f"{sim_percentage}% Match")
        self.sim_lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        
        # Score color gradient (green for high, orange/gray for lower)
        if sim_percentage >= 80:
            self.sim_lbl.setStyleSheet("color: #34d399; background: rgba(52, 211, 153, 0.1); padding: 2px 6px; border-radius: 4px;")
        elif sim_percentage >= 60:
            self.sim_lbl.setStyleSheet("color: #fbbf24; background: rgba(251, 191, 36, 0.1); padding: 2px 6px; border-radius: 4px;")
        else:
            self.sim_lbl.setStyleSheet("color: #9ca3af; background: rgba(156, 163, 175, 0.1); padding: 2px 6px; border-radius: 4px;")
            
        row1.addWidget(self.sim_lbl)
        main_layout.addLayout(row1)
        
        # Row 2: File Path
        self.path_lbl = QLabel(item_data['file_path'])
        self.path_lbl.setFont(QFont("Segoe UI", 8))
        self.path_lbl.setStyleSheet("color: #9ca3af;")
        main_layout.addWidget(self.path_lbl)
        
        # Row 3: Text Snippet (Context)
        self.snippet_lbl = QLabel()
        self.snippet_lbl.setWordWrap(True)
        self.snippet_lbl.setFont(QFont("Segoe UI", 9))
        self.snippet_lbl.setStyleSheet("color: #d1d5db; line-height: 1.4;")
        # Limit snippet size and clean up any newlines for clean listing
        metrics = self.fontMetrics()
        cleaned_snippet = item_data['text_content'].replace('\n', ' ')
        elided_text = metrics.elidedText(cleaned_snippet, Qt.ElideRight, 600)
        self.snippet_lbl.setText(elided_text)
        main_layout.addWidget(self.snippet_lbl)
        
        # Row 4: File metadata details
        meta_row = QHBoxLayout()
        size_kb = item_data['file_size'] / 1024
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        
        # Handle datetime parse
        try:
            dt = datetime.fromisoformat(item_data['last_modified'])
            date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            date_str = item_data['last_modified']
            
        self.meta_lbl = QLabel(f"Size: {size_str}   |   Modified: {date_str}")
        self.meta_lbl.setFont(QFont("Segoe UI", 8))
        self.meta_lbl.setStyleSheet("color: #6b7280;")
        meta_row.addWidget(self.meta_lbl)
        
        main_layout.addLayout(meta_row)
        
        self.setLayout(main_layout)

class SearchWindow(QMainWindow):
    """Spotlight-style search popup window."""
    def __init__(self, embedding_engine):
        super().__init__()
        self.embedding_engine = embedding_engine
        
        # Configure frameless, transparent window
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(700, 480)
        
        # Setup Core UI layouts
        self.central_widget = QWidget(self)
        self.central_widget.setObjectName("CentralWidget")
        # Sleek dark glass style
        self.central_widget.setStyleSheet("""
            QWidget#CentralWidget {
                background-color: rgba(15, 15, 18, 0.93);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 12px;
            }
        """)
        
        # Drop shadow for float appearance
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(0, 4)
        self.central_widget.setGraphicsEffect(shadow)
        
        main_layout = QVBoxLayout(self.central_widget)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)
        
        # 1. Search Bar Area
        search_layout = QHBoxLayout()
        search_layout.setSpacing(10)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search documents by context... (e.g., 지난달 마케팅 전략 리포트)")
        self.search_input.setFont(QFont("Segoe UI", 12))
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                color: #f3f4f6;
                padding: 10px 14px;
            }
            QLineEdit:focus {
                background-color: rgba(255, 255, 255, 0.09);
                border: 1px solid #8b5cf6;
            }
        """)
        self.search_input.textChanged.connect(self.on_search_text_changed)
        self.search_input.returnPressed.connect(self.open_selected_file)
        search_layout.addWidget(self.search_input)
        main_layout.addLayout(search_layout)
        
        # Separator Line
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        sep.setStyleSheet("background-color: rgba(255, 255, 255, 0.06); max-height: 1px; border: none;")
        main_layout.addWidget(sep)
        
        # 2. Results list
        self.results_list = QListWidget()
        self.results_list.setFocusPolicy(Qt.StrongFocus)
        self.results_list.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: none;
            }
            QListWidget::item {
                background-color: rgba(255, 255, 255, 0.02);
                border: 1px solid rgba(255, 255, 255, 0.04);
                border-radius: 8px;
                margin-bottom: 8px;
            }
            QListWidget::item:selected {
                background-color: rgba(139, 92, 246, 0.15);
                border: 1px solid #8b5cf6;
            }
            QListWidget::item:hover {
                background-color: rgba(255, 255, 255, 0.05);
            }
        """)
        self.results_list.itemDoubleClicked.connect(self.on_item_double_clicked)
        main_layout.addWidget(self.results_list)
        
        # 3. Status Bar Area
        status_layout = QHBoxLayout()
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setFont(QFont("Segoe UI", 9))
        self.status_lbl.setStyleSheet("color: #6b7280;")
        status_layout.addWidget(self.status_lbl)
        
        status_layout.addStretch()
        
        self.shortcut_lbl = QLabel("Win + Alt + F  |  Esc to Close")
        self.shortcut_lbl.setFont(QFont("Segoe UI", 8))
        self.shortcut_lbl.setStyleSheet("color: #4b5563;")
        status_layout.addWidget(self.shortcut_lbl)
        
        main_layout.addLayout(status_layout)
        
        self.setCentralWidget(self.central_widget)
        
        # Global Hotkey Registration
        self.register_global_hotkey()

    def register_global_hotkey(self):
        """Registers Win + Alt + F as global hotkey using Windows User32 API."""
        hwnd = int(self.winId())
        # Modifiers: MOD_ALT (0x0001) | MOD_WIN (0x0008) = 0x0009
        success = ctypes.windll.user32.RegisterHotKey(hwnd, HOTKEY_ID, MOD_ALT | MOD_WIN, VK_F)
        if not success:
            print("Warning: Failed to register Win + Alt + F hotkey. Trying Alt + F...")
            # Fallback to Alt + F (0x0001)
            success = ctypes.windll.user32.RegisterHotKey(hwnd, HOTKEY_ID, MOD_ALT, VK_F)
            if not success:
                print("Error: Failed to register fallback hotkey.")

    def nativeEvent(self, event_type, message):
        """Processes native Windows messages to capture global hotkey event."""
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.toggle_visibility()
                return True, 0
        return super().nativeEvent(event_type, message)

    def toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.center_on_screen()
            self.raise_()
            self.activateWindow()
            self.search_input.setFocus()
            self.search_input.selectAll()

    def center_on_screen(self):
        """Positions the search window in the center of the primary screen."""
        screen_geo = QApplication.primaryScreen().geometry()
        x = (screen_geo.width() - self.width()) // 2
        y = (screen_geo.height() - self.height()) // 2
        self.move(x, y)

    def changeEvent(self, event):
        """Auto-hides search window when clicking outside (loss of focus)."""
        if event.type() == QEvent.ActivationChange:
            if not self.isActiveWindow():
                self.hide()
        super().changeEvent(event)

    def keyPressEvent(self, event):
        """Handles special hotkeys like Escape, Up/Down arrow inside search window."""
        if event.key() == Qt.Key_Escape:
            self.hide()
        elif event.key() == Qt.Key_Down:
            # Shift focus from text input to list if down arrow is pressed
            if self.search_input.hasFocus() and self.results_list.count() > 0:
                self.results_list.setFocus()
                self.results_list.setCurrentRow(0)
        else:
            super().keyPressEvent(event)

    @Slot(str)
    def on_search_text_changed(self, text):
        query = text.strip()
        if not query:
            self.results_list.clear()
            return
            
        try:
            # 1. Generate embedding for query
            query_vector = self.embedding_engine.get_embedding(query)
            
            # 2. Query DB
            results = query_similar_documents(query_vector, limit=5)
            
            # 3. Update UI
            self.results_list.clear()
            for item in results:
                list_item = QListWidgetItem(self.results_list)
                list_item.setSizeHint(QSize(600, 95))
                
                widget = SearchResultWidget(item)
                self.results_list.setItemWidget(list_item, widget)
                
            # Select first item by default
            if self.results_list.count() > 0:
                self.results_list.setCurrentRow(0)
        except Exception as e:
            print(f"Search error: {e}")

    def open_selected_file(self):
        """Opens the selected file on disk using default Windows application."""
        current_item = self.results_list.currentItem()
        # Fallback to the first item if no item is explicitly selected
        if not current_item and self.results_list.count() > 0:
            current_item = self.results_list.item(0)
            
        if current_item:
            widget = self.results_list.itemWidget(current_item)
            if widget and hasattr(widget, 'item_data'):
                filepath = widget.item_data['file_path']
                try:
                    os.startfile(filepath)
                    self.hide() # Hide search popup on success
                except Exception as e:
                    print(f"Error opening file {filepath}: {e}")

    def on_item_double_clicked(self, item):
        self.open_selected_file()

    @Slot(str)
    def update_watcher_status(self, status):
        """Slot to receive indexer worker status updates."""
        self.status_lbl.setText(f"Status: {status}")

    @Slot(int, int)
    def update_watcher_progress(self, current, total):
        """Slot to receive indexer worker progress updates."""
        if total > 0:
            self.status_lbl.setText(f"Indexing: {current} / {total} files...")
        else:
            self.status_lbl.setText("Status: Ready")

    def closeEvent(self, event):
        # Unregister hotkey on cleanup
        hwnd = int(self.winId())
        ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID)
        super().closeEvent(event)


class SystemTrayApp:
    """Manages the Windows system tray and core background processes."""
    def __init__(self, app, search_window, worker, watcher):
        self.app = app
        self.search_window = search_window
        self.worker = worker
        self.watcher = watcher
        
        # System Tray Icon setup
        self.tray_icon = QSystemTrayIcon(create_app_icon(), self.app)
        self.tray_icon.setToolTip("ContextFinder - Local Semantic Search")
        
        # Connect signals
        self.worker.status_changed.connect(self.search_window.update_watcher_status)
        self.worker.progress_changed.connect(self.search_window.update_watcher_progress)
        
        # Build tray context menu
        self.menu = QMenu()
        self.search_action = self.menu.addAction("Search Documents")
        self.search_action.triggered.connect(self.search_window.toggle_visibility)
        
        self.menu.addSeparator()
        
        self.reindex_action = self.menu.addAction("Re-index Now")
        self.reindex_action.triggered.connect(self.reindex_directories)
        
        self.exit_action = self.menu.addAction("Exit")
        self.exit_action.triggered.connect(self.exit_application)
        
        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        
    def start(self):
        self.tray_icon.show()
        # Start background index worker thread
        self.worker.start()
        # Start file monitor observer
        self.watcher.start()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger: # Left click
            self.search_window.toggle_visibility()

    def reindex_directories(self):
        """Triggers manual re-scan of monitored directories."""
        self.worker.scan_and_sync()

    def exit_application(self):
        print("Stopping services and exiting...")
        # Stop background threads safely
        self.watcher.stop()
        self.worker.stop()
        self.tray_icon.hide()
        self.app.quit()
        sys.exit(0)
