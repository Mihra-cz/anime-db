#!/usr/bin/env bash
# LAB ONLY control script for the isolated AnimeDB V6 laptop lab.
# Touches only /srv/animedb-v6-lab, /run/animedb-v6-lab, Compose projects
# animedb-v6-lab / animedb-v6-lab-smb and the transient unit animedb-v6-lab-coordinator.
# Never uses Nexus paths, production SQLite, production NAS or credentials.
#
#   deploy/lab/lab.sh build              # user: build lab images from this checkout
#   sudo deploy/lab/lab.sh up            # root: first run = setup + data (as you) + start
#   deploy/lab/lab.sh data               # user: (re)generate golden synthetic data
#   sudo deploy/lab/lab.sh setup         # root, once: directories, configs, secrets
#   sudo deploy/lab/lab.sh start         # root: SMB, CIFS mounts, proof, stack, coordinator
#   sudo deploy/lab/lab.sh resume        # root: after reboot, restore SMB/mounts/edge/coordinator only
#   deploy/lab/lab.sh status             # user: read-only status
#   deploy/lab/lab.sh smoke              # user: non-destructive smoke test
#   sudo deploy/lab/lab.sh reset         # root: stop, restore golden DB/library, clear evidence, start
#   sudo deploy/lab/lab.sh hook-mode ok|fail-snapshot|fail-stop-writers|fail-start-writers
#   sudo deploy/lab/lab.sh stop          # root: coordinator, stack, mounts, SMB
#   sudo deploy/lab/lab.sh teardown --yes  # root: stop and remove every lab resource
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB=/srv/animedb-v6-lab
RUN=/run/animedb-v6-lab
PROJECT=animedb-v6-lab
SMB_PROJECT=animedb-v6-lab-smb
UNIT=animedb-v6-lab-coordinator
SHARE=//127.0.0.1/animelab
ORIGIN=http://127.0.0.1:18080
LAB_UID=1000
LAB_GID=1000
EDGE_GID=101
CIFS_OPTS="vers=3.1.1,port=1445,uid=$LAB_UID,gid=$LAB_GID,nosuid,nodev,noexec"

say() { printf '[lab] %s\n' "$*"; }
die() { printf '[lab] STOP: %s\n' "$*" >&2; exit 1; }
need_root() { [[ $EUID -eq 0 ]] || die "run with sudo: sudo $0 $1"; }
need_user() { [[ $EUID -ne 0 ]] || die "run as the regular user (docker group), not root"; }

image_tag() {
    # Content hash of exactly the Docker build inputs (see .dockerignore).
    (cd "$REPO" && find pyproject.toml README.md LICENSE app deploy/nexus/preflight.py deploy/nexus/first_run.py \
        -type f ! -path '*/__pycache__/*' ! -name '*.py[cod]' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
}
# The tag is recomputed from the current build inputs, so a stale image is never used.
lab_image() { printf 'anime-db-lab:v6-%s\n' "$(image_tag)"; }

compose_lab() {
    # Same approved root-owned generation that the coordinator uses.
    local current gen
    current="$LAB/stack/approved-v6/current.json"
    gen="$LAB/stack/approved-v6/$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$current")"
    /usr/bin/docker compose --project-directory "$LAB/stack" --env-file "$gen/.env" -p "$PROJECT" \
        -f "$gen/compose.1.yaml" -f "$gen/compose.2.yaml" -f "$gen/compose.3.yaml" "$@"
}
compose_smb() { /usr/bin/docker compose -p "$SMB_PROJECT" -f "$REPO/deploy/lab/compose.smb.yaml" "$@"; }

cmd_build() {
    need_user build
    local tag
    tag="$(lab_image)"
    say "building $tag (amd64, from this working tree; production SQLite is excluded by .dockerignore)"
    docker build --build-arg APP_UID=$LAB_UID --build-arg APP_GID=$LAB_GID -t "$tag" "$REPO"
    docker build -t animedb-v6-lab-smb:local "$REPO/deploy/lab/smb"
    say "OK $tag"
}

cmd_data() {
    need_user data
    [[ -d $LAB/golden ]] || die "run sudo $0 setup first"
    local stage="$LAB/golden/.generate-$$"
    "$REPO/.venv/bin/python" "$REPO/deploy/lab/make_lab_data.py" --out "$stage"
    rm -rf "$LAB/golden/library" "$LAB/golden/anime.db"
    mv "$stage/library" "$LAB/golden/library"
    mv "$stage/anime.db" "$LAB/golden/anime.db"
    rmdir "$stage"
    say "OK golden data in $LAB/golden"
}

install_dir() { install -d -o "$2" -g "$3" -m "$4" "$1"; }

cmd_setup() {
    need_root setup
    local user_dirs=(appdata/db appdata/state appdata/artwork appdata/backups
        appdata/approval/inbox appdata/approval/outbox appdata/approval/ledger)
    install_dir "$LAB" root root 0755
    for d in stack libexec edge appdata appdata/approval mnt mnt/ro mnt/rw smb; do install_dir "$LAB/$d" root root 0755; done
    for d in "${user_dirs[@]}"; do install_dir "$LAB/$d" $LAB_UID $LAB_GID 0700; done
    install_dir "$LAB/appdata/service" $LAB_UID $LAB_GID 0755
    install_dir "$LAB/smb/share" $LAB_UID $LAB_GID 0755
    install_dir "$LAB/golden" $LAB_UID $LAB_GID 0755
    for d in appdata/coordinator smb/secrets snapshots logs; do install_dir "$LAB/$d" root root 0700; done
    for user in lab_scan lab_worker; do
        if [[ ! -f $LAB/smb/secrets/$user.pass ]]; then
            (umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$LAB/smb/secrets/$user.pass")
            (umask 077; printf 'username=%s\npassword=%s\n' "$user" "$(cat "$LAB/smb/secrets/$user.pass")" \
                > "$LAB/smb/secrets/$user.cred")
        fi
    done
    [[ -f $LAB/stack/lab-hooks.json ]] || install -o root -g root -m 0644 /dev/stdin "$LAB/stack/lab-hooks.json" \
        <<<'{"snapshot":"ok","stop_writers":"ok","start_writers":"ok"}'
    say "OK directories and lab-only secrets under $LAB (no system SMB server, no fstab, no /etc changes)"
}

install_trusted_files() {
    local image="$1"
    install -o root -g root -m 0755 "$REPO/deploy/nexus/reorganization_coordinator.py" "$LAB/libexec/reorganization_coordinator.py"
    install -o root -g root -m 0644 "$REPO/deploy/nexus/reorganization_deployment.py" "$LAB/libexec/reorganization_deployment.py"
    install -o root -g root -m 0644 "$REPO/deploy/lab/lab_entrypoint.py" "$LAB/libexec/lab_entrypoint.py"
    install -o root -g root -m 0755 "$REPO/deploy/lab/lab_capability.py" "$LAB/libexec/lab_capability.py"
    for hook in v6-snapshot v6-stop-writers v6-start-writers; do
        install -o root -g root -m 0755 "$REPO/deploy/lab/hooks/$hook" "$LAB/libexec/$hook"
    done
    for f in nginx.conf index.html service.js; do
        install -o root -g root -m 0644 "$REPO/deploy/nexus/service-edge/$f" "$LAB/edge/$f"
    done
    install -o root -g root -m 0644 "$REPO/deploy/lab/compose.yaml" "$LAB/stack/compose.yaml"
    sed "s|image: LAB_IMAGE|image: $image|" "$REPO/deploy/lab/compose.v6.yaml" \
        | install -o root -g root -m 0644 /dev/stdin "$LAB/stack/compose.v6.yaml"
    printf 'ANIMEDB_UID=%s\nANIMEDB_GID=%s\n' $LAB_UID $LAB_GID | install -o root -g root -m 0644 /dev/stdin "$LAB/stack/.env"
    python3 - "$LAB" "$REPO" "$image" "$SHARE" "$ORIGIN" "$RUN" <<'PY' | install -o root -g root -m 0600 /dev/stdin "$LAB/stack/reorganization-coordinator.json"
import json, sys
lab, repo, image, share, origin, run = sys.argv[1:]
a = lab + '/appdata'
print(json.dumps({'version': 1, 'image': image, 'uid': 1000, 'gid': 1000, 'public_origin': origin,
    'compose_files': [lab + '/stack/compose.yaml', repo + '/deploy/lab/compose.edge.yaml', lab + '/stack/compose.v6.yaml'],
    'worker_config': lab + '/stack/reorganization-worker.json', 'snapshot_hook': lab + '/libexec/v6-snapshot',
    'writers_stop_hook': lab + '/libexec/v6-stop-writers', 'writers_start_hook': lab + '/libexec/v6-start-writers',
    'socket_gid': 101, 'nas_source': share, 'database_dir': a + '/db', 'state_dir': a + '/state',
    'media_ro': lab + '/mnt/ro', 'media_rw': lab + '/mnt/rw', 'inbox': a + '/approval/inbox',
    'outbox': a + '/approval/outbox', 'ledger': a + '/approval/ledger', 'backups': a + '/backups',
    'service_status': a + '/service', 'control_dir': run, 'private_dir': a + '/coordinator',
    'runtime_evidence': lab + '/stack/runtime-evidence.json'}, indent=2))
PY
    python3 - "$SHARE" <<'PY' | install -o root -g root -m 0644 /dev/stdin "$LAB/stack/reorganization-worker.json"
import json, sys
print(json.dumps({'database': '/data/anime.db', 'library_root': '/media/anime', 'share': sys.argv[1],
    'share_root': '/', 'windows_root': '\\\\animedb-lab\\animelab', 'inbox': '/approval-inbox',
    'outbox': '/approval-outbox', 'ledger': '/approval-ledger', 'runtime_lock': '/state/runtime.lock',
    'backups': '/backups', 'service': '/service-status', 'mount_read_write_checked': True,
    'safe_write_capability_proven': True, 'rename_noreplace_capability_proven': True}, indent=2))
PY
}

approve_deployment() {
    # Stage the three sources (repo edge file stays user-owned, like Nexus src/)
    # and approve exactly the staged generation by its manifest SHA.
    local out gen sha
    out="$(cd "$LAB/libexec" && python3 reorganization_deployment.py stage --project-name "$PROJECT" \
        --compose "$LAB/stack/compose.yaml" --compose "$REPO/deploy/lab/compose.edge.yaml" \
        --compose "$LAB/stack/compose.v6.yaml" --env-file "$LAB/stack/.env")"
    gen="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["generation"])' "$out")"
    sha="$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["reviewed_sha256"])' "$out")"
    (cd "$LAB/libexec" && python3 reorganization_deployment.py approve --generation "$gen" --reviewed-sha256 "$sha") >/dev/null
    say "approved deployment generation $(basename "$gen") sha256=$sha"
}

mounted() { findmnt -rn -M "$1" >/dev/null 2>&1; }

mount_views() {
    for _ in $(seq 1 30); do
        smbclient -L 127.0.0.1 -p 1445 -A "$LAB/smb/secrets/lab_scan.cred" -m SMB3 >/dev/null 2>&1 && break; sleep 1
    done
    mounted "$LAB/mnt/ro" || mount -t cifs "$SHARE" "$LAB/mnt/ro" -o "credentials=$LAB/smb/secrets/lab_scan.cred,ro,$CIFS_OPTS"
    mounted "$LAB/mnt/rw" || mount -t cifs "$SHARE" "$LAB/mnt/rw" -o "credentials=$LAB/smb/secrets/lab_worker.cred,rw,$CIFS_OPTS"
}

unmount_views() {
    for view in rw ro; do mounted "$LAB/mnt/$view" && umount "$LAB/mnt/$view"; done
    return 0
}

start_coordinator() {
    systemctl is-active --quiet "$UNIT" && return 0
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    # Same process properties as deploy/nexus/systemd/animedb-reorganization.service.
    systemd-run --unit="$UNIT" --description='AnimeDB V6 LAB coordinator' --collect \
        -p User=root -p Group=$EDGE_GID -p UMask=0077 -p Restart=no -p TimeoutStopSec=30 \
        -p KillMode=control-group -p NoNewPrivileges=yes -p LockPersonality=yes \
        -p 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' \
        -p 'CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER' \
        /usr/bin/python3 "$LAB/libexec/reorganization_coordinator.py" --config "$LAB/stack/reorganization-coordinator.json"
}

cmd_resume() {
    # After a reboot: restore SMB, CIFS views, the edge and the coordinator exactly as left.
    # No reseed, no capability probe (it writes to the library), no new deployment
    # generation and no app start; a STOP state stays inspectable as it is.
    need_root resume
    [[ -f $LAB/stack/approved-v6/current.json ]] || die "no approved deployment; use sudo $0 start"
    install -d -o root -g $EDGE_GID -m 0750 "$RUN"
    compose_smb up -d smb
    mount_views
    compose_lab up -d --no-build --pull=never edge
    start_coordinator
    for _ in $(seq 1 30); do [[ -S $RUN/coordinator.sock ]] && break; sleep 1; done
    [[ -S $RUN/coordinator.sock ]] || die "coordinator socket missing; see journalctl -u $UNIT"
    say "OK resumed without changes; the app was not started (runtime evidence is stale after remount by design)"
}

cmd_start() {
    need_root start
    [[ -f $LAB/stack/lab-hooks.json ]] || die "run sudo $0 setup first"
    [[ -f $LAB/golden/anime.db ]] || die "generate data first: $0 data"
    local image
    image="$(lab_image)"
    docker image inspect "$image" >/dev/null || die "lab image $image missing; run $0 build"
    # First start: seed the working DB and share from the golden copy.
    if [[ ! -f $LAB/appdata/db/anime.db ]]; then
        install -o $LAB_UID -g $LAB_GID -m 0600 "$LAB/golden/anime.db" "$LAB/appdata/db/anime.db"
    fi
    if [[ -z "$(ls -A "$LAB/smb/share")" ]]; then
        cp -a "$LAB/golden/library/." "$LAB/smb/share/"
    fi
    install_trusted_files "$image"
    install -d -o root -g $EDGE_GID -m 0750 "$RUN"
    compose_smb up -d smb
    mount_views
    python3 "$LAB/libexec/lab_capability.py" --rw "$LAB/mnt/rw" --ro "$LAB/mnt/ro" --share "$SHARE" \
        --evidence "$LAB/stack/runtime-evidence.json" --report "$LAB/appdata/coordinator/lab-capability-report.json" >/dev/null
    say "capability proof written for the current RW mount identity"
    approve_deployment
    compose_lab config -q
    compose_lab up -d --no-build --pull=never app edge
    start_coordinator
    for _ in $(seq 1 60); do
        curl -fsS "$ORIGIN/_service/app-health" >/dev/null 2>&1 && [[ -S $RUN/coordinator.sock ]] && break; sleep 1
    done
    curl -fsS "$ORIGIN/_service/app-health" >/dev/null || die "app health through edge not confirmed"
    [[ -S $RUN/coordinator.sock ]] || die "coordinator socket missing; see journalctl -u $UNIT"
    say "OK lab running: open $ORIGIN/reorganization"
}

cmd_stop() {
    need_root stop
    systemctl stop "$UNIT" 2>/dev/null || true
    [[ -f $LAB/stack/approved-v6/current.json ]] && compose_lab down || true
    unmount_views
    compose_smb down || true
    say "OK lab stopped (data, evidence and images kept)"
}

cmd_reset() {
    need_root reset
    [[ -f $LAB/golden/anime.db ]] || die "no golden data"
    cmd_stop
    # Fixed worker containers are kept by design; the lab removes only its own names.
    docker ps -aq --filter 'name=^anime-db-v6-(prepare|start)-[0-9a-f]{64}-' | while read -r id; do
        if docker inspect "$id" --format '{{range .Mounts}}{{println .Source}}{{end}}' | grep -q "^$LAB/"; then
            docker rm -f "$id" >/dev/null
        fi
    done
    find "$LAB/smb/share" -mindepth 1 -delete
    cp -a "$LAB/golden/library/." "$LAB/smb/share/"
    for d in appdata/db appdata/state appdata/artwork appdata/backups appdata/service \
             appdata/approval/inbox appdata/approval/outbox appdata/approval/ledger appdata/coordinator snapshots; do
        find "$LAB/$d" -mindepth 1 -delete
    done
    install -o $LAB_UID -g $LAB_GID -m 0600 "$LAB/golden/anime.db" "$LAB/appdata/db/anime.db"
    rm -f "$LAB/stack/runtime-evidence.json"
    printf '%s\n' '{"snapshot":"ok","stop_writers":"ok","start_writers":"ok"}' > "$LAB/stack/lab-hooks.json"
    say "restored golden DB and library; cleared lab evidence"
    cmd_start
}

cmd_hook_mode() {
    need_root hook-mode
    local s=ok w=ok r=ok
    case "${1:-}" in
        ok) ;; fail-snapshot) s=fail ;; fail-stop-writers) w=fail ;; fail-start-writers) r=fail ;;
        *) die "hook-mode ok|fail-snapshot|fail-stop-writers|fail-start-writers" ;;
    esac
    printf '{"snapshot":"%s","stop_writers":"%s","start_writers":"%s"}\n' $s $w $r > "$LAB/stack/lab-hooks.json"
    say "hook mode: $(cat "$LAB/stack/lab-hooks.json")"
}

cmd_teardown() {
    need_root teardown
    [[ "${1:-}" == --yes ]] || die "teardown deletes $LAB and $RUN; repeat with --yes"
    cmd_stop
    docker ps -aq --filter 'name=^anime-db-v6-(prepare|start)-[0-9a-f]{64}-' | while read -r id; do
        docker inspect "$id" --format '{{range .Mounts}}{{println .Source}}{{end}}' | grep -q "^$LAB/" && docker rm -f "$id" >/dev/null
    done
    mounted "$LAB/mnt/ro" || mounted "$LAB/mnt/rw" && die "lab mounts still active"
    rm -rf --one-file-system "$LAB" "$RUN"
    say "OK removed $LAB and $RUN (images kept: docker image rm $(lab_image) animedb-v6-lab-smb:local)"
}

cmd_status() {
    say "image: $(lab_image) $(docker image inspect "$(lab_image)" >/dev/null 2>&1 && echo present || echo 'NOT BUILT for the current code')"
    for p in "$PROJECT" "$SMB_PROJECT"; do
        docker ps -a --filter "label=com.docker.compose.project=$p" --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
    done
    docker ps -a --filter 'name=^anime-db-v6-' --format '  worker {{.Names}}  {{.Status}}'
    say "coordinator unit: $(systemctl is-active "$UNIT" 2>/dev/null || true)"
    # The regular user is intentionally outside GID 101; check the socket as the edge sees it.
    printf '  socket (seen by edge): '
    docker exec animedb-v6-lab-edge-1 stat -c '%U:%G %a %F' /run/animedb-control/coordinator.sock 2>/dev/null || echo missing
    for view in ro rw; do
        findmnt -rn -M "$LAB/mnt/$view" -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null | cut -c1-140 | sed 's/^/  /' || say "$view not mounted"
    done
    printf '  app-health via edge: '; curl -fsS -m 3 "$ORIGIN/_service/app-health" 2>/dev/null || printf 'unavailable'; echo
    printf '  progress.json: '; curl -fsS -m 3 "$ORIGIN/_service/progress.json" 2>/dev/null | head -c 200 || printf 'none'; echo
}

cmd_smoke() {
    need_user smoke
    "$REPO/.venv/bin/python" "$REPO/deploy/lab/lab_smoke.py"
}

cmd_up() {
    # One sudo invocation for the first run: setup, golden data as the invoking user, start.
    need_root up
    [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]] || die "run via sudo from the regular user account"
    cmd_setup
    [[ -f $LAB/golden/anime.db ]] || runuser -u "$SUDO_USER" -- "$0" data
    cmd_start
}

case "${1:-}" in
    up) cmd_up ;; resume) cmd_resume ;;
    build) cmd_build ;; data) cmd_data ;; setup) cmd_setup ;; start) cmd_start ;; stop) cmd_stop ;;
    reset) cmd_reset ;; hook-mode) cmd_hook_mode "${2:-}" ;; teardown) cmd_teardown "${2:-}" ;;
    status) cmd_status ;; smoke) cmd_smoke ;;
    *) sed -n "2,19p" "$0"; exit 2 ;;
esac
