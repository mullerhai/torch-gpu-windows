#!/usr/bin/env python3
"""
Publish torch-gpu-linux to Maven Central using Sonatype Central Portal API.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
import ssl

ssl_ctx = ssl._create_unverified_context()

# Config
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STAGE = Path(os.environ.get("STAGE_DIR", SCRIPT_DIR / "staging"))
DEFAULT_BUNDLE = Path(os.environ.get("BUNDLE_DIR", SCRIPT_DIR / "bundles"))

GROUP_ID = "io.github.mullerhai"
GROUP_PATH = GROUP_ID.replace(".", "/")
ARTIFACT_ID = "torch-gpu-windows"
VERSION = "13.3-9.25-1.5.14-GA-1.0"

PROJECT_URL = "https://github.com/mullerhai/torch-gpu-windows"
SCM_URL = "https://github.com/mullerhai/torch-gpu-windows"
SCM_CONN = "scm:git:git://github.com/mullerhai/torch-gpu-windows.git"
SCM_DEV = "scm:git:ssh://git@github.com/mullerhai/torch-gpu-windows.git"
LICENSE_NAME = "Apache License, Version 2.0"
LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
DEV_NAME = "Muller Hai"
DEV_EMAIL = "hai710459649@foxmail.com"
DEV_URL = "https://github.com/mullerhai"
ORG_NAME = "mullerhai"
DEV_ID = "mullerhai"

CENTRAL_UPLOAD = "https://central.sonatype.com/api/v1/publisher/upload"
CENTRAL_STATUS = "https://central.sonatype.com/api/v1/publisher/status"
CENTRAL_PUBLISH = "https://central.sonatype.com/api/v1/publisher/deployment"


def log(msg: str) -> None:
    print(msg, flush=True)


def sha_digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(path: Path) -> None:
    for algo, ext in (("md5", ".md5"), ("sha1", ".sha1"), ("sha256", ".sha256"), ("sha512", ".sha512")):
        (path.parent / (path.name + ext)).write_text(sha_digest(path, algo) + "\n", encoding="ascii")


def gpg_sign(path: Path, key_id: str = "C908541CBE90F9F460D4039DF46B9492FFC59C9A") -> Path:
    sig = path.with_suffix(path.suffix + ".asc")
    if sig.exists():
        sig.unlink()
    env = os.environ.copy()
    env["GNUPGHOME"] = "/home/muller/.gnupg-publish"
    cmd = [
        "gpg",
        "--homedir", env["GNUPGHOME"],
        "--batch",
        "--yes",
        "--local-user",
        key_id,
        "--detach-sign",
        "--armor",
        "--output",
        str(sig),
        str(path),
    ]
    if env.get("GPG_PASSPHRASE"):
        cmd.extend(["--pinentry-mode", "loopback", "--passphrase-fd", "0"])
        subprocess.run(cmd, input=env["GPG_PASSPHRASE"] + "\n", text=True, check=True, env=env)
    else:
        subprocess.run(cmd, check=True, env=env)
    return sig


def build_pom() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{GROUP_ID}</groupId>
  <artifactId>{ARTIFACT_ID}</artifactId>
  <version>{VERSION}</version>
  <packaging>pom</packaging>
  <name>{ARTIFACT_ID}</name>
  <description>PyTorch GPU Linux distribution with CUDA support</description>
  <url>{PROJECT_URL}</url>
  <licenses>
    <license>
      <name>{LICENSE_NAME}</name>
      <url>{LICENSE_URL}</url>
      <distribution>repo</distribution>
    </license>
  </licenses>
  <developers>
    <developer>
      <id>{DEV_ID}</id>
      <name>{DEV_NAME}</name>
      <email>{DEV_EMAIL}</email>
      <url>{DEV_URL}</url>
      <organization>{ORG_NAME}</organization>
      <organizationUrl>{DEV_URL}</organizationUrl>
    </developer>
  </developers>
  <scm>
    <url>{SCM_URL}</url>
    <connection>{SCM_CONN}</connection>
    <developerConnection>{SCM_DEV}</developerConnection>
  </scm>
</project>
"""


def minimal_sources_jar(out: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "README-sources.txt",
            f"{ARTIFACT_ID} {VERSION}\nSources not bundled for this republished artifact.\nSee {PROJECT_URL}\n",
        )
        zf.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\nCreated-By: mullerhai-publish\n\n",
        )
    out.write_bytes(buf.getvalue())


def minimal_javadoc_jar(out: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            f"{ARTIFACT_ID} {VERSION}\n"
            f"Javadoc not generated for this platform-native / republished artifact.\n"
            f"See {PROJECT_URL}\n"
        )
        zf.writestr("README-javadoc.txt", readme)
        zf.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\nCreated-By: mullerhai-publish\n\n",
        )
    out.write_bytes(buf.getvalue())


def stage_artifact(stage: Path, source_jar: Path) -> list[Path]:
    out_dir = stage / GROUP_PATH / ARTIFACT_ID / VERSION
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []

    # POM
    pom_out = out_dir / f"{ARTIFACT_ID}-{VERSION}.pom"
    pom_out.write_text(build_pom(), encoding="utf-8")
    produced.append(pom_out)

    # Main jar (copy from source, or write minimal placeholder if no source jar available)
    main_out = out_dir / f"{ARTIFACT_ID}-{VERSION}.jar"
    if source_jar.exists() and source_jar.stat().st_size > 0:
        shutil.copy2(source_jar, main_out)
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "README.txt",
                f"{ARTIFACT_ID} {VERSION}\nNo binary artifact for this republished POM-only module.\nSee {PROJECT_URL}\n",
            )
            zf.writestr(
                "META-INF/MANIFEST.MF",
                "Manifest-Version: 1.0\nCreated-By: mullerhai-publish\n\n",
            )
        main_out.write_bytes(buf.getvalue())
    produced.append(main_out)

    # sources
    sources_out = out_dir / f"{ARTIFACT_ID}-{VERSION}-sources.jar"
    minimal_sources_jar(sources_out)
    produced.append(sources_out)

    # javadoc
    javadoc_out = out_dir / f"{ARTIFACT_ID}-{VERSION}-javadoc.jar"
    minimal_javadoc_jar(javadoc_out)
    produced.append(javadoc_out)

    log(f"  staged {ARTIFACT_ID}:{VERSION} -> {out_dir} ({len(produced)} files)")
    return produced


def sign_all(stage: Path, skip: bool = False) -> None:
    if skip:
        log("Skipping GPG signing (--no-sign flag)")
        return
    files = [p for p in stage.rglob("*") if p.is_file() and not p.name.endswith(".asc") and not any(p.name.endswith(e) for e in (".md5", ".sha1", ".sha256", ".sha512"))]
    for p in sorted(files):
        log(f"  sign {p.relative_to(stage)}")
        write_checksums(p)
        gpg_sign(p)
        sig = p.with_suffix(p.suffix + ".asc")
        if sig.exists():
            write_checksums(sig)
    log("Sign complete.")


def bundle_all(stage: Path, bundle_dir: Path) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zip_path = bundle_dir / f"torch-gpu-windows-{VERSION}-{stamp}.zip"
    if zip_path.exists():
        zip_path.unlink()

    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(stage.rglob("*")):
            if not p.is_file():
                continue
            arc = p.relative_to(stage).as_posix()
            zf.write(p, arcname=arc)
            count += 1
    log(f"Bundle: {zip_path} ({count} files, {zip_path.stat().st_size / (1<<20):.1f} MiB)")
    return zip_path


def get_credentials() -> tuple:
    user = os.environ.get("CENTRAL_USERNAME") or os.environ.get("SONATYPE_USERNAME")
    pwd = os.environ.get("CENTRAL_PASSWORD") or os.environ.get("SONATYPE_PASSWORD")
    if not user or not pwd:
        settings = Path.home() / ".m2" / "settings.xml"
        if settings.exists():
            try:
                tree = ET.parse(settings)
                for server in tree.getroot().iter():
                    if server.tag.endswith("server"):
                        sid = None
                        for child in server:
                            if child.tag.endswith("id"):
                                sid = child.text
                            if child.tag.endswith("username"):
                                user = child.text
                            if child.tag.endswith("password"):
                                pwd = child.text
                        if sid == "ossrh" or sid == "central":
                            break
            except Exception as e:
                log(f"warn: could not parse settings.xml: {e}")
    if not user or not pwd:
        raise SystemExit(
            "Missing Central credentials. Set CENTRAL_USERNAME and CENTRAL_PASSWORD "
            "or put them in ~/.m2/settings.xml under <server><id>ossrh</id>."
        )
    return user, pwd


def upload_bundle(zip_path: Path, publishing_type: str = "USER_MANAGED") -> str:
    user, pwd = get_credentials()
    file_size = zip_path.stat().st_size
    log(f"Uploading {zip_path.name} ({file_size/(1<<20):.1f} MiB) to Central Portal ...")

    netrc_path = Path(tempfile.gettempdir()) / "muller_netrc"
    netrc_path.write_text(f"machine central.sonatype.com login {user} password {pwd}\n", encoding="utf-8")

    body_path = Path(tempfile.gettempdir()) / f"central_upload_body_{os.getpid()}.txt"
    cmd = [
        "curl",
        "-sS",
        "-X",
        "POST",
        "--http1.1",
        "-H",
        "Expect:",
        "-n",
        "--netrc-file",
        str(netrc_path),
        "-F",
        f"bundle=@{zip_path};type=application/zip",
        f"https://central.sonatype.com/api/v1/publisher/upload?publishingType={publishing_type}&name={zip_path.stem}",
        "--connect-timeout",
        "120",
        "--max-time",
        "7200",
        "-o",
        str(body_path),
        "-w",
        "HTTP_CODE=%{http_code} SIZE_UPLOAD=%{size_upload} TIME=%{time_total}\n",
    ]

    start_time = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start_time
    netrc_path.unlink(missing_ok=True)

    log(f"  upload completed in {elapsed:.1f}s")
    if proc.stdout:
        log(f"  curl meta: {proc.stdout.strip()}")
    if proc.returncode != 0:
        log(f"  curl stderr: {(proc.stderr or '')[-1000:]}")
        raise SystemExit(f"Upload failed with return code {proc.returncode}")

    deployment_id = body_path.read_text(encoding="utf-8", errors="replace").strip() if body_path.exists() else ""
    body_path.unlink(missing_ok=True)

    http_code = None
    if proc.stdout and "HTTP_CODE=" in proc.stdout:
        try:
            http_code = proc.stdout.split("HTTP_CODE=")[1].split()[0]
        except Exception:
            http_code = None

    if http_code and http_code not in ("200", "201", "202"):
        log(f"  unexpected HTTP {http_code}: {deployment_id[:500]}")
        raise SystemExit(f"Upload failed HTTP {http_code}: {deployment_id[:200]}")

    if not deployment_id or "{" in deployment_id:
        log(f"  unexpected response: {deployment_id[:500]}")
        raise SystemExit(f"Upload failed: {deployment_id[:200]}")

    log(f"Upload OK. deploymentId = {deployment_id}")
    return deployment_id


def poll_status(deployment_id: str, timeout_s: int = 1800) -> dict:
    user, pwd = get_credentials()
    token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    url = f"{CENTRAL_STATUS}?id={deployment_id}"
    body_path = Path(tempfile.gettempdir()) / f"central_status_{os.getpid()}.json"

    start = time.time()
    while True:
        cmd = [
            "curl",
            "-sS",
            "-X",
            "POST",
            "--http1.1",
            "-H",
            f"Authorization: Bearer {token}",
            url,
            "-o",
            str(body_path),
            "-w",
            "%{http_code}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        raw = body_path.read_text(encoding="utf-8", errors="replace") if body_path.exists() else ""
        body_path.unlink(missing_ok=True)

        http_code = (proc.stdout or "").strip()
        if http_code not in ("200", "201", "202", "204"):
            log(f"  Status check HTTP {http_code}: {raw[:200]}")
            time.sleep(15)
            continue

        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"state": "?"}

        state = data.get("deploymentState") or data.get("state") or "?"
        log(f"  deployment {deployment_id}: {state}")
        if state in ("PUBLISHED", "FAILED", "VALIDATED"):
            return data
        if time.time() - start > timeout_s:
            log("Timeout waiting for deployment; check https://central.sonatype.com/publishing")
            return data
        time.sleep(15)


def publish_deployment(deployment_id: str) -> None:
    user, pwd = get_credentials()
    token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    url = f"{CENTRAL_PUBLISH}/{deployment_id}"
    body_path = Path(tempfile.gettempdir()) / f"central_publish_{os.getpid()}.json"
    cmd = [
        "curl",
        "-sS",
        "-X",
        "POST",
        "--http1.1",
        "-H",
        f"Authorization: Bearer {token}",
        url,
        "-o",
        str(body_path),
        "-w",
        "%{http_code}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    body_path.unlink(missing_ok=True)
    log(f"Publish requested for {deployment_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish torch-gpu-linux to Maven Central")
    parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--source-jar", type=Path, default=None)
    parser.add_argument("--upload", action="store_true", help="Upload after staging and signing")
    parser.add_argument("--publishing-type", choices=["USER_MANAGED", "AUTOMATIC"], default="USER_MANAGED")
    parser.add_argument("--no-wait", action="store_true", help="Upload and return immediately")
    parser.add_argument("--publish", action="store_true", help="Call publish API after upload")
    parser.add_argument("--no-sign", action="store_true", help="Skip GPG signing (for manual signing)")
    args = parser.parse_args()

    log(f"""
============================================================
  torch-gpu-linux -> Maven Central
============================================================
  groupId    : {GROUP_ID}
  artifactId : {ARTIFACT_ID}
  version    : {VERSION}
  source jar : {args.source_jar or 'none'}
============================================================
""")

    # Stage
    stage = args.stage_dir
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    # Belt-and-suspenders: remove any stale version subdir left behind by rmtree failures
    stale_version_dir = stage / GROUP_PATH / ARTIFACT_ID / VERSION
    if stale_version_dir.exists():
        shutil.rmtree(stale_version_dir, ignore_errors=True)

    source_jar = args.source_jar
    if not source_jar:
        default_jar = Path("target") / f"{ARTIFACT_ID}-{VERSION}.jar"
        if default_jar.exists():
            source_jar = default_jar
        else:
            default_jar = Path(__file__).parent / "target" / f"{ARTIFACT_ID}-{VERSION}.jar"
            if default_jar.exists():
                source_jar = default_jar

    log(f"Staging into {stage}")
    stage_artifact(stage, source_jar or Path("/dev/null"))

    # Sign
    log(f"Signing artifacts under {stage}")
    sign_all(stage, skip=args.no_sign)

    # Bundle
    log(f"Bundling into {args.bundle_dir}")
    zip_path = bundle_all(stage, args.bundle_dir)

    if args.upload:
        dep_id = upload_bundle(zip_path, publishing_type=args.publishing_type)
        if args.no_wait:
            log(f"Upload submitted. deploymentId={dep_id}")
            log("Review: https://central.sonatype.com/publishing/deployments")
            return 0
        data = poll_status(dep_id)
        if args.publish and data.get("deploymentState") == "VALIDATED":
            publish_deployment(dep_id)
            poll_status(dep_id)
        log(f"Done. deploymentId={dep_id}")
        log("Review: https://central.sonatype.com/publishing/deployments")
    else:
        log(f"Bundle ready: {zip_path}")
        log("Set CENTRAL_USERNAME/CENTRAL_PASSWORD then re-run with --upload")

    return 0


if __name__ == "__main__":
    sys.exit(main())
