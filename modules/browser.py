from urllib.parse import urlencode
from aiohttp import ClientSession
from decimal import Decimal
from hashlib import sha256
from loguru import logger
from time import time
import hmac

from modules import DataBase
from modules.retry import retry, have_json


class Browser:

    BASE_URL: str = "https://fapi.asterdex.com"

    def __init__(self, proxy: str, api_key: str, label: str, db: DataBase):
        self.max_retries = 5
        self.api_key = api_key
        self.label = label
        self.db = db

        if proxy not in ['https://log:pass@ip:port', 'http://log:pass@ip:port', 'log:pass@ip:port', '', None]:
            self.proxy = "http://" + proxy.removeprefix("https://").removeprefix("http://")
            logger.opt(colors=True).debug(f'[•] <white>{self.label}</white> | Got proxy <white>{self.proxy}</white>')
        else:
            self.proxy = None
            logger.opt(colors=True).warning(f'[-] <white>{self.label}</white> | Dont use proxies!')

        self.sessions = []
        self.session = self.get_new_session()


    def get_new_session(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Origin": "https://www.asterdex.com",
            "Referer": "https://www.asterdex.com/",
        }

        session = ClientSession(headers=headers)
        session.proxy = self.proxy

        self.sessions.append(session)
        return session


    async def close_sessions(self):
        for session in self.sessions:
            await session.close()


    def _build_signature(self, all_params: dict, method: str):
        query_string = urlencode(all_params)

        signature = hmac.new(
            self.api_key.split(':')[1].encode('utf-8'),
            query_string.encode('utf-8'),
            sha256
        ).hexdigest()

        return {
            "headers": {
                "X-MBX-APIKEY": self.api_key.split(':')[0],
            },
            "data" if method == "POST" else "params": {
                "signature": signature
            }
        }


    @have_json
    async def send_request(self, **kwargs):
        if kwargs.get("session"):
            session = kwargs["session"]
            del kwargs["session"]
        else:
            session = self.session

        if kwargs.get("method"): kwargs["method"] = kwargs["method"].upper()
        if self.proxy:
            kwargs["proxy"] = self.proxy

        if kwargs.get("build_signature"):
            del kwargs["build_signature"]

            if kwargs.get("params") is None:
                kwargs["params"] = {}
            kwargs["params"].update({
                "timestamp": int(time() * 1000),
                "recvWindow": 5000,
            })

            if kwargs["method"] == "POST":
                all_params = {**kwargs.get("params", {}), **kwargs.get("data", {})}
                kwargs["data"] = all_params
                if kwargs.get("params"): del kwargs["params"]
            else:
                all_params = kwargs.get("params", {})

            for k, v in self._build_signature(all_params, kwargs["method"]).items():
                if kwargs.get(k) is None:
                    kwargs[k] = v
                else:
                    kwargs[k].update(v)

        return await session.request(**kwargs)


    async def get_tokens_data(self):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v1/exchangeInfo',
        )
        response = await r.json()
        if response.get("symbols") is None:
            raise Exception(f'Failed to get tokens data: {response}')
        return response["symbols"]


    async def create_order(self, order_data: dict):
        r = await self.send_request(
            method="POST",
            url=f'{self.BASE_URL}/fapi/v1/order',
            data=order_data,
            build_signature=True,
        )
        response = await r.json()
        if response.get("orderId") is None:
            raise Exception(f'Failed to create order: {response}')
        return response


    async def get_balance(self):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v2/balance',
            build_signature=True,
        )
        response = await r.json()
        if type(response) is not list:
            raise Exception(f'Failed to get balance: {response}')

        return next((float(token["availableBalance"]) for token in response if token["asset"] == "USDT"), 0)


    async def get_token_price(self, token_name: str):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v1/ticker/price',
            params={"symbol": token_name + "USDT"},
        )
        response = await r.json()
        if response.get("price") is None:
            raise Exception(f'Failed to get {token_name} price: {response}')

        return Decimal(response["price"])


    async def get_leverages(self):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v4/account',
            build_signature=True,
        )
        response = await r.json()
        if response.get("positions") is None:
            raise Exception(f'Failed to get leverages: {response}')

        return {p["symbol"].removesuffix("USDT"): int(p["leverage"]) for p in response["positions"]}


    async def change_leverage(self, token_name: str, leverage: int):
        r = await self.send_request(
            method="POST",
            url=f'{self.BASE_URL}/fapi/v1/leverage',
            data={
                "symbol": f"{token_name}USDT",
                "leverage": leverage,
            },
            build_signature=True,
        )
        response = await r.json()
        if response.get("leverage") != leverage:
            raise Exception(f'Failed to change leverage: {response}')

        return True


    async def get_account_positions(self):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v2/positionRisk',
            build_signature=True,
        )
        response = await r.json()
        if type(response) is not list:
            raise Exception(f'Failed to get account positions: {response}')

        return [p for p in response if Decimal(p["positionAmt"])]


    async def get_account_orders(self):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v1/openOrders',
            build_signature=True,
        )
        response = await r.json()
        if type(response) is not list:
            raise Exception(f'Failed to get account orders: {response}')

        return response


    async def close_all_open_orders(self, token_name: str):
        r = await self.send_request(
            method="DELETE",
            url=f'{self.BASE_URL}/fapi/v1/allOpenOrders',
            params={"symbol": f"{token_name}USDT"},
            build_signature=True,
        )
        response = await r.json()
        if response != {'code': 200, 'msg': 'The operation of cancel all open order is done.'}:
            raise Exception(f'Failed to close {token_name} orders: {response}')

        return True


    async def get_token_order_book(self, token_name: str):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v1/depth',
            params={"symbol": f"{token_name}USDT", "limit": 50},
        )
        response = await r.json()
        if response.get("bids") is None or response.get("asks") is None:
            raise Exception(f'Failed to get {token_name} order book: {response}')

        return {"BUY": float(response["bids"][0][0]), "SELL": float(response["asks"][0][0])}


    async def get_order_result(self, token_name: str, order_id: int):
        r = await self.send_request(
            method="GET",
            url=f'{self.BASE_URL}/fapi/v1/order',
            params={"symbol": f"{token_name}USDT", "orderId": order_id},
            build_signature=True,
        )
        response = await r.json()
        if response.get("status") is None:
            raise Exception(f'Failed to get {token_name} order: {response}')

        return response


    async def cancel_order(self, token_name: str, order_id: int):
        r = await self.send_request(
            method="DELETE",
            url=f'{self.BASE_URL}/fapi/v1/order',
            params={"symbol": f"{token_name}USDT", "orderId": order_id},
            build_signature=True,
        )
        response = await r.json()
        if response.get("status") != "CANCELED":
            raise Exception(f'Failed to close {token_name} order: {response}')

        return True
