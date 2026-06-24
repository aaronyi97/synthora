"""
Aliyun SMS adapter — sends verification codes via Alibaba Cloud SMS.

Configuration (environment variables):
  ALIYUN_ACCESS_KEY_ID      — AccessKey ID from Aliyun RAM
  ALIYUN_ACCESS_KEY_SECRET  — AccessKey Secret from Aliyun RAM
  ALIYUN_SMS_SIGN_NAME      — SMS signature name (e.g. "Synthora")
  ALIYUN_SMS_TEMPLATE_CODE  — SMS template code (e.g. "SMS_123456789")

Template must contain ${code} variable, e.g.:
  "您的验证码是${code}，5分钟内有效，请勿泄露。"

If credentials are not set, falls back to console logging (dev mode).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import string

logger = logging.getLogger(__name__)

_ACCESS_KEY_ID = os.environ.get("ALIYUN_ACCESS_KEY_ID", "")
_ACCESS_KEY_SECRET = os.environ.get("ALIYUN_ACCESS_KEY_SECRET", "")
_SIGN_NAME = os.environ.get("ALIYUN_SMS_SIGN_NAME", "Synthora")
_TEMPLATE_CODE = os.environ.get("ALIYUN_SMS_TEMPLATE_CODE", "")

_DEV_MODE = not (_ACCESS_KEY_ID and _ACCESS_KEY_SECRET and _TEMPLATE_CODE)


def generate_code(length: int = 6) -> str:
    """Generate a numeric verification code."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


async def send_verification_code(phone: str, code: str) -> tuple[bool, str]:
    """
    Send an SMS verification code to the given phone number.

    Returns (success: bool, message: str).
    In dev mode (no credentials), logs the code and returns success.
    """
    if _DEV_MODE:
        # SEC-L1-01: In production, refuse to send if SMS credentials missing (fail-safe)
        if os.environ.get("ENV", "").lower() == "production":
            logger.error(
                "[SMS] Production environment but SMS credentials not configured! "
                "Refusing to proceed. Set ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET / ALIYUN_SMS_TEMPLATE_CODE."
            )
            return False, "SMS service unavailable"
        logger.warning(
            f"[SMS DEV MODE] Would send code {code} to {phone}. "
            "Set ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET / ALIYUN_SMS_TEMPLATE_CODE to enable real SMS."
        )
        return True, "dev_mode"

    try:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_dysmsapi20170525 import models as sms_models
    except ImportError:
        logger.error(
            "alibabacloud-dysmsapi20170525 not installed. "
            "Run: pip install alibabacloud-dysmsapi20170525"
        )
        return False, "SMS SDK not installed"

    try:
        config = open_api_models.Config(
            access_key_id=_ACCESS_KEY_ID,
            access_key_secret=_ACCESS_KEY_SECRET,
        )
        config.endpoint = "dysmsapi.aliyuncs.com"
        client = Client(config)

        send_request = sms_models.SendSmsRequest(
            phone_numbers=phone,
            sign_name=_SIGN_NAME,
            template_code=_TEMPLATE_CODE,
            template_param=json.dumps({"code": code}),
        )
        response = client.send_sms(send_request)
        body = response.body
        if body.code == "OK":
            logger.info(f"SMS sent to {phone[-4:]}****, request_id={body.request_id}")
            return True, "ok"
        else:
            logger.error(f"Aliyun SMS error: code={body.code} message={body.message}")
            return False, body.message or body.code
    except Exception as exc:
        logger.error(f"SMS send failed for {phone}: {exc}")
        return False, str(exc)
