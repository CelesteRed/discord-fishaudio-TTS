"""Verify that we can demux Fish's Ogg/Opus output to raw Opus packets,
and inspect the TOC byte of each packet to confirm Discord compatibility.

Discord requires:
  - 48000 Hz
  - 20ms frames
  - mono or stereo (will check what Fish gives us)

The Opus TOC byte (first byte of each packet):
  - bits 7-3: configuration (0-31)
  - bit 2: stereo flag (0=mono, 1=stereo)
  - bits 1-0: frame count code

Configurations 12-15 are CELT-only at 48kHz. 14-15 = 20ms frames @ 48kHz.
Reference: RFC 6716 section 3.1
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

try:
    from pyogg import OpusFile, OggOpusWriter  # noqa: F401
    HAS_PYOGG = True
except Exception as e:
    print(f"pyogg import error: {e}")
    HAS_PYOGG = False


def parse_ogg_pages(data: bytes):
    """Yield (header_type, granule_pos, page_seq, segments_bytes) for each Ogg page."""
    pos = 0
    n = len(data)
    while pos < n:
        if data[pos:pos+4] != b"OggS":
            raise ValueError(f"bad page at offset {pos}: {data[pos:pos+4]!r}")
        # capture pattern + version + header_type + granule_pos(8) + serial(4) + seq(4) + crc(4) + n_segs
        version = data[pos+4]
        header_type = data[pos+5]
        granule_pos = struct.unpack_from("<q", data, pos+6)[0]
        page_seq = struct.unpack_from("<I", data, pos+18)[0]
        n_segs = data[pos+26]
        seg_table = data[pos+27:pos+27+n_segs]
        body_start = pos + 27 + n_segs
        # Group segments into packets (segments of 255 are continuations)
        packets = []
        cur = bytearray()
        seg_pos = body_start
        for seglen in seg_table:
            cur += data[seg_pos:seg_pos+seglen]
            seg_pos += seglen
            if seglen < 255:
                packets.append(bytes(cur))
                cur = bytearray()
        # leftover (cur) means packet continues to next page
        body_len = sum(seg_table)
        yield {
            "header_type": header_type,
            "granule_pos": granule_pos,
            "page_seq": page_seq,
            "packets_complete": packets,
            "leftover": bytes(cur),
            "page_size": 27 + n_segs + body_len,
        }
        pos = body_start + body_len


def decode_toc(b: int) -> dict:
    config = (b >> 3) & 0x1F
    stereo = (b >> 2) & 0x01
    code = b & 0x03
    # Frame durations indexed by config (RFC 6716 Table 2)
    # CELT-only @ 48kHz: configs 16-19 = 2.5/5/10/20 ms
    # Hybrid @ 48kHz: 14-15 = 10/20 ms
    # SILK @ 8kHz: 0-3 = 10/20/40/60 ms ... etc
    fd = {
        0: 10, 1: 20, 2: 40, 3: 60,    # SILK NB
        4: 10, 5: 20, 6: 40, 7: 60,    # SILK MB
        8: 10, 9: 20, 10: 40, 11: 60,  # SILK WB
        12: 10, 13: 20,                # Hybrid SWB
        14: 10, 15: 20,                # Hybrid FB
        16: 2.5, 17: 5, 18: 10, 19: 20,  # CELT NB
        20: 2.5, 21: 5, 22: 10, 23: 20,  # CELT WB
        24: 2.5, 25: 5, 26: 10, 27: 20,  # CELT SWB
        28: 2.5, 29: 5, 30: 10, 31: 20,  # CELT FB
    }
    bw = {
        **dict.fromkeys(range(0, 4), "NB-8k"),
        **dict.fromkeys(range(4, 8), "MB-12k"),
        **dict.fromkeys(range(8, 12), "WB-16k"),
        **dict.fromkeys(range(12, 14), "SWB-24k"),
        **dict.fromkeys(range(14, 16), "FB-48k"),
        **dict.fromkeys(range(16, 20), "CELT-NB-8k"),
        **dict.fromkeys(range(20, 24), "CELT-WB-16k"),
        **dict.fromkeys(range(24, 28), "CELT-SWB-24k"),
        **dict.fromkeys(range(28, 32), "CELT-FB-48k"),
    }
    return {
        "config": config,
        "stereo": bool(stereo),
        "code": code,
        "frame_ms": fd.get(config),
        "mode": bw.get(config),
        "raw": f"0x{b:02x}",
    }


def main(path: str) -> None:
    data = Path(path).read_bytes()
    print(f"file: {path}, size: {len(data)} bytes")

    pages = list(parse_ogg_pages(data))
    print(f"\nOgg pages: {len(pages)}")
    for i, p in enumerate(pages[:3]):
        print(f"  page#{i} seq={p['page_seq']} header_type=0x{p['header_type']:02x} granule={p['granule_pos']} complete-packets={len(p['packets_complete'])} leftover-bytes={len(p['leftover'])}")

    # Collect all packets
    packets = []
    leftover = bytearray()
    for p in pages:
        if leftover:
            if p["packets_complete"]:
                packets.append(bytes(leftover) + p["packets_complete"][0])
                packets.extend(p["packets_complete"][1:])
            else:
                leftover += p["leftover"]
                continue
        else:
            packets.extend(p["packets_complete"])
        if p["leftover"]:
            leftover = bytearray(p["leftover"])
        else:
            leftover = bytearray()

    print(f"\ntotal opus-layer packets: {len(packets)}")

    # First packet should be OpusHead (header), second OpusTags, rest audio
    head = packets[0]
    print(f"\npacket#0 (OpusHead) size={len(head)}")
    if head[:8] == b"OpusHead":
        version = head[8]
        channels = head[9]
        pre_skip = struct.unpack_from("<H", head, 10)[0]
        sample_rate = struct.unpack_from("<I", head, 12)[0]
        print(f"  version={version} channels={channels} pre_skip={pre_skip} input_sample_rate={sample_rate}")

    tags = packets[1]
    print(f"\npacket#1 (OpusTags) size={len(tags)}")
    if tags[:8] == b"OpusTags":
        # Vendor string length + vendor
        vlen = struct.unpack_from("<I", tags, 8)[0]
        vendor = tags[12:12+vlen].decode("utf-8", "replace")
        print(f"  vendor={vendor!r}")

    # Audio packets
    audio_packets = packets[2:]
    print(f"\naudio packets: {len(audio_packets)}")
    print("\nfirst 5 audio packet TOCs:")
    for i, pkt in enumerate(audio_packets[:5]):
        toc = decode_toc(pkt[0])
        print(f"  pkt#{i} size={len(pkt)} {toc}")

    # Aggregate packet stats
    sizes = [len(p) for p in audio_packets]
    print(f"\naudio packet size: min={min(sizes)} max={max(sizes)} avg={sum(sizes)//len(sizes)}")

    # Verify uniformity of TOC
    tocs = {decode_toc(p[0])["raw"] for p in audio_packets}
    print(f"distinct TOC bytes: {tocs}")

    # Total duration check
    total_ms = sum(decode_toc(p[0])["frame_ms"] or 0 for p in audio_packets)
    print(f"total audio duration (sum of packet frame_ms): {total_ms} ms = {total_ms/1000:.2f}s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fish_probe/probe.opus")
