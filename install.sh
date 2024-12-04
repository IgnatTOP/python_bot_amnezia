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
    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}Python 3 не установлен${NC}"
        run_with_spinner "Установка Python 3" "sudo apt-get install python3 -y -qq"
    fi

    # Проверяем версию Python
    PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    
    # Преобразуем версию в числовой формат для сравнения (например, 3.7 -> 3.07)
    VERSION_NORMALIZED=$(echo "$PYTHON_VERSION" | awk -F. '{ printf("%d.%02d\n", $1, $2) }')
    MIN_VERSION="3.07"  # Минимальная версия 3.7
    
    if (( $(echo "$VERSION_NORMALIZED < $MIN_VERSION" | bc -l) )); then
        echo -e "${RED}Требуется Python версииЯЯЯЯ 3.7 или выше. Текущая версия: $PYTHON_VERSION${NC}"
        return 1
    fi

    echo -e "${GREEN}Python $PYTHON_VERSION установлен${NC}"

    # Установка pip если он отсутствует
    if ! command -v pip3 &>/dev/null; then
        echo -e "${YELLOW}pip3 не установлен. Устанавливаем...${NC}"
        run_with_spinner "Установка pip3" "sudo apt-get install python3-pip -y -qq"
    fi

    # Обновление pip до последней версии
    run_with_spinner "Обновление pip" "python3 -m pip install --upgrade pip -q"
    
    return 0
}

install_dependencies() {
    # Установка системных зависимостей
    run_with_spinner "Установка системных зависимостей" "sudo apt-get install qrencode jq net-tools iptables resolvconf git bc python3-venv -y -qq"
    
    # Создание и активация виртуального окружения
    if [ ! -d "venv" ]; then
        run_with_spinner "Создание виртуального окружения" "python3 -m venv venv"
    fi
    source venv/bin/activate
    
    # Установка Python зависимостей
    run_with_spinner "Установка Python зависимостей" "pip install --upgrade pip && pip install -r requirements.txt"
    
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
            configure_yookassa
        fi
    fi
    
    # Деактивация виртуального окружения
    deactivate
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
    
    # Проверка формата secret_key (поддержка старого и нового формата)
    if ! [[ "$secret_key" =~ ^(test|live)_[A-Za-z0-9_\-]+$ ]] && ! [[ "$secret_key" =~ ^(TEST|PROD):[0-9]+$ ]]; then
        echo -e "${RED}Ошибка: неверный формат secret_key${NC}"
        echo -e "${YELLOW}Поддерживаемые форматы:${NC}"
        echo -e "1. Новый формат: test_XXXXX или live_XXXXX"
        echo -e "2. Старый формат: TEST:XXXXX или PROD:XXXXX"
        return 1
    fi
    
    return 0
}

configure_yookassa() {
    echo -e "\n${BLUE}Настройка YooKassa платежей${NC}"
    echo -e "${YELLOW}Данные можно найти в личном кабинете YooKassa: https://yookassa.ru/my${NC}"
    
    while true; do
        # Запрос данных YooKassa
        echo -e "\n${YELLOW}Введите shop_id YooKassa (только цифры):${NC}"
        read -r yookassa_shop_id
        
        echo -e "${YELLOW}Введите secret_key YooKassa (начинается с test_ или live_):${NC}"
        read -r yookassa_secret_key
        
        # Валидация введенных данных
        if validate_yookassa_credentials "$yookassa_shop_id" "$yookassa_secret_key"; then
            break
        else
            echo -e "\n${YELLOW}Хотите попробовать ввести данные снова? (y/n)${NC}"
            read -r retry
            if [ "$retry" != "y" ]; then
                echo -e "${RED}Настройка YooKassa прервана${NC}"
                return 1
            fi
        fi
    done
    
    echo -e "\n${YELLOW}Введите username бота для return_url (без символа @):${NC}"
    read -r bot_username
    
    while [ -z "$bot_username" ] || [[ "$bot_username" == @* ]]; do
        if [ -z "$bot_username" ]; then
            echo -e "${RED}Username бота не может быть пустым.${NC}"
        else
            echo -e "${RED}Username бота должен быть без символа @${NC}"
        fi
        echo -e "${YELLOW}Попробуйте снова:${NC}"
        read -r bot_username
    done
    
    # Создание конфигурационного файла
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
    
    echo -e "${GREEN}Конфигурация YooKassa успешно сохранена${NC}"
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
    echo -e "\n${BLUE}Настройка виртуального окружения...${NC}"
    
    # Создание виртуального окружения если оно не существует
    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi
    
    # Активация виртуального окружения
    source venv/bin/activate
    
    # Установка зависимостей в виртуальное окружение
    pip install --upgrade pip
    pip install -r requirements.txt
    
    # Создание __init__.py файлов для корректной работы импортов
    touch awg/__init__.py
    touch awg/config/__init__.py
    
    # Деактивация виртуального окружения
    deactivate
    
    echo -e "${GREEN}✓ Виртуальное окружение настроено${NC}"
}

initialize_bot() {
    echo -e "\n${BLUE}Инициализация бота...${NC}"
    
    # Активация виртуального окружения
    source venv/bin/activate
    
    # Запуск бота с правильным PYTHONPATH
    PYTHONPATH=$(pwd) python3 -m awg.bot_manager
    
    # Деактивация виртуального окружения
    deactivate
}

create_service() {
    cat > /etc/systemd/system/$SERVICE_NAME.service << EOL
[Unit]
Description=AWG Bot Service
After=network.target

[Service]
User=$USER
WorkingDirectory=$(pwd)
Environment=PYTHONPATH=$(pwd)
ExecStart=$(pwd)/venv/bin/python3 -m awg.bot_manager
Restart=always

[Install]
WantedBy=multi-user.target
EOL

    run_with_spinner "Создание службы" "chmod 644 /etc/systemd/system/$SERVICE_NAME.service"
    run_with_spinner "Обновление systemd" "systemctl daemon-reload"
    run_with_spinner "Запуск службы" "systemctl start $SERVICE_NAME"
    run_with_spinner "Включение автозапуска" "systemctl enable $SERVICE_NAME"
    
    echo -e "\n${GREEN}Служба запущена${NC}"
}

install_bot() {
    get_ubuntu_version
    update_and_clean_system
    if ! check_python; then
        echo -e "${RED}Ошибка при проверке Python. Установка прервана.${NC}"
        exit 1
    fi
    echo -e "${GREEN}Начинаем установку зависимостей...${NC}"
    install_dependencies
    install_and_configure_needrestart
    clone_repository
    setup_venv
    initialize_bot
    create_service
}

main() {
    systemctl list-units --type=service --all | grep -q "$SERVICE_NAME.service" && installed_menu || { install_bot; ( sleep 1; rm -- "$SCRIPT_PATH" ) & exit 0; }
}

main
