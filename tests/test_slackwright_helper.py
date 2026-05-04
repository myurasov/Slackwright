# Copyright 2026 Mikhail Yurasov
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Regression tests for the top-level ``./slackwright`` helper script."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "slackwright"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _seed_dep_entrypoints(
    venv: Path,
    *,
    with_driver: bool = True,
    with_cli: bool = True,
) -> None:
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("python", "playwright", "pytest", "ruff"):
        _write_executable(bin_dir / name, "#!/usr/bin/env bash\n")
    if with_driver:
        driver_dir = venv / "lib" / "python3.12" / "site-packages" / "playwright" / "driver"
        driver_dir.mkdir(parents=True, exist_ok=True)
        _write_executable(driver_dir / "node", "#!/usr/bin/env bash\n")
        if with_cli:
            package_dir = driver_dir / "package"
            package_dir.mkdir(parents=True, exist_ok=True)
            package_dir.joinpath("cli.js").write_text("#!/usr/bin/env node\n")


def _fake_venv(tmp_path: Path) -> tuple[Path, Path, Path]:
    venv = tmp_path / "venv"
    bin_dir = venv / "bin"
    package_dir = tmp_path / "fakepkg"
    (package_dir / "playwright").mkdir(parents=True)
    bin_dir.mkdir(parents=True)

    (package_dir / "playwright" / "__init__.py").write_text("")
    (package_dir / "playwright" / "sync_api.py").write_text(
        """
from __future__ import annotations

import os


class _BrowserType:
    @property
    def executable_path(self) -> str:
        return os.environ["FAKE_BROWSER_EXECUTABLE"]


class _SyncPlaywright:
    chromium = _BrowserType()

    def __enter__(self) -> "_SyncPlaywright":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def sync_playwright() -> _SyncPlaywright:
    return _SyncPlaywright()
""".lstrip()
    )

    _write_executable(
        bin_dir / "python",
        """#!/usr/bin/env bash
export PYTHONPATH="${FAKE_PLAYWRIGHT_PACKAGE}${PYTHONPATH:+:${PYTHONPATH}}"
exec "${REAL_PYTHON}" "$@"
""",
    )
    _write_executable(
        bin_dir / "playwright",
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_PLAYWRIGHT_INSTALL_LOG}"
""",
    )
    return venv, package_dir, tmp_path / "playwright-install.log"


def _run_ensure_browser(
    *,
    tmp_path: Path,
    venv: Path,
    package_dir: Path,
    install_log: Path,
    browser_executable: Path,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to test the slackwright helper script")

    command = "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(HELPER))}",
            f"VENV_DIR={shlex.quote(str(venv))}",
            "PW_BROWSER=chromium",
            "ensure_browser",
        ]
    )
    env = os.environ.copy()
    env.update(
        {
            "FAKE_BROWSER_EXECUTABLE": str(browser_executable),
            "FAKE_PLAYWRIGHT_INSTALL_LOG": str(install_log),
            "FAKE_PLAYWRIGHT_PACKAGE": str(package_dir),
            "REAL_PYTHON": sys.executable,
        }
    )
    return subprocess.run(
        [bash, "-c", command],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_ensure_deps(
    *,
    tmp_path: Path,
    venv: Path,
    install_stamp: Path,
    uv_log: Path,
    fail_first: bool = False,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to test the slackwright helper script")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_UV_LOG}"
if [ "$1" = "venv" ]; then
  target="${@: -1}"
  mkdir -p "$target/bin"
  exit 0
fi
if [ "${FAKE_UV_FAIL_FIRST:-}" = "1" ] && [ ! -f "${FAKE_UV_LOG}.failed" ]; then
  touch "${FAKE_UV_LOG}.failed"
  exit 1
fi
if [ "$1" = "pip" ]; then
  mkdir -p "${FAKE_VENV}/bin"
  for name in python playwright pytest ruff; do
    printf '#!/usr/bin/env bash\n' > "${FAKE_VENV}/bin/${name}"
    chmod +x "${FAKE_VENV}/bin/${name}"
  done
  mkdir -p "${FAKE_VENV}/lib/python3.12/site-packages/playwright/driver/package"
  printf '#!/usr/bin/env bash\n' > "${FAKE_VENV}/lib/python3.12/site-packages/playwright/driver/node"
  chmod +x "${FAKE_VENV}/lib/python3.12/site-packages/playwright/driver/node"
  printf '#!/usr/bin/env node\n' > "${FAKE_VENV}/lib/python3.12/site-packages/playwright/driver/package/cli.js"
fi
""",
    )
    command = "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(HELPER))}",
            f"VENV_DIR={shlex.quote(str(venv))}",
            f"INSTALL_STAMP={shlex.quote(str(install_stamp))}",
            "ensure_deps",
        ]
    )
    env = os.environ.copy()
    env.update(
        {
            "FAKE_UV_FAIL_FIRST": "1" if fail_first else "0",
            "FAKE_UV_LOG": str(uv_log),
            "FAKE_VENV": str(venv),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    return subprocess.run(
        [bash, "-c", command],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_ensure_deps_resyncs_when_install_stamp_is_stale(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    install_stamp = venv / ".slackwright-installed"
    install_stamp.touch()
    uv_log = tmp_path / "uv.log"

    result = _run_ensure_deps(
        tmp_path=tmp_path,
        venv=venv,
        install_stamp=install_stamp,
        uv_log=uv_log,
    )

    assert result.returncode == 0, result.stderr
    assert uv_log.read_text() == "pip install -e .[dev] --quiet\n"


def test_ensure_deps_resyncs_when_playwright_driver_is_missing(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    _seed_dep_entrypoints(venv, with_driver=False)
    install_stamp = venv / ".slackwright-installed"
    install_stamp.touch()
    uv_log = tmp_path / "uv.log"

    result = _run_ensure_deps(
        tmp_path=tmp_path,
        venv=venv,
        install_stamp=install_stamp,
        uv_log=uv_log,
    )

    assert result.returncode == 0, result.stderr
    assert uv_log.read_text() == "pip install -e .[dev] --quiet\n"
    assert (
        venv / "lib" / "python3.12" / "site-packages" / "playwright" / "driver" / "node"
    ).exists()


def test_ensure_deps_resyncs_when_playwright_cli_payload_is_missing(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    _seed_dep_entrypoints(venv, with_cli=False)
    install_stamp = venv / ".slackwright-installed"
    install_stamp.touch()
    uv_log = tmp_path / "uv.log"

    result = _run_ensure_deps(
        tmp_path=tmp_path,
        venv=venv,
        install_stamp=install_stamp,
        uv_log=uv_log,
    )

    assert result.returncode == 0, result.stderr
    assert uv_log.read_text() == "pip install -e .[dev] --quiet\n"
    assert (
        venv
        / "lib"
        / "python3.12"
        / "site-packages"
        / "playwright"
        / "driver"
        / "package"
        / "cli.js"
    ).exists()


def test_ensure_deps_recreates_venv_when_sync_fails(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    sentinel = bin_dir / "stale"
    sentinel.touch()
    install_stamp = venv / ".slackwright-installed"
    install_stamp.touch()
    uv_log = tmp_path / "uv.log"

    result = _run_ensure_deps(
        tmp_path=tmp_path,
        venv=venv,
        install_stamp=install_stamp,
        uv_log=uv_log,
        fail_first=True,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert uv_log.read_text().splitlines() == [
        "pip install -e .[dev] --quiet",
        f"venv --python 3.12 {venv}",
        "pip install -e .[dev] --quiet",
    ]


def test_ensure_browser_installs_when_executable_path_is_missing(tmp_path: Path) -> None:
    venv, package_dir, install_log = _fake_venv(tmp_path)
    browser_executable = tmp_path / "missing-browser"

    result = _run_ensure_browser(
        tmp_path=tmp_path,
        venv=venv,
        package_dir=package_dir,
        install_log=install_log,
        browser_executable=browser_executable,
    )

    assert result.returncode == 0, result.stderr
    assert install_log.read_text() == "install chromium\n"


def test_ensure_browser_skips_install_when_executable_is_present(tmp_path: Path) -> None:
    venv, package_dir, install_log = _fake_venv(tmp_path)
    browser_executable = tmp_path / "browser"
    _write_executable(browser_executable, "#!/usr/bin/env bash\n")

    result = _run_ensure_browser(
        tmp_path=tmp_path,
        venv=venv,
        package_dir=package_dir,
        install_log=install_log,
        browser_executable=browser_executable,
    )

    assert result.returncode == 0, result.stderr
    assert not install_log.exists()
