import pytest

from ingest.config import ConfigError, StorageConfig, _env, env_optional

ALL_VARS = {
    "R2_ENDPOINT": "https://acc.r2.cloudflarestorage.com",
    "R2_BUCKET": "nexrad-l3",
    "R2_ACCESS_KEY_ID": "ak",
    "R2_SECRET_ACCESS_KEY": "sk",
    "CLOUDFLARE_ACCOUNT_ID": "acc",
    "D1_DATABASE_ID": "db",
    "CLOUDFLARE_API_TOKEN": "tok",
}


def test_from_env(monkeypatch):
    for k, v in ALL_VARS.items():
        monkeypatch.setenv(k, v)
    cfg = StorageConfig.from_env()
    assert cfg.r2_bucket == "nexrad-l3"
    assert cfg.cf_api_token == "tok"


def test_env_file_gana_sobre_env(monkeypatch, tmp_path):
    secret = tmp_path / "token"
    secret.write_text("del-fichero\n")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "del-entorno")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN_FILE", str(secret))
    assert _env("CLOUDFLARE_API_TOKEN") == "del-fichero"


def test_falta_variable(monkeypatch):
    for k in ALL_VARS:
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"{k}_FILE", raising=False)
    with pytest.raises(ConfigError, match="R2_ENDPOINT"):
        StorageConfig.from_env()


def test_fichero_ilegible(monkeypatch, tmp_path):
    monkeypatch.setenv("R2_BUCKET_FILE", str(tmp_path / "no-existe"))
    with pytest.raises(ConfigError, match="R2_BUCKET_FILE"):
        _env("R2_BUCKET")


def test_env_optional_devuelve_none_si_falta(monkeypatch):
    monkeypatch.delenv("BETTERSTACK_SOURCE_TOKEN", raising=False)
    monkeypatch.delenv("BETTERSTACK_SOURCE_TOKEN_FILE", raising=False)
    assert env_optional("BETTERSTACK_SOURCE_TOKEN") is None


def test_env_optional_respeta_convencion_file(monkeypatch, tmp_path):
    secret = tmp_path / "token"
    secret.write_text("del-fichero\n")
    monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN_FILE", str(secret))
    assert env_optional("BETTERSTACK_SOURCE_TOKEN") == "del-fichero"


def test_env_optional_lanza_si_file_ilegible(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN_FILE", str(tmp_path / "no-existe"))
    with pytest.raises(ConfigError, match="BETTERSTACK_SOURCE_TOKEN_FILE"):
        env_optional("BETTERSTACK_SOURCE_TOKEN")


def test_storage_config_repr_no_filtra_secretos(monkeypatch):
    for k, v in ALL_VARS.items():
        monkeypatch.setenv(k, v)
    cfg = StorageConfig.from_env()
    r = repr(cfg)
    assert cfg.r2_access_key_id not in r
    assert cfg.r2_secret_access_key not in r
    assert cfg.cf_api_token not in r
    assert cfg.r2_bucket in r
