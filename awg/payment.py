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
        
        # Создаем Base64 токен для аутентификации
        import base64
        auth_string = f"{shop_id}:{secret_key}"
        self.auth_token = base64.b64encode(auth_string.encode()).decode()
        
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
                    "return_url": "https://yookassa.ru/"
                },
                "description": f"VPN License {plan}",
                "merchant_customer_id": str(user_id),
                "metadata": {
                    "payment_id": payment_id,
                    "user_id": user_id,
                    "plan": plan
                }
            }

            headers = {
                "Authorization": f"Basic {self.auth_token}",
                "Idempotence-Key": payment_id,
                "Content-Type": "application/json"
            }

            logger.info(f"Creating payment for user {user_id}, plan: {plan}, amount: {amount}")
            logger.debug(f"Request headers: {headers}")
            logger.debug(f"Request payload: {payload}")
            
            response = requests.post(
                "https://api.yookassa.ru/v3/payments",
                json=payload,
                headers=headers
            )

            logger.debug(f"Response status: {response.status_code}")
            logger.debug(f"Response headers: {response.headers}")
            
            try:
                response_data = response.json()
                logger.debug(f"Response data: {response_data}")
            except ValueError as e:
                logger.error(f"Failed to parse response as JSON: {e}")
                logger.error(f"Raw response: {response.text}")
                raise Exception("Invalid response from YooKassa API")
            
            if response.status_code == 200:
                payment_data = response_data
                confirmation_url = payment_data.get("confirmation", {}).get("confirmation_url")
                
                if not confirmation_url:
                    error_msg = "No confirmation URL in response"
                    logger.error(f"{error_msg}: {response_data}")
                    raise Exception(error_msg)
                
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
                error_msg = f"Payment creation failed with status {response.status_code}"
                if "description" in response_data:
                    error_msg += f": {response_data['description']}"
                elif "message" in response_data:
                    error_msg += f": {response_data['message']}"
                else:
                    error_msg += f": {response.text}"
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
            logger.error(f"Payment {payment_id} not found in local storage")
            return False

        payment_data = self.payments[payment_id]
        yookassa_payment_id = payment_data.get("payment_id")

        if not yookassa_payment_id:
            logger.error(f"YooKassa payment ID not found for payment {payment_id}")
            return False

        try:
            headers = {
                "Authorization": f"Basic {self.auth_token}",
                "Content-Type": "application/json"
            }

            logger.info(f"Checking payment status for {payment_id} (YooKassa ID: {yookassa_payment_id})")
            response = requests.get(
                f"https://api.yookassa.ru/v3/payments/{yookassa_payment_id}",
                headers=headers
            )

            if response.status_code == 200:
                payment_info = response.json()
                status = payment_info.get("status")
                logger.info(f"Payment {payment_id} status: {status}")
                
                if status == "succeeded":
                    self.payments[payment_id]["status"] = "completed"
                    self.payments[payment_id]["completed_at"] = datetime.now().isoformat()
                    self._save_payments()
                    return True
                elif status in ["canceled", "expired"]:
                    logger.warning(f"Payment {payment_id} is {status}")
                    self.payments[payment_id]["status"] = status
                    self._save_payments()
            else:
                response_data = response.json()
                error_msg = f"Failed to check payment status. Status code: {response.status_code}"
                if "description" in response_data:
                    error_msg += f", Error: {response_data['description']}"
                logger.error(error_msg)
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error while checking payment {payment_id}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error while checking payment {payment_id}: {str(e)}")
        
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

    def get_payment_status(self, payment_id: str) -> Optional[str]:
        """Get the current status of a payment"""
        if payment_id not in self.payments:
            return None
            
        payment_data = self.payments[payment_id]
        yookassa_payment_id = payment_data.get("payment_id")
        
        if not yookassa_payment_id:
            return None
            
        try:
            headers = {
                "Authorization": f"Basic {self.auth_token}",
                "Content-Type": "application/json"
            }
            
            response = requests.get(
                f"https://api.yookassa.ru/v3/payments/{yookassa_payment_id}",
                headers=headers
            )
            
            if response.status_code == 200:
                payment_info = response.json()
                return payment_info.get("status")
            else:
                logger.error(f"Failed to get payment status: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting payment status: {e}")
            return None
