import pytest
from fastapi import HTTPException

from app.auth import require_ops_token


def test_token_correcto_no_lanza(monkeypatch):
    monkeypatch.setenv("NEXRAD_API_TOKEN_OPS", "secreto")
    require_ops_token(authorization="Bearer secreto")


def test_token_incorrecto_lanza_401(monkeypatch):
    monkeypatch.setenv("NEXRAD_API_TOKEN_OPS", "secreto")
    with pytest.raises(HTTPException) as exc:
        require_ops_token(authorization="Bearer otro")
    assert exc.value.status_code == 401


def test_sin_bearer_lanza_401(monkeypatch):
    monkeypatch.setenv("NEXRAD_API_TOKEN_OPS", "secreto")
    with pytest.raises(HTTPException) as exc:
        require_ops_token(authorization="secreto")
    assert exc.value.status_code == 401


def test_token_file_gana_sobre_env(monkeypatch, tmp_path):
    secret = tmp_path / "token"
    secret.write_text("del-fichero\n")
    monkeypatch.setenv("NEXRAD_API_TOKEN_OPS", "del-entorno")
    monkeypatch.setenv("NEXRAD_API_TOKEN_OPS_FILE", str(secret))
    with pytest.raises(HTTPException):
        require_ops_token(authorization="Bearer del-entorno")
    require_ops_token(authorization="Bearer del-fichero")
