"""update.sh against throwaway git repos: a bare "origin" and a clone standing in for /opt/wth-edge.

install.sh and the health check are stand-ins: install fails when the checked-out tree has a
BROKEN file, health fails when it has UNHEALTHY, so rollbacks can be driven by commits.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from wth_edge import config as config_mod
from wth_edge.doctor import OK, WARN, update_check

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not shutil.which("git") or not shutil.which("flock"), reason="needs git and flock")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


class Pi:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.dev = tmp / "dev"          # where "merges" happen
        self.install_dir = tmp / "opt"  # the Pi's checkout
        self.status_dir = tmp / "var"
        self.device_file = tmp / "device.toml"
        self.state_file = tmp / "state.json"
        self.install_log = tmp / "install.log"

        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.dev)], check=True, capture_output=True)
        shutil.copy(REPO / "update.sh", self.dev / "update.sh")
        (self.dev / "app.txt").write_text("v1\n")
        git(self.dev, "add", ".")
        git(self.dev, "commit", "-qm", "v1")
        git(self.dev, "push", "-q", "origin", "HEAD:main", "HEAD:deploy")
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(self.install_dir)], check=True, capture_output=True)

        install = tmp / "fake-install.sh"
        install.write_text(f'#!/bin/sh\ngit -C "{self.install_dir}" rev-parse HEAD >> "{self.install_log}"\n'
                           f'[ ! -e "{self.install_dir}/BROKEN" ]\n')
        health = tmp / "fake-health.sh"
        health.write_text(f'#!/bin/sh\n[ ! -e "{self.install_dir}/UNHEALTHY" ]\n')
        for f in (install, health):
            f.chmod(0o755)
        self.env = {
            **os.environ, **GIT_ENV,
            "WTH_UPDATE_TESTING": "1",
            "WTH_DEVICE_FILE": str(self.device_file),
            "WTH_STATE_FILE": str(self.state_file),
            "WTH_UPDATE_DIR": str(self.status_dir),
            "WTH_UPDATE_LOCK": str(tmp / "update.lock"),
            "WTH_UPDATE_INSTALL": str(install),
            "WTH_UPDATE_HEALTH": str(health),
            "WTH_UPDATE_HEALTH_WAIT": "0",
        }

    def merge(self, name: str, branch: str = "deploy", remove: tuple[str, ...] = (), **files: str) -> str:
        """Commit on top of origin/<branch> and push, like merging a PR."""
        git(self.dev, "checkout", "-q", "-B", branch, f"origin/{branch}")
        for fname, body in files.items():
            (self.dev / fname).write_text(body)
        for fname in remove:
            git(self.dev, "rm", "-q", fname)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", name)
        git(self.dev, "push", "-q", "origin", f"HEAD:{branch}")
        git(self.dev, "fetch", "-q", "origin")
        return git(self.dev, "rev-parse", "HEAD")

    def update(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.install_dir / "update.sh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def head(self) -> str:
        return git(self.install_dir, "rev-parse", "HEAD")

    def branch(self) -> str:
        return git(self.install_dir, "symbolic-ref", "--short", "HEAD")

    def installs(self) -> list[str]:
        return self.install_log.read_text().split() if self.install_log.exists() else []

    def status(self) -> dict:
        return json.loads((self.status_dir / "update.json").read_text())


@pytest.fixture
def pi(tmp_path: Path) -> Pi:
    return Pi(tmp_path)


def test_first_run_switches_from_main_to_deploy(pi: Pi):
    r = pi.update("--auto")
    assert r.returncode == 0, r.stdout + r.stderr
    assert pi.branch() == "deploy"
    assert len(pi.installs()) == 1
    assert pi.status()["result"] == "updated"


def test_up_to_date_does_nothing(pi: Pi):
    pi.update("--auto")
    r = pi.update("--auto")
    assert r.returncode == 0
    assert len(pi.installs()) == 1
    assert pi.status()["result"] == "up_to_date"


def test_merge_to_deploy_is_installed(pi: Pi):
    pi.update("--auto")
    new = pi.merge("v2", **{"app.txt": "v2\n"})
    r = pi.update("--auto")
    assert r.returncode == 0, r.stdout + r.stderr
    assert pi.head() == new
    assert pi.installs()[-1] == new
    assert "v2" in r.stdout


def test_main_does_not_deploy(pi: Pi):
    pi.update("--auto")
    before = pi.head()
    pi.merge("only on main", branch="main", **{"app.txt": "main only\n"})
    pi.update("--auto")
    assert pi.head() == before


def test_failed_install_rolls_back_and_is_not_retried(pi: Pi):
    pi.update("--auto")
    good = pi.head()
    bad = pi.merge("broken", BROKEN="x")
    r = pi.update("--auto")
    assert r.returncode == 1
    assert pi.head() == good and pi.branch() == "deploy"
    assert pi.installs()[-2:] == [bad, good]
    st = pi.status()
    assert st["result"] == "rolled_back" and st["other_commit"] == bad

    r = pi.update("--auto")
    assert r.returncode == 0
    assert pi.status()["result"] == "skipped_bad"
    assert pi.installs()[-1] == good  # no new install

    fixed = pi.merge("fix", remove=("BROKEN",))
    assert pi.update("--auto").returncode == 0
    assert pi.head() == fixed
    assert not (pi.status_dir / "update-bad-commit").exists()


def test_unhealthy_service_rolls_back(pi: Pi):
    pi.update("--auto")
    good = pi.head()
    pi.merge("crashes", UNHEALTHY="x")
    assert pi.update("--auto").returncode == 1
    assert pi.head() == good
    assert pi.status()["result"] == "rolled_back"


def test_waits_while_the_bridge_is_opening(pi: Pi):
    pi.update("--auto")
    before = pi.head()
    pending = pi.merge("v2", **{"app.txt": "v2\n"})
    pi.state_file.write_text(json.dumps({"updated_at": time.time(), "opening": {"phase": "lifting"}}))
    r = pi.update("--auto")
    assert r.returncode == 0
    assert pi.head() == before
    st = pi.status()
    assert st["result"] == "deferred" and st["pending_commit"] == pending
    assert "lifting" in st["message"]

    pi.state_file.write_text(json.dumps({"updated_at": time.time(), "opening": {"phase": "flowing"},
                                         "recording": {"recording": True}}))
    pi.update("--auto")
    assert pi.head() == before

    # Stale state means the app isn't running: nothing to interrupt.
    pi.state_file.write_text(json.dumps({"updated_at": time.time() - 600, "opening": {"phase": "lifting"}}))
    pi.update("--auto")
    assert pi.head() == pending


def test_auto_off(pi: Pi):
    pi.device_file.write_text('[update]\nauto = false\n')
    r = pi.update("--auto")
    assert r.returncode == 0
    assert pi.installs() == []
    assert "off" in r.stdout


def test_manual_update_always_reinstalls(pi: Pi):
    pi.update("--auto")
    r = pi.update()
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(pi.installs()) == 2


def test_manual_update_ignores_the_bad_commit_list_and_busy_state(pi: Pi):
    pi.update("--auto")
    bad = pi.merge("broken", BROKEN="x")
    pi.update("--auto")
    pi.state_file.write_text(json.dumps({"updated_at": time.time(), "opening": {"phase": "raised"}}))
    pi.update()
    assert pi.installs()[-2] == bad  # it tried again


def test_other_branch_from_device_file(pi: Pi):
    pi.device_file.write_text('[update]\nbranch = "main"\n')
    new = pi.merge("main change", branch="main", **{"app.txt": "main\n"})
    assert pi.update("--auto").returncode == 0
    assert pi.branch() == "main" and pi.head() == new


def test_missing_branch(pi: Pi):
    pi.device_file.write_text('[update]\nbranch = "nope"\n')
    r = pi.update("--auto")
    assert r.returncode == 1
    assert "no branch 'nope'" in r.stdout
    assert pi.installs() == []


def test_local_edits_are_never_overwritten(pi: Pi):
    pi.update("--auto")
    pi.merge("v2", **{"app.txt": "v2\n"})
    (pi.install_dir / "app.txt").write_text("edited on the pi\n")
    r = pi.update("--auto")
    assert r.returncode == 1
    assert (pi.install_dir / "app.txt").read_text() == "edited on the pi\n"
    assert pi.status()["result"] == "dirty"


def test_doctor_reports_the_last_update(tmp_path: Path):
    cfg = config_mod.Config()
    cfg.paths.data_dir = str(tmp_path)
    level, _, detail = update_check(cfg)
    assert level == WARN and "no check yet" in detail

    (tmp_path / "update.json").write_text(json.dumps({"result": "updated", "branch": "deploy", "commit": "abcdef123", "message": "now on deploy"}))
    level, _, detail = update_check(cfg)
    assert level == OK and "abcdef1" in detail

    (tmp_path / "update.json").write_text(json.dumps({"result": "rolled_back", "branch": "deploy", "commit": "abcdef123", "message": "x"}))
    assert update_check(cfg)[0] == WARN

    cfg.update.auto = False
    assert update_check(cfg)[0] == OK
