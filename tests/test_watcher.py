import shutil
import threading
import time
from pathlib import Path

import pytest

from ingest.storage.publish import PublishResult
from ingest.watcher import ProductProcessor, run_watcher

SAMPLE = Path(__file__).parent / "data" / "AMX_N0B_2026_07_06_15_45_17"


@pytest.fixture
def input_dir(tmp_path):
    d = tmp_path / "incoming"
    d.mkdir()
    return d


def test_once_sin_publish_conserva_cog_y_borra_crudo(input_dir, tmp_path):
    shutil.copy(SAMPLE, input_dir / SAMPLE.name)
    cogs = tmp_path / "cogs"
    heartbeat = input_dir / ".heartbeat"

    stats = run_watcher(
        input_dir,
        ProductProcessor(output_dir=cogs),
        heartbeat=heartbeat,
        once=True,
    )

    assert stats.processed == 1
    assert stats.failed == 0
    assert not (input_dir / SAMPLE.name).exists()
    assert (cogs / "AMX_N0B_20260706_154517.tif").exists()
    assert heartbeat.exists()


def test_once_con_publisher_no_deja_cog_local(input_dir):
    shutil.copy(SAMPLE, input_dir / SAMPLE.name)
    calls = []

    def publisher(cog_path, prod, grid):
        assert cog_path.exists()
        calls.append((cog_path.name, prod.site_id))
        return PublishResult(r2_key="k", size_bytes=cog_path.stat().st_size)

    stats = run_watcher(input_dir, ProductProcessor(publisher=publisher), once=True)

    assert stats.processed == 1
    assert calls == [("AMX_N0B_20260706_154517.tif", "AMX")]
    # crudo borrado y ningún .tif suelto (el COG fue efímero)
    assert list(input_dir.iterdir()) == []


def test_fichero_corrupto_va_a_failed(input_dir, tmp_path):
    bad = input_dir / "AMX_N0B_garbage"
    bad.write_bytes(b"esto no es un producto level 3")
    shutil.copy(SAMPLE, input_dir / SAMPLE.name)

    stats = run_watcher(input_dir, ProductProcessor(output_dir=tmp_path / "cogs"), once=True)

    assert stats.processed == 1
    assert stats.failed == 1
    assert stats.failed_files == ["AMX_N0B_garbage"]
    assert (input_dir / "failed" / "AMX_N0B_garbage").exists()
    assert not bad.exists()


def test_watcher_vivo_procesa_escritura_atomica(input_dir, tmp_path):
    """Simula al injector: tmp oculto + rename dentro del directorio."""
    stop = threading.Event()
    stats_box = {}

    def run():
        stats_box["stats"] = run_watcher(
            input_dir, ProductProcessor(output_dir=tmp_path / "cogs"), stop=stop
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.5)  # observer arrancando

    tmp = input_dir / ".AMX_N0B.tmp"
    shutil.copy(SAMPLE, tmp)
    tmp.rename(input_dir / SAMPLE.name)

    deadline = time.monotonic() + 30
    while (input_dir / SAMPLE.name).exists() and time.monotonic() < deadline:
        time.sleep(0.2)

    stop.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert stats_box["stats"].processed == 1
    assert (tmp_path / "cogs" / "AMX_N0B_20260706_154517.tif").exists()


def test_processor_sin_publisher_ni_output_dir_falla():
    with pytest.raises(ValueError):
        ProductProcessor()
