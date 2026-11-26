#!/usr/bin/env python3
"""Publishes Steading's signed packages to pkgs.stead.ing.

A component's release carries attested rpms. `sync` finds releases not yet
in `edge`, verifies each rpm's attestation against the component's release
workflow at that tag, signs it once into `pool/`, promotes the newest build
that has soaked in `edge`, rebuilds each channel's dnf metadata and signs
it, and writes the promotion history to `channels/<component>.json`.
`promote` and `yank` are the two manual controls.

Everything the repository is lives in the bucket; this program keeps no
state of its own. Every command is safe to run again after a failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHES = ("x86_64", "aarch64")
CHANNELS = ("edge", "stable")
SOAK = dt.timedelta(hours=48)
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
RELEASE_PACKAGE = "steading-release"

# A channel's metadata lives at fedora/<channel>/<arch>/, three levels
# below the pool every channel shares.
POOL_FROM_REPO = "../../../"

IMMUTABLE = "public, max-age=31536000, immutable"
MUTABLE = "no-cache"
CONTENT_TYPES = {
    ".rpm": "application/x-rpm",
    ".xml": "application/xml",
    ".gz": "application/gzip",
    ".asc": "application/pgp-signature",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".repo": "text/plain; charset=utf-8",
}


def content_type(path: Path) -> str:
    if path.name.startswith("RPM-GPG-KEY-"):
        return "application/pgp-keys"
    return CONTENT_TYPES.get(path.suffix, "application/octet-stream")


class Refused(Exception):
    """A command that must not go ahead, with the reason."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args: str) -> str:
    return run(*args, stdout=subprocess.PIPE).stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def version_key(version: str) -> tuple[int, int, int]:
    match = VERSION.match(version)
    if not match:
        raise ValueError(f"not a plain X.Y.Z version: {version!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


# --- Promotion history -------------------------------------------------


@dataclass
class History:
    """channels/<component>.json: every version, and when it moved."""

    component: str
    versions: dict[str, dict]

    @classmethod
    def load(cls, component: str, data: dict | None) -> History:
        if data is None:
            return cls(component, {})
        if data.get("component") != component:
            raise Refused(f"channels/{component}.json names {data.get('component')!r}")
        return cls(component, {entry["version"]: entry for entry in data["versions"]})

    def dump(self, now: dt.datetime) -> dict:
        ordered = sorted(
            self.versions.values(),
            key=lambda e: version_key(e["version"]),
            reverse=True,
        )
        return {
            "component": self.component,
            "updated_at": iso(now),
            "versions": ordered,
        }

    def add(
        self, version: str, tag: str, rpms: dict[str, dict], now: dt.datetime
    ) -> dict:
        entry = {
            "version": version,
            "tag": tag,
            "edge_at": iso(now),
            "stable_at": None,
            "yanked": False,
            "yanked_at": None,
            "yank_reason": None,
            "rpms": rpms,
        }
        self.versions[version] = entry
        return entry

    def live(self) -> list[dict]:
        return [e for e in self.versions.values() if not e["yanked"]]

    def in_channel(self, channel: str) -> list[dict]:
        if channel == "edge":
            return self.live()
        return [e for e in self.live() if e["stable_at"]]

    def newest_stable(self) -> str | None:
        stable = [e["version"] for e in self.in_channel("stable")]
        return max(stable, key=version_key) if stable else None


def choose_promotion(history: History, now: dt.datetime) -> str | None:
    """The newest edge build that has soaked and is newer than stable."""
    floor = history.newest_stable()
    eligible = [
        e["version"]
        for e in history.live()
        if not e["stable_at"]
        and now - parse_time(e["edge_at"]) >= SOAK
        and (floor is None or version_key(e["version"]) > version_key(floor))
    ]
    return max(eligible, key=version_key) if eligible else None


def promote(history: History, version: str, now: dt.datetime) -> None:
    entry = history.versions.get(version)
    if entry is None:
        raise Refused(f"{history.component} {version} is not in edge")
    if entry["yanked"]:
        raise Refused(f"{history.component} {version} is yanked")
    if entry["stable_at"]:
        raise Refused(f"{history.component} {version} is already in stable")
    floor = history.newest_stable()
    if floor is not None and version_key(version) < version_key(floor):
        raise Refused(
            f"stable already has {history.component} {floor}, newer than {version}"
        )
    entry["stable_at"] = iso(now)


def yank(history: History, version: str, reason: str, now: dt.datetime) -> None:
    entry = history.versions.get(version)
    if entry is None:
        raise Refused(f"{history.component} {version} was never published")
    if entry["yanked"]:
        raise Refused(f"{history.component} {version} is already yanked")
    if not reason.strip():
        raise Refused("a yank needs a reason")
    entry.update(yanked=True, yanked_at=iso(now), yank_reason=reason.strip())


# --- Storage -----------------------------------------------------------


class Store:
    """The bucket, by key."""

    def get(self, key: str, dest: Path) -> bool:
        raise NotImplementedError

    def put(self, key: str, src: Path, cache_control: str) -> None:
        raise NotImplementedError

    def tag(self, key: str) -> str | None:
        """The object's contents and content type, or None when it is absent."""
        raise NotImplementedError

    def get_json(self, key: str) -> dict | None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "object"
            return json.loads(path.read_text()) if self.get(key, path) else None

    def put_if_changed(self, key: str, src: Path, cache_control: str) -> bool:
        if self.tag(key) == self.tag_of(src):
            return False
        self.put(key, src, cache_control)
        return True

    @staticmethod
    def tag_of(path: Path) -> str:
        """What tag() would say of this file once it is put."""
        md5 = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
        return f"{md5} {content_type(path)}"


class LocalStore(Store):
    """A directory standing in for the bucket, for tests."""

    def __init__(self, root: Path):
        self.root = root

    def get(self, key: str, dest: Path) -> bool:
        source = self.root / key
        if not source.is_file():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return True

    def put(self, key: str, src: Path, cache_control: str) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)

    def tag(self, key: str) -> str | None:
        path = self.root / key
        return self.tag_of(path) if path.is_file() else None


class R2Store(Store):
    """The R2 bucket, through its S3 API and the AWS CLI.

    R2_ENDPOINT and R2_BUCKET name it; AWS_ACCESS_KEY_ID and
    AWS_SECRET_ACCESS_KEY are the bucket-scoped R2 token.
    """

    def __init__(self) -> None:
        self.endpoint = os.environ["R2_ENDPOINT"]
        self.bucket = os.environ["R2_BUCKET"]

    def _s3api(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "aws",
                "s3api",
                "--endpoint-url",
                self.endpoint,
                "--region",
                "auto",
                *args,
            ],
            check=check,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                # R2 doesn't take every checksum newer CLIs send by default.
                "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
                "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
            },
        )

    def tag(self, key: str) -> str | None:
        result = self._s3api(
            "head-object", "--bucket", self.bucket, "--key", key, check=False
        )
        if result.returncode != 0:
            if "Not Found" in result.stderr or "404" in result.stderr:
                return None
            raise RuntimeError(f"head-object {key}: {result.stderr.strip()}")
        head = json.loads(result.stdout)
        return f"{head['ETag'].strip(chr(34))} {head.get('ContentType', '')}"

    def get(self, key: str, dest: Path) -> bool:
        if self.tag(key) is None:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._s3api("get-object", "--bucket", self.bucket, "--key", key, str(dest))
        return True

    def put(self, key: str, src: Path, cache_control: str) -> None:
        self._s3api(
            "put-object",
            "--bucket",
            self.bucket,
            "--key",
            key,
            "--body",
            str(src),
            "--content-type",
            content_type(src),
            "--cache-control",
            cache_control,
        )
        log(f"  put {key}")


# --- Release sources ---------------------------------------------------


@dataclass
class Component:
    name: str
    repository: str
    workflow: str
    image: str | None = None


def load_components(path: Path) -> list[Component]:
    data = tomllib.loads(path.read_text())
    return [Component(name=name, **fields) for name, fields in data.items()]


class GitHubSource:
    """A component's GitHub releases, verified against its release workflow."""

    def tags(self, component: Component) -> list[str]:
        """Published releases that carry rpms, in one paginated call."""
        pages = json.loads(
            output(
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{component.repository}/releases?per_page=100",
            )
        )
        return [
            release["tag_name"]
            for page in pages
            for release in page
            if not release["draft"]
            and any(a["name"].endswith(".rpm") for a in release["assets"])
        ]

    def fetch(self, component: Component, tag: str, dest: Path) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        run(
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            component.repository,
            "--pattern",
            "*.rpm",
            "--dir",
            str(dest),
            "--clobber",
        )
        rpms = sorted(dest.glob("*.rpm"))
        for rpm in rpms:
            # A failure raises: an rpm that doesn't verify stops the run.
            run(
                "gh",
                "attestation",
                "verify",
                str(rpm),
                "--repo",
                component.repository,
                "--signer-workflow",
                f"{component.repository}/{component.workflow}",
                "--source-ref",
                f"refs/tags/{tag}",
                "--deny-self-hosted-runners",
                stdout=subprocess.DEVNULL,
            )
            log(f"  attestation verified: {rpm.name}")
        return rpms


class DirectorySource:
    """Releases as directories of rpms, <root>/<component>/<tag>/, for tests.

    It verifies nothing, so it only ever publishes to a LocalStore.
    """

    def __init__(self, root: Path):
        self.root = root

    def tags(self, component: Component) -> list[str]:
        base = self.root / component.name
        return sorted(p.name for p in base.iterdir()) if base.is_dir() else []

    def fetch(self, component: Component, tag: str, dest: Path) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        rpms = []
        for rpm in sorted((self.root / component.name / tag).glob("*.rpm")):
            shutil.copyfile(rpm, dest / rpm.name)
            rpms.append(dest / rpm.name)
        return rpms


# --- Signing -------------------------------------------------------------


class Signer:
    """The Steading key, already imported into GNUPGHOME."""

    def __init__(self, fingerprint: str, public_key: Path, work: Path):
        if not re.fullmatch(r"[0-9A-F]{40}", fingerprint):
            raise Refused("SIGNING_KEY_FINGERPRINT is not a 40-digit fingerprint")
        listed = output(
            "gpg", "--batch", "--with-colons", "--list-secret-keys", fingerprint
        )
        if f"fpr:::::::::{fingerprint}:" not in listed:
            raise Refused(
                "the imported secret key is not the one SIGNING_KEY_FINGERPRINT names"
            )
        self.fingerprint = fingerprint
        # An rpm database holding this key and no other: a package passes
        # --checksig against it only if this key signed it.
        self.rpmdb = work / "rpmdb"
        self.rpmdb.mkdir(parents=True, exist_ok=True)
        run("rpmkeys", "--dbpath", str(self.rpmdb), "--import", str(public_key))

    def sign_rpm(self, rpm: Path) -> None:
        run(
            "rpmsign",
            "--define",
            f"_openpgp_sign_id {self.fingerprint}",
            "--define",
            f"_gpg_name {self.fingerprint}",
            "--addsign",
            str(rpm),
            stdout=subprocess.DEVNULL,
        )
        self.check_rpm(rpm)

    def check_rpm(self, rpm: Path) -> None:
        result = subprocess.run(
            ["rpmkeys", "--dbpath", str(self.rpmdb), "--checksig", str(rpm)],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0 or "signatures OK" not in result.stdout:
            raise Refused(
                f"{rpm.name} is not signed by the Steading key: {result.stdout.strip()}"
            )

    def sign_file(self, path: Path) -> Path:
        signature = path.with_name(path.name + ".asc")
        run(
            "gpg",
            "--batch",
            "--yes",
            "--local-user",
            self.fingerprint,
            "--detach-sign",
            "--armor",
            "--output",
            str(signature),
            str(path),
        )
        return signature


def rpm_identity(rpm: Path) -> dict[str, str]:
    fields = output(
        "rpm",
        "-qp",
        "--nosignature",
        "--qf",
        "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SHA256HEADER}",
        str(rpm),
    ).split("\t")
    return dict(zip(("name", "version", "release", "arch", "header"), fields))


# --- Publishing ----------------------------------------------------------


@dataclass
class Context:
    store: Store
    source: GitHubSource | DirectorySource
    components: list[Component]
    work: Path
    now: dt.datetime
    signer: Signer | None = None

    def need_signer(self) -> Signer:
        if self.signer is None:
            raise Refused("signing is needed but no key is loaded")
        return self.signer


def ingest(ctx: Context, component: Component, history: History, tag: str) -> bool:
    """Verify, sign and pool one release's rpms; add it to edge."""
    version = tag.removeprefix("v")
    if version in history.versions:
        return False
    try:
        version_key(version)
    except ValueError:
        log(f"{component.name} {tag}: not a plain vX.Y.Z tag, skipped")
        return False

    fetched = ctx.source.fetch(
        component, tag, ctx.work / "releases" / component.name / tag
    )
    if not fetched:
        log(f"{component.name} {tag}: the release carries no rpms, skipped")
        return False

    by_arch: dict[str, Path] = {}
    for rpm in fetched:
        ident = rpm_identity(rpm)
        if ident["name"] != component.name or ident["version"] != version:
            raise Refused(
                f"{rpm.name} is {ident['name']} {ident['version']}, not {component.name} {version}"
            )
        if ident["arch"] not in ARCHES or ident["arch"] in by_arch:
            raise Refused(
                f"{component.name} {tag}: unexpected or duplicate {ident['arch']} rpm {rpm.name}"
            )
        by_arch[ident["arch"]] = rpm
    missing = set(ARCHES) - set(by_arch)
    if missing:
        raise Refused(
            f"{component.name} {tag}: no rpm for {', '.join(sorted(missing))}"
        )

    rpms = {}
    for arch, rpm in sorted(by_arch.items()):
        rpms[arch] = pool(ctx, component.name, rpm)
    history.add(version, tag, rpms, ctx.now)
    log(f"{component.name} {version}: added to edge")
    return True


def pool(ctx: Context, component: str, rpm: Path) -> dict:
    """Sign an rpm into pool/<component>/, once.

    A pooled rpm is never replaced. If an earlier run pooled it and then
    failed, the pooled copy is kept when its header is this rpm's.
    """
    key = f"pool/{component}/{rpm.name}"
    signer = ctx.need_signer()
    signed = ctx.work / "pool" / component / rpm.name
    signed.parent.mkdir(parents=True, exist_ok=True)
    if ctx.store.get(key, signed):
        if rpm_identity(signed)["header"] != rpm_identity(rpm)["header"]:
            raise Refused(f"{key} already exists with different contents")
        signer.check_rpm(signed)
        log(f"  {key}: already pooled")
    else:
        shutil.copyfile(rpm, signed)
        signer.sign_rpm(signed)
        ctx.store.put(key, signed, IMMUTABLE)
    return {"path": key, "sha256": sha256(signed), "size": signed.stat().st_size}


def ingest_release_package(ctx: Context, history: History) -> bool:
    """Build steading-release from this repository when its version is new."""
    spec = ROOT / "release" / f"{RELEASE_PACKAGE}.spec"
    version = output("rpmspec", "-q", "--srpm", "--qf", "%{VERSION}", str(spec)).strip()
    if version in history.versions:
        return False
    topdir = ctx.work / "rpmbuild"
    sources = topdir / "SOURCES"
    sources.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        ROOT / "site" / "RPM-GPG-KEY-steading", sources / "RPM-GPG-KEY-steading"
    )
    shutil.copyfile(ROOT / "release" / "steading.repo", sources / "steading.repo")
    run(
        "rpmbuild",
        "-bb",
        "--quiet",
        "--define",
        f"_topdir {topdir}",
        str(spec),
        stdout=subprocess.DEVNULL,
    )
    (rpm,) = (topdir / "RPMS" / "noarch").glob(
        f"{RELEASE_PACKAGE}-{version}-*.noarch.rpm"
    )
    entry = history.add(
        version, f"v{version}", {"noarch": pool(ctx, RELEASE_PACKAGE, rpm)}, ctx.now
    )
    # The repository's own configuration needs no soak.
    entry["stable_at"] = entry["edge_at"]
    log(f"{RELEASE_PACKAGE} {version}: added to edge and stable")
    return True


def repomd_files(repomd: Path) -> list[str]:
    ns = {"r": "http://linux.duke.edu/metadata/repo"}
    root = ET.parse(repomd).getroot()
    return [d.find("r:location", ns).get("href") for d in root.findall("r:data", ns)]


def fetch_repo(ctx: Context, prefix: str, dest: Path) -> bool:
    """Download a channel's current metadata, or report it absent."""
    if not ctx.store.get(
        f"{prefix}/repodata/repomd.xml", dest / "repodata" / "repomd.xml"
    ):
        return False
    for href in repomd_files(dest / "repodata" / "repomd.xml"):
        if not ctx.store.get(f"{prefix}/{href}", dest / href):
            raise Refused(
                f"{prefix}/repodata/repomd.xml names {href}, which is missing"
            )
    return True


def primary_packages(repo: Path) -> dict[str, tuple[str, int]]:
    ns = {"c": "http://linux.duke.edu/metadata/common"}
    (primary,) = [
        h for h in repomd_files(repo / "repodata" / "repomd.xml") if "primary.xml" in h
    ]
    with gzip.open(repo / primary) as f:
        root = ET.parse(f).getroot()
    packages = {}
    for package in root.findall("c:package", ns):
        href = package.find("c:location", ns).get("href")
        packages[href] = (
            package.find("c:checksum", ns).text,
            int(package.find("c:size", ns).get("package")),
        )
    return packages


def build_repo(
    ctx: Context,
    channel: str,
    arch: str,
    entries: list[dict],
    caches: list[Path],
    full: bool,
) -> Path:
    """Write one channel's metadata for one architecture, and sign it.

    Each pooled rpm's metadata is taken from earlier metadata in `caches`
    (createrepo_c --update --skip-stat), so the stage holds an empty file
    in its place, and only rpms new to every cache are read. With `full`,
    every rpm is downloaded and read instead.
    """
    stage = ctx.work / "stage" / channel / arch
    out = ctx.work / "repos" / channel / arch
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    expected = {}
    for entry in entries:
        path = entry["path"]
        local = ctx.work / path
        staged = stage / path
        staged.parent.mkdir(parents=True, exist_ok=True)
        if not local.is_file() and full and not ctx.store.get(path, local):
            raise Refused(f"{path} is in the history but not in the bucket")
        if local.is_file():
            shutil.copyfile(local, staged)
        else:
            staged.touch()
        expected[POOL_FROM_REPO + path] = (entry["sha256"], entry["size"])

    args = [
        "createrepo_c",
        "--quiet",
        "--location-prefix",
        POOL_FROM_REPO,
        "--general-compress-type",
        "gz",
        "--outputdir",
        str(out),
    ]
    usable = [c for c in caches if (c / "repodata" / "repomd.xml").is_file()]
    if usable and not full:
        args += ["--update", "--skip-stat"]
        for cache in usable:
            args += ["--update-md-path", str(cache)]
    run(*args, str(stage))

    # The metadata must describe exactly the rpms the history says, as
    # they are in the pool: a placeholder read as a package, or a package
    # missing from every cache, fails here rather than reaching a user.
    found = primary_packages(out)
    if found != expected:
        wrong = sorted(set(found.items()) ^ set(expected.items()))
        raise Refused(
            f"{channel}/{arch} metadata disagrees with the history (run sync --full): {wrong}"
        )

    ctx.need_signer().sign_file(out / "repodata" / "repomd.xml")
    return out


def publish_repos(ctx: Context, histories: dict[str, History], full: bool) -> None:
    for arch in ARCHES:
        built: dict[str, Path] = {}
        for channel in CHANNELS:
            entries = [
                rpm
                for history in histories.values()
                for version in history.in_channel(channel)
                for rpm_arch, rpm in version["rpms"].items()
                if rpm_arch in (arch, "noarch")
            ]
            current = ctx.work / "current" / channel / arch
            shutil.rmtree(current, ignore_errors=True)
            fetch_repo(ctx, f"fedora/{channel}/{arch}", current)
            # stable is a subset of edge, so edge's new metadata covers it.
            caches = [built["edge"], current] if channel == "stable" else [current]
            built[channel] = build_repo(ctx, channel, arch, entries, caches, full)

        # Content-addressed files first, so a repomd.xml never names a
        # file that isn't there yet; then each repomd.xml with its
        # signature.
        for channel, out in built.items():
            prefix = f"fedora/{channel}/{arch}"
            for href in repomd_files(out / "repodata" / "repomd.xml"):
                ctx.store.put_if_changed(f"{prefix}/{href}", out / href, IMMUTABLE)
        for channel, out in built.items():
            prefix = f"fedora/{channel}/{arch}"
            ctx.store.put(
                f"{prefix}/repodata/repomd.xml",
                out / "repodata" / "repomd.xml",
                MUTABLE,
            )
            ctx.store.put(
                f"{prefix}/repodata/repomd.xml.asc",
                out / "repodata" / "repomd.xml.asc",
                MUTABLE,
            )
            log(f"{channel}/{arch}: published")


def publish_site(ctx: Context, histories: dict[str, History]) -> None:
    site = ctx.work / "site"
    shutil.rmtree(site, ignore_errors=True)
    shutil.copytree(ROOT / "site", site)
    fingerprint = ctx.need_signer().fingerprint
    grouped = " ".join(fingerprint[i : i + 4] for i in range(0, 40, 4))
    index = site / "index.html"
    index.write_text(index.read_text().replace("@FINGERPRINT@", grouped))
    for path in sorted(p for p in site.rglob("*") if p.is_file()):
        ctx.store.put_if_changed(str(path.relative_to(site)), path, MUTABLE)

    # A stable address for the newest steading-release, to install from.
    release = histories[RELEASE_PACKAGE]
    newest = max(release.in_channel("stable"), key=lambda e: version_key(e["version"]))
    local = ctx.work / newest["rpms"]["noarch"]["path"]
    if not local.is_file():
        ctx.store.get(newest["rpms"]["noarch"]["path"], local)
    ctx.store.put_if_changed(f"{RELEASE_PACKAGE}.noarch.rpm", local, MUTABLE)


def load_histories(ctx: Context) -> tuple[dict[str, History], dict[str, str | None]]:
    histories, tags = {}, {}
    for name in [c.name for c in ctx.components] + [RELEASE_PACKAGE]:
        key = f"channels/{name}.json"
        tags[name] = ctx.store.tag(key)
        histories[name] = History.load(name, ctx.store.get_json(key))
    return histories, tags


def save_histories(
    ctx: Context, histories: dict[str, History], tags: dict[str, str | None]
) -> None:
    for name, history in histories.items():
        key = f"channels/{name}.json"
        # Another run changed the history since this one read it: stop
        # rather than overwrite it. The next run starts from theirs.
        if ctx.store.tag(key) != tags[name]:
            raise Refused(f"{key} changed during this run")
        path = ctx.work / "channels" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history.dump(ctx.now), indent=2) + "\n")
        ctx.store.put(key, path, MUTABLE)


def sync(ctx: Context, rebuild: bool, full: bool, change=None) -> None:
    histories, tags = load_histories(ctx)
    changed = rebuild or full

    if change is not None:
        change(histories)
        changed = True

    changed |= ingest_release_package(ctx, histories[RELEASE_PACKAGE])
    for component in ctx.components:
        history = histories[component.name]
        for tag in ctx.source.tags(component):
            changed |= ingest(ctx, component, history, tag)
        promoted = choose_promotion(history, ctx.now)
        if promoted:
            promote(history, promoted, ctx.now)
            log(
                f"{component.name} {promoted}: promoted to stable after soaking in edge"
            )
            changed = True

    if not ctx.store.tag("fedora/stable/x86_64/repodata/repomd.xml"):
        changed = True
    if changed:
        publish_repos(ctx, histories, full)
        save_histories(ctx, histories, tags)
    publish_site(ctx, histories)
    if not changed:
        log("nothing to publish")


# --- Image tags ----------------------------------------------------------


def image_tags(history: History) -> dict[str, str]:
    """The tags promotion owns on a component's image, and their versions.

    `stable` and `latest` name the newest stable build, and each `X.Y` the
    newest stable build in that series. Yanked builds are never named.
    """
    stable = sorted(
        (e["version"] for e in history.in_channel("stable")), key=version_key
    )
    tags: dict[str, str] = {}
    for version in stable:
        major, minor, _ = version_key(version)
        tags[f"{major}.{minor}"] = version
    if stable:
        tags["stable"] = tags["latest"] = stable[-1]
    return tags


def manifest_digest(reference: str) -> str | None:
    """The digest of what a tag names: the index itself, not one platform's image."""
    result = subprocess.run(
        ["skopeo", "inspect", "--raw", reference], check=False, capture_output=True
    )
    if result.returncode != 0:
        return None
    return "sha256:" + hashlib.sha256(result.stdout).hexdigest()


def reconcile_images(components: list[Component], base_url: str, dry_run: bool) -> None:
    for component in components:
        if not component.image:
            continue
        request = urllib.request.Request(
            f"{base_url}/channels/{component.name}.json",
            headers={"User-Agent": "steading-ai/pkgs.stead.ing"},
        )
        with urllib.request.urlopen(request) as response:
            history = History.load(component.name, json.load(response))
        for tag, version in sorted(image_tags(history).items()):
            source = f"docker://{component.image}:{version}"
            target = f"docker://{component.image}:{tag}"
            want = manifest_digest(source)
            if want is None:
                raise Refused(f"{component.image}:{version} does not exist")
            if manifest_digest(target) == want:
                continue
            log(f"{component.image}:{tag} -> {version} ({want})")
            if not dry_run:
                run("skopeo", "copy", "--all", "--preserve-digests", source, target)


# --- Command line --------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--store", default="r2", help="'r2', or a directory standing in for the bucket"
    )
    parser.add_argument(
        "--source", default="github", help="'github', or a directory of test releases"
    )
    parser.add_argument("--components", type=Path, default=ROOT / "components.toml")
    parser.add_argument(
        "--work", type=Path, help="scratch directory (default: a temporary one)"
    )
    parser.add_argument(
        "--now", help="the time to act at, YYYY-MM-DDTHH:MM:SSZ (tests only)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sync_cmd = commands.add_parser(
        "sync", help="publish new releases and promote soaked builds"
    )
    sync_cmd.add_argument("--rebuild", action="store_true", help="rebuild all metadata")
    sync_cmd.add_argument(
        "--full", action="store_true", help="rebuild it from the rpms themselves"
    )
    promote_cmd = commands.add_parser("promote", help="move a build to stable now")
    promote_cmd.add_argument("component")
    promote_cmd.add_argument("version")
    yank_cmd = commands.add_parser("yank", help="remove a build from both channels")
    yank_cmd.add_argument("component")
    yank_cmd.add_argument("version")
    yank_cmd.add_argument("--reason", required=True)
    images_cmd = commands.add_parser("images", help="move image tags to match stable")
    images_cmd.add_argument("--base-url", default="https://pkgs.stead.ing")
    images_cmd.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    components = load_components(args.components)
    try:
        if args.command == "images":
            reconcile_images(components, args.base_url, args.dry_run)
            return 0

        if args.store == "r2":
            store: Store = R2Store()
        else:
            store = LocalStore(Path(args.store))
        if args.source == "github":
            source: GitHubSource | DirectorySource = GitHubSource()
        else:
            if not isinstance(store, LocalStore):
                raise Refused("a test source never publishes to the bucket")
            source = DirectorySource(Path(args.source))
        if args.now and not isinstance(store, LocalStore):
            raise Refused("--now is for tests, and never publishes to the bucket")
        now = (
            parse_time(args.now)
            if args.now
            else dt.datetime.now(dt.UTC).replace(microsecond=0)
        )

        with tempfile.TemporaryDirectory() as scratch:
            work = args.work or Path(scratch)
            work.mkdir(parents=True, exist_ok=True)
            signer = Signer(
                os.environ.get("SIGNING_KEY_FINGERPRINT", ""),
                ROOT / "site" / "RPM-GPG-KEY-steading",
                work,
            )
            ctx = Context(store, source, components, work, now, signer)
            names = {c.name for c in components}

            def require(component: str) -> None:
                if component not in names:
                    raise Refused(
                        f"no component named {component!r} in {args.components.name}"
                    )

            if args.command == "sync":
                sync(ctx, args.rebuild, args.full)
            elif args.command == "promote":
                require(args.component)
                sync(
                    ctx,
                    False,
                    False,
                    lambda h: promote(h[args.component], args.version, now),
                )
                log(f"{args.component} {args.version}: promoted to stable by hand")
            elif args.command == "yank":
                require(args.component)
                sync(
                    ctx,
                    False,
                    False,
                    lambda h: yank(h[args.component], args.version, args.reason, now),
                )
                log(f"{args.component} {args.version}: yanked")
    except Refused as refusal:
        log(f"pkgs: {refusal}")
        return 1
    except subprocess.CalledProcessError as failure:
        log(
            f"pkgs: {' '.join(map(str, failure.cmd[:3]))} ... exited {failure.returncode}"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
