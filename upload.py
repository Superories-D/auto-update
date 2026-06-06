#!/usr/bin/env python3
"""ESE song auto-updater.

This CLI can pull the ESE repository, upload only songs that are not present in
uploaded2.json, persist successful uploads, and send a Resend email summary.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import uuid
from typing import Iterable
from urllib.parse import urljoin

import requests


KNOWN_TYPES = {
    "01 Pop",
    "02 Anime",
    "03 Vocaloid",
    "04 Children and Folk",
    "05 Variety",
    "06 Classical",
    "07 Game Music",
    "08 Live Festival Mode",
    "09 Namco Original",
    "10 Taiko Towers",
    "11 Dan Dojo",
}

COMMANDS = {"run", "upload", "pull", "scan", "status", "email-test"}
DEFAULT_AUDIO_EXTENSIONS = (".ogg",)
RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclasses.dataclass(frozen=True)
class Song:
    key: str
    song_type: str
    name: str
    path: pathlib.Path
    tja_path: pathlib.Path | None
    music_path: pathlib.Path | None


@dataclasses.dataclass
class Config:
    script_dir: pathlib.Path
    env_file: pathlib.Path
    ese_dir: pathlib.Path
    site_url: str
    state_file: pathlib.Path
    audio_extensions: tuple[str, ...]
    upload_attempts: int
    retry_wait_seconds: int
    git_pull_args: list[str]
    git_proxy_url: str
    resend_api_key: str
    resend_from: str
    resend_to: list[str]
    email_subject_prefix: str
    email_max_songs: int


@dataclasses.dataclass
class GitResult:
    attempted: bool
    ok: bool
    changed: bool
    before: str | None
    after: str | None
    message: str


@dataclasses.dataclass
class UploadResult:
    total_songs: int
    uploaded_state_count_before: int
    uploaded_state_count_after: int
    candidates: list[str]
    uploaded: list[str]
    failed: dict[str, str]
    skipped_missing_assets: dict[str, str]
    dry_run: bool
    fatal_error: str | None = None


@dataclasses.dataclass
class RunResult:
    started_at: dt.datetime
    finished_at: dt.datetime
    ese_dir: pathlib.Path
    git: GitResult
    upload: UploadResult
    email_sent: bool
    email_message: str


def print_info(message: str) -> None:
    print(message, flush=True)


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = value.strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        print_info(f"Invalid integer {value!r}; using {default}.")
        return default


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_env_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key] = value
    return values


def env_get(env_file_values: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key, env_file_values.get(key, default))


def _get_basedir(script_dir: pathlib.Path) -> str:
    try:
        sys.path.insert(0, str(script_dir / "taiko-web2"))
        import config  # type: ignore

        base = getattr(config, "BASEDIR", "/")
        if not isinstance(base, str):
            return "/"
        return base if base.endswith("/") else f"{base}/"
    except Exception:
        return "/"


def build_endpoint(script_dir: pathlib.Path, site_url: str, endpoint: str) -> str:
    if not site_url:
        basedir = _get_basedir(script_dir)
        return f"http://127.0.0.1{basedir}api/{endpoint}"

    base = site_url.strip()
    if not base.lower().startswith(("http://", "https://")):
        base = f"http://{base}"
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, f"api/{endpoint}")


def classify_name(name: str) -> int:
    if not name:
        return 2
    char = name[0]
    if "0" <= char <= "9":
        return 0
    if "A" <= char <= "Z" or "a" <= char <= "z":
        return 1
    return 2


def is_valid_type_dir(name: str) -> bool:
    return name in KNOWN_TYPES or bool(re.match(r"^\d{2}\s", name))


def looks_like_ese_dir(path: pathlib.Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    if (path / ".git").exists():
        return True
    return any(child.is_dir() and is_valid_type_dir(child.name) for child in path.iterdir())


def detect_ese_dir(script_dir: pathlib.Path, requested: str | None) -> pathlib.Path:
    if requested:
        return pathlib.Path(requested).expanduser().resolve()

    candidates = [
        script_dir.parent / "ESE",
        script_dir / "ESE",
        pathlib.Path.home() / "ESE",
    ]
    if os.name == "nt":
        candidates.append(pathlib.Path("D:/ESE"))

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if looks_like_ese_dir(resolved):
            return resolved
    return (script_dir.parent / "ESE").resolve()


def find_first_with_ext(directory: pathlib.Path, extensions: Iterable[str]) -> pathlib.Path | None:
    normalized = tuple(ext.lower() for ext in extensions)
    matches = [
        entry
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix.lower() in normalized
    ]
    matches.sort(key=lambda item: item.name.lower())
    return matches[0] if matches else None


def discover_songs(ese_dir: pathlib.Path, audio_extensions: tuple[str, ...]) -> list[Song]:
    if not looks_like_ese_dir(ese_dir):
        raise FileNotFoundError(f"ESE directory not found or invalid: {ese_dir}")

    type_dirs = [
        item
        for item in ese_dir.iterdir()
        if item.is_dir() and not item.name.startswith(".") and is_valid_type_dir(item.name)
    ]
    type_dirs.sort(key=lambda item: item.name)

    songs: list[Song] = []
    for type_dir in type_dirs:
        song_dirs = [item for item in type_dir.iterdir() if item.is_dir()]
        song_dirs.sort(key=lambda item: (classify_name(item.name), item.name.lower()))
        for song_dir in song_dirs:
            tja_path = find_first_with_ext(song_dir, (".tja",))
            music_path = find_first_with_ext(song_dir, audio_extensions)
            songs.append(
                Song(
                    key=f"{type_dir.name}/{song_dir.name}",
                    song_type=type_dir.name,
                    name=song_dir.name,
                    path=song_dir,
                    tja_path=tja_path,
                    music_path=music_path,
                )
            )
    return songs


def load_uploaded_set(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("uploaded", [])
    if not isinstance(items, list):
        raise ValueError(f"{path} must contain an 'uploaded' list")
    return {str(item) for item in items}


def save_uploaded_set(path: pathlib.Path, uploaded: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    payload = {"uploaded": sorted(uploaded)}
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def upload_song(
    config: Config,
    session: requests.Session,
    upload_url: str,
    song: Song,
) -> tuple[bool, str]:
    if song.tja_path is None:
        return False, "missing_tja"
    if song.music_path is None:
        return False, "missing_audio"

    for attempt in range(1, config.upload_attempts + 1):
        try:
            with song.tja_path.open("rb") as tja_file, song.music_path.open("rb") as music_file:
                files = {
                    "file_tja": ("main.tja", tja_file, "text/plain"),
                    "file_music": ("music.ogg", music_file, "audio/ogg"),
                }
                response = session.post(
                    upload_url,
                    files=files,
                    data={"song_type": song.song_type},
                    timeout=60,
                )
            if response.status_code != 200:
                return False, f"http_status_{response.status_code}: {response.text[:300]}"
            try:
                data = response.json()
            except ValueError:
                return False, f"invalid_json: {response.text[:300]}"
            if data.get("success") is True:
                return True, "ok"
            return False, str(data.get("error") or data)
        except (
            requests.exceptions.ProxyError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            if attempt >= config.upload_attempts:
                return False, f"network_error: {exc}"
            print_info(
                f"Network error while uploading {song.key}; retry "
                f"{attempt}/{config.upload_attempts} in {config.retry_wait_seconds}s."
            )
            time.sleep(config.retry_wait_seconds)
        except Exception as exc:
            return False, f"error: {exc}"
    return False, "unknown_error"


def git_env(config: Config) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "SSH_ASKPASS": "true",
        }
    )
    if config.git_proxy_url:
        env.update(
            {
                "ALL_PROXY": config.git_proxy_url,
                "HTTPS_PROXY": config.git_proxy_url,
                "HTTP_PROXY": config.git_proxy_url,
                "all_proxy": config.git_proxy_url,
                "https_proxy": config.git_proxy_url,
                "http_proxy": config.git_proxy_url,
            }
        )
    return env


def git_head(config: Config) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=config.ese_dir,
        env=git_env(config),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def pull_repository(config: Config) -> GitResult:
    if not (config.ese_dir / ".git").exists():
        return GitResult(False, True, False, None, None, "ESE is not a git repository; pull skipped.")

    try:
        before = git_head(config)
    except Exception:
        before = None

    command = ["git"]
    if config.git_proxy_url:
        command.extend(["-c", f"http.proxy={config.git_proxy_url}"])
    command.extend(["pull", *config.git_pull_args])
    try:
        result = subprocess.run(
            command,
            cwd=config.ese_dir,
            env=git_env(config),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except FileNotFoundError:
        return GitResult(True, False, False, before, before, "git command not found.")
    except Exception as exc:
        return GitResult(True, False, False, before, before, f"git pull error: {exc}")

    try:
        after = git_head(config)
    except Exception:
        after = None
    output = result.stdout.strip()
    return GitResult(
        attempted=True,
        ok=result.returncode == 0,
        changed=bool(before and after and before != after),
        before=before,
        after=after,
        message=output or ("Already up to date." if result.returncode == 0 else "git pull failed."),
    )


def upload_new_songs(config: Config, dry_run: bool = False) -> UploadResult:
    songs = discover_songs(config.ese_dir, config.audio_extensions)
    uploaded_set = load_uploaded_set(config.state_file)
    before_count = len(uploaded_set)
    candidates = [song for song in songs if song.key not in uploaded_set]
    uploaded: list[str] = []
    failed: dict[str, str] = {}
    skipped: dict[str, str] = {}

    if not candidates:
        return UploadResult(
            total_songs=len(songs),
            uploaded_state_count_before=before_count,
            uploaded_state_count_after=before_count,
            candidates=[],
            uploaded=[],
            failed={},
            skipped_missing_assets={},
            dry_run=dry_run,
        )

    upload_url = build_endpoint(config.script_dir, config.site_url, "upload")
    with requests.Session() as session:
        session.trust_env = False
        for song in candidates:
            if song.tja_path is None or song.music_path is None:
                missing = []
                if song.tja_path is None:
                    missing.append("TJA")
                if song.music_path is None:
                    missing.append("audio")
                skipped[song.key] = f"missing {' and '.join(missing)}"
                print_info(f"Skipped {song.key}: {skipped[song.key]}")
                continue

            if dry_run:
                print_info(f"Would upload {song.key}")
                continue

            ok, message = upload_song(config, session, upload_url, song)
            if ok:
                uploaded_set.add(song.key)
                uploaded.append(song.key)
                save_uploaded_set(config.state_file, uploaded_set)
                print_info(f"Uploaded {song.key}")
            else:
                failed[song.key] = message
                print_info(f"Failed {song.key}: {message}")

    if dry_run:
        after_count = before_count
    else:
        after_count = len(uploaded_set)

    return UploadResult(
        total_songs=len(songs),
        uploaded_state_count_before=before_count,
        uploaded_state_count_after=after_count,
        candidates=[song.key for song in candidates],
        uploaded=uploaded,
        failed=failed,
        skipped_missing_assets=skipped,
        dry_run=dry_run,
    )


def upload_error_result(message: str, dry_run: bool = False) -> UploadResult:
    return UploadResult(
        total_songs=0,
        uploaded_state_count_before=0,
        uploaded_state_count_after=0,
        candidates=[],
        uploaded=[],
        failed={},
        skipped_missing_assets={},
        dry_run=dry_run,
        fatal_error=message,
    )


def fetch_server_songs(config: Config) -> set[str]:
    songs_url = build_endpoint(config.script_dir, config.site_url, "songs")
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(songs_url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("songs", payload.get("data", []))
    if not isinstance(payload, list):
        raise ValueError("Server songs API did not return a list")

    server_songs: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        category = item.get("category") or item.get("song_type") or item.get("type")
        title = item.get("title") or item.get("name")
        if category and title:
            server_songs.add(f"{category}/{title}")
    return server_songs


def send_resend_email(
    config: Config,
    subject: str,
    text_body: str,
    html_body: str,
) -> tuple[bool, str]:
    if not config.resend_api_key or not config.resend_from or not config.resend_to:
        return False, "Resend email skipped; RESEND_API_KEY, RESEND_FROM, or RESEND_TO is not configured."

    payload = {
        "from": config.resend_from,
        "to": config.resend_to,
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    headers = {
        "Authorization": f"Bearer {config.resend_api_key}",
        "Content-Type": "application/json",
        "User-Agent": "ese-auto-updater/1.0",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    response = requests.post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=30)
    if 200 <= response.status_code < 300:
        try:
            email_id = response.json().get("id", "")
        except ValueError:
            email_id = ""
        return True, f"Email sent via Resend{f' ({email_id})' if email_id else ''}."
    return False, f"Resend error HTTP {response.status_code}: {response.text[:500]}"


def format_song_list(items: list[str], max_items: int) -> tuple[str, str]:
    if not items:
        return "None", "<p>None</p>"
    shown = items[:max_items]
    more = len(items) - len(shown)
    text_lines = "\n".join(f"- {item}" for item in shown)
    html_items = "".join(f"<li>{html.escape(item)}</li>" for item in shown)
    if more > 0:
        text_lines += f"\n- ... and {more} more"
        html_items += f"<li>... and {more} more</li>"
    return text_lines, f"<ul>{html_items}</ul>"


def build_email(config: Config, result: RunResult) -> tuple[str, str, str]:
    upload = result.upload
    status = "OK" if result.git.ok and not upload.failed and not upload.fatal_error else "Needs attention"
    subject = (
        f"{config.email_subject_prefix} {status}: "
        f"{len(upload.uploaded)} new song(s), {upload.total_songs} total"
    )

    uploaded_text, uploaded_html = format_song_list(upload.uploaded, config.email_max_songs)
    failed_text, failed_html = format_song_list(list(upload.failed), config.email_max_songs)
    skipped_text, skipped_html = format_song_list(
        list(upload.skipped_missing_assets), config.email_max_songs
    )

    duration = result.finished_at - result.started_at
    pull_status = "skipped"
    if result.git.attempted:
        pull_status = "ok" if result.git.ok else "failed"

    text_body = f"""ESE update summary

Started: {result.started_at.isoformat(timespec="seconds")}
Finished: {result.finished_at.isoformat(timespec="seconds")}
Duration: {duration}
ESE directory: {result.ese_dir}

Git pull: {pull_status}
Git before: {result.git.before or "-"}
Git after: {result.git.after or "-"}
Git message:
{result.git.message}

New candidates: {len(upload.candidates)}
Uploaded successfully: {len(upload.uploaded)}
Upload failures: {len(upload.failed)}
Skipped missing assets: {len(upload.skipped_missing_assets)}
Current local song count: {upload.total_songs}
Recorded uploaded count: {upload.uploaded_state_count_after}
Fatal upload error: {upload.fatal_error or "-"}

Uploaded songs:
{uploaded_text}

Failed songs:
{failed_text}

Skipped songs:
{skipped_text}
"""

    html_body = f"""
<h2>ESE update summary</h2>
<table>
  <tr><td>Started</td><td>{html.escape(result.started_at.isoformat(timespec="seconds"))}</td></tr>
  <tr><td>Finished</td><td>{html.escape(result.finished_at.isoformat(timespec="seconds"))}</td></tr>
  <tr><td>Duration</td><td>{html.escape(str(duration))}</td></tr>
  <tr><td>ESE directory</td><td>{html.escape(str(result.ese_dir))}</td></tr>
  <tr><td>Git pull</td><td>{html.escape(pull_status)}</td></tr>
  <tr><td>Git before</td><td>{html.escape(result.git.before or "-")}</td></tr>
  <tr><td>Git after</td><td>{html.escape(result.git.after or "-")}</td></tr>
  <tr><td>New candidates</td><td>{len(upload.candidates)}</td></tr>
  <tr><td>Uploaded successfully</td><td>{len(upload.uploaded)}</td></tr>
  <tr><td>Upload failures</td><td>{len(upload.failed)}</td></tr>
  <tr><td>Skipped missing assets</td><td>{len(upload.skipped_missing_assets)}</td></tr>
  <tr><td>Current local song count</td><td>{upload.total_songs}</td></tr>
  <tr><td>Recorded uploaded count</td><td>{upload.uploaded_state_count_after}</td></tr>
  <tr><td>Fatal upload error</td><td>{html.escape(upload.fatal_error or "-")}</td></tr>
</table>
<h3>Git message</h3>
<pre>{html.escape(result.git.message)}</pre>
<h3>Uploaded songs</h3>
{uploaded_html}
<h3>Failed songs</h3>
{failed_html}
<h3>Skipped songs</h3>
{skipped_html}
"""
    return subject, text_body, html_body


def run_daily(config: Config, no_pull: bool = False, no_email: bool = False, dry_run: bool = False) -> RunResult:
    started = dt.datetime.now().astimezone()
    git_result = GitResult(False, True, False, None, None, "Pull disabled.")
    if not no_pull:
        print_info(f"Pulling ESE repository: {config.ese_dir}")
        try:
            git_result = pull_repository(config)
        except Exception as exc:
            git_result = GitResult(True, False, False, None, None, f"git pull error: {exc}")
        print_info(git_result.message)

    try:
        upload_result = upload_new_songs(config, dry_run=dry_run)
    except Exception as exc:
        upload_result = upload_error_result(f"upload workflow error: {exc}", dry_run=dry_run)
        print_info(upload_result.fatal_error)

    finished = dt.datetime.now().astimezone()
    result = RunResult(
        started_at=started,
        finished_at=finished,
        ese_dir=config.ese_dir,
        git=git_result,
        upload=upload_result,
        email_sent=False,
        email_message="Email disabled.",
    )

    if not no_email:
        try:
            subject, text_body, html_body = build_email(config, result)
            sent, message = send_resend_email(config, subject, text_body, html_body)
            result.email_sent = sent
            result.email_message = message
            print_info(message)
        except Exception as exc:
            result.email_sent = False
            result.email_message = f"email workflow error: {exc}"
            print_info(result.email_message)
    return result


def build_config(args: argparse.Namespace) -> Config:
    script_dir = pathlib.Path(__file__).resolve().parent
    env_file_arg = getattr(args, "env_file", ".env")
    env_file = pathlib.Path(env_file_arg)
    if not env_file.is_absolute():
        env_file = script_dir / env_file
    env_values = load_env_file(env_file)

    requested_ese = getattr(args, "ese_dir", None) or env_get(env_values, "ESE_DIR")
    ese_dir = detect_ese_dir(script_dir, requested_ese or None)

    state_file_text = getattr(args, "state_file", None) or env_get(
        env_values, "STATE_FILE", "uploaded2.json"
    )
    state_file = pathlib.Path(state_file_text)
    if not state_file.is_absolute():
        state_file = script_dir / state_file

    audio_extensions = split_csv(env_get(env_values, "AUDIO_EXTENSIONS", ".ogg"))
    if not audio_extensions:
        audio_extensions = list(DEFAULT_AUDIO_EXTENSIONS)
    audio_extensions = [ext if ext.startswith(".") else f".{ext}" for ext in audio_extensions]

    pull_args_text = env_get(env_values, "GIT_PULL_ARGS", "--ff-only")
    git_pull_args = shlex.split(pull_args_text)

    resend_to = split_csv(env_get(env_values, "RESEND_TO"))
    return Config(
        script_dir=script_dir,
        env_file=env_file,
        ese_dir=ese_dir,
        site_url=getattr(args, "site_url", None) or env_get(env_values, "SITE_URL", ""),
        state_file=state_file,
        audio_extensions=tuple(audio_extensions),
        upload_attempts=parse_int(env_get(env_values, "UPLOAD_ATTEMPTS", "3"), 3),
        retry_wait_seconds=parse_int(env_get(env_values, "RETRY_WAIT_SECONDS", "10"), 10),
        git_pull_args=git_pull_args,
        git_proxy_url=env_get(env_values, "GIT_PROXY_URL", "socks5h://127.0.0.1:40000"),
        resend_api_key=env_get(env_values, "RESEND_API_KEY"),
        resend_from=env_get(env_values, "RESEND_FROM"),
        resend_to=resend_to,
        email_subject_prefix=env_get(env_values, "EMAIL_SUBJECT_PREFIX", "[ESE Update]"),
        email_max_songs=parse_int(env_get(env_values, "EMAIL_MAX_SONGS", "200"), 200),
    )


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", default=".env", help="Path to .env config file.")
    parser.add_argument("--ese-dir", help="Path to the ESE repository.")
    parser.add_argument("--site-url", help="Taiko site base URL. Defaults to local config/127.0.0.1.")
    parser.add_argument("--state-file", help="Uploaded JSON state file. Defaults to uploaded2.json.")
    parser.add_argument("--use-proxy", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-proxy", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--proxy-url", help=argparse.SUPPRESS)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto pull and upload new ESE songs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Pull, upload new songs, and email a summary.")
    add_common_options(run_parser)
    run_parser.add_argument("--no-pull", action="store_true", help="Skip git pull.")
    run_parser.add_argument("--no-email", action="store_true", help="Skip Resend email.")
    run_parser.add_argument("--dry-run", action="store_true", help="Show uploads without sending files.")

    upload_parser = subparsers.add_parser("upload", help="Upload only songs absent from uploaded2.json.")
    add_common_options(upload_parser)
    upload_parser.add_argument("--dry-run", action="store_true", help="Show uploads without sending files.")

    pull_parser = subparsers.add_parser("pull", help="Run git pull in the ESE repository.")
    add_common_options(pull_parser)

    scan_parser = subparsers.add_parser("scan", help="List candidate new songs.")
    add_common_options(scan_parser)
    scan_parser.add_argument(
        "--server",
        action="store_true",
        help="Compare local songs with the site api/songs endpoint instead of uploaded2.json.",
    )

    status_parser = subparsers.add_parser("status", help="Show ESE and uploaded state counts.")
    add_common_options(status_parser)

    email_parser = subparsers.add_parser("email-test", help="Send a Resend test email.")
    add_common_options(email_parser)
    return parser


def command_run(args: argparse.Namespace) -> int:
    config = build_config(args)
    result = run_daily(
        config,
        no_pull=args.no_pull,
        no_email=args.no_email,
        dry_run=args.dry_run,
    )
    upload = result.upload
    print_info(
        "Done: "
        f"{len(upload.uploaded)} uploaded, {len(upload.failed)} failed, "
        f"{len(upload.skipped_missing_assets)} skipped, {upload.total_songs} total songs."
    )
    return 0


def command_upload(args: argparse.Namespace) -> int:
    config = build_config(args)
    result = upload_new_songs(config, dry_run=args.dry_run)
    print_info(
        f"Candidates: {len(result.candidates)}; uploaded: {len(result.uploaded)}; "
        f"failed: {len(result.failed)}; skipped: {len(result.skipped_missing_assets)}; "
        f"total songs: {result.total_songs}."
    )
    return 0 if not result.failed else 1


def command_pull(args: argparse.Namespace) -> int:
    config = build_config(args)
    result = pull_repository(config)
    print_info(result.message)
    if result.before or result.after:
        print_info(f"HEAD: {result.before or '-'} -> {result.after or '-'}")
    return 0 if result.ok else 1


def command_scan(args: argparse.Namespace) -> int:
    config = build_config(args)
    local_songs = discover_songs(config.ese_dir, config.audio_extensions)
    if args.server:
        server_songs = fetch_server_songs(config)
        missing = [song.key for song in local_songs if song.key not in server_songs]
    else:
        uploaded = load_uploaded_set(config.state_file)
        missing = [song.key for song in local_songs if song.key not in uploaded]
    for key in missing:
        print(key)
    print_info(f"Found {len(missing)} song(s).")
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = build_config(args)
    songs = discover_songs(config.ese_dir, config.audio_extensions)
    uploaded = load_uploaded_set(config.state_file)
    candidates = [song.key for song in songs if song.key not in uploaded]
    missing_assets = [
        song.key for song in songs if song.tja_path is None or song.music_path is None
    ]
    print_info(f"ESE directory: {config.ese_dir}")
    print_info(f"State file: {config.state_file}")
    print_info(f"Local songs: {len(songs)}")
    print_info(f"Recorded uploaded: {len(uploaded)}")
    print_info(f"Candidate new songs: {len(candidates)}")
    print_info(f"Songs missing TJA/audio: {len(missing_assets)}")
    return 0


def command_email_test(args: argparse.Namespace) -> int:
    config = build_config(args)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    subject = f"{config.email_subject_prefix} test"
    text_body = f"Resend test email from ESE updater at {now}."
    html_body = f"<p>Resend test email from ESE updater at {html.escape(now)}.</p>"
    sent, message = send_resend_email(config, subject, text_body, html_body)
    print_info(message)
    return 0 if sent else 1


def legacy_main(argv: list[str]) -> int:
    legacy = argparse.Namespace(
        env_file=".env",
        ese_dir=argv[0] if len(argv) >= 1 else None,
        site_url=argv[1] if len(argv) >= 2 else None,
        state_file=None,
    )
    mode = argv[3].strip() if len(argv) >= 4 else "1"
    if mode == "2":
        legacy.server = True
        return command_scan(legacy)
    legacy.dry_run = False
    return command_upload(legacy)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        return legacy_main(argv)

    parser = make_parser()
    args = parser.parse_args(argv)
    handlers = {
        "run": command_run,
        "upload": command_upload,
        "pull": command_pull,
        "scan": command_scan,
        "status": command_status,
        "email-test": command_email_test,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print_info("Interrupted.")
        return 130
    except Exception as exc:
        print_info(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
