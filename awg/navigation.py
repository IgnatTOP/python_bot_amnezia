from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

class UserStates(StatesGroup):
    """States for regular users"""
    MAIN_MENU = State()
    BUYING_VPN = State()
    SELECTING_PLAN = State()
    AWAITING_PAYMENT = State()
    VIEWING_VPN_KEY = State()
    HELP_MENU = State()

class AdminStates(StatesGroup):
    """States for admin users"""
    ADMIN_MENU = State()
    ADDING_USER = State()
    MANAGING_USERS = State()
    VIEWING_USER = State()
    PAYMENT_HISTORY = State()
    BROADCASTING = State()

class Navigation:
    def __init__(self, admin_id: int):
        self.admin_id = admin_id
        
    def get_main_menu(self, user_id: int) -> InlineKeyboardMarkup:
        """Generate main menu based on user type"""
        markup = InlineKeyboardMarkup(row_width=2)
        
        if user_id == self.admin_id:
            markup.add(
                InlineKeyboardButton("👥 Добавить пользователя", callback_data="add_user"),
                InlineKeyboardButton("📊 Управление пользователями", callback_data="manage_users")
            )
            markup.add(
                InlineKeyboardButton("💳 История платежей", callback_data="payment_history"),
                InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")
            )
            markup.add(
                InlineKeyboardButton("📦 Создать бекап", callback_data="create_backup")
            )
        else:
            markup.add(
                InlineKeyboardButton("🛒 Купить VPN", callback_data="buy_vpn"),
                InlineKeyboardButton("🔑 Мой VPN ключ", callback_data="my_vpn_key")
            )
            markup.add(
                InlineKeyboardButton("❓ Помощь", callback_data="help"),
                InlineKeyboardButton("💰 Баланс", callback_data="balance")
            )
        
        return markup

    def get_subscription_menu(self) -> InlineKeyboardMarkup:
        """Generate subscription plans menu"""
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("1 месяц - 500₽", callback_data="sub_1_month"),
            InlineKeyboardButton("3 месяца - 1200₽", callback_data="sub_3_months")
        )
        markup.add(
            InlineKeyboardButton("6 месяцев - 2000₽", callback_data="sub_6_months"),
            InlineKeyboardButton("12 месяцев - 3500₽", callback_data="sub_12_months")
        )
        markup.add(InlineKeyboardButton("« Назад", callback_data="return_main"))
        return markup

    def get_user_management_menu(self) -> InlineKeyboardMarkup:
        """Generate user management menu for admin"""
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("👥 Список пользователей", callback_data="list_users"),
            InlineKeyboardButton("🔍 Поиск пользователя", callback_data="search_user")
        )
        markup.add(
            InlineKeyboardButton("📊 Статистика", callback_data="user_stats"),
            InlineKeyboardButton("⚠️ Проблемные клиенты", callback_data="problem_users")
        )
        markup.add(InlineKeyboardButton("« Назад", callback_data="return_main"))
        return markup

    def get_vpn_key_menu(self, has_active_key: bool) -> InlineKeyboardMarkup:
        """Generate VPN key management menu"""
        markup = InlineKeyboardMarkup(row_width=2)
        if has_active_key:
            markup.add(
                InlineKeyboardButton("🔄 Обновить ключ", callback_data="regenerate_key"),
                InlineKeyboardButton("📱 QR код", callback_data="show_qr")
            )
            markup.add(
                InlineKeyboardButton("📊 Статистика", callback_data="key_stats"),
                InlineKeyboardButton("⚠️ Проблемы", callback_data="key_issues")
            )
        markup.add(InlineKeyboardButton("« Назад", callback_data="return_main"))
        return markup

    @staticmethod
    def get_back_button(callback_data: str = "return_main") -> InlineKeyboardMarkup:
        """Generate a back button markup"""
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("« Назад", callback_data=callback_data))
        return markup

    @staticmethod
    async def handle_invalid_state(callback: CallbackQuery, state: FSMContext):
        """Handle invalid state transitions"""
        await callback.answer("Это действие недоступно в текущем состоянии", show_alert=True)
        logger.warning(f"Invalid state transition attempted by user {callback.from_user.id}")
        # Reset state to main menu
        await state.finish()
        await callback.message.answer("Возвращаемся в главное меню...")
