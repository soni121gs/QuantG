import os

gateway_path = r"d:\Quant\QuantG\backend\brokers\upstox_gateway.py"
server_path = r"d:\Quant\QuantG\backend\server.py"

# 1. Patch upstox_gateway.py
with open(gateway_path, "r", encoding="utf-8") as f:
    gw_content = f.read()

target_gw = """    def get_option_chain(self, underlying_key: str, expiry_date: str) -> Dict[str, Any]:
        \"\"\"Fetch low-latency index Option Chain lookup from Upstox.\"\"\"
        return self._request("GET", "/v2/option/chain", params={
            "instrument_key": underlying_key,
            "expiry_date": expiry_date,
        })"""

replacement_gw = """    def get_option_chain(self, underlying_key: str, expiry_date: Optional[str] = None) -> Dict[str, Any]:
        \"\"\"Fetch low-latency index Option Chain lookup from Upstox.\"\"\"
        params = {"instrument_key": underlying_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        return self._request("GET", "/v2/option/chain", params=params)"""

# 2. Patch server.py
with open(server_path, "r", encoding="utf-8") as f:
    srv_content = f.read()

target_srv = """        expiry_dt = datetime.now() + timedelta(days=(7 - datetime.now().weekday() + 3) % 7)
        instrument_token = None
        tradingsymbol = None
        try:
            spot_key = upstox_keys.get(underlying)
            if spot_key:
                chain = await asyncio.to_thread(upstox_gw.get_option_chain, spot_key, expiry_dt.date().isoformat())
                if chain and chain.get("status") == "success":
                    data = chain.get("data", []) or []
                    for node in data:
                        opt_node = node.get("call_options" if opt_type == "CE" else "put_options") or {}
                        node_strike = float(node.get("strike_price") or opt_node.get("strike_price") or 0)
                        if opt_node and int(node_strike) == int(strike):
                            instrument_token = opt_node.get("instrument_key")
                            tradingsymbol = opt_node.get("trading_symbol")
                            break"""

replacement_srv = """        expiry_dt = datetime.now() + timedelta(days=(7 - datetime.now().weekday() + 3) % 7)
        instrument_token = None
        tradingsymbol = None
        try:
            spot_key = upstox_keys.get(underlying)
            if spot_key:
                chain = await asyncio.to_thread(upstox_gw.get_option_chain, spot_key, None)
                if chain and chain.get("status") == "success":
                    data = chain.get("data", []) or []
                    for node in data:
                        node_strike = float(node.get("strike_price") or 0)
                        if int(node_strike) == int(strike):
                            opt_node = node.get("call_options" if opt_type == "CE" else "put_options") or {}
                            if opt_node:
                                instrument_token = opt_node.get("instrument_key")
                                tradingsymbol = opt_node.get("trading_symbol")
                                if node.get("expiry"):
                                    try:
                                        from datetime import datetime as dt_parser
                                        expiry_dt = dt_parser.strptime(node["expiry"], "%Y-%m-%d")
                                    except Exception:
                                        pass
                                break"""

patched = False
if target_gw in gw_content:
    gw_content = gw_content.replace(target_gw, replacement_gw)
    with open(gateway_path, "w", encoding="utf-8") as f:
        f.write(gw_content)
    print("SUCCESS: upstox_gateway.py patched")
    patched = True
else:
    print("ERROR: target_gw not found")

if target_srv in srv_content:
    srv_content = srv_content.replace(target_srv, replacement_srv)
    with open(server_path, "w", encoding="utf-8") as f:
        f.write(srv_content)
    print("SUCCESS: server.py patched")
    patched = True
else:
    print("ERROR: target_srv not found")
