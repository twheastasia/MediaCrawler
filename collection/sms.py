"""User-configured local SMS inbox. Codes and messages never leave memory."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass,field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT=Path(__file__).resolve().parents[1]
SETTINGS_PATH=ROOT/'data'/'sms_auth_config.json'
BRANDS={
 'bili':('哔哩哔哩','bilibili'), 'dy':('抖音',), 'xhs':('小红书',),
 'zhihu':('知乎',), 'ks':('快手',), 'wb':('微博','新浪'),
 'tieba':('百度','贴吧'), 'weixin':('微信','搜狗'),
}


class SmsError(RuntimeError):
    """Only fixed diagnostic codes are allowed in exception messages."""


@dataclass(frozen=True)
class Settings:
    endpoint:str
    phone:str=field(repr=False)
    enabled:bool=True
    timeout:float=120
    sim_id:int|None=None
    sub_id:int|None=None

    @classmethod
    def load(cls,path=SETTINGS_PATH):
        if not path.exists():return None
        try:
            raw=json.loads(path.read_text())
            if not raw.get('enabled'):return None
            u=urlsplit(raw['endpoint']);phone=str(raw['phone'])
            if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError
            if not re.fullmatch(r'1\d{10}',phone):raise ValueError
            return cls(raw['endpoint'],phone,timeout=min(180,max(30,float(raw.get('timeout',120)))),
                       sim_id=raw.get('sim_id'),sub_id=raw.get('sub_id'))
        except (ValueError,TypeError,KeyError):raise SmsError('sms_configuration_invalid') from None


@dataclass(frozen=True)
class Code:
    value:str=field(repr=False)
    received_at:float
    message_id:str=field(repr=False)


def identity(record):
    return hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def timestamp(value):
    try:
        value=float(value)
        return value/1000 if value>100_000_000_000 else value
    except (ValueError,TypeError):return 0


def code_in(content):
    if not re.search(r'验证码|校验码|动态码|短信码|verification\s*code|one[ -]time\s*(?:code|password)',content,re.I):return None
    # Only digits close to an explicit OTP label, never arbitrary phone/ID digits.
    values=[]
    labels=r'(?:验证码|校验码|动态码|短信码|verification\s*code|one[ -]time\s*(?:code|password))'
    for pattern in [labels+r'[^\d\n]{0,16}(?<!\d)(\d{4,8})(?!\d)',r'(?<!\d)(\d{4,8})(?!\d)[^\d\n]{0,12}'+labels]:
        values.extend(re.findall(pattern,content,re.I))
    values=set(values)
    return next(iter(values)) if len(values)==1 else None


def select_code(records,platform,settings,requested_at,baseline=(),now=None):
    now=time.time() if now is None else now
    if platform not in BRANDS:raise SmsError('sms_platform_unsupported')
    # number is the SENDER, not the receiver. Receiver is bound explicitly to
    # this local inbox by the user's configuration; enforce recipient if supplied.
    scoped=[]
    for row in records:
        if not isinstance(row,dict) or row.get('type') not in (1,'1'):continue
        recipient=row.get('recipient') or row.get('to_number')
        if recipient and re.sub(r'\D','',str(recipient))[-11:]!=settings.phone:continue
        if settings.sim_id is not None and row.get('sim_id')!=settings.sim_id:continue
        if settings.sub_id is not None and row.get('sub_id')!=settings.sub_id:continue
        scoped.append(row)
    partitions={(r.get('sim_id'),r.get('sub_id')) for r in scoped}
    if len(partitions)>1 and settings.sim_id is None and settings.sub_id is None:
        raise SmsError('sms_multiple_sim_partitions')
    candidates=[]
    for row in scoped:
        stamp=timestamp(row.get('date'));key=identity(row)
        if key in baseline or stamp<requested_at-3 or stamp>now+5 or now-stamp>180:continue
        content=str(row.get('content') or '')
        if not any(brand.lower() in content.lower() for brand in BRANDS[platform]):continue
        # A message mentioning two different services is ambiguous.
        matching={p for p,brands in BRANDS.items() if any(b.lower() in content.lower() for b in brands)}
        if matching!={platform}:continue
        value=code_in(content)
        if value:candidates.append(Code(value,stamp,key))
    if not candidates:return None
    latest=max(c.received_at for c in candidates)
    newest=[c for c in candidates if c.received_at==latest]
    if len({c.value for c in newest})!=1:raise SmsError('sms_code_ambiguous')
    return newest[0]


class Inbox:
    def __init__(self,settings,gate,client_factory=None):
        self.settings,self.gate=settings,gate
        self.client_factory=client_factory or httpx.AsyncClient

    async def query(self):
        async def fetch():
            try:
                async with self.client_factory(timeout=12,trust_env=False,follow_redirects=False) as client:
                    response=await client.post(self.settings.endpoint,
                        headers={'Content-Type':'application/json; charset=utf-8'},
                        json={'data':{'type':1,'page_num':1,'page_size':10}})
                    if response.status_code!=200:raise SmsError('sms_query_http_error')
                    data=response.json()
                    if not isinstance(data,dict) or data.get('code') not in (0,1,200) or not isinstance(data.get('data'),list):
                        raise SmsError('sms_query_response_invalid')
                    return data['data']
            except SmsError:raise
            except Exception:raise SmsError('sms_query_failed') from None
        return await self.gate.action('sms_inbox_query',fetch)

    async def wait(self,platform,requested_at,baseline,before_query=None):
        deadline=time.monotonic()+self.settings.timeout
        while time.monotonic()<deadline:
            if before_query:await before_query()
            records=await self.query()
            match=select_code(records,platform,self.settings,requested_at,baseline)
            records.clear()
            if match:return match
            # The platform gate enforces >=15s (WeChat >=10s), including errors.
            # This small sleep prevents busy loops with fake/test gates as well.
            await asyncio.sleep(1)
        raise SmsError('sms_code_timeout')


@asynccontextmanager
async def phone_lock(settings,path=None,timeout=180):
    path=path or ROOT/'data'/('sms_auth_'+hashlib.sha256(settings.phone.encode()).hexdigest()[:16]+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as stream:
        deadline=time.monotonic()+timeout
        while True:
            try:
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise SmsError('sms_phone_busy') from None
                await asyncio.sleep(1)
        try:yield
        finally:fcntl.flock(stream,fcntl.LOCK_UN)
