#!/bin/sh
# LAB ONLY: register the two lab SMB identities from root-owned secret files.
set -eu
for user in lab_scan lab_worker; do
    password="$(cat "/secrets/$user.pass")"
    printf '%s\n%s\n' "$password" "$password" | smbpasswd -a -s "$user" >/dev/null
done
exec smbd --foreground --no-process-group --debug-stdout
