"""Tests for the development shim `dev_galpal.py`.

Covers the two stdlib-only entry points:
  - `init` (and its single `--upgrade` flag)
  - `--version` / `-v`

Both must work on a fresh clone where third-party deps aren't installed yet,
so the tests stub `venv.create` and `subprocess.run` to keep them hermetic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load the top-level dev_galpal.py wrapper as a module under a non-conflicting
# name so we don't shadow the `_galpal` package's namespace.
_spec = importlib.util.spec_from_file_location("galpal_wrapper", PROJECT_ROOT / "dev_galpal.py")
assert _spec is not None and _spec.loader is not None
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)


# --------------------------------------------------------------------------- main() dispatch


def test_main_intercepts_init_before_importing_internals(monkeypatch):
    """`dev_galpal.py init` must dispatch to _bootstrap_init without ever
    importing `_galpal.cli` (which would transitively pull in third-party deps
    that may not be installed yet on a fresh clone)."""
    captured: dict = {}
    monkeypatch.setattr(wrapper, "_bootstrap_init", lambda argv: captured.setdefault("argv", argv))
    monkeypatch.setattr("sys.argv", ["dev_galpal.py", "init", "--upgrade"])

    # Sentinel that would explode if main() ever falls through to the cli import.
    err = AssertionError("cli.main was reached when init should have short-circuited")

    def must_not_be_called(*a, **kw):
        raise err

    monkeypatch.setattr("_galpal.cli.main", must_not_be_called, raising=False)

    wrapper.main()
    assert captured["argv"] == ["--upgrade"]


def test_main_handles_version_flag(monkeypatch, capsys):
    from _galpal._version import __version__

    monkeypatch.setattr("sys.argv", ["dev_galpal.py", "--version"])
    wrapper.main()
    out = capsys.readouterr().out
    assert f"galpal {__version__}" in out


def test_main_handles_short_v_flag(monkeypatch, capsys):
    from _galpal._version import __version__

    monkeypatch.setattr("sys.argv", ["dev_galpal.py", "-v"])
    wrapper.main()
    out = capsys.readouterr().out
    assert f"galpal {__version__}" in out


# --------------------------------------------------------------------------- _bootstrap_init


@pytest.fixture
def bootstrap_env(tmp_path, monkeypatch):
    """Hermetic fixture for `_bootstrap_init`.

    Writes minimal requirements files, points the wrapper's `__file__` at the
    tmpdir so `here` resolves there, and stubs `venv.create` + `subprocess.run`
    (both imported lazily inside the function) by patching the stdlib modules
    they resolve to. Pretends the tmpdir is a git work tree so the
    hooks-config path can be exercised.
    """
    import subprocess as _subprocess
    import venv as _venv

    (tmp_path / "requirements.txt").write_text("msal>=1.36\n")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\nruff==0.15.12\n")

    monkeypatch.setattr(wrapper, "__file__", str(tmp_path / "dev_galpal.py"))

    venv_calls: list[Path] = []
    monkeypatch.setattr(_venv, "create", lambda d, **kw: venv_calls.append(d))

    run_calls: list[list[str]] = []

    def fake_run(cmd, *, check=True, **kw):
        run_calls.append(list(cmd))
        # Tmpdir isn't a real git repo; pretend it is so init can reach the hooks step.
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return SimpleNamespace(stdout="true\n", returncode=0)
        # No core.hooksPath set yet — return empty + non-zero like real git does.
        if cmd[:5] == ["git", "config", "--local", "--get", "core.hooksPath"]:
            return SimpleNamespace(stdout="", returncode=1)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(_subprocess, "run", fake_run)

    return SimpleNamespace(tmp=tmp_path, venv_calls=venv_calls, run_calls=run_calls)


def test_bootstrap_creates_venv_and_installs_runtime_plus_dev_deps(bootstrap_env):
    """The default (and only) install mode: venv + both requirements files + hook config.

    There are no `--dev` / `--hooks` flags any more — the dev bootstrap always
    does both. This test pins that contract.
    """
    wrapper._bootstrap_init([])
    # Created exactly one venv at tmp/.venv.
    assert len(bootstrap_env.venv_calls) == 1
    assert bootstrap_env.venv_calls[0] == bootstrap_env.tmp / ".venv"

    pip_call = next(c for c in bootstrap_env.run_calls if "install" in c)
    reqs_in_call = [pip_call[i + 1] for i, x in enumerate(pip_call) if x == "-r"]
    # Both requirements files are pulled in unconditionally.
    assert any(r.endswith("requirements.txt") for r in reqs_in_call)
    assert any(r.endswith("requirements-dev.txt") for r in reqs_in_call)
    # Hash-pinning was removed in favor of pyproject's version ceilings —
    # the hook should NOT pass --require-hashes.
    assert "--require-hashes" not in pip_call
    # And the hooks were configured.
    assert any(c[:4] == ["git", "config", "--local", "core.hooksPath"] for c in bootstrap_env.run_calls)


def test_bootstrap_upgrade_passes_through_to_pip(bootstrap_env):
    """`--upgrade` is the one remaining flag — refreshes deps to latest matching pins."""
    wrapper._bootstrap_init(["--upgrade"])
    pip_call = next(c for c in bootstrap_env.run_calls if "install" in c)
    assert "--upgrade" in pip_call


def test_bootstrap_fails_when_requirements_file_missing(bootstrap_env):
    """A missing requirements.txt would otherwise produce a confusing pip
    stack trace ("file not found" surrounded by pip's noisy resolver output).
    Better to fail loudly here with a clear message."""
    (bootstrap_env.tmp / "requirements.txt").unlink()
    with pytest.raises(SystemExit) as exc:
        wrapper._bootstrap_init([])
    assert "Missing" in str(exc.value)
    assert "requirements.txt" in str(exc.value)


def test_bootstrap_fails_outside_git_work_tree(bootstrap_env, monkeypatch):
    """If the wrapper directory isn't inside a git work tree, hook config can't be
    wired. Fail fast with a clear message instead of letting `git config` surface
    its own opaque error further down."""
    import subprocess as _subprocess

    real_run = bootstrap_env.run_calls

    def not_a_repo_run(cmd, *, check=True, **kw):
        real_run.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return SimpleNamespace(stdout="", returncode=128)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(_subprocess, "run", not_a_repo_run)

    with pytest.raises(SystemExit) as exc:
        wrapper._bootstrap_init([])
    assert "git work tree" in str(exc.value)
    # And no `git config --local core.hooksPath .githooks` write was made.
    assert not any(c[:4] == ["git", "config", "--local", "core.hooksPath"] for c in real_run)


def test_bootstrap_refuses_to_clobber_existing_hookspath(bootstrap_env, monkeypatch):
    """If core.hooksPath is already pointing at a non-galpal value, refuse and exit
    instead of silently bypassing the user's existing audit/signing hooks."""
    import subprocess as _subprocess

    real_run = bootstrap_env.run_calls

    def existing_hookspath_run(cmd, *, check=True, **kw):
        real_run.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return SimpleNamespace(stdout="true\n", returncode=0)
        if cmd[:5] == ["git", "config", "--local", "--get", "core.hooksPath"]:
            return SimpleNamespace(stdout=".husky\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(_subprocess, "run", existing_hookspath_run)

    with pytest.raises(SystemExit) as exc:
        wrapper._bootstrap_init([])
    assert "Refusing to overwrite" in str(exc.value)
    assert ".husky" in str(exc.value)
    # And no `git config --local core.hooksPath .githooks` write was made.
    assert not any(c[:4] == ["git", "config", "--local", "core.hooksPath"] for c in real_run)


def test_bootstrap_skips_hook_write_when_already_correctly_configured(bootstrap_env, monkeypatch):
    """If core.hooksPath is already `.githooks` (a re-run of `init`), don't
    re-write it. Idempotent — no spurious `git config` writes on repeat runs."""
    import subprocess as _subprocess

    real_run = bootstrap_env.run_calls

    def already_configured_run(cmd, *, check=True, **kw):
        real_run.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return SimpleNamespace(stdout="true\n", returncode=0)
        if cmd[:5] == ["git", "config", "--local", "--get", "core.hooksPath"]:
            return SimpleNamespace(stdout=".githooks\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(_subprocess, "run", already_configured_run)

    wrapper._bootstrap_init([])
    # No write — only the read of the existing value.
    write_calls = [c for c in real_run if c[:4] == ["git", "config", "--local", "core.hooksPath"]]
    assert write_calls == []


def test_bootstrap_reuses_existing_venv(bootstrap_env):
    """A venv that already exists AND has a working bin/pip is reused — venv.create
    is not called, but pip install still runs (handles `init --upgrade` etc.)."""
    venv_dir = bootstrap_env.tmp / ".venv"
    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    pip_name = "pip.exe" if sys.platform == "win32" else "pip"
    (bin_dir / pip_name).touch()  # mark the venv as healthy

    wrapper._bootstrap_init([])
    assert bootstrap_env.venv_calls == []
    assert any("install" in c for c in bootstrap_env.run_calls)


def test_bootstrap_recreates_broken_venv(bootstrap_env):
    """If `.venv/` exists but `bin/pip` is missing (interrupted prior init,
    manual chmod), don't silently fail at `subprocess.run([pip, ...])`. Detect
    the breakage and recreate."""
    venv_dir = bootstrap_env.tmp / ".venv"
    venv_dir.mkdir()  # exists but empty — no bin/pip

    wrapper._bootstrap_init([])
    # Recreated: venv.create was called.
    assert len(bootstrap_env.venv_calls) == 1
    assert bootstrap_env.venv_calls[0] == venv_dir


def test_bootstrap_refuses_old_python(bootstrap_env, monkeypatch):
    """Running `init` on Python < MIN_PYTHON_VERSION fails fast with a clear
    message. Without this, msal/typing-syntax errors would surface deep in
    the user's first real run.

    Implementation: bump MIN_PYTHON_VERSION above the running interpreter
    rather than fake `sys.version_info`, which is hard to mock with the right
    shape across Python versions.
    """
    monkeypatch.setattr(wrapper, "MIN_PYTHON_VERSION", (99, 0))
    with pytest.raises(SystemExit) as exc:
        wrapper._bootstrap_init([])
    assert "requires Python 99.0" in str(exc.value)


def test_bootstrap_cleans_up_half_created_venv_on_pip_failure(bootstrap_env, monkeypatch):
    """If pip install fails on a freshly-created venv, remove the half-installed
    venv so the next `init` starts clean. Without this, the next run reuses a
    broken venv and pip install fails again with no clear hint."""
    import subprocess as _subprocess

    def failing_run(cmd, *, check=True, **kw):
        if "install" in cmd:
            raise _subprocess.CalledProcessError(returncode=1, cmd=cmd)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(_subprocess, "run", failing_run)
    with pytest.raises(SystemExit) as exc:
        wrapper._bootstrap_init([])
    assert "pip install failed" in str(exc.value)
    # The venv we just created should have been removed.
    assert not (bootstrap_env.tmp / ".venv").exists()
