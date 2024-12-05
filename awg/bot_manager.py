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

def get_main_menu_markup(user_id):
    markup = InlineKeyboardMarkup(row_width=1)
    if user_id == admin:
        markup.add(
            InlineKeyboardButton("Добавить пользователя", callback_data="add_user"),
            InlineKeyboardButton("Получить конфигурацию пользователя", callback_data="get_config"),
            InlineKeyboardButton("Список клиентов", callback_data="list_users"),
            InlineKeyboardButton("Создать бекап", callback_data="create_backup"),
            InlineKeyboardButton("История платежей", callback_data="payment_history"),
            InlineKeyboardButton("Отправить сообщение всем", callback_data="mass_message")
        )
    else:
        markup.add(
            InlineKeyboardButton("Купить VPN", callback_data="buy_vpn"),
            InlineKeyboardButton("Мой VPN ключ", callback_data="my_vpn_key"),
            InlineKeyboardButton("Помощь", callback_data="help")
        )
    return markup

user_main_messages = {}
isp_cache = {}
ISP_CACHE_FILE = 'files/isp_cache.json'
CACHE_TTL = timedelta(hours=24)

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

def create_zip(backup_filepath):
    with zipfile.ZipFile(backup_filepath, 'w') as zipf:
        for main_file in ['awg-decode.py', 'newclient.sh', 'removeclient.sh']:
            if os.path.exists(main_file):
                zipf.write(main_file, main_file)
        for root, dirs, files in os.walk('files'):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, os.getcwd())
                zipf.write(filepath, arcname)
        for root, dirs, files in os.walk('users'):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, os.getcwd())
                zipf.write(filepath, arcname)

async def delete_message_after_delay(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except:
        pass

def parse_relative_time(relative_str: str) -> datetime:
    try:
        parts = relative_str.lower().replace(' ago', '').split(', ')
        delta = timedelta()
        for part in parts:
            number, unit = part.split(' ')
            number = int(number)
            if 'minute' in unit:
                delta += timedelta(minutes=number)
            elif 'second' in unit:
                delta += timedelta(seconds=number)
            elif 'hour' in unit:
                delta += timedelta(hours=number)
            elif 'day' in unit:
                delta += timedelta(days=number)
            elif 'week' in unit:
                delta += timedelta(weeks=number)
            elif 'month' in unit:
                delta += timedelta(days=30 * number)
            elif 'year' in unit:
                delta += timedelta(days=365 * number)
        return datetime.now(pytz.UTC) - delta
    except Exception as e:
        logger.error(f"Ошибка при парсинге относительного времени '{relative_str}': {e}")
        return None

@dp.message_handler(commands=['start', 'help'])
async def help_command_handler(message: types.Message):
    markup = get_main_menu_markup(message.from_user.id)
    sent_message = await message.answer("Выберите действие:", reply_markup=markup)
    user_main_messages[message.from_user.id] = {'chat_id': sent_message.chat.id, 'message_id': sent_message.message_id}
    try:
        await bot.pin_chat_message(chat_id=message.chat.id, message_id=sent_message.message_id, disable_notification=True)
    except:
        pass

@dp.message_handler()
async def handle_messages(message: types.Message):
    user_state = user_main_messages.get(message.from_user.id, {}).get('state')
    if user_state == 'waiting_for_user_name':
        user_name = message.text.strip()
        if not all(c.isalnum() or c in "-_" for c in user_name):
            await message.reply("Имя пользователя может содержать только буквы, цифры, дефисы и подчёркивания.")
            asyncio.create_task(delete_message_after_delay(message.chat.id, message.message_id, delay=2))
            return
        user_main_messages[message.from_user.id]['client_name'] = user_name
        user_main_messages[message.from_user.id]['state'] = 'waiting_for_duration'
        duration_buttons = [
            InlineKeyboardButton("1 час", callback_data=f"duration_1h_{user_name}_noipv6"),
            InlineKeyboardButton("1 день", callback_data=f"duration_1d_{user_name}_noipv6"),
            InlineKeyboardButton("1 неделя", callback_data=f"duration_1w_{user_name}_noipv6"),
            InlineKeyboardButton("1 месяц", callback_data=f"duration_1m_{user_name}_noipv6"),
            InlineKeyboardButton("Без ограничений", callback_data=f"duration_unlimited_{user_name}_noipv6"),
            InlineKeyboardButton("Домой", callback_data="home")
        ]
        duration_markup = InlineKeyboardMarkup(row_width=1).add(*duration_buttons)
        main_chat_id = user_main_messages[message.from_user.id].get('chat_id')
        main_message_id = user_main_messages[message.from_user.id].get('message_id')
        if main_chat_id and main_message_id:
            await bot.edit_message_text(
                chat_id=main_chat_id,
                message_id=main_message_id,
                text=f"Выберите время действия конфигурации для пользователя **{user_name}**:",
                parse_mode="Markdown",
                reply_markup=duration_markup
            )
        else:
            await message.answer("Ошибка: главное сообщение не найдено.")
    else:
        await message.reply("Неизвестная команда или действие.")
        asyncio.create_task(delete_message_after_delay(message.chat.id, message.message_id, delay=2))

@dp.callback_query_handler(lambda c: c.data.startswith('add_user'))
async def prompt_for_user_name(callback_query: types.CallbackQuery):
    main_chat_id = user_main_messages.get(callback_query.from_user.id, {}).get('chat_id')
    main_message_id = user_main_messages.get(callback_query.from_user.id, {}).get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text="Введите имя пользователя для добавления:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("Домой", callback_data="home")
            )
        )
        user_main_messages[callback_query.from_user.id]['state'] = 'waiting_for_user_name'
    else:
        await callback_query.answer("Ошибка: главное сообщение не найдено.", show_alert=True)
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

@dp.callback_query_handler(lambda c: c.data.startswith('duration_'))
async def set_config_duration(callback: types.CallbackQuery):
    parts = callback.data.split('_')
    if len(parts) < 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    duration_choice = parts[1]
    client_name = parts[2]
    ipv6_flag = parts[3]
    user_main_messages[callback.from_user.id]['duration_choice'] = duration_choice
    user_main_messages[callback.from_user.id]['state'] = 'waiting_for_traffic_limit'
    traffic_buttons = [
        InlineKeyboardButton(limit, callback_data=f"traffic_limit_{limit}_{client_name}")
        for limit in TRAFFIC_LIMITS
    ]
    traffic_markup = InlineKeyboardMarkup(row_width=1).add(*traffic_buttons)
    await bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=f"Выберите лимит трафика для пользователя **{client_name}**:",
        parse_mode="Markdown",
        reply_markup=traffic_markup
    )
    await callback.answer()

def format_vpn_key(vpn_key, num_lines=8):
    line_length = len(vpn_key) // num_lines
    if len(vpn_key) % num_lines != 0:
        line_length += 1
    lines = [vpn_key[i:i+line_length] for i in range(0, len(vpn_key), line_length)]
    formatted_key = '\n'.join(lines)
    return formatted_key

@dp.callback_query_handler(lambda c: c.data.startswith('traffic_limit_'))
async def set_traffic_limit(callback_query: types.CallbackQuery):
    parts = callback_query.data.split('_', 3)
    if len(parts) < 4:
        await callback_query.answer("Некорректные данные.", show_alert=True)
        return
    traffic_limit = parts[2]
    client_name = parts[3]
    traffic_bytes = parse_traffic_limit(traffic_limit)
    if traffic_limit != "Неограниченно" and traffic_bytes is None:
        await callback_query.answer("Некорректный формат лимита трафика.", show_alert=True)
        return
    user_main_messages[callback_query.from_user.id]['traffic_limit'] = traffic_limit
    user_main_messages[callback_query.from_user.id]['state'] = None
    duration_choice = user_main_messages.get(callback_query.from_user.id, {}).get('duration_choice')
    if duration_choice == '1h':
        duration = timedelta(hours=1)
    elif duration_choice == '1d':
        duration = timedelta(days=1)
    elif duration_choice == '1w':
        duration = timedelta(weeks=1)
    elif duration_choice == '1m':
        duration = timedelta(days=30)
    elif duration_choice == 'unlimited':
        duration = None
    else:
        duration = None
    if duration:
        expiration_time = datetime.now(pytz.UTC) + duration
        db.set_user_expiration(client_name, expiration_time, traffic_limit)
        scheduler.add_job(
            deactivate_user,
            trigger=DateTrigger(run_date=expiration_time),
            args=[client_name],
            id=client_name
        )
        confirmation_text = f"Пользователь **{client_name}** добавлен. \nКонфигурация истечет через **{duration_choice}**."
    else:
        db.set_user_expiration(client_name, None, traffic_limit)
        confirmation_text = f"Пользователь **{client_name}** добавлен с неограниченным временем действия."
    if traffic_limit != "Неограниченно":
        confirmation_text += f"\nЛимит трафика: **{traffic_limit}**."
    else:
        confirmation_text += f"\nЛимит трафика: **♾️ Неограниченно**."
    success = db.root_add(client_name, ipv6=False)
    if success:
        try:
            conf_path = os.path.join('users', client_name, f'{client_name}.conf')
            vpn_key = ""
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
            if os.path.exists(conf_path):
                with open(conf_path, 'rb') as config:
                    sent_doc = await bot.send_document(
                        callback_query.from_user.id,
                        config,
                        caption=caption,
                        parse_mode="Markdown",
                        disable_notification=True
                    )
                    asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_doc.message_id, delay=15))
        except FileNotFoundError:
            confirmation_text = "Не удалось найти файлы конфигурации для указанного пользователя."
            sent_message = await bot.send_message(callback_query.from_user.id, confirmation_text, parse_mode="Markdown", disable_notification=True)
            asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_message.message_id, delay=15))
            await callback_query.answer()
            return
        except Exception as e:
            logger.error(f"Ошибка при отправке конфигурации: {e}")
            confirmation_text = "Произошла ошибка."
            sent_message = await bot.send_message(callback_query.from_user.id, confirmation_text, parse_mode="Markdown", disable_notification=True)
            asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_message.message_id, delay=15))
            await callback_query.answer()
            return
        sent_confirmation = await bot.send_message(
            chat_id=callback_query.from_user.id,
            text=confirmation_text,
            parse_mode="Markdown",
            disable_notification=True
        )
        asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_confirmation.message_id, delay=15))
    else:
        confirmation_text = "Не удалось добавить пользователя."
        sent_confirmation = await bot.send_message(
            chat_id=callback_query.from_user.id,
            text=confirmation_text,
            parse_mode="Markdown",
            disable_notification=True
        )
        asyncio.create_task(delete_message_after_delay(callback_query.from_user.id, sent_confirmation.message_id, delay=15))
    main_chat_id = user_main_messages.get(callback_query.from_user.id, {}).get('chat_id')
    main_message_id = user_main_messages.get(callback_query.from_user.id, {}).get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text="Выберите действие:",
            reply_markup=get_main_menu_markup(callback_query.from_user.id)
        )
    else:
        await callback_query.answer("Выберите действие:", show_alert=True)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('client_'))
async def client_selected_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
        
    _, username = callback_query.data.split('client_', 1)
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Получить конфигурацию", callback_data=f"send_config_{username}"),
        InlineKeyboardButton("Удалить пользователя", callback_data=f"delete_{username}"),
        InlineKeyboardButton("« Назад", callback_data="list_users"),
        InlineKeyboardButton("« В главное меню", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        f"Действия с пользователем {username}:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('list_users'))
async def list_users_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return

    clients = db.get_client_list()
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    if not clients:
        keyboard.add(InlineKeyboardButton("« Назад", callback_data="return_home"))
        await callback_query.message.edit_text(
            "Список пользователей пуст.",
            reply_markup=keyboard
        )
        return

    for client in clients:
        username = client[0]
        keyboard.add(InlineKeyboardButton(username, callback_data=f"client_{username}"))
    keyboard.add(InlineKeyboardButton("« Назад", callback_data="return_home"))
    
    await callback_query.message.edit_text(
        "Выберите пользователя:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('delete_'))
async def client_delete_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
        
    _, username = callback_query.data.split('delete_', 1)
    success = await deactivate_user(username)
    
    if success:
        confirmation_text = f"Пользователь {username} успешно удален."
    else:
        confirmation_text = f"Не удалось удалить пользователя {username}."
        
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("« Назад к списку", callback_data="list_users"),
        InlineKeyboardButton("« В главное меню", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        confirmation_text,
        reply_markup=keyboard
    )

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
    main_chat_id = user_main_messages.get(callback_query.from_user.id, {}).get('chat_id')
    main_message_id = user_main_messages.get(callback_query.from_user.id, {}).get('message_id')
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

@dp.callback_query_handler(lambda c: c.data == 'home')
async def return_home(callback_query: types.CallbackQuery):
    try:
        markup = get_main_menu_markup(callback_query.from_user.id)
        await callback_query.message.edit_text(
            "Главное меню:",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Error in return_home: {e}")
        await callback_query.answer("Произошла ошибка при возврате в главное меню", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == 'admin_menu')
async def admin_menu_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
        
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Список пользователей", callback_data="list_users"),
        InlineKeyboardButton("Создать пользователя", callback_data="create_user"),
        InlineKeyboardButton("Создать бэкап", callback_data="create_backup"),
        InlineKeyboardButton("« В главное меню", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        "Меню администратора:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == 'list_users')
async def list_users_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
        
    clients = db.get_client_list()
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    if not clients:
        keyboard.add(InlineKeyboardButton("« Назад", callback_data="admin_menu"))
        await callback_query.message.edit_text(
            "Список пользователей пуст.",
            reply_markup=keyboard
        )
        return

    for client in clients:
        username = client[0]
        keyboard.add(InlineKeyboardButton(username, callback_data=f"client_{username}"))
    keyboard.add(InlineKeyboardButton("« Назад", callback_data="admin_menu"))
    
    await callback_query.message.edit_text(
        "Выберите пользователя:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('client_'))
async def client_selected_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
        
    _, username = callback_query.data.split('client_', 1)
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Получить конфигурацию", callback_data=f"send_config_{username}"),
        InlineKeyboardButton("Удалить пользователя", callback_data=f"delete_{username}"),
        InlineKeyboardButton("« Назад к списку", callback_data="list_users"),
        InlineKeyboardButton("« В главное меню", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        f"Действия с пользователем {username}:",
        reply_markup=keyboard
    )    

@dp.callback_query_handler(lambda c: c.data == 'get_config')
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
    main_chat_id = user_main_messages.get(callback_query.from_user.id, {}).get('chat_id')
    main_message_id = user_main_messages.get(callback_query.from_user.id, {}).get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text="Выберите пользователя для получения конфигурации:",
            reply_markup=keyboard
        )
    else:
        sent_message = await callback_query.message.reply("Выберите пользователя для получения конфигурации:", reply_markup=keyboard)
        user_main_messages[callback_query.from_user.id] = {'chat_id': sent_message.chat.id, 'message_id': sent_message.message_id}
        try:
            await bot.pin_chat_message(chat_id=sent_message.chat.id, message_id=sent_message.message_id, disable_notification=True)
        except:
            pass
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

def parse_transfer(transfer_str):
    try:
        if '/' in transfer_str:
            incoming, outgoing = transfer_str.split('/')
            incoming = incoming.strip()
            outgoing = outgoing.strip()
            incoming_match = re.match(r'^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)$', incoming, re.IGNORECASE)
            outgoing_match = re.match(r'^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)$', outgoing, re.IGNORECASE)
            def convert_to_bytes(value, unit):
                size_map = {
                    'B': 1,
                    'KB': 10**3,
                    'KiB': 1024,
                    'MB': 10**6,
                    'MiB': 1024**2,
                    'GB': 10**9,
                    'GiB': 1024**3,
                }
                return float(value) * size_map.get(unit, 1)
            incoming_bytes = convert_to_bytes(*incoming_match.groups()) if incoming_match else 0
            outgoing_bytes = convert_to_bytes(*outgoing_match.groups()) if outgoing_match else 0
            return incoming_bytes, outgoing_bytes
        else:
            parts = re.split(r'[/,]', transfer_str)
            if len(parts) >= 2:
                incoming = parts[0].strip()
                outgoing = parts[1].strip()
                incoming_match = re.match(r'^(\d+(?:\.\d+)?)\s*(\w+)$', incoming)
                outgoing_match = re.match(r'^(\d+(?:\.\d+)?)\s*(\w+)$', outgoing)
                def convert_to_bytes(value, unit):
                    size_map = {
                        'B': 1,
                        'KB': 10**3,
                        'KiB': 1024,
                        'MB': 10**6,
                        'MiB': 1024**2,
                        'GB': 10**9,
                        'GiB': 1024**3,
                    }
                    return float(value) * size_map.get(unit, 1)
                incoming_bytes = convert_to_bytes(*incoming_match.groups()) if incoming_match else 0
                outgoing_bytes = convert_to_bytes(*outgoing_match.groups()) if outgoing_match else 0
                return incoming_bytes, outgoing_bytes
            else:
                return 0, 0
    except Exception as e:
        logger.error(f"Ошибка при парсинге трафика: {e}")
        return 0, 0

def humanize_bytes(bytes_value):
    return humanize.naturalsize(bytes_value, binary=False)

async def read_traffic(username):
    traffic_file = os.path.join('users', username, 'traffic.json')
    os.makedirs(os.path.dirname(traffic_file), exist_ok=True)
    if not os.path.exists(traffic_file):
        traffic_data = {
            "total_incoming": 0,
            "total_outgoing": 0,
            "last_incoming": 0,
            "last_outgoing": 0
        }
        async with aiofiles.open(traffic_file, 'w') as f:
            await f.write(json.dumps(traffic_data))
        return traffic_data
    else:
        async with aiofiles.open(traffic_file, 'r') as f:
            content = await f.read()
            try:
                traffic_data = json.loads(content)
                return traffic_data
            except json.JSONDecodeError:
                logger.error(f"Ошибка при чтении traffic.json для пользователя {username}. Инициализация заново.")
                traffic_data = {
                    "total_incoming": 0,
                    "total_outgoing": 0,
                    "last_incoming": 0,
                    "last_outgoing": 0
                }
                async with aiofiles.open(traffic_file, 'w') as f_write:
                    await f_write.write(json.dumps(traffic_data))
                return traffic_data

async def update_traffic(username, incoming_bytes, outgoing_bytes):
    traffic_data = await read_traffic(username)
    delta_incoming = incoming_bytes - traffic_data.get('last_incoming', 0)
    delta_outgoing = outgoing_bytes - traffic_data.get('last_outgoing', 0)
    if delta_incoming < 0:
        delta_incoming = 0
    if delta_outgoing < 0:
        delta_outgoing = 0
    traffic_data['total_incoming'] += delta_incoming
    traffic_data['total_outgoing'] += delta_outgoing
    traffic_data['last_incoming'] = incoming_bytes
    traffic_data['last_outgoing'] = outgoing_bytes
    traffic_file = os.path.join('users', username, 'traffic.json')
    async with aiofiles.open(traffic_file, 'w') as f:
        await f.write(json.dumps(traffic_data))
    return traffic_data

async def update_all_clients_traffic():
    logger.info("Начало обновления трафика для всех клиентов.")
    active_clients = db.get_active_list()
    for client in active_clients:
        username = client[0]
        transfer = client[2]
        incoming_bytes, outgoing_bytes = parse_transfer(transfer)
        traffic_data = await update_traffic(username, incoming_bytes, outgoing_bytes)
        logger.info(f"Обновлён трафик для пользователя {username}: Входящий {traffic_data['total_incoming']} B, Исходящий {traffic_data['total_outgoing']} B")
        traffic_limit = db.get_user_traffic_limit(username)
        if traffic_limit != "Неограниченно":
            limit_bytes = parse_traffic_limit(traffic_limit)
            total_bytes = traffic_data.get('total_incoming', 0) + traffic_data.get('total_outgoing', 0)
            if total_bytes >= limit_bytes:
                await deactivate_user(username)
    logger.info("Завершено обновление трафика для всех клиентов.")

async def generate_vpn_key(conf_path: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            'python3.11',
            'awg-decode.py',
            '--encode',
            conf_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(f"awg-decode.py ошибка: {stderr.decode().strip()}")
            return ""
        vpn_key = stdout.decode().strip()
        if vpn_key.startswith('vpn://'):
            return vpn_key
        else:
            logger.error(f"awg-decode.py вернул некорректный формат: {vpn_key}")
            return ""
    except Exception as e:
        logger.error(f"Ошибка при вызове awg-decode.py: {e}")
        return ""

async def deactivate_user(client_name: str):
    success = db.deactive_user_db(client_name)
    if success:
        db.remove_user_expiration(client_name)
        try:
            scheduler.remove_job(job_id=client_name)
        except:
            pass
        user_dir = os.path.join('users', client_name)
        try:
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir)
        except Exception as e:
            logger.error(f"Ошибка при удалении директории для пользователя {client_name}: {e}")
        confirmation_text = f"Конфигурация пользователя **{client_name}** была деактивирована из-за превышения лимита трафика."
        sent_message = await bot.send_message(admin, confirmation_text, parse_mode="Markdown", disable_notification=True)
        asyncio.create_task(delete_message_after_delay(admin, sent_message.message_id, delay=15))
    else:
        sent_message = await bot.send_message(admin, f"Не удалось деактивировать пользователя **{client_name}**.", parse_mode="Markdown", disable_notification=True)
        asyncio.create_task(delete_message_after_delay(admin, sent_message.message_id, delay=15))

async def check_environment():
    try:
        cmd = "docker ps --filter 'name={}' --format '{{{{.Names}}}}'".format(DOCKER_CONTAINER)
        container_names = subprocess.check_output(cmd, shell=True).decode().strip().split('\n')
        if DOCKER_CONTAINER not in container_names:
            logger.error(f"Контейнер Docker '{DOCKER_CONTAINER}' не найден. Необходима инициализация AmneziaVPN.")
            return False
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка при проверке Docker-контейнера: {e}")
        return False
    try:
        cmd = f"docker exec {DOCKER_CONTAINER} test -f {WG_CONFIG_FILE}"
        subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError:
        logger.error(f"Конфигурационный файл WireGuard '{WG_CONFIG_FILE}' не найден в контейнере '{DOCKER_CONTAINER}'. Необходима инициализация AmneziaVPN.")
        return False
    return True

async def periodic_ensure_peer_names():
    db.ensure_peer_names()

async def on_startup(dp):
    os.makedirs('files/connections', exist_ok=True)
    os.makedirs('users', exist_ok=True)
    await load_isp_cache_task()
    environment_ok = await check_environment()
    if not environment_ok:
        logger.error("Необходимо инициализировать AmneziaVPN перед запуском бота.")
        await bot.send_message(admin, "Необходимо инициализировать AmneziaVPN перед запуском бота.")
        await bot.close()
        sys.exit(1)
    if not scheduler.running:
        scheduler.add_job(update_all_clients_traffic, IntervalTrigger(minutes=1))
        scheduler.add_job(periodic_ensure_peer_names, IntervalTrigger(minutes=1))
        scheduler.start()
        logger.info("Планировщик запущен для обновления трафика каждые 5 минут.")
    users = db.get_users_with_expiration()
    for user in users:
        client_name, expiration_time, traffic_limit = user
        if expiration_time:
            try:
                expiration_datetime = datetime.fromisoformat(expiration_time)
            except ValueError:
                logger.error(f"Некорректный формат даты для пользователя {client_name}: {expiration_time}")
                continue
            if expiration_datetime.tzinfo is None:
                expiration_datetime = expiration_datetime.replace(tzinfo=pytz.UTC)
            if expiration_datetime > datetime.now(pytz.UTC):
                scheduler.add_job(
                    deactivate_user,
                    trigger=DateTrigger(run_date=expiration_datetime),
                    args=[client_name],
                    id=client_name
                )
                logger.info(f"Запланирована деактивация пользователя {client_name} на {expiration_datetime}")
            else:
                await deactivate_user(client_name)

async def on_shutdown(dp):
    scheduler.shutdown()
    logger.info("Планировщик остановлен.")

async def create_payment(user_id: int, period: str) -> dict:
    amount = PAYMENT_AMOUNTS[period]
    payment = Payment.create({
        "amount": {
            "value": str(amount),
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": f"https://t.me/AmneziaVPNIZbot"
        },
        "capture": True,
        "description": f"VPN подписка на {period.split('_')[0]} месяц(ев)",
        "metadata": {
            "user_id": user_id,
            "period": period
        }
    })
    
    db.add_payment(user_id, payment.id, amount)
    return payment.confirmation.confirmation_url

@dp.callback_query_handler(lambda c: c.data == 'buy_vpn')
async def buy_vpn_callback(callback_query: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("1 месяц - 500₽", callback_data="pay_1_month"),
        InlineKeyboardButton("3 месяца - 1200₽", callback_data="pay_3_months"),
        InlineKeyboardButton("6 месяцев - 2000₽", callback_data="pay_6_months"),
        InlineKeyboardButton("12 месяцев - 3500₽", callback_data="pay_12_months"),
        InlineKeyboardButton("« Назад", callback_data="return_home")
    )
    await callback_query.message.edit_text(
        "Выберите период подписки:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('pay_'))
async def handle_payment(callback_query: types.CallbackQuery):
    period = callback_query.data.replace('pay_', '')
    payment_url = await create_payment(callback_query.from_user.id, period)
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("Оплатить", url=payment_url),
        InlineKeyboardButton("Проверить оплату", callback_data=f"check_payment_{period}"),
        InlineKeyboardButton("« Назад", callback_data="buy_vpn")
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
                # Получаем параметры из конфига
                config = db.get_config()
                endpoint = config.get('endpoint', '')
                wg_config = config.get('wg_config', '')
                container = config.get('container', '')
                
                # Генерируем ключ
                result = subprocess.run(
                    ['bash', 'awg/newclient.sh', client_name, endpoint, wg_config, container],
                    capture_output=True,
                    text=True
                )
                
                if result.returncode == 0:
                    vpn_key = result.stdout.strip()
                    keyboard = InlineKeyboardMarkup()
                    keyboard.add(
                        InlineKeyboardButton("« В главное меню", callback_data="return_home")
                    )
                    
                    await callback_query.message.edit_text(
                        f"Оплата успешна! Ваш VPN ключ:\n\n{format_vpn_key(vpn_key)}\n\n"
                        "Для настройки VPN скопируйте этот ключ и следуйте инструкции в приложении Amnezia VPN.",
                        reply_markup=keyboard
                    )
                else:
                    logger.error(f"Error generating VPN key: {result.stderr}")
                    raise Exception(f"Failed to generate VPN key: {result.stderr}")
                    
            except Exception as e:
                logger.error(f"Error generating VPN key: {e}")
                keyboard = InlineKeyboardMarkup()
                keyboard.add(
                    InlineKeyboardButton("Попробовать снова", callback_data=f"retry_key_generation_{user_id}"),
                    InlineKeyboardButton("« В главное меню", callback_data="return_home")
                )
                await callback_query.message.edit_text(
                    "Произошла ошибка при генерации ключа. Пожалуйста, попробуйте позже или обратитесь в поддержку.",
                    reply_markup=keyboard
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

@dp.callback_query_handler(lambda c: c.data.startswith('retry_key_generation_'))
async def retry_key_generation(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.replace('retry_key_generation_', ''))
    client_name = f"user_{user_id}"
    
    try:
        # Получаем параметры из конфига
        config = db.get_config()
        endpoint = config.get('endpoint', '')
        wg_config = config.get('wg_config', '')
        container = config.get('container', '')
        
        # Генерируем ключ
        result = subprocess.run(
            ['bash', 'awg/newclient.sh', client_name, endpoint, wg_config, container],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            vpn_key = result.stdout.strip()
            keyboard = InlineKeyboardMarkup()
            keyboard.add(
                InlineKeyboardButton("« В главное меню", callback_data="return_home")
            )
            
            await callback_query.message.edit_text(
                f"Ваш VPN ключ:\n\n{format_vpn_key(vpn_key)}\n\n"
                "Для настройки VPN скопируйте этот ключ и следуйте инструкции в приложении Amnezia VPN.",
                reply_markup=keyboard
            )
        else:
            logger.error(f"Error generating VPN key on retry: {result.stderr}")
            raise Exception(f"Failed to generate VPN key: {result.stderr}")
            
    except Exception as e:
        logger.error(f"Error generating VPN key on retry: {e}")
        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton("Попробовать снова", callback_data=f"retry_key_generation_{user_id}"),
            InlineKeyboardButton("« В главное меню", callback_data="return_home")
        )
        await callback_query.message.edit_text(
            "Произошла ошибка при генерации ключа. Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            reply_markup=keyboard
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
        # Получаем параметры из конфига
        config = db.get_config()
        endpoint = config.get('endpoint', '')
        wg_config = config.get('wg_config', '')
        container = config.get('container', '')
        
        # Генерируем ключ
        result = subprocess.run(
            ['bash', 'awg/newclient.sh', client_name, endpoint, wg_config, container],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            vpn_key = result.stdout.strip()
        else:
            logger.error(f"Error generating VPN key: {result.stderr}")
            raise Exception(f"Failed to generate VPN key: {result.stderr}")
            
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
        reply_markup=get_main_menu_markup(message.from_user.id)
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
                # Получаем параметры из конфига
                config = db.get_config()
                endpoint = config.get('endpoint', '')
                wg_config = config.get('wg_config', '')
                container = config.get('container', '')
                
                # Генерируем ключ
                result = subprocess.run(
                    ['bash', 'awg/newclient.sh', client_name, endpoint, wg_config, container],
                    capture_output=True,
                    text=True
                )
                
                if result.returncode == 0:
                    vpn_key = result.stdout.strip()
                    # Send VPN key to user
                    await bot.send_message(
                        user_id,
                        f"Спасибо за оплату! Ваш VPN ключ:\n\n{format_vpn_key(vpn_key)}\n\n"
                        "Для настройки VPN скопируйте этот ключ и следуйте инструкции в приложении Amnezia VPN."
                    )
                else:
                    logger.error(f"Error generating VPN key: {result.stderr}")
                    raise Exception(f"Failed to generate VPN key: {result.stderr}")
                    
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
