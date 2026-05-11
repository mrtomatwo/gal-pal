#!/usr/bin/env python3
"""Development shim for galpal — `python dev_galpal.py …`.

End users install via `pipx install galpal` and invoke the `galpal` console
script; this file is for working *on* galpal in a clone of the repo.

On a fresh clone:

    python dev_galpal.py init           # creates .venv/, installs deps + dev tools, sets up the pre-commit hook
    python dev_galpal.py init --upgrade # also passes --upgrade to pip (refresh deps to latest matching pins)

`init` always installs both `requirements.txt` (runtime deps) and
`requirements-dev.txt` (ruff / pyright / pytest) and configures the in-repo
pre-commit hook. Idempotent — safe to re-run.

After `init` you use the venv interpreter for everything else:

    .venv/bin/python dev_galpal.py --help
    .venv/bin/python dev_galpal.py pull --dry-run

The wrapper exists so dev work doesn't need a `pipx install` — your edits
in `_galpal/` are picked up immediately by every `dev_galpal.py …`
invocation. Production users get the same code via the wheel + console
script.
"""

import sys

MIN_PYTHON_VERSION = (3, 11)


def _bootstrap_init(argv: list[str]) -> None:
    """Create .venv/, install runtime + dev deps, configure the pre-commit hook.

    Stdlib-only — runs on a fresh clone where third-party deps aren't yet
    installed.
    """
    import argparse
    import os
    import shutil
    import subprocess
    import venv
    from pathlib import Path

    # Preflight 1: Python version. A user on 3.9 hits weird errors deep in msal
    # at runtime; failing fast at the top is much friendlier.
    if sys.version_info < MIN_PYTHON_VERSION:
        actual = f"{sys.version_info.major}.{sys.version_info.minor}"
        wanted = f"{MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}"
        sys.exit(f"galpal requires Python {wanted}+; this interpreter is {actual} ({sys.executable}).")

    parser = argparse.ArgumentParser(
        prog="dev_galpal.py init",
        description=(
            "One-shot dev-environment setup: create .venv/, install runtime + dev "
            "deps, and configure the in-repo pre-commit hook. Idempotent — safe "
            "to re-run."
        ),
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="pass --upgrade to pip (refresh deps to the latest pins)",
    )
    args = parser.parse_args(argv)

    here = Path(__file__).resolve().parent
    venv_dir = here / ".venv"
    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    pip = bin_dir / ("pip.exe" if sys.platform == "win32" else "pip")
    py = bin_dir / ("python.exe" if sys.platform == "win32" else "python")

    # Preflight 2: requirements files exist. A missing requirements.txt
    # produces an opaque pip stack trace; this is friendlier and points at
    # the cause.
    reqs = [here / "requirements.txt", here / "requirements-dev.txt"]
    for r in reqs:
        if not r.exists():
            sys.exit(f"Missing {r}. Re-run from a complete clone.")

    # Preflight 3: detect a broken existing venv. If `.venv/` exists but
    # `bin/pip` is missing (a previous interrupted `init`, manual chmod, etc.)
    # we'd silently fail at `subprocess.run([pip, ...])` below; rm and recreate.
    venv_was_fresh = not venv_dir.exists()
    if venv_dir.exists() and not pip.exists():
        print(f"Existing venv at {venv_dir} is broken (no pip); recreating ...", flush=True)
        shutil.rmtree(venv_dir, ignore_errors=True)
        venv_was_fresh = True
    if venv_was_fresh:
        print(f"Creating venv at {venv_dir} ...", flush=True)
        venv.create(venv_dir, with_pip=True)
    else:
        print(f"Reusing existing venv at {venv_dir}.", flush=True)

    cmd = [str(pip), "install"]
    if args.upgrade:
        cmd.append("--upgrade")
    for r in reqs:
        cmd.extend(["-r", str(r)])
    print(f"Running: {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        # Friendly error + cleanup of half-installed venv (we created it just
        # above, so the caller is no worse off after the cleanup).
        if venv_was_fresh:
            shutil.rmtree(venv_dir, ignore_errors=True)
            cleanup_note = " The half-installed .venv has been removed."
        else:
            cleanup_note = ""
        sys.exit(
            f"pip install failed (exit {e.returncode}). See the error above; "
            f"if it's a transient network issue, re-run `python {sys.argv[0]} init`."
            f"{cleanup_note}"
        )

    # Hooks: always configured. The dev environment is the only place where
    # the in-repo pre-commit hook makes sense (pipx-installed end users
    # neither have a clone nor want pre-commit logic).
    # Verify `here` is actually a git work tree before touching git config —
    # without this, a user who copied dev_galpal.py + the requirements files
    # out of the repo gets a confusing "fatal: not in a git repository"
    # further down.
    is_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=here,
        capture_output=True,
        text=True,
        check=False,
    )
    if is_repo.returncode != 0 or is_repo.stdout.strip() != "true":
        sys.exit(
            f"`init` needs a git work tree at {here}, but `git rev-parse "
            f"--is-inside-work-tree` reports it isn't one. The pre-commit "
            f"hook ships in .githooks/ inside the repo and is wired up via "
            f"`git config --local core.hooksPath`; running this outside "
            f"a clone has nothing to wire to.\n"
            f"If you genuinely want hooks here, `git init` first."
        )
    # Refuse to overwrite an existing core.hooksPath that points somewhere
    # other than `.githooks` — corporate dotfile templates (Husky, lefthook,
    # commit-signing wrappers) often set this, and silently hijacking the
    # value bypasses whatever audit/signing they enforce.
    existing = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=here,
        capture_output=True,
        text=True,
        check=False,
    )
    current = existing.stdout.strip()
    if current and current != ".githooks":
        sys.exit(
            f"Refusing to overwrite existing core.hooksPath={current!r}.\n"
            f"Set it manually if you really want galpal's hook:\n"
            f"  git config --local core.hooksPath .githooks"
        )
    if current != ".githooks":
        print("Configuring git pre-commit hook (core.hooksPath = .githooks) ...", flush=True)
        subprocess.run(["git", "config", "--local", "core.hooksPath", ".githooks"], cwd=here, check=True)

    # Verify the hook script is executable. Windows checkouts, archive
    # extraction, and `chmod -R` can all silently strip the bit; without it
    # git skips the hook on every commit and the user gets the wrong-shaped
    # success signal. `chmod +x` is cheap to run unconditionally — POSIX-only
    # since Windows ignores the executable bit at the filesystem level.
    if sys.platform != "win32":
        hook = here / ".githooks" / "pre-commit"
        if hook.exists() and not os.access(hook, os.X_OK):
            print(f"Making {hook} executable (the bit was missing) ...", flush=True)
            hook.chmod(hook.stat().st_mode | 0o111)

    print()
    print("Done. Next:")
    print(f"  {py} dev_galpal.py --help")
    print(f"  {py} dev_galpal.py pull --dry-run")


def main() -> None:
    """Entry point.

    Handles the `init` bootstrap subcommand and `-v`/`--version` with stdlib
    only, before any import that would require third-party deps. Everything
    else delegates to `_galpal.cli.main()`, which transitively loads
    msal/requests/tqdm.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        _bootstrap_init(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("-v", "--version"):
        # Import the leaf module directly (not `from _galpal import …`) so we
        # don't execute `_galpal/__init__.py`, which on a future refactor might
        # transitively import third-party deps that aren't installed yet.
        from _galpal._version import __version__

        print(f"galpal {__version__}")
        return

    try:
        from _galpal.cli import main as cli_main
    except ImportError as e:
        sys.exit(
            f"Failed to import galpal internals ({e}).\n"
            f"If this is a fresh clone, run `python {sys.argv[0]} init` first."
        )
    cli_main()


if __name__ == "__main__":
    main()
