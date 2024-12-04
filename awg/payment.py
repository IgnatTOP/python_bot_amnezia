import uuid
import json
import logging
from datetime import datetime
from yoomoney import Client, Quickpay
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
    def __init__(self, token: str):
        self.client = Client(token)
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
        
        quickpay = Quickpay(
            receiver="YOUR_YOOMONEY_WALLET",
            quickpay_form="shop",
            targets=f"VPN License {plan}",
            paymentType="SB",
            sum=amount,
            label=payment_id
        )

        self.payments[payment_id] = {
            "user_id": user_id,
            "plan": plan,
            "amount": amount,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "completed_at": None
        }
        self._save_payments()
        
        return payment_id, quickpay.redirected_url

    def check_payment(self, payment_id: str) -> bool:
        if payment_id not in self.payments:
            return False

        history = self.client.operation_history(label=payment_id)
        
        for operation in history.operations:
            if operation.status == "success":
                self.payments[payment_id]["status"] = "completed"
                self.payments[payment_id]["completed_at"] = datetime.now().isoformat()
                self._save_payments()
                return True
        
        return False

    def get_user_payments(self, user_id: int) -> List[Dict]:
        return [
            payment for payment in self.payments.values()
            if payment["user_id"] == user_id
        ]

    def get_all_payments(self) -> Dict:
        return self.payments
