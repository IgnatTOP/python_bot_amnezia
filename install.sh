#!/bin/bash

SERVICE_NAME="awg_bot"

GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
BLUE=$'\033[0;34m'
NC=$'\033[0m'

ENABLE_LOGS=false

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="$(basename "$0")"
SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_NAME"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --quiet) ENABLE_LOGS=false ;;
        --verbose) ENABLE_LOGS=true ;;
        *) 
            echo -e "${RED}Неизвестный параметр: $1${NC}"
            echo "Использование: $0 [--quiet|--verbose]"
            exit 1
            ;;
    esac
    shift
done

get_ubuntu_version() {
    if command -v lsb_release &>/dev/null; then
        UBUNTU_VERSION=$(lsb_release -rs)
        UBUNTU_CODENAME=$(lsb_release -cs)
        DISTRIB_ID=$(lsb_release -is)
        [[ "$DISTRIB_ID" != "Ubuntu" ]] && { echo -e "${RED}Скрипт поддерживает только Ubuntu. Обнаружена система: $DISTRIB_ID${NC}"; exit 1; }
    else
        echo -e "${RED}lsb_release не установлен. Необходимо установить lsb-release${NC}"
        exit 1
    fi
}

run_with_spinner() {
    local description="$1"
    shift
    local cmd="$@"

    if [ "$ENABLE_LOGS" = true ]; then
        echo -e "${BLUE}${description}...${NC}"
        eval "$cmd"
        local status=$?
        if [ $status -eq 0 ]; then
            echo -e "${GREEN}${description}... Done!${NC}\n"
        else
            echo -e "${RED}${description}... Failed!${NC}"
            echo -e "${RED}Ошибка при выполнении команды: $cmd${NC}\n"
            exit 1
        fi
    else
        local stdout_temp=$(mktemp)
        local stderr_temp=$(mktemp)

        eval "$cmd" >"$stdout_temp" 2>"$stderr_temp" &
        local pid=$!

        local spinner='|/-\'
        local i=0

        echo -ne "${BLUE}${description}...${NC} ${spinner:i++%${#spinner}:1}"

        while kill -0 "$pid" 2>/dev/null; do
            printf "\r${BLUE}${description}...${NC} ${spinner:i++%${#spinner}:1}"
            sleep 0.1
        done

        wait "$pid"
        local status=$?
        
        if [ $status -eq 0 ]; then
            printf "\r${BLUE}${description}...${NC} ${spinner:i%${#spinner}:1} ${GREEN}Done!${NC}\n\n"
        else
            printf "\r${BLUE}${description}...${NC} ${spinner:i%${#spinner}:1} ${RED}Failed!${NC}\n\n"
            echo -e "${RED}Ошибка при выполнении команды: $cmd${NC}"
            echo -e "${RED}Вывод ошибки:${NC}"
            cat "$stderr_temp"
        fi

        rm -f "$stdout_temp" "$stderr_temp"

        if [ $status -ne 0 ]; then
            exit 1
        fi
    fi
}

check_updates() {
    local stdout_temp=$(mktemp)
    local stderr_temp=$(mktemp)
    
    if ! git pull >"$stdout_temp" 2>"$stderr_temp"; then
        echo -e "${RED}Ошибка при проверке обновлений:${NC}"
        cat "$stderr_temp"
        rm -f "$stdout_temp" "$stderr_temp"
        echo
        return 1
    fi

    local changes=$(cat "$stdout_temp")
    rm -f "$stdout_temp" "$stderr_temp"

    if [[ "$changes" == "Already up to date." ]]; then
        echo -e "${GREEN}Обновления не требуются${NC}\n"
    elif [[ -n "$changes" ]]; then
        echo -e "${GREEN}Обновления установлены:${NC}"
        echo "$changes"
        echo
        read -p "Требуется перезапустить службу для применения обновлений. Перезапустить? (y/n): " restart
        if [[ "$restart" =~ ^[Yy]$ ]]; then
            run_with_spinner "Перезапуск службы" "sudo systemctl restart $SERVICE_NAME -qq"
            echo -e "${GREEN}Служба перезапущена${NC}\n"
        else
            echo -e "${YELLOW}Для применения обновлений требуется перезапустить службу${NC}\n"
        fi
    else
        echo -e "${RED}Неожиданный ответ при проверке обновлений${NC}\n"
    fi
}

service_control_menu() {
    while true; do
        echo -e "\n${BLUE}=== Управление службой $SERVICE_NAME ===${NC}"
        sudo systemctl status "$SERVICE_NAME" | grep -E "Active:|Loaded:"
        echo -e "\n${GREEN}1${NC}. Остановить службу"
        echo -e "${GREEN}2${NC}. Перезапустить службу"
        echo -e "${GREEN}3${NC}. Переустановить службу"
        echo -e "${RED}4${NC}. Удалить службу"
        echo -e "${YELLOW}5${NC}. Назад"
        
        echo -ne "\n${BLUE}Выберите действие:${NC} "
        read action
        case $action in
            1) run_with_spinner "Остановка службы" "sudo systemctl stop $SERVICE_NAME -qq" ;;
            2) run_with_spinner "Перезапуск службы" "sudo systemctl restart $SERVICE_NAME -qq" ;;
            3) create_service ;;
            4) run_with_spinner "Удаление службы" "sudo systemctl stop $SERVICE_NAME -qq && sudo systemctl disable $SERVICE_NAME -qq && sudo rm /etc/systemd/system/$SERVICE_NAME.service && sudo systemctl daemon-reload -qq" ;;
            5) return 0 ;;
            *) echo -e "${RED}Некорректный ввод${NC}" ;;
        esac
    done
}

installed_menu() {
    while true; do
        echo -e "\n${BLUE}=== AWG Docker Telegram Bot ===${NC}"
        echo -e "${GREEN}1${NC}. Проверить обновления"
        echo -e "${GREEN}2${NC}. Управление службой"
        echo -e "${YELLOW}3${NC}. Выход"
        
        echo -ne "\n${BLUE}Выберите действие:${NC} "
        read action
        case $action in
            1) check_updates ;;
            2) service_control_menu ;;
            3) exit 0 ;;
            *) echo -e "${RED}Некорректный ввод${NC}" ;;
        esac
    done
}

update_and_clean_system() {
    run_with_spinner "Обновление системы" "sudo apt-get update -qq && sudo apt-get upgrade -y -qq"
    run_with_spinner "Очистка системы" "sudo apt-get autoclean -qq && sudo apt-get autoremove --purge -y -qq"
}

check_python() {
    if command -v python3.11 &>/dev/null; then
        echo -e "\n${GREEN}Python 3.11 установлен${NC}"
        return 0
    fi
    
    echo -e "\n${RED}Python 3.11 не установлен${NC}"
    read -p "Установить Python 3.11? (y/n): " install_python
    
    if [[ "$install_python" =~ ^[Yy]$ ]]; then
       if [[ "$UBUNTU_VERSION" == "24.04" ]]; then
            local max_attempts=30
            local attempt=1
            
            while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
                echo -e "${YELLOW}Ожидание освобождения dpkg lock (попытка $attempt из $max_attempts)${NC}"
                attempt=$((attempt + 1))
                if [ $attempt -gt $max_attempts ]; then
                    echo -e "${RED}Превышено время ожидания освобождения dpkg lock${NC}"
                    exit 1
                fi
                sleep 10
            done
        fi
        
        run_with_spinner "Установка Python 3.11" "sudo apt-get install software-properties-common -y && sudo add-apt-repository ppa:deadsnakes/ppa -y && sudo apt-get update -qq && sudo apt-get install python3.11 python3.11-venv python3.11-dev -y -qq"
            
        if ! command -v python3.11 &>/dev/null; then
            echo -e "\n${RED}Не удалось установить Python 3.11${NC}"
            exit 1
        fi
    else
        echo -e "\n${RED}Установка Python 3.11 обязательна${NC}"
        exit 1
    fi
}

install_dependencies() {
    run_with_spinner "Установка зависимостей" "sudo apt-get install -y python3-pip python3-venv git"
    run_with_spinner "Установка дополнительных зависимостей" "sudo apt-get install -y python3-dev build-essential"
}

install_and_configure_needrestart() {
    run_with_spinner "Установка needrestart" "sudo apt-get install needrestart -y -qq"
    sudo sed -i 's/^#\?\(nrconf{restart} = "\).*$/\1a";/' /etc/needrestart/needrestart.conf
    grep -q 'nrconf{restart} = "a";' /etc/needrestart/needrestart.conf || echo 'nrconf{restart} = "a";' | sudo tee /etc/needrestart/needrestart.conf >/dev/null 2>&1
}

clone_repository() {
    if [[ -d "python_bot_amnezia" ]]; then
        echo -e "\n${YELLOW}Репозиторий существует${NC}"
        cd python_bot_amnezia || { echo -e "\n${RED}Ошибка перехода в директорию${NC}"; exit 1; }
    else
        run_with_spinner "Клонирование репозитория" "git clone https://github.com/IgnatTOP/python_bot_amnezia.git python_bot_amnezia"
        cd python_bot_amnezia || { echo -e "\n${RED}Ошибка перехода в директорию${NC}"; exit 1; }
    fi
    SCRIPT_DIR="$(pwd)"
}

setup_venv() {
    run_with_spinner "Создание виртуального окружения" "python3 -m venv venv"
    source venv/bin/activate
    run_with_spinner "Обновление pip" "./venv/bin/pip install --upgrade pip"
    run_with_spinner "Установка зависимостей" "./venv/bin/pip install -r requirements.txt"
    run_with_spinner "Установка YooKassa" "./venv/bin/pip install yookassa"
}

set_permissions() {
    find . -type f -name "*.sh" -exec chmod +x {} \; 2>chmod_error.log
    local status=$?
    [[ $status -ne 0 ]] && { cat chmod_error.log; rm -f chmod_error.log; exit 1; }
    rm -f chmod_error.log
}

initialize_bot() {
    if [ ! -f "$SCRIPT_DIR/files/setting.ini" ]; then
        echo -e "\n${YELLOW}Настройка бота${NC}"
        
        read -p "Введите токен Telegram бота: " bot_token
        read -p "Введите Telegram ID администратора: " admin_id
        read -p "Введите Shop ID YooKassa: " yookassa_shop_id
        read -p "Введите Secret Key YooKassa: " yookassa_secret_key
        
        docker_container=$(get_amnezia_container)
        
        cmd="docker exec $docker_container find / -name wg0.conf"
        wg_config_file=$(eval "$cmd" 2>/dev/null || echo "/opt/amnezia/awg/wg0.conf")
        
        endpoint=$(curl -s https://api.ipify.org || read -p "Введите внешний IP-адрес сервера: ")
        
        mkdir -p "$SCRIPT_DIR/files"
        
        cat > "$SCRIPT_DIR/files/setting.ini" << EOL
[setting]
bot_token = $bot_token
admin_id = $admin_id
docker_container = $docker_container
wg_config_file = $wg_config_file
endpoint = $endpoint
yookassa_shop_id = $yookassa_shop_id
yookassa_secret_key = $yookassa_secret_key
EOL
        
        echo -e "${GREEN}Файл конфигурации создан${NC}"
    else
        echo -e "${YELLOW}Файл конфигурации уже существует${NC}"
    fi
    
    # Create required directories
    mkdir -p "$SCRIPT_DIR/files/connections"
    mkdir -p "$SCRIPT_DIR/files/traffic"
    mkdir -p "$SCRIPT_DIR/files/payments"
    
    # Set proper permissions
    chown -R $SUDO_USER:$SUDO_USER "$SCRIPT_DIR/files"
    chmod -R 755 "$SCRIPT_DIR/files"
}

create_service() {
    cat > /etc/systemd/system/${SERVICE_NAME}.service << EOL
[Unit]
Description=AWG Bot Service
After=network.target

[Service]
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/python3 awg/bot_manager.py
Restart=always

[Install]
WantedBy=multi-user.target
EOL

    run_with_spinner "Перезагрузка systemd" "systemctl daemon-reload"
    run_with_spinner "Включение сервиса" "systemctl enable ${SERVICE_NAME}.service"
    run_with_spinner "Запуск сервиса" "systemctl start ${SERVICE_NAME}.service"
}

install_bot() {
    get_ubuntu_version
    update_and_clean_system
    check_python
    install_dependencies
    install_and_configure_needrestart
    clone_repository
    setup_venv
    set_permissions
    initialize_bot
    create_service
}

main() {
    systemctl list-units --type=service --all | grep -q "$SERVICE_NAME.service" && installed_menu || { install_bot; ( sleep 1; rm -- "$SCRIPT_PATH" ) & exit 0; }
}

main
