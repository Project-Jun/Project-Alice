"""
AI 개인 비서 앱 - 메인 진입점
"""
import sys
import os
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from ui.main_window import MainWindow

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AI 개인 비서")
    app.setOrganizationName("MyAIAssistant")
    
    # HiDPI 지원
    app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
