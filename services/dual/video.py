"""Expose one video track from muxed fMP4 without changing sample offsets."""
from __future__ import annotations

import re


def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    while start < end:
        if end - start < 8:
            raise ValueError("truncated MP4 box")
        size = int.from_bytes(data[start:start + 4], "big")
        header = 8
        if size == 1:
            if end - start < 16:
                raise ValueError("truncated extended MP4 box")
            size = int.from_bytes(data[start + 8:start + 16], "big")
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError("invalid MP4 box size")
        yield data[start + 4:start + 8], start, start + header, start + size
        start += size


def video_track_id(data: bytes) -> int:
    videos = []
    for kind, _, payload, end in boxes(data):
        if kind != b"moov":
            continue
        for kind, _, payload, end in boxes(data, payload, end):
            if kind != b"trak":
                continue
            track_id = None
            handler = None
            for kind, _, payload, end in boxes(data, payload, end):
                if kind == b"tkhd":
                    if payload >= end or data[payload] not in (0, 1):
                        raise ValueError("invalid track header version")
                    offset = payload + (20 if data[payload] == 1 else 12)
                    if offset + 4 > end:
                        raise ValueError("truncated track header")
                    track_id = int.from_bytes(data[offset:offset + 4], "big")
                elif kind == b"mdia":
                    for kind, _, payload, end in boxes(data, payload, end):
                        if kind == b"hdlr":
                            if payload + 12 > end:
                                raise ValueError("truncated handler")
                            handler = data[payload + 8:payload + 12]
            if handler == b"vide" and track_id:
                videos.append(track_id)
    if len(videos) != 1:
        raise ValueError("DUAL requires exactly one fMP4 video track")
    return videos[0]


def video_only(data: bytes, track_id: int) -> bytes:
    """Replace other track boxes with equal-size free boxes.

    Keeping every byte position preserves trun data_offset, absolute offsets,
    sidx sizes, byte ranges and decode timestamps. Unreferenced audio samples
    remain in mdat; they are not advertised or decoded as an audio track.
    """
    out = bytearray(data)

    def discard(start, payload, end):
        out[start + 4:start + 8] = b"free"
        out[payload:end] = b"\0" * (end - payload)

    def filter_children(start, end, track_box, id_offset):
        kept = 0
        for kind, box_start, payload, box_end in boxes(data, start, end):
            if kind == track_box:
                if track_box in (b"trak", b"traf"):
                    header_kind = b"tkhd" if track_box == b"trak" else b"tfhd"
                    ids = []
                    for child, _, cp, ce in boxes(data, payload, box_end):
                        if child == header_kind:
                            if cp + 4 > ce or (child == b"tkhd" and data[cp] not in (0, 1)):
                                raise ValueError("invalid MP4 track header")
                            offset = cp + (20 if data[cp] == 1 else 12) if child == b"tkhd" else cp + 4
                            if offset + 4 > ce:
                                raise ValueError("truncated MP4 track ID")
                            current_id = int.from_bytes(data[offset:offset + 4], "big")
                            if child == b"tfhd" and current_id == track_id:
                                flags = int.from_bytes(data[cp + 1:cp + 4], "big")
                                if not flags & (0x020000 | 0x000001):
                                    raise ValueError("DUAL requires explicit fragment data offsets")
                            ids.append(current_id)
                    if len(ids) != 1:
                        raise ValueError("missing MP4 track header")
                    current = ids[0]
                else:
                    if payload + id_offset + 4 > box_end:
                        raise ValueError("truncated MP4 track ID")
                    current = int.from_bytes(data[payload + id_offset:payload + id_offset + 4], "big")
                if current != track_id:
                    discard(box_start, payload, box_end)
                else:
                    kept += 1
            elif kind == b"mvex":
                filter_children(payload, box_end, b"trex", 4)
        return kept

    found = False
    for kind, _, payload, end in boxes(data):
        if kind == b"moov":
            found = True
            if video_track_id(data) != track_id:
                raise ValueError("DUAL video track changed")
            filter_children(payload, end, b"trak", 0)
        elif kind == b"moof":
            found = True
            if filter_children(payload, end, b"traf", 0) != 1:
                raise ValueError("missing or duplicate DUAL video fragment")
    if not found:
        raise ValueError("DUAL expected an fMP4 init or media segment")
    return bytes(out)


def byte_range(data: bytes, requested: str | None):
    """Apply a single client byte range after filtering the complete object."""
    total = len(data)
    if not requested:
        return data, 200, None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
    if not match or not any(match.groups()):
        return b"", 416, f"bytes */{total}"
    start = int(match.group(1)) if match.group(1) else max(0, total - int(match.group(2)))
    end = min(total - 1, int(match.group(2))) if match.group(1) and match.group(2) else total - 1
    if start > end:
        return b"", 416, f"bytes */{total}"
    return data[start:end + 1], 206, f"bytes {start}-{end}/{total}"
