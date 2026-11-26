#!/bin/bash
# The publishing job, end to end, against a directory standing in for the
# bucket: releases reach edge, soak, reach stable, are yanked, and dnf
# installs each state with gpgcheck and repo_gpgcheck on. Run as root in a
# Fedora container, from a checkout:
#
#   podman run --rm -v "$PWD:/src:ro,Z" docker.io/library/fedora:45 bash /src/test/e2e.sh
#
# The signing key is a throwaway made here; the repository's own key is
# never touched.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d)"
trap 'gpgconf --kill all 2>/dev/null || true; kill "${server:-0}" 2>/dev/null || true; rm -rf "$W"' EXIT

step() { printf '\n== %s\n' "$*"; }
fail() {
	echo "FAIL: $*" >&2
	exit 1
}

step "tools"
dnf -y -q --setopt=install_weak_deps=False install \
	rpm-build rpm-sign createrepo_c gnupg2 python3 >/dev/null

# A copy of the checkout, so the throwaway public key can take the real
# one's place.
cp -a "$SRC" "$W/repo"
REPO="$W/repo"
BUCKET="$W/bucket"
RELEASES="$W/releases"
mkdir -p "$BUCKET" "$RELEASES"

step "a throwaway signing key"
export GNUPGHOME="$W/gnupg"
mkdir -m 700 "$GNUPGHOME"
gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
	--quick-generate-key "e2e (throwaway)" rsa4096 sign never 2>/dev/null
SIGNING_KEY_FINGERPRINT="$(gpg --with-colons --list-secret-keys | awk -F: '$1 == "fpr" {print $10; exit}')"
export SIGNING_KEY_FINGERPRINT
gpg --armor --export "$SIGNING_KEY_FINGERPRINT" >"$REPO/site/RPM-GPG-KEY-steading"

release() { # <version>: a fake skift release, both architectures
	local version="$1" arch top="$W/rpmbuild"
	mkdir -p "$top/SPECS" "$RELEASES/skift/v$version"
	cat >"$top/SPECS/skift.spec" <<EOF
Name: skift
Version: $version
Release: 1
Summary: e2e
License: ISC
%global debug_package %{nil}
%global __strip /bin/true
%description
e2e
%install
mkdir -p %{buildroot}/usr/bin
printf '#!/bin/sh\necho skift $version\n' >%{buildroot}/usr/bin/skift
chmod 755 %{buildroot}/usr/bin/skift
%files
/usr/bin/skift
EOF
	for arch in x86_64 aarch64; do
		rpmbuild -bb --quiet --define "_topdir $top" --target "$arch" "$top/SPECS/skift.spec" 2>/dev/null
		cp "$top/RPMS/$arch/skift-$version-1.$arch.rpm" "$RELEASES/skift/v$version/"
	done
}

pkgs() { # <time> <args...>
	local now="$1"
	shift
	python3 "$REPO/tools/pkgs.py" --store "$BUCKET" --source "$RELEASES" --now "$now" "$@"
}

history() { # <jq-ish python expression over the history>
	python3 -c "import json,sys; h=json.load(open('$BUCKET/channels/skift.json')); v={e['version']: e for e in h['versions']}; print($1)"
}

step "serve the bucket"
(cd "$BUCKET" && exec python3 -m http.server 8080 --bind 127.0.0.1 >/dev/null 2>&1) &
server=$!
for _ in $(seq 50); do
	if curl -fs -o /dev/null http://127.0.0.1:8080/; then break; fi
	sleep 0.1
done

# The site's repo file, pointed at the local server; <channel> picks which
# of its two repositories the dnf below uses.
repo_file() { # <channel>
	sed -e 's|https://pkgs.stead.ing|http://127.0.0.1:8080|' "$REPO/site/steading.repo" >/etc/yum.repos.d/steading.repo
	if [[ "$1" == edge ]]; then repo_id=steading-edge; else repo_id=steading; fi
}

dnf_steading() {
	dnf -y -q --repo="$repo_id" --refresh "$@"
}

newest() { # the newest skift version the enabled channel offers
	dnf_steading repoquery --latest-limit=1 --qf '%{version}\n' skift 2>/dev/null | tail -1
}

T0="2026-12-01T00:00:00Z"

step "first publish: v0.1.0 reaches edge, stable has only steading-release"
release 0.1.0
pkgs "$T0" sync
[[ "$(history "v['0.1.0']['stable_at']")" == None ]] || fail "0.1.0 reached stable without soaking"
repo_file edge
[[ "$(newest)" == 0.1.0 ]] || fail "edge does not offer 0.1.0"
repo_file stable
[[ -z "$(newest)" ]] || fail "stable offers skift before anything soaked"

step "an rpm the history doesn't know is never published"
mkdir -p "$RELEASES/skift/v0.1.1"
cp "$RELEASES/skift/v0.1.0/skift-0.1.0-1.x86_64.rpm" "$RELEASES/skift/v0.1.1/"
if pkgs "2026-12-01T01:00:00Z" sync 2>"$W/err"; then fail "a v0.1.1 release carrying 0.1.0's rpm was published"; fi
grep -q "not skift 0.1.1" "$W/err" || fail "the refusal did not say why: $(cat "$W/err")"
rm -rf "$RELEASES/skift/v0.1.1"

step "nothing new: a second sync publishes nothing"
pkgs "2026-12-01T02:00:00Z" sync 2>"$W/out"
grep -q "nothing to publish" "$W/out" || fail "a sync with nothing new republished: $(cat "$W/out")"

step "48 hours on: v0.1.0 is promoted, v0.2.0 reaches edge"
release 0.2.0
pkgs "2026-12-03T00:00:00Z" sync
[[ "$(history "v['0.1.0']['stable_at']")" == 2026-12-03T00:00:00Z ]] || fail "0.1.0 was not promoted"
[[ "$(history "v['0.2.0']['stable_at']")" == None ]] || fail "0.2.0 reached stable without soaking"

step "stable installs with both signature checks on"
repo_file stable
grep -q '^repo_gpgcheck=1' /etc/yum.repos.d/steading.repo
dnf_steading install skift >/dev/null
[[ "$(skift)" == "skift 0.1.0" ]] || fail "stable installed $(skift)"

step "stable serves the very rpm edge did"
edge_digest="$(history "v['0.1.0']['rpms']['x86_64']['sha256']")"
installed_from="$(history "v['0.1.0']['rpms']['x86_64']['path']")"
[[ "$(sha256sum "$BUCKET/$installed_from" | cut -d' ' -f1)" == "$edge_digest" ]] || fail "pool object differs from history"
for channel in edge stable; do
	python3 - "$BUCKET/fedora/$channel/x86_64" "$edge_digest" <<'EOF' || fail "$channel's metadata names another digest for 0.1.0"
import gzip, re, sys, xml.etree.ElementTree as ET
repo, digest = sys.argv[1:]
repomd = open(f"{repo}/repodata/repomd.xml").read()
primary = re.search(r'href="(repodata/[^"]*primary.xml.gz)"', repomd).group(1)
xml = gzip.open(f"{repo}/{primary}").read().decode()
assert re.search(r'<checksum type="sha256" pkgid="YES">' + digest, xml), "digest missing"
EOF
done

step "edge offers v0.2.0, and upgrades to it"
repo_file edge
dnf_steading upgrade skift >/dev/null
[[ "$(skift)" == "skift 0.2.0" ]] || fail "edge upgraded to $(skift)"

step "tampered metadata is refused"
cp "$BUCKET/fedora/edge/x86_64/repodata/repomd.xml" "$W/repomd.xml"
sed -i 's|<revision>|<revision>1|' "$BUCKET/fedora/edge/x86_64/repodata/repomd.xml"
if dnf_steading --setopt='*.skip_if_unavailable=0' makecache >/dev/null 2>&1; then fail "dnf accepted tampered metadata"; fi
cp "$W/repomd.xml" "$BUCKET/fedora/edge/x86_64/repodata/repomd.xml"

step "yank v0.2.0: it leaves edge, and promote refuses it"
pkgs "2026-12-03T01:00:00Z" yank skift 0.2.0 --reason "e2e"
[[ "$(history "v['0.2.0']['yanked']")" == True ]] || fail "0.2.0 is not marked yanked"
[[ "$(newest)" == 0.1.0 ]] || fail "edge still offers $(newest) after the yank"
if pkgs "2026-12-03T02:00:00Z" promote skift 0.2.0 2>/dev/null; then fail "a yanked build was promoted"; fi

step "promote now: v0.3.0 reaches stable without soaking"
release 0.3.0
pkgs "2026-12-03T03:00:00Z" sync
pkgs "2026-12-03T04:00:00Z" promote skift 0.3.0
repo_file stable
[[ "$(newest)" == 0.3.0 ]] || fail "stable offers $(newest), not 0.3.0"

step "the soak never moves stable backwards"
release 0.2.1
pkgs "2026-12-03T05:00:00Z" sync
pkgs "2026-12-06T00:00:00Z" sync
[[ "$(history "v['0.2.1']['stable_at']")" == None ]] || fail "0.2.1 was promoted behind 0.3.0"

step "metadata lost: a rebuild from caches refuses, one from the rpms recovers"
rm -rf "$BUCKET/fedora"
if pkgs "2026-12-06T01:00:00Z" sync --rebuild 2>"$W/err"; then fail "metadata was rebuilt from placeholders"; fi
grep -q "pkgs:" "$W/err" || fail "the refusal was not a clean one: $(tail -5 "$W/err")"
pkgs "2026-12-06T01:00:00Z" sync --full
[[ "$(newest)" == 0.3.0 ]] || fail "after a full rebuild stable offers $(newest)"

step "steading-release installs the repository and the key"
rm /etc/yum.repos.d/steading.repo
dnf -y -q --disablerepo='*' install "http://127.0.0.1:8080/steading-release.noarch.rpm" >/dev/null
test -f /etc/pki/rpm-gpg/RPM-GPG-KEY-steading || fail "no key installed"
grep -q '^gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-steading' /etc/yum.repos.d/steading.repo ||
	fail "the installed repo file does not use the installed key"
rpmkeys --checksig "$BUCKET/steading-release.noarch.rpm" | grep -q 'signatures OK' ||
	fail "steading-release is not signed"

step "the site names the key's fingerprint"
grep -q "${SIGNING_KEY_FINGERPRINT:0:4} ${SIGNING_KEY_FINGERPRINT:4:4}" "$BUCKET/index.html" ||
	fail "index.html does not show the fingerprint"

printf '\nPASS\n'
