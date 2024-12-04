#!/bin/bash

set -e

if [ -z "$1" ]; then
    echo "Error: CLIENT_NAME argument is not provided"
    exit 1
fi

if [ -z "$2" ]; then
    echo "Error: ENDPOINT argument is not provided"
    exit 1
fi

if [ -z "$3" ]; then
    echo "Error: WG_CONFIG_FILE argument is not provided"
    exit 1
fi

if [ -z "$4" ]; then
    echo "Error: DOCKER_CONTAINER argument is not provided"
    exit 1
fi

CLIENT_NAME="$1"
ENDPOINT="$2"
WG_CONFIG_FILE="$3"
DOCKER_CONTAINER="$4"

if [[ ! "$CLIENT_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Error: Invalid CLIENT_NAME. Only letters, numbers, underscores, and hyphens are allowed."
    exit 1
fi

pwd=$(pwd)
mkdir -p "$pwd/users/$CLIENT_NAME"
mkdir -p "$pwd/files"

key=$(docker exec -i $DOCKER_CONTAINER wg genkey)
psk=$(docker exec -i $DOCKER_CONTAINER wg genpsk)

SERVER_CONF_PATH="$pwd/files/server.conf"
docker exec -i $DOCKER_CONTAINER cat $WG_CONFIG_FILE > "$SERVER_CONF_PATH"

SERVER_PRIVATE_KEY=$(awk '/^PrivateKey\s*=/ {print $3}' "$SERVER_CONF_PATH")
SERVER_PUBLIC_KEY=$(echo "$SERVER_PRIVATE_KEY" | docker exec -i $DOCKER_CONTAINER wg pubkey)
LISTEN_PORT=$(awk '/ListenPort\s*=/ {print $3}' "$SERVER_CONF_PATH")
ADDITIONAL_PARAMS=$(awk '/^Jc\s*=|^Jmin\s*=|^Jmax\s*=|^S1\s*=|^S2\s*=|^H[1-4]\s*=/' "$SERVER_CONF_PATH")

octet=2
while grep -E "AllowedIPs\s*=\s*10\.8\.1\.$octet/32" "$SERVER_CONF_PATH" > /dev/null; do
    (( octet++ ))
done

if [ "$octet" -gt 254 ]; then
    echo "Error: WireGuard internal subnet 10.8.1.0/24 is full"
    exit 1
fi

CLIENT_IP="10.8.1.$octet/32"
ALLOWED_IPS="$CLIENT_IP"

CLIENT_PUBLIC_KEY=$(echo "$key" | docker exec -i $DOCKER_CONTAINER wg pubkey)

cat << EOF >> "$SERVER_CONF_PATH"
[Peer]
# $CLIENT_NAME
PublicKey = $CLIENT_PUBLIC_KEY
PresharedKey = $psk
AllowedIPs = $ALLOWED_IPS

EOF

docker cp "$SERVER_CONF_PATH" $DOCKER_CONTAINER:$WG_CONFIG_FILE

docker exec -i $DOCKER_CONTAINER sh -c "wg-quick down $WG_CONFIG_FILE && wg-quick up $WG_CONFIG_FILE"

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

CLIENTS_TABLE_PATH="$pwd/files/clientsTable"
docker exec -i $DOCKER_CONTAINER cat /opt/amnezia/awg/clientsTable > "$CLIENTS_TABLE_PATH" || echo "[]" > "$CLIENTS_TABLE_PATH"

CREATION_DATE=$(date)
if [ -f "$CLIENTS_TABLE_PATH" ]; then
    jq --arg clientId "$CLIENT_PUBLIC_KEY" \
       --arg clientName "$CLIENT_NAME" \
       --arg creationDate "$CREATION_DATE" \
       '. += [{"clientId": $clientId, "userData": {"clientName": $clientName, "creationDate": $creationDate}}]' \
       "$CLIENTS_TABLE_PATH" > "$CLIENTS_TABLE_PATH.tmp"
    mv "$CLIENTS_TABLE_PATH.tmp" "$CLIENTS_TABLE_PATH"
else
    jq -n --arg clientId "$CLIENT_PUBLIC_KEY" \
          --arg clientName "$CLIENT_NAME" \
          --arg creationDate "$CREATION_DATE" \
          '[{"clientId": $clientId, "userData": {"clientName": $clientName, "creationDate": $creationDate}}]' \
          > "$CLIENTS_TABLE_PATH"
fi

docker cp "$CLIENTS_TABLE_PATH" $DOCKER_CONTAINER:/opt/amnezia/awg/clientsTable

traffic_file="$pwd/users/$CLIENT_NAME/traffic.json"
echo '{
    "total_incoming": 0,
    "total_outgoing": 0,
    "last_incoming": 0,
    "last_outgoing": 0
}' > "$traffic_file"

echo "Client $CLIENT_NAME successfully added to WireGuard"
