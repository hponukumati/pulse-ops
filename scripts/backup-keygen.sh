#!/usr/bin/env sh
# Create the backup encryption key pair (age X25519).
#   identity (DECRYPTION secret) -> $PULSEOPS_BACKUP_KEY_DIR (default ~/.pulseops-backup-keys), outside the repo
#   recipient (public)           -> infra/backup-keys/recipient.txt (used by backup.sh; gitignored)
# Keep the identity separate from the backups (e.g. a password manager or offline media).
set -eu
cd "$(dirname "$0")/.."
KEYDIR=${PULSEOPS_BACKUP_KEY_DIR:-$HOME/.pulseops-backup-keys}
[ -f "$KEYDIR/identity.txt" ] && { echo "identity already exists at $KEYDIR/identity.txt" >&2; exit 1; }
umask 077; mkdir -p "$KEYDIR" infra/backup-keys
docker compose --profile tools build -q backup-tools >/dev/null
docker compose --profile tools run --rm -T --no-deps backup-tools age-keygen 2>/dev/null > "$KEYDIR/identity.txt"
grep '^# public key: ' "$KEYDIR/identity.txt" | sed 's/^# public key: //' > infra/backup-keys/recipient.txt
# Public key: must be readable by backup.sh's container user, which differs from the file owner on Linux.
chmod 644 infra/backup-keys/recipient.txt
echo "identity: $KEYDIR/identity.txt (keep secret, separate from backups)"
echo "recipient: infra/backup-keys/recipient.txt"
