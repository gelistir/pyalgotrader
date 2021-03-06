""""""

import hashlib
import hmac
import sys
import time
from copy import copy
from datetime import datetime, timedelta
from threading import Lock
from urllib.parse import urlencode

from requests import ConnectionError

from vnpy.event import Event
from vnpy.api.rest import Request, RestClient
from vnpy.api.websocket import WebsocketClient
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.constant import (
    Direction,
    Exchange,
    OrderType,
    Product,
    Status,
    Offset,
    Interval
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    TickData,
    OrderData,
    TradeData,
    PositionData,
    AccountData,
    ContractData,
    BarData,
    OrderRequest,
    CancelRequest,
    SubscribeRequest,
    HistoryRequest
)

REST_HOST = "https://www.bitmex.com/api/v1"
WEBSOCKET_HOST = "wss://www.bitmex.com/realtime"

TESTNET_REST_HOST = "https://testnet.bitmex.com/api/v1"
TESTNET_WEBSOCKET_HOST = "wss://testnet.bitmex.com/realtime"

STATUS_BITMEX2VT = {
    "New": Status.NOTTRADED,
    "Partially filled": Status.PARTTRADED,
    "Filled": Status.ALLTRADED,
    "Canceled": Status.CANCELLED,
    "Rejected": Status.REJECTED,
}

DIRECTION_VT2BITMEX = {Direction.LONG: "Buy", Direction.SHORT: "Sell"}
DIRECTION_BITMEX2VT = {v: k for k, v in DIRECTION_VT2BITMEX.items()}

ORDERTYPE_VT2BITMEX = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "Market",
    OrderType.STOP: "Stop"
}
ORDERTYPE_BITMEX2VT = {v: k for k, v in ORDERTYPE_VT2BITMEX.items()}

INTERVAL_VT2BITMEX = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "1h",
    Interval.DAILY: "1d",
}

TIMEDELTA_MAP = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}


class BitmexGateway(BaseGateway):
    """
    VN Trader Gateway for BitMEX connection.
    """

    default_setting = {
        "ID": "",
        "Secret": "",
        " sessions ": 3,
        " server ": ["REAL", "TESTNET"],
        " proxy address ": "",
        " proxy port ": "",
    }

    exchanges = [Exchange.BITMEX]

    def __init__(self, event_engine):
        """Constructor"""
        super(BitmexGateway, self).__init__(event_engine, "BITMEX")

        self.rest_api = BitmexRestApi(self)
        self.ws_api = BitmexWebsocketApi(self)

        event_engine.register(EVENT_TIMER, self.process_timer_event)

    def connect(self, setting: dict):
        """"""
        key = setting["ID"]
        secret = setting["Secret"]
        session_number = setting[" sessions "]
        server = setting[" server "]
        proxy_host = setting[" proxy address "]
        proxy_port = setting[" proxy port "]

        if proxy_port.isdigit():
            proxy_port = int(proxy_port)
        else:
            proxy_port = 0

        self.rest_api.connect(key, secret, session_number,
                              server, proxy_host, proxy_port)

        self.ws_api.connect(key, secret, server, proxy_host, proxy_port)
        # websocket will push all account status on connected, including asset, position and orders.

    def subscribe(self, req: SubscribeRequest):
        """"""
        self.ws_api.subscribe(req)

    def send_order(self, req: OrderRequest):
        """"""
        return self.rest_api.send_order(req)

    def cancel_order(self, req: CancelRequest):
        """"""
        self.rest_api.cancel_order(req)

    def query_account(self):
        """"""
        pass

    def query_position(self):
        """"""
        pass

    def query_history(self, req: HistoryRequest):
        """"""
        return self.rest_api.query_history(req)

    def close(self):
        """"""
        self.rest_api.stop()
        self.ws_api.stop()

    def process_timer_event(self, event: Event):
        """"""
        self.rest_api.reset_rate_limit()


class BitmexRestApi(RestClient):
    """
    BitMEX REST API
    """

    def __init__(self, gateway: BaseGateway):
        """"""
        super(BitmexRestApi, self).__init__()

        self.gateway = gateway
        self.gateway_name = gateway.gateway_name

        self.key = ""
        self.secret = ""

        self.order_count = 1_000_000
        self.order_count_lock = Lock()

        self.connect_time = 0

        # Use 60 by default, and will update after first request
        self.rate_limit_limit = 60
        self.rate_limit_remaining = 60
        self.rate_limit_sleep = 0

    def sign(self, request):
        """
        Generate BitMEX signature.
        """
        # Sign
        expires = int(time.time() + 30)

        if request.params:
            query = urlencode(request.params)
            path = request.path + "?" + query
        else:
            path = request.path

        if request.data:
            request.data = urlencode(request.data)
        else:
            request.data = ""

        msg = request.method + "/api/v1" + path + str(expires) + request.data
        signature = hmac.new(
            self.secret, msg.encode(), digestmod=hashlib.sha256
        ).hexdigest()

        # Add headers
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "api-key": self.key,
            "api-expires": str(expires),
            "api-signature": signature,
        }

        request.headers = headers
        return request

    def connect(
        self,
        key: str,
        secret: str,
        session_number: int,
        server: str,
        proxy_host: str,
        proxy_port: int,
    ):
        """
        Initialize connection to REST server.
        """
        self.key = key
        self.secret = secret.encode()

        self.connect_time = (
            int(datetime.now().strftime("%y%m%d%H%M%S")) * self.order_count
        )

        if server == "REAL":
            self.init(REST_HOST, proxy_host, proxy_port)
        else:
            self.init(TESTNET_REST_HOST, proxy_host, proxy_port)

        self.start(session_number)

        self.gateway.write_log("REST API successful start ")

    def _new_order_id(self):
        """"""
        with self.order_count_lock:
            self.order_count += 1
            return self.order_count

    def send_order(self, req: OrderRequest):
        """"""
        if not self.check_rate_limit():
            return ""

        orderid = str(self.connect_time + self._new_order_id())

        data = {
            "symbol": req.symbol,
            "side": DIRECTION_VT2BITMEX[req.direction],
            "ordType": ORDERTYPE_VT2BITMEX[req.type],
            "orderQty": int(req.volume),
            "clOrdID": orderid,
        }

        inst = []   # Order special instructions

        # Only add price for limit order.
        if req.type == OrderType.LIMIT:
            data["price"] = req.price
        elif req.type == OrderType.STOP:
            data["stopPx"] = req.price
            inst.append("LastPrice")

        # Check for close order
        if req.offset == Offset.CLOSE:
            inst.append("ReduceOnly")

        # Generate execInst
        if inst:
            data["execInst"] = ",".join(inst)

        order = req.create_order_data(orderid, self.gateway_name)

        self.add_request(
            "POST",
            "/order",
            callback=self.on_send_order,
            data=data,
            extra=order,
            on_failed=self.on_send_order_failed,
            on_error=self.on_send_order_error,
        )

        self.gateway.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest):
        """"""
        if not self.check_rate_limit():
            return

        orderid = req.orderid

        if orderid.isdigit():
            params = {"clOrdID": orderid}
        else:
            params = {"orderID": orderid}

        self.add_request(
            "DELETE",
            "/order",
            callback=self.on_cancel_order,
            params=params,
            on_error=self.on_cancel_order_error,
        )

    def query_history(self, req: HistoryRequest):
        """"""
        if not self.check_rate_limit():
            return

        history = []
        count = 750
        start_time = req.start.isoformat()

        while True:
            # Create query params
            params = {
                "binSize": INTERVAL_VT2BITMEX[req.interval],
                "symbol": req.symbol,
                "count": count,
                "startTime": start_time
            }

            # Add end time if specified
            if req.end:
                params["endTime"] = req.end.isoformat()

            # Get response from server
            resp = self.request(
                "GET",
                "/trade/bucketed",
                params=params
            )

            # Break if request failed with other status code
            if resp.status_code // 100 != 2:
                msg = f" failure to obtain historical data ， status code ：{resp.status_code}， information ：{resp.text}"
                self.gateway.write_log(msg)
                break
            else:
                data = resp.json()
                if not data:
                    msg = f" obtain historical data is empty ， starting time ：{start_time}， quantity ：{count}"
                    break

                for d in data:
                    dt = datetime.strptime(
                        d["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
                    bar = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=dt,
                        interval=req.interval,
                        volume=d["volume"],
                        open_price=d["open"],
                        high_price=d["high"],
                        low_price=d["low"],
                        close_price=d["close"],
                        gateway_name=self.gateway_name
                    )
                    history.append(bar)

                begin = data[0]["timestamp"]
                end = data[-1]["timestamp"]
                msg = f" get historical success ，{req.symbol} - {req.interval.value}，{begin} - {end}"
                self.gateway.write_log(msg)

                # Break if total data count less than 750 (latest date collected)
                if len(data) < 750:
                    break

                # Update start time
                start_time = bar.datetime + TIMEDELTA_MAP[req.interval]

        return history

    def on_send_order_failed(self, status_code: str, request: Request):
        """
        Callback when sending order failed on server.
        """
        self.update_rate_limit(request)

        order = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        if request.response.text:
            data = request.response.json()
            error = data["error"]
            msg = f" commissioned failure ， status code ：{status_code}， types of ：{error['name']},  information ：{error['message']}"
        else:
            msg = f" commissioned failure ， status code ：{status_code}"

        self.gateway.write_log(msg)

    def on_send_order_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ):
        """
        Callback when sending order caused exception.
        """
        order = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        # Record exception if not ConnectionError
        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_send_order(self, data, request):
        """Websocket will push a new order status"""
        self.update_rate_limit(request)

    def on_cancel_order_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ):
        """
        Callback when cancelling order failed on server.
        """
        # Record exception if not ConnectionError
        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_cancel_order(self, data, request):
        """Websocket will push a new order status"""
        self.update_rate_limit(request)

    def on_failed(self, status_code: int, request: Request):
        """
        Callback to handle request failed.
        """
        self.update_rate_limit(request)

        data = request.response.json()
        error = data["error"]
        msg = f" request failed ， status code ：{status_code}， types of ：{error['name']},  information ：{error['message']}"
        self.gateway.write_log(msg)

    def on_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ):
        """
        Callback to handler request exception.
        """
        msg = f" trigger abnormal ， status code ：{exception_type}， information ：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb, request)
        )

    def update_rate_limit(self, request: Request):
        """
        Update current request limit remaining status.
        """
        if request.response is None:
            return
        headers = request.response.headers

        self.rate_limit_remaining = int(headers["x-ratelimit-remaining"])

        self.rate_limit_sleep = int(headers.get("Retry-After", 0))
        if self.rate_limit_sleep:
            self.rate_limit_sleep += 1      # 1 extra second sleep

    def reset_rate_limit(self):
        """
        Reset request limit remaining every 1 second.
        """
        self.rate_limit_remaining += 1
        self.rate_limit_remaining = min(
            self.rate_limit_remaining, self.rate_limit_limit)

        # Countdown of retry sleep seconds
        if self.rate_limit_sleep:
            self.rate_limit_sleep -= 1

    def check_rate_limit(self):
        """
        Check if rate limit is reached before sending out requests.
        """
        # Already received 429 from server
        if self.rate_limit_sleep:
            msg = f" your requests are too frequent ， has been BitMEX limit ， please wait {self.rate_limit_sleep} after the second try "
            self.gateway.write_log(msg)
            return False
        # Just local request limit is reached
        elif not self.rate_limit_remaining:
            msg = " the request frequency is too high ， there are triggered BitMEX flow control risk ， please try again later "
            self.gateway.write_log(msg)
            return False
        else:
            self.rate_limit_remaining -= 1
            return True


class BitmexWebsocketApi(WebsocketClient):
    """"""

    def __init__(self, gateway):
        """"""
        super(BitmexWebsocketApi, self).__init__()

        self.gateway = gateway
        self.gateway_name = gateway.gateway_name

        self.key = ""
        self.secret = ""

        self.callbacks = {
            "trade": self.on_tick,
            "orderBook10": self.on_depth,
            "execution": self.on_trade,
            "order": self.on_order,
            "position": self.on_position,
            "margin": self.on_account,
            "instrument": self.on_contract,
        }

        self.ticks = {}
        self.accounts = {}
        self.orders = {}
        self.trades = set()

    def connect(
        self, key: str, secret: str, server: str, proxy_host: str, proxy_port: int
    ):
        """"""
        self.key = key
        self.secret = secret.encode()

        if server == "REAL":
            self.init(WEBSOCKET_HOST, proxy_host, proxy_port)
        else:
            self.init(TESTNET_WEBSOCKET_HOST, proxy_host, proxy_port)

        self.start()

    def subscribe(self, req: SubscribeRequest):
        """
        Subscribe to tick data upate.
        """
        tick = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            name=req.symbol,
            datetime=datetime.now(),
            gateway_name=self.gateway_name,
        )
        self.ticks[req.symbol] = tick

    def on_connected(self):
        """"""
        self.gateway.write_log("Websocket API connection succeeded ")
        self.authenticate()

    def on_disconnected(self):
        """"""
        self.gateway.write_log("Websocket API disconnect ")

    def on_packet(self, packet: dict):
        """"""
        if "error" in packet:
            self.gateway.write_log("Websocket API an error ：%s" % packet["error"])

            if "not valid" in packet["error"]:
                self.active = False

        elif "request" in packet:
            req = packet["request"]
            success = packet["success"]

            if success:
                if req["op"] == "authKey":
                    self.gateway.write_log("Websocket API verify successful authorization ")
                    self.subscribe_topic()

        elif "table" in packet:
            name = packet["table"]
            callback = self.callbacks[name]

            if isinstance(packet["data"], list):
                for d in packet["data"]:
                    callback(d)
            else:
                callback(packet["data"])

    def on_error(self, exception_type: type, exception_value: Exception, tb):
        """"""
        msg = f" trigger abnormal ， status code ：{exception_type}， information ：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(self.exception_detail(
            exception_type, exception_value, tb))

    def authenticate(self):
        """
        Authenticate websockey connection to subscribe private topic.
        """
        expires = int(time.time())
        method = "GET"
        path = "/realtime"
        msg = method + path + str(expires)
        signature = hmac.new(
            self.secret, msg.encode(), digestmod=hashlib.sha256
        ).hexdigest()

        req = {"op": "authKey", "args": [self.key, expires, signature]}
        self.send_packet(req)

    def subscribe_topic(self):
        """
        Subscribe to all private topics.
        """
        req = {
            "op": "subscribe",
            "args": [
                "instrument",
                "trade",
                "orderBook10",
                "execution",
                "order",
                "position",
                "margin",
            ],
        }
        self.send_packet(req)

    def on_tick(self, d):
        """"""
        symbol = d["symbol"]
        tick = self.ticks.get(symbol, None)
        if not tick:
            return

        tick.last_price = d["price"]
        tick.datetime = datetime.strptime(
            d["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
        self.gateway.on_tick(copy(tick))

    def on_depth(self, d):
        """"""
        symbol = d["symbol"]
        tick = self.ticks.get(symbol, None)
        if not tick:
            return

        for n, buf in enumerate(d["bids"][:5]):
            price, volume = buf
            tick.__setattr__("bid_price_%s" % (n + 1), price)
            tick.__setattr__("bid_volume_%s" % (n + 1), volume)

        for n, buf in enumerate(d["asks"][:5]):
            price, volume = buf
            tick.__setattr__("ask_price_%s" % (n + 1), price)
            tick.__setattr__("ask_volume_%s" % (n + 1), volume)

        tick.datetime = datetime.strptime(
            d["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
        self.gateway.on_tick(copy(tick))

    def on_trade(self, d):
        """"""
        # Filter trade update with no trade volume and side (funding)
        if not d["lastQty"] or not d["side"]:
            return

        tradeid = d["execID"]
        if tradeid in self.trades:
            return
        self.trades.add(tradeid)

        if d["clOrdID"]:
            orderid = d["clOrdID"]
        else:
            orderid = d["orderID"]

        trade = TradeData(
            symbol=d["symbol"],
            exchange=Exchange.BITMEX,
            orderid=orderid,
            tradeid=tradeid,
            direction=DIRECTION_BITMEX2VT[d["side"]],
            price=d["lastPx"],
            volume=d["lastQty"],
            time=d["timestamp"][11:19],
            gateway_name=self.gateway_name,
        )

        self.gateway.on_trade(trade)

    def on_order(self, d):
        """"""
        if "ordStatus" not in d:
            return

        sysid = d["orderID"]
        order = self.orders.get(sysid, None)
        if not order:
            if d["clOrdID"]:
                orderid = d["clOrdID"]
            else:
                orderid = sysid

            # time = d["timestamp"][11:19]

            order = OrderData(
                symbol=d["symbol"],
                exchange=Exchange.BITMEX,
                type=ORDERTYPE_BITMEX2VT[d["ordType"]],
                orderid=orderid,
                direction=DIRECTION_BITMEX2VT[d["side"]],
                price=d["price"],
                volume=d["orderQty"],
                time=d["timestamp"][11:19],
                gateway_name=self.gateway_name,
            )
            self.orders[sysid] = order

        order.traded = d.get("cumQty", order.traded)
        order.status = STATUS_BITMEX2VT.get(d["ordStatus"], order.status)

        self.gateway.on_order(copy(order))

    def on_position(self, d):
        """"""
        position = PositionData(
            symbol=d["symbol"],
            exchange=Exchange.BITMEX,
            direction=Direction.NET,
            volume=d.get("currentQty", 0),
            gateway_name=self.gateway_name,
        )

        self.gateway.on_position(position)

    def on_account(self, d):
        """"""
        accountid = str(d["account"])
        account = self.accounts.get(accountid, None)
        if not account:
            account = AccountData(accountid=accountid,
                                  gateway_name=self.gateway_name)
            self.accounts[accountid] = account

        account.balance = d.get("marginBalance", account.balance)
        account.available = d.get("availableMargin", account.available)
        account.frozen = account.balance - account.available

        self.gateway.on_account(copy(account))

    def on_contract(self, d):
        """"""
        if "tickSize" not in d:
            return

        if not d["lotSize"]:
            return

        contract = ContractData(
            symbol=d["symbol"],
            exchange=Exchange.BITMEX,
            name=d["symbol"],
            product=Product.FUTURES,
            pricetick=d["tickSize"],
            size=d["lotSize"],
            stop_supported=True,
            net_position=True,
            history_data=True,
            gateway_name=self.gateway_name,
        )

        self.gateway.on_contract(contract)
