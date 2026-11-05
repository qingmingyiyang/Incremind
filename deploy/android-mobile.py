import argparse
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess
import tempfile
import time
from urllib.request import urlopen


ROOTS = "android-attestation-roots.pem"
STATUS = "android-attestation-revocations.json"
OFFICIAL = "https://android.googleapis.com/attestation/"
SERVER_DIR = Path("/etc/chriptmas/android-mobile")
MAX_BYTES = 2 * 1024 * 1024
MAX_IMPORT_AGE_SECONDS = 36 * 3600


def fetch(name):
    url = OFFICIAL + name
    with urlopen(url, timeout=15) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("unexpected Android attestation source")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Android attestation download too large")
    return data


def valid_roots(data):
    text = data.decode("ascii")
    certificates = re.findall(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----", text)
    if not certificates or "".join(text.split()) != "".join("".join(certificates).split()):
        raise ValueError("invalid Android attestation roots")
    for certificate in certificates:
        ssl.PEM_cert_to_DER_cert(certificate)
    return data


def valid_status(data):
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
        raise ValueError("invalid Android attestation revocations")
    for serial, entry in value["entries"].items():
        if (not re.fullmatch(r"[0-9a-f]+", serial) or not isinstance(entry, dict)
                or entry.get("status") not in {"REVOKED", "SUSPENDED"}):
            raise ValueError("invalid Android attestation revocation entry")
    return data


def read_snapshot(directory, name):
    path = directory / name
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        details = os.fstat(handle.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_BYTES:
            raise ValueError("invalid Android attestation snapshot file")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Android attestation snapshot too large")
    if not 0 <= time.time() - details.st_mtime < MAX_IMPORT_AGE_SECONDS:
        raise ValueError("Android attestation snapshot is stale or from the future")
    return data, details.st_mtime


def write_file(path, data, mode, gid=None, modified=None):
    with path.open("xb") as handle:
        os.chmod(path, mode)
        if gid is not None:
            os.fchown(handle.fileno(), 0, gid)
        handle.write(data)
        handle.flush()
        if modified is not None:
            os.utime(handle.fileno(), (modified, modified))
        os.fsync(handle.fileno())


def snapshot(destination):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Android attestation snapshot destination already exists")
    roots = json.loads(fetch("root"))
    if not isinstance(roots, list) or not all(isinstance(cert, str) for cert in roots):
        raise ValueError("invalid official Android attestation roots")
    root_bundle = valid_roots(("\n".join(cert.strip() for cert in roots) + "\n").encode("ascii"))
    revocations = valid_status(fetch("status"))
    with tempfile.TemporaryDirectory(prefix=".attestation-", dir=destination.parent) as scratch:
        staged = Path(scratch) / "snapshot"
        staged.mkdir(mode=0o700)
        write_file(staged / ROOTS, root_bundle, 0o600)
        write_file(staged / STATUS, revocations, 0o600)
        staged.rename(destination)


def configure(args, gid):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", args.package):
        raise ValueError("invalid Android application ID")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", args.signer_sha256):
        raise ValueError("invalid Android release signer fingerprint")
    if SERVER_DIR.exists() or SERVER_DIR.is_symlink():
        raise FileExistsError("Android mobile server configuration already exists")
    with tempfile.TemporaryDirectory(prefix=".android-mobile-", dir=SERVER_DIR.parent) as scratch:
        scratch = Path(scratch)
        source = args.source
        if source is None:
            source = scratch / "official"
            snapshot(source)
        staged = scratch / "server"
        staged.mkdir(mode=0o750)
        os.chown(staged, 0, gid)
        roots, _ = read_snapshot(source, ROOTS)
        status, modified = read_snapshot(source, STATUS)
        write_file(staged / ROOTS, valid_roots(roots), 0o640, gid)
        write_file(staged / STATUS, valid_status(status), 0o640, gid, modified)
        ca = staged / "client-ca.pem"
        key = staged / "client-ca.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-noenc", "-days", "3650", "-subj", "/CN=Incremind Android Client CA",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", key, "-out", ca,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        for path in (key, ca):
            os.chmod(path, 0o640)
            os.chown(path, 0, gid)
        token = secrets.token_hex(32)
        write_file(staged / "proxy-token", (token + "\n").encode("ascii"), 0o640, gid)
        directive = f'proxy_set_header X-Incremind-Mobile-Proxy "{token}";\n'
        write_file(staged / "nginx-proxy-token.conf", directive.encode("ascii"), 0o600)
        write_file(staged / "android-package.txt", (args.package + "\n").encode("ascii"), 0o640, gid)
        write_file(staged / "android-signer-sha256.txt", (args.signer_sha256.lower() + "\n").encode("ascii"), 0o640, gid)
        staged.rename(SERVER_DIR)


def refresh(source, gid):
    import fcntl

    if SERVER_DIR.is_symlink() or not SERVER_DIR.is_dir():
        raise ValueError("Android mobile server configuration unavailable")
    data, modified = (fetch("status"), time.time()) if source is None else read_snapshot(source, STATUS)
    data = valid_status(data)
    destination = SERVER_DIR / STATUS
    flags = os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR
    with os.fdopen(os.open(SERVER_DIR / ".revocations.lock", flags, 0o600), "r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.is_symlink() or destination.stat().st_mtime > modified:
            raise ValueError("Android attestation snapshot would roll back the revocation list")
        with tempfile.TemporaryDirectory(prefix=".revocations-", dir=SERVER_DIR) as scratch:
            incoming = Path(scratch) / STATUS
            write_file(incoming, data, 0o640, gid, modified)
            incoming.replace(destination)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    downloader = commands.add_parser("snapshot")
    downloader.add_argument("destination", type=Path)
    initializer = commands.add_parser("init")
    initializer.add_argument("package")
    initializer.add_argument("signer_sha256")
    updater = commands.add_parser("refresh")
    for command in (initializer, updater):
        command.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot(args.destination)
        return
    if os.name != "posix" or os.geteuid() != 0:
        raise PermissionError("server Android configuration requires root")
    import grp

    gid = grp.getgrnam("chriptmas").gr_gid
    if args.command == "init":
        configure(args, gid)
    else:
        refresh(args.source, gid)


if __name__ == "__main__":
    main()
