"""Check release/main updates and launch the shared Windows updater."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

from core import paths
from core.logger import logger
from core.version import __version__

REPOSITORY = "DopeEx/EDFlightControl"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
LATEST_RELEASE_PAGE = f"{REPOSITORY_URL}/releases/latest"
MAIN_COMMIT_API = f"https://api.github.com/repos/{REPOSITORY}/commits/main"


@dataclass(frozen=True)
class MainCommit:
    sha: str
    date: str
    message: str

    def describe(self) -> str:
        return f"{self.sha[:8]} ({self.date[:10]})\n{self.message[:100]}"


@dataclass(frozen=True)
class InstalledBuild:
    channel: str
    revision: str
    version: str
    commit_date: str = ""
    commit_message: str = ""

    def describe(self) -> str:
        if self.channel == "main":
            return MainCommit(self.revision, self.commit_date, self.commit_message).describe()
        return f"Release {self.revision}"


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Parse 'v1.2.3', 'V1.2.3' or '1.2.3' into (1, 2, 3). Returns None if it is not a plain version."""
    match = re.fullmatch(r"[vV]?(\d+)\.(\d+)\.(\d+)", text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(latest: str, current: str = __version__) -> bool:
    """True if the version 'latest' is higher than 'current'. Unparsable versions are never newer."""
    latest_parts = parse_version(latest)
    current_parts = parse_version(current)
    if latest_parts is None or current_parts is None:
        return False
    return latest_parts > current_parts


def get_latest_release(timeout: float = 5.0) -> str | None:
    """Return the tag of the latest GitHub release, or None if there is none or it cannot be reached."""
    try:
        response = requests.get(LATEST_RELEASE_API, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:
            logger.debug("Update check: no release published yet.")
            return None
        response.raise_for_status()
        payload = response.json()
        tag = payload.get("tag_name") if isinstance(payload, dict) else None
        if not isinstance(tag, str) or parse_version(tag) is None:
            raise ValueError("GitHub returned no valid release version.")
        return tag
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Update check failed: {e}")
        return None


def get_main_commit(timeout: float = 5.0) -> MainCommit | None:
    try:
        response = requests.get(MAIN_COMMIT_API, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        payload = response.json()
        sha = payload["sha"]
        date = payload["commit"]["committer"]["date"]
        message = payload["commit"]["message"]
        if not isinstance(sha, str) or re.fullmatch(r"[a-fA-F0-9]{40}", sha) is None:
            raise ValueError("Invalid main commit ID.")
        if not isinstance(date, str) or not date or not isinstance(message, str) or not message.strip():
            raise ValueError("Missing main commit details.")
        return MainCommit(sha.lower(), date, message.splitlines()[0])
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        logger.warning(f"Main update check failed: {error}")
        return None


def get_installed_build(root: Path = paths.ROOT) -> InstalledBuild | None:
    try:
        payload = json.loads((root / ".update" / "installed.json").read_text(encoding="utf-8-sig"))
        channel, revision, version = payload["channel"], payload["revision"], payload["version"]
        if channel not in ("release", "main") or not isinstance(revision, str) or version != __version__:
            raise ValueError("Installed update metadata does not match this program version.")
        if channel == "main" and re.fullmatch(r"[a-fA-F0-9]{40}", revision) is None:
            raise ValueError("Invalid installed commit ID.")
        if channel == "release" and parse_version(revision) != parse_version(version):
            raise ValueError("Invalid installed release tag.")
        date = payload.get("commitDate", "")
        message = payload.get("commitMessage", "")
        if not isinstance(date, str) or not isinstance(message, str):
            raise ValueError("Invalid installed commit details.")
        return InstalledBuild(channel, revision, version, date, message.splitlines()[0] if message else "")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as error:
        logger.warning(f"Could not read installed update metadata: {error}")
        return None


def main_update_available(commit: MainCommit | None, installed: InstalledBuild | None) -> bool:
    return commit is not None and (
        installed is None or installed.channel != "main" or installed.revision.lower() != commit.sha.lower()
    )


def launch_updater(channel: str, revision: str, root: Path = paths.ROOT) -> None:
    if channel == "release":
        valid = re.fullmatch(r"[vV]?\d+\.\d+\.\d+", revision)
    elif channel == "main":
        valid = re.fullmatch(r"[a-fA-F0-9]{40}", revision)
    else:
        valid = None
    if valid is None:
        raise ValueError("Invalid updater channel or revision.")
    if any((folder / ".git").exists() for folder in (root, *root.parents)):
        raise RuntimeError("The ZIP updater cannot update a Git working copy. Use a separate extracted installation.")
    launcher = root / "update_edfc.bat"
    if not launcher.is_file():
        raise FileNotFoundError(f"Updater launcher not found: {launcher}")
    subprocess.Popen(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", launcher.name, channel, revision],
        cwd=root,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
