#!/usr/bin/env bash
# Update this Pi to the newest commit on its deploy branch, and roll back if it breaks.
#
#   sudo /opt/wth-edge/update.sh          # now: fetch, check out, re-run install.sh
#   /opt/wth-edge/update.sh --auto        # what wth-update.timer runs every few minutes
#
# --auto only acts when the branch has a new commit, waits while the bridge is opening or a
# clip is recording, skips a commit that already failed here, and does nothing when
# `[update] auto = false` in /etc/wth/device.toml. Without --auto it always re-runs
# install.sh (that is `sudo wth-edge update`).
#
# After install.sh it waits for wth-edge.service to stay up. If install.sh fails or the
# service doesn't stay up, it checks the previous commit back out, re-runs install.sh and
# remembers the bad commit so the timer doesn't retry it until the branch moves on.
#
# The branch comes from `[update] branch` in /etc/wth/device.toml (default "deploy").
# Everything lives in main() so bash has read the whole script before git replaces it.
set -euo pipefail

main() {
  local INSTALL_DIR DEVICE_FILE STATE_FILE STATUS_DIR LOCK_FILE TESTING
  INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  DEVICE_FILE="${WTH_DEVICE_FILE:-/etc/wth/device.toml}"
  STATE_FILE="${WTH_STATE_FILE:-/run/wth/state.json}"
  STATUS_DIR="${WTH_UPDATE_DIR:-/var/lib/wth}"
  LOCK_FILE="${WTH_UPDATE_LOCK:-/run/lock/wth-update.lock}"
  # Tests point these at stand-ins and run without root.
  TESTING="${WTH_UPDATE_TESTING:-0}"
  local INSTALL_CMD="${WTH_UPDATE_INSTALL:-$INSTALL_DIR/install.sh}"
  local HEALTH_CMD="${WTH_UPDATE_HEALTH:-}"

  local AUTO=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --auto) AUTO=1; shift ;;
      -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "unknown option: $1" >&2; return 2 ;;
    esac
  done

  if [[ $TESTING != 1 && $EUID -ne 0 ]]; then
    echo "run as root: sudo $0" >&2
    return 1
  fi

  mkdir -p "$STATUS_DIR"

  # One update at a time (the timer and a manual run can overlap).
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "another update is running"
    return 0
  fi

  local BRANCH AUTO_ON HEALTH_WAIT
  BRANCH="$(toml_get "$DEVICE_FILE" update.branch deploy)"
  AUTO_ON="$(toml_get "$DEVICE_FILE" update.auto true)"
  HEALTH_WAIT="$(toml_get "$DEVICE_FILE" update.health_wait_s 45)"
  HEALTH_WAIT="${WTH_UPDATE_HEALTH_WAIT:-$HEALTH_WAIT}"

  if [[ $AUTO -eq 1 && $AUTO_ON != true ]]; then
    log "auto updates are off ([update] auto = false in $DEVICE_FILE)"
    return 0
  fi

  # Run git as whoever owns the checkout, so root never trips git's safe.directory check.
  local OWNER
  OWNER="$(stat -c %U "$INSTALL_DIR")"
  g() {
    if [[ $TESTING != 1 && $OWNER != root && $EUID -eq 0 ]]; then
      runuser -u "$OWNER" -- git -C "$INSTALL_DIR" "$@"
    else
      git -C "$INSTALL_DIR" "$@"
    fi
  }

  if ! g fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" 2>"$STATUS_DIR/.fetch.err"; then
    local err; err="$(tr '\n' ' ' <"$STATUS_DIR/.fetch.err")"; rm -f "$STATUS_DIR/.fetch.err"
    if g ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
      record fetch_failed "" "" "git fetch failed: $err"
    else
      record fetch_failed "" "" "no branch '$BRANCH' on origin (create it, or set [update] branch in $DEVICE_FILE)"
    fi
    return 1
  fi
  rm -f "$STATUS_DIR/.fetch.err"

  local CURRENT TARGET CURRENT_BRANCH
  CURRENT="$(g rev-parse HEAD)"
  TARGET="$(g rev-parse "refs/remotes/origin/$BRANCH")"
  CURRENT_BRANCH="$(g symbolic-ref --quiet --short HEAD || echo detached)"

  if [[ $AUTO -eq 1 ]]; then
    if [[ $CURRENT == "$TARGET" && $CURRENT_BRANCH == "$BRANCH" ]]; then
      record up_to_date "$CURRENT" "" "on $BRANCH at ${CURRENT:0:7}"
      return 0
    fi
    if [[ -f "$STATUS_DIR/update-bad-commit" && "$(cat "$STATUS_DIR/update-bad-commit")" == "$TARGET" ]]; then
      record skipped_bad "$CURRENT" "$TARGET" "${TARGET:0:7} failed here before; waiting for a newer commit on $BRANCH (or run: sudo wth-edge update)"
      return 0
    fi
    local busy
    if busy="$(device_busy "$STATE_FILE")"; then
      record deferred "$CURRENT" "$TARGET" "${TARGET:0:7} waiting: $busy"
      return 0
    fi
  fi

  # Never throw away edits someone made on the Pi. Untracked files (.venv, clips) are fine.
  if [[ -n "$(g status --porcelain --untracked-files=no)" ]]; then
    record dirty "$CURRENT" "$TARGET" "local changes in $INSTALL_DIR; commit or 'git stash' them first: $(g status --porcelain --untracked-files=no | tr '\n' ' ')"
    return 1
  fi

  log "updating $BRANCH: ${CURRENT:0:7} -> ${TARGET:0:7} ($(g log -1 --format=%s "$TARGET"))"
  g checkout --quiet -B "$BRANCH" "$TARGET"
  g branch --quiet --set-upstream-to="origin/$BRANCH" "$BRANCH" >/dev/null 2>&1 || true

  if install_and_check; then
    rm -f "$STATUS_DIR/update-bad-commit"
    record updated "$TARGET" "$CURRENT" "now on $BRANCH at ${TARGET:0:7}"
    return 0
  fi

  # Roll back to what was running before.
  log "update to ${TARGET:0:7} failed; rolling back to ${CURRENT:0:7}"
  echo "$TARGET" >"$STATUS_DIR/update-bad-commit"
  g checkout --quiet -B "$BRANCH" "$CURRENT"
  if install_and_check; then
    record rolled_back "$CURRENT" "$TARGET" "${TARGET:0:7} failed (install or health check); back on ${CURRENT:0:7}"
  else
    record rollback_failed "$CURRENT" "$TARGET" "${TARGET:0:7} failed and so did the rollback to ${CURRENT:0:7}; check journalctl -u wth-update -u wth-edge"
  fi
  return 1
}

log() { printf '%s\n' "$*"; }

# toml_get FILE dotted.key DEFAULT (same as install.sh)
toml_get() {
  python3 - "$1" "$2" "$3" <<'PY'
import sys, tomllib
path, key, default = sys.argv[1:4]
try:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    for part in key.split("."):
        data = data[part]
    print(str(data).lower() if isinstance(data, bool) else data)
except (FileNotFoundError, KeyError, TypeError, tomllib.TOMLDecodeError):
    print(default)
PY
}

# Prints why and succeeds when a restart now would cut something short.
device_busy() {
  python3 - "$1" <<'PY'
import json, sys, time
try:
    with open(sys.argv[1]) as fh:
        st = json.load(fh)
except (OSError, ValueError):
    sys.exit(1)  # not running or no state: nothing to interrupt
if time.time() - float(st.get("updated_at") or 0) > 60:
    sys.exit(1)  # stale state: the app isn't running
phase = (st.get("opening") or {}).get("phase")
if phase not in (None, "flowing", "unknown"):
    print(f"bridge phase is {phase}")
    sys.exit(0)
if (st.get("recording") or {}).get("recording"):
    print("a clip is recording")
    sys.exit(0)
sys.exit(1)
PY
}

# install.sh, then make sure wth-edge.service comes up and stays up.
install_and_check() {
  if ! "$INSTALL_CMD"; then
    log "install.sh failed"
    return 1
  fi
  if [[ -n $HEALTH_CMD ]]; then
    sleep "$HEALTH_WAIT"
    "$HEALTH_CMD"
    return
  fi
  local restarts_before
  restarts_before="$(systemctl show -p NRestarts --value wth-edge.service 2>/dev/null || echo 0)"
  sleep "$HEALTH_WAIT"
  if ! systemctl is-active --quiet wth-edge.service; then
    log "wth-edge.service is not running after ${HEALTH_WAIT}s"
    return 1
  fi
  local restarts_after
  restarts_after="$(systemctl show -p NRestarts --value wth-edge.service 2>/dev/null || echo 0)"
  if [[ $restarts_after != "$restarts_before" ]]; then
    log "wth-edge.service restarted $((restarts_after - restarts_before)) time(s) in ${HEALTH_WAIT}s"
    return 1
  fi
  log "wth-edge.service is up"
}

# record RESULT COMMIT OTHER MESSAGE: log it and keep the last outcome for doctor.
record() {
  local result="$1" commit="$2" other="$3" message="$4"
  log "$message"
  python3 - "$STATUS_DIR/update.json" "$result" "$commit" "$other" "$message" "$BRANCH" <<'PY'
import json, os, sys, time
path, result, commit, other, message, branch = sys.argv[1:7]
try:
    with open(path) as fh:
        data = json.load(fh)
except (OSError, ValueError):
    data = {}
now = time.time()
data.update({"checked_at": now, "branch": branch, "result": result, "message": message})
# commit: what is checked out now. other: the commit replaced, failed or still waiting.
if commit:
    data["commit"] = commit
if result in ("updated", "rolled_back", "rollback_failed"):
    data.update({"changed_at": now, "other_commit": other})
data["pending_commit"] = other if result in ("deferred", "skipped_bad", "dirty") else None
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=1)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

main "$@"
exit $?
