#!/usr/bin/env bash
set -Eeuo pipefail

# SAS Agent article campaign preflight
#
# Verifies that the host is in a controlled state before running benchmarks.
# The script does not modify system services. It only checks and reports.
#
# Usage:
#   ./scripts/preflight_article.sh
#   ./scripts/preflight_article.sh --wait
#   ./scripts/preflight_article.sh --image sas-agent:article
#   MAX_TEMP_C=65 MAX_LOAD_1M=1.5 ./scripts/preflight_article.sh --wait
#
# Exit codes:
#   0 = all mandatory checks passed
#   1 = one or more mandatory checks failed
#   2 = invalid arguments

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MAX_TEMP_C="${MAX_TEMP_C:-65}"
MAX_LOAD_1M="${MAX_LOAD_1M:-1.5}"
MAX_CPU_BUSY_PCT="${MAX_CPU_BUSY_PCT:-10}"
MIN_AVAILABLE_MEM_MB="${MIN_AVAILABLE_MEM_MB:-4096}"
MIN_FREE_DISK_GB="${MIN_FREE_DISK_GB:-15}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-1800}"
WAIT_INTERVAL_SECONDS="${WAIT_INTERVAL_SECONDS:-15}"

ARTICLE_IMAGE="${ARTICLE_IMAGE:-sas-agent:article}"
MANIFEST_DIR="${MANIFEST_DIR:-artifacts/manifests}"
ALLOW_CONTAINERS_REGEX="${ALLOW_CONTAINERS_REGEX:-^$}"
REQUIRE_AC_POWER="${REQUIRE_AC_POWER:-1}"
REQUIRE_CLEAN_GIT="${REQUIRE_CLEAN_GIT:-1}"
REQUIRE_IMAGE="${REQUIRE_IMAGE:-1}"
REQUIRE_NTP_SYNC="${REQUIRE_NTP_SYNC:-1}"

WAIT_MODE=0
VERBOSE=0

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
FAILURES=()
WARNINGS=()
INFO=()
STARTED_AT_UTC="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
HOSTNAME_VALUE="$(hostname 2>/dev/null || echo unknown)"
SCRIPT_VERSION="1.0.0"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage:
  preflight_article.sh [options]

Options:
  --wait                  Wait until temperature/load/CPU become acceptable.
  --image NAME            Docker image to verify.
  --manifest-dir DIR      Directory for the generated JSON report.
  --allow-containers REGEX
                          Ignore running containers whose names match REGEX.
  --verbose               Print diagnostic details.
  -h, --help              Show this help.

Environment variables:
  MAX_TEMP_C               Maximum CPU Tctl temperature. Default: 65
  MAX_LOAD_1M              Maximum 1-minute load average. Default: 1.5
  MAX_CPU_BUSY_PCT         Maximum host CPU busy percentage. Default: 10
  MIN_AVAILABLE_MEM_MB     Minimum available memory. Default: 4096
  MIN_FREE_DISK_GB         Minimum free disk space. Default: 15
  MAX_WAIT_SECONDS         Maximum wait time in --wait mode. Default: 1800
  WAIT_INTERVAL_SECONDS    Polling interval in --wait mode. Default: 15
  ARTICLE_IMAGE            Docker image to verify. Default: sas-agent:article
  MANIFEST_DIR             Report directory. Default: artifacts/manifests
  ALLOW_CONTAINERS_REGEX   Regex for allowed running container names.
  REQUIRE_AC_POWER         1 to require AC power, 0 to disable. Default: 1
  REQUIRE_CLEAN_GIT        1 to require clean Git tree. Default: 1
  REQUIRE_IMAGE            1 to require Docker image. Default: 1
  REQUIRE_NTP_SYNC         1 to require synchronized clock. Default: 1
EOF
}

log() {
    printf '%s\n' "$*"
}

ok() {
    printf '  [OK]   %s\n' "$*"
}

warn() {
    WARNINGS+=("$*")
    printf '  [WARN] %s\n' "$*"
}

fail() {
    FAILURES+=("$*")
    printf '  [FAIL] %s\n' "$*"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

as_number() {
    local value="${1:-}"
    [[ "$value" =~ ^-?[0-9]+([.][0-9]+)?$ ]]
}

float_le() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a <= b) }'
}

float_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a >= b) }'
}

json_escape() {
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait)
            WAIT_MODE=1
            shift
            ;;
        --image)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            ARTICLE_IMAGE="$2"
            shift 2
            ;;
        --manifest-dir)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            MANIFEST_DIR="$2"
            shift 2
            ;;
        --allow-containers)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            ALLOW_CONTAINERS_REGEX="$2"
            shift 2
            ;;
        --verbose)
            VERBOSE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    printf 'ERROR: run this script inside the Git repository.\n' >&2
    exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
mkdir -p "$MANIFEST_DIR"

REPORT_PATH="$MANIFEST_DIR/preflight-$(date -u +'%Y%m%dT%H%M%SZ').json"

log "=== SAS Agent article preflight ==="
log "Repository: $REPO_ROOT"
log "Host:       $HOSTNAME_VALUE"
log "Started:    $STARTED_AT_UTC"
log

# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------
read_tctl() {
    local value=""

    if have sensors; then
        value="$(
            sensors 2>/dev/null |
            awk '
                /^Tctl:/ {
                    gsub(/[+°C]/, "", $2)
                    print $2
                    exit
                }
            '
        )"
    fi

    if ! as_number "$value"; then
        # Common hwmon fallback for k10temp
        local hwmon
        for hwmon in /sys/class/hwmon/hwmon*; do
            [[ -r "$hwmon/name" ]] || continue
            if [[ "$(cat "$hwmon/name" 2>/dev/null)" == "k10temp" ]]; then
                local label_file input_file label
                for label_file in "$hwmon"/temp*_label; do
                    [[ -r "$label_file" ]] || continue
                    label="$(cat "$label_file" 2>/dev/null)"
                    if [[ "$label" == "Tctl" ]]; then
                        input_file="${label_file%_label}_input"
                        if [[ -r "$input_file" ]]; then
                            value="$(awk '{printf "%.3f", $1/1000}' "$input_file")"
                            break 2
                        fi
                    fi
                done
            fi
        done
    fi

    if as_number "$value"; then
        printf '%s\n' "$value"
        return 0
    fi
    return 1
}

read_cpu_busy_pct() {
    # Two samples separated by one second.
    local a b
    a="$(awk '/^cpu / {print $2,$3,$4,$5,$6,$7,$8,$9,$10}' /proc/stat)"
    sleep 1
    b="$(awk '/^cpu / {print $2,$3,$4,$5,$6,$7,$8,$9,$10}' /proc/stat)"

    awk -v a="$a" -v b="$b" '
        BEGIN {
            split(a, A, " "); split(b, B, " ");
            idle_a = A[4] + A[5];
            idle_b = B[4] + B[5];
            total_a = 0; total_b = 0;
            for (i=1; i<=9; i++) {
                total_a += A[i];
                total_b += B[i];
            }
            dt = total_b - total_a;
            di = idle_b - idle_a;
            if (dt <= 0) {
                print "nan";
            } else {
                printf "%.3f\n", 100 * (dt - di) / dt;
            }
        }
    '
}

read_available_mem_mb() {
    awk '/MemAvailable:/ {printf "%.0f\n", $2/1024}' /proc/meminfo
}

read_swap_io() {
    # Return "si so" from the second vmstat sample.
    if have vmstat; then
        vmstat 1 2 | tail -1 | awk '{print $7, $8}'
    else
        printf 'unknown unknown\n'
    fi
}

read_ac_power() {
    local online_file
    for online_file in /sys/class/power_supply/*/online; do
        [[ -r "$online_file" ]] || continue
        if [[ "$(cat "$online_file" 2>/dev/null)" == "1" ]]; then
            printf 'online\n'
            return 0
        fi
    done

    # Desktop machines may not expose power_supply entries.
    if ! compgen -G '/sys/class/power_supply/*' >/dev/null; then
        printf 'not_applicable\n'
        return 0
    fi

    printf 'offline\n'
    return 1
}

read_ntp_sync() {
    if have timedatectl; then
        timedatectl show -p NTPSynchronized --value 2>/dev/null || true
    fi
}

read_governors() {
    local file
    for file in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        [[ -r "$file" ]] || continue
        cat "$file"
    done | sort -u
}

read_turbo_state() {
    if [[ -r /sys/devices/system/cpu/cpufreq/boost ]]; then
        printf 'cpufreq_boost=%s\n' "$(cat /sys/devices/system/cpu/cpufreq/boost)"
    elif [[ -r /sys/devices/system/cpu/intel_pstate/no_turbo ]]; then
        printf 'intel_no_turbo=%s\n' "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)"
    else
        printf 'unknown\n'
    fi
}

# ---------------------------------------------------------------------------
# Stable-state wait
# ---------------------------------------------------------------------------
wait_for_stability() {
    local deadline=$((SECONDS + MAX_WAIT_SECONDS))

    while true; do
        local temp load cpu
        temp="$(read_tctl 2>/dev/null || echo unknown)"
        load="$(awk '{print $1}' /proc/loadavg)"
        cpu="$(read_cpu_busy_pct)"

        local stable=1

        if ! as_number "$temp" || ! float_le "$temp" "$MAX_TEMP_C"; then
            stable=0
        fi

        if ! float_le "$load" "$MAX_LOAD_1M"; then
            stable=0
        fi

        if ! as_number "$cpu" || ! float_le "$cpu" "$MAX_CPU_BUSY_PCT"; then
            stable=0
        fi

        printf 'Waiting: Tctl=%s°C, load1=%s, CPU busy=%s%%\n' \
            "$temp" "$load" "$cpu"

        if [[ "$stable" == "1" ]]; then
            return 0
        fi

        if (( SECONDS >= deadline )); then
            return 1
        fi

        sleep "$WAIT_INTERVAL_SECONDS"
    done
}

if [[ "$WAIT_MODE" == "1" ]]; then
    log "Waiting for a stable host state..."
    if wait_for_stability; then
        ok "Host reached the configured stability thresholds."
    else
        fail "Host did not stabilize within ${MAX_WAIT_SECONDS}s."
    fi
    log
fi

# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
log "[1/14] Git state"
GIT_COMMIT="$(git rev-parse HEAD)"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
GIT_DIRTY_COUNT="$(git status --porcelain | wc -l | tr -d ' ')"

ok "Commit: $GIT_COMMIT"
ok "Branch: $GIT_BRANCH"

if [[ "$REQUIRE_CLEAN_GIT" == "1" && "$GIT_DIRTY_COUNT" != "0" ]]; then
    fail "Working tree is dirty ($GIT_DIRTY_COUNT changed/untracked files)."
    if [[ "$VERBOSE" == "1" ]]; then
        git status --short
    fi
else
    ok "Working tree is clean."
fi
log

# ---------------------------------------------------------------------------
# AC power
# ---------------------------------------------------------------------------
log "[2/14] Power source"
AC_POWER="$(read_ac_power || true)"
case "$AC_POWER" in
    online)
        ok "AC power is connected."
        ;;
    not_applicable)
        ok "No battery-backed power supply detected; treating as desktop."
        ;;
    *)
        if [[ "$REQUIRE_AC_POWER" == "1" ]]; then
            fail "AC power is not connected."
        else
            warn "AC power is not connected."
        fi
        ;;
esac
log

# ---------------------------------------------------------------------------
# Clock synchronization
# ---------------------------------------------------------------------------
log "[3/14] Clock synchronization"
NTP_SYNC="$(read_ntp_sync)"
if [[ "$NTP_SYNC" == "yes" ]]; then
    ok "System clock is synchronized."
elif [[ "$REQUIRE_NTP_SYNC" == "1" ]]; then
    fail "System clock is not reported as synchronized."
else
    warn "System clock synchronization could not be confirmed."
fi
log

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
log "[4/14] Docker"
DOCKER_VERSION="unknown"
DOCKER_IMAGE_ID="unknown"
DOCKER_IMAGE_DIGEST="unknown"

if ! have docker; then
    fail "Docker CLI is not installed."
elif ! docker info >/dev/null 2>&1; then
    fail "Docker daemon is unavailable."
else
    DOCKER_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
    ok "Docker daemon is available (server $DOCKER_VERSION)."

    RUNNING_NAMES="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
    UNRELATED_NAMES="$(
        if [[ -n "$RUNNING_NAMES" ]]; then
            printf '%s\n' "$RUNNING_NAMES" |
                grep -Ev "$ALLOW_CONTAINERS_REGEX" || true
        fi
    )"

    if [[ -n "$UNRELATED_NAMES" ]]; then
        fail "Unrelated Docker containers are running: $(echo "$UNRELATED_NAMES" | paste -sd, -)"
    else
        ok "No unrelated Docker containers are running."
    fi

    if docker image inspect "$ARTICLE_IMAGE" >/dev/null 2>&1; then
        DOCKER_IMAGE_ID="$(docker image inspect "$ARTICLE_IMAGE" --format '{{.Id}}')"
        DOCKER_IMAGE_DIGEST="$(
            docker image inspect "$ARTICLE_IMAGE" \
                --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}none{{end}}'
        )"
        ok "Docker image found: $ARTICLE_IMAGE"
        ok "Image ID: $DOCKER_IMAGE_ID"
        if [[ "$DOCKER_IMAGE_DIGEST" == "none" ]]; then
            warn "Image has no RepoDigest; record the immutable image ID."
        else
            ok "Image digest: $DOCKER_IMAGE_DIGEST"
        fi
    elif [[ "$REQUIRE_IMAGE" == "1" ]]; then
        fail "Required Docker image was not found: $ARTICLE_IMAGE"
    else
        warn "Docker image was not found: $ARTICLE_IMAGE"
    fi
fi
log

# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------
log "[5/14] CPU temperature"
TCTL_C="$(read_tctl 2>/dev/null || echo unknown)"
if as_number "$TCTL_C"; then
    if float_le "$TCTL_C" "$MAX_TEMP_C"; then
        ok "CPU Tctl: ${TCTL_C}°C (limit ${MAX_TEMP_C}°C)."
    else
        fail "CPU Tctl is ${TCTL_C}°C; limit is ${MAX_TEMP_C}°C."
    fi
else
    fail "Could not read the k10temp Tctl sensor."
fi
log

# ---------------------------------------------------------------------------
# CPU load and activity
# ---------------------------------------------------------------------------
log "[6/14] Host load"
LOAD_1M="$(awk '{print $1}' /proc/loadavg)"
CPU_BUSY_PCT="$(read_cpu_busy_pct)"

if float_le "$LOAD_1M" "$MAX_LOAD_1M"; then
    ok "1-minute load average: $LOAD_1M (limit $MAX_LOAD_1M)."
else
    fail "1-minute load average is $LOAD_1M; limit is $MAX_LOAD_1M."
fi

if as_number "$CPU_BUSY_PCT" && float_le "$CPU_BUSY_PCT" "$MAX_CPU_BUSY_PCT"; then
    ok "CPU busy: ${CPU_BUSY_PCT}% (limit ${MAX_CPU_BUSY_PCT}%)."
else
    fail "CPU busy is ${CPU_BUSY_PCT}%; limit is ${MAX_CPU_BUSY_PCT}%."
fi

if [[ "$VERBOSE" == "1" ]]; then
    log "Top CPU consumers:"
    ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -12
fi
log

# ---------------------------------------------------------------------------
# CPU governor and turbo
# ---------------------------------------------------------------------------
log "[7/14] CPU frequency policy"
GOVERNORS="$(read_governors || true)"
TURBO_STATE="$(read_turbo_state)"

if [[ -z "$GOVERNORS" ]]; then
    warn "CPU governor could not be read."
else
    GOVERNOR_COUNT="$(printf '%s\n' "$GOVERNORS" | sed '/^$/d' | wc -l | tr -d ' ')"
    if [[ "$GOVERNOR_COUNT" == "1" ]]; then
        ok "CPU governor: $GOVERNORS"
    else
        fail "CPU governors are inconsistent across cores: $(echo "$GOVERNORS" | paste -sd, -)"
    fi
fi
ok "Turbo/boost state: $TURBO_STATE"
log

# ---------------------------------------------------------------------------
# Memory and swap
# ---------------------------------------------------------------------------
log "[8/14] Memory and swap"
AVAILABLE_MEM_MB="$(read_available_mem_mb)"
read -r SWAP_IN SWAP_OUT <<<"$(read_swap_io)"

if float_ge "$AVAILABLE_MEM_MB" "$MIN_AVAILABLE_MEM_MB"; then
    ok "Available memory: ${AVAILABLE_MEM_MB} MB."
else
    fail "Available memory is ${AVAILABLE_MEM_MB} MB; minimum is ${MIN_AVAILABLE_MEM_MB} MB."
fi

if [[ "$SWAP_IN" == "unknown" ]]; then
    warn "vmstat is unavailable; swap activity was not checked."
elif [[ "$SWAP_IN" == "0" && "$SWAP_OUT" == "0" ]]; then
    ok "No swap activity detected."
else
    fail "Swap activity detected: si=$SWAP_IN, so=$SWAP_OUT."
fi
log

# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
log "[9/14] Disk capacity"
REPO_FREE_GB="$(df -Pk "$REPO_ROOT" | awk 'NR==2 {printf "%.1f", $4/1024/1024}')"
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
DOCKER_FREE_GB="$(
    if [[ -e "$DOCKER_ROOT" ]]; then
        df -Pk "$DOCKER_ROOT" | awk 'NR==2 {printf "%.1f", $4/1024/1024}'
    else
        echo unknown
    fi
)"

if float_ge "$REPO_FREE_GB" "$MIN_FREE_DISK_GB"; then
    ok "Repository filesystem free: ${REPO_FREE_GB} GB."
else
    fail "Repository filesystem has ${REPO_FREE_GB} GB free; minimum is ${MIN_FREE_DISK_GB} GB."
fi

if as_number "$DOCKER_FREE_GB"; then
    if float_ge "$DOCKER_FREE_GB" "$MIN_FREE_DISK_GB"; then
        ok "Docker filesystem free: ${DOCKER_FREE_GB} GB."
    else
        fail "Docker filesystem has ${DOCKER_FREE_GB} GB free; minimum is ${MIN_FREE_DISK_GB} GB."
    fi
else
    warn "Could not determine free space for Docker root: $DOCKER_ROOT"
fi
log

# ---------------------------------------------------------------------------
# Updates and package-manager activity
# ---------------------------------------------------------------------------
log "[10/14] Background updates"
ACTIVE_UPDATE_UNITS=()

if have systemctl; then
    for unit in \
        unattended-upgrades.service \
        apt-daily.service \
        apt-daily-upgrade.service \
        packagekit.service \
        snapd.service \
        fwupd.service
    do
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            ACTIVE_UPDATE_UNITS+=("$unit")
        fi
    done
fi

if ((${#ACTIVE_UPDATE_UNITS[@]} > 0)); then
    fail "Background update services are active: $(IFS=,; echo "${ACTIVE_UPDATE_UNITS[*]}")"
else
    ok "No known background update service is active."
fi

APT_LOCKED=0
for lock in \
    /var/lib/dpkg/lock-frontend \
    /var/lib/dpkg/lock \
    /var/lib/apt/lists/lock \
    /var/cache/apt/archives/lock
do
    if [[ -e "$lock" ]] && have fuser && fuser "$lock" >/dev/null 2>&1; then
        APT_LOCKED=1
        fail "Package-manager lock is in use: $lock"
    fi
done
if [[ "$APT_LOCKED" == "0" ]]; then
    ok "No package-manager lock is in use."
fi
log

# ---------------------------------------------------------------------------
# Known sync/indexing/background applications
# ---------------------------------------------------------------------------
log "[11/14] Interfering user processes"
INTERFERING_REGEX='(dropbox|onedrive|rclone|syncthing|nextcloud|tracker-miner|baloo_file|updatedb|borg|restic|deja-dup|rsync|chrome|chromium|firefox|code|idea|pycharm|teams|zoom|slack)'
INTERFERING_PROCS="$(
    ps -eo pid=,comm=,args= |
    grep -Ei "$INTERFERING_REGEX" |
    grep -Ev 'grep -E|preflight_article' || true
)"

if [[ -n "$INTERFERING_PROCS" ]]; then
    warn "Potentially interfering processes were detected."
    if [[ "$VERBOSE" == "1" ]]; then
        printf '%s\n' "$INTERFERING_PROCS"
    fi
else
    ok "No common sync, indexing, backup, browser, or IDE process detected."
fi
log

# ---------------------------------------------------------------------------
# I/O pressure
# ---------------------------------------------------------------------------
log "[12/14] I/O pressure"
IO_PRESSURE_AVG10="unknown"
if [[ -r /proc/pressure/io ]]; then
    IO_PRESSURE_AVG10="$(
        awk '/^some / {
            for (i=1; i<=NF; i++) {
                if ($i ~ /^avg10=/) {
                    split($i,a,"=")
                    print a[2]
                    exit
                }
            }
        }' /proc/pressure/io
    )"
    ok "I/O pressure avg10: ${IO_PRESSURE_AVG10}%."
else
    warn "Linux PSI I/O metrics are unavailable."
fi
log

# ---------------------------------------------------------------------------
# CPU throttling hints
# ---------------------------------------------------------------------------
log "[13/14] Kernel warnings and throttling hints"
DMESG_WARNINGS="unavailable"
if dmesg >/dev/null 2>&1; then
    DMESG_WARNINGS="$(
        dmesg --level=err,warn 2>/dev/null |
        tail -200 |
        grep -Ei 'thermal|thrott|mce|hardware error|nvme.*error|i/o error|oom' || true
    )"
    if [[ -n "$DMESG_WARNINGS" ]]; then
        warn "Recent kernel warnings related to thermal, storage, hardware, or OOM were found."
        if [[ "$VERBOSE" == "1" ]]; then
            printf '%s\n' "$DMESG_WARNINGS"
        fi
    else
        ok "No recent relevant kernel warning detected."
    fi
else
    warn "dmesg is not readable by the current user."
fi
log

# ---------------------------------------------------------------------------
# Final reproducibility metadata
# ---------------------------------------------------------------------------
log "[14/14] Reproducibility metadata"
CPU_MODEL="$(lscpu 2>/dev/null | awk -F: '/Model name/ {sub(/^[ \t]+/,"",$2); print $2; exit}')"
LOGICAL_CPUS="$(nproc)"
KERNEL_VERSION="$(uname -r)"
OS_PRETTY="$(
    if [[ -r /etc/os-release ]]; then
        . /etc/os-release
        printf '%s' "${PRETTY_NAME:-unknown}"
    else
        printf 'unknown'
    fi
)"
PYTHON_VERSION="$(python3 --version 2>&1 || echo unavailable)"

ok "CPU: ${CPU_MODEL:-unknown}"
ok "Logical CPUs: $LOGICAL_CPUS"
ok "Kernel: $KERNEL_VERSION"
ok "OS: $OS_PRETTY"
ok "Python: $PYTHON_VERSION"
log

# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------
COMPLETED_AT_UTC="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

export REPORT_PATH SCRIPT_VERSION STARTED_AT_UTC COMPLETED_AT_UTC
export HOSTNAME_VALUE REPO_ROOT GIT_COMMIT GIT_BRANCH GIT_DIRTY_COUNT
export AC_POWER NTP_SYNC DOCKER_VERSION ARTICLE_IMAGE DOCKER_IMAGE_ID
export DOCKER_IMAGE_DIGEST TCTL_C LOAD_1M CPU_BUSY_PCT GOVERNORS TURBO_STATE
export AVAILABLE_MEM_MB SWAP_IN SWAP_OUT REPO_FREE_GB DOCKER_ROOT DOCKER_FREE_GB
export IO_PRESSURE_AVG10 CPU_MODEL LOGICAL_CPUS KERNEL_VERSION OS_PRETTY PYTHON_VERSION
export MAX_TEMP_C MAX_LOAD_1M MAX_CPU_BUSY_PCT MIN_AVAILABLE_MEM_MB MIN_FREE_DISK_GB

FAILURES_TEXT="$(printf '%s\n' "${FAILURES[@]:-}")"
WARNINGS_TEXT="$(printf '%s\n' "${WARNINGS[@]:-}")"
export FAILURES_TEXT WARNINGS_TEXT

python3 <<'PY'
import json
import os
from pathlib import Path

def num(name):
    value = os.environ.get(name, "")
    try:
        return float(value)
    except ValueError:
        return None

def integer(name):
    value = os.environ.get(name, "")
    try:
        return int(value)
    except ValueError:
        return None

def lines(name):
    value = os.environ.get(name, "")
    return [line for line in value.splitlines() if line.strip()]

report = {
    "schema_version": "1.0",
    "script_version": os.environ["SCRIPT_VERSION"],
    "status": "pass" if not lines("FAILURES_TEXT") else "fail",
    "started_at_utc": os.environ["STARTED_AT_UTC"],
    "completed_at_utc": os.environ["COMPLETED_AT_UTC"],
    "host": {
        "hostname": os.environ.get("HOSTNAME_VALUE"),
        "os": os.environ.get("OS_PRETTY"),
        "kernel": os.environ.get("KERNEL_VERSION"),
        "cpu_model": os.environ.get("CPU_MODEL"),
        "logical_cpus": integer("LOGICAL_CPUS"),
        "python": os.environ.get("PYTHON_VERSION"),
    },
    "repository": {
        "root": os.environ.get("REPO_ROOT"),
        "commit": os.environ.get("GIT_COMMIT"),
        "branch": os.environ.get("GIT_BRANCH"),
        "dirty_file_count": integer("GIT_DIRTY_COUNT"),
    },
    "docker": {
        "version": os.environ.get("DOCKER_VERSION"),
        "image": os.environ.get("ARTICLE_IMAGE"),
        "image_id": os.environ.get("DOCKER_IMAGE_ID"),
        "image_digest": os.environ.get("DOCKER_IMAGE_DIGEST"),
        "root_dir": os.environ.get("DOCKER_ROOT"),
    },
    "environment": {
        "ac_power": os.environ.get("AC_POWER"),
        "ntp_synchronized": os.environ.get("NTP_SYNC"),
        "tctl_c": num("TCTL_C"),
        "load_1m": num("LOAD_1M"),
        "cpu_busy_pct": num("CPU_BUSY_PCT"),
        "cpu_governors": lines("GOVERNORS"),
        "turbo_state": os.environ.get("TURBO_STATE"),
        "available_memory_mb": num("AVAILABLE_MEM_MB"),
        "swap_in": num("SWAP_IN"),
        "swap_out": num("SWAP_OUT"),
        "repository_free_gb": num("REPO_FREE_GB"),
        "docker_free_gb": num("DOCKER_FREE_GB"),
        "io_pressure_avg10_pct": num("IO_PRESSURE_AVG10"),
    },
    "thresholds": {
        "max_temp_c": num("MAX_TEMP_C"),
        "max_load_1m": num("MAX_LOAD_1M"),
        "max_cpu_busy_pct": num("MAX_CPU_BUSY_PCT"),
        "min_available_memory_mb": num("MIN_AVAILABLE_MEM_MB"),
        "min_free_disk_gb": num("MIN_FREE_DISK_GB"),
    },
    "failures": lines("FAILURES_TEXT"),
    "warnings": lines("WARNINGS_TEXT"),
}

path = Path(os.environ["REPORT_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
PY

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log "=== Summary ==="
log "Report: $REPORT_PATH"
log "Warnings: ${#WARNINGS[@]}"
log "Failures: ${#FAILURES[@]}"

if ((${#WARNINGS[@]} > 0)); then
    log
    log "Warnings:"
    printf '  - %s\n' "${WARNINGS[@]}"
fi

if ((${#FAILURES[@]} > 0)); then
    log
    log "Preflight FAILED:"
    printf '  - %s\n' "${FAILURES[@]}"
    exit 1
fi

log
log "PRE-FLIGHT PASSED"
log "The host is ready for the controlled benchmark campaign."
exit 0
