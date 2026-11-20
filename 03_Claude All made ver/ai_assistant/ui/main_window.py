"""
메인 윈도우 UI - PyQt6 기반 AI 비서 인터페이스
"""
import os
import sys
import uuid
import yaml
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QSplitter,
    QListWidget, QListWidgetItem, QTabWidget, QFileDialog,
    QComboBox, QMessageBox, QDialog, QDialogButtonBox,
    QDateTimeEdit, QScrollArea, QFrame, QStatusBar, QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QDateTime, QTimer, QSize
)
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QTextCursor, QKeySequence, QShortcut

from core.llm_manager import LLMManager, load_config
from core.database import DatabaseManager
from core.file_handler import FileHandler
from core.web_search import WebSearcher
from core.email_manager import EmailManager


# ─── 스트리밍 작업자 스레드 ───────────────────────────────────
class StreamWorker(QThread):
    chunk_received = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, llm: LLMManager, message: str, system_prompt: str = None):
        super().__init__()
        self.llm = llm
        self.message = message
        self.system_prompt = system_prompt

    def run(self):
        try:
            for chunk in self.llm.chat_stream(self.message, self.system_prompt):
                self.chunk_received.emit(chunk)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


# ─── 채팅 버블 위젯 ──────────────────────────────────────────
class ChatBubble(QFrame):
    def __init__(self, role: str, content: str, timestamp: str = ""):
        super().__init__()
        self._build_ui(role, content, timestamp)

    def _build_ui(self, role: str, content: str, timestamp: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        is_user = role == "user"

        # 발신자 레이블
        header = QLabel("나" if is_user else "🤖 AI 비서")
        header.setFont(QFont("맑은 고딕", 9, QFont.Weight.Bold))
        header.setStyleSheet(f"color: {'#7CB9E8' if is_user else '#90EE90'};")
        layout.addWidget(header)

        # 메시지 내용
        content_label = QTextEdit()
        content_label.setReadOnly(True)
        content_label.setPlainText(content)
        content_label.setFrameShape(QFrame.Shape.NoFrame)
        content_label.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        doc_height = content_label.document().size().height()
        content_label.setMaximumHeight(min(int(doc_height) + 20, 400))

        bg = "#1e3a5f" if is_user else "#1e3a2f"
        content_label.setStyleSheet(
            f"background-color: {bg}; color: #e8e8e8; padding: 4px; "
            f"border-radius: 6px; font-size: 13px;"
        )
        layout.addWidget(content_label)

        if timestamp:
            ts = QLabel(timestamp)
            ts.setStyleSheet("color: #666; font-size: 10px;")
            ts.setAlignment(Qt.AlignmentFlag.AlignRight if is_user else Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(ts)

        border_color = "#2a5a9f" if is_user else "#2a5a3f"
        self.setStyleSheet(
            f"QFrame {{ background-color: transparent; border-left: 3px solid {border_color}; "
            f"margin-{'left' if not is_user else 'right'}: 0px; "
            f"margin-{'right' if not is_user else 'left'}: 40px; }}"
        )

    def update_content(self, content: str):
        te = self.findChild(QTextEdit)
        if te:
            te.setPlainText(content)
            doc_height = te.document().size().height()
            te.setMaximumHeight(min(int(doc_height) + 20, 400))


# ─── 메인 윈도우 ─────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.llm = LLMManager()
        self.db = DatabaseManager(self.config["database"]["path"])
        self.file_handler = FileHandler()
        self.searcher = WebSearcher(self.config)
        self.email_mgr = EmailManager(self.config)
        self.session_id = str(uuid.uuid4())
        self._current_ai_bubble = None
        self._current_ai_text = ""
        self._stream_worker = None

        self._setup_ui()
        self._apply_dark_theme()
        self._setup_shortcuts()

    # ── UI 구성 ──────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle("🤖 AI 개인 비서")
        cfg_ui = self.config.get("ui", {})
        self.resize(cfg_ui.get("window_width", 1200), cfg_ui.get("window_height", 800))

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 좌측 사이드바
        sidebar = self._build_sidebar()
        root_layout.addWidget(sidebar)

        # 우측 메인 탭
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.addTab(self._build_chat_tab(), "💬 대화")
        self.tabs.addTab(self._build_memo_tab(), "📝 메모")
        self.tabs.addTab(self._build_schedule_tab(), "📅 일정")
        self.tabs.addTab(self._build_email_tab(), "📧 이메일")
        root_layout.addWidget(self.tabs, 1)

        # 상태바
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._update_status()

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(200)
        sidebar.setObjectName("sidebar")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(8, 12, 8, 8)

        title = QLabel("🤖 AI 비서")
        title.setFont(QFont("맑은 고딕", 14, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # AI 제공자 선택
        layout.addWidget(QLabel("AI 엔진:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["Claude (Anthropic)", "OpenAI (GPT)", "Ollama (로컬)"])
        providers = ["claude", "openai", "ollama"]
        current = self.config["llm"]["provider"]
        if current in providers:
            self.provider_combo.setCurrentIndex(providers.index(current))
        self.provider_combo.currentIndexChanged.connect(self._on_provider_change)
        layout.addWidget(self.provider_combo)

        layout.addSpacing(10)

        # 음성 버튼
        self.mic_btn = QPushButton("🎙️ 음성 입력 시작")
        self.mic_btn.setCheckable(True)
        self.mic_btn.clicked.connect(self._toggle_voice)
        self.mic_btn.setToolTip("클릭하여 녹음 시작/중지")
        layout.addWidget(self.mic_btn)

        self.tts_btn = QPushButton("🔊 음성 응답 켜기")
        self.tts_btn.setCheckable(True)
        self.tts_btn.setChecked(False)
        layout.addWidget(self.tts_btn)

        layout.addSpacing(10)

        # 파일 첨부
        attach_btn = QPushButton("📎 파일 읽기/요약")
        attach_btn.clicked.connect(self._attach_file)
        layout.addWidget(attach_btn)

        # 웹 검색
        search_btn = QPushButton("🌐 웹 검색")
        search_btn.clicked.connect(self._web_search_dialog)
        layout.addWidget(search_btn)

        # 이메일 확인
        email_btn = QPushButton("📬 이메일 확인")
        email_btn.clicked.connect(self._check_emails)
        layout.addWidget(email_btn)

        layout.addStretch()

        # 대화 초기화
        clear_btn = QPushButton("🗑️ 대화 초기화")
        clear_btn.clicked.connect(self._clear_chat)
        layout.addWidget(clear_btn)

        return sidebar

    def _build_chat_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # 채팅 영역 (스크롤 가능)
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setSpacing(6)
        self.chat_layout.setContentsMargins(8, 8, 8, 8)
        self.chat_layout.addStretch()

        self.chat_scroll.setWidget(self.chat_container)
        layout.addWidget(self.chat_scroll)

        # 환영 메시지
        self._add_system_message("안녕하세요! 저는 당신의 AI 개인 비서입니다. 무엇이든 도와드릴게요. 😊")

        # 입력 영역
        input_frame = QWidget()
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(8, 4, 8, 8)

        self.chat_input = QTextEdit()
        self.chat_input.setMaximumHeight(80)
        self.chat_input.setPlaceholderText("메시지를 입력하세요... (Ctrl+Enter로 전송)")
        input_layout.addWidget(self.chat_input)

        send_btn = QPushButton("전송\n▶")
        send_btn.setFixedWidth(60)
        send_btn.setFixedHeight(76)
        send_btn.clicked.connect(self._send_message)
        input_layout.addWidget(send_btn)

        layout.addWidget(input_frame)
        return widget

    def _build_memo_tab(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)

        # 좌측: 메모 목록
        left = QWidget()
        left.setFixedWidth(220)
        left_layout = QVBoxLayout(left)

        left_layout.addWidget(QLabel("📝 메모 목록"))

        self.memo_search = QLineEdit()
        self.memo_search.setPlaceholderText("검색...")
        self.memo_search.textChanged.connect(self._filter_memos)
        left_layout.addWidget(self.memo_search)

        self.memo_list = QListWidget()
        self.memo_list.currentItemChanged.connect(self._on_memo_select)
        left_layout.addWidget(self.memo_list)

        new_memo_btn = QPushButton("+ 새 메모")
        new_memo_btn.clicked.connect(self._new_memo)
        left_layout.addWidget(new_memo_btn)

        layout.addWidget(left)

        # 우측: 메모 편집
        right = QWidget()
        right_layout = QVBoxLayout(right)

        self.memo_title_input = QLineEdit()
        self.memo_title_input.setPlaceholderText("제목")
        self.memo_title_input.setFont(QFont("맑은 고딕", 14, QFont.Weight.Bold))
        right_layout.addWidget(self.memo_title_input)

        self.memo_content_input = QTextEdit()
        self.memo_content_input.setPlaceholderText("내용을 입력하세요...")
        right_layout.addWidget(self.memo_content_input)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("💾 저장")
        save_btn.clicked.connect(self._save_memo)
        del_btn = QPushButton("🗑️ 삭제")
        del_btn.clicked.connect(self._delete_memo)
        ai_btn = QPushButton("🤖 AI 요약")
        ai_btn.clicked.connect(self._ai_summarize_memo)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(del_btn)
        btn_row.addWidget(ai_btn)
        right_layout.addLayout(btn_row)

        layout.addWidget(right)
        self._load_memos()
        return widget

    def _build_schedule_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        header = QHBoxLayout()
        header.addWidget(QLabel("📅 일정 관리"))
        header.addStretch()
        add_btn = QPushButton("+ 일정 추가")
        add_btn.clicked.connect(self._add_schedule_dialog)
        header.addWidget(add_btn)
        layout.addLayout(header)

        self.schedule_list = QListWidget()
        layout.addWidget(self.schedule_list)
        self._load_schedules()
        return widget

    def _build_email_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        header = QHBoxLayout()
        header.addWidget(QLabel("📧 이메일"))
        header.addStretch()
        compose_btn = QPushButton("✏️ 이메일 작성")
        compose_btn.clicked.connect(self._compose_email_dialog)
        header.addWidget(compose_btn)
        layout.addLayout(header)

        self.email_list = QListWidget()
        layout.addWidget(self.email_list)

        draft_btn = QPushButton("🤖 AI로 이메일 초안 작성")
        draft_btn.clicked.connect(self._ai_draft_email)
        layout.addWidget(draft_btn)

        return widget

    # ── 이벤트 핸들러 ─────────────────────────────────────────

    def _send_message(self):
        text = self.chat_input.toPlainText().strip()
        if not text or self._stream_worker and self._stream_worker.isRunning():
            return

        self.chat_input.clear()
        timestamp = datetime.now().strftime("%H:%M")

        # 사용자 버블 추가
        self._add_bubble("user", text, timestamp)

        # AI 응답 버블 (스트리밍)
        self._current_ai_text = ""
        self._current_ai_bubble = self._add_bubble("assistant", "...", "")

        # 컨텍스트 추가 (메모/일정)
        memo_ctx = self.db.get_memo_context()
        system = None
        if memo_ctx:
            system = (
                "당신은 사용자의 AI 개인 비서입니다. 한국어로 친절하고 정확하게 답변하세요.\n\n"
                f"사용자의 현재 데이터:\n{memo_ctx}"
            )

        self._stream_worker = StreamWorker(self.llm, text, system)
        self._stream_worker.chunk_received.connect(self._on_chunk)
        self._stream_worker.finished.connect(self._on_stream_finished)
        self._stream_worker.error.connect(self._on_stream_error)
        self._stream_worker.start()

        self.status_bar.showMessage("AI가 응답 중...")

    def _on_chunk(self, text: str):
        self._current_ai_text += text
        if self._current_ai_bubble:
            self._current_ai_bubble.update_content(self._current_ai_text)
        self._scroll_to_bottom()

    def _on_stream_finished(self):
        ts = datetime.now().strftime("%H:%M")
        if self._current_ai_bubble:
            ts_label = self._current_ai_bubble.findChildren(QLabel)
            if ts_label:
                ts_label[-1].setText(ts)

        # TTS
        if self.tts_btn.isChecked() and self._current_ai_text:
            self._speak_text(self._current_ai_text)

        self._update_status()

    def _on_stream_error(self, err: str):
        self._add_system_message(f"❌ 오류 발생: {err}")
        self._update_status()

    def _add_bubble(self, role: str, content: str, timestamp: str) -> ChatBubble:
        bubble = ChatBubble(role, content, timestamp)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        self._scroll_to_bottom()
        return bubble

    def _add_system_message(self, text: str):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #888; font-size: 12px; padding: 8px;")
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, label)

    def _scroll_to_bottom(self):
        QTimer.singleShot(50, lambda: self.chat_scroll.verticalScrollBar().setValue(
            self.chat_scroll.verticalScrollBar().maximum()
        ))

    def _clear_chat(self):
        if QMessageBox.question(self, "초기화", "대화 내역을 초기화할까요?") == QMessageBox.StandardButton.Yes:
            for i in reversed(range(self.chat_layout.count() - 1)):
                w = self.chat_layout.itemAt(i).widget()
                if w:
                    w.deleteLater()
            self.llm.clear_history()
            self.session_id = str(uuid.uuid4())
            self._add_system_message("대화가 초기화되었습니다.")

    def _on_provider_change(self, idx: int):
        providers = ["claude", "openai", "ollama"]
        self.llm.switch_provider(providers[idx])
        self._add_system_message(f"AI 엔진이 {self.provider_combo.currentText()}로 변경되었습니다.")
        self._update_status()

    def _toggle_voice(self, checked: bool):
        try:
            from core.voice import VoiceHandler
            if not hasattr(self, 'voice'):
                self.voice = VoiceHandler(self.config)
                self.voice.stt_result.connect(self._on_stt_result)
                self.voice.error_occurred.connect(lambda e: self._add_system_message(f"음성 오류: {e}"))

            if checked:
                self.mic_btn.setText("🔴 녹음 중... (클릭하여 중지)")
                self.voice.start_recording()
            else:
                self.mic_btn.setText("🎙️ 음성 입력 시작")
                self.voice.stop_recording()
        except ImportError:
            QMessageBox.warning(self, "설치 필요",
                "음성 기능을 사용하려면 다음 패키지가 필요합니다:\n"
                "pip install openai-whisper pyaudio edge-tts pygame")
            self.mic_btn.setChecked(False)

    def _on_stt_result(self, text: str):
        self.chat_input.setPlainText(text)
        self.mic_btn.setChecked(False)
        self.mic_btn.setText("🎙️ 음성 입력 시작")

    def _speak_text(self, text: str):
        if hasattr(self, 'voice'):
            self.voice.speak(text[:2000])

    def _attach_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "파일 선택", "",
            "문서 파일 (*.pdf *.docx *.txt *.xlsx *.csv *.pptx *.md);;모든 파일 (*)"
        )
        if path:
            try:
                content = self.file_handler.read_file(path)
                prompt = self.file_handler.build_summary_prompt(path, content)
                self.chat_input.setPlainText(prompt)
                self.tabs.setCurrentIndex(0)
                self._add_system_message(f"📎 파일 '{Path(path).name}' 읽기 완료. '전송'을 눌러 요약을 요청하세요.")
            except Exception as e:
                QMessageBox.critical(self, "파일 오류", str(e))

    def _web_search_dialog(self):
        query, ok = self._simple_input_dialog("🌐 웹 검색", "검색어를 입력하세요:")
        if ok and query:
            prompt = self.searcher.build_search_prompt(query)
            self.chat_input.setPlainText(prompt)
            self.tabs.setCurrentIndex(0)

    def _check_emails(self):
        try:
            emails = self.email_mgr.get_recent_emails(limit=5)
            formatted = self.email_mgr.format_email_list(emails)
            self.chat_input.setPlainText(f"내 최근 이메일을 분석해줘:\n\n{formatted}")
            self.tabs.setCurrentIndex(0)
        except Exception as e:
            QMessageBox.warning(self, "이메일 오류",
                f"이메일을 가져올 수 없습니다.\nconfig.yaml에서 이메일 설정을 확인하세요.\n\n오류: {e}")

    # ── 메모 관련 ─────────────────────────────────────────────

    def _load_memos(self):
        self.memo_list.clear()
        for m in self.db.get_memos():
            item = QListWidgetItem(m["title"])
            item.setData(Qt.ItemDataRole.UserRole, m)
            self.memo_list.addItem(item)

    def _filter_memos(self, text: str):
        self.memo_list.clear()
        for m in self.db.get_memos(text):
            item = QListWidgetItem(m["title"])
            item.setData(Qt.ItemDataRole.UserRole, m)
            self.memo_list.addItem(item)

    def _on_memo_select(self, item):
        if item:
            m = item.data(Qt.ItemDataRole.UserRole)
            self.memo_title_input.setText(m["title"])
            self.memo_content_input.setPlainText(m["content"])
            self._current_memo_id = m["id"]
        else:
            self._current_memo_id = None

    def _new_memo(self):
        self.memo_list.clearSelection()
        self.memo_title_input.clear()
        self.memo_content_input.clear()
        self._current_memo_id = None
        self.memo_title_input.setFocus()

    def _save_memo(self):
        title = self.memo_title_input.text().strip()
        content = self.memo_content_input.toPlainText()
        if not title:
            QMessageBox.warning(self, "경고", "제목을 입력하세요.")
            return
        if hasattr(self, '_current_memo_id') and self._current_memo_id:
            self.db.update_memo(self._current_memo_id, title, content)
        else:
            self.db.add_memo(title, content)
        self._load_memos()
        self.status_bar.showMessage("메모 저장됨", 2000)

    def _delete_memo(self):
        if hasattr(self, '_current_memo_id') and self._current_memo_id:
            if QMessageBox.question(self, "삭제", "이 메모를 삭제할까요?") == QMessageBox.StandardButton.Yes:
                self.db.delete_memo(self._current_memo_id)
                self._new_memo()
                self._load_memos()

    def _ai_summarize_memo(self):
        content = self.memo_content_input.toPlainText()
        if content:
            prompt = f"다음 메모를 간결하게 요약해줘:\n\n{content}"
            self.chat_input.setPlainText(prompt)
            self.tabs.setCurrentIndex(0)

    # ── 일정 관련 ─────────────────────────────────────────────

    def _load_schedules(self):
        self.schedule_list.clear()
        for s in self.db.get_schedules():
            text = f"📅 {s['start_time'][:16]}  |  {s['title']}"
            if s.get('description'):
                text += f"\n    {s['description'][:60]}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.schedule_list.addItem(item)

    def _add_schedule_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("일정 추가")
        dialog.setMinimumWidth(350)
        layout = QVBoxLayout(dialog)

        title_input = QLineEdit()
        title_input.setPlaceholderText("일정 제목")
        layout.addWidget(QLabel("제목:"))
        layout.addWidget(title_input)

        start_dt = QDateTimeEdit(QDateTime.currentDateTime())
        start_dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        layout.addWidget(QLabel("시작 시간:"))
        layout.addWidget(start_dt)

        desc_input = QLineEdit()
        desc_input.setPlaceholderText("설명 (선택)")
        layout.addWidget(QLabel("설명:"))
        layout.addWidget(desc_input)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            title = title_input.text().strip()
            if title:
                self.db.add_schedule(
                    title=title,
                    start_time=start_dt.dateTime().toString("yyyy-MM-dd HH:mm"),
                    description=desc_input.text()
                )
                self._load_schedules()

    # ── 이메일 관련 ───────────────────────────────────────────

    def _compose_email_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("이메일 작성")
        dialog.setMinimumWidth(500)
        layout = QVBoxLayout(dialog)

        to_input = QLineEdit(); to_input.setPlaceholderText("받는 사람 이메일")
        subject_input = QLineEdit(); subject_input.setPlaceholderText("제목")
        body_input = QTextEdit(); body_input.setPlaceholderText("내용...")
        body_input.setMinimumHeight(200)

        layout.addWidget(QLabel("받는 사람:")); layout.addWidget(to_input)
        layout.addWidget(QLabel("제목:")); layout.addWidget(subject_input)
        layout.addWidget(QLabel("내용:")); layout.addWidget(body_input)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("전송")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.email_mgr.send_email(to_input.text(), subject_input.text(), body_input.toPlainText())
                QMessageBox.information(self, "성공", "이메일이 전송되었습니다.")
            except Exception as e:
                QMessageBox.critical(self, "전송 실패", str(e))

    def _ai_draft_email(self):
        instructions, ok = self._simple_input_dialog("🤖 이메일 초안 작성",
            "어떤 이메일을 작성할까요?\n예: '거래처에 계약 연장 요청 이메일 작성'")
        if ok and instructions:
            prompt = self.email_mgr.build_draft_prompt(instructions)
            self.chat_input.setPlainText(prompt)
            self.tabs.setCurrentIndex(0)

    # ── 유틸리티 ──────────────────────────────────────────────

    def _simple_input_dialog(self, title: str, label: str):
        from PyQt6.QtWidgets import QInputDialog
        return QInputDialog.getText(self, title, label)

    def _update_status(self):
        provider = self.config["llm"]["provider"]
        model = self.config["llm"].get(provider, {}).get("model", "")
        self.status_bar.showMessage(f"AI: {provider.upper()} ({model})  |  세션: {self.session_id[:8]}")

    def _setup_shortcuts(self):
        # Ctrl+Enter로 메시지 전송
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut.activated.connect(self._send_message)

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1a1a2e;
                color: #e0e0e0;
            }
            #sidebar {
                background-color: #16213e;
                border-right: 1px solid #0f3460;
            }
            QTabWidget::pane {
                border: 1px solid #0f3460;
                background-color: #1a1a2e;
            }
            QTabBar::tab {
                background-color: #16213e;
                color: #aaa;
                padding: 8px 16px;
                border: 1px solid #0f3460;
            }
            QTabBar::tab:selected {
                background-color: #0f3460;
                color: #fff;
            }
            QTextEdit, QLineEdit, QListWidget {
                background-color: #16213e;
                border: 1px solid #0f3460;
                color: #e0e0e0;
                border-radius: 4px;
                padding: 4px;
            }
            QPushButton {
                background-color: #0f3460;
                color: #e0e0e0;
                border: none;
                padding: 6px 12px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #1a5276;
            }
            QPushButton:checked {
                background-color: #c0392b;
            }
            QComboBox {
                background-color: #16213e;
                border: 1px solid #0f3460;
                color: #e0e0e0;
                padding: 4px;
                border-radius: 4px;
            }
            QScrollArea {
                border: none;
            }
            QStatusBar {
                background-color: #0f3460;
                color: #aaa;
            }
            QLabel { color: #e0e0e0; }
        """)
