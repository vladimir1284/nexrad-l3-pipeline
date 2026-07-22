"""Configuración central de logging: consola + envío opcional a BetterStack.

Chokepoint único (llamado desde `ingest.cli.main`): todos los subcomandos
comparten esta config porque sus loggers (`"l3proc"`, `"ingest.wind"`,
`"ingest.lightning"`, ...) son hijos del logger raíz.

Redacción: filtro adjunto a cada *handler* (no al logger raíz — `Logger.filter()`
solo se invoca en el logger donde se hizo la llamada de logging, no en sus
ancestros durante la propagación; `Handler.filter()` sí corre para todo
record que llega a ese handler sin importar el logger de origen, que es lo
que necesitamos ya que todo el código loguea vía `"l3proc"`/`"ingest.*"`,
nunca el root directamente). Sustituye cualquier valor de secreto conocido
(R2/CF/BetterStack) por `***REDACTED***` en el mensaje y en el traceback,
antes de que el handler los emita — cubre tanto la consola como
`LogtailHandler` (su `emit()` llama a `self.format(record)`, que usa
`record.getMessage()`/`record.exc_text`). Es sustitución literal por valor:
no cubre secretos transformados (base64, URL firmada, etc.) ni datos
pasados vía `extra={...}` en llamadas de logging (ninguna lo hace hoy).
"""

import logging
import os

from ingest.config import env_optional

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
REDACTED = "***REDACTED***"

_SECRET_ENV_NAMES = (
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "CLOUDFLARE_API_TOKEN",
    "BETTERSTACK_SOURCE_TOKEN",
)


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        message = record.getMessage()
        redacted_message = self._redact(message)
        if redacted_message != message:
            record.msg = redacted_message
            record.args = ()

        if record.exc_info:
            exc_text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._redact(exc_text)
            record.exc_info = None

        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)

        return True

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text


def configure_logging() -> None:
    """Idempotente: cada llamada reemplaza los handlers del root logger
    (como `logging.basicConfig(force=True)`) en vez de acumularlos — llamarla
    más de una vez en el mismo proceso (tests, REPL) no debe ir dejando
    StreamHandlers/LogtailHandlers huérfanos atrás."""
    level_name = os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    secrets = [v for v in (env_optional(name) for name in _SECRET_ENV_NAMES) if v]
    redactor = SecretRedactingFilter(secrets)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    stream_handler.addFilter(redactor)
    root.addHandler(stream_handler)

    token = env_optional("BETTERSTACK_SOURCE_TOKEN")
    if not token:
        return
    host = env_optional("BETTERSTACK_HOST")
    if not host:
        logging.getLogger(__name__).warning(
            "BETTERSTACK_SOURCE_TOKEN seteado pero falta BETTERSTACK_HOST — "
            "no se envían logs a BetterStack"
        )
        return

    from logtail import LogtailHandler  # import diferido: solo se paga si hay token

    logtail_handler = LogtailHandler(source_token=token, host=host)
    logtail_handler.addFilter(redactor)
    root.addHandler(logtail_handler)
