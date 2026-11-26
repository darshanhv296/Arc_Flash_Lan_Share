"""
Arc Flash High-Speed LAN Transfer Engine for PC (AF01 Binary + HTTP/1.1 Dual Server & Client).

Key Performance & Protocol Parity with Android:
- Deterministic port 53317 (with 53318..53327 fallback) serving BOTH AF01 binary & HTTP/1.1
- Single persistent TCP session reused for ALL files, batches, and bidirectional control frames
- SO_SNDBUF = 4 MB, SO_RCVBUF = 4 MB, TCP_NODELAY = 1, 1 MB chunks, 32 MB sliding window, 2 MB ACKs
- Double-buffered producer-consumer sender pipeline & sequential 1 MB buffered `.arcpart` receiver I/O
- Whole-file IEEE CRC32 verification + path-confined atomic file finalization
- Folder Tree transfer + on-the-fly Folder-to-ZIP archive builder
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import selectors
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from arc_protocol import (
    ACK_GRANULARITY_BYTES,
    ACTION_TO_CONTROL_CODE,
    CHUNK_SIZE_BYTES,
    CONTROL_ACTION_NAMES,
    CTRL_DISCONNECT,
    CTRL_PAUSE,
    CTRL_RESUME,
    CTRL_STOP,
    DATA_HEADER_SIZE,
    DEFAULT_PORT,
    DISK_BUFFER_BYTES,
    LOGICAL_STREAM_COUNT,
    MAGIC_BYTES,
    MAX_CHUNK_SIZE_BYTES,
    PORT_FALLBACK_COUNT,
    SLIDING_WINDOW_BYTES,
    SOCKET_CONNECT_TIMEOUT_S,
    SOCKET_READ_TIMEOUT_S,
    SOCKET_RECV_BUFFER_BYTES,
    SOCKET_SEND_BUFFER_BYTES,
    SOCKET_STREAM_BUFFER_BYTES,
    TYPE_ACK,
    TYPE_CONTROL,
    TYPE_DATA,
    TYPE_DONE,
    TYPE_DONE_ACK,
    TYPE_ERROR,
    TYPE_FILE_META,
    TYPE_HELLO,
    AckFrame,
    ControlFrame,
    DoneAckFrame,
    DoneFrame,
    ErrorFrame,
    FileMetaFrame,
    HelloFrame,
    combine_crc32,
    crc32_chunk,
    decode_ack,
    decode_control,
    decode_done,
    decode_done_ack,
    decode_error,
    decode_file_meta,
    decode_hello,
    encode_ack,
    encode_control,
    encode_done,
    encode_done_ack,
    encode_error,
    encode_file_meta,
    encode_hello,
    matches_category,
    pack_data_header,
    pack_frame,
    read_exact,
    read_frame_header,
    reserve_unique_confined_path,
    sanitize_relative_path,
    unpack_data_meta,
)
from discovery import DiscoveredPeer, LanDiscoveryService, QrPayloadCodec

MAX_PARALLEL_TRANSFERS = 6


# ═══════════════════════════════════════════════════════════════════════════
# Socket Tuning & Stream Wrappers
# ═══════════════════════════════════════════════════════════════════════════

def tune_socket(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER_BYTES)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER_BYTES)
    except OSError:
        pass
    sock.settimeout(SOCKET_READ_TIMEOUT_S)


class _PrefixedStream:
    """Presents prepended probe bytes followed by buffered reads from a socket."""

    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self._sock = sock
        self._buf = bytearray(initial)
        self._buf_pos = 0

    def read(self, n: int) -> bytes:
        if n <= 0:
            return b""
        if self._buf_pos < len(self._buf):
            avail = len(self._buf) - self._buf_pos
            take = min(n, avail)
            out = bytes(self._buf[self._buf_pos : self._buf_pos + take])
            self._buf_pos += take
            if self._buf_pos >= len(self._buf):
                self._buf.clear()
                self._buf_pos = 0
            return out
        # Read larger chunks into internal buffer for fewer syscalls
        want = max(n, 256 * 1024)
        data = self._sock.recv(want)
        if not data:
            return b""
        if len(data) <= n:
            return data
        self._buf.extend(data[n:])
        self._buf_pos = 0
        return data[:n]

    def readline(self, max_len: int = 16384) -> bytes | None:
        line = bytearray()
        while len(line) < max_len:
            b = self.read(1)
            if not b:
                return bytes(line) if line else None
            line.extend(b)
            if b == b"\n":
                return bytes(line)
        raise ValueError("HTTP header line too long")


def _sock_send_all(sock: socket.socket, data: bytes | bytearray | memoryview, is_closed_fn: Callable[[], bool]) -> None:
    view = memoryview(data)
    total = len(view)
    sent = 0
    while sent < total:
        if is_closed_fn():
            raise OSError("Socket closed")
        try:
            n = sock.send(view[sent:])
            if n <= 0:
                raise OSError("Socket broken during write")
            sent += n
        except socket.timeout:
            if is_closed_fn():
                raise OSError("Socket closed")
            continue


# ═══════════════════════════════════════════════════════════════════════════
# Persistent Single-Session Binary Client (Sender)
# ═══════════════════════════════════════════════════════════════════════════

class _OutboundFileState:
    def __init__(self, file_id: int, total_size: int, on_progress: Callable[[int], None]) -> None:
        self.file_id = file_id
        self.total_size = total_size
        self.on_progress = on_progress
        self.cond = threading.Condition()
        self.available_credit = SLIDING_WINDOW_BYTES
        self.last_acked_bytes = 0
        self.done_ack: DoneAckFrame | None = None
        self.error_msg: str | None = None
        self.finished = False


class _BinaryClientSession:
    def __init__(
        self,
        endpoint_key: str,
        sock: socket.socket,
        device_id_provider: Callable[[], str],
        on_remote_control: Callable[[str, str], None],
        on_session_closed: Callable[[str], None],
    ) -> None:
        self.endpoint_key = endpoint_key
        self.sock = sock
        self._did_provider = device_id_provider
        self._on_remote_control = on_remote_control
        self._on_session_closed = on_session_closed

        self._write_lock = threading.Lock()
        self._write_buf = bytearray()
        self._files_lock = threading.Lock()
        self.active_files: dict[int, _OutboundFileState] = {}
        self.closed = False
        self.clean_disconnect = False
        self.paused = False

        self._reader_thread = threading.Thread(target=self._reader_loop, name="arc-tx-reader", daemon=True)
        self._reader_thread.start()

    def is_alive(self) -> bool:
        return not self.closed and self.sock.fileno() >= 0

    def write_control_and_flush(self, frame_type: int, payload: bytes) -> None:
        frame = pack_frame(frame_type, payload)
        with self._write_lock:
            if self.closed:
                raise OSError("Session closed")
            if self._write_buf:
                self._write_buf.extend(frame)
                blob = bytes(self._write_buf)
                self._write_buf.clear()
                _sock_send_all(self.sock, blob, lambda: self.closed)
            else:
                _sock_send_all(self.sock, frame, lambda: self.closed)

    def write_data_chunk(
        self,
        file_id: int,
        stream_id: int,
        chunk_index: int,
        total_chunks: int,
        offset: int,
        chunk_bytes: bytes,
    ) -> None:
        hdr = pack_data_header(file_id, stream_id, chunk_index, total_chunks, offset, len(chunk_bytes))
        with self._write_lock:
            if self.closed:
                raise OSError("Session closed")
            if len(chunk_bytes) < SOCKET_STREAM_BUFFER_BYTES and len(self._write_buf) + len(hdr) + len(chunk_bytes) < SOCKET_STREAM_BUFFER_BYTES:
                self._write_buf.extend(hdr)
                self._write_buf.extend(chunk_bytes)
                return
            if self._write_buf:
                self._write_buf.extend(hdr)
                self._write_buf.extend(chunk_bytes)
                blob = bytes(self._write_buf)
                self._write_buf.clear()
                _sock_send_all(self.sock, blob, lambda: self.closed)
            else:
                _sock_send_all(self.sock, hdr + chunk_bytes, lambda: self.closed)

    def flush_buffer(self) -> None:
        with self._write_lock:
            if self._write_buf:
                blob = bytes(self._write_buf)
                self._write_buf.clear()
                _sock_send_all(self.sock, blob, lambda: self.closed)

    def _fail_all_pending(self, reason: str) -> None:
        with self._files_lock:
            states = list(self.active_files.values())
        for st in states:
            with st.cond:
                if not st.finished:
                    st.error_msg = reason
                    st.finished = True
                    st.cond.notify_all()

    def close(self, reason: str = "Closed", clean: bool = False) -> None:
        if self.closed:
            return
        self.clean_disconnect = self.clean_disconnect or clean
        self.closed = True
        self._fail_all_pending(reason)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if threading.current_thread() is not self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.5)
        try:
            self.sock.close()
        except OSError:
            pass

    def _reader_loop(self) -> None:
        stream = _PrefixedStream(self.sock)
        try:
            while not self.closed:
                try:
                    header = read_frame_header(stream)
                except socket.timeout:
                    continue
                if header is None:
                    break
                frame_type, payload_len = header
                payload = b""
                if payload_len > 0:
                    while True:
                        try:
                            payload = read_exact(stream, payload_len) or b""
                            break
                        except socket.timeout:
                            if self.closed:
                                raise EOFError("Closed")
                    if len(payload) != payload_len:
                        raise EOFError("Truncated frame payload")

                if frame_type == TYPE_ACK:
                    ack = decode_ack(payload)
                    with self._files_lock:
                        st = self.active_files.get(ack.file_id)
                    if st is not None:
                        with st.cond:
                            if ack.credit_bytes_granted > 0:
                                st.available_credit += ack.credit_bytes_granted
                                st.cond.notify_all()
                        if ack.acked_bytes_total > st.last_acked_bytes:
                            st.last_acked_bytes = ack.acked_bytes_total
                            try:
                                st.on_progress(min(ack.acked_bytes_total, st.total_size))
                            except Exception:
                                pass

                elif frame_type == TYPE_DONE_ACK:
                    done_ack = decode_done_ack(payload)
                    with self._files_lock:
                        st = self.active_files.get(done_ack.file_id)
                    if st is not None:
                        with st.cond:
                            st.done_ack = done_ack
                            st.finished = True
                            st.cond.notify_all()

                elif frame_type == TYPE_ERROR:
                    err = decode_error(payload)
                    with self._files_lock:
                        st = self.active_files.get(err.file_id)
                    if st is not None:
                        with st.cond:
                            st.error_msg = err.message
                            st.finished = True
                            st.cond.notify_all()

                elif frame_type == TYPE_CONTROL:
                    ctrl = decode_control(payload)
                    action = CONTROL_ACTION_NAMES.get(ctrl.action_code, "")
                    if ctrl.action_code == CTRL_PAUSE:
                        self.paused = True
                    elif ctrl.action_code == CTRL_RESUME:
                        self.paused = False
                    elif ctrl.action_code == CTRL_STOP:
                        self.paused = False
                        self._fail_all_pending("Stopped by remote peer")
                    elif ctrl.action_code == CTRL_DISCONNECT:
                        self.clean_disconnect = True
                    if action:
                        try:
                            self._on_remote_control(action, ctrl.peer_id)
                        except Exception:
                            pass
                    if ctrl.action_code == CTRL_DISCONNECT:
                        break
        except (OSError, EOFError, ValueError):
            pass
        finally:
            was_clean = self.clean_disconnect
            self.close("Session closed", clean=was_clean)
            if not was_clean:
                try:
                    self._on_session_closed("Connection closed")
                except Exception:
                    pass


class ArcBinaryClient:
    """Single-session persistent AF01 sender client."""

    def __init__(
        self,
        local_identity_provider: Callable[[], tuple[str, str, int]],
        is_paused_provider: Callable[[], bool],
        on_remote_control: Callable[[str, str], None],
        on_session_closed: Callable[[str], None],
    ) -> None:
        self._identity_provider = local_identity_provider
        self._is_paused_provider = is_paused_provider
        self._on_remote_control = on_remote_control
        self._on_session_closed = on_session_closed

        self._session_lock = threading.Lock()
        self._session: _BinaryClientSession | None = None
        self._next_file_id = 1
        self._file_id_lock = threading.Lock()
        self.connect_count = 0

    def is_session_alive(self) -> bool:
        s = self._session
        return s is not None and s.is_alive()

    def ensure_session(self, host: str, port: int) -> _BinaryClientSession:
        key = f"{host}:{port}"
        existing = self._session
        if existing is not None and existing.endpoint_key == key and existing.is_alive():
            return existing

        with self._session_lock:
            recheck = self._session
            if recheck is not None and recheck.endpoint_key == key and recheck.is_alive():
                return recheck
            if recheck is not None:
                recheck.close("Switching peer session", clean=True)
                self._session = None

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tune_socket(sock)
            sock.settimeout(SOCKET_CONNECT_TIMEOUT_S)
            try:
                sock.connect((host, port))
            except Exception:
                sock.close()
                raise
            sock.settimeout(SOCKET_READ_TIMEOUT_S)
            self.connect_count += 1

            did, name, listen_port = self._identity_provider()
            hello_payload = encode_hello(
                HelloFrame(
                    device_id=did,
                    device_name=name,
                    listen_port=listen_port,
                    stream_count=LOGICAL_STREAM_COUNT,
                    chunk_size=CHUNK_SIZE_BYTES,
                )
            )
            _sock_send_all(
                sock,
                MAGIC_BYTES + pack_frame(TYPE_HELLO, hello_payload),
                lambda: False,
            )

            session = _BinaryClientSession(
                endpoint_key=key,
                sock=sock,
                device_id_provider=lambda: self._identity_provider()[0],
                on_remote_control=self._on_remote_control,
                on_session_closed=self._on_session_closed,
            )
            self._session = session
            return session

    def send_pair_handshake(self, host: str, port: int) -> bool:
        try:
            session = self.ensure_session(host, port)
            did, name, listen_port = self._identity_provider()
            hello_payload = encode_hello(
                HelloFrame(
                    device_id=did,
                    device_name=name,
                    listen_port=listen_port,
                    stream_count=LOGICAL_STREAM_COUNT,
                    chunk_size=CHUNK_SIZE_BYTES,
                )
            )
            session.write_control_and_flush(TYPE_HELLO, hello_payload)
            return True
        except Exception:
            return False

    def send_control(self, host: str, port: int, action: str) -> bool:
        code = ACTION_TO_CONTROL_CODE.get(action.upper(), CTRL_DISCONNECT)
        try:
            session = self.ensure_session(host, port)
            did = self._identity_provider()[0]
            payload = encode_control(ControlFrame(action_code=code, peer_id=did))
            session.write_control_and_flush(TYPE_CONTROL, payload)
            return True
        except Exception:
            return False

    def send_disconnect_notice(self) -> None:
        s = self._session
        if s is not None and s.is_alive():
            try:
                did = self._identity_provider()[0]
                payload = encode_control(ControlFrame(action_code=CTRL_DISCONNECT, peer_id=did))
                s.clean_disconnect = True
                s.write_control_and_flush(TYPE_CONTROL, payload)
                time.sleep(0.1)
            except Exception:
                pass
        self.close_all(clean=True)

    def close_all(self, clean: bool = True) -> None:
        with self._session_lock:
            s = self._session
            self._session = None
        if s is not None:
            s.close("Session closed by caller", clean=clean)

    def send_file(
        self,
        host: str,
        port: int,
        file_path: str,
        relative_name: str,
        batch_id: str,
        transfer_id: str,
        on_progress: Callable[[int], None],
        is_cancelled: Callable[[], bool],
        chunk_size: int = CHUNK_SIZE_BYTES,
    ) -> None:
        chunk_size = max(64 * 1024, min(chunk_size, MAX_CHUNK_SIZE_BYTES))
        total_size = os.path.getsize(file_path)
        total_chunks = 0 if total_size == 0 else (total_size + chunk_size - 1) // chunk_size

        session = self.ensure_session(host, port)
        with self._file_id_lock:
            file_id = self._next_file_id
            self._next_file_id += 1

        outbound = _OutboundFileState(file_id=file_id, total_size=total_size, on_progress=on_progress)
        with session._files_lock:
            session.active_files[file_id] = outbound

        try:
            meta_payload = encode_file_meta(
                FileMetaFrame(
                    file_id=file_id,
                    total_size=total_size,
                    chunk_size=chunk_size,
                    total_chunks=total_chunks,
                    relative_name=relative_name,
                    batch_id=batch_id,
                    transfer_id=transfer_id,
                )
            )
            session.write_control_and_flush(TYPE_FILE_META, meta_payload)

            file_crc = 0
            if total_size > 0:
                file_crc = self._stream_double_buffered(
                    session=session,
                    outbound=outbound,
                    file_path=file_path,
                    file_id=file_id,
                    total_chunks=total_chunks,
                    chunk_size=chunk_size,
                    is_cancelled=is_cancelled,
                )

            done_payload = encode_done(
                DoneFrame(
                    file_id=file_id,
                    total_bytes=total_size,
                    crc32=file_crc,
                    total_chunks=total_chunks,
                )
            )
            session.write_control_and_flush(TYPE_DONE, done_payload)

            with outbound.cond:
                while not outbound.finished and session.is_alive():
                    if is_cancelled():
                        raise InterruptedError("Transfer cancelled")
                    outbound.cond.wait(timeout=0.25)

            if outbound.error_msg:
                raise OSError(outbound.error_msg)
            done_ack = outbound.done_ack
            if done_ack is None:
                raise OSError("Session closed before DONE_ACK")
            if not done_ack.crc32_valid:
                raise OSError(f"Checksum mismatch on receiver for {relative_name}")
            if done_ack.bytes_saved != total_size:
                raise OSError(f"Receiver byte mismatch ({done_ack.bytes_saved} != {total_size})")
            on_progress(total_size)
        finally:
            with session._files_lock:
                session.active_files.pop(file_id, None)

    def _stream_double_buffered(
        self,
        session: _BinaryClientSession,
        outbound: _OutboundFileState,
        file_path: str,
        file_id: int,
        total_chunks: int,
        chunk_size: int,
        is_cancelled: Callable[[], bool],
    ) -> int:
        chunk_q: queue.Queue[Any] = queue.Queue(maxsize=2)
        sentinel = object()
        producer_error: list[BaseException] = []
        crcs: list[int] = [0] * total_chunks
        lens: list[int] = [0] * total_chunks

        def producer() -> None:
            try:
                with open(file_path, "rb", buffering=DISK_BUFFER_BYTES) as fp:
                    offset = 0
                    for idx in range(total_chunks):
                        if is_cancelled() or session.closed:
                            break
                        data = fp.read(chunk_size)
                        if not data:
                            raise EOFError(f"Unexpected EOF reading {file_path}")
                        crc = crc32_chunk(data)
                        crcs[idx] = crc
                        lens[idx] = len(data)
                        while True:
                            if is_cancelled() or session.closed:
                                return
                            try:
                                chunk_q.put((idx, offset, data), timeout=0.2)
                                break
                            except queue.Full:
                                continue
                        offset += len(data)
            except BaseException as exc:
                producer_error.append(exc)
            finally:
                while True:
                    try:
                        chunk_q.put(sentinel, timeout=0.2)
                        break
                    except queue.Full:
                        if is_cancelled() or session.closed:
                            break

        prod_thread = threading.Thread(target=producer, name=f"arc-disk-rd-{file_id}", daemon=True)
        prod_thread.start()

        while True:
            if is_cancelled():
                prod_thread.join(timeout=1.0)
                raise InterruptedError("Transfer cancelled")
            try:
                item = chunk_q.get(timeout=0.2)
            except queue.Empty:
                if producer_error:
                    raise producer_error[0]
                if not session.is_alive():
                    raise OSError("Session closed during transfer")
                continue

            if item is sentinel:
                break
            idx, offset, data = item
            needed = len(data)

            # Pause gate & credit sliding window
            while True:
                if is_cancelled():
                    raise InterruptedError("Transfer cancelled")
                if self._is_paused_provider() or session.paused:
                    time.sleep(0.05)
                    continue
                with outbound.cond:
                    if outbound.available_credit >= needed:
                        outbound.available_credit -= needed
                        break
                    if outbound.finished or not session.is_alive():
                        raise OSError(outbound.error_msg or "Session interrupted while awaiting credit")
                    outbound.cond.wait(timeout=0.2)

            session.write_data_chunk(
                file_id=file_id,
                stream_id=0,
                chunk_index=idx,
                total_chunks=total_chunks,
                offset=offset,
                chunk_bytes=data,
            )

        prod_thread.join(timeout=2.0)
        if producer_error:
            raise producer_error[0]

        combined = 0
        for i in range(total_chunks):
            combined = crcs[0] if i == 0 else combine_crc32(combined, crcs[i], lens[i])
        return combined


# ═══════════════════════════════════════════════════════════════════════════
# Dual-Mode LAN Server (AF01 Binary Fast Path + HTTP/1.1 Peer & Web UI)
# ═══════════════════════════════════════════════════════════════════════════

class _BinaryIncomingFile:
    def __init__(
        self,
        file_id: int,
        transfer_id: str,
        relative_name: str,
        total_size: int,
        total_chunks: int,
        temp_path: str,
    ) -> None:
        self.file_id = file_id
        self.transfer_id = transfer_id
        self.relative_name = relative_name
        self.total_size = total_size
        self.total_chunks = total_chunks
        self.temp_path = temp_path
        self._fp = open(temp_path, "wb", buffering=DISK_BUFFER_BYTES)

        self.chunk_crcs: list[int] = [0] * total_chunks
        self.chunk_lens: list[int] = [0] * total_chunks
        self.next_chunk_idx = 0
        self.pending_reorder: dict[int, bytes] = {}
        self.chunks_written = 0
        self.bytes_written = 0
        self.unacked_written = 0
        self.last_ui_reported = 0
        self.last_ui_time = 0.0
        self.all_done_event = threading.Event()
        self.write_error: str | None = None
        self.closed = False
        self.lock = threading.Lock()
        if total_chunks == 0:
            self.all_done_event.set()

    def write_chunk_ordered(self, chunk_index: int, data: bytes) -> list[tuple[int, bytes]]:
        """Writes sequentially in order, buffering out-of-order chunks if needed. Returns persisted [(idx, data)]."""
        persisted: list[tuple[int, bytes]] = []
        if self.closed:
            return persisted
        if chunk_index == self.next_chunk_idx:
            self._fp.write(data)
            persisted.append((chunk_index, data))
            self.next_chunk_idx += 1
            while self.next_chunk_idx in self.pending_reorder:
                queued = self.pending_reorder.pop(self.next_chunk_idx)
                self._fp.write(queued)
                persisted.append((self.next_chunk_idx, queued))
                self.next_chunk_idx += 1
        elif chunk_index > self.next_chunk_idx and chunk_index not in self.pending_reorder:
            self.pending_reorder[chunk_index] = data
        return persisted

    def compute_combined_crc32(self) -> int:
        if self.total_chunks <= 0:
            return 0
        combined = self.chunk_crcs[0]
        for i in range(1, self.total_chunks):
            combined = combine_crc32(combined, self.chunk_crcs[i], self.chunk_lens[i])
        return combined

    def close_and_flush(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.pending_reorder.clear()
            try:
                self._fp.flush()
            except OSError:
                pass
            try:
                self._fp.close()
            except OSError:
                pass

    def abort_and_delete(self) -> None:
        with self.lock:
            already = self.closed
            self.closed = True
            self.pending_reorder.clear()
            self.all_done_event.set()
            if not already:
                try:
                    self._fp.close()
                except OSError:
                    pass
            try:
                if os.path.exists(self.temp_path):
                    os.remove(self.temp_path)
            except OSError:
                pass


class _IncomingBinarySessionHandle:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.write_lock = threading.Lock()
        self.active_files: dict[int, _BinaryIncomingFile] = {}
        self.closed = False

    def send_frame(self, frame_type: int, payload: bytes) -> None:
        frame = pack_frame(frame_type, payload)
        with self.write_lock:
            if self.closed:
                return
            _sock_send_all(self.sock, frame, lambda: self.closed)


# ═══════════════════════════════════════════════════════════════════════════
# Transfer Manager & State Model
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TransferItemState:
    id: str
    name: str
    direction: str  # "SEND" | "RECEIVE"
    size_bytes: int
    bytes_transferred: int = 0
    status: str = "WAITING"  # WAITING, TRANSFERRING, PAUSED, COMPLETED, FAILED, CANCELLED
    error_message: str | None = None
    saved_path: str | None = None
    local_source_path: str | None = None
    batch_id: str = ""
    category: str = "FILES"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        pct = 100.0 if self.status == "COMPLETED" else (
            min(100.0, (self.bytes_transferred * 100.0 / self.size_bytes)) if self.size_bytes > 0 else 0.0
        )
        return {
            "id": self.id,
            "name": self.name,
            "direction": self.direction,
            "sizeBytes": self.size_bytes,
            "bytesTransferred": self.bytes_transferred,
            "progress": round(pct, 1),
            "status": self.status,
            "errorMessage": self.error_message,
            "savedPath": self.saved_path,
            "batchId": self.batch_id,
            "category": self.category,
        }


class _DirectionMetrics:
    """Batch-scoped Speed & Elapsed Timer matching Android TransferViewModel.kt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.speed_bps: float = 0.0
        self.elapsed_ms: int = 0
        self._batch_started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_accum_s: float = 0.0
        self._last_sample_t: float | None = None
        self._window_bytes: int = 0
        self._is_running = False

    def reset_for_new_batch(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._batch_started_at = now
            self._paused_at = None
            self._paused_accum_s = 0.0
            self._last_sample_t = now
            self._window_bytes = 0
            self.speed_bps = 0.0
            self.elapsed_ms = 0
            self._is_running = True

    def on_bytes(self, delta: int) -> None:
        if delta <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._batch_started_at is None:
                self._batch_started_at = now
                self._last_sample_t = now
                self._is_running = True
            self._window_bytes += delta
            if self._last_sample_t is not None:
                dt = now - self._last_sample_t
                if dt >= 0.45:
                    inst = self._window_bytes / dt
                    self.speed_bps = inst if self.speed_bps <= 0 else (0.35 * inst + 0.65 * self.speed_bps)
                    self._window_bytes = 0
                    self._last_sample_t = now

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            now = time.monotonic()
            if paused:
                if self._is_running and self._paused_at is None:
                    self._paused_at = now
                    self.elapsed_ms = self._compute_elapsed_ms_locked(now)
                    self.speed_bps = 0.0
                    self._window_bytes = 0
            else:
                if self._paused_at is not None:
                    self._paused_accum_s += now - self._paused_at
                    self._paused_at = None
                    self._last_sample_t = now

    def freeze_completed(self, total_bytes: int) -> None:
        with self._lock:
            now = time.monotonic()
            if self._batch_started_at is not None:
                self.elapsed_ms = self._compute_elapsed_ms_locked(now)
                sec = max(self.elapsed_ms / 1000.0, 0.001)
                if total_bytes > 0:
                    self.speed_bps = total_bytes / sec
            self._is_running = False
            self._paused_at = None
            self._window_bytes = 0

    def reset_all(self) -> None:
        with self._lock:
            self.speed_bps = 0.0
            self.elapsed_ms = 0
            self._batch_started_at = None
            self._paused_at = None
            self._paused_accum_s = 0.0
            self._last_sample_t = None
            self._window_bytes = 0
            self._is_running = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._is_running and self._paused_at is None and self._batch_started_at is not None:
                self.elapsed_ms = self._compute_elapsed_ms_locked(time.monotonic())
            return {
                "speedBytesPerSec": round(self.speed_bps, 1),
                "elapsedMs": int(self.elapsed_ms),
            }

    def _compute_elapsed_ms_locked(self, now: float) -> int:
        if self._batch_started_at is None:
            return int(self.elapsed_ms)
        ref = self._paused_at if self._paused_at is not None else now
        dur = max(0.0, ref - self._batch_started_at - self._paused_accum_s)
        return int(dur * 1000)


class ArcTransferManager:
    """Coordinates pairing, discovery, binary client/server, queues, categories, and ZIP folders."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        save_dir: str,
        theme: str = "system",
        preferred_port: int = DEFAULT_PORT,
        on_config_save: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.save_dir = os.path.abspath(os.path.expanduser(save_dir))
        self.theme = theme if theme in ("system", "light", "dark") else "system"
        self.preferred_port = preferred_port
        self._on_config_save = on_config_save

        os.makedirs(self.save_dir, exist_ok=True)
        self._staging_root = os.path.join(tempfile.gettempdir(), f"arcflash_pc_{self.device_id[:8]}")
        self._rx_staging_dir = os.path.join(self._staging_root, "rx")
        self._tx_staging_dir = os.path.join(self._staging_root, "tx")
        shutil.rmtree(self._staging_root, ignore_errors=True)
        os.makedirs(self._rx_staging_dir, exist_ok=True)
        os.makedirs(self._tx_staging_dir, exist_ok=True)

        self._state_lock = threading.RLock()
        self.paired = False
        self.paired_peer_id: str | None = None
        self.paired_peer_name: str | None = None
        self.paired_peer_host: str | None = None
        self.paired_peer_port: int = 0

        self.batch_paused = False
        self._stopped_flag = False
        self._cancelled_ids: set[str] = set()

        self._send_items: list[TransferItemState] = []
        self._recv_items: list[TransferItemState] = []
        self._send_metrics = _DirectionMetrics()
        self._recv_metrics = _DirectionMetrics()

        self._tx_queue: queue.Queue[str] = queue.Queue()
        self._running = False
        self._server_sock: socket.socket | None = None
        self.listen_port = 0
        self._active_rx_sessions: set[_IncomingBinarySessionHandle] = set()
        self._active_rx_lock = threading.Lock()
        self._zip_batches: dict[str, dict[str, Any]] = {}

        qr_payload, qr_exp = QrPayloadCodec.create_payload(self.device_id)
        self.qr_payload = qr_payload
        self.qr_expires_at_ms = qr_exp

        self.client = ArcBinaryClient(
            local_identity_provider=lambda: (self.device_id, self.device_name, self.listen_port),
            is_paused_provider=lambda: self.batch_paused,
            on_remote_control=self._handle_remote_control,
            on_session_closed=self._handle_client_session_closed,
        )

        self.discovery = LanDiscoveryService(
            device_id_provider=lambda: self.device_id,
            device_name_provider=lambda: self.device_name,
            listen_port_provider=lambda: self.listen_port,
        )

        self._tx_workers: list[threading.Thread] = []
        self._accept_thread: threading.Thread | None = None
        self._accept_wake_r: socket.socket | None = None
        self._accept_wake_w: socket.socket | None = None

    def start(self) -> int:
        if self._running:
            return self.listen_port
        self._running = True
        sock = self._bind_server_socket(self.preferred_port)
        self._server_sock = sock
        self.listen_port = sock.getsockname()[1]
        wake_r, wake_w = socket.socketpair()
        wake_r.setblocking(False)
        wake_w.setblocking(False)
        self._accept_wake_r = wake_r
        self._accept_wake_w = wake_w

        self._accept_thread = threading.Thread(target=self._accept_loop, name="arc-srv-accept", daemon=True)
        self._accept_thread.start()
        self._tx_workers = [
            threading.Thread(target=self._tx_worker_loop, name=f"arc-tx-worker-{index + 1}", daemon=True)
            for index in range(MAX_PARALLEL_TRANSFERS)
        ]
        for worker in self._tx_workers:
            worker.start()
        self.discovery.start()
        return self.listen_port

    def stop(self) -> None:
        self._running = False
        self.client.send_disconnect_notice()
        self.discovery.stop()
        wake_w = self._accept_wake_w
        if wake_w is not None:
            try:
                wake_w.send(b"\x01")
            except OSError:
                pass
        srv = self._server_sock
        if srv is not None:
            try:
                srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        accept_t = self._accept_thread
        if accept_t is not None and accept_t.is_alive():
            accept_t.join(timeout=2.0)
        for tx_worker in self._tx_workers:
            if tx_worker.is_alive():
                tx_worker.join(timeout=2.0)
        with self._active_rx_lock:
            sessions = list(self._active_rx_sessions)
        for s in sessions:
            s.closed = True
            for f in list(s.active_files.values()):
                f.abort_and_delete()
            try:
                s.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.sock.close()
            except OSError:
                pass
        self._server_sock = None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        for w in (self._accept_wake_r, self._accept_wake_w):
            if w is not None:
                try:
                    w.close()
                except OSError:
                    pass
        self._accept_wake_r = None
        self._accept_wake_w = None
        shutil.rmtree(self._staging_root, ignore_errors=True)

    def _bind_server_socket(self, preferred: int) -> socket.socket:
        candidates = list(range(preferred, preferred + PORT_FALLBACK_COUNT + 1)) + [0] if preferred > 0 else [0]
        last_err: Exception | None = None
        for port in candidates:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER_BYTES)
                except OSError:
                    pass
                s.bind(("0.0.0.0", port))
                s.listen(64)
                return s
            except OSError as exc:
                last_err = exc
                s.close()
        raise OSError(f"Could not bind ArcLanServer on {preferred}: {last_err}")

    # ── Public / UI State Snapshot (NO raw IP or port exposed!) ───────────

    def get_ui_state(self) -> dict[str, Any]:
        # Auto-regenerate expired QR if needed
        now_ms = int(time.time() * 1000)
        if now_ms >= self.qr_expires_at_ms:
            self.regenerate_qr()

        lan = self.discovery.get_lan_status()
        peers = self.discovery.get_peers()
        with self._state_lock:
            send_dicts = [x.to_dict() for x in self._send_items]
            recv_dicts = [x.to_dict() for x in self._recv_items]
            can_ctrl = any(
                x.status in ("WAITING", "TRANSFERRING", "PAUSED")
                for x in (self._send_items + self._recv_items)
            )
            return {
                "deviceId": self.device_id,
                "deviceName": self.device_name,
                "saveDir": self.save_dir,
                "theme": self.theme,
                "paired": self.paired,
                "pairedPeerName": self.paired_peer_name,
                "batchPaused": self.batch_paused,
                "canControlTransfers": can_ctrl,
                "lanStatus": {
                    "isLanConnected": lan.is_lan_connected,
                    "transportLabel": lan.transport_label,
                },
                "availablePeers": [
                    {
                        "deviceId": p.device_id,
                        "displayName": p.display_name,
                    }
                    for p in peers
                ],
                "qrPayload": self.qr_payload,
                "qrExpiresAtMs": self.qr_expires_at_ms,
                "tuningSummary": "AF01 · 6 parallel · 4 MB buf · 32 MB win",
                "sendItems": send_dicts,
                "receiveItems": recv_dicts,
                "sendSummary": self._build_tab_summary(self._send_items, self._send_metrics),
                "receiveSummary": self._build_tab_summary(self._recv_items, self._recv_metrics),
            }

    @staticmethod
    def _build_tab_summary(items: list[TransferItemState], metrics: _DirectionMetrics) -> dict[str, Any]:
        total_count = len(items)
        active_count = sum(1 for x in items if x.status in ("WAITING", "TRANSFERRING", "PAUSED"))
        done_count = sum(1 for x in items if x.status == "COMPLETED")
        failed_count = sum(1 for x in items if x.status == "FAILED")
        total_bytes = sum(max(0, x.size_bytes) for x in items if x.status != "CANCELLED")
        done_bytes = sum(
            x.size_bytes if x.status == "COMPLETED" else min(x.bytes_transferred, x.size_bytes)
            for x in items
            if x.status != "CANCELLED"
        )
        progress = min(1.0, done_bytes / total_bytes) if total_bytes > 0 else 0.0
        snap = metrics.snapshot()
        return {
            "totalCount": total_count,
            "activeCount": active_count,
            "doneCount": done_count,
            "failedCount": failed_count,
            "transferredBytes": done_bytes,
            "totalBytes": total_bytes,
            "overallProgress": round(progress, 4),
            "speedBytesPerSec": snap["speedBytesPerSec"],
            "elapsedMs": snap["elapsedMs"],
        }

    # ── Pairing & Control Actions ─────────────────────────────────────────

    def regenerate_qr(self) -> tuple[str, int]:
        payload, exp = QrPayloadCodec.create_payload(self.device_id)
        with self._state_lock:
            self.qr_payload = payload
            self.qr_expires_at_ms = exp
        return payload, exp

    def connect_to_peer_id(self, peer_device_id: str) -> tuple[bool, str]:
        peer = self.discovery.find_peer(peer_device_id)
        if peer is None or not peer.host_address or not peer.port:
            return False, "Device not reachable on LAN — tap Refresh"
        return self.connect_to_endpoint(peer.device_id, peer.display_name, peer.host_address, peer.port)

    def connect_to_endpoint(self, peer_id: str, peer_name: str, host: str, port: int) -> tuple[bool, str]:
        ok = self.client.send_pair_handshake(host, port)
        if not ok:
            return False, f"Could not reach {peer_name}"
        self._on_peer_paired(peer_id, peer_name, host, port)
        return True, f"Connected to {peer_name}"

    def disconnect_peer(self) -> None:
        host, port = self.paired_peer_host, self.paired_peer_port
        self.stop_all_transfers(notify_peer=False)
        if host and port > 0:
            self.client.send_disconnect_notice()
        with self._state_lock:
            self.paired = False
            self.paired_peer_id = None
            self.paired_peer_name = None
            self.paired_peer_host = None
            self.paired_peer_port = 0

    def _on_peer_paired(self, peer_id: str, peer_name: str, host: str, port: int) -> None:
        if not peer_id or not host or port <= 0:
            return
        with self._state_lock:
            self.paired = True
            self.paired_peer_id = peer_id
            self.paired_peer_name = peer_name or f"Device {peer_id[:8]}"
            self.paired_peer_host = host
            self.paired_peer_port = port
        self.discovery.register_manual_or_incoming_peer(peer_id, peer_name, host, port)

    def _handle_client_session_closed(self, reason: str) -> None:
        with self._state_lock:
            self.paired = False
            self.paired_peer_id = None
            self.paired_peer_name = None
            self.paired_peer_host = None
            self.paired_peer_port = 0

    def _handle_remote_control(self, action: str, peer_id: str) -> None:
        act = action.upper()
        if act == "PAUSE":
            self._apply_pause_state(True)
        elif act == "RESUME":
            self._apply_pause_state(False)
        elif act == "STOP":
            self.stop_all_transfers(notify_peer=False)
        elif act == "DISCONNECT":
            self.stop_all_transfers(notify_peer=False)
            with self._state_lock:
                self.paired = False
                self.paired_peer_id = None
                self.paired_peer_name = None
                self.paired_peer_host = None
                self.paired_peer_port = 0

    def _apply_pause_state(self, paused: bool) -> None:
        with self._state_lock:
            self.batch_paused = paused
            for item in self._send_items + self._recv_items:
                if paused and item.status in ("WAITING", "TRANSFERRING"):
                    item.status = "PAUSED"
                elif not paused and item.status == "PAUSED":
                    item.status = "TRANSFERRING" if item.bytes_transferred > 0 else "WAITING"
        self._send_metrics.set_paused(paused)
        self._recv_metrics.set_paused(paused)

    def toggle_pause_resume(self) -> bool:
        new_paused = not self.batch_paused
        self._apply_pause_state(new_paused)
        action = "PAUSE" if new_paused else "RESUME"
        self._push_control_to_peer(action)
        return new_paused

    def stop_all_transfers(self, notify_peer: bool = True) -> None:
        if notify_peer:
            self._push_control_to_peer("STOP")
        with self._state_lock:
            self._stopped_flag = True
            self.batch_paused = False
            for item in self._send_items + self._recv_items:
                if item.status in ("WAITING", "TRANSFERRING", "PAUSED"):
                    item.status = "CANCELLED"
                    self._cancelled_ids.add(item.id)
        with self._active_rx_lock:
            sessions = list(self._active_rx_sessions)
        for s in sessions:
            for fid, f in list(s.active_files.items()):
                s.active_files.pop(fid, None)
                f.abort_and_delete()
        self._send_metrics.set_paused(False)
        self._recv_metrics.set_paused(False)

    def _push_control_to_peer(self, action: str) -> None:
        with self._state_lock:
            host = self.paired_peer_host
            port = self.paired_peer_port
        if host and port > 0 and self.client.is_session_alive():
            if self.client.send_control(host, port, action):
                return
        with self._active_rx_lock:
            sessions = list(self._active_rx_sessions)
        code = ACTION_TO_CONTROL_CODE.get(action.upper(), CTRL_DISCONNECT)
        payload = encode_control(ControlFrame(action_code=code, peer_id=self.device_id))
        sent = False
        for s in sessions:
            if not s.closed:
                try:
                    s.send_frame(TYPE_CONTROL, payload)
                    sent = True
                except OSError:
                    pass
        if not sent and host and port > 0:
            self.client.send_control(host, port, action)

    def clear_transfers(self, tab: str) -> None:
        key = (tab or "send").lower()
        with self._state_lock:
            if key == "receive":
                self._recv_items.clear()
                self._recv_metrics.reset_all()
            else:
                # Delete staged outbound files for cleared items
                for it in self._send_items:
                    if it.local_source_path and it.local_source_path.startswith(self._tx_staging_dir):
                        try:
                            os.remove(it.local_source_path)
                        except OSError:
                            pass
                self._send_items.clear()
                self._send_metrics.reset_all()

    def cancel_item(self, transfer_id: str) -> None:
        with self._state_lock:
            self._cancelled_ids.add(transfer_id)
            for it in self._send_items + self._recv_items:
                if it.id == transfer_id and it.status in ("WAITING", "TRANSFERRING", "PAUSED"):
                    it.status = "CANCELLED"
        with self._active_rx_lock:
            for s in list(self._active_rx_sessions):
                for fid, f in list(s.active_files.items()):
                    if f.transfer_id == transfer_id:
                        s.active_files.pop(fid, None)
                        f.abort_and_delete()

    def retry_item(self, transfer_id: str) -> tuple[bool, str]:
        with self._state_lock:
            if not self.paired or not self.paired_peer_host or self.paired_peer_port <= 0:
                return False, "Connect to a device first"
            target = next((x for x in self._send_items if x.id == transfer_id), None)
            if target is None or not target.local_source_path or not os.path.exists(target.local_source_path):
                return False, "Source file unavailable to retry"
            self._cancelled_ids.discard(transfer_id)
            target.status = "WAITING"
            target.bytes_transferred = 0
            target.error_message = None
            self._stopped_flag = False
            self._tx_queue.put(target.id)
            return True, "Queued for retry"

    def update_settings(self, save_dir: str | None = None, theme: str | None = None, device_name: str | None = None) -> tuple[bool, str]:
        if save_dir is not None and save_dir.strip():
            resolved = os.path.abspath(os.path.expanduser(save_dir.strip()))
            try:
                os.makedirs(resolved, exist_ok=True)
                self.save_dir = resolved
            except OSError as exc:
                return False, f"Invalid storage folder: {exc}"
        if theme in ("system", "light", "dark"):
            self.theme = theme
        if device_name is not None and device_name.strip():
            self.device_name = device_name.strip()[:40]
        if self._on_config_save:
            self._on_config_save({
                "device_id": self.device_id,
                "device_name": self.device_name,
                "save_dir": self.save_dir,
                "theme": self.theme,
            })
        return True, "Settings updated"

    # ── Outbound Enqueueing (Category Filtering + Folders + ZIP) ──────────

    def enqueue_local_file(
        self,
        source_path: str,
        relative_name: str,
        batch_id: str,
        category: str = "FILES",
    ) -> TransferItemState | None:
        if not matches_category(relative_name, category):
            return None
        size = os.path.getsize(source_path)
        item_id = f"tx_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        item = TransferItemState(
            id=item_id,
            name=relative_name.replace("\\", "/").lstrip("/"),
            direction="SEND",
            size_bytes=size,
            batch_id=batch_id,
            category=category,
            local_source_path=source_path,
        )
        with self._state_lock:
            no_active = not any(x.status in ("WAITING", "TRANSFERRING", "PAUSED") for x in self._send_items)
            if no_active:
                self._send_metrics.reset_for_new_batch()
            self._stopped_flag = False
            self._send_items.append(item)
        self._tx_queue.put(item.id)
        return item

    def enqueue_local_folder_as_zip(
        self,
        folder_path: str,
        zip_name: str | None = None,
        batch_id: str | None = None,
    ) -> TransferItemState:
        folder = Path(folder_path).resolve()
        if not folder.is_dir():
            raise ValueError(f"Not a directory: {folder_path}")
        bid = batch_id or f"zip_{int(time.time() * 1000)}"
        out_name = zip_name or f"{folder.name}.zip"
        if not out_name.lower().endswith(".zip"):
            out_name += ".zip"
        zip_stage = os.path.join(self._tx_staging_dir, f"{uuid.uuid4().hex}_{os.path.basename(out_name)}")
        with zipfile.ZipFile(zip_stage, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for root, _, files in os.walk(folder):
                for fn in sorted(files):
                    full = Path(root) / fn
                    rel = os.path.join(folder.name, str(full.relative_to(folder))).replace("\\", "/")
                    zf.write(full, arcname=rel)
        item = self.enqueue_local_file(zip_stage, os.path.basename(out_name), bid, category="FOLDERS")
        assert item is not None
        return item

    def zip_batch_stage_append(self, zip_batch_id: str, rel_name: str, staged_file_path: str) -> None:
        batch_dir = os.path.join(self._tx_staging_dir, f"zipbatch_{sanitize_relative_path(zip_batch_id)[1]}")
        os.makedirs(batch_dir, exist_ok=True)
        sub_dirs, fname = sanitize_relative_path(rel_name)
        target_dir = os.path.join(batch_dir, *sub_dirs) if sub_dirs else batch_dir
        os.makedirs(target_dir, exist_ok=True)
        dst = os.path.join(target_dir, fname)
        shutil.move(staged_file_path, dst)
        with self._state_lock:
            entry = self._zip_batches.setdefault(zip_batch_id, {"dir": batch_dir, "files": []})
            entry["files"].append((dst, "/".join(sub_dirs + [fname])))

    def zip_batch_finalize_and_send(self, zip_batch_id: str, folder_name: str) -> TransferItemState:
        with self._state_lock:
            entry = self._zip_batches.pop(zip_batch_id, None)
        if not entry or not entry["files"]:
            raise ValueError("No files uploaded for ZIP folder")
        safe_folder = sanitize_relative_path(folder_name or "folder")[1]
        zip_filename = f"{safe_folder}.zip" if not safe_folder.lower().endswith(".zip") else safe_folder
        zip_stage = os.path.join(self._tx_staging_dir, f"{uuid.uuid4().hex}_{zip_filename}")
        with zipfile.ZipFile(zip_stage, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for src_path, arcname in entry["files"]:
                zf.write(src_path, arcname=arcname)
        shutil.rmtree(entry["dir"], ignore_errors=True)
        item = self.enqueue_local_file(zip_stage, zip_filename, zip_batch_id, category="FOLDERS")
        assert item is not None
        return item

    def _tx_worker_loop(self) -> None:
        while self._running:
            try:
                item_id = self._tx_queue.get(timeout=0.3)
            except queue.Empty:
                continue

            with self._state_lock:
                item = next((x for x in self._send_items if x.id == item_id), None)
                host = self.paired_peer_host
                port = self.paired_peer_port
                paired = self.paired
                cancelled = item_id in self._cancelled_ids or self._stopped_flag

            if item is None or cancelled:
                continue
            if not paired or not host or port <= 0:
                with self._state_lock:
                    item.status = "FAILED"
                    item.error_message = "Not paired with a device"
                continue
            if not item.local_source_path or not os.path.exists(item.local_source_path):
                with self._state_lock:
                    item.status = "FAILED"
                    item.error_message = "Source file missing"
                continue

            with self._state_lock:
                item.status = "PAUSED" if self.batch_paused else "TRANSFERRING"
            prev_bytes = [0]

            def on_prog(acked_bytes: int) -> None:
                delta = max(0, acked_bytes - prev_bytes[0])
                prev_bytes[0] = max(prev_bytes[0], acked_bytes)
                with self._state_lock:
                    item.bytes_transferred = min(acked_bytes, item.size_bytes)
                    if not self.batch_paused and item.status == "PAUSED":
                        item.status = "TRANSFERRING"
                self._send_metrics.on_bytes(delta)

            try:
                self.client.send_file(
                    host=host,
                    port=port,
                    file_path=item.local_source_path,
                    relative_name=item.name,
                    batch_id=item.batch_id,
                    transfer_id=item.id,
                    on_progress=on_prog,
                    is_cancelled=lambda: (not self._running) or (item.id in self._cancelled_ids) or self._stopped_flag,
                )
                with self._state_lock:
                    item.bytes_transferred = item.size_bytes
                    item.status = "COMPLETED"
                    item.error_message = None
            except InterruptedError:
                with self._state_lock:
                    item.status = "CANCELLED"
            except Exception as exc:
                with self._state_lock:
                    item.status = "FAILED"
                    item.error_message = str(exc)

            with self._state_lock:
                still_active = any(x.status in ("WAITING", "TRANSFERRING", "PAUSED") for x in self._send_items)
                if not still_active:
                    total_done = sum(x.size_bytes for x in self._send_items if x.status == "COMPLETED")
                    self._send_metrics.freeze_completed(total_done)

    # ── Server Accept & Dual Dispatch (AF01 Binary vs HTTP/1.1) ───────────

    def _accept_loop(self) -> None:
        srv = self._server_sock
        wake_r = self._accept_wake_r
        if srv is None:
            return
        sel = selectors.DefaultSelector()
        try:
            srv.setblocking(False)
            sel.register(srv, selectors.EVENT_READ, data="accept")
            if wake_r is not None:
                sel.register(wake_r, selectors.EVENT_READ, data="wake")
            while self._running:
                try:
                    events = sel.select(timeout=0.5)
                except OSError:
                    break
                if not self._running:
                    break
                for key, _ in events:
                    if key.data == "wake":
                        return
                    if key.data == "accept":
                        while self._running:
                            try:
                                client_sock, addr = srv.accept()
                            except BlockingIOError:
                                break
                            except OSError:
                                return
                            client_sock.setblocking(True)
                            tune_socket(client_sock)
                            t = threading.Thread(
                                target=self._dispatch_connection,
                                args=(client_sock, addr[0]),
                                name="arc-conn",
                                daemon=True,
                            )
                            t.start()
        finally:
            try:
                sel.close()
            except Exception:
                pass

    def _dispatch_connection(self, sock: socket.socket, remote_ip: str) -> None:
        try:
            probe = bytearray()
            probe_deadline = time.monotonic() + 10.0
            while len(probe) < 4 and self._running:
                try:
                    chunk = sock.recv(4 - len(probe))
                except socket.timeout:
                    if time.monotonic() >= probe_deadline:
                        return
                    continue
                if not chunk:
                    return
                probe.extend(chunk)
            if len(probe) < 4:
                return
            if bytes(probe) == MAGIC_BYTES:
                self._handle_binary_session(sock, _PrefixedStream(sock), remote_ip)
            else:
                self._handle_http_connection(sock, _PrefixedStream(sock, bytes(probe)), remote_ip)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ── Incoming AF01 Binary Session ──────────────────────────────────────

    def _handle_binary_session(self, sock: socket.socket, stream: _PrefixedStream, remote_ip: str) -> None:
        handle = _IncomingBinarySessionHandle(sock)
        with self._active_rx_lock:
            self._active_rx_sessions.add(handle)

        write_q: queue.Queue[Any] = queue.Queue(maxsize=MAX_PARALLEL_TRANSFERS * 2)
        sentinel = object()
        connected_peer_id: str | None = None
        clean_stop = False

        def disk_worker() -> None:
            while True:
                item = write_q.get()
                if item is sentinel:
                    break
                fstate, chunk_idx, data = item
                if fstate is None or fstate.closed:
                    continue
                try:
                    while self.batch_paused and self._running and not fstate.closed and not handle.closed:
                        time.sleep(0.04)
                    if fstate.closed or not self._running:
                        continue
                    with fstate.lock:
                        persisted = fstate.write_chunk_ordered(chunk_idx, data)
                        for p_idx, p_data in persisted:
                            if 0 <= p_idx < fstate.total_chunks:
                                fstate.chunk_crcs[p_idx] = crc32_chunk(p_data)
                                fstate.chunk_lens[p_idx] = len(p_data)
                            fstate.bytes_written += len(p_data)
                            fstate.unacked_written += len(p_data)
                            fstate.chunks_written += 1
                            is_final = fstate.chunks_written >= fstate.total_chunks

                            if fstate.unacked_written >= ACK_GRANULARITY_BYTES or is_final:
                                grant = fstate.unacked_written
                                fstate.unacked_written = 0
                                if grant > 0:
                                    ack_payload = encode_ack(
                                        AckFrame(
                                            file_id=fstate.file_id,
                                            acked_bytes_total=min(fstate.bytes_written, fstate.total_size),
                                            credit_bytes_granted=grant,
                                        )
                                    )
                                    handle.send_frame(TYPE_ACK, ack_payload)

                            now_t = time.monotonic()
                            if is_final or (fstate.bytes_written - fstate.last_ui_reported >= 512 * 1024 and (now_t - fstate.last_ui_time) >= 0.08):
                                delta = fstate.bytes_written - fstate.last_ui_reported
                                fstate.last_ui_reported = fstate.bytes_written
                                fstate.last_ui_time = now_t
                                self._on_rx_progress(fstate.transfer_id, fstate.bytes_written, delta)

                            if is_final:
                                fstate.all_done_event.set()
                except Exception as exc:
                    fstate.write_error = str(exc)
                    fstate.all_done_event.set()
                    handle.send_frame(TYPE_ERROR, encode_error(ErrorFrame(fstate.file_id, str(exc))))

        worker_threads = [
            threading.Thread(target=disk_worker, name=f"arc-rx-disk-{index + 1}", daemon=True)
            for index in range(MAX_PARALLEL_TRANSFERS)
        ]
        for worker_thread in worker_threads:
            worker_thread.start()

        try:
            while self._running and not handle.closed:
                try:
                    header = read_frame_header(stream)
                except socket.timeout:
                    continue
                if header is None:
                    break
                frame_type, payload_len = header

                if frame_type == TYPE_HELLO:
                    payload = self._read_frame_payload(stream, payload_len, handle)
                    hello = decode_hello(payload)
                    connected_peer_id = hello.device_id.strip() or None
                    if connected_peer_id and remote_ip and hello.listen_port > 0:
                        self._on_peer_paired(connected_peer_id, hello.device_name, remote_ip, hello.listen_port)

                elif frame_type == TYPE_FILE_META:
                    payload = self._read_frame_payload(stream, payload_len, handle)
                    meta = decode_file_meta(payload)
                    temp_path = os.path.join(self._rx_staging_dir, f".arcpart_{meta.file_id}_{time.time_ns()}.tmp")
                    tid = meta.transfer_id or f"rx_{int(time.time() * 1000)}_{meta.file_id}"
                    fstate = _BinaryIncomingFile(
                        file_id=meta.file_id,
                        transfer_id=tid,
                        relative_name=meta.relative_name or "received_file",
                        total_size=meta.total_size,
                        total_chunks=meta.total_chunks,
                        temp_path=temp_path,
                    )
                    handle.active_files[meta.file_id] = fstate
                    self._on_rx_started(tid, fstate.relative_name, meta.total_size, meta.batch_id)

                elif frame_type == TYPE_DATA:
                    if payload_len < DATA_HEADER_SIZE:
                        raise ValueError("Truncated DATA frame")
                    meta_buf = self._read_frame_payload(stream, DATA_HEADER_SIZE, handle)
                    file_id, _, chunk_idx, _, _ = unpack_data_meta(meta_buf)
                    data_len = payload_len - DATA_HEADER_SIZE
                    data = self._read_frame_payload(stream, data_len, handle) if data_len > 0 else b""
                    fstate = handle.active_files.get(file_id)
                    write_q.put((fstate, chunk_idx, data))

                elif frame_type == TYPE_DONE:
                    payload = self._read_frame_payload(stream, payload_len, handle)
                    done = decode_done(payload)
                    fstate = handle.active_files.get(done.file_id)
                    if fstate is not None:
                        threading.Thread(
                            target=self._finalize_incoming_file,
                            args=(handle, fstate, done),
                            daemon=True,
                        ).start()

                elif frame_type == TYPE_CONTROL:
                    payload = self._read_frame_payload(stream, payload_len, handle)
                    ctrl = decode_control(payload)
                    pid = ctrl.peer_id or (connected_peer_id or "")
                    action = CONTROL_ACTION_NAMES.get(ctrl.action_code, "")
                    if ctrl.action_code == CTRL_STOP:
                        clean_stop = True
                        self._handle_remote_control("STOP", pid)
                        break
                    elif ctrl.action_code == CTRL_DISCONNECT:
                        clean_stop = True
                        self._handle_remote_control("DISCONNECT", pid)
                        break
                    elif action:
                        self._handle_remote_control(action, pid)
                else:
                    if payload_len > 0:
                        self._read_frame_payload(stream, payload_len, handle)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            handle.closed = True
            for _ in worker_threads:
                write_q.put(sentinel)
            for worker_thread in worker_threads:
                worker_thread.join(timeout=3.0)
            for fid, leftover in list(handle.active_files.items()):
                handle.active_files.pop(fid, None)
                if not leftover.closed:
                    leftover.abort_and_delete()
                    self._on_rx_failed(leftover.transfer_id, leftover.bytes_written, "Connection closed")
            with self._active_rx_lock:
                self._active_rx_sessions.discard(handle)
            if not clean_stop and self._running and connected_peer_id:
                with self._state_lock:
                    if self.paired_peer_id == connected_peer_id:
                        self.paired = False
                        self.paired_peer_id = None
                        self.paired_peer_name = None
                        self.paired_peer_host = None
                        self.paired_peer_port = 0

    def _read_frame_payload(self, stream: _PrefixedStream, length: int, handle: _IncomingBinarySessionHandle) -> bytes:
        if length <= 0:
            return b""
        while True:
            if handle.closed or not self._running:
                raise EOFError("Session closed")
            try:
                data = read_exact(stream, length)
                if data is None or len(data) != length:
                    raise EOFError("Unexpected EOF reading payload")
                return data
            except socket.timeout:
                continue

    def _finalize_incoming_file(
        self,
        handle: _IncomingBinarySessionHandle,
        fstate: _BinaryIncomingFile,
        done: DoneFrame,
    ) -> None:
        try:
            fstate.all_done_event.wait(timeout=30.0)
            if fstate.write_error:
                raise OSError(fstate.write_error)
            chunks_ok = fstate.chunks_written == done.total_chunks
            bytes_ok = fstate.bytes_written == done.total_bytes
            crc = fstate.compute_combined_crc32() if done.total_chunks > 0 else 0
            crc_ok = chunks_ok and bytes_ok and (crc == done.crc32)
            if not crc_ok:
                fstate.abort_and_delete()
                handle.send_frame(TYPE_DONE_ACK, encode_done_ack(DoneAckFrame(fstate.file_id, fstate.bytes_written, False)))
                self._on_rx_failed(fstate.transfer_id, fstate.bytes_written, "Checksum mismatch")
                return

            fstate.close_and_flush()
            dest_path = reserve_unique_confined_path(self.save_dir, fstate.relative_name)
            try:
                os.replace(fstate.temp_path, dest_path)
            except OSError:
                shutil.move(fstate.temp_path, dest_path)

            handle.send_frame(TYPE_DONE_ACK, encode_done_ack(DoneAckFrame(fstate.file_id, done.total_bytes, True)))
            self._on_rx_completed(fstate.transfer_id, done.total_bytes, dest_path)
        except Exception as exc:
            fstate.abort_and_delete()
            handle.send_frame(TYPE_ERROR, encode_error(ErrorFrame(fstate.file_id, str(exc))))
            self._on_rx_failed(fstate.transfer_id, fstate.bytes_written, str(exc))
        finally:
            handle.active_files.pop(fstate.file_id, None)

    def _on_rx_started(self, tid: str, rel_name: str, size_bytes: int, batch_id: str) -> None:
        with self._state_lock:
            no_active = not any(x.status in ("WAITING", "TRANSFERRING", "PAUSED") for x in self._recv_items)
            if no_active:
                self._recv_metrics.reset_for_new_batch()
            self._recv_items.append(
                TransferItemState(
                    id=tid,
                    name=rel_name,
                    direction="RECEIVE",
                    size_bytes=size_bytes,
                    status="PAUSED" if self.batch_paused else "TRANSFERRING",
                    batch_id=batch_id,
                )
            )

    def _on_rx_progress(self, tid: str, bytes_rx: int, delta: int) -> None:
        with self._state_lock:
            it = next((x for x in self._recv_items if x.id == tid), None)
            if it is not None:
                it.bytes_transferred = min(bytes_rx, it.size_bytes)
                if not self.batch_paused and it.status == "WAITING":
                    it.status = "TRANSFERRING"
        self._recv_metrics.on_bytes(delta)

    def _on_rx_completed(self, tid: str, size_bytes: int, saved_path: str) -> None:
        with self._state_lock:
            it = next((x for x in self._recv_items if x.id == tid), None)
            if it is not None:
                it.bytes_transferred = size_bytes
                it.size_bytes = size_bytes
                it.status = "COMPLETED"
                it.saved_path = saved_path
            still_active = any(x.status in ("WAITING", "TRANSFERRING", "PAUSED") for x in self._recv_items)
            if not still_active:
                total_done = sum(x.size_bytes for x in self._recv_items if x.status == "COMPLETED")
                self._recv_metrics.freeze_completed(total_done)

    def _on_rx_failed(self, tid: str, bytes_rx: int, reason: str) -> None:
        with self._state_lock:
            it = next((x for x in self._recv_items if x.id == tid), None)
            if it is not None:
                it.bytes_transferred = bytes_rx
                if it.status != "CANCELLED":
                    it.status = "FAILED"
                    it.error_message = reason
            still_active = any(x.status in ("WAITING", "TRANSFERRING", "PAUSED") for x in self._recv_items)
            if not still_active:
                total_done = sum(x.size_bytes for x in self._recv_items if x.status == "COMPLETED")
                self._recv_metrics.freeze_completed(total_done)

    # ── HTTP/1.1 Server (Android Peer Compatibility + Local PC Web UI) ─────

    @staticmethod
    def _is_loopback_client(remote_ip: str) -> bool:
        clean = (remote_ip or "").strip().lower()
        if clean.startswith("::ffff:"):
            clean = clean[7:]
        try:
            return ipaddress.ip_address(clean).is_loopback
        except ValueError:
            return clean in ("127.0.0.1", "::1", "localhost")

    def _handle_http_connection(self, sock: socket.socket, stream: _PrefixedStream, remote_ip: str) -> None:
        bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        static_dir = os.path.join(bundle_dir, "static")
        while self._running:
            try:
                req_line_b = stream.readline()
            except socket.timeout:
                if self.batch_paused:
                    continue
                break
            if not req_line_b:
                break
            req_line = req_line_b.decode("utf-8", errors="replace").strip()
            if not req_line:
                continue
            parts = req_line.split(" ")
            if len(parts) < 2:
                self._http_send_text(sock, 400, "Bad Request", "Bad Request", keep_alive=False)
                break
            method = parts[0].upper()
            raw_target = parts[1]
            parsed = urllib.parse.urlparse(raw_target)
            path = parsed.path

            headers: dict[str, str] = {}
            while True:
                hline = stream.readline()
                if hline is None:
                    return
                hstr = hline.decode("utf-8", errors="replace").rstrip("\r\n")
                if not hstr:
                    break
                if ":" in hstr:
                    k, v = hstr.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            keep_alive = headers.get("connection", "").lower() != "close"

            # ── Peer LAN Compatibility Endpoints ──
            if method == "GET" and path == "/ping":
                body = json.dumps({
                    "did": self.device_id,
                    "name": self.device_name,
                    "port": self.listen_port,
                    "status": "ready",
                    "protocol": "AF01",
                }, separators=(",", ":"))
                self._http_send_json(sock, 200, "OK", body, keep_alive=keep_alive)
                if not keep_alive:
                    break
                continue

            if method == "POST" and path == "/pair":
                clen = max(0, min(int(headers.get("content-length", "0") or 0), 65536))
                body_b = self._read_http_body(stream, clen)
                obj = json.loads(body_b.decode("utf-8", errors="ignore") or "{}")
                pid = str(obj.get("did", "")).strip()
                pname = str(obj.get("name", "")).strip() or f"Peer {pid[:8]}"
                pport = int(obj.get("port", 0) or 0)
                if pid and pport > 0 and remote_ip:
                    self._on_peer_paired(pid, pname, remote_ip, pport)
                resp = json.dumps({
                    "did": self.device_id,
                    "name": self.device_name,
                    "port": self.listen_port,
                    "status": "paired",
                }, separators=(",", ":"))
                self._http_send_json(sock, 200, "OK", resp, keep_alive=keep_alive)
                if not keep_alive:
                    break
                continue

            if method == "POST" and path == "/control":
                clen = max(0, min(int(headers.get("content-length", "0") or 0), 16384))
                body_b = self._read_http_body(stream, clen)
                obj = json.loads(body_b.decode("utf-8", errors="ignore") or "{}")
                pid = str(obj.get("did", "")).strip()
                act = str(obj.get("action", "")).strip().upper()
                self._http_send_text(sock, 200, "OK", "ACK", keep_alive=keep_alive)
                if act:
                    self._handle_remote_control(act, pid)
                if not keep_alive:
                    break
                continue

            if method == "POST" and path == "/disconnect":
                clen = max(0, min(int(headers.get("content-length", "0") or 0), 16384))
                body_b = self._read_http_body(stream, clen)
                obj = json.loads(body_b.decode("utf-8", errors="ignore") or "{}")
                pid = str(obj.get("did", "")).strip()
                self._http_send_text(sock, 200, "OK", "BYE", keep_alive=False)
                self._handle_remote_control("DISCONNECT", pid)
                break

            if method == "POST" and path == "/upload":
                ok = self._handle_http_upload(sock, stream, headers, keep_alive)
                if not ok or not keep_alive:
                    break
                continue

            # ── Local PC Web UI Static Assets ──
            if method == "GET" and path in ("/", "/index.html", "/styles.css", "/app.js", "/qrcode.js", "/brand-icon.png"):
                if not self._is_loopback_client(remote_ip):
                    self._http_send_text(sock, 403, "Forbidden", "Open Arc Flash Web UI on the PC host (127.0.0.1).", keep_alive=False)
                    break
                rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
                fpath = os.path.realpath(os.path.join(static_dir, rel))
                if not fpath.startswith(os.path.realpath(static_dir) + os.sep) or not os.path.isfile(fpath):
                    self._http_send_text(sock, 404, "Not Found", "Not Found", keep_alive=keep_alive)
                    break
                ctype = {
                    ".html": "text/html; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png",
                }.get(os.path.splitext(fpath)[1].lower(), "application/octet-stream")
                with open(fpath, "rb") as fp:
                    self._http_send_bytes(sock, 200, "OK", ctype, fp.read(), keep_alive=keep_alive)
                if not keep_alive:
                    break
                continue

            # ── Local PC Web UI Control APIs (Loopback Only) ──
            if path.startswith("/api/"):
                if not self._is_loopback_client(remote_ip):
                    self._http_send_json(sock, 403, "Forbidden", '{"error":"Local access only"}', keep_alive=False)
                    break
                ok = self._handle_local_api(sock, stream, method, path, headers, keep_alive)
                if not ok or not keep_alive:
                    break
                continue

            self._http_send_text(sock, 404, "Not Found", "Not Found", keep_alive=False)
            break

    def _read_http_body(self, stream: _PrefixedStream, length: int) -> bytes:
        if length <= 0:
            return b""
        buf = bytearray()
        while len(buf) < length:
            try:
                chunk = stream.read(length - len(buf))
            except socket.timeout:
                if not self._running:
                    break
                continue
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _handle_http_upload(self, sock: socket.socket, stream: _PrefixedStream, headers: dict[str, str], keep_alive: bool) -> bool:
        tid = headers.get("x-arc-id") or f"rx_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        rel_name = urllib.parse.unquote(headers.get("x-arc-name", "received_file"))
        batch_id = headers.get("x-arc-batch-id", "")
        size_bytes = int(headers.get("content-length") or headers.get("x-arc-size") or 0)

        self._on_rx_started(tid, rel_name, size_bytes, batch_id)
        temp_path = os.path.join(self._rx_staging_dir, f".httppart_{time.time_ns()}.tmp")
        received = 0
        last_reported = 0
        last_t = 0.0
        try:
            with open(temp_path, "wb", buffering=DISK_BUFFER_BYTES) as fp:
                rem = size_bytes
                while rem > 0:
                    while self.batch_paused and self._running:
                        time.sleep(0.05)
                    if not self._running:
                        raise InterruptedError("Cancelled")
                    to_read = min(DISK_BUFFER_BYTES, rem)
                    try:
                        chunk = stream.read(to_read)
                    except socket.timeout:
                        if self.batch_paused:
                            continue
                        raise
                    if not chunk:
                        raise EOFError("Unexpected EOF during HTTP upload")
                    fp.write(chunk)
                    received += len(chunk)
                    rem -= len(chunk)
                    now_t = time.monotonic()
                    if rem == 0 or (received - last_reported >= 512 * 1024 and (now_t - last_t) >= 0.08):
                        delta = received - last_reported
                        last_reported = received
                        last_t = now_t
                        self._on_rx_progress(tid, received, delta)
            dest = reserve_unique_confined_path(self.save_dir, rel_name)
            try:
                os.replace(temp_path, dest)
            except OSError:
                shutil.move(temp_path, dest)
            self._on_rx_completed(tid, size_bytes, dest)
            self._http_send_text(sock, 200, "OK", "OK", keep_alive=keep_alive, extra_headers={"X-Arc-Bytes-Saved": str(received)})
            return True
        except Exception as exc:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            self._on_rx_failed(tid, received, str(exc))
            self._http_send_text(sock, 500, "Error", str(exc), keep_alive=False)
            return False

    def _handle_local_api(
        self,
        sock: socket.socket,
        stream: _PrefixedStream,
        method: str,
        path: str,
        headers: dict[str, str],
        keep_alive: bool,
    ) -> bool:
        if method == "GET" and path == "/api/pick-save-dir":
            try:
                import tkinter
                from tkinter import filedialog

                root = tkinter.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                try:
                    selected = filedialog.askdirectory(
                        parent=root,
                        title="Choose Arc Flash storage folder",
                        initialdir=self.save_dir,
                        mustexist=True,
                    )
                finally:
                    root.destroy()
                if selected:
                    response = json.dumps({"ok": True, "path": os.path.abspath(selected)})
                else:
                    response = '{"ok":false,"cancelled":true}'
                self._http_send_json(sock, 200, "OK", response, keep_alive=keep_alive)
            except Exception as exc:
                self._http_send_json(
                    sock,
                    500,
                    "Error",
                    json.dumps({"ok": False, "error": f"Could not open folder picker: {exc}"}),
                    keep_alive=keep_alive,
                )
            return True

        if method == "GET" and path == "/api/state":
            body = json.dumps(self.get_ui_state(), separators=(",", ":"))
            self._http_send_json(sock, 200, "OK", body, keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/send-stream":
            clen = int(headers.get("content-length") or 0)
            rel_name = urllib.parse.unquote(headers.get("x-arc-name", "file"))
            batch_id = headers.get("x-arc-batch-id", f"pc_{int(time.time() * 1000)}")
            category = (headers.get("x-arc-category") or "FILES").upper()
            zip_mode = headers.get("x-arc-zip-mode", "0") == "1"

            if not zip_mode and not matches_category(rel_name, category):
                self._read_http_body(stream, clen)
                self._http_send_json(
                    sock,
                    200,
                    "OK",
                    json.dumps({"skipped": True, "reason": f"Filtered out of {category}"}),
                    keep_alive=keep_alive,
                )
                return True

            stage_path = os.path.join(self._tx_staging_dir, f"tx_{uuid.uuid4().hex}_{sanitize_relative_path(rel_name)[1]}")
            try:
                with open(stage_path, "wb", buffering=DISK_BUFFER_BYTES) as fp:
                    rem = clen
                    while rem > 0:
                        chunk = stream.read(min(DISK_BUFFER_BYTES, rem))
                        if not chunk:
                            raise EOFError("Stream closed early")
                        fp.write(chunk)
                        rem -= len(chunk)
                if zip_mode:
                    self.zip_batch_stage_append(batch_id, rel_name, stage_path)
                    self._http_send_json(sock, 200, "OK", json.dumps({"staged": True}), keep_alive=keep_alive)
                else:
                    item = self.enqueue_local_file(stage_path, rel_name, batch_id, category=category)
                    self._http_send_json(
                        sock,
                        200,
                        "OK",
                        json.dumps({"queued": True, "item": item.to_dict() if item else None}),
                        keep_alive=keep_alive,
                    )
                return True
            except Exception as exc:
                try:
                    if os.path.exists(stage_path):
                        os.remove(stage_path)
                except OSError:
                    pass
                self._http_send_json(sock, 500, "Error", json.dumps({"error": str(exc)}), keep_alive=False)
                return False

        # JSON body endpoints
        clen = max(0, min(int(headers.get("content-length") or 0), 256 * 1024))
        raw = self._read_http_body(stream, clen)
        try:
            data = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
        except Exception:
            data = {}

        if method == "POST" and path == "/api/connect":
            ok, msg = self.connect_to_peer_id(str(data.get("deviceId", "")))
            self._http_send_json(sock, 200 if ok else 400, "OK", json.dumps({"ok": ok, "message": msg}), keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/disconnect":
            self.disconnect_peer()
            self._http_send_json(sock, 200, "OK", '{"ok":true}', keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/refresh":
            self.discovery.force_refresh()
            self._http_send_json(sock, 200, "OK", '{"ok":true}', keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/pause-resume":
            paused = self.toggle_pause_resume()
            self._http_send_json(sock, 200, "OK", json.dumps({"ok": True, "paused": paused}), keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/stop":
            self.stop_all_transfers(notify_peer=True)
            self._http_send_json(sock, 200, "OK", '{"ok":true}', keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/clear":
            self.clear_transfers(str(data.get("tab", "send")))
            self._http_send_json(sock, 200, "OK", '{"ok":true}', keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/qr/regenerate":
            payload, exp = self.regenerate_qr()
            self._http_send_json(sock, 200, "OK", json.dumps({"qrPayload": payload, "qrExpiresAtMs": exp}), keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/settings":
            ok, msg = self.update_settings(
                save_dir=data.get("saveDir"),
                theme=data.get("theme"),
                device_name=data.get("deviceName"),
            )
            self._http_send_json(sock, 200 if ok else 400, "OK", json.dumps({"ok": ok, "message": msg}), keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/send-zip-finish":
            bid = str(data.get("batchId", ""))
            fname = str(data.get("folderName", "folder"))
            try:
                item = self.zip_batch_finalize_and_send(bid, fname)
                self._http_send_json(sock, 200, "OK", json.dumps({"ok": True, "item": item.to_dict()}), keep_alive=keep_alive)
            except Exception as exc:
                self._http_send_json(sock, 400, "Bad Request", json.dumps({"ok": False, "error": str(exc)}), keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/cancel-item":
            self.cancel_item(str(data.get("id", "")))
            self._http_send_json(sock, 200, "OK", '{"ok":true}', keep_alive=keep_alive)
            return True

        if method == "POST" and path == "/api/retry-item":
            ok, msg = self.retry_item(str(data.get("id", "")))
            self._http_send_json(sock, 200 if ok else 400, "OK", json.dumps({"ok": ok, "message": msg}), keep_alive=keep_alive)
            return True

        self._http_send_json(sock, 404, "Not Found", '{"error":"Unknown API route"}', keep_alive=keep_alive)
        return True

    def _http_send_text(
        self,
        sock: socket.socket,
        code: int,
        reason: str,
        body: str,
        keep_alive: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._http_send_bytes(sock, code, reason, "text/plain; charset=utf-8", body.encode("utf-8"), keep_alive, extra_headers)

    def _http_send_json(self, sock: socket.socket, code: int, reason: str, body: str, keep_alive: bool) -> None:
        self._http_send_bytes(sock, code, reason, "application/json; charset=utf-8", body.encode("utf-8"), keep_alive)

    @staticmethod
    def _http_send_bytes(
        sock: socket.socket,
        code: int,
        reason: str,
        content_type: str,
        body_bytes: bytes,
        keep_alive: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        lines = [
            f"HTTP/1.1 {code} {reason}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body_bytes)}",
            "Cache-Control: no-store",
            f"Connection: {'keep-alive' if keep_alive else 'close'}",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}")
        header_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        _sock_send_all(sock, header_bytes + body_bytes, lambda: False)
