import os
import time

from ingest.cli import main


def test_healthy(tmp_path):
    hb = tmp_path / "hb"
    hb.touch()
    assert main(["health", "--heartbeat", str(hb)]) == 0


def test_sin_heartbeat(tmp_path):
    assert main(["health", "--heartbeat", str(tmp_path / "no")]) == 1


def test_heartbeat_viejo(tmp_path):
    hb = tmp_path / "hb"
    hb.touch()
    old = time.time() - 1000
    os.utime(hb, (old, old))
    assert main(["health", "--heartbeat", str(hb), "--max-age", "300"]) == 1


def test_backlog_excedido(tmp_path):
    hb = tmp_path / "hb"
    hb.touch()
    d = tmp_path / "incoming"
    d.mkdir()
    for i in range(3):
        (d / f"f{i}").touch()
    (d / ".oculto").touch()  # los ocultos no cuentan
    args = ["health", "--heartbeat", str(hb), "--dir", str(d)]
    assert main([*args, "--max-backlog", "3"]) == 0
    assert main([*args, "--max-backlog", "2"]) == 1
