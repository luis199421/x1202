#!/bin/sh
# One-time setup: creates the key that signs the apt repository.
#
#   sh packaging/make-signing-key.sh
#
# Writes the PUBLIC key to packaging/apt-repo-key.asc (commit that file) and the
# PRIVATE key to a file you add as the GitHub secret APT_SIGNING_KEY, then delete.
# Run it on your own computer or the Pi, not on a shared machine.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
if [ -f "$HERE/apt-repo-key.asc" ]; then
    echo "packaging/apt-repo-key.asc already exists: the key was made before."
    echo "Delete it first only if you really want a new key (Pis that installed the old"
    echo "one will need the next version installed by hand to get the new key)."
    exit 1
fi
GNUPGHOME=$(mktemp -d /tmp/pi5gpg.XXXXXX)   # short path: gpg-agent socket limit
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT
gpg --batch --quiet --passphrase '' \
    --quick-gen-key "Pi 5 Energy Plan apt repository" ed25519 sign never
gpg --batch --armor --export > "$HERE/apt-repo-key.asc"
SECRET=$(mktemp "${TMPDIR:-/tmp}/pi5-energyplan-signing-key.XXXXXX")
gpg --batch --armor --export-secret-keys > "$SECRET"
chmod 600 "$SECRET"
cat <<MSG
Done.

1. Add the private key as a GitHub secret named APT_SIGNING_KEY:
   GitHub → your repository → Settings → Secrets and variables → Actions →
   New repository secret. Paste the whole content of:
       $SECRET
   (or: gh secret set APT_SIGNING_KEY < "$SECRET")
   Then delete that file:  rm "$SECRET"

2. Commit and push the public key:
       git add packaging/apt-repo-key.asc && git commit -m "Add apt repository key" && git push

3. Follow "Publishing a new version" in packaging/README.md.
MSG
