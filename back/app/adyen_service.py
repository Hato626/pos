"""Adyen Checkout v71 and Balance Platform v2. Card details stay on Adyen."""
import base64
import hashlib
import hmac
import os
import re
from urllib.parse import urlsplit
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from .platform_models import PlatformAdyenConfig, PlatformAdyenPayment


def cipher() -> Fernet:
    key = os.environ.get('PLATFORM_SECRETS_KEY', '')
    if not key:
        raise HTTPException(503, 'Platform payment encryption key is not configured')
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise HTTPException(503, 'Platform payment encryption key is invalid') from None


def encrypt(value: str) -> str:
    return cipher().encrypt(value.encode()).decode() if value else ''


def decrypt(value: str) -> str:
    if not value:
        return ''
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        raise HTTPException(503, 'Payment credentials cannot be decrypted') from None


def configured(c: PlatformAdyenConfig | None) -> bool:
    return bool(c and c.api_key_encrypted and c.balance_api_key_encrypted and c.hmac_key_encrypted
                and c.merchant_account and c.balance_platform and c.liable_balance_account
                and (c.environment == 'test' or c.live_prefix))


def api(c: PlatformAdyenConfig, method: str, path: str, *, body=None, key=None, balance=False) -> dict:
    if c.environment not in ('test', 'live'):
        raise HTTPException(409, 'Unknown Adyen environment')
    if balance:
        base = 'https://balanceplatform-api-' + c.environment + '.adyen.com/bcl/v2'
        secret = decrypt(c.balance_api_key_encrypted)
    else:
        if c.environment == 'test':
            base = 'https://checkout-test.adyen.com/v71'
        else:
            if not re.fullmatch(r'[A-Za-z0-9-]+', c.live_prefix):
                raise HTTPException(409, 'Adyen live endpoint prefix is required')
            base = 'https://' + c.live_prefix + '-checkout-live.adyenpayments.com/checkout/v71'
        secret = decrypt(c.api_key_encrypted)
    if not secret:
        raise HTTPException(409, 'Adyen API credential is missing')
    headers = {'X-API-Key': secret, 'Content-Type': 'application/json'}
    if key:
        headers['Idempotency-Key'] = key
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = client.request(method, base+path, json=body, headers=headers)
        if response.status_code >= 400:
            # Do not return payment credentials, full upstream bodies or customer details.
            raise HTTPException(502, f'Adyen rejected the request (HTTP {response.status_code}); check credentials and account configuration')
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Adyen request could not be confirmed; retry with the same request') from None


def verify_account(c: PlatformAdyenConfig, account: str, holder: str | None = None, *, recipient=True) -> dict:
    if not account or not re.fullmatch(r'[A-Za-z0-9_-]+', account):
        raise HTTPException(409, 'Adyen balance account is not configured')
    data = api(c, 'GET', '/balanceAccounts/'+account, balance=True)
    account_holder = data.get('accountHolderId')
    if data.get('id') != account or data.get('status') != 'active' or not account_holder:
        raise HTTPException(409, 'Adyen balance account is not active')
    if holder and account_holder != holder:
        raise HTTPException(409, 'Balance account does not belong to the configured account holder')
    owner = api(c, 'GET', '/accountHolders/'+account_holder, balance=True)
    if owner.get('balancePlatform') != c.balance_platform or owner.get('status') != 'active':
        raise HTTPException(409, 'Adyen account holder is inactive or belongs to another balance platform')
    capabilities = owner.get('capabilities', {})
    required = ('receivePayments', 'receiveFromPlatformPayments', 'sendToTransferInstrument') if recipient else ()
    missing = [name for name in required if not capabilities.get(name, {}).get('allowed')]
    if missing:
        raise HTTPException(409, 'Adyen verification is incomplete: '+', '.join(missing))
    return {'balance_account': account, 'account_holder': account_holder,
            'status': data['status'], 'capabilities': {k: bool(capabilities.get(k,{}).get('allowed')) for k in required},
            'balances': data.get('balances', [])}


def splits(p: PlatformAdyenPayment, amount: int | None = None, fee: int | None = None, *, refund=False) -> list[dict]:
    value = p.amount_cents if amount is None else amount
    commission = p.commission_cents if fee is None else fee
    items = []
    if value-commission:
        items.append({'type':'BalanceAccount','account':p.balance_account,
                      'amount':{'currency':p.currency,'value':value-commission}, 'reference':p.id+'-restaurant'})
    if commission:
        items.append({'type':'Commission','account':p.liable_balance_account,
                      'amount':{'currency':p.currency,'value':commission}, 'reference':p.id+'-commission'})
    if not refund:
        items.append({'type':'PaymentFee','account':p.liable_balance_account,'reference':p.id+'-fees'})
    return items


def valid_checkout_url(url: str) -> bool:
    u = urlsplit(url)
    host = (u.hostname or '').lower()
    return u.scheme == 'https' and not u.username and not u.password and (
        host in ('adyen.link','test.adyen.link') or host.endswith('.adyen.com') or host.endswith('.adyen.link'))


def hmac_payload(item: dict) -> str:
    amount = item.get('amount') or {}
    values = [item.get('pspReference',''), item.get('originalReference',''), item.get('merchantAccountCode',''),
              item.get('merchantReference',''), str(amount.get('value','')), amount.get('currency',''),
              item.get('eventCode',''), str(item.get('success','')).lower()]
    return ':'.join(str(v).replace('\\','\\\\').replace(':','\\:') for v in values)


def verify_hmac(item: dict, c: PlatformAdyenConfig) -> bool:
    signature = (item.get('additionalData') or {}).get('hmacSignature')
    if not isinstance(signature,str):
        return False
    for encrypted in (c.hmac_key_encrypted, c.previous_hmac_key_encrypted):
        key = decrypt(encrypted)
        if not key:
            continue
        try:
            expected = base64.b64encode(hmac.new(bytes.fromhex(key),hmac_payload(item).encode(),hashlib.sha256).digest()).decode()
        except ValueError:
            continue
        if hmac.compare_digest(expected,signature):
            return True
    return False
