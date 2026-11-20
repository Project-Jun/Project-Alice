"""
음성 입출력 모듈
STT: OpenAI Whisper (로컬)
TTS: Microsoft Edge TTS (무료, 고품질)
"""
import asyncio
import threading
import tempfile
import os
import queue
from PyQt6.QtCore import QObject, pyqtSignal


class VoiceHandler(QObject):
    """음성 녹음, STT, TTS 처리"""
    
    stt_result = pyqtSignal(str)       # 텍스트 변환 완료
    tts_started = pyqtSignal()         # TTS 시작
    tts_finished = pyqtSignal()        # TTS 완료
    error_occurred = pyqtSignal(str)   # 오류 발생

    def __init__(self, config: dict):
        super().__init__()
        self.config = config["voice"]
        self.is_recording = False
        self.is_speaking = False
        self._whisper_model = None
        self._tts_voice = self.config.get("tts_voice", "ko-KR-SunHiNeural")

    def _load_whisper(self):
        """Whisper 모델 지연 로딩 (첫 사용 시)"""
        if self._whisper_model is None:
            import whisper
            model_size = self.config.get("whisper_model", "base")
            print(f"Whisper '{model_size}' 모델 로딩 중...")
            self._whisper_model = whisper.load_model(model_size)
        return self._whisper_model

    def start_recording(self):
        """마이크 녹음 시작 (별도 스레드)"""
        self.is_recording = True
        threading.Thread(target=self._record_audio, daemon=True).start()

    def stop_recording(self):
        """녹음 중지 후 STT 변환"""
        self.is_recording = False

    def _record_audio(self):
        """PyAudio로 마이크 녹음"""
        try:
            import pyaudio
            import wave

            CHUNK = 1024
            FORMAT = pyaudio.paInt16
            CHANNELS = 1
            RATE = 16000

            p = pyaudio.PyAudio()
            stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                           input=True, frames_per_buffer=CHUNK)

            frames = []
            while self.is_recording:
                data = stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)

            stream.stop_stream()
            stream.close()
            p.terminate()

            # 임시 WAV 파일 저장
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name

            with wave.open(tmp_path, 'wb') as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(p.get_sample_size(FORMAT))
                wf.setframerate(RATE)
                wf.writeframes(b''.join(frames))

            # Whisper STT
            self._transcribe(tmp_path)
            os.unlink(tmp_path)

        except Exception as e:
            self.error_occurred.emit(f"녹음 오류: {e}")

    def _transcribe(self, audio_path: str):
        """Whisper로 음성→텍스트 변환"""
        try:
            model = self._load_whisper()
            result = model.transcribe(audio_path, language="ko")
            text = result["text"].strip()
            if text:
                self.stt_result.emit(text)
        except Exception as e:
            self.error_occurred.emit(f"STT 오류: {e}")

    def speak(self, text: str):
        """텍스트를 음성으로 변환 (별도 스레드)"""
        if self.is_speaking:
            return
        threading.Thread(target=self._speak_thread, args=(text,), daemon=True).start()

    def _speak_thread(self, text: str):
        """Edge TTS로 TTS 실행"""
        try:
            self.is_speaking = True
            self.tts_started.emit()
            asyncio.run(self._edge_tts(text))
        except Exception as e:
            self.error_occurred.emit(f"TTS 오류: {e}")
        finally:
            self.is_speaking = False
            self.tts_finished.emit()

    async def _edge_tts(self, text: str):
        """Microsoft Edge TTS (무료, 고품질 한국어 지원)"""
        import edge_tts
        import pygame

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path = f.name

        communicate = edge_tts.Communicate(text, self._tts_voice)
        await communicate.save(tmp_path)

        # pygame으로 재생
        pygame.mixer.init()
        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            await asyncio.sleep(0.1)
        pygame.mixer.quit()
        os.unlink(tmp_path)

    def stop_speaking(self):
        """TTS 중지"""
        try:
            import pygame
            pygame.mixer.music.stop()
        except Exception:
            pass
        self.is_speaking = False
