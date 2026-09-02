"""Auth de un solo scope: bearer token de ops.

Sin roles ni tabla de usuarios — el único consumidor es workers/ops, así
que un token estático (Docker secret en el Swarm, `wrangler secret` del
lado del Worker) alcanza. Mismo patrón conceptual que el
CLOUDFLARE_API_TOKEN que ingest/ ya usaba contra D1.
"""

import os
import secrets

from fastapi import Header, HTTPException


def _expected_token() -> str:
    # Convención _FILE (Docker secrets) gana sobre el env var plano,
    # igual que ingest/config.py._env.
    token_file = os.environ.get("NEXRAD_API_TOKEN_OPS_FILE")
    if token_file:
        return open(token_file).read().strip()
    token = os.environ.get("NEXRAD_API_TOKEN_OPS")
    if token:
        return token
    raise RuntimeError("falta NEXRAD_API_TOKEN_OPS (o _FILE) en el entorno")


def require_ops_token(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not secrets.compare_digest(token, _expected_token()):
        raise HTTPException(status_code=401, detail="token inválido")
