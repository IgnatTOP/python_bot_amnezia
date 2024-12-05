import os
import subprocess
import configparser
import json
import pytz
import socket
import logging
import tempfile
from datetime import datetime

EXPIRATIONS_FILE = 'files/expirations.json'
PAYMENTS_FILE = 'files/payments.json'
UTC = pytz.UTC

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_amnezia_container():
    cmd = "docker ps --filter 'name=amnezia-awg' --format '{{.Names}}'"
    try:
        output = subprocess.check_output(cmd, shell=True).decode().strip()
        if output:
            return output
        else:
            logger.error("Docker-контейнер 'amnezia-awg' не найден или не запущен.")
            exit(1)
    except subprocess.CalledProcessError:
        logger.error("Не удалось выполнить Docker-команду для поиска контейнера 'amnezia-awg'.")
        exit(1)

def create_config(path='files/setting.ini'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    config = configparser.ConfigParser()
    config.add_section("setting")

    bot_token = input('Введите токен Telegram бота: ').strip()
    admin_id = input('Введите Telegram ID администратора: ').strip()

    docker_container = get_amnezia_container()
    logger.info(f"Найден Docker-контейнер: {docker_container}")

    cmd = f"docker exec {docker_container} find / -name wg0.conf"
    try:
        wg_config_file = subprocess.check_output(cmd, shell=True).decode().strip()
        if not wg_config_file:
            logger.warning("Не удалось найти файл конфигурации WireGuard 'wg0.conf' в контейнере. Используется путь по умолчанию.")
            wg_config_file = '/opt/amnezia/awg/wg0.conf'
    except subprocess.CalledProcessError:
        logger.warning("Ошибка при определении пути к файлу конфигурации WireGuard. Используется путь по умолчанию.")
        wg_config_file = '/opt/amnezia/awg/wg0.conf'

    try:
        endpoint = subprocess.check_output("curl -s https://api.ipify.org", shell=True).decode().strip()
        socket.inet_aton(endpoint)
    except (subprocess.CalledProcessError, socket.error):
        logger.error("Ошибка при определении внешнего IP-адреса сервера.")
        endpoint = input('Не удалось автоматически определить внешний IP-адрес. Пожалуйста, введите его вручную: ').strip()

    config.set("setting", "bot_token", bot_token)
    config.set("setting", "admin_id", admin_id)
    config.set("setting", "docker_container", docker_container)
    config.set("setting", "wg_config_file", wg_config_file)
    config.set("setting", "endpoint", endpoint)

    with open(path, "w") as config_file:
        config.write(config_file)
    logger.info(f"Конфигурация сохранена в {path}")

def ensure_peer_names():
    setting = get_config()
    wg_config_file = setting['wg_config_file']
    docker_container = setting['docker_container']

    clientsTable = get_full_clients_table()
    clients_dict = {client['clientId']: client['userData'] for client in clientsTable}

    try:
        cmd = f"docker exec -i {docker_container} cat {wg_config_file}"
        config_content = subprocess.check_output(cmd, shell=True).decode('utf-8')

        lines = config_content.splitlines()
        new_config_lines = []
        i = 0
        modified = False
        updated_clientsTable = False

        while i < len(lines):
            line = lines[i]
            if line.strip().startswith('[Peer]'):
                peer_block = [line]
                i += 1
                has_name_comment = False
                client_public_key = ''
                while i < len(lines) and lines[i].strip() != '':
                    peer_line = lines[i]
                    if peer_line.strip().startswith('#'):
                        has_name_comment = True
                    elif peer_line.strip().startswith('PublicKey ='):
                        client_public_key = peer_line.strip().split('=', 1)[1].strip()
                    peer_block.append(peer_line)
                    i += 1
                if not has_name_comment:
                    if client_public_key in clients_dict:
                        client_name = clients_dict[client_public_key].get('clientName', f"client_{client_public_key[:6]}")
                    else:
                        client_name = f"client_{client_public_key[:6]}"
                        clients_dict[client_public_key] = {
                            'clientName': client_name,
                            'creationDate': datetime.now().isoformat()
                        }
                        updated_clientsTable = True
                    peer_block.insert(1, f'# {client_name}')
                    modified = True
                new_config_lines.extend(peer_block)
                if i < len(lines):
                    new_config_lines.append(lines[i])
                    i += 1
            else:
                new_config_lines.append(line)
                i += 1

        if modified:
            new_config_content = '\n'.join(new_config_lines)
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_config:
                temp_config.write(new_config_content)
                temp_config_path = temp_config.name
            docker_cmd = f"docker cp {temp_config_path} {docker_container}:{wg_config_file}"
            subprocess.check_call(docker_cmd, shell=True)
            os.remove(temp_config_path)
            logger.info("Конфигурационный файл WireGuard обновлён с добавлением комментариев # name_client.")

        if updated_clientsTable:
            clientsTable_list = [{'clientId': key, 'userData': value} for key, value in clients_dict.items()]
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_clientsTable:
                json.dump(clientsTable_list, temp_clientsTable)
                temp_clientsTable_path = temp_clientsTable.name
            docker_cmd = f"docker cp {temp_clientsTable_path} {docker_container}:/opt/amnezia/awg/clientsTable"
            subprocess.check_call(docker_cmd, shell=True)
            os.remove(temp_clientsTable_path)
            logger.info("clientsTable обновлён с новыми клиентами.")
    except Exception as e:
        logger.error(f"Ошибка при обновлении комментариев в конфигурации WireGuard: {e}")

def get_config(path='files/setting.ini'):
    if not os.path.exists(path):
        create_config(path)

    config = configparser.ConfigParser()
    config.read(path)
    out = {}
    for key in config['setting']:
        out[key] = config['setting'][key]

    return out

def save_client_endpoint(username, endpoint):
    os.makedirs('files/connections', exist_ok=True)
    file_path = os.path.join('files', 'connections', f'{username}_ip.json')
    timestamp = datetime.now().strftime('%d.%m.%Y %H:%M')
    ip_address = endpoint.split(':')[0]

    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
    else:
        data = {}

    data[ip_address] = timestamp

    with open(file_path, 'w') as f:
        json.dump(data, f)

def root_add(id_user, ipv6=False):
    setting = get_config()
    endpoint = setting['endpoint']
    wg_config_file = setting['wg_config_file']
    docker_container = setting['docker_container']

    clients = get_client_list()
    client_entry = next((c for c in clients if c[0] == id_user), None)
    if client_entry:
        logger.info(f"Пользователь {id_user} уже существует. Генерация конфигурации невозможна без приватного ключа.")
        return False
    else:
        cmd = ["./newclient.sh", id_user, endpoint, wg_config_file, docker_container]
        if subprocess.call(cmd) == 0:
            return True
        return False

def get_clients_from_clients_table():
    setting = get_config()
    docker_container = setting['docker_container']
    clients_table_path = '/opt/amnezia/awg/clientsTable'
    try:
        cmd = f"docker exec -i {docker_container} cat {clients_table_path}"
        call = subprocess.check_output(cmd, shell=True)
        clients_table = json.loads(call.decode('utf-8'))
        client_map = {client['clientId']: client['userData']['clientName'] for client in clients_table}
        return client_map
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка при получении clientsTable: {e}")
        return {}
    except json.JSONDecodeError:
        logger.error("Ошибка при разборе clientsTable JSON.")
        return {}

def parse_client_name(full_name):
    return full_name.split('[')[0].strip()

def get_client_list():
    setting = get_config()
    wg_config_file = setting['wg_config_file']
    docker_container = setting['docker_container']

    client_map = get_clients_from_clients_table()

    try:
        cmd = f"docker exec -i {docker_container} cat {wg_config_file}"
        call = subprocess.check_output(cmd, shell=True)
        config_content = call.decode('utf-8')

        clients = []
        lines = config_content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('[Peer]'):
                client_public_key = ''
                allowed_ips = ''
                client_name = 'Unknown'
                i += 1
                while i < len(lines):
                    peer_line = lines[i].strip()
                    if peer_line == '':
                        break
                    if peer_line.startswith('#'):
                        full_client_name = peer_line[1:].strip()
                        client_name = parse_client_name(full_client_name)
                    elif peer_line.startswith('PublicKey ='):
                        client_public_key = peer_line.split('=', 1)[1].strip()
                    elif peer_line.startswith('AllowedIPs ='):
                        allowed_ips = peer_line.split('=', 1)[1].strip()
                    i += 1
                client_name = client_map.get(client_public_key, client_name if 'client_name' in locals() else 'Unknown')
                clients.append([client_name, client_public_key, allowed_ips])
            else:
                i += 1
        return clients
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка при получении списка клиентов: {e}")
        return []

def get_active_list():
    setting = get_config()
    docker_container = setting['docker_container']

    client_map = get_clients_from_clients_table()

    try:
        clients = get_client_list()
        client_key_map = {client[1]: client[0] for client in clients}

        cmd = f"docker exec -i {docker_container} wg show"
        call = subprocess.check_output(cmd, shell=True)
        wg_output = call.decode('utf-8')

        active_clients = []
        current_peer = {}
        for line in wg_output.splitlines():
            line = line.strip()
            if line.startswith('peer:'):
                peer_public_key = line.split('peer: ')[1].strip()
                current_peer = {'public_key': peer_public_key}
            elif line.startswith('endpoint:') and 'public_key' in current_peer:
                current_peer['endpoint'] = line.split('endpoint: ')[1].strip()
            elif line.startswith('latest handshake:') and 'public_key' in current_peer:
                current_peer['latest_handshake'] = line.split('latest handshake: ')[1].strip()
            elif line.startswith('transfer:') and 'public_key' in current_peer:
                current_peer['transfer'] = line.split('transfer: ')[1].strip()
            elif line == '' and 'public_key' in current_peer:
                last_handshake = current_peer.get('latest_handshake', '').lower()
                if last_handshake not in ['never', 'нет данных', '-']:
                    peer_public_key = current_peer.get('public_key')
                    if peer_public_key in client_key_map:
                        username = client_key_map[peer_public_key]
                        last_time = current_peer.get('latest_handshake', 'Нет данных')
                        transfer = current_peer.get('transfer', 'Нет данных')
                        endpoint = current_peer.get('endpoint', 'Нет данных')
                        save_client_endpoint(username, endpoint)
                        active_clients.append([username, last_time, transfer, endpoint])
                current_peer = {}

        if 'public_key' in current_peer:
            last_handshake = current_peer.get('latest_handshake', '').lower()
            if last_handshake not in ['never', 'нет данных', '-']:
                peer_public_key = current_peer.get('public_key')
                if peer_public_key in client_key_map:
                    username = client_key_map[peer_public_key]
                    last_time = current_peer.get('latest_handshake', 'Нет данных')
                    transfer = current_peer.get('transfer', 'Нет данных')
                    endpoint = current_peer.get('endpoint', 'Нет данных')
                    save_client_endpoint(username, endpoint)
                    active_clients.append([username, last_time, transfer, endpoint])

        return active_clients

    except subprocess.CalledProcessError as e:
        print(f"Ошибка при получении активных клиентов: {e}")
        return []

def deactive_user_db(client_name):
    setting = get_config()
    wg_config_file = setting['wg_config_file']
    docker_container = setting['docker_container']

    clients = get_client_list()
    client_entry = next((c for c in clients if c[0] == client_name), None)
    if client_entry:
        client_public_key = client_entry[1]
        if subprocess.call(["./removeclient.sh", client_name, client_public_key, wg_config_file, docker_container]) == 0:
            return True
    else:
        logger.error(f"Пользователь {client_name} не найден в списке клиентов.")
    return False

def load_expirations():
    if not os.path.exists(EXPIRATIONS_FILE):
        return {}
    with open(EXPIRATIONS_FILE, 'r') as f:
        try:
            data = json.load(f)
            for user, info in data.items():
                if info.get('expiration_time'):
                    data[user]['expiration_time'] = datetime.fromisoformat(info['expiration_time']).replace(tzinfo=UTC)
                else:
                    data[user]['expiration_time'] = None
            return data
        except json.JSONDecodeError:
            logger.error("Ошибка при загрузке expirations.json.")
            return {}

def save_expirations(expirations):
    os.makedirs(os.path.dirname(EXPIRATIONS_FILE), exist_ok=True)
    data = {}
    for user, info in expirations.items():
        data[user] = {
            'expiration_time': info['expiration_time'].isoformat() if info['expiration_time'] else None,
            'traffic_limit': info.get('traffic_limit', "Неограниченно")
        }
    with open(EXPIRATIONS_FILE, 'w') as f:
        json.dump(data, f)

def set_user_expiration(username: str, expiration: datetime, traffic_limit: str):
    expirations = load_expirations()
    if username not in expirations:
        expirations[username] = {}
    if expiration:
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=UTC)
        expirations[username]['expiration_time'] = expiration
    else:
        expirations[username]['expiration_time'] = None
    expirations[username]['traffic_limit'] = traffic_limit
    save_expirations(expirations)

def remove_user_expiration(username: str):
    expirations = load_expirations()
    if username in expirations:
        del expirations[username]
        save_expirations(expirations)

def get_users_with_expiration():
    expirations = load_expirations()
    return [(user, info['expiration_time'].isoformat() if info['expiration_time'] else None, info.get('traffic_limit', "Неограниченно")) for user, info in expirations.items()]

def get_user_expiration(username: str):
    expirations = load_expirations()
    return expirations.get(username, {}).get('expiration_time', None)

def get_user_traffic_limit(username: str):
    expirations = load_expirations()
    return expirations.get(username, {}).get('traffic_limit', "Неограниченно")

def load_payments():
    if os.path.exists(PAYMENTS_FILE):
        try:
            with open(PAYMENTS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_payments(payments):
    os.makedirs(os.path.dirname(PAYMENTS_FILE), exist_ok=True)
    with open(PAYMENTS_FILE, 'w') as f:
        json.dump(payments, f, indent=4)

def add_payment(user_id: int, payment_id: str, amount: float, status: str = 'pending'):
    payments = load_payments()
    payment_data = {
        'user_id': user_id,
        'payment_id': payment_id,
        'amount': amount,
        'status': status,
        'timestamp': datetime.now(UTC).isoformat()
    }
    if str(user_id) not in payments:
        payments[str(user_id)] = []
    payments[str(user_id)].append(payment_data)
    save_payments(payments)
    return payment_data

def update_payment_status(payment_id: str, status: str):
    payments = load_payments()
    for user_payments in payments.values():
        for payment in user_payments:
            if payment['payment_id'] == payment_id:
                payment['status'] = status
                save_payments(payments)
                return True
    return False

def get_user_payments(user_id: int):
    payments = load_payments()
    return payments.get(str(user_id), [])

def get_all_payments():
    return load_payments()
