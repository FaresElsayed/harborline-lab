#!/bin/sh
# Starts as root for one job. If a bind-mounted folder is missing, Docker recreates it owned by root, and the app, which
# runs as a normal user, could then never write there. On Windows that survives restarts, so it is repaired on every start.
uid="${HL_UID:-10001}"
gid="${HL_GID:-10001}"
if [ "$(id -u)" != "0" ]; then
  exec "$@"
fi
as_app="setpriv --reuid=$uid --regid=$gid --clear-groups"
for dir in /data/landing /data/answer_key; do
  if ! $as_app test -w "$dir"; then
    chown "$uid:$gid" "$dir" || echo "Could not take ownership of $dir" >&2
  fi
done
exec $as_app "$@"
