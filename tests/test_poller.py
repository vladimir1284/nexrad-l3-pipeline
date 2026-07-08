import json
from pathlib import Path

from ingest.poller import STATE_FILE, PollConfig, poll_cycle
from tests.test_replay import NOW, FakeS3, _key


def _cfg(tmp_path: Path, **kw) -> PollConfig:
    d = tmp_path / "incoming"
    d.mkdir(exist_ok=True)
    return PollConfig(input_dir=d, sites=["AMX"], mnemonics=["N0B"], **kw)


def test_primer_ciclo_baja_hasta_catchup(tmp_path):
    objects = {_key("2026_07_06", f"1{h}_00_00"): b"x" for h in range(8)}
    cfg = _cfg(tmp_path, catchup=3)
    state = {}

    n = poll_cycle(cfg, state, s3=FakeS3(objects), now=NOW)

    assert n == 3
    names = sorted(p.name for p in cfg.input_dir.iterdir())
    assert names == sorted(objects)[-3:]
    assert state["AMX_N0B"] == sorted(objects)[-1]


def test_ciclo_sin_novedades_no_baja_nada(tmp_path):
    objects = {_key("2026_07_06", "10_00_00"): b"x"}
    cfg = _cfg(tmp_path)
    state = {"AMX_N0B": _key("2026_07_06", "10_00_00")}

    assert poll_cycle(cfg, state, s3=FakeS3(objects), now=NOW) == 0
    assert list(cfg.input_dir.iterdir()) == []


def test_solo_baja_lo_posterior_al_watermark(tmp_path):
    objects = {_key("2026_07_06", t): b"x" for t in ("10_00_00", "11_00_00", "12_00_00")}
    cfg = _cfg(tmp_path)
    state = {"AMX_N0B": _key("2026_07_06", "10_00_00")}

    n = poll_cycle(cfg, state, s3=FakeS3(objects), now=NOW)

    assert n == 2
    names = sorted(p.name for p in cfg.input_dir.iterdir())
    assert names == [_key("2026_07_06", "11_00_00"), _key("2026_07_06", "12_00_00")]


def test_fallo_en_un_par_no_tumba_el_ciclo(tmp_path):
    class BrokenS3(FakeS3):
        def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
            if Prefix.startswith("AMX"):
                raise RuntimeError("boom")
            return super().list_objects_v2(Bucket, Prefix, MaxKeys, ContinuationToken)

    objects = {"JUA_N0B_2026_07_06_10_00_00": b"x"}
    d = tmp_path / "incoming"
    d.mkdir()
    cfg = PollConfig(input_dir=d, sites=["AMX", "JUA"], mnemonics=["N0B"])
    state = {}

    n = poll_cycle(cfg, state, s3=BrokenS3(objects), now=NOW)

    assert n == 1
    assert "JUA_N0B" in state and "AMX_N0B" not in state


def test_estado_json_roundtrip(tmp_path):
    from ingest.poller import _load_state, _save_state

    path = tmp_path / STATE_FILE
    _save_state(path, {"AMX_N0B": "k1"})
    assert _load_state(path) == {"AMX_N0B": "k1"}
    assert json.loads(path.read_text()) == {"AMX_N0B": "k1"}
    # corrupto o ausente → estado vacío, no excepción
    path.write_text("{basura")
    assert _load_state(path) == {}
    assert _load_state(tmp_path / "no-existe") == {}
