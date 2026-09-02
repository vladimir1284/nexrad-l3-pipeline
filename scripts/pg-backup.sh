#!/bin/sh
# Backup nocturno de Postgres a R2: pg_dump -Fc + subida vía la API S3 de
# R2 (aws-cli), retención 7 diarios + 4 semanales. Proporcional a escala
# demo: sin WAL/PITR — un dump lógico diario alcanza porque ingest es
# idempotente (reprocesar re-puebla vía UPSERT).
set -eu

PG_PASSWORD=$(cat "${PG_PASSWORD_FILE:?}")
R2_ACCESS_KEY_ID=$(cat "${R2_ACCESS_KEY_ID_FILE:?}")
R2_SECRET_ACCESS_KEY=$(cat "${R2_SECRET_ACCESS_KEY_FILE:?}")
export PGPASSWORD="$PG_PASSWORD"
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"

# Se instala una vez por vida del contenedor (el loop de abajo corre en
# el mismo proceso indefinidamente) — no en cada ciclo de backup.
apk add --no-cache aws-cli >/dev/null 2>&1 || true

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DOW=$(date -u +%u) # 1=lunes .. 7=domingo
DUMP="/tmp/nexrad-l3_${STAMP}.dump"
KEY_DAILY="pg-backups/daily/nexrad-l3_${STAMP}.dump"
KEY_WEEKLY="pg-backups/weekly/nexrad-l3_${STAMP}.dump"

backup_once() {
    pg_dump -Fc -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -f "$DUMP"
    aws --endpoint-url "$R2_ENDPOINT" s3 cp "$DUMP" "s3://${R2_BUCKET}/${KEY_DAILY}"
    if [ "$DOW" = "7" ]; then
        aws --endpoint-url "$R2_ENDPOINT" s3 cp "$DUMP" "s3://${R2_BUCKET}/${KEY_WEEKLY}"
    fi
    rm -f "$DUMP"

    # Retención: 7 diarios + 4 semanales — borra lo más viejo que exceda.
    for prefix_count in "pg-backups/daily/ 7" "pg-backups/weekly/ 4"; do
        set -- $prefix_count
        prefix=$1
        keep=$2
        aws --endpoint-url "$R2_ENDPOINT" s3api list-objects-v2 \
            --bucket "$R2_BUCKET" --prefix "$prefix" \
            --query 'sort_by(Contents, &LastModified)[].Key' --output text \
            | tr '\t' '\n' | head -n -"$keep" \
            | while read -r old_key; do
                [ -n "$old_key" ] && aws --endpoint-url "$R2_ENDPOINT" s3 rm "s3://${R2_BUCKET}/${old_key}"
            done
    done
}

# Loop diario simple: correr una vez al arranque, luego cada 24 h.
# Rollback/observabilidad más fina (cron real, retries) queda para
# cuando haga falta — un contenedor `restart: on-failure` que reintenta
# el dump completo al día siguiente alcanza a esta escala.
while true; do
    backup_once
    sleep 86400
done
