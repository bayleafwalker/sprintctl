#!/usr/bin/env bash
# Stop and remove stale disposable-PostgreSQL clusters left by a hard-killed
# scripts/pg-disposable-tests.sh run. Runs from that script before it creates
# its own dir, and standalone.
#
# Safety contract: docs/guides/postgres-integration-tests.md. It only looks at
# directories named sprintctl-pg.* that the current user owns, directly under
# /tmp and under the tmp base (TMPDIR, or /tmp when TMPDIR is over 60 chars).
#
# Each run writes <dir>/owner = "<pid> <starttime>" right after mktemp, where
# starttime is field 22 of /proc/<pid>/stat. A dir is stale when its owner pid is
# dead, or alive with a different start time (pid reuse). A stale dir's cluster is
# stopped (pg_ctl -D <dir>/data -m immediate stop; then SIGQUIT, then kill -9, each
# only for a process whose cmdline names <dir>/data) and the dir removed, with one
# line printed per swept dir. A dir whose owner is alive is never touched. A dir
# without a usable owner file is reported and left alone. Always exits 0.
set -uo pipefail

# libpq reads every PG* variable; none may redirect pg_ctl.
while IFS= read -r name; do unset "$name"; done < <(compgen -e | grep '^PG' || true)

say() { echo "pg-disposable-sweep: $*"; }

# Start time of a pid (clock ticks since boot): field 22 of /proc/<pid>/stat,
# counted after the last ")" because the command name may contain spaces.
start_ticks() {
  local stat rest
  [ -r "/proc/$1/stat" ] || return 1
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  rest="${stat##*) }"
  local -a fields
  read -ra fields <<<"$rest"
  [ -n "${fields[19]:-}" ] || return 1
  echo "${fields[19]}"
}

# Alive and not a zombie.
pid_alive() {
  local stat rest
  if [ -r "/proc/$1/stat" ]; then
    stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
    rest="${stat##*) }"
    [ "${rest%% *}" != Z ]
  else
    kill -0 "$1" 2>/dev/null
  fi
}

# True when the process cmdline names the data dir as a whole argument.
names_data_dir() {
  local cmd
  [ -r "/proc/$1/cmdline" ] || return 1
  cmd="$(tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null)" || return 1
  case " $cmd " in *" $2 "*) return 0 ;; esac
  return 1
}

wait_gone() {
  local i
  for i in $(seq 1 50); do
    pid_alive "$1" || return 0
    sleep 0.2
  done
  return 1
}

stop_cluster() {
  local dir="$1" data="$1/data" pid="" real
  [ -f "$data/postmaster.pid" ] || { echo "no postmaster.pid"; return; }
  pid="$(head -1 "$data/postmaster.pid" 2>/dev/null)"
  case "$pid" in ''|*[!0-9]*) echo "unreadable postmaster.pid"; return ;; esac
  pid_alive "$pid" || { echo "postmaster $pid already gone"; return; }
  real="$(cd "$data" 2>/dev/null && pwd -P)"
  if ! names_data_dir "$pid" "$data" && ! { [ -n "$real" ] && names_data_dir "$pid" "$real"; }; then
    echo "pid $pid in postmaster.pid is not this data dir's postmaster; left running"
    return
  fi
  if command -v pg_ctl >/dev/null 2>&1; then
    timeout 60 pg_ctl -D "$data" -m immediate -w stop >/dev/null 2>&1
    if ! pid_alive "$pid"; then echo "postmaster $pid stopped with pg_ctl"; return; fi
  fi
  # pg_ctl is missing or failed: SIGQUIT is what -m immediate sends and lets the
  # postmaster release shared memory; kill -9 only when it does not exit.
  kill -QUIT "$pid" 2>/dev/null
  if wait_gone "$pid"; then echo "postmaster $pid stopped with SIGQUIT"; return; fi
  kill -9 "$pid" 2>/dev/null
  wait_gone "$pid" || true
  echo "postmaster $pid killed with SIGKILL"
}

tmp_base="${TMPDIR:-/tmp}"
[ "${#tmp_base}" -le 60 ] || tmp_base=/tmp

seen=""
for base in /tmp "$tmp_base"; do
  [ -d "$base" ] || continue
  base_real="$(cd "$base" && pwd -P)"
  case "$seen" in *"|$base_real|"*) continue ;; esac
  seen="$seen|$base_real|"
  for dir in "$base"/sprintctl-pg.*; do
    [ -d "$dir" ] && [ ! -L "$dir" ] || continue
    [ -O "$dir" ] || continue
    owner_file="$dir/owner"
    if [ ! -f "$owner_file" ]; then
      say "SKIP $dir: no owner file, left alone"
      continue
    fi
    owner_pid="" owner_start=""
    read -r owner_pid owner_start _ <"$owner_file" || true
    case "${owner_pid:-}" in ''|*[!0-9]*)
      say "SKIP $dir: unreadable owner file, left alone"
      continue ;;
    esac
    reason=""
    if ! pid_alive "$owner_pid"; then
      reason="owner pid $owner_pid is dead"
    else
      live_start="$(start_ticks "$owner_pid" || true)"
      case "${owner_start:-}:$live_start" in
        *[!0-9:]*|:*|*:) ;;  # start time unknown on either side: treat as alive
        *) [ "$owner_start" = "$live_start" ] || reason="owner pid $owner_pid was reused (start time $live_start, recorded $owner_start)" ;;
      esac
    fi
    if [ -z "$reason" ]; then
      say "KEEP $dir: owner pid $owner_pid is alive"
      continue
    fi
    outcome="$(stop_cluster "$dir")"
    rm -rf "$dir"
    say "swept $dir: $reason; $outcome"
  done
done
exit 0
