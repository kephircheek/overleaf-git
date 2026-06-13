from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from argparse import ArgumentParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from warnings import warn
from zipfile import ZipFile

DEFAULT_COOKIES_PATH: Path = Path.home() / ".overleaf-git"
DEFAULT_CHUNK_SIZE: int = 65536
DEFAULT_OVERLEAF_CACHE_DIR: str = ".overleaf"


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
    # user_agent: str = "overleaf-pull/v0.1.0 (+https://github.com/kephircheek/overleaf-pull)"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:151.0) Gecko/20100101 Firefox/151.0"
    )
    referer: str = "https://www.overleaf.com/"
    accept: str = "application/json"

    @classmethod
    def load_cookies(cls, cookies_path: Path | str) -> OverleafClient:
        """Load cookies from file."""
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
        """Download project version as zip."""
        path = Path(dest).resolve() / f"{version or pid}.zip"
        if version and path.exists():
            raise FileExistsError(path)
        version_ = f"/version/{version}" if version else ""
        url = f"https://www.overleaf.com/project/{pid}{version_}/zip"
        request = Request(url, headers=self.headers)
        with urlopen(request, timeout=120) as response:
            total = 0
            with path.open("wb") as f:
                while chunk := response.read(DEFAULT_CHUNK_SIZE):
                    total += f.write(chunk)
        return total


def make_diff_dir_name(update: dict[str, Any]) -> str:
    return f"{update['fromV']}-{update['toV']}"


def fetch(olc: OverleafClient, pid: str, update: dict[str, Any], path: Path) -> None:
    overleaf_dir = path / DEFAULT_OVERLEAF_CACHE_DIR
    if next((o.get("add") for o in update["project_ops"]), None):
        (overleaf_dir / "zip").mkdir(parents=True, exist_ok=True)
        try:
            olc.download(pid, overleaf_dir / "zip", update["toV"])
        except FileExistsError:
            return

    if len(update["pathnames"]) == 0:
        return

    diff_base_dir = overleaf_dir / "diff"
    diff_dir_name = make_diff_dir_name(update)
    target_dir = diff_base_dir / diff_dir_name
    if target_dir.exists():
        return

    tmp_dir = diff_base_dir / f".{diff_dir_name}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for pathname in update["pathnames"]:
        diff = olc.diff(pid, update["fromV"], update["toV"], pathname)
        (tmp_dir / f"{pathname}.json").write_text(json.dumps(diff))
    tmp_dir.rename(target_dir)


def get_update_changes(
    update: dict[str, Any],
) -> tuple[list[str], list[str], list[tuple[str, str]], list[str]]:
    modified = update["pathnames"]
    added, renamed, removed = [], [], []
    for project_op in reversed(update["project_ops"]):
        if remove_op := project_op.get("remove"):
            removed.append(remove_op["pathname"])
        elif add_op := project_op.get("add"):
            added.append(add_op["pathname"])
        elif rename_op := project_op.get("rename"):
            pathname = rename_op["pathname"]
            new_pathname = rename_op["newPathname"]
            renamed.append((pathname, new_pathname))
        else:
            warn(f"skip unknown project operation in update: {project_op}", stacklevel=2)
    return added, modified, renamed, removed


def get_target_content(diff: list[dict[str, Any]]) -> str:
    """
    Assemble the current file content from an Overleaf forward diff.

    Args:
        diff: List of diff operations from /diff endpoint.
                  Each op may contain 'u' (added text), 'd' (deleted text),
                  or 'i' (cursor/metadata marker).

    Returns:
        Reconstructed file content as a single string.
    """
    parts: list[str] = []
    for op in diff:
        if "u" in op:
            parts.append(op["u"])
        if "i" in op:
            parts.append(op["i"])
        # 'd' = text removed in the new version → skip
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class GitClient:
    path: Path = field(default_factory=Path.cwd)

    @classmethod
    def init_overleaf_project(cls, pid: str, path: str | Path | None = None) -> GitClient:
        path = Path.cwd() if path is None else Path(path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        gc = cls(path)
        gc.run(["init"])
        gc.run(["config", "--local", "overleaf.projectId", pid])
        return gc

    def run(self, cmd: list[str], check: bool = True) -> Any:
        return subprocess.run(
            ["git", *cmd], cwd=self.path, capture_output=True, text=True, check=check
        )

    def commit(self, message: str, name: str, email: str, date: str) -> Any:
        return self.run(
            [
                "commit",
                "-m",
                message,
                "--author",
                f"{name} <{email}>",
                "--date",
                date,
                "--allow-empty",
            ]
        )

    @property
    def overleaf_project_id(self) -> str:
        result = self.run(["config", "--local", "overleaf.projectId"])
        return str(result.stdout.strip())

    def has_no_changes(self) -> bool:
        result = self.run(["status", "--porcelain"], check=False)
        return str(result.stdout.strip()) == ""


def apply(update: dict[str, Any], path: Path) -> None:
    added, modified, renamed, deleted = get_update_changes(update)
    gc = GitClient(path)
    if len(deleted) > 0:
        gc.run(["rm", *deleted])
    for p, p_ in renamed:
        gc.run(["mv", p, p_])

    if (zp := (path / DEFAULT_OVERLEAF_CACHE_DIR / "zip" / f"{update['toV']}.zip")).exists():
        for pathname in added + modified:
            with (
                ZipFile(zp, "r") as zf,
                zf.open(pathname) as src,
                (path / pathname).open("wb") as dst,
            ):
                shutil.copyfileobj(src, dst, length=DEFAULT_CHUNK_SIZE)
            gc.run(["add", pathname])

    elif len(added) == 0:
        diff_dir_path = path / DEFAULT_OVERLEAF_CACHE_DIR / "diff" / make_diff_dir_name(update)
        for pathname in modified:
            if (diff_path := diff_dir_path / f"{pathname}.json").exists():
                diff: list[dict[str, Any]] = json.loads(diff_path.read_text())
                content = get_target_content(diff)
                (path / pathname).write_text(content)
                gc.run(["add", pathname])
            else:
                raise FileNotFoundError(diff_path)
    else:
        raise FileNotFoundError(zp)


def make_commit_message(update: dict[str, Any]) -> str:
    return "Initial commit" if (v := update["toV"]) == 0 else f"Update to v{v}"


def get_commit_author_and_date(update: dict[str, Any]) -> tuple[str, str, str]:
    meta = update.get("meta", {})
    users = meta.get("users", [{}])
    user = users[0] if users else {}
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "Unknown"
    email = user.get("email", name.replace(" ", ".").lower() + "@overleaf.local")
    ts_ms = meta.get("end_ts", meta.get("start_ts", 0))
    ts_sec = ts_ms / 1000 if ts_ms > 1e12 else ts_ms
    date = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts_sec)) if ts_sec else ""
    return name, email, date


def pull(olc: OverleafClient, path: Path) -> None:
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True).returncode == 0:
        raise NotImplementedError("git repository should be empty")
    gc = GitClient(path)
    pid = gc.overleaf_project_id
    updates = olc.updates(pid)
    for update in updates:
        fetch(olc, pid, update, path)
        apply(update, path)
        msg = make_commit_message(update)
        name, email, date = get_commit_author_and_date(update)
        if gc.has_no_changes():
            warn(f"empty commit: {msg}", stacklevel=2)
        gc.commit(msg, name, email, date)


def clone(olc: OverleafClient, pid: str, path: Path) -> None:
    path = (Path.cwd() / pid) if path is None else path
    GitClient.init_overleaf_project(pid, path)
    pull(olc, path)


def main() -> None:
    parser = ArgumentParser(
        description="Clone Overleaf project history into a local Git repository"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    clone_parser = subparsers.add_parser("clone", help="Clone a repository into a new directory")
    clone_parser.add_argument("pid", help="Overleaf project ID")
    clone_parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=None,
        help="The name of a new directory to clone into.",
    )
    clone_parser.add_argument(
        "--cookies",
        required=False,
        type=Path,
        default=Path.home() / ".overleaf.cookies",
        help="Path to file containing Overleaf cookies",
    )

    args = parser.parse_args()
    if args.command == "clone":
        dest = (args.directory or (Path.cwd() / args.pid)).resolve()
        olc = OverleafClient.load_cookies(args.cookies)
        clone(olc, args.pid, dest)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
