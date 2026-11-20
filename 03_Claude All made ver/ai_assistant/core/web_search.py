"""
웹 검색 모듈
기본: DuckDuckGo (무료, API 키 불필요)
선택: Serper API (Google 검색, 더 정확)
"""
from datetime import datetime


class WebSearcher:
    """웹 검색 및 결과 처리"""

    def __init__(self, config: dict):
        self.config = config["search"]
        self.engine = self.config.get("engine", "duckduckgo")
        self.max_results = self.config.get("max_results", 5)

    def search(self, query: str) -> str:
        """검색 후 AI에게 전달할 텍스트 반환"""
        results = []

        if self.engine == "duckduckgo":
            results = self._duckduckgo_search(query)
        elif self.engine == "serper":
            results = self._serper_search(query)

        if not results:
            return f"'{query}' 검색 결과를 찾을 수 없습니다."

        formatted = [f"[웹 검색: {query}]\n검색 날짜: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
        for i, r in enumerate(results, 1):
            formatted.append(f"{i}. **{r.get('title', '')}**\n   {r.get('snippet', '')}\n   출처: {r.get('url', '')}")

        return "\n\n".join(formatted)

    def _duckduckgo_search(self, query: str) -> list[dict]:
        try:
            from duckduckgo_search import DDGS
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=self.max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "snippet": r.get("body", ""),
                        "url": r.get("href", ""),
                    })
            return results
        except ImportError:
            raise ImportError("웹 검색을 위해 'pip install duckduckgo-search'가 필요합니다.")
        except Exception as e:
            return [{"title": "검색 오류", "snippet": str(e), "url": ""}]

    def _serper_search(self, query: str) -> list[dict]:
        import requests
        api_key = self.config.get("serper_api_key", "")
        if not api_key:
            return self._duckduckgo_search(query)

        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": self.max_results, "hl": "ko"},
            timeout=10,
        )
        data = resp.json()
        results = []
        for item in data.get("organic", []):
            results.append({
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
            })
        return results

    def build_search_prompt(self, query: str) -> str:
        search_results = self.search(query)
        return (
            f"다음은 '{query}'에 대한 최신 웹 검색 결과입니다. "
            f"이 정보를 바탕으로 사용자의 질문에 답변해주세요.\n\n{search_results}"
        )
