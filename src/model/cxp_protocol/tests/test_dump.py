"""cxp_protocol.dump: the raw downlink dump round-trips, and says when it is cut short."""

import gzip
import io

import pytest

from cxp_protocol import dump, packets as p
from cxp_protocol.kcodes import IDLE_KMASK, IDLE_WORD


def _beats():
    # IDLE, one 4-word stream packet, an I/O ack inserted in it, IDLE.
    pkt = p.stream_packet(1, 7, p.data_beats([0x11223344, 0x55667788]))
    ack = p.io_ack()
    beats = [(IDLE_WORD, IDLE_KMASK)] * 3 + pkt[:3] + ack + pkt[3:]
    beats += [(IDLE_WORD, IDLE_KMASK)] * 2
    return beats


def test_round_trip(tmp_path):
    path = tmp_path / "downlink.bin.gz"
    w = dump.DumpWriter(str(path), tx_period_ps=10_000)
    beats = _beats()
    for i, (word, k) in enumerate(beats):
        w.write(100 + i, word, k)
    w.close()

    d = dump.read(str(path))
    assert d.header.tx_period_ps == 10_000
    assert d.header.version == dump.VERSION
    assert not d.truncated
    assert [(c, wd, k) for c, wd, k in d.records] == \
        [(100 + i, wd, k) for i, (wd, k) in enumerate(beats)]

    # The lazy reader sees the same records.
    hdr, it = dump.iter_records(str(path))
    assert hdr.tx_period_ps == 10_000
    assert list(it) == d.records


def test_summary_counts_packets(tmp_path):
    path = tmp_path / "d.bin.gz"
    w = dump.DumpWriter(str(path), tx_period_ps=8_000)
    for i, (word, k) in enumerate(_beats()):
        w.write(i, word, k)
    w.close()
    s = dump.summarise(dump.read(str(path)))
    assert s["idle_words"] == 5
    assert s["packets"] == {"0x01": 1, "ioack": 1}
    assert s["deframer_errors"] == 0
    assert dump.main([str(path)]) == 0


def test_cap_truncates_and_marks(tmp_path):
    path = tmp_path / "cap.bin.gz"
    w = dump.DumpWriter(str(path), tx_period_ps=10_000, max_bytes=1)
    w.FLUSH_RECORDS = 8
    for i in range(100):
        w.write(i, i * 0x01010101, 0)       # incompressible enough to pass 1 byte
    w.close()
    assert w.truncated
    d = dump.read(str(path))
    assert d.truncated
    assert len(d.records) == 8              # the first flush, then the mark


def test_rejects_foreign_file():
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(b"NOPE" + bytes(12))
    buf.seek(0)
    with pytest.raises(ValueError, match="magic"):
        dump.read(buf)


def test_idle_runs_flag_counts_the_run(tmp_path):
    """With FLAG_IDLE_RUNS an IDLE record stands for its whole run."""
    path = tmp_path / "runs.bin.gz"
    w = dump.DumpWriter(str(path), tx_period_ps=10_000, flags=dump.FLAG_IDLE_RUNS)
    beats = _beats()
    # Keep only the first IDLE of each run; the runs are 3 and 2 words.
    w.write(0, IDLE_WORD, IDLE_KMASK)
    cyc = 3
    for word, k in beats[3:-2]:
        w.write(cyc, word, k)
        cyc += 1
    w.write(cyc, IDLE_WORD, IDLE_KMASK)
    w.write(cyc + 2, IDLE_WORD, IDLE_KMASK)     # the run's end, one record
    w.close()
    d = dump.read(str(path))
    assert d.header.flags == dump.FLAG_IDLE_RUNS
    s = dump.summarise(d)
    assert s["idle_words"] == 3 + 2 + 1
    assert s["packets"] == {"0x01": 1, "ioack": 1}
    assert s["deframer_errors"] == 0
