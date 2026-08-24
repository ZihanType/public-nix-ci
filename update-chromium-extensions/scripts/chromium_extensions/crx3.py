"""Deterministic ZIP normalization and the minimal official CRX3 envelope.

CRX3 is the signed container format for a .crx file. It is independent of the
extension's `manifest_version` and can contain either Manifest V2 or V3 code.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import tempfile
import threading
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
import zipfile

from .model import VERSION_RE, normalize_extension_name


CRX_MAGIC = b"Cr24"
CRX_VERSION = 3
CRX3_SIGNED_DATA_PREFIX = b"CRX3 SignedData\x00"

# These are operational anti-zip-bomb guardrails, not upstream format limits.
# They are intentionally far above normal browser-extension sizes while keeping
# an untrusted Release asset from exhausting a GitHub-hosted runner.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 60_000
MAX_CRX3_HEADER_BYTES = 1024 * 1024

# Locale message catalogs are tiny in normal extensions. Bounding the one file
# needed to resolve a display name prevents a highly compressed Web Store CRX
# from expanding an untrusted messages.json without limit.
MAX_LOCALIZATION_MESSAGES_BYTES = 1024 * 1024

LOCALIZED_MESSAGE_RE = re.compile(r"__MSG_(.*?)__", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\$(.*?)\$")
MESSAGE_NAME_RE = re.compile(r"[A-Za-z0-9_@]+")
DEFAULT_LOCALE_RE = re.compile(r"[A-Za-z0-9_]+")

# ZIP timestamps cannot represent dates before 1980. A fixed earliest timestamp
# avoids runner filesystem metadata changing otherwise identical CRX bytes.
CANONICAL_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# macOS LibreSSL has shown intermittent verification failures when many short
# lived `openssl dgst` processes operate concurrently. Serializing CLI calls is
# cheap relative to network downloads and keeps local and GitHub runners stable.
_OPENSSL_LOCK = threading.Lock()


def sha256_hex(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varint cannot be negative")
    rendered = bytearray()
    while value >= 0x80:
        rendered.append((value & 0x7F) | 0x80)
        value >>= 7
    rendered.append(value)
    return bytes(rendered)


def _protobuf_bytes(field_number: int, contents: bytes) -> bytes:
    if field_number <= 0:
        raise ValueError("protobuf field number must be positive")
    return _encode_varint((field_number << 3) | 2) + _encode_varint(len(contents)) + contents


def _decode_varint(contents: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(contents) and shift <= 63:
        byte = contents[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid or truncated protobuf varint")


def _protobuf_fields(contents: bytes) -> Tuple[Tuple[int, int, object], ...]:
    fields: List[Tuple[int, int, object]] = []
    offset = 0
    while offset < len(contents):
        tag, offset = _decode_varint(contents, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 0:
            raise ValueError("protobuf field number zero is invalid")
        if wire_type == 0:
            value, offset = _decode_varint(contents, offset)
        elif wire_type == 1:
            if offset + 8 > len(contents):
                raise ValueError("truncated fixed64 protobuf field")
            value = contents[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = _decode_varint(contents, offset)
            if offset + length > len(contents):
                raise ValueError("truncated length-delimited protobuf field")
            value = contents[offset : offset + length]
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(contents):
                raise ValueError("truncated fixed32 protobuf field")
            value = contents[offset : offset + 4]
            offset += 4
        else:
            raise ValueError("unsupported protobuf wire type %d" % wire_type)
        fields.append((field_number, wire_type, value))
    return tuple(fields)


def extension_id_from_public_key(public_key_der: bytes) -> str:
    digest_prefix = hashlib.sha256(public_key_der).digest()[:16]
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest_prefix.hex())


def _extension_id_from_crx_id(crx_id: bytes) -> str:
    if len(crx_id) != 16:
        raise ValueError("CRX3 signed header contains an invalid CRX ID length")
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in crx_id.hex())


def _run_openssl(
    arguments: Sequence[str],
    *,
    input_bytes: Optional[bytes] = None,
    openssl: str = "openssl",
) -> bytes:
    with _OPENSSL_LOCK:
        result = subprocess.run(
            [openssl] + list(arguments),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(
            "OpenSSL command failed (%s): %s"
            % (" ".join(arguments), result.stderr.decode("utf-8", errors="replace").strip())
        )
    return result.stdout


def generate_rsa_private_key(path: Path, *, openssl: str = "openssl") -> None:
    """Generate the 2048-bit RSA key conventionally used by Chromium packers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with _OPENSSL_LOCK:
        result = subprocess.run(
            [openssl, "genrsa", "-out", str(path), "2048"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(
            "failed to generate RSA key: %s"
            % result.stderr.decode("utf-8", errors="replace").strip()
        )
    path.chmod(0o600)


def public_key_der(private_key_path: Path, *, openssl: str = "openssl") -> bytes:
    return _run_openssl(
        ["pkey", "-in", str(private_key_path), "-pubout", "-outform", "DER"],
        openssl=openssl,
    )


@dataclass(frozen=True)
class CanonicalExtension:
    zip_bytes: bytes
    manifest: Mapping[str, object]
    name: str
    source_root: str

    @property
    def version(self) -> str:
        version = self.manifest["version"]
        assert isinstance(version, str)
        return version


@dataclass(frozen=True)
class ExtensionManifest:
    """Validated manifest metadata needed by the artifact pipeline."""

    value: Mapping[str, object]
    name: str

    @property
    def version(self) -> str:
        version = self.value["version"]
        assert isinstance(version, str)
        return version


def _extension_name_from_archive(
    manifest: Mapping[str, object],
    archive: zipfile.ZipFile,
    root: PurePosixPath,
) -> str:
    """Resolve manifest name references against the deterministic default locale."""

    raw_name = manifest.get("name")
    if not isinstance(raw_name, str):
        raise ValueError("manifest.json contains an invalid name")
    references = tuple(LOCALIZED_MESSAGE_RE.finditer(raw_name))
    if not references:
        return normalize_extension_name(raw_name)

    default_locale = manifest.get("default_locale")
    if (
        not isinstance(default_locale, str)
        or not DEFAULT_LOCALE_RE.fullmatch(default_locale)
    ):
        raise ValueError("localized manifest name requires a valid default_locale")
    messages_path = root / "_locales" / default_locale / "messages.json"
    try:
        messages_info = archive.getinfo(messages_path.as_posix())
    except KeyError as error:
        raise ValueError(
            "default locale %s has no messages.json" % default_locale
        ) from error
    if messages_info.file_size > MAX_LOCALIZATION_MESSAGES_BYTES:
        raise ValueError(
            "default locale messages.json exceeds the %d-byte limit"
            % MAX_LOCALIZATION_MESSAGES_BYTES
        )
    try:
        messages_value = json.loads(archive.read(messages_info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("default locale messages.json is not valid UTF-8 JSON") from error
    if not isinstance(messages_value, dict):
        raise ValueError("default locale messages.json must contain an object")

    messages: Dict[str, object] = {}
    for key, message in messages_value.items():
        folded_key = key.casefold()
        # Chromium inserts each catalog item into a lower-cased dictionary in
        # source order, so the last spelling wins when keys differ only by case.
        messages[folded_key] = message

    def replace_message(match: re.Match[str]) -> str:
        key = match.group(1)
        if not MESSAGE_NAME_RE.fullmatch(key) or key.startswith("@@"):
            raise ValueError("manifest name contains an invalid localized message reference")
        message_value = messages.get(key.casefold())
        if not isinstance(message_value, dict):
            raise ValueError("default locale has no message %s" % key)
        message = message_value.get("message")
        if not isinstance(message, str):
            raise ValueError("default locale message %s has no string message" % key)

        placeholders_value = message_value.get("placeholders", {})
        if not isinstance(placeholders_value, dict):
            raise ValueError("default locale message %s has invalid placeholders" % key)
        placeholders: Dict[str, str] = {}
        for placeholder_key, placeholder_value in placeholders_value.items():
            if not MESSAGE_NAME_RE.fullmatch(placeholder_key):
                raise ValueError(
                    "default locale message %s has an invalid placeholder name" % key
                )
            if not isinstance(placeholder_value, dict) or not isinstance(
                placeholder_value.get("content"), str
            ):
                raise ValueError(
                    "default locale message %s has an invalid placeholder %s"
                    % (key, placeholder_key)
                )
            placeholders[placeholder_key.casefold()] = placeholder_value["content"]

        def replace_placeholder(placeholder_match: re.Match[str]) -> str:
            placeholder_key = placeholder_match.group(1)
            if not MESSAGE_NAME_RE.fullmatch(placeholder_key):
                return placeholder_match.group(0)
            content = placeholders.get(placeholder_key.casefold())
            if content is None:
                raise ValueError(
                    "default locale message %s uses undefined placeholder %s"
                    % (key, placeholder_key)
                )
            return content

        return PLACEHOLDER_RE.sub(replace_placeholder, message)

    return normalize_extension_name(LOCALIZED_MESSAGE_RE.sub(replace_message, raw_name))


def _safe_archive_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise ValueError("ZIP contains an invalid path: %r" % name)
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("ZIP contains an unsafe path: %r" % name)
    return path


def canonicalize_extension_zip(contents: bytes) -> CanonicalExtension:
    if len(contents) > MAX_ARCHIVE_BYTES:
        raise ValueError("ZIP exceeds the %d-byte compressed size limit" % MAX_ARCHIVE_BYTES)

    try:
        archive = zipfile.ZipFile(io.BytesIO(contents))
    except (zipfile.BadZipFile, OSError) as error:
        raise ValueError("upstream asset is not a valid ZIP: %s" % error) from error

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise ValueError("ZIP exceeds the %d-entry limit" % MAX_ARCHIVE_FILES)
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                "ZIP exceeds the %d-byte uncompressed size limit" % MAX_UNCOMPRESSED_BYTES
            )

        paths: Dict[PurePosixPath, zipfile.ZipInfo] = {}
        manifest_paths: List[PurePosixPath] = []
        for info in infos:
            path = _safe_archive_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
            if path in paths:
                raise ValueError("ZIP contains a duplicate path: %s" % path)
            paths[path] = info
            if info.flag_bits & 0x1:
                raise ValueError("ZIP contains an encrypted entry: %s" % path)

            unix_mode = info.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if file_type == stat.S_IFLNK:
                raise ValueError("ZIP contains a symbolic link: %s" % path)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("ZIP contains a special filesystem entry: %s" % path)
            if not info.is_dir() and path.name == "manifest.json":
                manifest_paths.append(path)

        if len(manifest_paths) != 1:
            raise ValueError(
                "ZIP must contain exactly one extension manifest.json; found %d"
                % len(manifest_paths)
            )
        manifest_path = manifest_paths[0]
        root = manifest_path.parent

        try:
            manifest_value = json.loads(archive.read(paths[manifest_path]).decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("manifest.json is not valid UTF-8 JSON: %s" % error) from error
        if not isinstance(manifest_value, dict):
            raise ValueError("manifest.json must contain an object")
        version = manifest_value.get("version")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise ValueError("manifest.json contains an invalid version")
        extension_name = _extension_name_from_archive(manifest_value, archive, root)

        canonical_files: List[Tuple[str, int, bytes]] = []
        for path, info in paths.items():
            if info.is_dir():
                continue
            try:
                relative = path.relative_to(root) if root.parts else path
            except ValueError:
                # Files outside the unique extension root are packaging metadata,
                # not part of the browser extension.
                continue
            relative_name = relative.as_posix()
            if not relative_name:
                continue
            unix_mode = info.external_attr >> 16
            permissions = 0o755 if unix_mode & 0o111 else 0o644
            canonical_files.append((relative_name, permissions, archive.read(info)))

    canonical_files.sort(key=lambda item: item[0].encode("utf-8"))
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as rendered:
        rendered.comment = b""
        for relative_name, permissions, file_contents in canonical_files:
            info = zipfile.ZipInfo(relative_name, date_time=CANONICAL_ZIP_TIMESTAMP)
            info.create_system = 3  # Unix, so external_attr permissions have a stable meaning.
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | permissions) << 16
            info.extra = b""
            info.comment = b""
            rendered.writestr(info, file_contents)

    source_root = "." if not root.parts else root.as_posix()
    return CanonicalExtension(output.getvalue(), manifest_value, extension_name, source_root)


@dataclass(frozen=True)
class CrxProof:
    algorithm: str
    public_key: bytes
    signature: bytes


@dataclass(frozen=True)
class ParsedCrx3:
    extension_id: str
    crx_id: bytes
    signed_header_data: bytes
    proofs: Tuple[CrxProof, ...]
    zip_bytes: bytes

    @property
    def signed_payload(self) -> bytes:
        return (
            CRX3_SIGNED_DATA_PREFIX
            + struct.pack("<I", len(self.signed_header_data))
            + self.signed_header_data
            + self.zip_bytes
        )


def parse_crx3(contents: bytes) -> ParsedCrx3:
    if len(contents) < 12 or contents[:4] != CRX_MAGIC:
        raise ValueError("artifact does not have a CRX header")
    version, header_size = struct.unpack("<II", contents[4:12])
    if version != CRX_VERSION:
        raise ValueError("expected CRX3, found CRX%d" % version)
    if header_size > MAX_CRX3_HEADER_BYTES:
        raise ValueError("CRX3 header exceeds the 1-MiB safety limit")
    if header_size > len(contents) - 12:
        raise ValueError("CRX3 header length exceeds artifact size")
    header = contents[12 : 12 + header_size]
    if any(token in header for token in (b"PK\x05\x06", b"PK\x06\x07", b"PK\x06\x06")):
        raise ValueError("CRX3 header contains a forbidden ZIP end-of-directory token")
    zip_bytes = contents[12 + header_size :]
    if not zip_bytes.startswith(b"PK"):
        raise ValueError("CRX3 payload is not a ZIP archive")

    proofs: List[CrxProof] = []
    signed_header_values: List[bytes] = []
    for field_number, wire_type, value in _protobuf_fields(header):
        if wire_type != 2:
            continue
        assert isinstance(value, bytes)
        if field_number in {2, 3}:  # RSA-SHA256 or ECDSA-SHA256 proof.
            proof_public_keys: List[bytes] = []
            proof_signatures: List[bytes] = []
            for proof_field, proof_wire, proof_value in _protobuf_fields(value):
                if proof_wire != 2:
                    continue
                assert isinstance(proof_value, bytes)
                if proof_field == 1:
                    proof_public_keys.append(proof_value)
                elif proof_field == 2:
                    proof_signatures.append(proof_value)
            if len(proof_public_keys) != 1 or len(proof_signatures) != 1:
                raise ValueError("CRX3 contains a malformed asymmetric key proof")
            algorithm = "rsa" if field_number == 2 else "ecdsa"
            proofs.append(CrxProof(algorithm, proof_public_keys[0], proof_signatures[0]))
        elif field_number == 10000:
            signed_header_values.append(value)

    if not proofs:
        raise ValueError("CRX3 contains no supported SHA-256 signature proof")
    if len(signed_header_values) != 1:
        raise ValueError("CRX3 must contain exactly one signed_header_data field")
    signed_header_data = signed_header_values[0]
    crx_ids = [
        value
        for field_number, wire_type, value in _protobuf_fields(signed_header_data)
        if field_number == 1 and wire_type == 2
    ]
    if len(crx_ids) != 1 or not isinstance(crx_ids[0], bytes):
        raise ValueError("CRX3 signed header must contain exactly one CRX ID")
    crx_id = crx_ids[0]
    return ParsedCrx3(
        _extension_id_from_crx_id(crx_id),
        crx_id,
        signed_header_data,
        tuple(proofs),
        zip_bytes,
    )


def _read_der_element(contents: bytes, offset: int) -> Tuple[int, bytes, int]:
    if offset >= len(contents):
        raise ValueError("truncated DER element")
    tag = contents[offset]
    offset += 1
    if offset >= len(contents):
        raise ValueError("truncated DER length")
    first_length = contents[offset]
    offset += 1
    if first_length & 0x80:
        length_octets = first_length & 0x7F
        if length_octets == 0 or length_octets > 4 or offset + length_octets > len(contents):
            raise ValueError("invalid DER length")
        length = int.from_bytes(contents[offset : offset + length_octets], "big")
        offset += length_octets
    else:
        length = first_length
    if offset + length > len(contents):
        raise ValueError("DER element exceeds input")
    return tag, contents[offset : offset + length], offset + length


def _rsa_numbers_from_spki(public_key: bytes) -> Tuple[int, int]:
    tag, spki, end = _read_der_element(public_key, 0)
    if tag != 0x30 or end != len(public_key):
        raise ValueError("RSA public key is not a DER SPKI sequence")
    algorithm_tag, _, offset = _read_der_element(spki, 0)
    if algorithm_tag != 0x30:
        raise ValueError("RSA SPKI has no algorithm sequence")
    bit_string_tag, bit_string, offset = _read_der_element(spki, offset)
    if bit_string_tag != 0x03 or offset != len(spki) or not bit_string or bit_string[0] != 0:
        raise ValueError("RSA SPKI has an invalid public-key bit string")
    rsa_tag, rsa_sequence, rsa_end = _read_der_element(bit_string[1:], 0)
    if rsa_tag != 0x30 or rsa_end != len(bit_string) - 1:
        raise ValueError("RSA public key is not a sequence")
    modulus_tag, modulus_bytes, rsa_offset = _read_der_element(rsa_sequence, 0)
    exponent_tag, exponent_bytes, rsa_offset = _read_der_element(rsa_sequence, rsa_offset)
    if modulus_tag != 0x02 or exponent_tag != 0x02 or rsa_offset != len(rsa_sequence):
        raise ValueError("RSA public key has invalid integer fields")
    modulus = int.from_bytes(modulus_bytes, "big", signed=False)
    exponent = int.from_bytes(exponent_bytes, "big", signed=False)
    if modulus <= 0 or exponent <= 1:
        raise ValueError("RSA public key has invalid values")
    return modulus, exponent


def _verify_rsa_pkcs1_sha256(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    """Verify RSA PKCS#1 v1.5 without sharing OpenSSL with the signer.

    This is public-key verification only: modular exponentiation recovers the
    encoded DigestInfo, which is compared to a locally computed SHA-256 digest.
    """

    try:
        modulus, exponent = _rsa_numbers_from_spki(public_key)
    except ValueError:
        return False
    modulus_size = (modulus.bit_length() + 7) // 8
    if len(signature) != modulus_size:
        return False
    encoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(
        modulus_size, "big"
    )
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(
        payload
    ).digest()
    padding_size = modulus_size - len(digest_info) - 3
    if padding_size < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding_size + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def _verify_ecdsa_signature(
    public_key: bytes,
    signature: bytes,
    payload: bytes,
    *,
    openssl: str,
) -> bool:
    with tempfile.TemporaryDirectory(prefix="verify-crx3-") as directory:
        root = Path(directory)
        der_path = root / "public.der"
        signature_path = root / "signature.bin"
        payload_path = root / "payload.bin"
        der_path.write_bytes(public_key)
        signature_path.write_bytes(signature)
        payload_path.write_bytes(payload)
        # `dgst -keyform DER` accepts the CRX proof's SubjectPublicKeyInfo
        # directly. Avoiding a separate DER-to-PEM process also makes parallel
        # verification cheaper and removes an unnecessary temporary artifact.
        arguments = [
            openssl,
            "dgst",
            "-sha256",
            "-verify",
            str(der_path),
            "-keyform",
            "DER",
            "-signature",
            str(signature_path),
            str(payload_path),
        ]
        with _OPENSSL_LOCK:
            result = subprocess.run(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        return result.returncode == 0


def verify_crx3(contents: bytes, *, openssl: str = "openssl") -> ParsedCrx3:
    parsed = parse_crx3(contents)
    valid_proof = False
    for proof in parsed.proofs:
        if hashlib.sha256(proof.public_key).digest()[:16] != parsed.crx_id:
            continue
        if proof.algorithm == "rsa":
            valid_signature = _verify_rsa_pkcs1_sha256(
                proof.public_key, proof.signature, parsed.signed_payload
            )
        else:
            valid_signature = _verify_ecdsa_signature(
                proof.public_key,
                proof.signature,
                parsed.signed_payload,
                openssl=openssl,
            )
        if valid_signature:
            valid_proof = True
            break
    if not valid_proof:
        raise ValueError("CRX3 has no valid signature for its signed extension ID")
    return parsed


def extension_manifest_from_crx3(
    contents: bytes, *, openssl: str = "openssl"
) -> ExtensionManifest:
    parsed = verify_crx3(contents, openssl=openssl)
    try:
        with zipfile.ZipFile(io.BytesIO(parsed.zip_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("CRX3 manifest.json must contain an object")
            version = manifest.get("version")
            if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
                raise ValueError("CRX3 manifest.json contains an invalid version")
            name = _extension_name_from_archive(manifest, archive, PurePosixPath())
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("CRX3 does not contain a valid root manifest.json: %s" % error) from error
    return ExtensionManifest(manifest, name)


def manifest_from_crx3(contents: bytes, *, openssl: str = "openssl") -> Mapping[str, object]:
    """Return the validated raw manifest for callers that do not need metadata."""

    return extension_manifest_from_crx3(contents, openssl=openssl).value


def build_crx3(canonical_zip: bytes, private_key_path: Path, *, openssl: str = "openssl") -> bytes:
    public_der = public_key_der(private_key_path, openssl=openssl)
    crx_id = hashlib.sha256(public_der).digest()[:16]
    signed_header_data = _protobuf_bytes(1, crx_id)
    signed_payload = (
        CRX3_SIGNED_DATA_PREFIX
        + struct.pack("<I", len(signed_header_data))
        + signed_header_data
        + canonical_zip
    )
    signature = _run_openssl(
        ["dgst", "-sha256", "-sign", str(private_key_path)],
        input_bytes=signed_payload,
        openssl=openssl,
    )
    proof = _protobuf_bytes(1, public_der) + _protobuf_bytes(2, signature)
    header = _protobuf_bytes(2, proof) + _protobuf_bytes(10000, signed_header_data)
    return CRX_MAGIC + struct.pack("<II", CRX_VERSION, len(header)) + header + canonical_zip


@dataclass(frozen=True)
class BuiltCrx3:
    contents: bytes
    extension_id: str
    public_key_fingerprint: str


def build_reproducible_crx3(
    canonical_zip: bytes,
    private_key_path: Path,
    *,
    openssl: str = "openssl",
) -> BuiltCrx3:
    first = build_crx3(canonical_zip, private_key_path, openssl=openssl)
    second = build_crx3(canonical_zip, private_key_path, openssl=openssl)
    if first != second:
        raise ValueError("CRX3 signer produced non-reproducible output")
    parsed = verify_crx3(first, openssl=openssl)
    if parsed.zip_bytes != canonical_zip:
        raise ValueError("verified CRX3 payload differs from the canonical ZIP")
    public_der = public_key_der(private_key_path, openssl=openssl)
    expected_id = extension_id_from_public_key(public_der)
    if parsed.extension_id != expected_id:
        raise ValueError("verified CRX3 ID differs from its signing key")
    return BuiltCrx3(first, expected_id, "sha256:" + sha256_hex(public_der))
