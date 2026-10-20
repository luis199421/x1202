#!/bin/sh
# Adds .deb files to the apt repository and signs it (used by the release workflow).
#
#   APT_SIGNING_KEY="$(cat private-key.asc)" sh packaging/publish-apt-repo.sh DEB_DIR REPO_DIR
#
# REPO_DIR keeps every published version (pool/main) and gets fresh, signed indexes:
#   dists/stable/{Release,InRelease,Release.gpg}, dists/stable/main/binary-*/Packages(.gz)
# Needs apt-ftparchive (apt-utils) and gpg.
set -e
DEBS=$(cd "$1" && pwd)
mkdir -p "$2"
REPO=$(cd "$2" && pwd)
[ -n "${APT_SIGNING_KEY:-}" ] || { echo "APT_SIGNING_KEY is empty (see packaging/README.md)"; exit 1; }

mkdir -p "$REPO/pool/main"
cp "$DEBS"/*.deb "$REPO/pool/main/"

# The package is "all": its index is published for each architecture (Pi OS 64-bit,
# 32-bit, and PCs), not as binary-all, so apt lists it once.
ARCHES="arm64 armhf amd64"
cd "$REPO"
for arch in $ARCHES; do
    d=dists/stable/main/binary-$arch
    mkdir -p "$d"
    apt-ftparchive packages pool > "$d/Packages"
    gzip -9 -n -k -f "$d/Packages"
done
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=pi5-energyplan \
    -o APT::FTPArchive::Release::Label=pi5-energyplan \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=stable \
    -o "APT::FTPArchive::Release::Architectures=$ARCHES" \
    -o APT::FTPArchive::Release::Components=main \
    -o APT::FTPArchive::Release::Description="Pi 5 Energy Plan" \
    release dists/stable > dists/stable/Release.new
mv dists/stable/Release.new dists/stable/Release

GNUPGHOME=$(mktemp -d /tmp/pi5gpg.XXXXXX)   # short path: gpg-agent socket limit
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT
printf '%s\n' "$APT_SIGNING_KEY" | gpg --batch --quiet --import
KEY=$(gpg --batch --list-secret-keys --with-colons | awk -F: '/^fpr/ {print $10; exit}')
rm -f dists/stable/InRelease dists/stable/Release.gpg
gpg --batch --yes --local-user "$KEY" --clearsign -o dists/stable/InRelease dists/stable/Release
gpg --batch --yes --local-user "$KEY" --armor --detach-sign -o dists/stable/Release.gpg \
    dists/stable/Release
gpg --batch --armor --export "$KEY" > pi5-energyplan.asc
echo "Published: $(ls pool/main | tr '\n' ' ')"
