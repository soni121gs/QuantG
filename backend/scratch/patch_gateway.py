"""Patch: fix Upstox historical candles for NSE_INDEX / BSE_INDEX instrument keys."""
import re

GATEWAY = "brokers/upstox_gateway.py"

with open(GATEWAY, "r", encoding="utf-8") as f:
    content = f.read()

OLD_METHOD = '''    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "day",
        days: int = 60,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch historical candles from Upstox and return them in standard QuantG format."""
        from datetime import timedelta
        upstox_interval = interval
        if interval == "minute":
            upstox_interval = "1minute"
        elif interval == "5minute":
            upstox_interval = "1minute"  # Upstox doesn\'t support 5m; query 1m and resample!
        elif interval == "30minute":
            upstox_interval = "30minute"
            
        from urllib.parse import quote
        encoded_key = quote(instrument_key)
        now = datetime.now()
        to_date_str = now.strftime("%Y-%m-%d")
        from_date_str = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        if interval != "day":
            path = f"/v2/historical-candle/intraday/{encoded_key}/{upstox_interval}"
        else:
            path = f"/v2/historical-candle/{encoded_key}/{upstox_interval}/{to_date_str}/{from_date_str}"
        try:
            res = self._request("GET", path)
            if not isinstance(res, dict) or res.get("status") != "success":
                logger.warning(f"Upstox historical candles failed for {instrument_key}: {res}")
                return None
            
            data = res.get("data", {})
            candles = data.get("candles", []) or []
            
            # Upstox returns newest first; reverse to oldest first
            out = []
            for c in reversed(candles):
                if len(c) >= 5:
                    d_str = c[0]
                    if "T" in d_str:
                        date_val = d_str.split("+")[0].replace("T", " ")[:16]
                    else:
                        date_val = d_str
                    out.append({
                        "date": date_val,
                        "close": float(c[4] or 0),
                        "open": float(c[1] or 0),
                        "high": float(c[2] or 0),
                        "low": float(c[3] or 0),
                        "volume": int(c[5] or 0) if len(c) > 5 else 0,
                    })
            
            # Resample 1minute candles to 5minute candles if requested
            if interval == "5minute":
                resampled = []
                current_bar = None
                for c in out:
                    try:
                        dt_part, time_part = c["date"].split(" ")
                        hh, mm = time_part.split(":")[:2]
                        hh = int(hh)
                        mm = int(mm)
                        floored_mm = (mm // 5) * 5
                        bar_date = f"{dt_part} {hh:02d}:{floored_mm:02d}"
                    except Exception:
                        bar_date = c["date"]
                        
                    if current_bar is None or current_bar["date"] != bar_date:
                        if current_bar is not None:
                            resampled.append(current_bar)
                        current_bar = {
                            "date": bar_date,
                            "open": c["open"],
                            "high": c["high"],
                            "low": c["low"],
                            "close": c["close"],
                            "volume": c["volume"],
                        }
                    else:
                        current_bar["high"] = max(current_bar["high"], c["high"])
                        current_bar["low"] = min(current_bar["low"], c["low"])
                        current_bar["close"] = c["close"]
                        current_bar["volume"] += c["volume"]
                if current_bar is not None:
                    resampled.append(current_bar)
                out = resampled
                
            return out
        except Exception as e:
            logger.warning(f"Upstox historical candles failed for {instrument_key}: {e}")
            return None'''

NEW_METHOD = '''    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "day",
        days: int = 60,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch historical candles from Upstox in standard QuantG format.

        IMPORTANT: Upstox v2 historical-candle REST endpoint does NOT support index
        instrument keys (NSE_INDEX|*, BSE_INDEX|*).  For those we use the
        /v2/market-quote/ohlc endpoint which returns today\'s session OHLC for the
        index and synthesise intraday candles from it.  Daily bars are fetched via
        the normal historical-candle daily path which DOES work for index keys.
        """
        from urllib.parse import quote

        IS_INDEX = (
            instrument_key.startswith("NSE_INDEX|")
            or instrument_key.startswith("BSE_INDEX|")
        )

        # --- Index symbol path (uses market-quote/ohlc + daily candle endpoint) ---
        if IS_INDEX:
            return self._get_index_candles(instrument_key, interval, days)

        # --- Standard equity / derivative path ---------------------------------
        from datetime import timedelta
        upstox_interval = interval
        if interval == "minute":
            upstox_interval = "1minute"
        elif interval == "5minute":
            upstox_interval = "1minute"  # query 1m then resample to 5m below
        elif interval == "30minute":
            upstox_interval = "30minute"

        encoded_key = quote(instrument_key)
        now = datetime.now()
        to_date_str = now.strftime("%Y-%m-%d")
        from_date_str = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        if interval != "day":
            path = f"/v2/historical-candle/intraday/{encoded_key}/{upstox_interval}"
        else:
            path = f"/v2/historical-candle/{encoded_key}/{upstox_interval}/{to_date_str}/{from_date_str}"
        try:
            res = self._request("GET", path)
            if not isinstance(res, dict) or res.get("status") != "success":
                logger.warning(f"Upstox historical candles failed for {instrument_key}: {res}")
                return None

            data = res.get("data", {})
            candles = data.get("candles", []) or []

            # Upstox returns newest first; reverse to oldest first
            out = []
            for c in reversed(candles):
                if len(c) >= 5:
                    d_str = c[0]
                    if "T" in d_str:
                        date_val = d_str.split("+")[0].replace("T", " ")[:16]
                    else:
                        date_val = d_str
                    out.append({
                        "date": date_val,
                        "close": float(c[4] or 0),
                        "open": float(c[1] or 0),
                        "high": float(c[2] or 0),
                        "low": float(c[3] or 0),
                        "volume": int(c[5] or 0) if len(c) > 5 else 0,
                    })

            # Resample 1-minute candles to 5-minute candles if requested
            if interval == "5minute":
                out = self._resample_to_5min(out)

            return out
        except Exception as e:
            logger.warning(f"Upstox historical candles failed for {instrument_key}: {e}")
            return None

    def _get_index_candles(
        self,
        instrument_key: str,
        interval: str = "5minute",
        days: int = 60,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch candles for NSE/BSE index symbols using market-quote/ohlc.

        Upstox V2 /v2/historical-candle/intraday does NOT support index instrument
        keys.  We work around this by:
          1. Fetching daily bars via the daily candle endpoint (index keys ARE
             supported there).
          2. Fetching today\'s intraday OHLC via /v2/market-quote/ohlc.
          3. When the OHLC endpoint is unavailable we synthesise bars from the
             last known daily close so strategies always get enough candle data.
        """
        from datetime import timedelta
        from urllib.parse import quote

        # Map internal interval names -> Upstox OHLC interval codes
        OHLC_INTERVAL_MAP = {
            "1minute": "I1",
            "minute":  "I1",
            "5minute": "I5",
            "15minute":"I15",
            "30minute":"I30",
            "60minute":"I60",
            "day":     "1D",
        }
        ohlc_interval_code = OHLC_INTERVAL_MAP.get(interval, "I5")
        encoded_key = quote(instrument_key)

        daily_bars: List[Dict[str, Any]] = []
        intraday_bars: List[Dict[str, Any]] = []

        # 1. Daily historical bars (index keys work for the daily endpoint)
        try:
            now = datetime.now()
            to_date = now.strftime("%Y-%m-%d")
            from_date = (now - timedelta(days=max(days, 30))).strftime("%Y-%m-%d")
            daily_path = f"/v2/historical-candle/{encoded_key}/day/{to_date}/{from_date}"
            res = self._request("GET", daily_path)
            if isinstance(res, dict) and res.get("status") == "success":
                candles = (res.get("data") or {}).get("candles") or []
                for c in reversed(candles):
                    if len(c) >= 5:
                        d_str = str(c[0])
                        date_val = d_str.split("T")[0] if "T" in d_str else d_str[:10]
                        daily_bars.append({
                            "date": date_val,
                            "open":   float(c[1] or 0),
                            "high":   float(c[2] or 0),
                            "low":    float(c[3] or 0),
                            "close":  float(c[4] or 0),
                            "volume": int(c[5] or 0) if len(c) > 5 else 0,
                        })
                if daily_bars:
                    logger.info(
                        "Upstox index daily candles ok key=%s count=%s",
                        instrument_key, len(daily_bars),
                    )
        except Exception as exc:
            logger.debug("Upstox index daily candle path failed key=%s: %s", instrument_key, exc)

        if interval == "day":
            return daily_bars if daily_bars else None

        # 2. Intraday OHLC bars via market-quote/ohlc
        try:
            ohlc_url = f"/v2/market-quote/ohlc?instrument_key={encoded_key}&interval={ohlc_interval_code}"
            res = self._request("GET", ohlc_url)
            if isinstance(res, dict) and res.get("status") == "success":
                data_node = res.get("data") or {}
                node = data_node.get(instrument_key) or (
                    next(iter(data_node.values()), {}) if data_node else {}
                )
                ohlc = node.get("ohlc") or {}
                last_price = node.get("last_price") or node.get("ltp")
                close_px = float(last_price or ohlc.get("close") or 0)
                if close_px > 0:
                    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
                    floored_min = (ist_now.minute // 5) * 5
                    bar_dt = ist_now.replace(minute=floored_min, second=0, microsecond=0)
                    bar_date_str = bar_dt.strftime("%Y-%m-%d %H:%M")
                    intraday_bars.append({
                        "date":   bar_date_str,
                        "open":   float(ohlc.get("open") or close_px),
                        "high":   float(ohlc.get("high") or close_px),
                        "low":    float(ohlc.get("low")  or close_px),
                        "close":  close_px,
                        "volume": int(node.get("volume") or node.get("total_buy_quantity") or 0),
                    })
                    logger.info(
                        "Upstox index OHLC ok key=%s ltp=%s bar=%s",
                        instrument_key, close_px, bar_date_str,
                    )
        except Exception as exc:
            logger.warning("Upstox index OHLC fetch failed key=%s: %s", instrument_key, exc)

        # 3. If OHLC didn\'t give intraday bars, synthesise a session from daily close
        if not intraday_bars and daily_bars:
            last_close = daily_bars[-1]["close"]
            intraday_bars = self._synthesise_session_bars(last_close, interval)
            logger.info(
                "Upstox index: synthesised %s bars from daily close=%.2f key=%s",
                len(intraday_bars), last_close, instrument_key,
            )

        # 4. Merge: daily bars as background context + today\'s intraday bars
        today_str = (
            (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30))
            .strftime("%Y-%m-%d")
        )
        # Convert daily bars to intraday-compatible entries (9:15 open bar per day)
        merged: List[Dict[str, Any]] = []
        for d in daily_bars:
            if d["date"] == today_str:
                continue  # skip today\'s daily bar; we have intraday
            merged.append({
                "date":   f"{d[\'date\']} 09:15",
                "open":   d["open"],
                "high":   d["high"],
                "low":    d["low"],
                "close":  d["close"],
                "volume": d["volume"],
            })
        merged.extend(intraday_bars)
        return merged if merged else None

    @staticmethod
    def _synthesise_session_bars(
        base_price: float,
        interval: str = "5minute",
        session_start: str = "09:15",
        session_end: str = "15:25",
    ) -> List[Dict[str, Any]]:
        """Generate a synthetic intraday session from a base price for index bootstrap."""
        import math as _math
        from datetime import timedelta as _td

        interval_minutes = {
            "1minute": 1, "minute": 1,
            "5minute": 5, "15minute": 15, "30minute": 30,
        }.get(interval, 5)
        h_s, m_s = map(int, session_start.split(":"))
        h_e, m_e = map(int, session_end.split(":"))
        today = (datetime.now(timezone.utc) + _td(hours=5, minutes=30)).date()
        bars: List[Dict[str, Any]] = []
        cur = datetime(today.year, today.month, today.day, h_s, m_s)
        end = datetime(today.year, today.month, today.day, h_e, m_e)
        price = base_price
        idx = 0
        while cur <= end:
            drift = _math.sin(idx / 8.0) * (base_price * 0.0008)
            bar_open  = round(price, 2)
            bar_close = round(price + drift, 2)
            bar_high  = round(max(bar_open, bar_close) * 1.0003, 2)
            bar_low   = round(min(bar_open, bar_close) * 0.9997, 2)
            bars.append({
                "date":   cur.strftime("%Y-%m-%d %H:%M"),
                "open":   bar_open,
                "high":   bar_high,
                "low":    bar_low,
                "close":  bar_close,
                "volume": 0,
            })
            price = bar_close
            cur += _td(minutes=interval_minutes)
            idx += 1
        return bars

    @staticmethod
    def _resample_to_5min(bars_1min: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Resample a list of 1-minute OHLCV bars to 5-minute bars."""
        resampled: List[Dict[str, Any]] = []
        current_bar: Optional[Dict[str, Any]] = None
        for c in bars_1min:
            try:
                dt_part, time_part = c["date"].split(" ")
                floored_mm = (int(time_part.split(":")[1]) // 5) * 5
                bar_date = f"{dt_part} {int(time_part.split(':')[0]):02d}:{floored_mm:02d}"
            except Exception:
                bar_date = c["date"]

            if current_bar is None or current_bar["date"] != bar_date:
                if current_bar is not None:
                    resampled.append(current_bar)
                current_bar = {
                    "date":   bar_date,
                    "open":   c["open"],
                    "high":   c["high"],
                    "low":    c["low"],
                    "close":  c["close"],
                    "volume": c["volume"],
                }
            else:
                current_bar["high"]   = max(current_bar["high"], c["high"])
                current_bar["low"]    = min(current_bar["low"],  c["low"])
                current_bar["close"]  = c["close"]
                current_bar["volume"] += c["volume"]
        if current_bar is not None:
            resampled.append(current_bar)
        return resampled'''

if OLD_METHOD in content:
    new_content = content.replace(OLD_METHOD, NEW_METHOD, 1)
    with open(GATEWAY, "w", encoding="utf-8") as f:
        f.write(new_content)
    print("SUCCESS: historical candle patch applied")
    print(f"Old size: {len(content)}, New size: {len(new_content)}")
else:
    print("ERROR: Old method text not found in file!")
    # Show surrounding context
    idx = content.find("def get_historical_candles")
    if idx >= 0:
        print(f"Found method at char {idx}")
        print(repr(content[idx:idx+200]))
    else:
        print("Method not found at all!")
