"""`tiroir work <sheaf url>`: fork a sheaf's session to this machine, against a fake hub.

The fake hub is an `http.server` on 127.0.0.1 serving `/_/session` and `/_/sessions` and recording
every request it gets. HOME is the test's tmp_path; no real `~/.claude` is touched. The real exec is
tried through a subprocess with a fake `claude` on PATH that records its argv and cwd.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from tiroir.work import main

TOKEN = "eyJhbGciOiJIUzI1NiJ9.SECRET-TOKEN-7f3a.sig"
SESSION_ID = "6f1c0e9a-2b3d-4c5e-8f70-1a2b3c4d5e6f"
TRANSCRIPT = b'{"type":"user","sessionId":"6f1c0e9a-2b3d-4c5e-8f70-1a2b3c4d5e6f"}\n'
URL = "https://hub.example.com/tt/acme/site/plan.md"
DOC = "tt/acme/site/plan.md"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ON_WINDOWS = sys.platform == "win32"
# Windows mode bits say only read-only or not: no 0600 to check, and a read-only folder takes files.
posix_modes = pytest.mark.skipif(ON_WINDOWS, reason="Windows has no POSIX mode bits")


class FakeHub:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.session_status = 200
        self.session_id = SESSION_ID
        self.transcript = TRANSCRIPT
        self.sessions_status = 200
        self.sessions_answer = {"ok": True, "hash": "abc123"}
        self.redirect = ""
        self.short_body = False
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _record(self, body: bytes) -> None:
                hub.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )

            def do_GET(self) -> None:
                self._record(b"")
                if not self.path.startswith("/_/session?"):
                    self.send_error(404)
                    return
                if hub.redirect:
                    self.send_response(302)
                    self.send_header("Location", hub.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if hub.session_status != 200:
                    self.send_response(hub.session_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("X-Sheaf-Session", hub.session_id)
                self.send_header("Content-Length", str(len(hub.transcript) + 100 * hub.short_body))
                self.end_headers()
                self.wfile.write(hub.transcript)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._record(body)
                answer = json.dumps(hub.sessions_answer).encode()
                status = hub.sessions_status if self.path == "/_/sessions" else 404
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def gets(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "GET"]

    def posts(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]


@pytest.fixture
def hub() -> Iterator[FakeHub]:
    fake = FakeHub()
    fake.thread.start()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    path = tmp_path / "work" / "acme_site"
    path.mkdir(parents=True)
    return path


def encoded(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path.resolve()))


def transcript_path(home: Path, cwd: Path, session_id: str = SESSION_ID) -> Path:
    return home / ".claude" / "projects" / encoded(cwd) / f"{session_id}.jsonl"


def assert_private(path: Path) -> None:
    if not ON_WINDOWS:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def symlink(link: Path, target: Path, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as failure:  # Windows without the right to make links
        pytest.skip(f"cannot make a symbolic link here ({failure})")


def run(home: Path, *argv: str, env: dict | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environment = {"HOME": str(home), "TIROIR_FRONT_TOKEN": TOKEN, **(env or {})}
    environment = {k: v for k, v in environment.items() if v is not None}
    code = main(list(argv), out=out, err=err, env=environment)
    return code, out.getvalue(), err.getvalue()


def dry_run(home: Path, hub: FakeHub, cwd: Path, **env) -> tuple[int, str, str]:
    return run(home, "work", URL, "--hub", hub.url, "--cwd", str(cwd), "--dry-run", env=env)


# --- the happy path, dry run ------------------------------------------------------------------


def test_dry_run_fetches_the_transcript_with_the_cookie(tmp_path, hub, work_dir):
    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    (get,) = hub.gets()
    assert get["path"] == "/_/session?doc=tt%2Facme%2Fsite%2Fplan.md"
    assert get["headers"]["cookie"] == f"token={TOKEN}"


def test_dry_run_writes_the_transcript_where_claude_finds_it_mode_0600(tmp_path, hub, work_dir):
    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    path = transcript_path(tmp_path, work_dir)
    assert path.read_bytes() == TRANSCRIPT
    assert_private(path)
    resolved = work_dir.resolve()  # `/` is `-`; Windows' `C:\` is `C--`, as Claude Code names it
    anchor = re.sub(r"[^A-Za-z0-9]", "-", resolved.anchor)
    assert path.parent.name == anchor + "-".join(resolved.parts[1:]).replace("_", "-")


def test_dry_run_prints_the_claude_command_and_records_no_fork(tmp_path, hub, work_dir):
    code, out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    match = re.search(
        rf"^claude --resume {SESSION_ID} --fork-session --session-id (\S+)$", out, re.MULTILINE
    )
    assert match, out
    assert UUID.match(match.group(1))
    assert str(work_dir.resolve()) in out
    assert hub.posts() == []


def test_cwd_defaults_to_the_current_directory(tmp_path, hub, work_dir, monkeypatch):
    monkeypatch.chdir(work_dir)

    code, _out, err = run(tmp_path, "work", URL, "--hub", hub.url, "--dry-run")

    assert code == 0, err
    assert transcript_path(tmp_path, work_dir).is_file()


# --- the transcript file ----------------------------------------------------------------------


def test_an_identical_transcript_already_there_is_fine(tmp_path, hub, work_dir):
    assert dry_run(tmp_path, hub, work_dir)[0] == 0

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err


def kept(folder: Path) -> list[Path]:
    """The copies the command kept aside in `folder`: `<id>.jsonl.kept-<UTC stamp>`."""
    return sorted(folder.glob(f"{SESSION_ID}.jsonl.kept-*"))


def test_a_different_file_with_the_same_id_is_kept_aside_and_replaced(tmp_path, hub, work_dir):
    path = transcript_path(tmp_path, work_dir)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"mine\n")

    code, out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    assert path.read_bytes() == TRANSCRIPT
    (aside,) = kept(path.parent)
    assert aside.read_bytes() == b"mine\n"
    assert re.fullmatch(rf"{SESSION_ID}\.jsonl\.kept-[0-9]{{8}}T[0-9]{{6}}Z(-[0-9]+)?", aside.name)
    assert f"An older copy of this session was kept as {aside}" in out


def test_the_same_id_in_another_project_folder_is_kept_aside_there(tmp_path, hub, work_dir):
    # Two sheaves of one project can share one session; the copy a fork of the first
    # left in another folder made a fork of the second refuse; a member cannot be asked to move it.
    other = tmp_path / ".claude" / "projects" / "-elsewhere" / f"{SESSION_ID}.jsonl"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"older\n")

    code, out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    assert not other.exists()
    (aside,) = kept(other.parent)
    assert aside.read_bytes() == b"older\n"
    assert transcript_path(tmp_path, work_dir).read_bytes() == TRANSCRIPT
    assert f"An older copy of this session was kept as {aside}" in out
    # Claude Code finds the session by `*/<id>.jsonl`: exactly one copy is left.
    assert list((tmp_path / ".claude" / "projects").glob(f"*/{SESSION_ID}.jsonl")) == [
        transcript_path(tmp_path, work_dir)
    ]


def test_two_copies_kept_in_the_same_second_both_stay(tmp_path, hub, work_dir, monkeypatch):
    import tiroir.work as handover

    monkeypatch.setattr(handover, "_stamp", lambda: "20261029T210000Z")
    path = transcript_path(tmp_path, work_dir)
    path.parent.mkdir(parents=True)
    for body in (b"first\n", b"second\n"):
        path.write_bytes(body)
        assert dry_run(tmp_path, hub, work_dir)[0] == 0
    assert sorted(p.read_bytes() for p in kept(path.parent)) == [b"first\n", b"second\n"]


def test_a_link_with_the_sessions_name_is_still_refused_and_left(tmp_path, hub, work_dir):
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(b"not a session\n")
    other = tmp_path / ".claude" / "projects" / "-elsewhere" / f"{SESSION_ID}.jsonl"
    other.parent.mkdir(parents=True)
    symlink(other, target)

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    assert str(other) in err
    assert other.is_symlink() and kept(other.parent) == []
    assert not transcript_path(tmp_path, work_dir).exists()


def test_a_link_at_the_target_is_refused_before_any_other_copy_moves(tmp_path, hub, work_dir):
    # Opus gate on PR 3: a copy elsewhere was kept aside, then the target refused, unsaid.
    other = tmp_path / ".claude" / "projects" / "-elsewhere" / f"{SESSION_ID}.jsonl"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"older\n")
    target = transcript_path(tmp_path, work_dir)
    target.parent.mkdir(parents=True)
    symlink(target, tmp_path / "nowhere.jsonl")

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    assert str(target) in err
    assert target.is_symlink() and other.read_bytes() == b"older\n" and kept(other.parent) == []


def test_an_older_snapshot_elsewhere_is_kept_aside_too_never_deleted(tmp_path, hub, work_dir):
    # Opus delta check: a copy a live `claude --resume` still appends to would lose its last lines
    # if it were deleted; kept aside, its inode keeps them.
    other = tmp_path / ".claude" / "projects" / "-elsewhere" / f"{SESSION_ID}.jsonl"
    other.parent.mkdir(parents=True)
    other.write_bytes(TRANSCRIPT[:10])

    code, out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    (aside,) = kept(other.parent)
    assert aside.read_bytes() == TRANSCRIPT[:10] and not other.exists()
    assert f"An older copy of this session was kept as {aside}" in out


def test_a_project_folder_that_links_to_the_target_folder_is_the_same_copy(tmp_path, hub, work_dir):
    # Opus delta check: the glob found the target again through a linked folder and settled it away.
    target = transcript_path(tmp_path, work_dir)
    target.parent.mkdir(parents=True)
    target.write_bytes(TRANSCRIPT[:10])  # an earlier fork's copy, which the glob finds twice
    symlink(tmp_path / ".claude" / "projects" / "-alias", target.parent, directory=True)

    code, out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    assert target.read_bytes() == TRANSCRIPT and kept(target.parent) == []
    assert "kept as" not in out


@posix_modes
def test_a_target_that_cannot_be_written_moves_no_other_copy(tmp_path, hub, work_dir):
    other = tmp_path / ".claude" / "projects" / "-elsewhere" / f"{SESSION_ID}.jsonl"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"older\n")
    folder = transcript_path(tmp_path, work_dir).parent
    folder.mkdir(parents=True)
    folder.chmod(0o500)
    try:
        code, _out, err = dry_run(tmp_path, hub, work_dir)
    finally:
        folder.chmod(0o700)

    assert code == 2, err
    assert other.read_bytes() == b"older\n" and kept(other.parent) == []


def test_a_keep_that_fails_after_another_names_the_one_already_kept(
    tmp_path, hub, work_dir, monkeypatch
):
    import tiroir.work as handover

    projects = tmp_path / ".claude" / "projects"
    first, second = (projects / name / f"{SESSION_ID}.jsonl" for name in ("-a", "-b"))
    for f in (first, second):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"older\n")
    real_unlink = handover.Path.unlink

    def unlink(self, *args, **kwargs):
        if self == second:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(handover.Path, "unlink", unlink)

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    (aside,) = kept(first.parent)
    assert f"Could not keep the older copy {second} aside" in err and str(aside) in err


def test_a_copy_that_cannot_be_kept_aside_is_one_plain_sentence(
    tmp_path, hub, work_dir, monkeypatch
):
    import tiroir.work as handover

    other = tmp_path / ".claude" / "projects" / "-elsewhere" / f"{SESSION_ID}.jsonl"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"older\n")
    real_unlink = handover.Path.unlink

    def unlink(self, *args, **kwargs):
        if self == other:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(handover.Path, "unlink", unlink)

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    assert f"Could not keep the older copy {other} aside (Permission denied)" in err
    assert other.read_bytes() == b"older\n" and kept(other.parent) == []


def test_a_session_id_of_another_shape_is_refused(tmp_path, hub, work_dir):
    hub.session_id = "../../etc/passwd"

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    assert "session" in err
    assert not (tmp_path / ".claude").exists()


# --- plain-sentence refusals ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "words"),
    [(401, "not signed in"), (403, "not open to you"), (404, "not open to you")],
)
def test_hub_refusals_are_one_plain_sentence(tmp_path, hub, work_dir, status, words):
    hub.session_status = status

    code, out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    assert words in err
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert TOKEN not in out + err


@pytest.mark.parametrize(
    "url",
    [
        "http://hub.example.com/tt/acme/site/plan.md",
        "https://hub.example.com/tt/acme/plan.md",
        "https://hub.example.com/tt/acme/site/sub/plan.md",
        "https://hub.example.com/tt/acme/site/plan.txt",
        "https://hub.example.com/tt/../site/plan.md",
        "https://hub.example.com/tt/%2e%2e/site/plan.md",
        "https://hub.example.com/tt/acme/site/plan.md?x=1",
        "https://user:pw@hub.example.com/tt/acme/site/plan.md",
        "tt/acme/site/plan.md",
    ],
)
def test_a_url_of_the_wrong_shape_is_refused_before_any_request(tmp_path, hub, work_dir, url):
    code, _out, err = run(tmp_path, "work", url, "--hub", hub.url, "--cwd", str(work_dir))

    assert code == 2
    assert "https://<host>/<root>/<client>/<project>/<file>.md" in err
    assert hub.requests == []


def test_the_hub_override_takes_only_local_http(tmp_path, hub, work_dir):
    code, _out, err = run(tmp_path, "work", URL, "--hub", "http://example.com", "--dry-run")

    assert code == 2
    assert "--hub" in err


# --- the token --------------------------------------------------------------------------------


def write_token_file(home: Path, mode: int, text: str = TOKEN + "\n") -> Path:
    path = home / ".config" / "tiroir" / "front-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_the_token_file_is_read_when_the_variable_is_unset(tmp_path, hub, work_dir):
    write_token_file(tmp_path, 0o600)

    code, _out, err = dry_run(tmp_path, hub, work_dir, TIROIR_FRONT_TOKEN=None)

    assert code == 0, err
    assert hub.gets()[0]["headers"]["cookie"] == f"token={TOKEN}"


@posix_modes
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700])
def test_a_token_file_with_loose_mode_is_refused(tmp_path, hub, work_dir, mode):
    path = write_token_file(tmp_path, mode)

    code, out, err = dry_run(tmp_path, hub, work_dir, TIROIR_FRONT_TOKEN=None)

    assert code == 2
    assert str(path) in err
    assert "chmod 600" in err
    assert TOKEN not in out + err
    assert hub.requests == []


def test_no_token_at_all_says_where_to_put_it(tmp_path, hub, work_dir):
    code, _out, err = dry_run(tmp_path, hub, work_dir, TIROIR_FRONT_TOKEN=None)

    assert code == 2
    assert "~/.config/tiroir/front-token" in err
    assert hub.requests == []


def test_a_token_with_a_line_break_inside_is_refused_without_showing_it(tmp_path, hub, work_dir):
    code, out, err = dry_run(tmp_path, hub, work_dir, TIROIR_FRONT_TOKEN=TOKEN + "\r\nX: y")

    assert code == 2
    assert TOKEN not in out + err
    assert hub.requests == []


# --- the real run: the fork is recorded, then claude is exec'd --------------------------------


def fake_claude(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "claude-call.json"
    code = (
        "import json, os, sys\n"
        "call = {'argv': sys.argv[1:], 'cwd': os.getcwd()}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(call))\n"
        f"sys.exit({exit_code})\n"
    )
    if sys.platform == "win32":
        # Windows finds commands by extension (PATHEXT), as it finds npm's `claude.cmd`.
        (tmp_path / "claude.py").write_text(code)
        (bin_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{tmp_path / "claude.py"}" %*\n')
        return bin_dir, record
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n{code}")
    script.chmod(0o755)
    return bin_dir, record


def run_for_real(
    tmp_path: Path,
    hub: FakeHub,
    cwd: Path,
    path_dirs: list[Path],
    url: str = URL,
    run_in: Path | None = None,
):
    env = {
        "HOME": str(tmp_path),
        "TIROIR_FRONT_TOKEN": TOKEN,
        "PATH": os.pathsep.join([*map(str, path_dirs), "/usr/bin", "/bin"]),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    for key in ("LD_LIBRARY_PATH", "SYSTEMROOT", "PATHEXT", "COMSPEC"):
        if key in os.environ:
            env[key] = os.environ[key]
    argv = [sys.executable, "-m", "tiroir.work", "work", url]
    argv += ["--hub", hub.url, "--cwd", str(cwd)]
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, cwd=run_in)


def test_the_fork_is_recorded_then_claude_runs_in_the_work_dir(tmp_path, hub, work_dir):
    bin_dir, record = fake_claude(tmp_path)

    done = run_for_real(tmp_path, hub, work_dir, [bin_dir])

    assert done.returncode == 0, done.stderr
    (post,) = hub.posts()
    assert post["path"] == "/_/sessions"
    assert post["headers"]["origin"] == "https://hub.example.com"
    assert post["headers"]["cookie"] == f"token={TOKEN}"
    assert post["headers"]["content-type"] == "application/json"
    body = json.loads(post["body"])
    assert set(body) == {"doc", "id", "machine"}
    assert body["doc"] == DOC
    assert UUID.match(body["id"]) and body["id"] != SESSION_ID
    assert body["machine"] == socket.gethostname().split(".")[0]
    call = json.loads(record.read_text())
    assert call["argv"] == ["--resume", SESSION_ID, "--fork-session", "--session-id", body["id"]]
    assert call["cwd"] == str(work_dir.resolve())
    assert transcript_path(tmp_path, work_dir).read_bytes() == TRANSCRIPT
    assert TOKEN not in done.stdout + done.stderr


def test_without_claude_on_path_nothing_is_recorded(tmp_path, hub, work_dir):
    done = run_for_real(tmp_path, hub, work_dir, [])

    assert done.returncode == 2
    assert "claude" in done.stderr
    assert hub.posts() == []


def test_a_refused_fork_record_stops_before_claude(tmp_path, hub, work_dir):
    bin_dir, record = fake_claude(tmp_path)
    hub.sessions_status = 403

    done = run_for_real(tmp_path, hub, work_dir, [bin_dir])

    assert done.returncode == 2
    assert "not open to you" in done.stderr and "the fork was not recorded" in done.stderr
    assert "no session on the box" not in done.stderr
    assert not record.exists()
    assert TOKEN not in done.stdout + done.stderr


def test_a_fork_record_the_hub_refuses_says_the_hubs_reason(tmp_path, hub, work_dir):
    bin_dir, record = fake_claude(tmp_path)
    hub.sessions_status = 409
    hub.sessions_answer = {
        "ok": False,
        "error": "sessions is written on one line: write it as one item per line",
    }

    done = run_for_real(tmp_path, hub, work_dir, [bin_dir])

    assert done.returncode == 2
    assert (
        "refused to record the fork: sessions is written on one line: write it as one item per line"
        in done.stderr
    )
    assert not record.exists()


@pytest.mark.parametrize("error", ["line one\nline two", "x" * 300, 42])
def test_a_reason_that_is_not_one_short_line_is_not_repeated(tmp_path, hub, work_dir, error):
    bin_dir, record = fake_claude(tmp_path)
    hub.sessions_status = 400
    hub.sessions_answer = {"ok": False, "error": error}

    done = run_for_real(tmp_path, hub, work_dir, [bin_dir])

    assert done.returncode == 2
    assert "answered 400; the fork was not recorded" in done.stderr
    assert "line two" not in done.stderr and "x" * 300 not in done.stderr
    assert not record.exists()


# --- review round: encoded addresses, redirects, origin, broken reads, the stored file ---------


@pytest.mark.parametrize(
    ("url", "doc"),
    [
        ("https://hub.example.com/tt/acme/site/plan%202.md", "tt/acme/site/plan 2.md"),
        ("https://hub.example.com/tt/acme/caf%C3%A9/plan.md", "tt/acme/café/plan.md"),
    ],
)
def test_the_address_the_hub_prints_is_decoded(tmp_path, hub, work_dir, url, doc):
    code, _out, err = run(
        tmp_path, "work", url, "--hub", hub.url, "--cwd", str(work_dir), "--dry-run"
    )

    assert code == 0, err
    (get,) = hub.gets()
    assert get["path"] == f"/_/session?doc={quote(doc, safe='')}"


@pytest.mark.parametrize(
    "url",
    [
        "https://hub.example.com/tt/acme/a%2Fb/plan.md",
        "https://hub.example.com/tt/acme/a%00b/plan.md",
        "https://hub.example.com/tt/acme/%FF/plan.md",
        "https://hub.example.com/tt/acme/%20site/plan.md",
    ],
)
def test_an_encoded_segment_that_decodes_to_no_name_is_refused(tmp_path, hub, work_dir, url):
    code, _out, err = run(tmp_path, "work", url, "--hub", hub.url, "--cwd", str(work_dir))

    assert code == 2
    assert "https://<host>/<root>/<client>/<project>/<file>.md" in err
    assert hub.requests == []


def test_a_redirect_is_refused_and_never_carries_the_token(tmp_path, hub, work_dir):
    elsewhere = FakeHub()
    elsewhere.thread.start()
    try:
        hub.redirect = f"{elsewhere.url}/_/session?doc=x"

        code, out, err = dry_run(tmp_path, hub, work_dir)

        assert code == 2
        assert elsewhere.requests == []
        assert not transcript_path(tmp_path, work_dir).exists()
        assert "Traceback" not in err and TOKEN not in out + err
    finally:
        elsewhere.server.shutdown()
        elsewhere.server.server_close()


def test_the_origin_is_the_canonical_one(tmp_path, hub, work_dir):
    bin_dir, _record = fake_claude(tmp_path)

    done = run_for_real(
        tmp_path, hub, work_dir, [bin_dir], url="https://Hub.Example.com:443/tt/acme/site/plan.md"
    )

    assert done.returncode == 0, done.stderr
    (post,) = hub.posts()
    assert post["headers"]["origin"] == "https://hub.example.com"


def test_a_transcript_cut_short_is_one_plain_sentence(tmp_path, hub, work_dir):
    hub.short_body = True

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    assert err.count("\n") == 1 and "Traceback" not in err
    assert not transcript_path(tmp_path, work_dir).exists()


def test_a_session_that_grew_since_the_last_fork_replaces_the_older_copy(tmp_path, hub, work_dir):
    path = transcript_path(tmp_path, work_dir)
    path.parent.mkdir(parents=True)
    path.write_bytes(TRANSCRIPT)
    hub.transcript = TRANSCRIPT + b'{"type":"assistant"}\n'

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 0, err
    assert path.read_bytes() == hub.transcript
    assert_private(path)
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_a_failed_write_leaves_no_file_behind(tmp_path, hub, work_dir, monkeypatch):
    def full(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", full)

    code, _out, err = dry_run(tmp_path, hub, work_dir)

    assert code == 2
    assert "Traceback" not in err
    assert list(transcript_path(tmp_path, work_dir).parent.glob("*")) == []


def test_an_emoji_in_the_folder_becomes_two_dashes_like_claude_code(tmp_path, hub, work_dir):
    cwd = work_dir / "sm\U0001f600le"
    cwd.mkdir()

    code, _out, err = dry_run(tmp_path, hub, cwd)

    assert code == 0, err
    name = str(cwd.resolve()).replace("\U0001f600", "--")
    expected = tmp_path / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", name)
    assert (expected / f"{SESSION_ID}.jsonl").read_bytes() == TRANSCRIPT


def test_a_folder_too_long_for_claude_codes_plain_name_is_refused(tmp_path, hub, work_dir):
    cwd = work_dir
    while len(str(cwd)) <= 200:
        cwd = cwd / ("d" * 40)
    cwd.mkdir(parents=True)

    code, _out, err = dry_run(tmp_path, hub, cwd)

    assert code == 2
    assert "--cwd" in err
    assert hub.requests == []


def test_claude_that_cannot_start_is_one_plain_sentence(tmp_path, hub, work_dir):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if ON_WINDOWS:
        (bin_dir / "claude.exe").write_bytes(b"not a program\n")
    else:
        script = bin_dir / "claude"
        script.write_text("#!/nonexistent/interpreter\n")
        script.chmod(0o755)

    done = run_for_real(tmp_path, hub, work_dir, [bin_dir])

    assert done.returncode == 2
    assert "Traceback" not in done.stderr
    assert "claude" in done.stderr


def test_a_relative_path_entry_still_finds_claude_after_the_chdir(tmp_path, hub, work_dir):
    _bin_dir, record = fake_claude(tmp_path)

    done = run_for_real(tmp_path, hub, work_dir, [Path("bin")], run_in=tmp_path)

    assert done.returncode == 0, done.stderr
    assert json.loads(record.read_text())["cwd"] == str(work_dir.resolve())


def test_a_forbidden_fork_record_says_the_hubs_reason(tmp_path, hub, work_dir):
    bin_dir, record = fake_claude(tmp_path)
    hub.sessions_status = 403
    hub.sessions_answer = {
        "ok": False,
        "error": "this request did not come from the front's own page",
    }

    done = run_for_real(tmp_path, hub, work_dir, [bin_dir])

    assert done.returncode == 2
    assert "this request did not come from the front's own page" in done.stderr
    assert not record.exists()


def test_a_tilde_the_hub_leaves_unencoded_is_accepted(tmp_path, hub, work_dir):
    url = "https://hub.example.com/tt/acme/site/a~b.md"

    code, _out, err = run(
        tmp_path, "work", url, "--hub", hub.url, "--cwd", str(work_dir), "--dry-run"
    )

    assert code == 0, err
    (get,) = hub.gets()
    assert get["path"] == f"/_/session?doc={quote('tt/acme/site/a~b.md', safe='')}"


@posix_modes
def test_a_project_folder_that_cannot_be_written_is_one_plain_sentence(tmp_path, hub, work_dir):
    folder = transcript_path(tmp_path, work_dir).parent
    folder.mkdir(parents=True)
    folder.chmod(0o500)
    try:
        code, _out, err = dry_run(tmp_path, hub, work_dir)
    finally:
        folder.chmod(0o700)

    assert code == 2
    assert err.count("\n") == 1 and "Traceback" not in err
    assert str(folder) in err


# --- Windows: no owners or mode bits, and no exec ---------------------------------------------


def as_windows(monkeypatch) -> None:
    """What `tiroir work` meets on Windows, on any runner: no `os.getuid`, and `WINDOWS` set."""
    import tiroir.work as handover

    monkeypatch.setattr(handover, "WINDOWS", True)
    monkeypatch.delattr(os, "getuid", raising=False)


def test_on_windows_the_token_file_is_read_without_unix_owners(
    tmp_path, hub, work_dir, monkeypatch
):
    # Windows has no `os.getuid`, and its mode bits say nothing about who can read the file.
    write_token_file(tmp_path, 0o644)
    as_windows(monkeypatch)

    code, _out, err = dry_run(tmp_path, hub, work_dir, TIROIR_FRONT_TOKEN=None)

    assert code == 0, err
    assert hub.gets()[0]["headers"]["cookie"] == f"token={TOKEN}"


def test_on_windows_claude_runs_as_a_child_and_its_exit_code_is_returned(
    tmp_path, hub, work_dir, monkeypatch
):
    # os.execv on Windows starts a new process and ends this one: the shell takes the console back
    # while claude still reads it. So claude runs as a child, waited for, in the work folder.
    bin_dir, record = fake_claude(tmp_path, exit_code=3)
    as_windows(monkeypatch)
    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("execv on Windows"))
    here = os.getcwd()

    code, _out, err = run(
        tmp_path, "work", URL, "--hub", hub.url, "--cwd", str(work_dir), env={"PATH": str(bin_dir)}
    )

    assert code == 3, err
    (post,) = hub.posts()
    call = json.loads(record.read_text())
    assert call["argv"] == [
        "--resume", SESSION_ID, "--fork-session", "--session-id", json.loads(post["body"])["id"]
    ]  # fmt: skip
    assert call["cwd"] == str(work_dir.resolve())
    assert os.getcwd() == here


def test_on_windows_ctrl_c_is_left_to_claude(tmp_path, hub, work_dir, monkeypatch):
    # Ctrl+C reaches every process on the console; claude answers it, and this one keeps waiting.
    import tiroir.work as handover

    class Child:
        waits = 0

        def __init__(self, argv, cwd):
            pass

        def wait(self):
            Child.waits += 1
            if Child.waits == 1:
                raise KeyboardInterrupt
            return 0

    bin_dir, _record = fake_claude(tmp_path)
    as_windows(monkeypatch)
    monkeypatch.setattr(handover.subprocess, "Popen", Child)

    code, _out, err = run(
        tmp_path, "work", URL, "--hub", hub.url, "--cwd", str(work_dir), env={"PATH": str(bin_dir)}
    )

    assert code == 0, err
    assert Child.waits == 2
