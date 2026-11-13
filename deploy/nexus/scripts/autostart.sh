#!/bin/bash
# Audited Nexus autostart, with only the Compose ingress moved to approval.
set -euo pipefail
NAS=/mnt/nas-anime

echo "AnimeDB: checking NAS"
timeout 20 stat "$NAS/." >/dev/null
mount_data=$(
    findmnt --kernel --raw --noheadings \
        --mountpoint "$NAS" --types cifs \
        --output SOURCE,VFS-OPTIONS
)
read -r source options <<< "$mount_data"
[[ "$source" == "//192.168.11.149/Anime" ]] || {
    echo "STOP: unexpected NAS source" >&2
    exit 1
}
[[ ",$options," == *,ro,* ]] || {
    echo "STOP: NAS is not mounted read-only" >&2
    exit 1
}

echo "AnimeDB: checking LAN firewall"
/usr/local/sbin/animedb-lan-fw apply
/usr/sbin/iptables -w 10 -C DOCKER-USER \
    ! -s 192.168.11.0/24 -p tcp \
    -m conntrack \
    --ctorigdst 192.168.11.238 \
    --ctorigdstport 8000 \
    --ctdir ORIGINAL \
    -m comment --comment animedb-lan-only \
    -j DROP

echo "AnimeDB: starting approved production containers"
/usr/bin/python3 /usr/local/libexec/anime-db/reorganization_autostart.py start
deadline=$((SECONDS + 900))
while (( SECONDS < deadline )); do
    if curl -fsS --max-time 2 \
        http://192.168.11.238:8000/health \
        2>/dev/null | grep -q '"status":"ok"'; then
        echo "AnimeDB: START PASS"
        exit 0
    fi
    sleep 2
done
echo "STOP: AnimeDB health check failed" >&2
exit 1
