#!/bin/bash

# Включаем строгий режим
set -euo pipefail
IFS=$'\n\t'

# Функция для логирования
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Функция очистки при ошибке
cleanup() {
    if [ $? -ne 0 ]; then
        log "Error occurred. Cleaning up..."
        [ -d "$pwd/users/$CLIENT_NAME" ] && rm -rf "$pwd/users/$CLIENT_NAME"
        [ -f "$SERVER_CONF_PATH.bak" ] && mv "$SERVER_CONF_PATH.bak" "$SERVER_CONF_PATH"
    fi
}

trap cleanup EXIT

# Проверка аргументов
if [ $# -ne 4 ]; then
    log "Error: Required arguments not provided"
    log "Usage: $0 CLIENT_NAME ENDPOINT WG_CONFIG_FILE DOCKER_CONTAINER"
    exit 1
fi

CLIENT_NAME="$1"
ENDPOINT="$2"
WG_CONFIG_FILE="$3"
DOCKER_CONTAINER="$4"

# Проверка имени клиента
if [[ ! "$CLIENT_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    log "Error: Invalid CLIENT_NAME. Only letters, numbers, underscores, and hyphens are allowed."
    exit 1
fi

# Проверка docker контейнера
if ! docker ps | grep -q "$DOCKER_CONTAINER"; then
    log "Error: Docker container $DOCKER_CONTAINER is not running"
    exit 1
fi

pwd=$(pwd)
mkdir -p "$pwd/users/$CLIENT_NAME"
mkdir -p "$pwd/files"

# Генерация ключей
log "Generating keys for $CLIENT_NAME..."
key=$(docker exec -i $DOCKER_CONTAINER wg genkey)
if [ -z "$key" ]; then
    log "Error: Failed to generate private key"
    exit 1
fi

psk=$(docker exec -i $DOCKER_CONTAINER wg genpsk)
if [ -z "$psk" ]; then
    log "Error: Failed to generate preshared key"
    exit 1
fi

SERVER_CONF_PATH="$pwd/files/server.conf"
cp "$SERVER_CONF_PATH" "$SERVER_CONF_PATH.bak" 2>/dev/null || true
docker exec -i $DOCKER_CONTAINER cat $WG_CONFIG_FILE > "$SERVER_CONF_PATH"

# Получение серверных параметров
SERVER_PRIVATE_KEY=$(awk '/^PrivateKey\s*=/ {print $3}' "$SERVER_CONF_PATH")
if [ -z "$SERVER_PRIVATE_KEY" ]; then
    log "Error: Failed to get server private key"
    exit 1
fi

SERVER_PUBLIC_KEY=$(echo "$SERVER_PRIVATE_KEY" | docker exec -i $DOCKER_CONTAINER wg pubkey)
LISTEN_PORT=$(awk '/ListenPort\s*=/ {print $3}' "$SERVER_CONF_PATH")
ADDITIONAL_PARAMS=$(awk '/^Jc\s*=|^Jmin\s*=|^Jmax\s*=|^S1\s*=|^S2\s*=|^H[1-4]\s*=/' "$SERVER_CONF_PATH")

# Поиск свободного IP
octet=2
while grep -E "AllowedIPs\s*=\s*10\.8\.1\.$octet/32" "$SERVER_CONF_PATH" > /dev/null; do
    (( octet++ ))
done

if [ "$octet" -gt 254 ]; then
    log "Error: WireGuard internal subnet 10.8.1.0/24 is full"
    exit 1
fi

CLIENT_IP="10.8.1.$octet/32"
ALLOWED_IPS="$CLIENT_IP"

CLIENT_PUBLIC_KEY=$(echo "$key" | docker exec -i $DOCKER_CONTAINER wg pubkey)
if [ -z "$CLIENT_PUBLIC_KEY" ]; then
    log "Error: Failed to generate client public key"
    exit 1
fi

# Добавление пира в конфигурацию сервера
log "Adding peer configuration..."
cat << EOF >> "$SERVER_CONF_PATH"
[Peer]
# $CLIENT_NAME
PublicKey = $CLIENT_PUBLIC_KEY
PresharedKey = $psk
AllowedIPs = $ALLOWED_IPS

EOF

# Обновление конфигурации WireGuard
log "Updating WireGuard configuration..."
if ! docker cp "$SERVER_CONF_PATH" $DOCKER_CONTAINER:$WG_CONFIG_FILE; then
    log "Error: Failed to copy configuration to container"
    exit 1
fi

if ! docker exec -i $DOCKER_CONTAINER sh -c "wg-quick down $WG_CONFIG_FILE && wg-quick up $WG_CONFIG_FILE"; then
    log "Error: Failed to restart WireGuard"
    exit 1
fi

# Создание клиентской конфигурации
log "Creating client configuration..."
cat << EOF > "$pwd/users/$CLIENT_NAME/$CLIENT_NAME.conf"
[Interface]
Address = $CLIENT_IP
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = $key
$ADDITIONAL_PARAMS

[Peer]
PublicKey = $SERVER_PUBLIC_KEY
PresharedKey = $psk
AllowedIPs = 0.0.0.0/0
Endpoint = $ENDPOINT:$LISTEN_PORT
PersistentKeepalive = 25
EOF

# Обновление таблицы клиентов
CLIENTS_TABLE_PATH="$pwd/files/clientsTable"
docker exec -i $DOCKER_CONTAINER cat /opt/amnezia/awg/clientsTable > "$CLIENTS_TABLE_PATH" 2>/dev/null || echo "[]" > "$CLIENTS_TABLE_PATH"

CREATION_DATE=$(date)
if [ -f "$CLIENTS_TABLE_PATH" ]; then
    if ! command -v jq &> /dev/null; then
        log "Error: jq is required but not installed"
        exit 1
    fi
    
    jq --arg clientId "$CLIENT_PUBLIC_KEY" \
       --arg clientName "$CLIENT_NAME" \
       --arg creationDate "$CREATION_DATE" \
       '. += [{"clientId": $clientId, "userData": {"clientName": $clientName, "creationDate": $creationDate}}]' \
       "$CLIENTS_TABLE_PATH" > "$CLIENTS_TABLE_PATH.tmp" && \
    mv "$CLIENTS_TABLE_PATH.tmp" "$CLIENTS_TABLE_PATH"
else
    jq -n --arg clientId "$CLIENT_PUBLIC_KEY" \
          --arg clientName "$CLIENT_NAME" \
          --arg creationDate "$CREATION_DATE" \
          '[{"clientId": $clientId, "userData": {"clientName": $clientName, "creationDate": $creationDate}}]' \
          > "$CLIENTS_TABLE_PATH"
fi

if ! docker cp "$CLIENTS_TABLE_PATH" $DOCKER_CONTAINER:/opt/amnezia/awg/clientsTable; then
    log "Error: Failed to copy clients table to container"
    exit 1
fi

# Инициализация файла трафика
traffic_file="$pwd/users/$CLIENT_NAME/traffic.json"
echo '{
    "total_incoming": 0,
    "total_outgoing": 0,
    "last_incoming": 0,
    "last_outgoing": 0
}' > "$traffic_file"

log "Client $CLIENT_NAME successfully added to WireGuard"
