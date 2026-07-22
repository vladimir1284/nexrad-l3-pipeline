import logging

import pytest

from ingest import logging_setup

BETTERSTACK_VARS = (
    "BETTERSTACK_SOURCE_TOKEN",
    "BETTERSTACK_SOURCE_TOKEN_FILE",
    "BETTERSTACK_HOST",
    "BETTERSTACK_HOST_FILE",
)
SECRET_VARS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "CLOUDFLARE_API_TOKEN")


class FakeLogtailHandler(logging.Handler):
    instances = []

    def __init__(self, source_token, host):
        super().__init__()
        self.source_token = source_token
        self.host = host
        FakeLogtailHandler.instances.append(self)

    def emit(self, record):
        pass


@pytest.fixture(autouse=True)
def _isolate_root_logger(monkeypatch):
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    for name in (*BETTERSTACK_VARS, *SECRET_VARS):
        monkeypatch.delenv(name, raising=False)
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    FakeLogtailHandler.instances.clear()


def _remote_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h, FakeLogtailHandler)]


def test_sin_token_no_adjunta_handler_remoto():
    logging_setup.configure_logging()
    assert _remote_handlers() == []
    assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)


def test_token_y_host_adjuntan_handler_remoto(monkeypatch):
    monkeypatch.setattr("logtail.LogtailHandler", FakeLogtailHandler)
    monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN", "tok-123")
    monkeypatch.setenv("BETTERSTACK_HOST", "example.betterstackdata.com")
    logging_setup.configure_logging()
    handlers = _remote_handlers()
    assert len(handlers) == 1
    assert handlers[0].source_token == "tok-123"
    assert handlers[0].host == "example.betterstackdata.com"


def test_token_sin_host_no_crashea_y_avisa(monkeypatch, capsys):
    # capsys, no caplog: configure_logging() reemplaza los handlers del root
    # (incluido el de caplog) para ser idempotente — ver docstring del módulo.
    monkeypatch.setattr("logtail.LogtailHandler", FakeLogtailHandler)
    monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN", "tok-123")
    logging_setup.configure_logging()
    assert _remote_handlers() == []
    assert "BETTERSTACK_HOST" in capsys.readouterr().err


def test_redaccion_de_mensaje(monkeypatch, capsys):
    # capsys (no caplog): caplog adjunta su propio handler al root ANTES de que
    # configure_logging() adjunte los nuestros, y el orden de la lista de
    # handlers determina quién ve el record antes de que nuestro filtro lo
    # mute — en producción configure_logging() es lo único que toca el root,
    # así que probamos el StreamHandler real (stderr), no el de pytest.
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKIA-SUPER-SECRETO")
    logging_setup.configure_logging()
    logger = logging.getLogger("ingest.test")
    logger.info("usando clave %s para subir", "AKIA-SUPER-SECRETO")
    err = capsys.readouterr().err
    assert "AKIA-SUPER-SECRETO" not in err
    assert logging_setup.REDACTED in err


def test_redaccion_de_traceback(monkeypatch, capsys):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-abc")
    logging_setup.configure_logging()
    logger = logging.getLogger("ingest.test")
    try:
        raise RuntimeError("fallo con cf-token-abc adentro")
    except RuntimeError:
        logger.exception("fallo de red")
    err = capsys.readouterr().err
    assert "cf-token-abc" not in err
    assert logging_setup.REDACTED in err
