from random import choice, uniform, shuffle, randint
from decimal import Decimal
from loguru import logger
from time import time
import asyncio
import json

from .browser import Browser
from .database import DataBase
from .retry import async_retry, CustomError
from .utils import sleeping, round_cut, make_border, async_sleep
from settings import (
    SLEEP_BETWEEN_CLOSE_ORDERS,
    SLEEP_BETWEEN_OPEN_ORDERS,
    SLEEP_AFTER_FUTURE,
    STOP_LOSS_SETTING,
    TOKENS_TO_TRADE,
    FUTURES_LIMITS,
    FUTURE_ACTIONS,
    PAIR_SETTINGS,
    TRADE_AMOUNTS,
    CANCEL_ORDERS,
    RETRY,
)


class AsterClient:
    switch_params: dict = {
        "BUY": "SELL",
        "SELL": "BUY",
    }
    actions_name: dict = {
        "BUY": "Long",
        "SELL": "Short",
    }
    switch_actions: dict = {
        "Long": "BUY",
        "Short": "SELL",
    }

    TOKENS_DATA: dict = {}

    def __init__(
            self,
            browser: Browser,
            apikey: str,
            encoded_apikey: str,
            address: str,
            label: str,
            proxy: str | None,
            db: DataBase,
            group_data: dict = None
    ):
        self.browser = browser
        self.apikey = apikey
        self.encoded_apikey = encoded_apikey
        self.address = address
        self.label = label
        self.db = db
        if group_data:
            self.group_number = group_data["group_number"]
            self.encoded_apikey = group_data["group_index"]
            self.prefix = f"[<i>{self.label}</i>] "
        else:
            self.group_number = None
            self.prefix = ""

        if proxy in [None, "", " ", "\n", 'http://log:pass@ip:port']:
            self.proxy = None
        else:
            self.proxy = "http://" + proxy.removeprefix("https://").removeprefix("http://")


        self.leverages = {}
        self.mode = None


    async def run_mode(self, mode: int):
        self.mode = mode
        await self.fetch_account_info()

        if mode == 1:
            status = await self.open_and_close_position(orders_type="MARKET")

        elif mode == 2:
            status = await self.open_and_close_position(orders_type="LIMIT")

        elif mode == 4:
            status = await self.close_positions()

        return status


    async def fetch_account_info(self):
        self.leverages, _ = await asyncio.gather(*[
            self.browser.get_leverages(),
            self.fetch_tokens_data(),
        ])


    async def change_leverage(self, token_name: str, leverage: int):
        if self.leverages[token_name] != leverage:
            await self.browser.change_leverage(token_name, leverage)
            self.log_message(f"Changed leverage <white>{self.leverages[token_name]}x</white> → <white>{leverage}x</white>")
            self.leverages[token_name] = leverage


    async def fetch_tokens_data(self):
        def count_digits(number_str):
            return number_str[2:].index('1') + 1 if number_str.startswith("0.") and '1' in number_str[2:] else 0

        if self.TOKENS_DATA:
            return

        for t in await self.browser.get_tokens_data():
            token_name = t["baseAsset"]
            formatted_filters = {f["filterType"]: f for f in t["filters"]}

            price_decimal = count_digits(formatted_filters["PRICE_FILTER"]["tickSize"])
            size_decimal = count_digits(
                max(formatted_filters["MARKET_LOT_SIZE"]["minQty"], formatted_filters["LOT_SIZE"]["minQty"]))

            self.TOKENS_DATA[token_name] = {"price": price_decimal, "size": size_decimal}


    async def open_and_close_position(self, orders_type: str):
        kwargs = {}
        possible_actions = [k for k, v in FUTURE_ACTIONS.items() if v]
        if not possible_actions:
            raise CustomError(f'You must enable Long/Short in settings!')

        futures_action = self.switch_actions[choice(possible_actions)]
        token_name = choice(list(TOKENS_TO_TRADE.keys()))

        await self.close_positions()
        await self.wait_for_price(token_name)

        usd_amount, leverage = await self.calculate_usdc_amount_for_order(token_name)
        await self.change_leverage(token_name, leverage)

        open_order_data = await self.place_order(
            token=token_name,
            side=futures_action,
            order_type=orders_type,
            usd_amount=usd_amount,
        )

        to_sleep = randint(*SLEEP_AFTER_FUTURE)
        if to_sleep:
            self.log_message(f'Sleep <white>{to_sleep}s</white> before close position')
            await async_sleep(to_sleep)

        if orders_type == "LIMIT":
            kwargs = {
                "price": self.calculate_limit_order_price(
                    Decimal(open_order_data["price"]),
                    self.switch_params[futures_action]
                ),
            }

        close_order_data = await self.place_order(
            token=token_name,
            side=self.switch_params[futures_action],
            order_type=orders_type,
            token_amount=float(open_order_data["executedQty"]),
            is_close=True,
            **kwargs
        )

        price_diff = Decimal(open_order_data["avgPrice"]) - Decimal(close_order_data["avgPrice"])
        if futures_action == "BUY":
            price_diff *= -1
        profit = round(Decimal(open_order_data["executedQty"]) * price_diff, 3)
        self.log_message(f"Profit: {profit}$", "+", "INFO")
        await self.db.append_report(
            key=self.encoded_apikey,
            text=f'\n{self.prefix}💰 <b>profit {profit}$</b>',
        )

        return True


    async def wait_for_price(self, token_name: str):
        first_check = True
        while True:
            token_price = await self.browser.get_token_price(token_name)
            if TOKENS_TO_TRADE[token_name]["prices"][0] <= token_price <= TOKENS_TO_TRADE[token_name]["prices"][1]:
                return

            elif first_check:
                first_check = False
                self.log_message(f'{token_name} current price {token_price}. Waiting for price in range {"-".join(str(v) for v in TOKENS_TO_TRADE[token_name]["prices"])}.')

            await async_sleep(5)


    async def close_positions(self, force_close: bool = False):
        positions, open_orders = None, None
        if self.mode != 4 and not FUTURES_LIMITS["close_previous"]:
            return

        if self.mode == 4 or force_close:
            additional_str, level = "", "INFO"
        else:
            additional_str, level = " before opening new...", "DEBUG"

        if self.mode != 4 or CANCEL_ORDERS["positions"] or force_close:
            positions = await self.browser.get_account_positions()
            if positions:
                self.log_message(f"Closing {len(positions)} positions{additional_str}", level=level)
                for position in positions:
                    await self.place_order(
                        token=position["symbol"].removesuffix("USDT"),
                        side="BUY" if Decimal(position["positionAmt"]) < 0 else "SELL",
                        order_type="MARKET",
                        token_amount=abs(Decimal(position["positionAmt"])),
                        is_close=True,
                    )

        if self.mode != 4 or CANCEL_ORDERS["orders"] or force_close:
            open_orders = await self.browser.get_account_orders()
            if open_orders:
                self.log_message(f"Closing {len(open_orders)} open orders{additional_str}", level=level)
                for token_name in list(set([o["symbol"].removesuffix("USDT") for o in open_orders])):
                    await self.browser.close_all_open_orders(token_name=token_name)

                await self.db.append_report(
                    key=self.encoded_apikey,
                    text=f"{self.prefix}close {len(open_orders)} open orders",
                    success=True,
                )

        if self.mode == 4 and not positions and not open_orders:
            self.log_message(f"No position and orders to close found", level=level)
            await self.db.append_report(
                key=self.encoded_apikey,
                text=f"{self.prefix}no position and orders to close found",
                success=True,
            )

        return True


    async def calculate_usdc_amount_for_order(self, token_name: str):
        leverage = max(1, randint(*TOKENS_TO_TRADE[token_name]["leverage"]))
        balance = Decimal(await self.browser.get_balance())
        if TRADE_AMOUNTS["amount"] != [0, 0]:
            amounts = TRADE_AMOUNTS["amount"].copy()
            if amounts[0] > balance:
                raise CustomError(f'Not enough balance, need at least {amounts[0]}$, but have {round(balance, 2)}$')
            elif amounts[1] > balance:
                amounts[1] = float(balance)
            usdc_amount = Decimal(str(uniform(*amounts)))
        else:
            percent = uniform(*TRADE_AMOUNTS["percent"]) / 100
            usdc_amount = balance * Decimal(str(percent))

        return usdc_amount * Decimal(leverage), leverage


    async def place_order(
            self,
            token: str,
            side: str,
            order_type: str,
            token_amount : float | Decimal = 0,
            usd_amount : float | Decimal = 0,
            price: float = 0,
            is_close: bool = False,
            to_sleep: int = 0,
    ):
        order_action = "Close" if is_close else "Open"
        action_name = self.actions_name[self.switch_params[side]] if is_close else self.actions_name[side]

        if to_sleep:
            self.log_message(f"Sleep {to_sleep}s before {action_name}")
            await async_sleep(to_sleep)

        token_price, token_prices = await asyncio.gather(*[
            self.browser.get_token_price(token),
            self.browser.get_token_order_book(token),
        ])
        if usd_amount:
            token_amount = usd_amount / token_price

        token_amount = float(round_cut(token_amount, self.TOKENS_DATA[token]["size"]))

        order_data = {
            "symbol": f"{token}USDT",
            "side": side,
            "positionSide": "BOTH",
            "type": order_type,
            "quantity": token_amount,
        }
        if order_type == "LIMIT":
            if not price:
                min_price = token_prices[side]
                if side == "BUY":
                    price = min_price - uniform(*TOKENS_TO_TRADE[token]["open_price"])
                else:
                    price = min_price + uniform(*TOKENS_TO_TRADE[token]["open_price"])

            order_data.update({
                "price": float(round_cut(price, self.TOKENS_DATA[token]["price"])),
                "timeInForce": "GTC",
            })

        elif order_type == "MARKET":
            order_data.update({
                "newOrderRespType": "RESULT",
                # "reduceOnly": True,
            })

        try:
            order_result = await self.browser.create_order(order_data)
        except Exception as err:
            if self.group_number:
                prefix = f"{self.label} | "
            else:
                prefix = ""
            raise Exception(f"{prefix}{err}")

        if order_type == "LIMIT":
            limit_order_result = await self.wait_for_limit_order_filled(
                token_name=token,
                order_id=order_result["orderId"],
                is_close=is_close,
                order_data=order_data
            )
            if limit_order_result == "reopen":
                await self.browser.cancel_order(token_name=token, order_id=order_result["orderId"])
                return await self.place_order(
                    token=token,
                    side=side,
                    order_type=order_type,
                    token_amount=token_amount,
                    is_close=is_close,
                )

            elif limit_order_result == "stop-loss":
                await self.browser.cancel_order(token_name=token, order_id=order_result["orderId"])
                return await self.place_order(
                    token=token,
                    side=side,
                    order_type="MARKET",
                    token_amount=token_amount,
                    is_close=is_close,
                )

            else:
                order_result = limit_order_result

        pos_usd = round(float(order_result["cumQuote"]), 2)
        pos_price = round_cut(order_result['avgPrice'], self.TOKENS_DATA[token]["price"])
        self.log_message(
            text=f"{order_action} {order_type.lower()} order "
                 f"<green>{action_name} {token_amount} {token} at {pos_price} (${pos_usd})</green>",
            level="INFO"
        )

        await self.db.append_report(
            key=self.encoded_apikey,
            text=f"{self.prefix}{order_action.lower()} {order_type.lower()} {action_name} {token_amount} {token} at {pos_price} (${pos_usd})",
            success=True
        )

        return order_result


    async def wait_for_limit_order_filled(
            self,
            token_name: str,
            order_id: int,
            is_close: bool,
            order_data: dict,
    ):
        order_action = "Close" if is_close else "Open"
        action_name = self.actions_name[self.switch_params[order_data["side"]]] if is_close else self.actions_name[order_data["side"]]
        minutes_str = f"{FUTURES_LIMITS['to_wait']} minute{'s' if FUTURES_LIMITS['to_wait'] > 1 else ''}" if not is_close else ""
        trigger_price = float(round_cut(self.calculate_trigger_price(
            side=order_data["side"],
            token_price=Decimal(str(order_data["price"]))
        ), self.TOKENS_DATA[token_name]["price"]))
        trigger_str = f" <red>Stop-loss at {trigger_price}</red>" if trigger_price and is_close else ""
        deadline = int(time() + FUTURES_LIMITS['to_wait'] * 60)

        self.log_message(
            f"Waiting for {order_action} limit order <white>{action_name} {order_data['quantity']} {token_name} "
            f"at {order_data['price']} (${round(order_data['price'] * order_data['quantity'], 2)})</white>"
            f"{trigger_str} filled{' for ' if minutes_str else ''}{minutes_str}..."
        )

        while True:
            order_result = await self.browser.get_order_result(token_name=token_name, order_id=order_id)

            if order_result["status"] == "NEW":
                if not is_close:
                    if time() >= deadline:
                        self.log_message(f"Order not filled in {minutes_str}, changing price", level="WARNING")
                        return "reopen"

                else:
                    if trigger_price:
                        current_token_price = (await self.browser.get_token_order_book(token_name))[order_data["side"]]
                        if (
                                (order_data["side"] == "BUY" and current_token_price >= trigger_price) or
                                (order_data["side"] == "SELL" and current_token_price <= trigger_price)
                        ):
                            self.log_message(
                                f"Stop-Loss: Closing position, current price <red>{current_token_price}</red>",
                                level="WARNING"
                            )
                            return "stop-loss"

            elif order_result["status"] == "FILLED":
                break

            else:
                raise Exception(f'Unexpected order result: {order_result}')

            await async_sleep(3)

        return order_result


    async def check_for_balance(self, needed_balance: float):
        balance = Decimal(await self.browser.get_balance())
        if needed_balance > balance:
            raise CustomError(f'Not enough balance, need at least {needed_balance}$, but have {round(balance, 2)}$')
        return balance


    @classmethod
    def calculate_limit_order_price(cls, old_price: Decimal, side: str):
        new_percent, new_amount = cls.calculate_setting_difference(
            amounts=FUTURES_LIMITS["price_diff_amount"],
            percents=FUTURES_LIMITS["price_diff_percent"],
        )
        if side == "SELL":
            if new_amount:
                token_price_raw = old_price + new_amount
            else:
                token_price_raw = old_price * new_percent
        else:
            if new_amount:
                token_price_raw = old_price - new_amount
            else:
                token_price_raw = old_price / new_percent

        return token_price_raw


    @classmethod
    def calculate_setting_difference(cls, amounts: list, percents: list):
        percent, amount = 0, 0
        if amounts != [0, 0]:
            amount = Decimal(str(uniform(*amounts)))
        else:
            random_percent = uniform(*percents)
            percent = Decimal("1") + Decimal(str(random_percent)) / 100

        return percent, amount


    @classmethod
    def calculate_trigger_price(cls, side: str, token_price: Decimal):
        if STOP_LOSS_SETTING["enable"]:
            trigger_percent, trigger_amount = cls.calculate_setting_difference(
                amounts=STOP_LOSS_SETTING["loss_diff_amount"],
                percents=STOP_LOSS_SETTING["loss_diff_percent"]
            )
            if  side == "BUY":
                if trigger_amount:
                    trigger_price = token_price + abs(trigger_amount)
                else:
                    trigger_price = token_price * abs(trigger_percent)
            else:
                if trigger_amount:
                    trigger_price = token_price - abs(trigger_amount)
                else:
                    trigger_price = token_price / abs(trigger_percent)

        else:
            trigger_price = 0

        return trigger_price


    def log_message(
            self,
            text: str,
            smile: str = "•",
            level: str = "DEBUG",
            colors: bool = True
    ):
        if self.group_number:
            if colors:
                label = f"<white>Group {self.group_number}</white> | <white>{self.label}</white>"
            else:
                label = f"Group {self.group_number} | {self.label}"
        else:
            label = f"<white>{self.label}</white>" if colors else self.label
        logger.opt(colors=colors).log(level.upper(), f'[{smile}] {label} | {text}')


class PairAccounts:
    def __init__(self, accounts: list[AsterClient], group_data: dict):
        self.accounts = accounts
        self.group_number = f"Group {group_data['group_number']}"
        self.group_index = group_data["group_index"]


    async def run(self, mode: int):
        for acc in self.accounts:
            acc.mode = mode
        await asyncio.gather(*[
            acc.fetch_account_info()
            for acc in self.accounts
        ])

        for account in self.get_randomized_accs(self.accounts):
            await account.close_positions()

        return await self.open_and_close_position()


    async def open_and_close_position(self):
        token_name = choice(list(TOKENS_TO_TRADE.keys()))
        first_check = True
        while True:
            token_price = await choice(self.accounts).browser.get_token_price(token_name)
            if TOKENS_TO_TRADE[token_name]["prices"][0] <= token_price <= TOKENS_TO_TRADE[token_name]["prices"][1]:
                break

            elif first_check:
                first_check = False
                self.log_group_message(
                    text=f'{token_name} current price {token_price}. Waiting for price'
                         f' in range {"-".join(str(v) for v in TOKENS_TO_TRADE[token_name]["prices"])}.',
                )

            await async_sleep(5)

        # OPEN
        begin_token_price = await choice(self.accounts).browser.get_token_price(token_name)
        open_values = self.calculate_deltaneutral_amounts(
            decimals=self.accounts[0].TOKENS_DATA[token_name]["size"],
            min_amount=TRADE_AMOUNTS["amount"][0] / float(begin_token_price),
            max_amount=TRADE_AMOUNTS["amount"][1] / float(begin_token_price),
            min_leverage=TOKENS_TO_TRADE[token_name]["leverage"][0],
            max_leverage=TOKENS_TO_TRADE[token_name]["leverage"][1],
        )
        await asyncio.gather(*[
            account.check_for_balance(needed_balance=open_values[account.address]["amount"])
            for account in self.accounts
        ])
        for acc in self.get_randomized_accs(self.accounts):
            await acc.change_leverage(token_name, open_values[acc.address]["leverage"])

        max_valuable_account_address = max(open_values, key=lambda k: open_values[k]['amount'])
        max_valuable_account = next(acc for acc in self.accounts if acc.address == max_valuable_account_address)
        market_accounts = [acc for acc in self.accounts if acc.address != max_valuable_account_address]

        open_order_data = await max_valuable_account.place_order(
            token=token_name,
            side=open_values[max_valuable_account.address]["side"],
            order_type="LIMIT",
            token_amount=open_values[max_valuable_account.address]["leveraged_amount"],
        )

        tasks = []
        to_sleep_total = 0
        for acc_index, account in enumerate(market_accounts):
            random_sleep = randint(*SLEEP_BETWEEN_OPEN_ORDERS) if acc_index else 0
            to_sleep = to_sleep_total + random_sleep
            to_sleep_total += random_sleep
            tasks.append(
                account.place_order(
                    token=token_name,
                    side=open_values[account.address]["side"],
                    token_amount=open_values[account.address]["leveraged_amount"],
                    order_type="MARKET",
                    to_sleep=to_sleep,
                )
            )

        try:
            opened_positions = await asyncio.gather(*tasks)
            formatted_positions = {account.address: opened_positions[acc_index] for acc_index, account in enumerate(market_accounts)}
            formatted_positions[max_valuable_account.address] = open_order_data
            opened_positions.append(open_order_data)
        except Exception as e:
            self.log_group_message(
                text=f"Failed to open {token_name} orders: {e}. Closing all positions...",
                smile="-",
                level="ERROR",
            )
            await self.accounts[-1].db.append_report(
                key=self.accounts[-1].encoded_apikey,
                text=f'failed to open {token_name} order',
                success=False
            )

            for account in self.get_randomized_accs(self.accounts):
                await account.close_positions(force_close=True)
            return False

        to_sleep = randint(*PAIR_SETTINGS["position_hold"])
        self.log_group_message(text=f"Sleeping {to_sleep}s before close positions...")
        await async_sleep(to_sleep)


        close_order_data = await max_valuable_account.place_order(
            token=token_name,
            side=max_valuable_account.switch_params[open_values[max_valuable_account.address]["side"]],
            order_type="LIMIT",
            token_amount=float(formatted_positions[max_valuable_account.address]["executedQty"]),
            is_close=True,
        )
        tasks = []
        to_sleep_total = 0
        randomized_accs = self.get_randomized_accs(market_accounts)
        for acc_index, account in enumerate(randomized_accs):
            random_sleep = randint(*SLEEP_BETWEEN_CLOSE_ORDERS) if acc_index else 0
            to_sleep = to_sleep_total + random_sleep
            to_sleep_total += random_sleep
            tasks.append(
                account.place_order(
                    side=account.switch_params[open_values[account.address]["side"]],
                    token=token_name,
                    token_amount=float(formatted_positions[account.address]["executedQty"]),
                    order_type="MARKET",
                    is_close=True,
                    to_sleep=to_sleep,
                )
            )
        try:
            closed_positions = await asyncio.gather(*tasks)
            closed_positions.append(close_order_data)

        except Exception as e:
            self.log_group_message(
                text=f"Failed to close {token_name} position: {e}. Closing all positions...",
                smile="-",
                level="ERROR",
            )
            await self.accounts[-1].db.append_report(
                key=self.accounts[-1].encoded_apikey,
                text=f'failed to close {token_name} position',
                success=False
            )

            for account in self.get_randomized_accs(self.accounts):
                await account.close_positions(force_close=True)
            return False

        total_profit = 0
        total_volume = 0
        for pos in opened_positions + closed_positions:
            pos_amount = Decimal(pos["executedQty"]) * Decimal(pos["avgPrice"])
            if pos["side"] == "BUY":
                pos_amount *= -1
            total_profit += pos_amount
            total_volume += abs(pos_amount)

        total_profit = round(total_profit, 3)
        total_volume = round(total_volume, 1)
        hundred_thousand_cost = round(-total_profit / total_volume * 100000, 3)
        self.log_group_message(
            text=f"Profit: <green>{total_profit}$</green> | "
                f"Total Volume: <green>{total_volume}$</green> | "
                f"100k$ Volume Cost: <green>{hundred_thousand_cost}$</green>",
            smile="+",
            level="INFO"
        )
        await self.accounts[-1].db.append_report(
            key=self.accounts[-1].encoded_apikey,
            text=f'\n💰 <b>profit {total_profit}$</b>'
                 f'\n💵 <b>volume {total_volume}$</b>'
                 f'\n💍 <b>100k$ volume cost: {hundred_thousand_cost}$</b>',
        )
        return True


    def get_randomized_accs(self, lst: list):
        randomized_accounts = lst[:]
        shuffle(randomized_accounts)
        return randomized_accounts


    def calculate_deltaneutral_amounts(
            self,
            decimals: int,
            min_amount: float,
            max_amount: float,
            min_leverage: int,
            max_leverage: int,
    ):
        def _find_factors_for_notional(
                target_notional_int: int,
                min_amount_int: int,
                max_amount_int: int,
                min_leverage: int,
                max_leverage: int
        ):
            possible_pairs = []
            for leverage in range(min_leverage, max_leverage + 1):
                if target_notional_int % leverage == 0:
                    amount_int = target_notional_int // leverage
                    if min_amount_int <= amount_int <= max_amount_int:
                        possible_pairs.append((amount_int, leverage))

            if not possible_pairs:
                return None

            return choice(possible_pairs)

        def _distribute_int_sum(
                total_sum: int,
                num_parts: int,
                min_val: int,
                max_val: int
        ):
            if num_parts == 0:
                return []

            parts = []
            remaining_sum = total_sum

            for i in range(num_parts - 1):
                upper_bound = min(max_val, remaining_sum - (num_parts - 1 - i) * min_val)
                lower_bound = max(min_val, remaining_sum - (num_parts - 1 - i) * max_val)

                if lower_bound > upper_bound:
                    return None

                part = randint(lower_bound, upper_bound)
                parts.append(part)
                remaining_sum -= part

            if not (min_val <= remaining_sum <= max_val):
                return None

            parts.append(remaining_sum)
            shuffle(parts)
            return parts

        def _try_generate_positions(
                decimals: int,
                min_amount: float,
                max_amount: float,
                min_leverage: int,
                max_leverage: int
        ):
            multiplier = 10 ** decimals
            min_amount_int = int(min_amount * multiplier)
            max_amount_int = int(max_amount * multiplier)

            addresses = [acc.address for acc in self.get_randomized_accs(self.accounts)]
            num_addresses = len(addresses)
            if num_addresses < 2: return None

            num_longs = randint(1, num_addresses - 1)
            num_shorts = num_addresses - num_longs

            if num_longs <= num_shorts:
                source_num, target_num = num_longs, num_shorts
                source_side, target_side = "BUY", "SELL"
            else:
                source_num, target_num = num_shorts, num_longs
                source_side, target_side = "SELL", "BUY"

            source_positions = []
            target_notional_int = 0
            for _ in range(source_num):
                amount_int = randint(min_amount_int, max_amount_int)
                leverage = randint(min_leverage, max_leverage)
                source_positions.append({
                    "side": source_side,
                    "amount": amount_int / multiplier,
                    "leverage": leverage,
                    "leveraged_amount": amount_int / multiplier * leverage
                })
                target_notional_int += amount_int * leverage

            min_notional_int_per_pos = min_amount_int * min_leverage
            max_notional_int_per_pos = max_amount_int * max_leverage

            notional_parts = _distribute_int_sum(
                target_notional_int,
                target_num,
                min_notional_int_per_pos,
                max_notional_int_per_pos
            )

            if notional_parts is None:
                return None

            target_positions = []
            for part in notional_parts:
                pair = _find_factors_for_notional(part, min_amount_int, max_amount_int, min_leverage, max_leverage)
                if pair is None:
                    return None

                amount_int, leverage = pair
                target_positions.append({
                    "side": target_side,
                    "amount": amount_int / multiplier,
                    "leverage": leverage,
                    "leveraged_amount": amount_int / multiplier * leverage
                })

            return {
                addr: pos_data
                for addr, pos_data in zip(addresses, target_positions + source_positions)
            }

        if len(self.accounts) % 2:  # if odd
            leveraged_diff = (max_amount * max_leverage) / (min_amount * min_leverage)
            if len(self.accounts) == 3:
                if leveraged_diff < 2.2:
                    min_amount = (max_amount * max_leverage) / (min_leverage * 2.2)
            else:
                if leveraged_diff < 2:
                    min_amount = (max_amount * max_leverage) / (min_leverage * 2)

        for _ in range(1000):
            result = _try_generate_positions(decimals, min_amount, max_amount, min_leverage, max_leverage)
            if result:
                return result

        raise Exception("Failed to calculate delta neutral positions")


    def log_group_message(
            self,
            text: str,
            smile: str = "•",
            level: str = "DEBUG",
            colors: bool = True,
            account_label: str = ""
    ):
        label = f"<white>{self.group_number}</white>" if colors else self.group_number
        if account_label:
            if colors:
                label += f" | <white>{account_label}</white>"
            else:
                label += f" | {account_label}"
        logger.opt(colors=colors).log(level.upper(), f'[{smile}] {label} | {text}')