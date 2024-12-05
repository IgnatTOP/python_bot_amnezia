from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.utils.exceptions import MessageNotModified
from datetime import datetime, timedelta
import logging
import db
from navigation import UserStates, AdminStates, Navigation
from services import VPNService, PaymentService, UserService
from typing import Union

logger = logging.getLogger(__name__)

class Handlers:
    def __init__(self, bot, admin_id: int, vpn_service: VPNService, payment_service: PaymentService, user_service: UserService):
        self.bot = bot
        self.admin_id = admin_id
        self.nav = Navigation(admin_id)
        self.vpn_service = vpn_service
        self.payment_service = payment_service
        self.user_service = user_service

    async def start_command(self, message: types.Message, state: FSMContext):
        """Handle /start command"""
        user_id = message.from_user.id
        await state.finish()
        
        if user_id == self.admin_id:
            await AdminStates.ADMIN_MENU.set()
            text = "👋 Добро пожаловать в панель администратора!"
        else:
            await UserStates.MAIN_MENU.set()
            text = (
                "👋 Добро пожаловать в VPN бот!\n\n"
                "🔐 Здесь вы можете:\n"
                "• Купить доступ к VPN\n"
                "• Управлять своим VPN ключом\n"
                "• Получить поддержку"
            )
        
        markup = self.nav.get_main_menu(user_id)
        await message.answer(text, reply_markup=markup)

    async def help_command(self, message: types.Message):
        """Handle /help command"""
        user_id = message.from_user.id
        if user_id == self.admin_id:
            text = (
                "🔧 Команды администратора:\n\n"
                "/start - Главное меню\n"
                "/help - Это сообщение\n"
                "/backup - Создать резервную копию\n"
                "/stats - Статистика системы"
            )
        else:
            text = (
                "ℹ️ Доступные команды:\n\n"
                "/start - Главное меню\n"
                "/help - Это сообщение\n"
                "/status - Статус вашего VPN"
            )
        await message.answer(text)

    async def handle_callback(self, callback: types.CallbackQuery, state: FSMContext):
        """Central callback handler"""
        user_id = callback.from_user.id
        current_state = await state.get_state()
        
        try:
            if callback.data == "return_main":
                await self.return_to_main_menu(callback, state)
                return

            if user_id == self.admin_id:
                await self.handle_admin_callback(callback, state, current_state)
            else:
                await self.handle_user_callback(callback, state, current_state)
                
        except MessageNotModified:
            await callback.answer()
        except Exception as e:
            logger.error(f"Error handling callback: {e}")
            await self.nav.handle_invalid_state(callback, state)

    async def handle_admin_callback(self, callback: types.CallbackQuery, state: FSMContext, current_state: str):
        """Handle admin callbacks"""
        data = callback.data
        
        try:
            if current_state == AdminStates.ADMIN_MENU.state:
                if data == "add_user":
                    await AdminStates.ADDING_USER.set()
                    await callback.message.edit_text(
                        "👤 Введите имя нового пользователя:",
                        reply_markup=self.nav.get_back_button()
                    )
                elif data == "manage_users":
                    await AdminStates.MANAGING_USERS.set()
                    markup = self.nav.get_user_management_menu()
                    await callback.message.edit_text(
                        "👥 Управление пользователями",
                        reply_markup=markup
                    )
                elif data == "payment_history":
                    await AdminStates.PAYMENT_HISTORY.set()
                    payments = db.get_all_payments()
                    text = "💳 История платежей:\n\n"
                    
                    for payment in payments:
                        text += (
                            f"ID: {payment['payment_id']}\n"
                            f"Пользователь: {payment['user_id']}\n"
                            f"Сумма: {payment['amount']} RUB\n"
                            f"Статус: {payment['status']}\n"
                            f"Дата: {payment['timestamp']}\n\n"
                        )
                    
                    await callback.message.edit_text(
                        text,
                        reply_markup=self.nav.get_back_button()
                    )
                elif data == "broadcast":
                    await AdminStates.BROADCASTING.set()
                    await callback.message.edit_text(
                        "📢 Введите сообщение для рассылки:",
                        reply_markup=self.nav.get_back_button()
                    )
            
            elif current_state == AdminStates.MANAGING_USERS.state:
                if data == "list_users":
                    users = db.get_clients_from_clients_table()
                    text = "📋 Список пользователей:\n\n"
                    
                    for user in users:
                        user_info = self.user_service.get_user_info(user['userData'])
                        expiration = user_info.get("expiration", "Нет данных")
                        traffic_limit = user_info.get("traffic_limit", "Нет данных")
                        
                        text += (
                            f"👤 {user['userData']}\n"
                            f"⏳ Истекает: {expiration}\n"
                            f"📊 Лимит трафика: {traffic_limit}\n\n"
                        )
                    
                    await callback.message.edit_text(
                        text,
                        reply_markup=self.nav.get_back_button("manage_users")
                    )

        except Exception as e:
            logger.error(f"Error handling admin callback: {e}")
            await self.nav.handle_invalid_state(callback, state)

    async def handle_user_callback(self, callback: types.CallbackQuery, state: FSMContext, current_state: str):
        """Handle regular user callbacks"""
        data = callback.data
        user_id = callback.from_user.id
        
        try:
            if current_state == UserStates.MAIN_MENU.state:
                if data == "buy_vpn":
                    await UserStates.BUYING_VPN.set()
                    markup = self.nav.get_subscription_menu()
                    await callback.message.edit_text(
                        "🛒 Выберите план подписки:",
                        reply_markup=markup
                    )
                elif data == "my_vpn_key":
                    await UserStates.VIEWING_VPN_KEY.set()
                    user_info = self.user_service.get_user_info(f"user_{user_id}")
                    
                    if user_info.get("expiration"):
                        expiration_date = user_info["expiration"]
                        traffic_limit = user_info["traffic_limit"]
                        markup = self.nav.get_vpn_key_menu(True)
                        
                        message_text = (
                            "🔑 Ваш VPN ключ:\n\n"
                            f"Срок действия: {expiration_date.strftime('%d.%m.%Y %H:%M')}\n"
                            f"Лимит трафика: {traffic_limit}\n\n"
                        )
                        
                        await callback.message.edit_text(
                            message_text,
                            reply_markup=markup
                        )
                    else:
                        markup = self.nav.get_vpn_key_menu(False)
                        await callback.message.edit_text(
                            "❌ У вас нет активного VPN ключа.\n"
                            "Приобретите подписку, чтобы получить доступ.",
                            reply_markup=markup
                        )
            
            elif current_state == UserStates.BUYING_VPN.state:
                if data.startswith("sub_"):
                    period = data.replace("sub_", "")
                    try:
                        payment_url = await self.payment_service.create_payment(user_id, period)
                        markup = types.InlineKeyboardMarkup()
                        markup.add(
                            types.InlineKeyboardButton("💳 Оплатить", url=payment_url),
                            types.InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_payment_{period}")
                        )
                        markup.add(self.nav.get_back_button("buy_vpn"))
                        
                        await callback.message.edit_text(
                            "Для оплаты перейдите по ссылке ниже.\n"
                            "После оплаты нажмите 'Проверить оплату'",
                            reply_markup=markup
                        )
                    except Exception as e:
                        logger.error(f"Payment creation error: {e}")
                        await callback.message.edit_text(
                            "❌ Произошла ошибка при создании платежа.\n"
                            "Пожалуйста, попробуйте позже.",
                            reply_markup=self.nav.get_back_button()
                        )
                
                elif data.startswith("check_payment_"):
                    period = data.replace("check_payment_", "")
                    payments = db.get_user_payments(user_id)
                    latest_payment = next((p for p in payments if p["status"] == "pending"), None)
                    
                    if latest_payment:
                        payment_status = await self.payment_service.check_payment_status(latest_payment["payment_id"])
                        if payment_status:
                            try:
                                username = f"user_{user_id}"
                                await self.user_service.add_user(
                                    username=username,
                                    duration=period,
                                    traffic_limit="Неограниченно"
                                )
                                
                                markup = self.nav.get_back_button("return_main")
                                await callback.message.edit_text(
                                    "✅ Оплата прошла успешно!\n"
                                    "VPN ключ создан и готов к использованию.\n"
                                    "Нажмите 'Мой VPN ключ' в главном меню для просмотра.",
                                    reply_markup=markup
                                )
                            except Exception as e:
                                logger.error(f"Error creating VPN key after payment: {e}")
                                await callback.message.edit_text(
                                    "❌ Оплата прошла успешно, но произошла ошибка при создании ключа.\n"
                                    "Пожалуйста, обратитесь в поддержку.",
                                    reply_markup=self.nav.get_back_button()
                                )
                        else:
                            await callback.answer("Оплата еще не поступила. Попробуйте позже.", show_alert=True)
                    else:
                        await callback.answer("Платеж не найден.", show_alert=True)

        except Exception as e:
            logger.error(f"Error handling user callback: {e}")
            await self.nav.handle_invalid_state(callback, state)

    async def return_to_main_menu(self, callback: types.CallbackQuery, state: FSMContext):
        """Return to main menu handler"""
        user_id = callback.from_user.id
        await state.finish()
        
        if user_id == self.admin_id:
            await AdminStates.ADMIN_MENU.set()
            text = "🔧 Панель администратора"
        else:
            await UserStates.MAIN_MENU.set()
            text = "📱 Главное меню"
            
        markup = self.nav.get_main_menu(user_id)
        await callback.message.edit_text(text, reply_markup=markup)

    async def check_user_has_key(self, user_id: int) -> bool:
        """Check if user has an active VPN key"""
        # Implement the check logic here
        return False  # Placeholder

    async def get_vpn_key_info(self, user_id: int) -> str:
        """Get user's VPN key information"""
        # Implement the key info retrieval logic here
        return "Key info placeholder"  # Placeholder
