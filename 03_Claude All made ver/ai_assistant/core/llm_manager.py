"""
LLM 연결 관리자 - Claude API / OpenAI API / Ollama 로컬 모델 지원
"""
import yaml
import os
from typing import Generator, Optional
import anthropic
import openai
import requests


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class LLMManager:
    """Claude, OpenAI, Ollama를 하나의 인터페이스로 관리"""

    def __init__(self):
        self.config = load_config()
        self.provider = self.config["llm"]["provider"]
        self.conversation_history = []
        self._init_client()

    def _init_client(self):
        """선택된 제공자에 따라 클라이언트 초기화"""
        cfg = self.config["llm"]

        if self.provider == "claude":
            self.client = anthropic.Anthropic(api_key=cfg["claude"]["api_key"])
            self.model = cfg["claude"]["model"]

        elif self.provider == "openai":
            self.client = openai.OpenAI(api_key=cfg["openai"]["api_key"])
            self.model = cfg["openai"]["model"]

        elif self.provider == "ollama":
            self.ollama_url = cfg["ollama"]["base_url"]
            self.model = cfg["ollama"]["model"]
            self.client = None

    def switch_provider(self, provider: str):
        """런타임 중 AI 제공자 전환"""
        self.provider = provider
        self.config["llm"]["provider"] = provider
        self._init_client()
        self.clear_history()

    def add_to_history(self, role: str, content: str):
        self.conversation_history.append({"role": role, "content": content})

    def clear_history(self):
        self.conversation_history = []

    def chat(self, message: str, system_prompt: str = None) -> str:
        """단순 응답 (스트리밍 없음)"""
        return "".join(self.chat_stream(message, system_prompt))

    def chat_stream(self, message: str, system_prompt: str = None) -> Generator[str, None, None]:
        """스트리밍 응답 생성기"""
        if system_prompt is None:
            system_prompt = (
                "당신은 사용자의 개인 AI 비서입니다. "
                "한국어로 친절하고 정확하게 답변하세요. "
                "파일 요약, 일정 관리, 이메일 작성, 웹 검색 등을 도와드릴 수 있습니다."
            )

        self.add_to_history("user", message)

        if self.provider == "claude":
            yield from self._claude_stream(system_prompt)
        elif self.provider == "openai":
            yield from self._openai_stream(system_prompt)
        elif self.provider == "ollama":
            yield from self._ollama_stream(system_prompt)

    def _claude_stream(self, system_prompt: str) -> Generator[str, None, None]:
        full_response = ""
        with self.client.messages.stream(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            messages=self.conversation_history,
        ) as stream:
            for text in stream.text_stream:
                full_response += text
                yield text
        self.add_to_history("assistant", full_response)

    def _openai_stream(self, system_prompt: str) -> Generator[str, None, None]:
        messages = [{"role": "system", "content": system_prompt}] + self.conversation_history
        full_response = ""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            full_response += text
            yield text
        self.add_to_history("assistant", full_response)

    def _ollama_stream(self, system_prompt: str) -> Generator[str, None, None]:
        import json
        messages = [{"role": "system", "content": system_prompt}] + self.conversation_history
        full_response = ""
        response = requests.post(
            f"{self.ollama_url}/api/chat",
            json={"model": self.model, "messages": messages, "stream": True},
            stream=True,
            timeout=120,
        )
        for line in response.iter_lines():
            if line:
                data = json.loads(line)
                text = data.get("message", {}).get("content", "")
                full_response += text
                yield text
                if data.get("done"):
                    break
        self.add_to_history("assistant", full_response)

    def get_available_ollama_models(self) -> list[str]:
        """설치된 Ollama 모델 목록 반환"""
        try:
            cfg = self.config["llm"]["ollama"]
            resp = requests.get(f"{cfg['base_url']}/api/tags", timeout=5)
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return []
