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
from payment import PaymentManager, LICENSE_PRICES
import random
import string
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.fsm_storage.redis import RedisStorage2
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

setting = db.get_config()
bot_token = setting.get('bot_token')
admin_id = setting.get('admin_id')
wg_config_file = setting.get('wg_config_file')
docker_container = setting.get('docker_container')
endpoint = setting.get('endpoint')
yoomoney_token = setting.get('yoomoney_token')
yoomoney_shop_id = setting.get('yoomoney_shop_id')
yoomoney_secret_key = setting.get('yoomoney_secret_key')

if not all([bot_token, admin_id, wg_config_file, docker_container, endpoint, yoomoney_token, yoomoney_shop_id, yoomoney_secret_key]):
    logger.error("Некоторые обязательные настройки отсутствуют в конфигурационном файле.")
    sys.exit(1)

bot = Bot(bot_token)
admin = int(admin_id)
WG_CONFIG_FILE = wg_config_file
DOCKER_CONTAINER = docker_container
ENDPOINT = endpoint

# Initialize payment manager with all required parameters
payment_manager = PaymentManager(
    token=setting.get('yoomoney_token'),
    shop_id=setting.get('yoomoney_shop_id'),
    secret_key=setting.get('yoomoney_secret_key')
)

class AdminMessageDeletionMiddleware(BaseMiddleware):
    async def on_process_message(self, message: types.Message, data: dict):
        if message.from_user.id == admin:
            asyncio.create_task(delete_message_after_delay(message.chat.id, message.message_id, delay=2))

dp = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=pytz.UTC)
scheduler.start()

dp.middleware.setup(AdminMessageDeletionMiddleware())

main_menu_markup = InlineKeyboardMarkup(row_width=1).add(
    InlineKeyboardButton("Добавить пользователя", callback_data="add_user"),
    InlineKeyboardButton("Получить конфигурацию пользователя", callback_data="get_config"),
    InlineKeyboardButton("Список клиентов", callback_data="list_users"),
    InlineKeyboardButton("Payment History", callback_data="payment_history"),
    InlineKeyboardButton("Создать бекап", callback_data="create_backup"),
    InlineKeyboardButton("Отправить сообщение всем", callback_data="broadcast_message")
)

user_menu_markup = InlineKeyboardMarkup(row_width=1).add(
    InlineKeyboardButton("Buy License", callback_data="buy_license"),
    InlineKeyboardButton("My License Info", callback_data="license_info"),
    InlineKeyboardButton("Regenerate Key", callback_data="regenerate_key"),
    InlineKeyboardButton("Delete License", callback_data="delete_license")
)

user_main_messages = {}
isp_cache = {}
ISP_CACHE_FILE = 'files/isp_cache.json'
CACHE_TTL = timedelta(hours=24)

TRAFFIC_LIMITS = ["5 GB", "10 GB", "30 GB", "100 GB", "Неограниченно"]

class BroadcastStates(StatesGroup):
    waiting_for_message = State()

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
    if message.chat.id == admin:
        sent_message = await message.answer("Выберите действие:", reply_markup=main_menu_markup)
        user_main_messages[admin] = {'chat_id': sent_message.chat.id, 'message_id': sent_message.message_id}
        try:
            await bot.pin_chat_message(chat_id=message.chat.id, message_id=sent_message.message_id, disable_notification=True)
        except:
            pass
    else:
        sent_message = await message.answer(
            "Добро пожаловать! Для использования VPN сервиса необходимо приобрести лицензию.",
            reply_markup=user_menu_markup
        )

@dp.message_handler()
async def handle_messages(message: types.Message):
    if message.chat.id == admin:
        user_state = user_main_messages.get(admin, {}).get('state')
        if user_state == 'waiting_for_user_name':
            user_name = message.text.strip()
            if not all(c.isalnum() or c in "-_" for c in user_name):
                await message.reply("Имя пользователя может содержать только буквы, цифры, дефисы и подчёркивания.")
                return
            user_main_messages[admin]['client_name'] = user_name
            user_main_messages[admin]['state'] = 'waiting_for_duration'
            duration_buttons = [
                InlineKeyboardButton("1 час", callback_data=f"duration_1h_{user_name}_noipv6"),
                InlineKeyboardButton("1 день", callback_data=f"duration_1d_{user_name}_noipv6"),
                InlineKeyboardButton("1 неделя", callback_data=f"duration_1w_{user_name}_noipv6"),
                InlineKeyboardButton("1 месяц", callback_data=f"duration_1m_{user_name}_noipv6"),
                InlineKeyboardButton("Без ограничений", callback_data=f"duration_unlimited_{user_name}_noipv6"),
                InlineKeyboardButton("Домой", callback_data="home")
            ]
            duration_markup = InlineKeyboardMarkup(row_width=1).add(*duration_buttons)
            await message.answer("Выберите длительность:", reply_markup=duration_markup)
    else:
        # Handle regular user messages if needed
        pass

@dp.callback_query_handler(lambda c: c.data.startswith('add_user'))
async def prompt_for_user_name(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text="Введите имя пользователя для добавления:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("Домой", callback_data="home")
            )
        )
        user_main_messages[admin]['state'] = 'waiting_for_user_name'
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
    if callback.from_user.id != admin:
        await callback.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    parts = callback.data.split('_')
    if len(parts) < 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    duration_choice = parts[1]
    client_name = parts[2]
    ipv6_flag = parts[3]
    user_main_messages[admin]['duration_choice'] = duration_choice
    user_main_messages[admin]['state'] = 'waiting_for_traffic_limit'
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
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
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
    user_main_messages[admin]['traffic_limit'] = traffic_limit
    user_main_messages[admin]['state'] = None
    duration_choice = user_main_messages.get(admin, {}).get('duration_choice')
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
                        admin,
                        config,
                        caption=caption,
                        parse_mode="Markdown",
                        disable_notification=True
                    )
                    asyncio.create_task(delete_message_after_delay(admin, sent_doc.message_id, delay=15))
        except FileNotFoundError:
            confirmation_text = "Не удалось найти файлы конфигурации для указанного пользователя."
            sent_message = await bot.send_message(admin, confirmation_text, parse_mode="Markdown", disable_notification=True)
            asyncio.create_task(delete_message_after_delay(admin, sent_message.message_id, delay=15))
            await callback_query.answer()
            return
        except Exception as e:
            logger.error(f"Ошибка при отправке конфигурации: {e}")
            confirmation_text = "Произошла ошибка."
            sent_message = await bot.send_message(admin, confirmation_text, parse_mode="Markdown", disable_notification=True)
            asyncio.create_task(delete_message_after_delay(admin, sent_message.message_id, delay=15))
            await callback_query.answer()
            return
        sent_confirmation = await bot.send_message(
            chat_id=admin,
            text=confirmation_text,
            parse_mode="Markdown",
            disable_notification=True
        )
        asyncio.create_task(delete_message_after_delay(admin, sent_confirmation.message_id, delay=15))
    else:
        confirmation_text = "Не удалось добавить пользователя."
        sent_confirmation = await bot.send_message(
            chat_id=admin,
            text=confirmation_text,
            parse_mode="Markdown",
            disable_notification=True
        )
        asyncio.create_task(delete_message_after_delay(admin, sent_confirmation.message_id, delay=15))
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text="Выберите действие:",
            reply_markup=main_menu_markup
        )
    else:
        await callback_query.answer("Выберите действие:", show_alert=True)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('client_'))
async def client_selected_callback(callback_query: types.CallbackQuery):
    _, username = callback_query.data.split('client_', 1)
    username = username.strip()
    clients = db.get_client_list()
    client_info = next((c for c in clients if c[0] == username), None)
    if not client_info:
        await callback_query.answer("Ошибка: пользователь не найден.", show_alert=True)
        return
    expiration_time = db.get_user_expiration(username)
    traffic_limit = db.get_user_traffic_limit(username)
    status = "🔴 Офлайн"
    incoming_traffic = "↓—"
    outgoing_traffic = "↑—"
    ipv4_address = "—"
    total_bytes = 0
    formatted_total = "0.00B"
    active_clients = db.get_active_list()
    active_info = next((ac for ac in active_clients if ac[0] == username), None)
    if active_info:
        last_handshake_str = active_info[1]
        if last_handshake_str.lower() not in ['never', 'нет данных', '-']:
            try:
                last_handshake_dt = parse_relative_time(last_handshake_str)
                if last_handshake_dt:
                    delta = datetime.now(pytz.UTC) - last_handshake_dt
                    if delta <= timedelta(minutes=1):
                        status = "🟢 Онлайн"
                    else:
                        status = "❌ Офлайн"
                    transfer = active_info[2]
                    incoming_bytes, outgoing_bytes = parse_transfer(transfer)
                    incoming_traffic = f"↓{humanize_bytes(incoming_bytes)}"
                    outgoing_traffic = f"↑{humanize_bytes(outgoing_bytes)}"
                    traffic_data = await update_traffic(username, incoming_bytes, outgoing_bytes)
                    total_bytes = traffic_data.get('total_incoming', 0) + traffic_data.get('total_outgoing', 0)
                    formatted_total = humanize_bytes(total_bytes)
                    if traffic_limit != "Неограниченно":
                        limit_bytes = parse_traffic_limit(traffic_limit)
                        if total_bytes >= limit_bytes:
                            await deactivate_user(username)
                            await callback_query.answer(f"Пользователь **{username}** превысил лимит трафика и был удален.", show_alert=True)
                            return
            except ValueError:
                logger.error(f"Некорректный формат даты для пользователя {username}: {last_handshake_str}")
                status = "❌ Офлайн"
    else:
        traffic_data = await read_traffic(username)
        total_bytes = traffic_data.get('total_incoming', 0) + traffic_data.get('total_outgoing', 0)
        formatted_total = humanize_bytes(total_bytes)
    allowed_ips = client_info[2]
    ipv4_match = re.search(r'(\d{1,3}\.){3}\d{1,3}/\d+', allowed_ips)
    if ipv4_match:
        ipv4_address = ipv4_match.group(0)
    else:
        ipv4_address = "—"
    if expiration_time:
        now = datetime.now(pytz.UTC)
        try:
            expiration_dt = expiration_time
            if expiration_dt.tzinfo is None:
                expiration_dt = expiration_dt.replace(tzinfo=pytz.UTC)
            remaining = expiration_dt - now
            if remaining.total_seconds() > 0:
                days, seconds = remaining.days, remaining.seconds
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                date_end = f"📅 {days}д {hours}ч {minutes}м"
            else:
                date_end = "📅 ♾️ Неограниченно"
        except Exception as e:
            logger.error(f"Ошибка при обработке даты окончания: {e}")
            date_end = "📅 ♾️ Неограниченно"
    else:
        date_end = "📅 ♾️ Неограниченно"
    if traffic_limit == "Неограниченно":
        traffic_limit_display = "♾️ Неограниченно"
    else:
        traffic_limit_display = traffic_limit
    text = (
	f"📧 *Имя:* {username}\n"
        f"🌐 *IPv4:* {ipv4_address}\n"
        f"🌐 *Статус соединения:* {status}\n"
        f"{date_end}\n"
        f"🔼 *Исходящий трафик:* {incoming_traffic}\n"
        f"🔽 *Входящий трафик:* {outgoing_traffic}\n"
        f"📊 *Всего:* ↑↓{formatted_total} из **{traffic_limit_display}**\n"
    )
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("IP info", callback_data=f"ip_info_{username}"),
        InlineKeyboardButton("Подключения", callback_data=f"connections_{username}")
    )
    keyboard.add(
        InlineKeyboardButton("Удалить", callback_data=f"delete_user_{username}")
    )
    keyboard.add(
        InlineKeyboardButton("Назад", callback_data="list_users"),
        InlineKeyboardButton("Домой", callback_data="home")
    )
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
    if main_chat_id and main_message_id:
        try:
            await bot.edit_message_text(
                chat_id=main_chat_id,
                message_id=main_message_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения: {e}")
            await callback_query.answer("Ошибка при обновлении сообщения.", show_alert=True)
    else:
        await callback_query.answer("Ошибка: главное сообщение не найдено.", show_alert=True)
        return
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('list_users'))
async def list_users_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    clients = db.get_client_list()
    if not clients:
        await callback_query.answer("Список пользователей пуст.", show_alert=True)
        return
    active_clients = db.get_active_list()
    active_clients_dict = {}
    for client in active_clients:
        username = client[0]
        last_handshake = client[1]
        active_clients_dict[username] = last_handshake
    keyboard = InlineKeyboardMarkup(row_width=2)
    now = datetime.now(pytz.UTC)
    for client in clients:
        username = client[0]
        last_handshake_str = active_clients_dict.get(username)
        if last_handshake_str and last_handshake_str.lower() not in ['never', 'нет данных', '-']:
            try:
                last_handshake_dt = parse_relative_time(last_handshake_str)
                if last_handshake_dt:
                    delta = now - last_handshake_dt
                    delta_days = delta.days
                    if delta_days <= 5:
                        status_display = f"🟢({delta_days}d) {username}"
                    else:
                        status_display = f"❌(?d) {username}"
                else:
                    status_display = f"❌(?d) {username}"
            except ValueError:
                logger.error(f"Некорректный формат даты для пользователя {username}: {last_handshake_str}")
                status_display = f"❌(?d) {username}"
        else:
            status_display = f"❌(?d) {username}"
        keyboard.insert(InlineKeyboardButton(status_display, callback_data=f"client_{username}"))
    keyboard.add(InlineKeyboardButton("Домой", callback_data="home"))
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
    if main_chat_id and main_message_id:
        try:
            await bot.edit_message_text(
                chat_id=main_chat_id,
                message_id=main_message_id,
                text="Выберите пользователя:",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения: {e}")
            await callback_query.answer("Ошибка при обновлении сообщения.", show_alert=True)
    else:
        sent_message = await callback_query.message.reply("Выберите пользователя:", reply_markup=keyboard)
        user_main_messages[admin] = {'chat_id': sent_message.chat.id, 'message_id': sent_message.message_id}
        try:
            await bot.pin_chat_message(chat_id=sent_message.chat.id, message_id=sent_message.message_id, disable_notification=True)
        except:
            pass
    await callback_query.answer()

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
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
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
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text=confirmation_text,
            parse_mode="Markdown",
            reply_markup=main_menu_markup
        )
    else:
        await callback_query.answer("Ошибка: главное сообщение не найдено.", show_alert=True)
        return
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "return_home")
async def return_home(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    is_admin = user_id == admin
    
    try:
        if is_admin:
            await callback_query.message.edit_text(
                "Выберите действие:",
                reply_markup=main_menu_markup
            )
        else:
            await callback_query.message.edit_text(
                "Выберите действие:",
                reply_markup=user_menu_markup
            )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in return_home: {e}")
        await callback_query.answer("Произошла ошибка при возврате в главное меню", show_alert=True)

@dp.callback_query_handler(lambda c: c.data.startswith('home'))
async def return_home(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
    if main_chat_id and main_message_id:
        user_main_messages[admin].pop('state', None)
        user_main_messages[admin].pop('client_name', None)
        user_main_messages[admin].pop('duration_choice', None)
        user_main_messages[admin].pop('traffic_limit', None)
        try:
            await bot.edit_message_text(
                chat_id=main_chat_id,
                message_id=main_message_id,
                text="Выберите действие:",
                reply_markup=main_menu_markup
            )
        except:
            sent_message = await callback_query.message.reply("Выберите действие:", reply_markup=main_menu_markup)
            user_main_messages[admin] = {'chat_id': sent_message.chat.id, 'message_id': sent_message.message_id}
            try:
                await bot.pin_chat_message(chat_id=sent_message.chat.id, message_id=sent_message.message_id, disable_notification=True)
            except:
                pass
    else:
        sent_message = await callback_query.message.reply("Выберите действие:", reply_markup=main_menu_markup)
        user_main_messages[admin] = {'chat_id': sent_message.chat.id, 'message_id': sent_message.message_id}
        try:
            await bot.pin_chat_message(chat_id=sent_message.chat.id, message_id=sent_message.message_id, disable_notification=True)
        except:
            pass
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == 'get_config')
async def list_users_for_config(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    clients = db.get_client_list()
    if not clients:
        await callback_query.answer("Список пользователей пуст.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(row_width=2)
    for client in clients:
        username = client[0]
        keyboard.insert(InlineKeyboardButton(username, callback_data=f"send_config_{username}"))
    keyboard.add(InlineKeyboardButton("Домой", callback_data="home"))
    main_chat_id = user_main_messages.get(admin, {}).get('chat_id')
    main_message_id = user_main_messages.get(admin, {}).get('message_id')
    if main_chat_id and main_message_id:
        await bot.edit_message_text(
            chat_id=main_chat_id,
            message_id=main_message_id,
            text="Выберите пользователя для получения конфигурации:",
            reply_markup=keyboard
        )
    else:
        sent_message = await callback_query.message.reply("Выберите пользователя для получения конфигурации:", reply_markup=keyboard)
        user_main_messages[admin] = {'chat_id': sent_message.chat.id, 'message_id': sent_message.message_id}
        try:
            await bot.pin_chat_message(chat_id=sent_message.chat.id, message_id=sent_message.message_id, disable_notification=True)
        except:
            pass
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('send_config_'))
async def send_user_config(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
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
                    admin,
                    config,
                    caption=caption,
                    parse_mode="Markdown",
                    disable_notification=True
                )
                sent_messages.append(sent_doc.message_id)
        else:
            confirmation_text = f"Не удалось создать конфигурацию для пользователя **{username}**."
            sent_message = await bot.send_message(admin, confirmation_text, parse_mode="Markdown", disable_notification=True)
            asyncio.create_task(delete_message_after_delay(admin, sent_message.message_id, delay=15))
            await callback_query.answer()
            return
    except Exception as e:
        confirmation_text = f"Произошла ошибка: {e}"
        sent_message = await bot.send_message(admin, confirmation_text, parse_mode="Markdown", disable_notification=True)
        asyncio.create_task(delete_message_after_delay(admin, sent_message.message_id, delay=15))
        await callback_query.answer()
        return
    if not sent_messages:
        confirmation_text = f"Не удалось найти файлы конфигурации для пользователя **{username}**."
        sent_message = await bot.send_message(admin, confirmation_text, parse_mode="Markdown", disable_notification=True)
        asyncio.create_task(delete_message_after_delay(admin, sent_message.message_id, delay=15))
        await callback_query.answer()
        return
    else:
        confirmation_text = f"Конфигурация для **{username}** отправлена."
        sent_confirmation = await bot.send_message(
            chat_id=admin,
            text=confirmation_text,
            parse_mode="Markdown",
            disable_notification=True
        )
        asyncio.create_task(delete_message_after_delay(admin, sent_confirmation.message_id, delay=15))
    for message_id in sent_messages:
        asyncio.create_task(delete_message_after_delay(admin, message_id, delay=15))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('create_backup'))
async def create_backup_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    date_str = datetime.now().strftime('%Y-%m-%d')
    backup_filename = f"backup_{date_str}.zip"
    backup_filepath = os.path.join(os.getcwd(), backup_filename)
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, create_zip, backup_filepath)
        if os.path.exists(backup_filepath):
            with open(backup_filepath, 'rb') as f:
                await bot.send_document(admin, f, caption=backup_filename, disable_notification=True)
            os.remove(backup_filepath)
        else:
            logger.error(f"Бекап файл не создан: {backup_filepath}")
            await bot.send_message(admin, "Не удалось создать бекап.", disable_notification=True)
    except Exception as e:
        logger.error(f"Ошибка при создании бекапа: {e}")
        await bot.send_message(admin, "Не удалось создать бекап.", disable_notification=True)
    await callback_query.answer()

def parse_transfer(transfer_str):
    try:
        if '/' in transfer_str:
            incoming, outgoing = transfer_str.split('/')
            incoming = incoming.strip()
            outgoing = outgoing.strip()
            incoming_match = re.match(r'([\d.]+)\s*(\w+)', incoming)
            outgoing_match = re.match(r'([\d.]+)\s*(\w+)', outgoing)
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
                incoming_match = re.match(r'([\d.]+)\s*(\w+)', incoming)
                outgoing_match = re.match(r'([\d.]+)\s*(\w+)', outgoing)
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

async def handle_user_vpn_access(user_id: int, username: str):
    """Handle VPN access for users based on their license status"""
    license_data = db.get_user_license(username)
    if not license_data:
        return False, "No active license found. Please purchase a license first."
    
    if datetime.now(pytz.UTC) > license_data['expiration']:
        return False, "Your license has expired. Please purchase a new license."
    
    return True, None

@dp.callback_query_handler(lambda c: c.data == 'buy_license')
async def buy_license_callback(callback_query: types.CallbackQuery):
    markup = InlineKeyboardMarkup(row_width=2)
    for plan, price in LICENSE_PRICES.items():
        readable_plan = plan.replace('_', ' ').title()
        button_text = f"{readable_plan} - {price}₽"
        markup.add(InlineKeyboardButton(button_text, callback_data=f"select_plan_{plan}"))
    markup.add(InlineKeyboardButton("« Назад", callback_data="return_home"))
    
    await callback_query.message.edit_text(
        "Выберите тарифный план:\n\n"
        "🔹 1 месяц - Базовый доступ\n"
        "🔹 3 месяца - Скидка 20%\n"
        "🔹 6 месяцев - Скидка 33%\n"
        "🔹 12 месяцев - Скидка 42%\n\n"
        "Все тарифы включают:\n"
        "✓ Безлимитный трафик\n"
        "✓ Высокая скорость\n"
        "✓ Поддержка 24/7",
        reply_markup=markup
    )

@dp.callback_query_handler(lambda c: c.data.startswith('select_plan_'))
async def select_plan_callback(callback_query: types.CallbackQuery):
    plan = callback_query.data.replace('select_plan_', '')
    try:
        payment_id, payment_url = payment_manager.create_payment(callback_query.from_user.id, plan)
        
        markup = InlineKeyboardMarkup().add(
            InlineKeyboardButton("💳 Оплатить", url=payment_url),
            InlineKeyboardButton("✓ Проверить оплату", callback_data=f"check_payment_{payment_id}")
        ).add(
            InlineKeyboardButton("« Назад", callback_data="buy_license")
        )
        
        await callback_query.message.edit_text(
            "🔒 Для активации доступа к VPN выполните следующие шаги:\n\n"
            "1. Нажмите кнопку «Оплатить»\n"
            "2. Выполните оплату\n"
            "3. Вернитесь в бот\n"
            "4. Нажмите «Проверить оплату»\n\n"
            "После успешной оплаты вы получите данные для подключения.",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Payment creation error: {e}")
        await callback_query.answer("Произошла ошибка при создании платежа. Попробуйте позже.")

@dp.callback_query_handler(lambda c: c.data.startswith('check_payment_'))
async def check_payment_callback(callback_query: types.CallbackQuery):
    payment_id = callback_query.data.replace('check_payment_', '')
    user_id = str(callback_query.from_user.id)
    
    try:
        is_paid = payment_manager.check_payment(payment_id)
        if is_paid:
            payment_data = payment_manager.payments.get(payment_id)
            if not payment_data:
                raise Exception("Payment data not found")
                
            plan = payment_data.get('plan')
            if not plan:
                raise Exception("Plan not found in payment data")
            
            # Calculate expiration date based on plan
            duration_map = {
                "1_month": 30,
                "3_months": 90,
                "6_months": 180,
                "12_months": 365
            }
            
            days = duration_map.get(plan)
            if not days:
                raise Exception(f"Invalid plan: {plan}")
                
            expiration_date = datetime.now() + timedelta(days=days)
            
            # Save license information first
            db.set_user_expiration(user_id, expiration_date, "Неограниченно")
            
            success_text = (
                "✅ Оплата успешно произведена!\n\n"
                f"📅 Срок действия: {expiration_date.strftime('%d.%m.%Y')}\n"
                "📊 Трафик: Неограниченно\n\n"
            )
            
            # Try to generate and send VPN config
            try:
                config_data = await generate_vpn_config(user_id)
                if config_data:
                    await send_vpn_config(callback_query.message.chat.id, config_data)
                    success_text += (
                        "⚙️ Конфигурация VPN отправлена выше.\n"
                        "📱 Установите приложение WireGuard для вашей платформы:\n"
                        "• iOS: https://apps.apple.com/app/wireguard/id1441195209\n"
                        "• Android: https://play.google.com/store/apps/details?id=com.wireguard.android\n"
                        "• Windows: https://download.wireguard.com/windows-client/wireguard-installer.exe\n"
                        "• macOS: https://apps.apple.com/app/wireguard/id1451685025"
                    )
                else:
                    success_text += (
                        "⚠️ Не удалось сгенерировать конфигурацию VPN.\n"
                        "Пожалуйста, обратитесь в поддержку или попробуйте позже через меню 'Информация о лицензии'"
                    )
            except Exception as e:
                logger.error(f"Error generating VPN config for user {user_id}: {e}")
                success_text += (
                    "⚠️ Произошла ошибка при генерации конфигурации VPN.\n"
                    "Пожалуйста, обратитесь в поддержку или попробуйте позже через меню 'Информация о лицензии'"
                )
            
            markup = InlineKeyboardMarkup().add(
                InlineKeyboardButton("« Главное меню", callback_data="return_home")
            )
            
            await callback_query.message.edit_text(success_text, reply_markup=markup)
            logger.info(f"Successfully processed payment {payment_id} for user {user_id}")
            
        else:
            payment_status = payment_manager.get_payment_status(payment_id)
            if payment_status in ["canceled", "expired"]:
                status_text = "❌ Платёж был отменен или истек срок ожидания."
            else:
                status_text = "⏳ Платёж еще не подтвержден. Пожалуйста, подождите или попробуйте снова."
            
            markup = InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔄 Проверить снова", callback_data=f"check_payment_{payment_id}"),
                InlineKeyboardButton("« Отмена", callback_data="return_home")
            )
            
            await callback_query.message.edit_text(
                f"{status_text}\n\nID платежа: {payment_id}",
                reply_markup=markup
            )
            
    except Exception as e:
        error_msg = f"Ошибка при проверке платежа: {str(e)}"
        logger.error(f"Payment check error for user {user_id}, payment {payment_id}: {str(e)}")
        await callback_query.answer(error_msg, show_alert=True)
        
        markup = InlineKeyboardMarkup().add(
            InlineKeyboardButton("« Главное меню", callback_data="return_home")
        )
        await callback_query.message.edit_text(
            f"❌ {error_msg}\n\nОбратитесь к администратору.",
            reply_markup=markup
        )
    
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == 'payment_history')
async def payment_history_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("Access denied")
        return
        
    payments = payment_manager.get_all_payments()
    history_text = "Payment History:\n\n"
    
    for payment_id, payment in payments.items():
        history_text += (
            f"User ID: {payment['user_id']}\n"
            f"Plan: {payment['plan']}\n"
            f"Amount: {payment['amount']} RUB\n"
            f"Status: {payment['status']}\n"
            f"Created: {payment['created_at']}\n"
            f"Completed: {payment['completed_at'] or 'Pending'}\n"
            f"-------------------\n"
        )
    
    # Split into chunks if too long
    if len(history_text) > 4096:
        chunks = [history_text[i:i+4096] for i in range(0, len(history_text), 4096)]
        for chunk in chunks:
            await callback_query.message.answer(chunk)
    else:
        await callback_query.message.edit_text(
            history_text,
            reply_markup=main_menu_markup
        )

@dp.callback_query_handler(lambda c: c.data == "broadcast_message")
async def broadcast_message_prompt(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != admin:
        await callback_query.answer("Доступ запрещен")
        return
    
    state = dp.current_state(user=callback_query.from_user.id)
    await state.set_state("waiting_for_broadcast")
    
    markup = InlineKeyboardMarkup().add(
        InlineKeyboardButton("« Отмена", callback_data="return_home")
    )
    
    await callback_query.message.edit_text(
        "Введите сообщение для рассылки всем пользователям:",
        reply_markup=markup
    )

@dp.message_handler(state="waiting_for_broadcast")
async def handle_broadcast_message(message: types.Message):
    if message.from_user.id != admin:
        return
    
    state = dp.current_state(user=message.from_user.id)
    await state.reset_state()
    
    users = db.get_clients_from_clients_table()
    sent_count = 0
    failed_count = 0
    
    for user in users:
        try:
            await bot.send_message(user['clientId'], 
                                 f"📢 Сообщение от администратора:\n\n{message.text}")
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to send broadcast to user {user['clientId']}: {e}")
            failed_count += 1
    
    await message.answer(
        f"✅ Рассылка завершена\n\n"
        f"Отправлено: {sent_count}\n"
        f"Ошибок: {failed_count}",
        reply_markup=main_menu_markup
    )

async def generate_vpn_config(username: str) -> dict:
    """Generate VPN configuration for a user"""
    try:
        # Create VPN configuration using existing scripts
        result = await asyncio.create_subprocess_shell(
            f'./newclient.sh {username}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()
        
        if result.returncode != 0:
            logger.error(f"Error generating VPN config: {stderr.decode()}")
            return None
            
        config_path = f'/etc/wireguard/clients/{username}.conf'
        qr_path = f'/etc/wireguard/clients/{username}_qr.png'
        
        if not os.path.exists(config_path) or not os.path.exists(qr_path):
            logger.error(f"Config files not found after generation")
            return None
            
        return {
            'config_path': config_path,
            'qr_path': qr_path
        }
    except Exception as e:
        logger.error(f"Error in generate_vpn_config: {e}")
        return None

async def send_vpn_config(chat_id: int, config_data: dict):
    """Send VPN configuration file and QR code to user"""
    if not config_data:
        raise Exception("No config data provided")
        
    try:
        # Send configuration file
        async with aiofiles.open(config_data['config_path'], 'rb') as config_file:
            await bot.send_document(
                chat_id,
                types.InputFile(config_file.name),
                caption="📝 Конфигурационный файл WireGuard"
            )
        
        # Send QR code
        async with aiofiles.open(config_data['qr_path'], 'rb') as qr_file:
            await bot.send_photo(
                chat_id,
                types.InputFile(qr_file.name),
                caption="📱 QR-код для быстрой настройки"
            )
    except Exception as e:
        logger.error(f"Error sending VPN config: {e}")
        raise Exception("Failed to send VPN configuration")

@dp.callback_query_handler(lambda c: c.data == "license_info")
async def license_info_callback(callback_query: types.CallbackQuery):
    user_id = str(callback_query.from_user.id)
    try:
        expiration = db.get_user_expiration(user_id)
        if not expiration:
            await callback_query.message.edit_text(
                "❌ У вас нет активной лицензии",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("« Назад", callback_data="return_home")
                )
            )
            return
            
        expiration_date = datetime.fromisoformat(expiration)
        days_left = (expiration_date - datetime.now()).days
        
        info_text = (
            "📋 Информация о лицензии:\n\n"
            f"📅 Действует до: {expiration_date.strftime('%d.%m.%Y')}\n"
            f"⏳ Осталось дней: {days_left}\n"
            "📊 Трафик: Неограниченно\n"
        )
        
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton("🔄 Обновить конфигурацию", callback_data="regenerate_key"),
            InlineKeyboardButton("❌ Удалить лицензию", callback_data="delete_license"),
            InlineKeyboardButton("« Назад", callback_data="return_home")
        )
        
        await callback_query.message.edit_text(info_text, reply_markup=markup)
    except Exception as e:
        logger.error(f"Error in license_info_callback: {e}")
        await callback_query.answer("Произошла ошибка при получении информации о лицензии", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == "regenerate_key")
async def regenerate_key_callback(callback_query: types.CallbackQuery):
    user_id = str(callback_query.from_user.id)
    try:
        # Проверяем наличие активной лицензии
        if not db.get_user_expiration(user_id):
            await callback_query.answer("У вас нет активной лицензии", show_alert=True)
            return
            
        # Генерируем новую конфигурацию
        config_data = await generate_vpn_config(user_id)
        if not config_data:
            raise Exception("Failed to generate new configuration")
            
        # Отправляем новую конфигурацию
        await send_vpn_config(callback_query.message.chat.id, config_data)
        
        await callback_query.message.edit_text(
            "✅ Новая конфигурация успешно сгенерирована и отправлена выше",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("« Назад", callback_data="return_home")
            )
        )
    except Exception as e:
        logger.error(f"Error in regenerate_key_callback: {e}")
        await callback_query.answer("Произошла ошибка при обновлении конфигурации", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == "delete_license")
async def delete_license_callback(callback_query: types.CallbackQuery):
    user_id = str(callback_query.from_user.id)
    try:
        if not db.get_user_expiration(user_id):
            await callback_query.answer("У вас нет активной лицензии", show_alert=True)
            return
            
        # Удаляем лицензию
        db.remove_user_expiration(user_id)
        
        # Деактивируем пользователя в WireGuard
        await deactivate_user(user_id)
        
        await callback_query.message.edit_text(
            "✅ Лицензия успешно удалена",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("« Назад", callback_data="return_home")
            )
        )
    except Exception as e:
        logger.error(f"Error in delete_license_callback: {e}")
        await callback_query.answer("Произошла ошибка при удалении лицензии", show_alert=True)

@dp.message_handler(commands=['broadcast'])
async def broadcast_command(message: types.Message):
    """Start broadcast message creation"""
    await BroadcastStates.waiting_for_message.set()
    await message.reply(
        "📢 Введите сообщение для рассылки\n"
        "Поддерживается HTML-форматирование\n"
        "Для отмены используйте /cancel",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message_handler(state=BroadcastStates.waiting_for_message)
async def process_broadcast_message(message: types.Message, state: FSMContext):
    """Process broadcast message and start sending"""
    if message.text == '/cancel':
        await state.finish()
        await message.reply("Рассылка отменена", reply_markup=get_admin_keyboard())
        return
        
    try:
        # Store the message
        await state.update_data(broadcast_message=message.text)
        
        # Ask for confirmation
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_broadcast"),
            InlineKeyboardButton("❌ Отменить", callback_data="cancel_broadcast")
        )
        
        preview = f"📢 Предпросмотр сообщения:\n\n{message.text}"
        await message.reply(preview, reply_markup=markup, parse_mode=types.ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Error in process_broadcast_message: {e}")
        await message.reply("Произошла ошибка при подготовке рассылки")
        await state.finish()

@dp.callback_query_handler(lambda c: c.data in ["confirm_broadcast", "cancel_broadcast"], state=BroadcastStates.waiting_for_message)
async def broadcast_confirmation(callback_query: types.CallbackQuery, state: FSMContext):
    """Handle broadcast confirmation"""
    if callback_query.data == "cancel_broadcast":
        await state.finish()
        await callback_query.message.edit_text("Рассылка отменена")
        return
        
    try:
        # Get the message
        data = await state.get_data()
        message_text = data['broadcast_message']
        
        # Get all users
        users = db.get_all_users()
        total_users = len(users)
        
        if not users:
            await callback_query.message.edit_text("❌ Нет пользователей для рассылки")
            await state.finish()
            return
            
        # Start progress message
        progress_message = await callback_query.message.edit_text(
            "⏳ Начинаем рассылку...\n"
            f"Всего пользователей: {total_users}"
        )
        
        # Send messages
        success_count = 0
        error_count = 0
        
        for i, user_id in enumerate(users, 1):
            try:
                await bot.send_message(
                    user_id,
                    message_text,
                    parse_mode=types.ParseMode.HTML
                )
                success_count += 1
            except Exception as e:
                logger.error(f"Error sending broadcast to {user_id}: {e}")
                error_count += 1
                
            # Update progress every 10 users
            if i % 10 == 0:
                await progress_message.edit_text(
                    f"⏳ Отправлено: {i}/{total_users}\n"
                    f"✅ Успешно: {success_count}\n"
                    f"❌ Ошибок: {error_count}"
                )
                
        # Final status
        await progress_message.edit_text(
            "✅ Рассылка завершена\n\n"
            f"📊 Статистика:\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"✅ Успешно доставлено: {success_count}\n"
            f"❌ Ошибок доставки: {error_count}"
        )
        
    except Exception as e:
        logger.error(f"Error in broadcast_confirmation: {e}")
        await callback_query.message.edit_text("❌ Произошла ошибка при выполнении рассылки")
        
    finally:
        await state.finish()

executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)
