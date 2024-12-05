import logging
import subprocess
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import db
from yookassa import Payment

logger = logging.getLogger(__name__)

class VPNService:
    def __init__(self, wg_config_file: str, docker_container: str, endpoint: str):
        self.wg_config_file = wg_config_file
        self.docker_container = docker_container
        self.endpoint = endpoint

    async def generate_vpn_key(self, username: str) -> str:
        """Generate a new VPN key for a user"""
        try:
            # Create temporary config file
            temp_conf = f"/tmp/{username}.conf"
            cmd = f"docker exec {self.docker_container} wg genkey | tee {temp_conf}.key | wg pubkey > {temp_conf}.pub"
            await asyncio.create_subprocess_shell(cmd)

            # Read private and public keys
            private_key = await self._read_file(f"{temp_conf}.key")
            public_key = await self._read_file(f"{temp_conf}.pub")

            # Generate client config
            config = self._generate_client_config(username, private_key, public_key)
            
            # Save config
            await self._save_config(username, config)
            
            return config
        except Exception as e:
            logger.error(f"Error generating VPN key: {e}")
            raise

    async def _read_file(self, filepath: str) -> str:
        """Read content from a file"""
        cmd = f"cat {filepath}"
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()

    def _generate_client_config(self, username: str, private_key: str, public_key: str) -> str:
        """Generate WireGuard client configuration"""
        config = f"""[Interface]
PrivateKey = {private_key}
Address = 10.0.0.2/32
DNS = 8.8.8.8, 8.8.4.4

[Peer]
PublicKey = {public_key}
Endpoint = {self.endpoint}:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25"""
        return config

    async def _save_config(self, username: str, config: str) -> None:
        """Save client configuration"""
        config_path = f"/etc/wireguard/clients/{username}.conf"
        cmd = f"docker exec {self.docker_container} bash -c 'mkdir -p /etc/wireguard/clients && echo \"{config}\" > {config_path}'"
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()

    async def delete_vpn_key(self, username: str) -> bool:
        """Delete a user's VPN key"""
        try:
            cmd = f"docker exec {self.docker_container} rm -f /etc/wireguard/clients/{username}.conf"
            proc = await asyncio.create_subprocess_shell(cmd)
            await proc.communicate()
            return True
        except Exception as e:
            logger.error(f"Error deleting VPN key: {e}")
            return False

class PaymentService:
    PAYMENT_AMOUNTS = {
        "1_month": 500,
        "3_months": 1200,
        "6_months": 2000,
        "12_months": 3500
    }

    @staticmethod
    async def create_payment(user_id: int, period: str) -> str:
        """Create a new payment for VPN subscription"""
        try:
            amount = PaymentService.PAYMENT_AMOUNTS.get(period)
            if not amount:
                raise ValueError(f"Invalid period: {period}")

            payment = Payment.create({
                "amount": {
                    "value": str(amount),
                    "currency": "RUB"
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": "https://t.me/your_bot_username"
                },
                "metadata": {
                    "user_id": user_id,
                    "period": period
                }
            })

            db.add_payment(user_id, payment.id, float(amount))
            return payment.confirmation.confirmation_url
        except Exception as e:
            logger.error(f"Error creating payment: {e}")
            raise

    @staticmethod
    async def check_payment_status(payment_id: str) -> bool:
        """Check the status of a payment"""
        try:
            payment = Payment.find_one(payment_id)
            if payment.status == "succeeded":
                db.update_payment_status(payment_id, "succeeded")
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking payment status: {e}")
            return False

class UserService:
    def __init__(self, vpn_service: VPNService):
        self.vpn_service = vpn_service

    async def add_user(self, username: str, duration: str, traffic_limit: str) -> bool:
        """Add a new VPN user"""
        try:
            # Generate VPN key
            config = await self.vpn_service.generate_vpn_key(username)
            
            # Calculate expiration
            expiration = self._calculate_expiration(duration)
            
            # Save user data
            db.set_user_expiration(username, expiration, traffic_limit)
            
            return True
        except Exception as e:
            logger.error(f"Error adding user: {e}")
            return False

    def _calculate_expiration(self, duration: str) -> datetime:
        """Calculate expiration date based on duration"""
        now = datetime.now()
        if duration == "1h":
            return now + timedelta(hours=1)
        elif duration == "1d":
            return now + timedelta(days=1)
        elif duration == "1w":
            return now + timedelta(weeks=1)
        elif duration == "1m":
            return now + timedelta(days=30)
        elif duration == "unlimited":
            return now + timedelta(years=100)
        else:
            raise ValueError(f"Invalid duration: {duration}")

    async def delete_user(self, username: str) -> bool:
        """Delete a VPN user"""
        try:
            # Delete VPN key
            await self.vpn_service.delete_vpn_key(username)
            
            # Remove from database
            db.deactive_user_db(username)
            db.remove_user_expiration(username)
            
            return True
        except Exception as e:
            logger.error(f"Error deleting user: {e}")
            return False

    @staticmethod
    def get_user_info(username: str) -> Dict:
        """Get user information"""
        try:
            expiration = db.get_user_expiration(username)
            traffic_limit = db.get_user_traffic_limit(username)
            return {
                "username": username,
                "expiration": expiration,
                "traffic_limit": traffic_limit
            }
        except Exception as e:
            logger.error(f"Error getting user info: {e}")
            return {}
