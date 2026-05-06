from pathlib import Path
import pytest
from discord_fish_tts.ogg_demux import OggDemuxer

FIXTURE = Path("/tmp/fish_probe/probe.opus")

def test_demux_full_file_yields_audio_packets():
    if not FIXTURE.exists():
        pytest.skip("probe fixture missing; run _scripts/probe_fish_opus.py first")
    data = FIXTURE.read_bytes()
    demux = OggDemuxer()
    demux.feed(data)
    packets = list(demux.packets())
    packets += list(demux.flush())

    assert len(packets) == 381, f"expected 381 audio packets, got {len(packets)}"
    for pkt in packets:
        # All Fish-encoded audio packets share TOC byte 0xf8 (CELT 20ms mono)
        assert pkt[0] == 0xF8, f"unexpected TOC byte 0x{pkt[0]:02x}"


def test_demux_skips_opushead_and_opustags():
    if not FIXTURE.exists():
        pytest.skip("probe fixture missing")
    data = FIXTURE.read_bytes()
    demux = OggDemuxer()
    demux.feed(data)
    packets = list(demux.packets())
    # No packet should start with 'OpusHead' or 'OpusTags'
    assert not any(p.startswith(b"OpusHead") for p in packets)
    assert not any(p.startswith(b"OpusTags") for p in packets)


def test_demux_handles_chunked_input():
    """Feed bytes in small chunks; must still extract all packets."""
    if not FIXTURE.exists():
        pytest.skip("probe fixture missing")
    data = FIXTURE.read_bytes()

    demux = OggDemuxer()
    chunk_size = 137  # arbitrary, crosses page/segment boundaries
    collected: list[bytes] = []
    for i in range(0, len(data), chunk_size):
        demux.feed(data[i:i+chunk_size])
        collected.extend(demux.packets())
    collected.extend(demux.flush())

    assert len(collected) == 381


def test_demux_empty_input_yields_nothing():
    demux = OggDemuxer()
    assert list(demux.packets()) == []
    assert list(demux.flush()) == []


def test_demux_packets_idempotent():
    """Calling packets() twice with no feed() between yields nothing on the second call."""
    if not FIXTURE.exists():
        pytest.skip("probe fixture missing")
    demux = OggDemuxer()
    demux.feed(FIXTURE.read_bytes())
    first = list(demux.packets())
    second = list(demux.packets())
    assert len(first) == 381
    assert second == []
