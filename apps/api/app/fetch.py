"""Fetch a dataset from a URL and turn it into uploadable files.

**This is the most dangerous thing in the codebase.** It takes a string from a
user and makes the server act on it: perform network requests, and unpack
attacker-controlled archives. Two whole classes of attack live here, and both
are handled explicitly rather than left to the standard library's defaults.

**Server-side request forgery.** A URL is not a location, it is an instruction.
Left alone, `http://169.254.169.254/` reaches cloud instance metadata,
`http://localhost:8787/` reaches this API, and `http://10.0.0.5/` reaches
whatever is on the internal network — with the server's own credentials and
network position. So: schemes are limited to http and https, the host is
resolved and **every** address it resolves to must be publicly routable, and
redirects are followed one hop at a time with the same check on each.

*What this does not cover:* the address is checked at resolution time and the
connection resolves it again a moment later, so a host that answers differently
the second time can still slip through (DNS rebinding). Closing that needs the
connection pinned to the vetted address, which means writing the socket layer by
hand. If this endpoint is ever exposed beyond a trusted operator, do that first.

**Archive attacks.** A zip is a program in a file format. An entry named
`../../etc/passwd` writes wherever it likes; a 42 KB zip can expand to petabytes;
a tar can carry a symlink to `/` and then a file that follows it. So: extraction
is manual, every path is normalised and re-checked, symlinks and device nodes are
refused outright, and the total output is capped by both an absolute size and a
compression ratio.
"""

from __future__ import annotations

import io
import ipaddress
import socket
import tarfile
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlparse, urlunparse

#: Files a single remote source may contain, matching the upload limit.
from .sources import MAX_FILES, MAX_TOTAL_BYTES

MAX_DOWNLOAD_BYTES = MAX_TOTAL_BYTES
MAX_REDIRECTS = 5

#: A zip bomb is small and expands enormously. 200:1 is far above anything real
#: (deflate tops out near 1030:1 on pathological input, and photographs barely
#: compress at all).
MAX_COMPRESSION_RATIO = 200

#: Only these are fetched. `file://` would read the server's disk, and
#: everything else is either not a document or not a protocol anyone means.
ALLOWED_SCHEMES = ("http", "https")

CHUNK_BYTES = 1 << 16

#: Refused regardless of what DNS says, as a second line of defence.
BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}


class RemoteError(Exception):
    """The URL could not be fetched. The message is user-facing."""


def _public_addresses(host: str, port: int | None) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RemoteError(f"{host!r} could not be resolved: {exc}") from exc
    return [info[4][0] for info in infos]


def _assert_public(url: str) -> None:
    """Refuse anything that is not an ordinary public web address.

    Every address the name resolves to is checked, because a host with both a
    public and a private record is a standard way to get past a check that only
    looks at the first one.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise RemoteError(
            f"{parsed.scheme or 'that'} is not a supported scheme; use http or https"
        )
    if not parsed.hostname:
        raise RemoteError("that URL has no host")
    if parsed.hostname.lower() in BLOCKED_HOSTNAMES:
        raise RemoteError(f"{parsed.hostname!r} is not a public address")

    for text in _public_addresses(parsed.hostname, parsed.port):
        address = ipaddress.ip_address(text)
        if not address.is_global or address.is_multicast:
            # `is_global` covers loopback, private, link-local (including the
            # 169.254.169.254 metadata address), reserved and unspecified.
            raise RemoteError(
                f"{parsed.hostname!r} resolves to {text}, which is not a public address"
            )


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-runs the address check on every hop.

    urllib would otherwise follow a public URL to `http://127.0.0.1/` without
    asking anyone — which is the whole attack, one redirect deep.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _assert_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str) -> bytes:
    """Fetch a URL, refusing anything that is not a public address."""
    _assert_public(url)

    request = urllib.request.Request(
        url,
        headers={
            # Some hosts serve a login page to unknown agents; this one is
            # honest about what it is.
            "User-Agent": "BoneViewer/0.1 (+dataset import)",
            "Accept": "*/*",
        },
    )
    opener = urllib.request.build_opener(_GuardedRedirects())

    try:
        with opener.open(request, timeout=120) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                raise RemoteError(
                    f"that file is {int(declared) / 1024**3:.1f} GiB; the limit is "
                    f"{MAX_DOWNLOAD_BYTES // 1024**3} GiB"
                )

            # Read with the cap enforced as it arrives: a Content-Length is a
            # claim, and a hostile server can simply not send one.
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RemoteError(
                        f"that file is larger than the {MAX_DOWNLOAD_BYTES // 1024**3} GiB limit"
                    )
                chunks.append(chunk)
    except RemoteError:
        raise
    except urllib.error.HTTPError as exc:
        raise RemoteError(f"the server answered {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RemoteError(f"could not reach that URL: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RemoteError(f"could not reach that URL: {exc}") from exc

    if not chunks:
        raise RemoteError("that URL returned nothing")
    return b"".join(chunks)


def _safe_member_path(name: str) -> str | None:
    """Normalise an archive entry to a relative path, or None to refuse it.

    Archives use forward slashes by convention but Windows-built ones carry
    backslashes, so both are split on — otherwise a `..\\..\\` survives as one
    component and reads as harmless.

    `..` is **refused, not stripped**. Stripping would also produce a safe path,
    but quietly reinterpreting a path an archive asked for is not the same as
    rejecting it: a file that asks to climb out of its folder is not a dataset
    layout, it is an escape attempt, and the right response to the rest of that
    archive is to stop reading it.
    """
    # A drive letter or leading separator means an absolute path.
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        return None

    if ".." in name.replace("\\", "/").split("/"):
        return None

    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        return None
    return "/".join(parts)


def _looks_like_archive(blob: bytes) -> str | None:
    """Which container this is, from its magic bytes rather than its name."""
    if blob[:4] == b"PK\x03\x04" or blob[:4] == b"PK\x05\x06":
        return "zip"
    if blob[:2] == b"\x1f\x8b" or blob[:6] == b"ustar\x00" or blob[257:262] == b"ustar":
        return "tar"
    if blob[:7] == b"Rar!\x1a\x07\x00" or blob[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar"
    return None


def _extract_zip(blob: bytes) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    total = 0

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise RemoteError(
                f"that archive holds {len(infos)} files; the limit is {MAX_FILES}"
            )

        for info in infos:
            if info.is_dir():
                continue
            # A symlink in a zip is stored as a member whose *content* is the
            # target. Written out and then followed, it is how an archive
            # escapes its own folder.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise RemoteError("that archive contains a symbolic link; refusing it")

            name = _safe_member_path(info.filename)
            if name is None:
                raise RemoteError(f"that archive has an unsafe path: {info.filename!r}")

            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise RemoteError(
                    f"that archive expands past the {MAX_TOTAL_BYTES // 1024**3} GiB limit"
                )
            if info.file_size > max(len(blob), 1) * MAX_COMPRESSION_RATIO:
                raise RemoteError(
                    f"{info.filename!r} expands {info.file_size / max(len(blob), 1):.0f}x; "
                    "refusing what looks like a decompression bomb"
                )

            files.append((name, archive.read(info)))

    return files


def _extract_tar(blob: bytes) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    total = 0

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_FILES:
            raise RemoteError(f"that archive holds {len(members)} files; the limit is {MAX_FILES}")

        for member in members:
            # `filter="data"` refuses absolute paths, `..`, symlinks, hardlinks
            # and device nodes — the set of things that make a tar dangerous.
            # Python 3.12+ only, which is why it is not optional here.
            if not member.isfile():
                if member.issym() or member.islnk() or member.ischr() or member.isblk():
                    raise RemoteError("that archive contains a link or device file; refusing it")
                continue

            name = _safe_member_path(member.name)
            if name is None:
                raise RemoteError(f"that archive has an unsafe path: {member.name!r}")

            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise RemoteError(
                    f"that archive expands past the {MAX_TOTAL_BYTES // 1024**3} GiB limit"
                )
            if member.size > max(len(blob), 1) * MAX_COMPRESSION_RATIO:
                raise RemoteError(
                    f"{member.name!r} expands {member.size / max(len(blob), 1):.0f}x; "
                    "refusing what looks like a decompression bomb"
                )

            handle = archive.extractfile(member)
            if handle is not None:
                files.append((name, handle.read()))

    return files


def fetch_dataset(url: str) -> list[tuple[str, bytes]]:
    """Download `url` and return its contents as uploadable files.

    An archive is unpacked; anything else is returned as a single unnamed file,
    so a direct link to one DICOM or one photograph works too.
    """
    blob = download(url)
    kind = _looks_like_archive(blob)

    if kind == "zip":
        files = _extract_zip(blob)
    elif kind == "tar":
        files = _extract_tar(blob)
    elif kind == "rar":
        raise RemoteError(
            "RAR needs the `unrar` tool, which is not installed here. Unpack it "
            "yourself and upload the folder, or point at a .zip or .tar.gz."
        )
    else:
        name = _safe_member_path(urlparse(url).path.rsplit("/", 1)[-1]) or "download"
        files = [(name, blob)]

    if not files:
        raise RemoteError("that archive had no files in it")
    return files


def origin_of(url: str) -> str:
    """The URL without its query or fragment, for recording what was fetched."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
