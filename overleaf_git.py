from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_COOKIES_PATH: Path = Path.home() / ".overleaf-git"
DEFAULT_CHUNK_SIZE: int = 65536


@dataclass(frozen=True, slots=True)
class OverleafClient:
    """
    Immutable client configuration for Overleaf API.

    Attributes:
        cookies: Raw cookie string or path to cookies file.
        user_agent: Browser User-Agent header value.
        referer: Referer header value.
        accept: Accept header value.
    """

    cookies: str
    user_agent: str = "overleaf-pull/v0.1.0 (+https://github.com/kephircheek/overleaf-pull)"
    referer: str = "https://www.overleaf.com/"
    accept: str = "application/json"

    @classmethod
    def load_cookies(cls, cookies_path: Path | str | None = None) -> OverleafClient:
        """Load cookies from file. Default `~/.overleaf-pull`"""
        cookies_path = Path(cookies_path) if cookies_path else DEFAULT_COOKIES_PATH
        if not cookies_path.exists():
            raise FileNotFoundError(f"Cookies file not found: {cookies_path}")
        cookies = cookies_path.read_text(encoding="utf-8").strip()
        return cls(cookies)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": self.accept,
            "Cookie": self.cookies,
            "Referer": self.referer,
        }

    def get_json(self, url: str) -> Any:
        """Safe GET request with JSON parsing."""
        request = Request(url, headers=self.headers)
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def updates(self, pid: str, min_count: int = 1000) -> list[dict[str, Any]]:
        """Fetch all project updates metadata."""
        url = f"https://www.overleaf.com/project/{pid}/updates?min_count={min_count}"
        data = self.get_json(url)
        updates: list[dict[str, Any]] = data.get("updates", [])
        updates.sort(key=lambda u: u["fromV"])
        return updates

    def diff(self, pid: str, from_v: int, to_v: int, pathname: str) -> list[dict[str, Any]]:
        """Fetch text diff for a specific file between two versions."""
        url = (
            f"https://www.overleaf.com/project/{pid}/diff"
            f"?from={from_v}&to={to_v}&pathname={quote(pathname)}"
        )
        data = self.get_json(url)
        diff: list[dict[str, Any]] = data.get("diff")
        return diff

    def download(self, pid: str, dest: Path | str, version: int | None = None) -> int:
        """Download project version as zip and extract to path with overwrite."""
        version_ = f"/version/{version}" if version else ""
        url = f"https://www.overleaf.com/project/{pid}{version_}/zip"
        request = Request(url, headers=self.headers)
        path = Path(dest).resolve() / f"{version or time.time_ns()}.zip"
        with urlopen(request, timeout=120) as response:
            total = 0
            with path.open("wb") as f:
                while chunk := response.read(DEFAULT_CHUNK_SIZE):
                    total += f.write(chunk)
        return total
