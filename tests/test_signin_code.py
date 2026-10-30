"""`tiroir work` with a hub-issued sign-in code (tt-tiroir#60, sheaf SPEC 9.13).

The code comes from a hidden prompt when stdin is a terminal, or from `TIROIR_SIGNIN_CODE`;
never from argv, a URL or a file. It goes as exactly one `Authorization: Bearer` line, only to a
front the client knows, and is never printed. The variable is removed before Claude Code starts.
The session token path (`TIROIR_FRONT_TOKEN`, `~/.config/tiroir/front-token`) stays until Hans's
acceptance (decision 5).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_work import TRANSCRIPT, transcript_path

import tiroir.work as handover
from tiroir.work import main

CODE = "sheaf_code_" + "Ab1-_" * 8 + "xyz"
FRONT = "thinkingtoo.tiroir.me"
URL = f"https://{FRONT}/list/thinkingtoo/tiroir/2026-10-28-contract.md"
NO_GOOD = "This sign-in code is no good: get a new one from Work on this."
NOT_OPEN = (
    "This sheaf is not open to you with this code: get a code from this sheaf's Work on this."
)


class Prompt:
    """Stands in for the hidden prompt: records what it was asked and answers `answer`."""

    def __init__(self, answer: str = CODE) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def __call__(self, text: str) -> str:
        self.asked.append(text)
        return self.answer


def run(home: Path, *argv: str, env: dict | None = None, prompt=None):
    out, err = io.StringIO(), io.StringIO()
    environment = {"HOME": str(home), "USERPROFILE": str(home)}
    environment.update(env or {})
    environment = {k: v for k, v in environment.items() if v is not None}
    code = main(list(argv), out=out, err=err, env=environment, prompt=prompt)
    return code, out.getvalue(), err.getvalue()


def dry(home, hub, cwd, url=URL, **kw):
    return run(home, "work", url, "--hub", hub.url, "--cwd", str(cwd), "--dry-run", **kw)


def test_the_code_comes_from_a_hidden_prompt_and_goes_as_one_bearer_line(tmp_path, hub, work_dir):
    prompt = Prompt()
    code, out, err = dry(tmp_path, hub, work_dir, prompt=prompt)

    assert code == 0, err
    assert prompt.asked == ["Sign-in code (Work on this > Get a sign-in code): "]
    (get,) = hub.gets()
    assert get["headers"]["authorization"] == f"Bearer {CODE}"
    assert "cookie" not in get["headers"]
    assert CODE not in out + err
    assert transcript_path(tmp_path, work_dir).read_bytes() == TRANSCRIPT


def test_the_code_is_read_from_the_variable_without_a_prompt(tmp_path, hub, work_dir):
    prompt = Prompt("never asked")
    code, _out, err = dry(tmp_path, hub, work_dir, env={"TIROIR_SIGNIN_CODE": CODE}, prompt=prompt)

    assert code == 0, err
    assert prompt.asked == []
    assert hub.gets()[0]["headers"]["authorization"] == f"Bearer {CODE}"


def test_the_code_is_sent_on_both_calls_and_the_fork_is_recorded(
    tmp_path, hub, work_dir, monkeypatch
):
    started = []
    monkeypatch.setattr(
        handover, "_start", lambda claude, command, cwd: started.append(command) or 0
    )
    monkeypatch.setattr(handover.shutil, "which", lambda *a, **k: "/usr/bin/claude")
    code, _out, err = run(
        tmp_path, "work", URL, "--hub", hub.url, "--cwd", str(work_dir), prompt=Prompt()
    )

    assert code == 0, err
    assert [r["headers"].get("authorization") for r in hub.requests] == [f"Bearer {CODE}"] * 2
    assert hub.posts()[0]["headers"]["origin"] == f"https://{FRONT}"
    assert started


@pytest.mark.parametrize(
    "typed",
    ["", "sheaf_code_short", "sk-1234567890", CODE + "x", CODE[:-1] + "=", " " + CODE + "\nX: y"],
)
def test_anything_but_one_code_of_the_right_shape_is_refused_before_anything_is_sent(
    tmp_path, hub, work_dir, typed
):
    code, out, err = dry(tmp_path, hub, work_dir, prompt=Prompt(typed))

    assert code == 2
    assert "That is not a sign-in code" in err
    assert hub.requests == []
    if typed:
        assert typed.strip() not in out + err


def test_spaces_around_a_pasted_code_are_dropped(tmp_path, hub, work_dir):
    code, _out, err = dry(tmp_path, hub, work_dir, prompt=Prompt(f"  {CODE}\n"))

    assert code == 0, err


def test_a_host_that_is_not_a_known_front_gets_nothing(tmp_path, hub, work_dir):
    prompt = Prompt()
    code, _out, err = dry(
        tmp_path,
        hub,
        work_dir,
        url="https://thinkingtoo.tiroir.me.evil.example/l/a/b/c.md",
        prompt=prompt,
    )

    assert code == 2
    assert (
        "thinkingtoo.tiroir.me.evil.example is not a front `tiroir work` knows; nothing was sent."
        in err
    )
    assert prompt.asked == []
    assert hub.requests == []


def test_a_front_named_in_the_config_is_known(tmp_path, hub, work_dir):
    config = tmp_path / ".config" / "tiroir" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('fronts = ["chat.example.com"]\n', encoding="utf-8")

    code, _out, err = dry(
        tmp_path, hub, work_dir, url="https://chat.example.com/l/a/b/c.md", prompt=Prompt()
    )

    assert code == 0, err


def test_a_config_that_does_not_parse_says_so_and_sends_nothing(tmp_path, hub, work_dir):
    config = tmp_path / ".config" / "tiroir" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("fronts = [chat.example.com\n", encoding="utf-8")

    code, _out, err = dry(
        tmp_path, hub, work_dir, url="https://chat.example.com/l/a/b/c.md", prompt=Prompt()
    )

    assert code == 2
    assert str(config) in err
    assert hub.requests == []


def test_without_a_terminal_or_the_variable_it_says_where_the_code_comes_from(
    tmp_path, hub, work_dir
):
    code, _out, err = dry(tmp_path, hub, work_dir)  # no prompt: tests run without a terminal

    assert code == 2
    assert "Get a sign-in code" in err
    assert "TIROIR_SIGNIN_CODE" in err
    assert hub.requests == []


@pytest.mark.parametrize("status, line", [(401, NO_GOOD), (404, NOT_OPEN), (403, NOT_OPEN)])
def test_the_download_refusals_have_their_own_sentences(tmp_path, hub, work_dir, status, line):
    hub.session_status = status
    code, out, err = dry(tmp_path, hub, work_dir, prompt=Prompt())

    assert code == 2
    assert line in err
    assert CODE not in out + err


def test_a_hub_that_is_down_is_said_as_today(tmp_path, hub, work_dir):
    hub.session_status = 502
    code, _out, err = dry(tmp_path, hub, work_dir, prompt=Prompt())

    assert code == 2
    assert f"The hub at {FRONT} answered 502; nothing was changed." in err


def record(tmp_path, hub, work_dir, monkeypatch):
    monkeypatch.setattr(handover, "_start", lambda claude, command, cwd: 0)
    monkeypatch.setattr(handover.shutil, "which", lambda *a, **k: "/usr/bin/claude")
    return run(tmp_path, "work", URL, "--hub", hub.url, "--cwd", str(work_dir), prompt=Prompt())


def test_a_refused_fork_gives_the_hubs_reason_and_asks_for_a_new_code(
    tmp_path, hub, work_dir, monkeypatch
):
    hub.sessions_status = 409
    hub.sessions_answer = {"ok": False, "error": "That session is already listed on this sheaf"}
    code, _out, err = record(tmp_path, hub, work_dir, monkeypatch)

    assert code == 2
    assert "That session is already listed on this sheaf" in err
    assert "get a new code to try again" in err


@pytest.mark.parametrize("status, line", [(401, NO_GOOD), (404, NOT_OPEN)])
def test_the_fork_refusals_have_the_codes_sentences(
    tmp_path, hub, work_dir, monkeypatch, status, line
):
    hub.sessions_status = status
    hub.sessions_answer = {"ok": False}
    code, _out, err = record(tmp_path, hub, work_dir, monkeypatch)

    assert code == 2
    assert line in err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a /bin/sh fake claude; Windows is covered by the Popen fake in test_work.py",
)
def test_the_variable_is_removed_before_claude_starts(tmp_path, hub, work_dir):
    """A real exec, through a subprocess, with a fake `claude` that writes down its environment."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen-env"
    fake = bin_dir / "claude"
    fake.write_text(f"#!/bin/sh\nenv > {seen}\n", encoding="utf-8")
    fake.chmod(0o755)
    script = (
        "import sys; from tiroir.work import main; "
        f"sys.exit(main(['work', {URL!r}, '--hub', {hub.url!r}, '--cwd', {str(work_dir)!r}]))"
    )
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TIROIR_SIGNIN_CODE": CODE,
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert "TIROIR_SIGNIN_CODE" not in seen.read_text(encoding="utf-8")
    assert CODE not in seen.read_text(encoding="utf-8")
    assert CODE not in done.stdout + done.stderr


def test_the_session_token_path_is_unchanged_while_it_lasts(tmp_path, hub, work_dir):
    token = "eyJhbGciOiJIUzI1NiJ9.SECRET-TOKEN-7f3a.sig"
    prompt = Prompt("never asked")
    code, _out, err = dry(tmp_path, hub, work_dir, env={"TIROIR_FRONT_TOKEN": token}, prompt=prompt)

    assert code == 0, err
    assert prompt.asked == []
    assert "Signing in with the session token in TIROIR_FRONT_TOKEN, not a sign-in code" in _out
    (get,) = hub.gets()
    assert get["headers"]["cookie"] == f"token={token}"
    assert "authorization" not in get["headers"]


def test_a_code_and_a_token_together_use_the_code_only(tmp_path, hub, work_dir):
    token = "eyJhbGciOiJIUzI1NiJ9.SECRET-TOKEN-7f3a.sig"
    code, _out, err = dry(
        tmp_path, hub, work_dir, env={"TIROIR_FRONT_TOKEN": token, "TIROIR_SIGNIN_CODE": CODE}
    )

    assert code == 0, err
    (get,) = hub.gets()
    assert get["headers"]["authorization"] == f"Bearer {CODE}"
    assert "cookie" not in get["headers"]


def test_the_code_never_goes_in_the_url(tmp_path, hub, work_dir):
    dry(tmp_path, hub, work_dir, prompt=Prompt())

    assert all(CODE not in r["path"] for r in hub.requests)


# --- a stored token the hub refuses (tiroir-client#7) ------------------------------------------

TOKEN = "eyJhbGciOiJIUzI1NiJ9.SECRET-TOKEN-7f3a.sig"


def test_a_refused_token_in_the_variable_falls_back_to_the_code(tmp_path, hub, work_dir):
    hub.refuse_cookie = True
    prompt = Prompt()
    code, out, err = dry(tmp_path, hub, work_dir, env={"TIROIR_FRONT_TOKEN": TOKEN}, prompt=prompt)

    assert code == 0, err
    assert "refused the session token in TIROIR_FRONT_TOKEN" in out
    assert prompt.asked == ["Sign-in code (Work on this > Get a sign-in code): "]
    first, second = hub.gets()
    assert first["headers"]["cookie"] == f"token={TOKEN}"
    assert second["headers"]["authorization"] == f"Bearer {CODE}"
    assert "cookie" not in second["headers"]
    assert TOKEN not in out + err
    assert CODE not in out + err
    assert transcript_path(tmp_path, work_dir).read_bytes() == TRANSCRIPT


def test_a_refused_token_file_is_left_in_place_and_the_code_is_asked(tmp_path, hub, work_dir):
    path = tmp_path / ".config" / "tiroir" / "front-token"
    path.parent.mkdir(parents=True)
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    hub.refuse_cookie = True
    prompt = Prompt()
    code, out, err = dry(tmp_path, hub, work_dir, prompt=prompt)

    assert code == 0, err
    assert "refused the session token in ~/.config/tiroir/front-token" in out
    assert len(prompt.asked) == 1
    assert hub.gets()[1]["headers"]["authorization"] == f"Bearer {CODE}"
    assert path.read_text() == TOKEN + "\n"
    assert TOKEN not in out + err


def test_a_refused_token_without_a_terminal_says_where_the_code_comes_from(tmp_path, hub, work_dir):
    hub.refuse_cookie = True
    code, out, err = dry(tmp_path, hub, work_dir, env={"TIROIR_FRONT_TOKEN": TOKEN})

    assert code == 2
    assert "refused the session token" in out
    assert "No sign-in code" in err
    assert len(hub.requests) == 1
    assert TOKEN not in out + err


def test_a_refused_token_on_an_unknown_front_asks_for_no_code(tmp_path, hub, work_dir):
    hub.refuse_cookie = True
    prompt = Prompt("never asked")
    url = "https://hub.example.com/tt/acme/site/plan.md"
    code, out, err = dry(
        tmp_path, hub, work_dir, url=url, env={"TIROIR_FRONT_TOKEN": TOKEN}, prompt=prompt
    )

    assert code == 2
    assert "hub.example.com is not a front `tiroir work` knows; nothing was sent." in err
    assert prompt.asked == []
    assert len(hub.requests) == 1
    assert TOKEN not in out + err


def test_after_a_refused_token_the_fork_is_recorded_with_the_code(
    tmp_path, hub, work_dir, monkeypatch
):
    started = []
    monkeypatch.setattr(
        handover, "_start", lambda claude, command, cwd: started.append(command) or 0
    )
    monkeypatch.setattr(handover.shutil, "which", lambda *a, **k: "/usr/bin/claude")
    hub.refuse_cookie = True
    code, out, err = run(
        tmp_path,
        "work",
        URL,
        "--hub",
        hub.url,
        "--cwd",
        str(work_dir),
        env={"TIROIR_FRONT_TOKEN": TOKEN},
        prompt=Prompt(),
    )

    assert code == 0, err
    (post,) = hub.posts()
    assert post["headers"]["authorization"] == f"Bearer {CODE}"
    assert "cookie" not in post["headers"]
    assert started
    assert TOKEN not in out + err
