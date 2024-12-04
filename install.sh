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
    echo -e "\n${BLUE}Проверка обновлений...${NC}"
    
    # Проверяем наличие git репозитория
    if [ ! -d ".git" ]; then
        echo -e "${YELLOW}Репозиторий git не найден. Клонируем заново...${NC}"
        cd .. || { echo -e "${RED}Ошибка перехода в родительскую директорию${NC}"; exit 1; }
        rm -rf python_bot_amnezia
        clone_repository
        echo -e "${GREEN}Репозиторий успешно обновлен${NC}"
        return 0
    fi

    # Если .git существует, проверяем обновления
    git remote update >/dev/null 2>&1
    
    UPSTREAM=${1:-'@{u}'}
    LOCAL=$(git rev-parse @)
    REMOTE=$(git rev-parse "$UPSTREAM")
    BASE=$(git merge-base @ "$UPSTREAM")

    if [ $? -ne 0 ]; then
        echo -e "${RED}Ошибка при проверке обновлений. Переустанавливаем репозиторий...${NC}"
        cd .. || { echo -e "${RED}Ошибка перехода в родительскую директорию${NC}"; exit 1; }
        rm -rf python_bot_amnezia
        clone_repository
        echo -e "${GREEN}Репозиторий успешно обновлен${NC}"
        return 0
    fi

    if [ "$LOCAL" = "$REMOTE" ]; then
        echo -e "${GREEN}Установлена последняя версия${NC}"
    elif [ "$LOCAL" = "$BASE" ]; then
        echo -e "${YELLOW}Найдены обновления. Обновляем...${NC}"
        git pull >/dev/null 2>&1
        echo -e "${GREEN}Обновление завершено${NC}"
    elif [ "$REMOTE" = "$BASE" ]; then
        echo -e "${YELLOW}Локальные изменения будут перезаписаны...${NC}"
        git reset --hard origin/main >/dev/null 2>&1
        git pull >/dev/null 2>&1
        echo -e "${GREEN}Обновление завершено${NC}"
    else
        echo -e "${RED}Ветки разошлись. Сбрасываем к удаленной версии...${NC}"
        git reset --hard origin/main >/dev/null 2>&1
        git pull >/dev/null 2>&1
        echo -e "${GREEN}Обновление завершено${NC}"
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
    # Установка системных зависимостей
    run_with_spinner "Установка системных зависимостей" "sudo apt-get install qrencode jq net-tools iptables resolvconf git -y -qq"
    
    # Установка Python зависимостей
    run_with_spinner "Установка Python зависимостей" "pip install -r requirements.txt"
    
    # Создание и настройка конфигурационных директорий
    run_with_spinner "Создание конфигурационных директорий" "mkdir -p awg/config files"
    
    # Настройка YooKassa если конфигурация отсутствует
    if [ ! -f "awg/config/yookassa.py" ]; then
        if ! configure_yookassa; then
            echo -e "${RED}Ошибка настройки YooKassa. Файл конфигурации не создан.${NC}"
            echo -e "${YELLOW}Вы можете настроить YooKassa позже, отредактировав файл awg/config/yookassa.py${NC}"
        fi
    else
        echo -e "${YELLOW}Конфигурация YooKassa уже существует. Хотите перенастроить? (y/n)${NC}"
        read -r answer
        if [ "$answer" = "y" ]; then
            if ! configure_yookassa; then
                echo -e "${RED}Ошибка настройки YooKassa. Старая конфигурация сохранена.${NC}"
            fi
        fi
    fi
}

validate_yookassa_credentials() {
    local shop_id="$1"
    local secret_key="$2"
    
    if [ -z "$shop_id" ] || [ -z "$secret_key" ]; then
        echo -e "${RED}Ошибка: shop_id и secret_key не могут быть пустыми${NC}"
        return 1
    fi
    
    # Проверка формата shop_id (должен быть числовым)
    if ! [[ "$shop_id" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}Ошибка: shop_id должен содержать только цифры${NC}"
        return 1
    fi
    
    # Проверка формата secret_key (должен содержать TEST: или PROD:)
    if ! [[ "$secret_key" =~ ^(TEST|PROD):[0-9]+$ ]]; then
        echo -e "${RED}Ошибка: secret_key должен быть в формате TEST:XXXXX или PROD:XXXXX${NC}"
        return 1
    fi
    
    return 0
}

configure_yookassa() {
    echo -e "\n${BLUE}Настройка YooKassa платежей${NC}"
    
    while true; do
        # Запрос данных YooKassa
        echo -e "${YELLOW}Введите shop_id YooKassa:${NC}"
        read -r yookassa_shop_id
        
        echo -e "${YELLOW}Введите secret_key YooKassa:${NC}"
        read -r yookassa_secret_key
        
        # Валидация введенных данных
        if validate_yookassa_credentials "$yookassa_shop_id" "$yookassa_secret_key"; then
            break
        else
            echo -e "${YELLOW}Хотите попробовать ввести данные снова? (y/n)${NC}"
            read -r retry
            if [ "$retry" != "y" ]; then
                echo -e "${RED}Настройка YooKassa прервана${NC}"
                return 1
            fi
        fi
    done
    
    echo -e "${YELLOW}Введите username бота для return_url (например: my_bot):${NC}"
    read -r bot_username
    
    while [ -z "$bot_username" ]; do
        echo -e "${RED}Username бота не может быть пустым. Попробуйте снова:${NC}"
        read -r bot_username
    done
    
    # Создание конфигурационного файла с правильными отступами
    cat > awg/config/yookassa.py << EOL
"""
YooKassa configuration and settings
"""

YOOKASSA_CONFIG = {
    "shop_id": "${yookassa_shop_id}",
    "secret_key": "${yookassa_secret_key}",
    "return_url": "https://t.me/${bot_username}"
}

SUBSCRIPTION_PRICES = {
    "1_month": 300,
    "3_months": 800,
    "6_months": 1500,
    "12_months": 2800
}

SUBSCRIPTION_DAYS = {
    "1_month": 30,
    "3_months": 90,
    "6_months": 180,
    "12_months": 365
}
EOL

    echo -e "${GREEN}✓ Конфигурация YooKassa сохранена${NC}"
    return 0
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
        return 0
    fi
    
    run_with_spinner "Клонирование репозитория" "git clone https://github.com/IgnatTOP/python_bot_amnezia.git >/dev/null 2>&1"
    cd python_bot_amnezia || { echo -e "\n${RED}Ошибка перехода в директорию${NC}"; exit 1; }
}

setup_venv() {
    if [[ -d "myenv" ]]; then
        echo -e "\n${YELLOW}Виртуальное окружение существует${NC}"
        return 0
    fi
    
    run_with_spinner "Настройка виртуального окружения" "python3.11 -m venv myenv && source myenv/bin/activate && pip install --upgrade pip && pip install -r $(pwd)/requirements.txt && deactivate"
}

set_permissions() {
    find . -type f -name "*.sh" -exec chmod +x {} \; 2>chmod_error.log
    local status=$?
    [[ $status -ne 0 ]] && { cat chmod_error.log; rm -f chmod_error.log; exit 1; }
    rm -f chmod_error.log
}

initialize_bot() {
    cd awg || { echo -e "\n${RED}Ошибка перехода в директорию${NC}"; exit 1; }
    
    ../myenv/bin/python3.11 bot_manager.py < /dev/tty &
    local BOT_PID=$!
    
    while [ ! -f "files/setting.ini" ]; do
        sleep 2
        kill -0 "$BOT_PID" 2>/dev/null || { echo -e "\n${RED}Бот завершил работу до инициализации${NC}"; exit 1; }
    done
    
    kill "$BOT_PID"
    wait "$BOT_PID" 2>/dev/null
    cd ..
}

create_service() {
    cat > /tmp/service_file << EOF
[Unit]
Description=AmneziaVPN Docker Telegram Bot
After=network.target

[Service]
User=$USER
WorkingDirectory=$(pwd)/awg
ExecStart=$(pwd)/myenv/bin/python3.11 bot_manager.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

    run_with_spinner "Создание службы" "sudo mv /tmp/service_file /etc/systemd/system/$SERVICE_NAME.service"
    run_with_spinner "Обновление systemd" "sudo systemctl daemon-reload -qq"
    run_with_spinner "Запуск службы" "sudo systemctl start $SERVICE_NAME -qq"
    run_with_spinner "Включение автозапуска" "sudo systemctl enable $SERVICE_NAME -qq"
    
    systemctl is-active --quiet "$SERVICE_NAME" && echo -e "\n${GREEN}Служба запущена${NC}" || echo -e "\n${RED}Ошибка запуска службы${NC}"
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
