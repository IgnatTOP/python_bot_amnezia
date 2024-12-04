#!/bin/bash

set -e

if [ "$#" -ne 4 ]; then
    echo "Usage: $0 CLIENT_NAME CLIENT_PUBLIC_KEY WG_CONFIG_FILE DOCKER_CONTAINER"
    exit 1
fi

CLIENT_NAME="$1"
CLIENT_PUBLIC_KEY="$2"
WG_CONFIG_FILE="$3"
DOCKER_CONTAINER="$4"

pwd=$(pwd)
mkdir -p "$pwd/files"
SERVER_CONF_PATH="$pwd/files/server.conf"

docker exec -i "$DOCKER_CONTAINER" cat "$WG_CONFIG_FILE" > "$SERVER_CONF_PATH"

awk -v pubkey="$CLIENT_PUBLIC_KEY" '
BEGIN {in_peer=0; skip=0}
/^\[Peer\]/ {
    in_peer=1
    peer_block = $0 "\n"
    next
}
in_peer == 1 {
    peer_block = peer_block $0 "\n"
    if ($0 ~ /^PublicKey\s*=/) {
        split($0, a, " = ")
        if (a[2] == pubkey) {
            skip=1
        }
    }
    if ($0 ~ /^\[Peer\]/ || $0 ~ /^\[Interface\]/) {
        if (skip == 1) {
            skip=0
            in_peer=0
            next
        } else {
            print peer_block
            in_peer=0
        }
    }
    if ($0 == "") {
        if (skip == 1) {
            skip=0
            in_peer=0
            next
        } else {
            print peer_block
            in_peer=0
        }
    }
    next
}
{
    print
}
END {
    if (in_peer == 1 && skip == 1) {
    } else if (in_peer ==1 ) {
        print peer_block
    }
}
' "$SERVER_CONF_PATH" > "$SERVER_CONF_PATH.tmp"

mv "$SERVER_CONF_PATH.tmp" "$SERVER_CONF_PATH"

docker exec -i "$DOCKER_CONTAINER" wg-quick strip "$WG_CONFIG_FILE" > /dev/null

docker cp "$SERVER_CONF_PATH" "$DOCKER_CONTAINER":"$WG_CONFIG_FILE"

docker exec -i "$DOCKER_CONTAINER" sh -c "wg-quick down '$WG_CONFIG_FILE' && wg-quick up '$WG_CONFIG_FILE'"

rm -f "users/$CLIENT_NAME/$CLIENT_NAME.conf"
rmdir "users/$CLIENT_NAME" 2>/dev/null || true

CLIENTS_TABLE_PATH="$pwd/files/clientsTable"
docker exec -i "$DOCKER_CONTAINER" cat /opt/amnezia/awg/clientsTable > "$CLIENTS_TABLE_PATH" || echo "[]" > "$CLIENTS_TABLE_PATH"

if [ -f "$CLIENTS_TABLE_PATH" ]; then
    jq --arg clientId "$CLIENT_PUBLIC_KEY" 'del(.[] | select(.clientId == $clientId))' "$CLIENTS_TABLE_PATH" > "$CLIENTS_TABLE_PATH.tmp"
    mv "$CLIENTS_TABLE_PATH.tmp" "$CLIENTS_TABLE_PATH"
    docker cp "$CLIENTS_TABLE_PATH" "$DOCKER_CONTAINER":/opt/amnezia/awg/clientsTable
fi

traffic_file="$pwd/users/$CLIENT_NAME/traffic.json"
rm -f "$traffic_file"

echo "Client $CLIENT_NAME успешно удален из WireGuard"
