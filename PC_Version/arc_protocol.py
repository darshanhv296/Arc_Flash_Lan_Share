"""
Arc Flash Binary Wire Protocol ("AF01") & Media Category Auto-Filter.

100% wire-compatible with Android's ArcBinaryProtocol.kt and ArcTransferConfig.kt:
- Session preface: 4-byte ASCII magic b"AF01"
- Frame layout: [1B type][4B big-endian signed int payload_len][payload]
- IEEE 802.3 CRC32 + GF(2) combine_crc32 for chunk-ordered verification
- Safe path confinement & ShareMe-style category extension filters
"""

from __future__ import annotations

import os
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Iterable, Tuple

MAGIC_BYTES = b"AF01"

DEFAULT_PORT = 53317
PORT_FALLBACK_COUNT = 10
UDP_DISCOVERY_PORT = 45890
SERVICE_TYPE = "_arcflash._tcp.local."
SERVICE_PREFIX = "arcflash-"

# Frame types
TYPE_HELLO = 0x01
TYPE_FILE_META = 0x02
TYPE_DATA = 0x03
TYPE_ACK = 0x04
TYPE_DONE = 0x05
TYPE_ERROR = 0x06
TYPE_DONE_ACK = 0x07
TYPE_CONTROL = 0x08

# Control actions
CTRL_PAUSE = 0x01
CTRL_RESUME = 0x02
CTRL_STOP = 0x03
CTRL_DISCONNECT = 0x04

CONTROL_ACTION_NAMES = {
    CTRL_PAUSE: "PAUSE",
    CTRL_RESUME: "RESUME",
    CTRL_STOP: "STOP",
    CTRL_DISCONNECT: "DISCONNECT",
}

ACTION_TO_CONTROL_CODE = {
    "PAUSE": CTRL_PAUSE,
    "RESUME": CTRL_RESUME,
    "STOP": CTRL_STOP,
    "DISCONNECT": CTRL_DISCONNECT,
}

# Performance tuning constants matching ArcTransferConfig.kt
DATA_HEADER_SIZE = 21
DATA_FULL_HEADER_SIZE = 26  # 5B frame header + 21B DATA header
MAX_FRAME_PAYLOAD_BYTES = 16 * 1024 * 1024

CHUNK_SIZE_BYTES = 1024 * 1024          # 1 MB default chunk size
MAX_CHUNK_SIZE_BYTES = 4 * 1024 * 1024  # 4 MB max chunk size
MIN_CHUNK_SIZE_BYTES = 64 * 1024
LOGICAL_STREAM_COUNT = 4

SOCKET_SEND_BUFFER_BYTES = 4 * 1024 * 1024
SOCKET_RECV_BUFFER_BYTES = 4 * 1024 * 1024
SOCKET_STREAM_BUFFER_BYTES = 256 * 1024
DISK_BUFFER_BYTES = 1024 * 1024
SLIDING_WINDOW_BYTES = 32 * 1024 * 1024
ACK_GRANULARITY_BYTES = 2 * 1024 * 1024
PIPELINE_QUEUE_CAPACITY = 32
SOCKET_CONNECT_TIMEOUT_S = 8.0
SOCKET_READ_TIMEOUT_S = 2.0

_FRAME_HEADER_STRUCT = struct.Struct(">Bi")
_DATA_FULL_HEADER_STRUCT = struct.Struct(">Bi i B i i q".replace(" ", ""))
_DATA_META_STRUCT = struct.Struct(">i B i i q".replace(" ", ""))
_ACK_STRUCT = struct.Struct(">iqi")
_DONE_STRUCT = struct.Struct(">iqqi")
_DONE_ACK_STRUCT = struct.Struct(">iqB")


@dataclass(frozen=True)
class HelloFrame:
    device_id: str
    device_name: str
    listen_port: int
    stream_count: int
    chunk_size: int


@dataclass(frozen=True)
class FileMetaFrame:
    file_id: int
    total_size: int
    chunk_size: int
    total_chunks: int
    relative_name: str
    batch_id: str
    transfer_id: str


@dataclass(frozen=True)
class AckFrame:
    file_id: int
    acked_bytes_total: int
    credit_bytes_granted: int


@dataclass(frozen=True)
class DoneFrame:
    file_id: int
    total_bytes: int
    crc32: int
    total_chunks: int


@dataclass(frozen=True)
class DoneAckFrame:
    file_id: int
    bytes_saved: int
    crc32_valid: bool


@dataclass(frozen=True)
class ErrorFrame:
    file_id: int
    message: str


@dataclass(frozen=True)
class ControlFrame:
    action_code: int
    peer_id: str


def encode_str(value: str) -> bytes:
    raw = (value or "").encode("utf-8")[:65535]
    return struct.pack(">H", len(raw)) + raw


def decode_str(buf: bytes, offset: int) -> Tuple[str, int]:
    if offset + 2 > len(buf):
        raise ValueError("Truncated string length")
    (length,) = struct.unpack_from(">H", buf, offset)
    offset += 2
    if offset + length > len(buf):
        raise ValueError("Truncated string payload")
    text = buf[offset : offset + length].decode("utf-8", errors="replace")
    return text, offset + length


def pack_frame(frame_type: int, payload: bytes = b"") -> bytes:
    return _FRAME_HEADER_STRUCT.pack(frame_type & 0xFF, len(payload)) + payload


def pack_data_header(
    file_id: int,
    stream_id: int,
    chunk_index: int,
    total_chunks: int,
    offset: int,
    data_length: int,
) -> bytes:
    payload_len = DATA_HEADER_SIZE + data_length
    return _DATA_FULL_HEADER_STRUCT.pack(
        TYPE_DATA,
        payload_len,
        file_id,
        stream_id & 0xFF,
        chunk_index,
        total_chunks,
        offset,
    )


def unpack_data_meta(buf: bytes) -> Tuple[int, int, int, int, int]:
    """Returns (file_id, stream_id, chunk_index, total_chunks, offset)."""
    return _DATA_META_STRUCT.unpack_from(buf, 0)


def encode_hello(frame: HelloFrame) -> bytes:
    return (
        encode_str(frame.device_id)
        + encode_str(frame.device_name)
        + struct.pack(">iBi", frame.listen_port, frame.stream_count & 0xFF, frame.chunk_size)
    )


def decode_hello(payload: bytes) -> HelloFrame:
    did, off = decode_str(payload, 0)
    name, off = decode_str(payload, off)
    listen_port, stream_count, chunk_size = struct.unpack_from(">iBi", payload, off)
    return HelloFrame(did, name, listen_port, stream_count, chunk_size)


def encode_file_meta(frame: FileMetaFrame) -> bytes:
    return (
        struct.pack(">iqii", frame.file_id, frame.total_size, frame.chunk_size, frame.total_chunks)
        + encode_str(frame.relative_name)
        + encode_str(frame.batch_id)
        + encode_str(frame.transfer_id)
    )


def decode_file_meta(payload: bytes) -> FileMetaFrame:
    file_id, total_size, chunk_size, total_chunks = struct.unpack_from(">iqii", payload, 0)
    off = 20
    relative_name, off = decode_str(payload, off)
    batch_id, off = decode_str(payload, off)
    transfer_id, _ = decode_str(payload, off)
    return FileMetaFrame(
        file_id=file_id,
        total_size=total_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        relative_name=relative_name,
        batch_id=batch_id,
        transfer_id=transfer_id,
    )


def encode_ack(frame: AckFrame) -> bytes:
    return _ACK_STRUCT.pack(frame.file_id, frame.acked_bytes_total, frame.credit_bytes_granted)


def decode_ack(payload: bytes) -> AckFrame:
    file_id, acked_bytes_total, credit_bytes_granted = _ACK_STRUCT.unpack_from(payload, 0)
    return AckFrame(file_id, acked_bytes_total, credit_bytes_granted)


def encode_done(frame: DoneFrame) -> bytes:
    return _DONE_STRUCT.pack(frame.file_id, frame.total_bytes, frame.crc32, frame.total_chunks)


def decode_done(payload: bytes) -> DoneFrame:
    file_id, total_bytes, crc32, total_chunks = _DONE_STRUCT.unpack_from(payload, 0)
    return DoneFrame(file_id, total_bytes, crc32, total_chunks)


def encode_done_ack(frame: DoneAckFrame) -> bytes:
    return _DONE_ACK_STRUCT.pack(frame.file_id, frame.bytes_saved, 1 if frame.crc32_valid else 0)


def decode_done_ack(payload: bytes) -> DoneAckFrame:
    file_id, bytes_saved, flag = _DONE_ACK_STRUCT.unpack_from(payload, 0)
    return DoneAckFrame(file_id, bytes_saved, flag == 1)


def encode_error(frame: ErrorFrame) -> bytes:
    return struct.pack(">i", frame.file_id) + encode_str(frame.message)


def decode_error(payload: bytes) -> ErrorFrame:
    (file_id,) = struct.unpack_from(">i", payload, 0)
    msg, _ = decode_str(payload, 4)
    return ErrorFrame(file_id, msg)


def encode_control(frame: ControlFrame) -> bytes:
    return struct.pack(">B", frame.action_code & 0xFF) + encode_str(frame.peer_id)


def decode_control(payload: bytes) -> ControlFrame:
    (code,) = struct.unpack_from(">B", payload, 0)
    peer_id, _ = decode_str(payload, 1)
    return ControlFrame(code, peer_id)


def crc32_chunk(data: bytes | bytearray | memoryview) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _gf2_matrix_times(mat: list[int], vec: int) -> int:
    acc = 0
    idx = 0
    while vec:
        if vec & 1:
            acc ^= mat[idx]
        vec >>= 1
        idx += 1
    return acc


def _gf2_matrix_square(mat: list[int]) -> list[int]:
    return [_gf2_matrix_times(mat, mat[n]) for n in range(32)]


def combine_crc32(crc1_in: int, crc2_in: int, len2: int) -> int:
    """Combines two IEEE 802.3 CRC32 values in O(log(len2)) matching ArcBinaryProtocol.combineCrc32."""
    if len2 <= 0:
        return crc1_in & 0xFFFFFFFF
    crc1 = crc1_in & 0xFFFFFFFF
    crc2 = crc2_in & 0xFFFFFFFF

    odd = [0] * 32
    odd[0] = 0xEDB88320
    row = 1
    for n in range(1, 32):
        odd[n] = row
        row <<= 1

    even = _gf2_matrix_square(odd)
    odd = _gf2_matrix_square(even)

    while True:
        even = _gf2_matrix_square(odd)
        if len2 & 1:
            crc1 = _gf2_matrix_times(even, crc1)
        len2 >>= 1
        if len2 == 0:
            break

        odd = _gf2_matrix_square(even)
        if len2 & 1:
            crc1 = _gf2_matrix_times(odd, crc1)
        len2 >>= 1
        if len2 == 0:
            break

    return (crc1 ^ crc2) & 0xFFFFFFFF


def read_exact(sock_or_stream, n: int) -> bytes | None:
    """Reads exactly n bytes from a buffered stream or socket. Returns None on clean EOF at byte 0."""
    if n <= 0:
        return b""
    is_sock = hasattr(sock_or_stream, "recv")
    chunks = bytearray()
    while len(chunks) < n:
        remaining = n - len(chunks)
        data = sock_or_stream.recv(remaining) if is_sock else sock_or_stream.read(remaining)
        if not data:
            if len(chunks) == 0:
                return None
            raise EOFError(f"Unexpected EOF reading binary frame ({remaining} bytes remaining)")
        chunks.extend(data)
    return bytes(chunks)


def read_frame_header(sock_or_stream) -> Tuple[int, int] | None:
    raw = read_exact(sock_or_stream, 5)
    if raw is None:
        return None
    frame_type, payload_len = _FRAME_HEADER_STRUCT.unpack(raw)
    if payload_len < 0 or payload_len > MAX_FRAME_PAYLOAD_BYTES:
        raise ValueError(f"Invalid frame payload size: {payload_len}")
    return frame_type, payload_len


_UNSAFE_SEGMENT_RE = re.compile(r'[\x00-\x1f<>:"|?*]')


def sanitize_relative_path(raw_name: str) -> Tuple[list[str], str]:
    """
    Normalizes an incoming relative file path into safe (sub_dirs, filename) preventing path traversal.
    """
    cleaned = (raw_name or "").replace("\\", "/").strip()
    # Strip Windows drive letters if accidentally included
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")

    segments: list[str] = []
    for raw_seg in cleaned.split("/"):
        seg = _UNSAFE_SEGMENT_RE.sub("_", raw_seg).strip().strip(".")
        if not seg or seg in (".", ".."):
            continue
        segments.append(seg[:180])

    if not segments:
        return [], "received_file"
    return segments[:-1], segments[-1]


def reserve_unique_confined_path(base_dir: str, raw_relative_name: str) -> str:
    """
    Resolves a unique destination file inside base_dir, verifying strict path confinement.
    """
    real_base = os.path.realpath(base_dir)
    os.makedirs(real_base, exist_ok=True)
    sub_dirs, filename = sanitize_relative_path(raw_relative_name)
    target_dir = os.path.realpath(os.path.join(real_base, *sub_dirs)) if sub_dirs else real_base
    if target_dir != real_base and not target_dir.startswith(real_base + os.sep):
        raise ValueError("Path escapes configured storage directory")
    os.makedirs(target_dir, exist_ok=True)

    candidate = os.path.realpath(os.path.join(target_dir, filename))
    if not candidate.startswith(real_base + os.sep):
        raise ValueError("File path escapes configured storage directory")

    if not os.path.exists(candidate):
        return candidate

    stem, ext = os.path.splitext(filename)
    counter = 1
    while counter < 10000:
        numbered = os.path.realpath(os.path.join(target_dir, f"{stem}_{counter}{ext}"))
        if not numbered.startswith(real_base + os.sep):
            raise ValueError("File path escapes configured storage directory")
        if not os.path.exists(numbered):
            return numbered
        counter += 1
    raise OSError("Unable to allocate unique destination filename")


# ═══════════════════════════════════════════════════════════════════════════
# ShareMe-style Auto-Filter Category Extensions (PC <-> PC & PC <-> APK)
# ═══════════════════════════════════════════════════════════════════════════

CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "IMAGES": {
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
        ".heic", ".heif", ".avif", ".svg", ".ico", ".tif", ".tiff",
    },
    "VIDEOS": {
        ".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp",
        ".m4v", ".flv", ".wmv", ".ts", ".mts", ".m2ts",
    },
    "MUSIC": {
        ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac",
        ".opus", ".wma", ".alac", ".aiff",
    },
    "APPS": {
        ".apk", ".apkm", ".apks", ".xapk", ".exe", ".msi",
        ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".msix",
    },
    "FILES": {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".csv", ".json", ".xml", ".md", ".rtf", ".odt",
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
        ".epub", ".html", ".htm", ".py", ".kt", ".java", ".js",
    },
}

_RINGTONE_NAME_HINTS = (
    "ringtone", "ringtones", "notification", "notifications",
    "alarm", "alarms", "hangouts_", "whatsapp audio",
)


def matches_category(filename_or_path: str, category: str) -> bool:
    """
    Auto-filters files for ShareMe-style categories:
    - MUSIC strictly matches music audio files and automatically filters out ringtones/system tones.
    - IMAGES / VIDEOS / APPS strictly match their respective media/package extensions.
    - FILES matches documents/archives/general files (or any non-empty filename when category is FILES/ALL).
    """
    cat = (category or "ALL").strip().upper()
    if cat in ("", "ALL", "FOLDERS", "ZIP"):
        return True

    norm = filename_or_path.replace("\\", "/").lower()
    base = os.path.basename(norm)
    _, ext = os.path.splitext(base)

    if cat == "FILES":
        # Allow explicit document/archive extensions OR any general non-executable file
        return bool(base)

    allowed = CATEGORY_EXTENSIONS.get(cat)
    if not allowed:
        return True
    if ext not in allowed:
        return False

    if cat == "MUSIC":
        if any(hint in norm for hint in _RINGTONE_NAME_HINTS):
            return False

    return True


def filter_paths_by_category(paths: Iterable[str], category: str) -> list[str]:
    return [p for p in paths if matches_category(p, category)]
