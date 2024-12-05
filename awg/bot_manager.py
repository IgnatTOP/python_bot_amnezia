import db
import aiohttp
import logging
import asyncio
import aiofiles
import os
import re
import tempfile
import json
import subprocess
import sys
import pytz
import zipfile
import ipaddress
import humanize
import shutil
from aiogram import Bot, types
from aiogram.dispatcher import Dispatcher
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from yookassa import Configuration, Payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

setting = db.get_config()
bot_token = setting.get('bot_token')
admin_id = setting.get('admin_id')
wg_config_file = setting.get('wg_config_file')
docker_container = setting.get('docker_container')
endpoint = setting.get('endpoint')

if not all([bot_token, admin_id, wg_config_file, docker_container, endpoint]):
    logger.error("Некоторые обязательные настройки отсутствуют в конфигурационном файле.")
    sys.exit(1)

bot = Bot(bot_token)
admin = int(admin_id)
WG_CONFIG_FILE = wg_config_file
DOCKER_CONTAINER = docker_container
ENDPOINT = endpoint

Configuration.account_id = '993270'
Configuration.secret_key = 'test_cE-RElZLKakvb585wjrh9XAoqGSyS_rcmta2v1MdURE'

PAYMENT_AMOUNTS = {
    "1_month": 500,  # 500 RUB for 1 month
    "3_months": 1200,  # 1200 RUB for 3 months
    "6_months": 2000,  # 2000 RUB for 6 months
    "12_months": 3500  # 3500 RUB for 12 months
}

class AdminMessageDeletionMiddleware(BaseMiddleware):
    async def on_process_message(self, message: types.Message, data: dict):
        if message.from_user.id == admin:
            asyncio.create_task(delete_message_after_delay(message.chat.id, message.message_id, delay=2))

dp = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=pytz.UTC)
scheduler.start()

dp.middleware.setup(AdminMessageDeletionMiddleware())

navigation_history = {}

def get_menu_markup(user_id, current_menu, include_back=True):
    markup = InlineKeyboardMarkup(row_width=1)
    
    if user_id == admin:
        if current_menu == 'main':
            markup.add(
                InlineKeyboardButton("Добавить пользователя", callback_data="add_user"),
                InlineKeyboardButton("Получить конфигурацию пользователя", callback_data="get_config"),
                InlineKeyboardButton("Список клиентов", callback_data="list_users"),
                InlineKeyboardButton("Создать бекап", callback_data="create_backup"),
                InlineKeyboardButton("История платежей", callback_data="payment_history"),
                InlineKeyboardButton("Отправить сообщение всем", callback_data="mass_message")
            )
        elif current_menu == 'user_list':
            clients = db.get_clients()
            for client in clients:
                markup.add(InlineKeyboardButton(f"👤 {client}", callback_data=f"client_{client}"))
            if include_back:
                markup.add(InlineKeyboardButton("◀️ Назад", callback_data="back"))
        elif current_menu == 'user_actions':
            markup.add(
                InlineKeyboardButton("Информация о подключениях", callback_data="connections"),
                InlineKeyboardButton("Удалить пользователя", callback_data="delete"),
                InlineKeyboardButton("◀️ Назад", callback_data="back")
            )
    else:
        if current_menu == 'main':
            markup.add(
                InlineKeyboardButton("Купить VPN", callback_data="buy_vpn"),
                InlineKeyboardButton("Мой VPN ключ", callback_data="my_vpn_key"),
                InlineKeyboardButton("Помощь", callback_data="help")
            )
        elif current_menu == 'buy_vpn':
            markup.add(
                InlineKeyboardButton("1 месяц - 500₽", callback_data="payment_1_month"),
                InlineKeyboardButton("3 месяца - 1200₽", callback_data="payment_3_months"),
                InlineKeyboardButton("6 месяцев - 2000₽", callback_data="payment_6_months"),
                InlineKeyboardButton("12 месяцев - 3500₽", callback_data="payment_12_months"),
                InlineKeyboardButton("◀️ Назад", callback_data="back")
            )
    
    return markup

def push_navigation_state(user_id, menu_name, message_id=None, chat_id=None, additional_data=None):
    if user_id not in navigation_history:
        navigation_history[user_id] = []
    
    state = {
        'menu': menu_name,
        'message_id': message_id,
        'chat_id': chat_id,
        'data': additional_data or {}
    }
    
    navigation_history[user_id].append(state)

def pop_navigation_state(user_id):
    if user_id in navigation_history and navigation_history[user_id]:
        navigation_history[user_id].pop()
        if navigation_history[user_id]:
            return navigation_history[user_id][-1]
    return {'menu': 'main', 'data': {}}

@dp.callback_query_handler(lambda c: c.data == 'back')
async def handle_back(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    previous_state = pop_navigation_state(user_id)
    
    markup = get_menu_markup(user_id, previous_state['menu'])
    menu_text = "Выберите действие:"
    
    if previous_state['menu'] == 'user_list':
        menu_text = "Список пользователей:"
    elif previous_state['menu'] == 'user_actions':
        menu_text = f"Действия с пользователем {previous_state['data'].get('client_name', '')}:"
    
    try:
        await bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text=menu_text,
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Error handling back navigation: {e}")
        # Если не удалось отредактировать сообщение, отправляем новое
        sent_message = await callback_query.message.reply(menu_text, reply_markup=markup)
        push_navigation_state(user_id, previous_state['menu'], sent_message.message_id, sent_message.chat.id)

TRAFFIC_LIMITS = ["5 GB", "10 GB", "30 GB", "100 GB", "Неограниченно"]

def get_interface_name():
    return os.path.basename(WG_CONFIG_FILE).split('.')[0]

async def load_isp_cache():
    global isp_cache
    if os.path.exists(ISP_CACHE_FILE):
        async with aiofiles.open(ISP_CACHE_FILE, 'r') as f:
            try:
                isp_cache = json.loads(await f.read())
                for ip in list(isp_cache.keys()):
                    isp_cache[ip]['timestamp'] = datetime.fromisoformat(isp_cache[ip]['timestamp'])
            except:
                isp_cache = {}

async def save_isp_cache():
    async with aiofiles.open(ISP_CACHE_FILE, 'w') as f:
        cache_to_save = {ip: {'isp': data['isp'], 'timestamp': data['timestamp'].isoformat()} for ip, data in isp_cache.items()}
        await f.write(json.dumps(cache_to_save))

async def get_isp_info(ip: str) -> str:
    now = datetime.now(pytz.UTC)
    if ip in isp_cache:
        if now - isp_cache[ip]['timestamp'] < CACHE_TTL:
            return isp_cache[ip]['isp']
    try:
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private:
            return "Private Range"
    except:
        return "Invalid IP"
    url = f"http://ip-api.com/json/{ip}?fields=status,message,isp"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('status') == 'success':
                        isp = data.get('isp', 'Unknown ISP')
                        isp_cache[ip] = {'isp': isp, 'timestamp': now}
                        await save_isp_cache()
                        return isp
    except:
        pass
    return "Unknown ISP"

async def cleanup_isp_cache():
    now = datetime.now(pytz.UTC)
    for ip in list(isp_cache.keys()):
        if now - isp_cache[ip]['timestamp'] >= CACHE_TTL:
            del isp_cache[ip]
    await save_isp_cache()

async def cleanup_connection_data(username: str):
    file_path = os.path.join('files', 'connections', f'{username}_ip.json')
    if os.path.exists(file_path):
        async with aiofiles.open(file_path, 'r') as f:
            try:
                data = json.loads(await f.read())
            except:
                data = {}
        sorted_ips = sorted(data.items(), key=lambda x: datetime.strptime(x[1], '%d.%m.%Y %H:%M'), reverse=True)
        limited_ips = dict(sorted_ips[:100])
        async with aiofiles.open(file_path, 'w') as f:
            await f.write(json.dumps(limited_ips))

async def load_isp_cache_task():
    await load_isp_cache()
    scheduler.add_job(cleanup_isp_cache, 'interval', hours=1)

@dp.callback_query_handler(lambda c: c.data == "list_users")
async def list_users_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id != admin:
        await callback_query.answer("У вас нет прав администратора")
        return
    
    markup = get_menu_markup(user_id, 'user_list')
    menu_text = "Список пользователей:"
    
    try:
        await bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text=menu_text,
            reply_markup=markup
        )
        push_navigation_state(user_id, 'user_list', callback_query.message.message_id, callback_query.message.chat.id)
    except Exception as e:
        logger.error(f"Error showing user list: {e}")
        sent_message = await callback_query.message.reply(menu_text, reply_markup=markup)
        push_navigation_state(user_id, 'user_list', sent_message.message_id, sent_message.chat.id)

@dp.callback_query_handler(lambda c: c.data.startswith('client_'))
async def client_selected_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
        
    _, username = callback_query.data.split('client_', 1)
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Получить конфигурацию", callback_data=f"get_config_{username}"),
        InlineKeyboardButton("Удалить пользователя", callback_data=f"delete_{username}"),
        InlineKeyboardButton("« Назад", callback_data="back"),
        InlineKeyboardButton("« В главное меню", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        f"Действия с пользователем {username}:",
        reply_markup=keyboard
    )
    push_navigation_state(callback_query.from_user.id, 'user_actions', callback_query.message.message_id, callback_query.message.chat.id, {'client_name': username})

@dp.callback_query_handler(lambda c: c.data.startswith('connections_'))
async def client_connections_callback(callback_query: types.CallbackQuery):
    _, username = callback_query.data.split('connections_', 1)
    username = username.strip()
    file_path = os.path.join('files', 'connections', f'{username}_ip.json')
    if not os.path.exists(file_path):
        await callback_query.answer("Нет данных о подключениях пользователя.", show_alert=True)
        return
    try:
        async with aiofiles.open(file_path, 'r') as f:
            data = json.loads(await f.read())
        sorted_ips = sorted(data.items(), key=lambda x: datetime.strptime(x[1], '%d.%m.%Y %H:%M'), reverse=True)
        last_connections = sorted_ips[:5]
        isp_tasks = [get_isp_info(ip) for ip, _ in last_connections]
        isp_results = await asyncio.gather(*isp_tasks)
        connections_text = f"*Последние подключения пользователя {username}:*\n"
        for (ip, timestamp), isp in zip(last_connections, isp_results):
            connections_text += f"{ip} ({isp}) - {timestamp}\n"
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("Назад", callback_data=f"client_{username}"),
            InlineKeyboardButton("Домой", callback_data="home")
        )
        await bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text=connections_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка при получении данных о подключениях для пользователя {username}: {e}")
        await callback_query.answer("Ошибка при получении данных о подключениях.", show_alert=True)
        return
    await cleanup_connection_data(username)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('ip_info_'))
async def ip_info_callback(callback_query: types.CallbackQuery):
    _, username = callback_query.data.split('ip_info_', 1)
    username = username.strip()
    active_clients = db.get_active_list()
    active_info = next((ac for ac in active_clients if ac[0] == username), None)
    if active_info:
        endpoint = active_info[3]
        ip_address = endpoint.split(':')[0]
    else:
        await callback_query.answer("Нет информации о подключении пользователя.", show_alert=True)
        return
    url = f"http://ip-api.com/json/{ip_address}?fields=message,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as,hosting"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if 'message' in data:
                        await callback_query.answer(f"Ошибка при получении данных: {data['message']}", show_alert=True)
                        return
                else:
                    await callback_query.answer(f"Ошибка при запросе к API: {resp.status}", show_alert=True)
                    return
    except Exception as e:
        logger.error(f"Ошибка при запросе к API: {e}")
        await callback_query.answer("Ошибка при запросе к API.", show_alert=True)
        return
    info_text = f"*IP информация для {username}:*\n"
    for key, value in data.items():
        info_text += f"{key.capitalize()}: {value}\n"
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Назад", callback_data=f"client_{username}"),
        InlineKeyboardButton("Домой", callback_data="home")
    )
    main_chat_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('chat_id')
    main_message_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('message_id')
    if main_chat_id and main_message_id:
        try:
            await bot.edit_message_text(
                chat_id=main_chat_id,
                message_id=main_message_id,
                text=info_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка при изменении сообщения: {e}")
            await callback_query.answer("Ошибка при обновлении сообщения.", show_alert=True)
            return
    else:
        await callback_query.answer("Ошибка: главное сообщение не найдено.", show_alert=True)
        return
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('delete_user_'))
async def client_delete_callback(callback_query: types.CallbackQuery):
    username = callback_query.data.split('delete_user_')[1]
    success = db.deactive_user_db(username)
    if success:
        db.remove_user_expiration(username)
        try:
            scheduler.remove_job(job_id=username)
        except:
            pass
        user_dir = os.path.join('users', username)
        try:
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir)
        except Exception as e:
            logger.error(f"Ошибка при удалении директории для пользователя {username}: {e}")
        confirmation_text = f"Пользователь **{username}** успешно удален."
    else:
        confirmation_text = f"Не удалось удалить пользователя **{username}**."
    main_chat_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('chat_id')
    main_message_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text=confirmation_text,
            parse_mode="Markdown",
            reply_markup=get_menu_markup(callback_query.from_user.id, 'user_list')
        )
    else:
        await callback_query.answer("Ошибка: главное сообщение не найдено.", show_alert=True)
        return
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == 'home')
async def return_home(callback_query: types.CallbackQuery):
    markup = get_menu_markup(callback_query.from_user.id, 'main')
    main_chat_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('chat_id')
    main_message_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('message_id')

    if main_chat_id and main_message_id:
        navigation_history[callback_query.from_user.id] = [{'menu': 'main', 'data': {}}]
        
        try:
            await bot.edit_message_text(
                chat_id=main_chat_id,
                message_id=main_message_id,
                text="Выберите действие:",
                reply_markup=markup
            )
        except:
            sent_message = await callback_query.message.reply("Выберите действие:", reply_markup=markup)
            push_navigation_state(callback_query.from_user.id, 'main', sent_message.message_id, sent_message.chat.id)
    else:
        sent_message = await callback_query.message.reply("Выберите действие:", reply_markup=markup)
        push_navigation_state(callback_query.from_user.id, 'main', sent_message.message_id, sent_message.chat.id)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('get_config'))
async def list_users_for_config(callback_query: types.CallbackQuery):
    clients = db.get_client_list()
    if not clients:
        await callback_query.answer("Список пользователей пуст.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(row_width=2)
    for client in clients:
        username = client[0]
        keyboard.insert(InlineKeyboardButton(username, callback_data=f"send_config_{username}"))
    keyboard.add(InlineKeyboardButton("Домой", callback_data="home"))
    main_chat_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('chat_id')
    main_message_id = navigation_history.get(callback_query.from_user.id, [{}])[-1].get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text="Выберите пользователя для получения конфигурации:",
            reply_markup=keyboard
        )
    else:
        sent_message = await callback_query.message.reply("Выберите пользователя для получения конфигурации:", reply_markup=keyboard)
        push_navigation_state(callback_query.from_user.id, 'main', sent_message.message_id, sent_message.chat.id)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('send_config_'))
async def send_user_config(callback_query: types.CallbackQuery):
    _, username = callback_query.data.split('send_config_', 1)
    username = username.strip()
    sent_messages = []
    try:
        user_dir = os.path.join('users', username)
        conf_path = os.path.join(user_dir, f'{username}.conf')
        if not os.path.exists(conf_path):
            await callback_query.answer("Конфигурационный файл пользователя отсутствует. Возможно, пользователь был создан вручную, и его конфигурация недоступна.", show_alert=True)
            return
        if os.path.exists(conf_path):
            vpn_key = await generate_vpn_key(conf_path)
            if vpn_key:
                instruction_text = (
                    "\nAmneziaVPN [Google Play](https://play.google.com/store/apps/details?id=org.amnezia.vpn&hl=ru), "
                    "[GitHub](https://github.com/amnezia-vpn/amnezia-client)"
                )
                formatted_key = format_vpn_key(vpn_key)
                key_message = f"```\n{formatted_key}\n```"
                caption = f"{instruction_text}\n{key_message}"
            else:
                caption = "VPN ключ не был сгенерирован."
            with open(conf_path, 'rb') as config:
                sent_doc = await bot.send_document(
                    callback_query.from_user.id,
                    config,
                    caption=caption,
                    parse_mode="Markdown",
                    disable_notification=True
                )
                sent_messages.append(sent_doc.message_id)
        else:
            confirmation_text = f"Не удалось создать конфигурацию для пользователя **{username}**."
            sent_message = await bot.send_message(callback_query.from_user.id, confirmation_text, parse_mode="Markdown", disable_notification=True)
            asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_message.message_id, delay=15))
            await callback_query.answer()
            return
    except Exception as e:
        confirmation_text = f"Произошла ошибка: {e}"
        sent_message = await bot.send_message(callback_query.from_user.id, confirmation_text, parse_mode="Markdown", disable_notification=True)
        asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_message.message_id, delay=15))
        await callback_query.answer()
        return
    if not sent_messages:
        confirmation_text = f"Не удалось найти файлы конфигурации для пользователя **{username}**."
        sent_message = await bot.send_message(callback_query.from_user.id, confirmation_text, parse_mode="Markdown", disable_notification=True)
        asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_message.message_id, delay=15))
        await callback_query.answer()
        return
    else:
        confirmation_text = f"Конфигурация для **{username}** отправлена."
        sent_confirmation = await bot.send_message(
            chat_id=callback_query.from_user.id,
            text=confirmation_text,
            parse_mode="Markdown",
            disable_notification=True
        )
        asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_confirmation.message_id, delay=15))
    for message_id in sent_messages:
        asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, message_id, delay=15))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == 'create_backup')
async def create_backup_callback(callback_query: types.CallbackQuery):
    date_str = datetime.now().strftime('%Y-%m-%d')
    backup_filename = f"backup_{date_str}.zip"
    backup_filepath = os.path.join(os.getcwd(), backup_filename)
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, create_zip, backup_filepath)
        if os.path.exists(backup_filepath):
            with open(backup_filepath, 'rb') as f:
                await bot.send_document(callback_query.from_user.id, f, caption=backup_filename, disable_notification=True)
            os.remove(backup_filepath)
        else:
            logger.error(f"Бекап файл не создан: {backup_filepath}")
            await bot.send_message(callback_query.from_user.id, "Не удалось создать бекап.", disable_notification=True)
    except Exception as e:
        logger.error(f"Ошибка при создании бекапа: {e}")
        await bot.send_message(callback_query.from_user.id, "Не удалось создать бекап.", disable_notification=True)
    await callback_query.answer()

def parse_traffic_limit(traffic_limit: str) -> int:
    mapping = {'B':1, 'KB':10**3, 'MB':10**6, 'GB':10**9, 'TB':10**12}
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)$', traffic_limit, re.IGNORECASE)
    if match:
        value = float(match.group(1))
        unit = match.group(2).upper()
        return int(value * mapping.get(unit, 1))
    else:
        return None

@dp.callback_query_handler(lambda c: c.data.startswith('buy_vpn'))
async def buy_vpn_callback(callback_query: types.CallbackQuery):
    keyboard = get_menu_markup(callback_query.from_user.id, 'buy_vpn')
    await callback_query.message.edit_text(
        "Выберите период подписки:",
        reply_markup=keyboard
    )
    push_navigation_state(callback_query.from_user.id, 'buy_vpn', callback_query.message.message_id, callback_query.message.chat.id)

@dp.callback_query_handler(lambda c: c.data.startswith('payment_'))
async def handle_payment(callback_query: types.CallbackQuery):
    period = callback_query.data.replace('payment_', '')
    payment_url = await create_payment(callback_query.from_user.id, period)
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("Оплатить", url=payment_url),
        InlineKeyboardButton("Проверить оплату", callback_data=f"check_payment_{period}"),
        InlineKeyboardButton("« Назад", callback_data="back")
    )
    
    await callback_query.message.edit_text(
        "Для оплаты нажмите кнопку ниже. После оплаты нажмите 'Проверить оплату' "
        "для получения вашего VPN ключа.",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('check_payment_'))
async def check_payment_status(callback_query: types.CallbackQuery):
    period = callback_query.data.replace('check_payment_', '')
    user_id = callback_query.from_user.id
    
    # Get user's latest payment
    payments = db.get_user_payments(user_id)
    if not payments:
        await callback_query.answer("Платеж не найден", show_alert=True)
        return

    latest_payment = payments[-1]
    payment_id = latest_payment['payment_id']
    
    try:
        # Check payment status in YooKassa
        payment = Payment.find_one(payment_id)
        
        if payment.status == 'succeeded':
            # Update payment status in database
            db.update_payment_status(payment_id, 'succeeded')
            
            # Generate VPN key for user
            client_name = f"user_{user_id}"
            try:
                vpn_key = await generate_vpn_key(client_name)
                keyboard = InlineKeyboardMarkup()
                keyboard.add(
                    InlineKeyboardButton("« В главное меню", callback_data="return_home")
                )
                
                await callback_query.message.edit_text(
                    f"Оплата успешна! Ваш VPN ключ:\n\n{format_vpn_key(vpn_key)}\n\n"
                    "Для настройки VPN скопируйте этот ключ и следуйте инструкции в приложении Amnezia VPN.",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Error generating VPN key: {e}")
                await callback_query.answer(
                    "Произошла ошибка при генерации ключа. Пожалуйста, обратитесь в поддержку.",
                    show_alert=True
                )
        elif payment.status == 'pending':
            await callback_query.answer(
                "Оплата еще не поступила. Пожалуйста, подождите или попробуйте позже.",
                show_alert=True
            )
        else:
            await callback_query.answer(
                f"Статус платежа: {payment.status}. Попробуйте оплатить снова.",
                show_alert=True
            )
    except Exception as e:
        logger.error(f"Error checking payment status: {e}")
        await callback_query.answer(
            "Произошла ошибка при проверке платежа. Попробуйте позже.",
            show_alert=True
        )

@dp.callback_query_handler(lambda c: c.data == 'my_vpn_key')
async def my_vpn_key_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    payments = db.get_user_payments(user_id)
    active_payments = [p for p in payments if p['status'] == 'succeeded']
    
    if not active_payments:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("Купить VPN", callback_data="buy_vpn"))
        keyboard.add(InlineKeyboardButton("« Назад", callback_data="return_home"))
        await callback_query.message.edit_text(
            "У вас нет активного VPN ключа. Для получения ключа необходимо приобрести подписку.",
            reply_markup=keyboard
        )
        return

    # Get or generate VPN key
    client_name = f"user_{user_id}"
    vpn_key = None
    try:
        vpn_key = await generate_vpn_key(client_name)
    except Exception as e:
        logger.error(f"Error generating VPN key: {e}")
        await callback_query.message.edit_text(
            "Произошла ошибка при генерации ключа. Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("« Назад", callback_data="return_home")
            )
        )
        return

    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Обновить ключ", callback_data="regenerate_key"),
        InlineKeyboardButton("Удалить ключ", callback_data="delete_key"),
        InlineKeyboardButton("« Назад", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        f"Ваш VPN ключ:\n\n{format_vpn_key(vpn_key)}\n\n"
        "Для настройки VPN скопируйте этот ключ и следуйте инструкции в приложении Amnezia VPN.",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == 'payment_history')
async def payment_history_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("Доступ запрещен")
        return
        
    payments = db.get_all_payments()
    message_text = "История платежей:\n\n"
    
    for user_id, user_payments in payments.items():
        for payment in user_payments:
            timestamp = datetime.fromisoformat(payment['timestamp'])
            message_text += (
                f"Пользователь: {user_id}\n"
                f"ID платежа: {payment['payment_id']}\n"
                f"Сумма: {payment['amount']} RUB\n"
                f"Статус: {payment['status']}\n"
                f"Дата: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
    
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("« Назад", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        message_text if message_text != "История платежей:\n\n" else "История платежей пуста",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == 'mass_message')
async def mass_message_prompt(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("Доступ запрещен")
        return
        
    await callback_query.message.edit_text(
        "Отправьте сообщение, которое нужно разослать всем пользователям:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("« Назад", callback_data="return_home")
        )
    )
    
    # Set state for next message
    user_states[callback_query.from_user.id] = "waiting_for_mass_message"

async def process_mass_message(message: types.Message):
    if message.from_user.id != admin:
        return
        
    # Get all unique user IDs from payments
    payments = db.get_all_payments()
    user_ids = set(int(user_id) for user_id in payments.keys())
    
    sent_count = 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, message.text)
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to send message to user {user_id}: {e}")
    
    await message.reply(
        f"Сообщение отправлено {sent_count} пользователям",
        reply_markup=get_menu_markup(message.from_user.id, 'main')
    )
    
    # Clear state
    user_states.pop(message.from_user.id, None)

# Update message handler to handle mass messaging
@dp.message_handler()
async def handle_messages(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    
    if state == "waiting_for_mass_message":
        await process_mass_message(message)
        return
        
    # ... rest of the existing handle_messages function ...

# Webhook handler for YooKassa payment notifications
async def handle_payment_notification(request):
    try:
        payment_data = await request.json()
        payment = Payment.find_one(payment_data['object']['id'])
        
        if payment.status == 'succeeded':
            user_id = payment.metadata.get('user_id')
            period = payment.metadata.get('period')
            
            # Update payment status in database
            db.update_payment_status(payment.id, 'succeeded')
            
            # Generate VPN key for user
            client_name = f"user_{user_id}"
            try:
                vpn_key = await generate_vpn_key(client_name)
                # Send VPN key to user
                await bot.send_message(
                    user_id,
                    f"Спасибо за оплату! Ваш VPN ключ:\n\n{format_vpn_key(vpn_key)}\n\n"
                    "Для настройки VPN скопируйте этот ключ и следуйте инструкции в приложении Amnezia VPN."
                )
            except Exception as e:
                logger.error(f"Error generating VPN key after payment: {e}")
                await bot.send_message(
                    user_id,
                    "Произошла ошибка при генерации ключа. Пожалуйста, обратитесь в поддержку."
                )
                
    except Exception as e:
        logger.error(f"Error processing payment notification: {e}")
        return web.Response(status=500)
        
    return web.Response(status=200)

executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)
