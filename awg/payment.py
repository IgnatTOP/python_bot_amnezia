import os
import json
import uuid
from datetime import datetime
from yookassa import Configuration, Payment

# YooKassa configuration
Configuration.account_id = '993270'
Configuration.secret_key = 'test_cE-RElZLKakvb585wjrh9XAoqGSyS_rcmta2v1MdURE'

PAYMENTS_FILE = 'files/payments.json'

def load_payments():
    if os.path.exists(PAYMENTS_FILE):
        with open(PAYMENTS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_payments(payments):
    os.makedirs(os.path.dirname(PAYMENTS_FILE), exist_ok=True)
    with open(PAYMENTS_FILE, 'w') as f:
        json.dump(payments, f)

def create_payment(amount, user_id, description="VPN Key"):
    idempotence_key = str(uuid.uuid4())
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
        "description": description,
        "metadata": {
            "user_id": str(user_id)
        }
    }, idempotence_key)
    
    payments = load_payments()
    payments[payment.id] = {
        "user_id": user_id,
        "amount": amount,
        "status": payment.status,
        "created_at": datetime.now().isoformat(),
        "description": description
    }
    save_payments(payments)
    
    return payment

def check_payment(payment_id):
    payment = Payment.find_one(payment_id)
    
    payments = load_payments()
    if payment_id in payments:
        payments[payment_id]["status"] = payment.status
        save_payments(payments)
    
    return payment.status == "succeeded"

def get_user_payments(user_id):
    payments = load_payments()
    return {pid: data for pid, data in payments.items() 
            if data["user_id"] == user_id}

def get_all_payments():
    return load_payments()
