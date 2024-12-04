import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import aiohttp
import base64
from . import db
from .config.yookassa import YOOKASSA_CONFIG

logger = logging.getLogger(__name__)

class PaymentManager:
    def __init__(self):
        self.shop_id = YOOKASSA_CONFIG['shop_id']
        self.secret_key = YOOKASSA_CONFIG['secret_key']
        self.return_url = YOOKASSA_CONFIG['return_url']
        self.auth = base64.b64encode(f"{self.shop_id}:{self.secret_key}".encode()).decode()

    async def create_payment(self, user_id: int, amount: float) -> Dict[str, Any]:
        """Create a new payment and return payment details"""
        payment_id = str(uuid.uuid4())
        
        # Create payment in YooKassa
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Basic {self.auth}",
                "Content-Type": "application/json",
                "Idempotence-Key": payment_id
            }
            payload = {
                "amount": {
                    "value": str(amount),
                    "currency": "RUB"
                },
                "capture": True,
                "confirmation": {
                    "type": "redirect",
                    "return_url": self.return_url
                },
                "description": f"Оплата доступа к боту для пользователя {user_id}",
                "metadata": {
                    "user_id": str(user_id)
                }
            }
            
            try:
                async with session.post("https://api.yookassa.ru/v3/payments", 
                                      json=payload, 
                                      headers=headers) as response:
                    result = await response.json()
                    if response.status == 200:
                        # Save payment info to database
                        db.save_payment(user_id, amount, result['id'])  # Use YooKassa payment ID
                        return {
                            "payment_id": result['id'],
                            "confirmation_url": result["confirmation"]["confirmation_url"],
                            "amount": amount,
                            "status": result['status']
                        }
                    else:
                        logger.error(f"Failed to create payment: {result}")
                        return None
            except Exception as e:
                logger.error(f"Error creating payment: {e}")
                return None

    async def check_payment(self, payment_id: str) -> Dict[str, Any]:
        """Check payment status in YooKassa"""
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Basic {self.auth}",
                "Content-Type": "application/json"
            }
            
            try:
                async with session.get(f"https://api.yookassa.ru/v3/payments/{payment_id}", 
                                     headers=headers) as response:
                    result = await response.json()
                    if response.status == 200:
                        # Update payment status in database
                        db.update_payment_status(payment_id, result['status'])
                        return {
                            "payment_id": result['id'],
                            "status": result['status'],
                            "paid": result['status'] == 'succeeded'
                        }
                    else:
                        logger.error(f"Failed to check payment: {result}")
                        return None
            except Exception as e:
                logger.error(f"Error checking payment: {e}")
                return None

    async def check_payment_status(self, payment_id: str) -> Optional[str]:
        """Check payment status in YooKassa system"""
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Basic {self.auth}",
                "Content-Type": "application/json"
            }
            
            try:
                async with session.get(f"https://api.yookassa.ru/v3/payments/{payment_id}", 
                                     headers=headers) as response:
                    result = await response.json()
                    if response.status == 200:
                        status = result.get("status", "unknown")
                        if status == "succeeded":
                            # Payment successful, update user subscription
                            metadata = result.get("metadata", {})
                            user_id = metadata.get("user_id")
                            if user_id:
                                amount = float(result["amount"]["value"])
                                # Calculate subscription days based on amount
                                days = self._get_subscription_days(amount)
                                if days:
                                    db.update_user_subscription(int(user_id), days)
                        
                        db.update_payment_status(payment_id, status)
                        return status
                    return None
            except Exception as e:
                logger.error(f"Error checking payment status: {e}")
                return None

    def _get_subscription_days(self, amount: float) -> Optional[int]:
        """Get subscription days based on payment amount"""
        from .config.yookassa import SUBSCRIPTION_PRICES, SUBSCRIPTION_DAYS
        
        # Find the closest matching subscription price
        for period, price in SUBSCRIPTION_PRICES.items():
            if abs(price - amount) < 1:  # Allow for small floating-point differences
                return SUBSCRIPTION_DAYS[period]
        return None

class KeyManager:
    @staticmethod
    def generate_key() -> str:
        """Generate a unique access key"""
        return str(uuid.uuid4())

    @staticmethod
    async def issue_new_key(user_id: int, days: int = 30) -> Optional[str]:
        """Issue a new key for the user and update their subscription"""
        user = db.get_user(user_id)
        if not user:
            return None

        # Generate new key
        new_key = KeyManager.generate_key()
        
        # Update user record with the new key
        users = db.load_users()
        users[str(user_id)]['current_key'] = new_key
        users[str(user_id)]['is_active'] = True
        db.save_users(users)
        
        # Update user's subscription
        db.update_user_subscription(user_id, days)
        
        return new_key

    @staticmethod
    async def revoke_key(user_id: int) -> bool:
        """Revoke user's current key"""
        user = db.get_user(user_id)
        if not user:
            return False
            
        # Update user record to remove the key
        users = db.load_users()
        users[str(user_id)]['current_key'] = None
        users[str(user_id)]['is_active'] = False
        db.save_users(users)
        
        return True
