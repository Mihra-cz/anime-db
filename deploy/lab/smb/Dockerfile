# LAB ONLY isolated SMB server; replaces the Synology NAS for the laptop lab.
FROM alpine@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache samba-server samba-common-tools \
    && addgroup -g 1000 labfiles && adduser -D -H -u 1000 -G labfiles -s /sbin/nologin labfiles \
    && adduser -D -H -u 1101 -s /sbin/nologin lab_scan \
    && adduser -D -H -u 1102 -s /sbin/nologin lab_worker
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /usr/local/bin/lab-smb-entrypoint
ENTRYPOINT ["/bin/sh", "/usr/local/bin/lab-smb-entrypoint"]
