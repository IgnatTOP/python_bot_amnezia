import uuid
import json
import logging
import hashlib
import hmac
from datetime import datetime
from yoomoney import Client
import requests
from typing import Optional, Dict, List

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PAYMENT_FILE = 'files/payments.json'
LICENSE_PRICES = {
    "1_month": 500,  # 500 RUB for 1 month
    "3_months": 1200,  # 1200 RUB for 3 months
    "6_months": 2000,  # 2000 RUB for 6 months
    "12_months": 3500  # 3500 RUB for 12 months
}

class PaymentManager:
    def __init__(self, token: str, shop_id: str, secret_key: str):
        self.token = token
        self.shop_id = shop_id
        self.secret_key = secret_key
        self._load_payments()

    def _load_payments(self):
        try:
            with open(PAYMENT_FILE, 'r') as f:
                self.payments = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.payments = {}
            self._save_payments()

    def _save_payments(self):
        with open(PAYMENT_FILE, 'w') as f:
            json.dump(self.payments, f, indent=4)

    def create_payment(self, user_id: int, plan: str) -> tuple[str, str]:
        if plan not in LICENSE_PRICES:
            raise ValueError("Invalid license plan")

        payment_id = str(uuid.uuid4())
        amount = LICENSE_PRICES[plan]

        try:
            # Create payment using YooMoney API
            payload = {
                "amount": {
                    "value": str(amount),
                    "currency": "RUB"
                },
                "capture": True,
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/your_bot_username"  # Replace with actual bot username
                },
                "description": f"VPN License {plan}",
                "metadata": {
                    "payment_id": payment_id,
                    "user_id": user_id,
                    "plan": plan
                }
            }

            headers = {
                "Authorization": f"Bearer {self.token}",
                "Idempotence-Key": payment_id,
                "Content-Type": "application/json"
            }

            logger.info(f"Creating payment for user {user_id}, plan: {plan}, amount: {amount}")
            response = requests.post(
                "https://api.yookassa.ru/v3/payments",
                json=payload,
                headers=headers
            )

            if response.status_code == 200:
                payment_data = response.json()
                confirmation_url = payment_data["confirmation"]["confirmation_url"]
                
                self.payments[payment_id] = {
                    "user_id": user_id,
                    "plan": plan,
                    "amount": amount,
                    "status": "pending",
                    "payment_id": payment_data["id"],
                    "created_at": datetime.now().isoformat(),
                    "completed_at": None
                }
                self._save_payments()
                
                logger.info(f"Payment created successfully: {payment_id}")
                return payment_id, confirmation_url
            else:
                error_msg = f"Payment creation failed with status {response.status_code}: {response.text}"
                logger.error(error_msg)
                raise Exception(error_msg)
        except requests.exceptions.RequestException as e:
            error_msg = f"Network error during payment creation: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error during payment creation: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)

    def check_payment(self, payment_id: str) -> bool:
        if payment_id not in self.payments:
            return False

        payment_data = self.payments[payment_id]
        yookassa_payment_id = payment_data.get("payment_id")

        if not yookassa_payment_id:
            return False

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        response = requests.get(
            f"https://api.yookassa.ru/v3/payments/{yookassa_payment_id}",
            headers=headers
        )

        if response.status_code == 200:
            payment_info = response.json()
            if payment_info["status"] == "succeeded":
                self.payments[payment_id]["status"] = "completed"
                self.payments[payment_id]["completed_at"] = datetime.now().isoformat()
                self._save_payments()
                return True
        
        return False

    def verify_notification(self, headers: dict, body: str) -> bool:
        received_signature = headers.get("Content-Signature")
        if not received_signature:
            return False

        hmac_signature = hmac.new(
            self.secret_key.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(received_signature, hmac_signature)

    def get_user_payments(self, user_id: int) -> List[Dict]:
        return [
            payment for payment in self.payments.values()
            if payment["user_id"] == user_id
        ]

    def get_all_payments(self) -> Dict:
        return self.payments
