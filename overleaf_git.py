from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
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

    def get(self, url: str) -> Any:
        """Safe GET request with JSON parsing."""
        req = Request(url, headers=self.headers)
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            print(f"  [ERROR] HTTP {e.code} for {url}", file=sys.stderr)
            raise

    def updates(self, pid: str, min_count: int = 1000) -> list[dict[str, Any]]:
        """Fetch all project updates metadata."""
        url = f"https://www.overleaf.com/project/{pid}/updates?min_count={min_count}"
        data = self.get(url)
        updates: list[dict[str, Any]] = data.get("updates", [])
        updates.sort(key=lambda u: u["fromV"])
        return updates

    def diff(self, pid: str, from_v: int, to_v: int, pathname: str) -> list[dict[str, Any]] | None:
        """Fetch text diff for a specific file between two versions."""
        url = (
            f"https://www.overleaf.com/project/{pid}/diff"
            f"?from={from_v}&to={to_v}&pathname={quote(pathname)}"
        )
        try:
            data = self.get(url)
            diff: list[dict[str, Any]] = data.get("diff")
            return diff
        except Exception:
            return None

    def download(
        self, pid: str, dest: Path | str, version: int | None = None
    ) -> list[dict[str, Any]] | None:
        """Download project version as zip and extract to path with overwrite."""
        version_ = f"/version/{version}" if version else ""
        url = f"https://www.overleaf.com/project/{pid}{version_}/zip"
        request = Request(url, headers=self.headers)
        try:
            response = urlopen(request, timeout=120)
        except HTTPError as exc:
            print(f"[ERROR] Failed to download version {version}: HTTP {exc.code}")
            return None

        dest = Path(dest).resolve()
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                while chunk := response.read(DEFAULT_CHUNK_SIZE):
                    tmp.write(chunk)

            extracted_files: list[dict[str, Any]] = []
            with zipfile.ZipFile(tmp_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue

                    target = dest / info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=DEFAULT_CHUNK_SIZE)

                    extracted_files.append(
                        {
                            "filename": info.filename,
                            "size": info.file_size,
                            "date_time": info.date_time,
                        }
                    )

            return extracted_files

        except (zipfile.BadZipFile, OSError) as exc:
            print(f"[ERROR] Extraction failed: {exc}")
            return None

        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
