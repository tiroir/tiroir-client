"""The `tiroir` command. `tiroir work <sheaf url>` forks a sheaf's session to this machine.

It runs on the member's laptop and never runs anything on the box. In order: fetch the sheaf's
session transcript from the hub (`GET /_/session`), store it where Claude Code looks for it
(`~/.claude/projects/<encoded cwd>/<id>.jsonl`, or `$CLAUDE_CONFIG_DIR/projects/...` when that is
set; mode 0600), record the fork on the sheaf (`POST /_/sessions`), then replace itself with
`claude --resume <id> --fork-session --session-id <fork id>` in the work folder. On Windows, which
has no such replacement, it runs claude as a child and waits for it.

The credential is a sign-in code the hub issued (sheaf SPEC 9.13): `sheaf_code_` and 43
base64url characters, good for one sheaf, once, for 10 minutes. It is read from a hidden prompt
when stdin is a terminal, or from `TIROIR_SIGNIN_CODE`; never from argv, a URL or a file. It is
sent as exactly one `Authorization: Bearer` line, only to a front this command knows (`FRONTS`,
and the `fronts` of `~/.config/tiroir/config.toml`), and the variable is removed before Claude
Code starts.

Until the code is accepted on the hub (tt-tiroir#60, decision 5), a member who has the session
token of the front, in `TIROIR_FRONT_TOKEN` or the file `~/.config/tiroir/front-token` (mode
0600, owned by the user; on Windows, in the user's own profile folder), signs in with it as a
cookie instead, unless `TIROIR_SIGNIN_CODE` is set. Neither is ever printed, logged or put in an
error message.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, unquote, urlsplit

TOKEN_VARIABLE = "TIROIR_FRONT_TOKEN"
TOKEN_FILE = "~/.config/tiroir/front-token"
CODE_VARIABLE = "TIROIR_SIGNIN_CODE"
CONFIG_DIR_VARIABLE = "CLAUDE_CONFIG_DIR"
CODE = re.compile(r"^sheaf_code_[A-Za-z0-9_-]{43}$")
CODE_PROMPT = "Sign-in code (Work on this > Get a sign-in code): "
CONFIG_FILE = "~/.config/tiroir/config.toml"
# The fronts a code may be sent to: a look-alike host would otherwise get a live code to replay.
FRONTS = frozenset({"thinkingtoo.tiroir.me"})
NO_GOOD = "This sign-in code is no good: get a new one from Work on this."
NOT_OPEN = (
    "This sheaf is not open to you with this code: get a code from this sheaf's Work on this."
)
REASON_MAX = 200  # characters of the hub's own reason repeated to the member
PROJECT_NAME_MAX = 200  # longer names get a hash suffix in Claude Code; not reproduced here
URL_SHAPE = "https://<host>/<root>/<client>/<project>/<file>.md"
SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SEGMENT = re.compile(r"^[A-Za-z0-9._~ %-]+$")
HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")
LOCAL_HUB = re.compile(r"^http://127\.0\.0\.1(:[0-9]{1,5})?$")
TOKEN_CHARS = re.compile(r"^[\x21-\x2b\x2d-\x3a\x3c-\x7e]+$")  # printable, no `,` or `;`
TIMEOUT = 60
WINDOWS = os.name == "nt"


class Refusal(Exception):
    """One plain sentence for the member; never carries the token."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise Refusal(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="tiroir", description="tiroir on your own machine.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    work = commands.add_parser("work", help="fork a sheaf's session to this machine")
    work.add_argument("url", help=f"the sheaf's address, {URL_SHAPE}")
    work.add_argument("--cwd", help="the folder Claude works in (default: the current one)")
    work.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and store the transcript, print the claude command, record nothing",
    )
    work.add_argument("--hub", help=argparse.SUPPRESS)  # tests only: http://127.0.0.1[:port]
    return parser


@dataclass(frozen=True)
class Sheaf:
    host: str
    doc: str

    @property
    def origin(self) -> str:
        return f"https://{self.host}"


def parse_sheaf_url(url: str) -> Sheaf:
    """`https://<host>/<root>/<client>/<project>/<file>.md` → host and address; anything else is
    refused (no query, no fragment, no user info, no dot segment). Segments are percent-decoded,
    as the hub encodes them (`roots.url_path`); one that decodes to a slash, a control character,
    bytes that are not UTF-8 or padding spaces is refused. The host is lowercased and loses
    `:443`, so the Origin header is the front's own."""
    wrong = Refusal(f"That is not a sheaf address; a sheaf address looks like {URL_SHAPE}.")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.query or parts.fragment or "@" in parts.netloc:
        raise wrong
    host = parts.netloc.lower().removesuffix(":443")
    if not HOST.match(host):
        raise wrong
    segments = parts.path.split("/")
    if len(segments) != 5 or segments[0] != "":
        raise wrong
    segments = [_segment(raw, wrong) for raw in segments[1:]]
    if not segments[-1].endswith(".md") or segments[-1] == ".md":
        raise wrong
    return Sheaf(host=host, doc="/".join(segments))


def _segment(raw: str, wrong: Refusal) -> str:
    if not SEGMENT.match(raw):
        raise wrong
    try:
        segment = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        raise wrong from None
    if "/" in segment or not segment.isprintable() or set(segment) <= {"."}:
        raise wrong
    if segment != segment.strip():
        raise wrong
    return segment


def read_token(env: Mapping[str, str], home: Path) -> str:
    token = env.get(TOKEN_VARIABLE)
    if token is None:
        path = home / TOKEN_FILE.removeprefix("~/")
        try:
            info = path.stat()
        except FileNotFoundError:
            raise Refusal(
                f"No sign-in token: put the token in {TOKEN_FILE} (see the README, "
                "'Your sign-in token')."
            ) from None
        # Windows has no owner ids, and its mode bits only say read-only or not; the file sits in
        # the user's profile folder, which other users cannot read.
        if not WINDOWS and (info.st_mode & 0o777 != 0o600 or info.st_uid != os.getuid()):
            raise Refusal(
                f"The token file {path} must be yours and readable by you alone: "
                f"run `chmod 600 {path}` and try again."
            )
        token = path.read_text(encoding="utf-8", errors="replace").strip()
    if not token or not TOKEN_CHARS.match(token):
        raise Refusal(
            f"The sign-in token is empty or has spaces or line breaks inside; copy it again "
            f"into {TOKEN_FILE}."
        )
    return token


def known_fronts(home: Path) -> frozenset[str]:
    """`FRONTS` and the `fronts` of the config file, lowercased."""
    path = home / CONFIG_FILE.removeprefix("~/")
    try:
        with path.open("rb") as config:
            fronts = tomllib.load(config).get("fronts", [])
    except FileNotFoundError:
        return FRONTS
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise Refusal(f"{path} could not be read as TOML; nothing was sent.") from None
    if not isinstance(fronts, list) or not all(isinstance(f, str) for f in fronts):
        raise Refusal(f"`fronts` in {path} must be a list of host names; nothing was sent.")
    return FRONTS | {f.lower().removesuffix(":443") for f in fronts}


def read_code(env: Mapping[str, str], prompt: Callable[[str], str] | None) -> str:
    """The code from `TIROIR_SIGNIN_CODE`, else from the hidden prompt (None: no terminal)."""
    code = env.get(CODE_VARIABLE)
    if code is None:
        if prompt is None:
            raise Refusal(
                "No sign-in code: on the sheaf's page, open Work on this > Get a sign-in code, "
                f"then run this in a terminal to paste it, or put it in {CODE_VARIABLE} with "
                f"`read -s {CODE_VARIABLE}; export {CODE_VARIABLE}`."
            )
        code = prompt(CODE_PROMPT)
    code = code.strip()
    if not CODE.match(code):
        raise Refusal(
            "That is not a sign-in code: copy it again from Work on this > Get a sign-in code."
        )
    return code


def _hidden_prompt(text: str) -> str:
    try:
        return getpass.getpass(text)
    except EOFError:
        return ""


def claude_projects(env: Mapping[str, str], home: Path) -> Path:
    """The folder Claude Code keeps its sessions in: `$CLAUDE_CONFIG_DIR/projects` when the variable
    is set and not empty, on every OS, else `<home>/.claude/projects`. Claude Code refuses a
    configuration home that is not an absolute path, so this command refuses it too, before
    anything is sent."""
    config = env.get(CONFIG_DIR_VARIABLE)
    if not config:
        return home / ".claude" / "projects"
    if not Path(config).is_absolute():
        raise Refusal(
            f"{CONFIG_DIR_VARIABLE} is {config!r}, not an absolute path, so Claude Code would not "
            "find the session there; nothing was sent."
        )
    return Path(config) / "projects"


def encode_project(cwd: Path) -> str:
    """Claude Code's project folder name: every UTF-16 code unit not [A-Za-z0-9] becomes '-', so a
    character outside the BMP (an emoji) becomes '--'. Claude Code shortens a name over 200
    characters with a hash this command does not reproduce, so such a folder is refused."""
    name = "".join(
        c if c.isascii() and c.isalnum() else "--" if ord(c) > 0xFFFF else "-" for c in str(cwd)
    )
    if len(name) > PROJECT_NAME_MAX:
        raise Refusal(
            f"The folder {cwd} has too long a path for Claude Code's session folder; "
            "pick a shorter one with --cwd."
        )
    return name


def machine_name() -> str:
    name = socket.gethostname().split(".")[0]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)[:63]
    return name or "laptop"


def _reason(answer: urlerror.HTTPError) -> str:
    """The hub's own `error` of a refusal, when it is one short line of text; else ""."""
    try:
        error = json.loads(answer.read(4096)).get("error")
    except (ValueError, AttributeError, OSError):
        return ""
    if not isinstance(error, str) or len(error) > REASON_MAX or not error.isprintable():
        return ""
    return error.strip().rstrip(".")


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """A redirect is a refusal: following it would send the token cookie wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urlrequest.build_opener(_NoRedirect)


class Hub:
    """The two hub routes, through the proxy (or a local fake hub in tests)."""

    def __init__(self, sheaf: Sheaf, base: str, token: str = "", code: str = "") -> None:
        self.sheaf = sheaf
        self.base = base
        self._code = bool(code)
        # One credential, as one header line: a sign-in code, else the front's session token.
        self._header = ("Authorization", f"Bearer {code}") if code else ("Cookie", f"token={token}")

    def _open(self, req: urlrequest.Request, recording: bool = False):
        req.add_header(*self._header)
        try:
            return _OPENER.open(req, timeout=TIMEOUT)
        except urlerror.HTTPError as answer:
            reason = _reason(answer)
            answer.close()
            raise self._refusal(answer.code, reason, recording) from None
        except (urlerror.URLError, OSError) as failure:
            reason = getattr(failure, "reason", failure)
            raise Refusal(f"Could not reach {self.sheaf.host} ({reason}).") from None

    def _refusal(self, status: int, reason: str, recording: bool) -> Refusal:
        host = self.sheaf.host
        if self._code:
            return self._code_refusal(status, reason, recording)
        if status == 401:
            return Refusal(
                f"You are not signed in to {host}: copy a fresh token into {TOKEN_FILE}."
            )
        if recording:
            # The transcript is already on this laptop; running the command again records the fork.
            if reason:
                return Refusal(f"The hub at {host} refused to record the fork: {reason}.")
            if status in (403, 404):
                return Refusal(
                    f"This sheaf is not open to you on {host}: the fork was not recorded."
                )
            return Refusal(f"The hub at {host} answered {status}; the fork was not recorded.")
        if status in (403, 404):
            return Refusal(
                f"This sheaf is not open to you on {host}, or it has no session on the box."
            )
        return Refusal(f"The hub at {host} answered {status}; nothing was changed.")

    def _code_refusal(self, status: int, reason: str, recording: bool) -> Refusal:
        """The answers of SPEC 9.13 as the member meets them. A refused fork spends the code, and
        the transcript is downloaded again before a fork is reported, so a retry needs a new one."""
        if status == 401:
            return Refusal(NO_GOOD)
        if status in (403, 404):
            return Refusal(NOT_OPEN)
        if recording:
            said = f"{reason}; " if reason else f"the hub at {self.sheaf.host} answered {status}; "
            return Refusal(f"The fork was not recorded: {said}get a new code to try again.")
        return Refusal(f"The hub at {self.sheaf.host} answered {status}; nothing was changed.")

    def _read(self, answer, outcome: str) -> bytes:
        try:
            return answer.read()
        except (OSError, http.client.HTTPException) as failure:
            name = type(failure).__name__
            raise Refusal(
                f"The answer from {self.sheaf.host} broke off ({name}); {outcome}."
            ) from None

    def fetch_session(self) -> tuple[str, bytes]:
        url = f"{self.base}/_/session?doc={quote(self.sheaf.doc, safe='')}"
        with self._open(urlrequest.Request(url, method="GET")) as answer:
            session_id = answer.headers.get("X-Sheaf-Session", "")
            body = self._read(answer, "nothing was changed")
        if not SESSION_ID.match(session_id):
            raise Refusal(f"The hub at {self.sheaf.host} did not name a valid session.")
        return session_id, body

    def record_fork(self, fork_id: str, machine: str) -> None:
        body = json.dumps({"doc": self.sheaf.doc, "id": fork_id, "machine": machine}).encode()
        req = urlrequest.Request(f"{self.base}/_/sessions", data=body, method="POST")
        req.add_header("Origin", self.sheaf.origin)
        req.add_header("Content-Type", "application/json")
        with self._open(req, recording=True) as answer:
            try:
                ok = json.loads(self._read(answer, "the fork was not recorded")).get("ok") is True
            except (ValueError, AttributeError, RecursionError):
                ok = False
        if not ok:
            raise Refusal(f"The hub at {self.sheaf.host} did not record the fork.")


def store_transcript(
    projects: Path, folder: str, session_id: str, body: bytes
) -> tuple[Path, list[Path]]:
    """Write the transcript as `<projects>/<folder>/<id>.jsonl`, mode 0600; return its path and the
    older copies kept aside. The bytes go to a temporary file first, so a failed write leaves
    nothing, and moves no other copy. Claude Code needs exactly one copy of a session, found as
    `*/<id>.jsonl`, and the member is never asked to move one (sheaves of one project can
    share one session). Once the target holds `body`, every other copy is kept aside
    as `<id>.jsonl.kept-<UTC stamp>` beside it, never deleted: a `claude --resume` still writing to
    it keeps its lines in the kept file. A folder that is the target's own under another name (a
    link, or another spelling on a case-insensitive disk) holds no other copy. A copy at the target
    that `body` extends (the box session grew since the last fork) is replaced, as it was before. A
    link or anything but a regular file under that name, here or elsewhere, is left alone and
    refused before anything."""
    target = projects / folder / f"{session_id}.jsonl"
    # The folder itself, not its name: a case-insensitive disk (a Mac) finds it under two spellings.
    here = os.stat(target.parent) if target.parent.is_dir() else None
    others = []
    if projects.is_dir():
        for other in sorted(projects.glob(f"*/{session_id}.jsonl")):
            if other != target and not (here and os.path.samestat(os.stat(other.parent), here)):
                if other.is_symlink() or not other.is_file():
                    raise Refusal(
                        f"This session is already on this machine in {other}, which is not a "
                        "plain file; it was left as it is. Move it away and try again."
                    )
                others.append(other)
    kept = _write(target, body)
    for other in others:
        try:
            kept.append(_keep_aside(other))
        except Refusal as refusal:
            if not kept:
                raise
            moved = ", ".join(str(k) for k in kept)
            raise Refusal(f"{refusal} Already kept aside: {moved}.") from None
    return target, kept


def _write(target: Path, body: bytes) -> list[Path]:
    """`body` at `target`, through a temporary file; the copy it replaces, kept aside if any."""
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tiroir-", suffix=".part", dir=target.parent)
    except OSError as failure:
        raise _unwritable(target, failure) from None
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file():
                raise _held(target) from None
            held = target.read_bytes()
            if held == body:
                return []
            kept = [] if body.startswith(held) else [_keep_aside(target)]
            os.replace(temporary, target)
            return kept
    except OSError as failure:
        raise _unwritable(target, failure) from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return []


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _keep_aside(path: Path) -> Path:
    """Rename `path` to `<name>.kept-<UTC stamp>` (`-1`, `-2`... when taken) in its own folder,
    which Claude Code no longer reads as a session; a link then an unlink, so no kept copy is
    overwritten. A failed unlink takes the new name back: the copy stays where it was."""
    base = f"{path.name}.kept-{_stamp()}"
    for n in range(1000):
        aside = path.with_name(base if n == 0 else f"{base}-{n}")
        try:
            os.link(path, aside)
        except FileExistsError:
            continue
        except OSError as failure:
            raise _not_kept(path, failure) from None
        try:
            path.unlink()
        except OSError as failure:
            aside.unlink(missing_ok=True)
            raise _not_kept(path, failure) from None
        return aside
    raise Refusal(f"Could not keep the older copy {path} aside; move it away and try again.")


def _not_kept(path: Path, failure: OSError) -> Refusal:
    return Refusal(
        f"Could not keep the older copy {path} aside ({failure.strerror}); "
        "move it away and try again."
    )


def _unwritable(target: Path, failure: OSError) -> Refusal:
    return Refusal(f"Could not write the transcript to {target.parent} ({failure.strerror}).")


def _held(target: Path) -> Refusal:
    return Refusal(
        f"A different file already holds this session at {target}; "
        "it was left as it is. Move it away and try again."
    )


def main(
    argv: Sequence[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] | None = None,
) -> int:
    """Run `tiroir`; return the exit code (a real `work` run does not return: it becomes claude).
    `prompt` asks for the sign-in code; by default a hidden prompt, when stdin is a terminal."""
    out = out or sys.stdout
    err = err or sys.stderr
    if env is None:
        env = os.environ
        if prompt is None and sys.stdin is not None and sys.stdin.isatty():
            prompt = _hidden_prompt
    try:
        return _work(_parser().parse_args(argv), out, env, prompt)
    except Refusal as refusal:
        err.write(f"tiroir: {refusal}\n")
        return 2


def _uses_token(env: Mapping[str, str], home: Path) -> bool:
    """Whether this run signs in with the front's session token: the member has one, and no code
    in the variable. Removed after Hans's acceptance (tt-tiroir#60, decision 5)."""
    if CODE_VARIABLE in env:
        return False
    return TOKEN_VARIABLE in env or (home / TOKEN_FILE.removeprefix("~/")).exists()


def _work(
    args: argparse.Namespace,
    out: TextIO,
    env: Mapping[str, str],
    prompt: Callable[[str], str] | None = None,
) -> int:
    # Claude Code's home on Windows is os.homedir(), USERPROFILE; a HOME there can point elsewhere.
    home = Path(env.get("USERPROFILE" if WINDOWS else "HOME") or Path.home())
    projects = claude_projects(env, home)
    sheaf = parse_sheaf_url(args.url)
    base = sheaf.origin
    if args.hub is not None:
        if not LOCAL_HUB.match(args.hub):
            raise Refusal("--hub is for tests only and takes http://127.0.0.1[:port].")
        base = args.hub
    cwd = Path(args.cwd or os.getcwd()).expanduser()
    # Claude Code names the project after its working folder as the system gives it: resolved on
    # macOS and Linux, as given on Windows, where resolve() makes a mapped drive \\server\share.
    cwd = Path(os.path.abspath(cwd)) if WINDOWS else cwd.resolve()
    if not cwd.is_dir():
        raise Refusal(f"The folder {cwd} does not exist.")
    folder = encode_project(cwd)
    claude = None if args.dry_run else shutil.which("claude", path=env.get("PATH"))
    if not args.dry_run and claude is None:
        raise Refusal("Claude Code is not installed here: the `claude` command was not found.")
    if claude is not None:
        claude = os.path.abspath(claude)  # a relative PATH entry would miss after the chdir
    if _uses_token(env, home):
        hub = Hub(sheaf, base, token=read_token(env, home))
        # Said every run, so a member who expects the code's prompt knows why none came.
        where = TOKEN_VARIABLE if TOKEN_VARIABLE in env else TOKEN_FILE
        out.write(
            f"Signing in with the session token in {where}, not a sign-in code; "
            "delete it to use a code.\n"
        )
    else:
        # The front first: a code typed for a host that is refused is a code shown for nothing.
        if sheaf.host not in known_fronts(home):
            raise Refusal(f"{sheaf.host} is not a front `tiroir work` knows; nothing was sent.")
        hub = Hub(sheaf, base, code=read_code(env, prompt))
    session_id, body = hub.fetch_session()
    path, kept = store_transcript(projects, folder, session_id, body)
    for aside in kept:
        out.write(f"An older copy of this session was kept as {aside}\n")
    fork_id = str(uuid.uuid4())
    command = ["claude", "--resume", session_id, "--fork-session", "--session-id", fork_id]
    out.write(f"Session {session_id} stored in {path}\n")
    if args.dry_run:
        out.write(f"Dry run: nothing recorded on the sheaf. In {cwd}, run:\n")
        out.write(" ".join(command) + "\n")
        return 0
    hub.record_fork(fork_id, machine_name())
    out.write(f"Fork {fork_id} recorded on {sheaf.doc}; starting Claude in {cwd}.\n")
    out.flush()
    try:
        return _start(claude, command, cwd)
    except OSError as failure:
        raise Refusal(
            f"Claude Code did not start ({failure.strerror}); the fork is recorded, so run "
            f"`{' '.join(command)}` in {cwd}."
        ) from None


def _start(claude: str, command: list[str], cwd: Path) -> int:
    """Become claude in `cwd`. Windows has no exec (`os.execv` starts a new process and ends this
    one, so the shell takes the console back while claude still reads it): there claude runs as a
    child, and its exit code is returned. Ctrl+C reaches both on the console and is claude's to
    answer, so this one keeps waiting."""
    # Claude Code and its tools never see the sign-in code: an explicit environment without it.
    env = {k: v for k, v in os.environ.items() if k != CODE_VARIABLE}
    if not WINDOWS:
        os.chdir(cwd)
        os.execve(claude, command, env)
    child = subprocess.Popen([claude, *command[1:]], cwd=cwd, env=env)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue


if __name__ == "__main__":
    sys.exit(main())
