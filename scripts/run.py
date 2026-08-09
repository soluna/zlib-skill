#!/usr/bin/env python3
"""Bootstrap and launch the bundled zlib-skill engine."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from zlib_anna import SKILL_VERSION, schema

SCRIPTS_DIR = Path(__file__).resolve().parent
LOCK_FILE = SCRIPTS_DIR / "requirements.lock"
READY_FILE = ".ready.json"
RUNTIME_DIR_ENVS = ("ZLIB_SKILL_RUNTIME_DIR", "ZLIB_ANNA_RUNTIME_DIR")
MINIMUM_PYTHON = (3, 9)
SETUP_TIMEOUT_SECONDS = 300
ENGINE_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from zlib_anna.engine import main; raise SystemExit(main())"
)


class RuntimeSetupError(Exception):
    """A sanitized runtime setup failure."""

    def __init__(self, step: str, cause: BaseException):
        super().__init__("The bundled runtime could not be prepared.")
        self.step = step
        self.error_type = type(cause).__name__


def runtime_root() -> Path:
    override = next((os.environ[name] for name in RUNTIME_DIR_ENVS if os.environ.get(name)), None)
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "zlib-skill" / "Cache"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "zlib-skill"


def runtime_fingerprint() -> str:
    try:
        lock_data = LOCK_FILE.read_bytes()
    except OSError as exc:
        raise RuntimeSetupError("read_dependency_lock", exc) from None
    identity = (
        f"{SKILL_VERSION}|{sys.implementation.name}|"
        f"{sys.version_info.major}.{sys.version_info.minor}|"
    ).encode()
    return hashlib.sha256(identity + lock_data).hexdigest()


def runtime_path() -> Path:
    python_tag = f"py{sys.version_info.major}{sys.version_info.minor}"
    return runtime_root() / f"{python_tag}-{runtime_fingerprint()[:16]}"


def runtime_python(runtime: Path) -> Path:
    if os.name == "nt":
        return runtime / "Scripts" / "python.exe"
    return runtime / "bin" / "python"


def runtime_is_ready(runtime: Path) -> bool:
    marker = runtime / READY_FILE
    python = runtime_python(runtime)
    if not marker.is_file() or not python.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return payload == {"fingerprint": runtime_fingerprint()}


@contextmanager
def runtime_lock(runtime: Path) -> Iterator[None]:
    lock_path = runtime.parent / f".{runtime.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_setup_step(command: list[str], step: str) -> None:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SETUP_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise RuntimeSetupError(step, exc) from None
    if result.returncode:
        raise RuntimeSetupError(step, RuntimeError("setup command failed"))


def build_runtime(runtime: Path) -> None:
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        runtime.parent.chmod(0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{runtime.name}-", dir=runtime.parent))
    step = "create_environment"
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(temporary)
        python = runtime_python(temporary)
        step = "install_dependencies"
        _run_setup_step(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--require-hashes",
                "--only-binary=:all:",
                "-r",
                str(LOCK_FILE),
            ],
            step,
        )
        step = "verify_dependencies"
        _run_setup_step(
            [str(python), "-I", "-c", "import requests; from bs4 import BeautifulSoup"],
            step,
        )
        (temporary / READY_FILE).write_text(
            json.dumps({"fingerprint": runtime_fingerprint()}),
            encoding="utf-8",
        )
        if runtime.exists():
            shutil.rmtree(runtime)
        temporary.replace(runtime)
    except RuntimeSetupError:
        raise
    except Exception as exc:
        raise RuntimeSetupError(step, exc) from None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def ensure_runtime() -> Path:
    if sys.version_info < MINIMUM_PYTHON:
        raise RuntimeSetupError("check_python", RuntimeError("unsupported Python"))
    runtime = runtime_path()
    if runtime_is_ready(runtime):
        return runtime_python(runtime)
    try:
        with runtime_lock(runtime):
            if not runtime_is_ready(runtime):
                print("Preparing the zlib-skill runtime...", file=sys.stderr)
                build_runtime(runtime)
    except RuntimeSetupError:
        raise
    except Exception as exc:
        raise RuntimeSetupError("lock_runtime", exc) from None
    if not runtime_is_ready(runtime):
        raise RuntimeSetupError("verify_runtime", RuntimeError("runtime is incomplete"))
    return runtime_python(runtime)


def runtime_error_payload(error: RuntimeSetupError) -> dict[str, object]:
    return schema.failure_envelope(
        code="RUNTIME_SETUP_FAILED",
        message="The bundled Skill runtime could not be prepared.",
        recoverable=True,
        suggestions=[
            "Check that Python 3.9+ can create virtual environments.",
            "Check network access to the configured Python package index, then retry.",
        ],
        details={"step": error.step, "error_type": error.error_type},
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments == ["--version"]:
        print(f"zlib-skill {SKILL_VERSION}")
        return 0
    try:
        python = ensure_runtime()
    except RuntimeSetupError as exc:
        if "--json" in arguments:
            print(json.dumps(runtime_error_payload(exc), ensure_ascii=False, indent=2))
        else:
            print(f"Error [RUNTIME_SETUP_FAILED]: {exc}", file=sys.stderr)
        return 1
    command = [
        str(python),
        "-I",
        "-c",
        ENGINE_BOOTSTRAP,
        str(SCRIPTS_DIR),
        *arguments,
    ]
    os.execve(str(python), command, os.environ.copy())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
