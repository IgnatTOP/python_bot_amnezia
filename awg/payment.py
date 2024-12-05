from yookassa import Configuration, Payment
import uuid
from datetime import datetime
import logging
import json
import os
import db
import time
from yookassa.domain.common.http_client_error import HttpClientError
from yookassa.domain.common.request_object import RequestObject

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PAYMENTS_FILE = 'files/payments.json'
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

# Initialize YooKassa configuration from settings
setting = db.get_config()
Configuration.account_id = setting.get('yookassa_shop_id')
Configuration.secret_key = setting.get('yookassa_token')

if not all([Configuration.account_id, Configuration.secret_key]):
    logger.error("YooKassa settings are missing in configuration file.")
    raise ValueError("YooKassa configuration is incomplete")

def load_payments():
    if os.path.exists(PAYMENTS_FILE):
        try:
            with open(PAYMENTS_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error("Error reading payments file. Creating new one.")
            return {}
    return {}

def save_payments(payments):
    try:
        os.makedirs(os.path.dirname(PAYMENTS_FILE), exist_ok=True)
        with open(PAYMENTS_FILE, 'w') as f:
            json.dump(payments, f)
    except Exception as e:
        logger.error(f"Error saving payments: {e}")

def create_payment(amount, user_id):
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise ValueError("Amount must be a positive number")
    if not isinstance(user_id, (int, str)):
        raise ValueError("Invalid user_id format")
    
    for attempt in range(MAX_RETRIES):
        try:
            idempotence_key = str(uuid.uuid4())
            payment = Payment.create({
                "amount": {
                    "value": str(amount),
                    "currency": "RUB"
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": setting.get('payment_return_url', 'https://t.me/your_bot_name')
                },
                "capture": True,
                "description": f"VPN subscription payment for user {user_id}"
            }, idempotence_key)

            # Save payment info
            payments = load_payments()
            payments[payment.id] = {
                'user_id': str(user_id),
                'amount': str(amount),
                'status': payment.status,
                'created_at': datetime.now().isoformat(),
                'idempotence_key': idempotence_key
            }
            save_payments(payments)
            
            return payment
        except HttpClientError as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Failed to create payment after {MAX_RETRIES} attempts: {e}")
                raise
            time.sleep(RETRY_DELAY)
        except Exception as e:
            logger.error(f"Unexpected error creating payment: {e}")
            raise

def check_payment(payment_id):
    if not payment_id:
        raise ValueError("Payment ID cannot be empty")
        
    try:
        payment = Payment.find_one(payment_id)
        
        # Update payment status in our records
        payments = load_payments()
        if payment_id in payments:
            payments[payment_id]['status'] = payment.status
            save_payments(payments)
            
        return payment
    except HttpClientError as e:
        logger.error(f"Error checking payment {payment_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error checking payment {payment_id}: {e}")
        return None

def get_payment_history(user_id=None):
    try:
        payments = load_payments()
        if user_id:
            return {k: v for k, v in payments.items() if v['user_id'] == str(user_id)}
        return payments
    except Exception as e:
        logger.error(f"Error getting payment history: {e}")
        return {}
