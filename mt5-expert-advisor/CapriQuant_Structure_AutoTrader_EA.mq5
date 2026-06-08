//+------------------------------------------------------------------+
//|                          CapriQuant_Structure_AutoTrader_EA.mq5  |
//|                                                                  |
//|  FULLY AUTOMATED TRADING EA                                      |
//|  - Sends market data to Python backend                           |
//|  - Polls the Structure Engine (/signal?engine=structure)         |
//|  - Automatically executes trades using structural levels         |
//|  - Uses stop_suggestion, tp1, tp2 returned by the backend        |
//+------------------------------------------------------------------+
// CANONICAL LIVE SOURCE: Prefer this file (or Realtime variant) for
// production. Other .mq5 in this folder and pb/ are divergent copies.
// Keep in sync when editing signal handling / risk / management.
//+------------------------------------------------------------------+
#property copyright "CapriQuant 2026"
#property version   "5.6"
#property strict
#property description "CapriQuant Live-Stream Auto-Trader — no historical backfill"

// ==================== INPUTS ====================
input string   ServerURL            = "http://127.0.0.1:8001";
input int      SignalPollSeconds    = 2;                  // Signal poll interval (OnTimer)
input int      DataSendIntervalMs   = 800;                // Min ms between /market-data posts
input string   SignalTimeframe      = "M5";               // Timeframe to request signal on

input int      BufferMaxM1Bars      = 15840;              // Backend rolling M1 cap (sent each post)
input int      MinCandlesM1         = 8;                  // Min M1 bars before signals (sent each post)

input double   MinConfidence        = 68.0;               // Only trade if backend confidence >= this
input double   RiskPercent          = 1.8;                // Risk per trade (% of equity)
input int      MaxTradesPerDay      = 3;
input double   MaxSpreadPoints      = 30;                 // Safety filter
input int      Magic                = 20260701;

input bool     EnableDataFeed       = true;               // POST /market-data (keep ON for buffer)
input bool     EnableTrading        = true;               // Execute trades from signals
input bool     LogAllSignals        = true;               // Print every signal received
input bool     CloseOppositeOnSignal = false;             // Close opposite position if new signal in opposite direction
input bool     DoBackfillOnStart    = false;              // Keep FALSE for pure real-time (recommended with the simple live_data aggregator). Only enable for post-downtime historic catch-up.

// ==================== GLOBALS ====================
datetime lastTradeDay = 0;
int      tradesToday  = 0;
int      httpTimeout  = 6000;
string   currentSymbol;
ulong    lastDataSendTime = 0;
int      g_dataPostsOk  = 0;
int      g_dataPostsFail = 0;

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   currentSymbol = _Symbol;
   EventSetTimer(SignalPollSeconds);

   Print("================================================================");
   Print("=== CapriQuant LIVE-STREAM AUTO-TRADER v5.6               ===");
   Print("Symbol: ", currentSymbol);
   Print("Data: OnTick ~", DataSendIntervalMs, "ms realtime M1 bar updates (backfill=", DoBackfillOnStart, ")");
   Print("Signals: ", SignalTimeframe, " every ", SignalPollSeconds, "s");
   Print("Buffer cap: ", BufferMaxM1Bars, " M1 | Min candles: ", MinCandlesM1);
   Print("================================================================");

   if(EnableDataFeed)
      SendMarketDataRealtime(true);

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("[CapriQuant] Data posts OK=", g_dataPostsOk, " failed=", g_dataPostsFail);
}

//+------------------------------------------------------------------+
//| OnTick — primary live data path (throttled)                      |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!EnableDataFeed) return;

   ulong nowMs = GetTickCount64();
   if(nowMs - lastDataSendTime < (ulong)DataSendIntervalMs)
      return;

   SendMarketDataRealtime(false);
   lastDataSendTime = nowMs;
}

//+------------------------------------------------------------------+
//| OnTimer — signals + timer fallback data send                     |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(EnableDataFeed)
   {
      ulong nowMs = GetTickCount64();
      if(nowMs - lastDataSendTime >= (ulong)DataSendIntervalMs)
      {
         SendMarketDataRealtime(false);
         lastDataSendTime = nowMs;
      }
   }

   if(!EnableTrading) return;

   MqlDateTime nowStruct, lastStruct;
   TimeToStruct(TimeCurrent(), nowStruct);
   TimeToStruct(lastTradeDay, lastStruct);

   if(nowStruct.day != lastStruct.day || lastTradeDay == 0)
   {
      tradesToday = 0;
      lastTradeDay = TimeCurrent();
   }

   if(tradesToday >= MaxTradesPerDay) return;

   string response = GetStructureSignal(SignalTimeframe);
   if(response == "") return;

   string sig  = ExtractJsonString(response, "signal");
   double conf = ExtractJsonDouble(response, "confidence");
   string rat  = ExtractJsonString(response, "rationale");
   Print("[CapriQuant] Signal → ", sig, " | conf=", conf, "% | ", rat);
   ProcessSignalResponse(response);
   ReportAnyClosedTrades();
}

//+------------------------------------------------------------------+
//| POST live tick + M1 bar + EA config to /market-data              |
//+------------------------------------------------------------------+
bool SendMarketDataRealtime(bool forceLog)
{
   double bid   = SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   double ask   = SymbolInfoDouble(currentSymbol, SYMBOL_ASK);
   double last  = SymbolInfoDouble(currentSymbol, SYMBOL_LAST);
   double open  = iOpen(currentSymbol, PERIOD_M1, 0);
   double high  = iHigh(currentSymbol, PERIOD_M1, 0);
   double low   = iLow(currentSymbol, PERIOD_M1, 0);
   double close = iClose(currentSymbol, PERIOD_M1, 0);
   long   vol   = iVolume(currentSymbol, PERIOD_M1, 0);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double spread  = (ask - bid) / _Point;
   datetime bar_time = iTime(currentSymbol, PERIOD_M1, 0);
   string ts_str = TimeToString(bar_time, TIME_DATE | TIME_SECONDS);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"TICK\",\"bid\":%.5f,\"ask\":%.5f,\"last\":%.5f,"
      "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d,"
      "\"spread\":%.2f,\"balance\":%.2f,\"equity\":%.2f,\"timestamp\":\"%s\","
      "\"buffer_max_m1\":%d,\"min_candles_m1\":%d,\"min_confidence\":%.1f,"
      "\"max_spread_points\":%.1f,\"data_send_interval_ms\":%d}",
      currentSymbol, bid, ask, last, open, high, low, close, vol,
      spread, balance, equity, ts_str,
      BufferMaxM1Bars, MinCandlesM1, MinConfidence, MaxSpreadPoints, DataSendIntervalMs);

   string headers = "Content-Type: application/json\r\n";
   uchar post_data[];
   StringToCharArray(payload, post_data, 0, StringLen(payload));
   uchar result_data[];
   string response_headers;

   ResetLastError();
   int res = WebRequest("POST", ServerURL + "/market-data", headers, httpTimeout,
                        post_data, result_data, response_headers);

   if(res == 200)
   {
      g_dataPostsOk++;
      if(forceLog || g_dataPostsOk <= 3 || g_dataPostsOk % 50 == 0)
         Print("[CapriQuant] market-data OK #", g_dataPostsOk, " ", currentSymbol, " close=", close);
      return true;
   }

   g_dataPostsFail++;
   if(forceLog || g_dataPostsFail <= 5)
      Print("[CapriQuant] market-data FAILED HTTP=", res, " err=", GetLastError());
   return false;
}

//+------------------------------------------------------------------+
//| Request signal from structure engine                             |
//+------------------------------------------------------------------+
string GetStructureSignal(string tf)
{
   // Send the real current spread (in points) instead of hardcoded 0.0
   double spreadPoints = (SymbolInfoDouble(currentSymbol, SYMBOL_ASK) - SymbolInfoDouble(currentSymbol, SYMBOL_BID)) / _Point;
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);

   string url = StringFormat("%s/signal/%s/%s?engine=structure&min_candles=%d&spread=%.1f&equity=%.2f",
                             ServerURL, currentSymbol, tf, MinCandlesM1, spreadPoints, equity);

   uchar  dummy[];
   uchar  result[];
   string headers;
   string response_headers;

   ResetLastError();
   int res = WebRequest("GET", url, headers, httpTimeout, dummy, result, response_headers);

   if(res != 200)
   {
      static bool firstError = true;
      if(firstError)
      {
         Print("[CapriQuant] Signal request failed. Code=", res, " - Check backend + WebRequest permissions");
         firstError = false;
      }
      return "";
   }

   return CharArrayToString(result);
}

//+------------------------------------------------------------------+
//| Process the signal response from Python                          |
//+------------------------------------------------------------------+
void ProcessSignalResponse(string json)
{
   string signalDir   = ExtractJsonString(json, "signal");
   double confidence  = ExtractJsonDouble(json, "confidence");
   string setup       = ExtractJsonString(json, "setup");
   string rationale   = ExtractJsonString(json, "rationale");
   double stop        = ExtractJsonDouble(json, "stop_suggestion");
   double validatedStop = ExtractJsonDouble(json, "validated_stop");
   double tp1         = ExtractJsonDouble(json, "tp1");
   double tp2         = ExtractJsonDouble(json, "tp2");
   double backendRisk = ExtractJsonDouble(json, "risk_pct");

   if(validatedStop > 0.0)
      stop = validatedStop;

   // Always print the rationale for HOLD signals (very useful for debugging)
   if(signalDir == "HOLD")
   {
      int candles = (int)ExtractJsonDouble(json, "candles_available");
      Print("[CapriQuant] HOLD | candles=", candles, " | ", rationale);
      return;
   }

   if(signalDir != "BUY" && signalDir != "SELL") return;

   if(LogAllSignals)
   {
      Print("========================================");
      Print("[CapriQuant] SIGNAL RECEIVED");
      Print("Direction : ", signalDir);
      Print("Confidence: ", confidence, "%");
      Print("Setup     : ", setup);
      Print("Rationale : ", rationale);
      Print("Stop / TP1: ", stop, " / ", tp1);
      Print("========================================");
   }

   // === FILTERS ===
   if(confidence < MinConfidence)
   {
      if(LogAllSignals) Print("[CapriQuant] REJECTED - Low confidence (", confidence, "%)");
      return;
   }

   double spread = (SymbolInfoDouble(currentSymbol, SYMBOL_ASK) - SymbolInfoDouble(currentSymbol, SYMBOL_BID)) / _Point;
   if(spread > MaxSpreadPoints)
   {
      if(LogAllSignals) Print("[CapriQuant] REJECTED - Spread too wide: ", spread);
      return;
   }

   // Prevent multiple trades per day limit
   if(tradesToday >= MaxTradesPerDay) return;

   // Check if we already have a position with this magic
   if(HasOpenPosition())
   {
      if(LogAllSignals) Print("[CapriQuant] Already have an open position. Skipping.");
      return;
   }

   // === EXECUTE TRADE ===
   double lots = CalculateLots(signalDir, stop, backendRisk);
   if(lots <= 0) return;

   ulong ticket = ExecuteTrade(signalDir, lots, stop, tp1, tp2, setup);

   if(ticket > 0)
   {
      tradesToday++;
      Print("[CapriQuant] *** TRADE EXECUTED *** ", signalDir, " | Lots: ", lots, " | Confidence: ", confidence, "%");
      SendTradeReport(signalDir, lots, stop, tp1, tp2, setup, ticket, "open", 0.0, "");
   }
}

//+------------------------------------------------------------------+
//| Check if we already have an open position with our magic         |
//+------------------------------------------------------------------+
bool HasOpenPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong posTicket = PositionGetTicket(i);
      if(PositionSelectByTicket(posTicket))
      {
         if(PositionGetString(POSITION_SYMBOL) == currentSymbol &&
            PositionGetInteger(POSITION_MAGIC) == Magic)
         {
            return true;
         }
      }
   }
   return false;
}

//+------------------------------------------------------------------+
//| Calculate lot size based on structural stop distance             |
//+------------------------------------------------------------------+
double CalculateLots(string direction, double stopPrice, double backendRiskPct)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double effectiveRiskPct = (backendRiskPct > 0.0) ? backendRiskPct : RiskPercent;
   double riskMoney = equity * (effectiveRiskPct / 100.0);

   double entry = (direction == "BUY") ? SymbolInfoDouble(currentSymbol, SYMBOL_ASK) :
                                         SymbolInfoDouble(currentSymbol, SYMBOL_BID);

   double stopDist = MathAbs(entry - stopPrice);
   if(stopDist < 0.00001) stopDist = SymbolInfoDouble(currentSymbol, SYMBOL_POINT) * 80;

   double point = SymbolInfoDouble(currentSymbol, SYMBOL_POINT);
   double tickValue = SymbolInfoDouble(currentSymbol, SYMBOL_TRADE_TICK_VALUE);
   if(tickValue <= 0) tickValue = 1.0;

   double lots = riskMoney / (stopDist / point * tickValue);
   lots = MathMax(0.01, MathMin(lots, 50.0));

   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
//| Send the trade order using levels from Python backend            |
//+------------------------------------------------------------------+
ulong ExecuteTrade(string direction, double lots, double sl, double tp1, double tp2, string setup)
{
   MqlTradeRequest  req = {};
   MqlTradeResult   res = {};

   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = currentSymbol;
   req.volume    = lots;
   req.type      = (direction == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price     = (direction == "BUY") ? SymbolInfoDouble(currentSymbol, SYMBOL_ASK) :
                                          SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   req.sl        = sl;
   req.tp        = tp1;                              // Use TP1 as main target
   req.deviation = 30;
   req.magic     = Magic;
   req.comment   = "CapriQuant-" + setup;

   if(!OrderSend(req, res))
   {
      Print("[CapriQuant] OrderSend FAILED: ", res.retcode, " - ", res.comment);
      return 0;
   }

   return res.order;
}

//+------------------------------------------------------------------+
//| Helper: Convert string TF to ENUM                                |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES StringToTimeframe(string tf)
{
   string s = tf;
   StringToUpper(s);
   if(s == "M1")   return PERIOD_M1;
   if(s == "M5")   return PERIOD_M5;
   if(s == "M15")  return PERIOD_M15;
   if(s == "M30")  return PERIOD_M30;
   if(s == "H1")   return PERIOD_H1;
   if(s == "H4")   return PERIOD_H4;
   if(s == "D1")   return PERIOD_D1;
   return PERIOD_CURRENT;
}

//+------------------------------------------------------------------+
//| Simple JSON extractors                                           |
//+------------------------------------------------------------------+
string ExtractJsonString(string json, string key)
{
   string k = "\"" + key + "\":\"";
   int p = StringFind(json, k);
   if(p < 0) return "";

   string v = StringSubstr(json, p + StringLen(k));
   int end = StringFind(v, "\"");
   if(end < 0) return "";
   return StringSubstr(v, 0, end);
}

double ExtractJsonDouble(string json, string key)
{
   string k = "\"" + key + "\":";
   int p = StringFind(json, k);
   if(p < 0) return 0.0;

   string v = StringSubstr(json, p + StringLen(k));
   int comma = StringFind(v, ",");
   int brace = StringFind(v, "}");
   int brk   = StringFind(v, "]");
   int end = MathMin(MathMin(comma, brace), brk);
   if(end < 0) end = StringLen(v);

   v = StringSubstr(v, 0, end);
   StringReplace(v, "\"", "");
   StringReplace(v, "[", "");
   StringReplace(v, "]", "");
   return StringToDouble(v);
}

//+------------------------------------------------------------------+
//| Report executed trade (open or close) to backend for UI + tracking
//| Extended to support close_reason / SL/TP for dashboard journal    |
//+------------------------------------------------------------------+
void SendTradeReport(string direction, double lots, double sl, double tp1, double tp2, string setup,
                     ulong ticket = 0, string status = "open", double close_price = 0.0, string close_reason = "")
{
   double entry = (direction == "BUY") ? SymbolInfoDouble(currentSymbol, SYMBOL_ASK) :
                                          SymbolInfoDouble(currentSymbol, SYMBOL_BID);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"direction\":\"%s\",\"entry_price\":%.5f,\"stop_loss\":%.5f,"
      "\"tp1\":%.5f,\"tp2\":%.5f,\"volume_lots\":%.2f,\"outcome\":\"%s\",\"notes\":\"CapriQuant-%s\","
      "\"ticket\":%I64u,\"status\":\"%s\",\"close_price\":%.5f,\"close_reason\":\"%s\","
      "\"setup\":\"%s\"}",
      currentSymbol, direction, entry, sl, tp1, tp2, lots, status, setup,
      ticket, status, close_price, close_reason, setup);

   string headers = "Content-Type: application/json\r\n";
   uchar post_data[];
   StringToCharArray(payload, post_data, 0, StringLen(payload));
   uchar result_data[];
   string response_headers;

   ResetLastError();
   int res = WebRequest("POST", ServerURL + "/report-trade", headers, httpTimeout, post_data, result_data, response_headers);
   if(res == 200)
      Print("[CapriQuant] Trade report OK (", status, ")");
   else
      Print("[CapriQuant] Trade report FAILED. HTTP=", res, " err=", GetLastError(),
            " — add ", ServerURL, " to Tools→Options→Expert Advisors→WebRequest URLs");
}

//+------------------------------------------------------------------+
//| Basic close reporter (called from OnTimer).                       |
//| For full per-ticket + exact SL/TP reason, keep g_knownOpenTickets |
//| (store details at open) and scan HistoryDealsTotal + DealGet* for |
//| DEAL_ENTRY_OUT + our magic + DEAL_REASON (SL/TP etc).             |
//+------------------------------------------------------------------+
void ReportAnyClosedTrades()
{
   static bool hadOurPosition = false;
   bool hasNow = HasOpenPosition();

   if (hadOurPosition && !hasNow)
   {
      Print("[CapriQuant] Detected close for ", currentSymbol, " - reporting for UI journal");
      // Send a close event. In production store the exact ticket/levels/setup from open
      // and report the real close_price + reason here.
      SendTradeReport("BUY", 0.01, 0, 0, 0, "position-closed", 0, "close", 0.0, "closed");
   }
   hadOurPosition = hasNow;
}

//+------------------------------------------------------------------+
