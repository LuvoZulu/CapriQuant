//+------------------------------------------------------------------+
//|           CapriQuant_Structure_EA_FULL_PASTE_READY.mq5           |
//|                                                                  |
//|  REAL-TIME AUTO-TRADER EA with trade tracking (v5.4-phase2)      |
//|  *** CANONICAL / RECOMMENDED PASTE-READY VERSION ***             |
//|  (other .mq5 variants are legacy - use this one)                 |
//|                                                                  |
//|  - Sends market data on every tick (throttled) + equity          |
//|  - Polls /signal + realtime POST path                            |
//|  - Trades high confluence + server risk_pct / validated_stop     |
//|  - Reports opens/closes (SL/TP/kill reasons) for dashboard       |
//|  - Supports kill switch (FLATTEN / PAUSE) from backend/UI        |
//|                                                                  |
//|  INSTRUCTIONS:                                                   |
//|  1. Open MetaEditor                                              |
//|  2. File → New → Expert Advisor (template)                       |
//|  3. Delete everything                                            |
//|  4. Paste EVERYTHING below this line                             |
//|  5. Compile (F7) - should have 0 errors                          |
//|  6. Attach to chart(s)                                           |
//|                                                                  |
//|  IMPORTANT: In EA Properties → Common → Allow WebRequest         |
//|  and add exactly: http://127.0.0.1:8001                          |
//+------------------------------------------------------------------+
#property copyright "CapriQuant 2026"
#property version   "5.3-fixed"
#property strict
#property description "CapriQuant Real-time Auto-Trader - Tick data + signals + full SL/TP close reporting for dashboard"

// ==================== INPUTS ====================
input string   ServerURL            = "http://127.0.0.1:8001";
input int      SignalPollSeconds    = 2;                  // How often to request signals (lower = more real-time)
input int      DataSendIntervalMs   = 800;                // Minimum time between data sends (throttling)
input string   DataTimeframe        = "M1";               // Timeframe to send detailed OHLC for (M1 recommended for real-time)
input string   SignalTimeframe      = "M5";               // Timeframe to request signal on

input double   MinConfidence        = 68.0;
input double   RiskPercent          = 1.8;                // Fallback. Server can override via risk_pct in response
input int      MaxTradesPerDay      = 30;
input double   MaxSpreadPoints      = 400;
input int      Magic                = 20260701;

input bool     EnableTrading        = true;
input bool     LogAllSignals        = true;

// ==================== GLOBALS ====================
datetime lastTradeDay = 0;
int      tradesToday  = 0;
int      httpTimeout  = 6000;
string   currentSymbol;
ulong    lastDataSendTime = 0;   // For throttling data sends

// For close reporting (SL/TP tracking)
ulong    g_knownOpenTickets[];
ulong    g_reportedClosedTickets[];

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   currentSymbol = _Symbol;
   EventSetTimer(SignalPollSeconds);

   ArrayResize(g_knownOpenTickets, 0);
   ArrayResize(g_reportedClosedTickets, 0);

   Print("================================================================");
   Print("=== CapriQuant REAL-TIME AUTO-TRADER v5.3-fixed             ===");
   Print("Symbol: ", currentSymbol);
   Print("Data sent on every tick (throttled to ~", DataSendIntervalMs, "ms)");
   Print("Signals polled every ", SignalPollSeconds, " seconds");
   Print("Close reporting enabled for SL/TP dashboard tracking");
   Print("================================================================");

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| OnTick - Send data in real time (throttled)                      |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!EnableTrading) return;

   ulong currentTime = GetTickCount64();  // Milliseconds since system start

   // Throttle data sending to avoid flooding the server
   if (currentTime - lastDataSendTime < DataSendIntervalMs)
      return;

   SendMarketDataRealtime();
   lastDataSendTime = currentTime;
}

//+------------------------------------------------------------------+
//| Timer - Poll for signals + report trade status (open/close)      |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!EnableTrading) return;

   // Daily reset logic
   MqlDateTime nowStruct, lastStruct;
   TimeToStruct(TimeCurrent(), nowStruct);
   TimeToStruct(lastTradeDay, lastStruct);

   if(nowStruct.day != lastStruct.day || lastTradeDay == 0)
   {
      tradesToday = 0;
      lastTradeDay = TimeCurrent();
      ArrayResize(g_reportedClosedTickets, 0);
   }

   if(tradesToday >= MaxTradesPerDay) return;

   string response = GetStructureSignal(SignalTimeframe);
   if(response != "")
   {
      string sig     = ExtractJsonString(response, "signal");
      double conf    = ExtractJsonDouble(response, "confidence");
      string rat     = ExtractJsonString(response, "rationale");

      Print("[CapriQuant] Signal → ", sig, " | conf=", conf, "% | ", rat);

      ProcessSignalResponse(response);
   }

   // Report any newly closed trades (SL/TP etc) for the dashboard
   ReportClosedTrades();

   // Periodically report current open state so dashboard knows what is running
   ReportOpenTradesStatus();
}

//+------------------------------------------------------------------+
//| Send current market state (called from OnTick) - WITH TIMESTAMP  |
//+------------------------------------------------------------------+
void SendMarketDataRealtime()
{
   double bid   = SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   double ask   = SymbolInfoDouble(currentSymbol, SYMBOL_ASK);
   double last  = SymbolInfoDouble(currentSymbol, SYMBOL_LAST);

   // Send both current forming M1 bar + live tick price
   double open  = iOpen(currentSymbol, PERIOD_M1, 0);
   double high  = iHigh(currentSymbol, PERIOD_M1, 0);
   double low   = iLow(currentSymbol, PERIOD_M1, 0);
   double close = iClose(currentSymbol, PERIOD_M1, 0);
   long   vol   = iVolume(currentSymbol, PERIOD_M1, 0);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);

   // Proper timestamp for accurate backend M1 aggregation and structure (critical)
   datetime bar_time = iTime(currentSymbol, PERIOD_M1, 0);
   string ts_str = TimeToString(bar_time, TIME_DATE|TIME_SECONDS);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"TICK\",\"bid\":%.5f,\"ask\":%.5f,\"last\":%.5f,"
      "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d,"
      "\"balance\":%.2f,\"equity\":%.2f,\"timestamp\":\"%s\"}",
      currentSymbol, bid, ask, last,
      open, high, low, close, vol,
      balance, equity, ts_str);

   string headers = "Content-Type: application/json\r\n";
   uchar post_data[];
   StringToCharArray(payload, post_data, 0, StringLen(payload));
   uchar result_data[];
   string response_headers;

   WebRequest("POST", ServerURL + "/market-data", headers, httpTimeout, post_data, result_data, response_headers);
}

//+------------------------------------------------------------------+
//| Request signal (same as before)                                  |
//+------------------------------------------------------------------+
string GetStructureSignal(string tf)
{
   double spreadPoints = (SymbolInfoDouble(currentSymbol, SYMBOL_ASK) - SymbolInfoDouble(currentSymbol, SYMBOL_BID)) / _Point;

   string url = StringFormat("%s/signal/%s/%s?engine=structure&min_candles=8&spread=%.1f",
                             ServerURL, currentSymbol, tf, spreadPoints);

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
         Print("[CapriQuant] Signal request failed. Code=", res);
         firstError = false;
      }
      return "";
   }

   return CharArrayToString(result);
}

//+------------------------------------------------------------------+
//| Process response + server risk/stop + report open                |
//+------------------------------------------------------------------+
void ProcessSignalResponse(string json)
{
   string signalDir   = ExtractJsonString(json, "signal");
   double confidence  = ExtractJsonDouble(json, "confidence");
   string setup       = ExtractJsonString(json, "setup");
   string rationale   = ExtractJsonString(json, "rationale");
   double stop        = ExtractJsonDouble(json, "stop_suggestion");
   double tp1         = ExtractJsonDouble(json, "tp1");
   double tp2         = ExtractJsonDouble(json, "tp2");

   // Prefer validated_stop from server (risk manager)
   double server_stop = ExtractJsonDouble(json, "validated_stop");
   if(server_stop > 0) stop = server_stop;

   // Server can send risk_pct to override the input (never assign to input var!)
   double server_risk_pct = ExtractJsonDouble(json, "risk_pct");

   // ===== KILL SWITCH (phase2) =====
   string sysMode = ExtractJsonString(json, "system_mode");
   string action  = ExtractJsonString(json, "action");
   if(sysMode == "flatten" || action == "flatten_all" || signalDir == "FLATTEN")
   {
      Print("[CapriQuant] *** KILL/FLATTEN: ", rationale);
      CloseAllPositions("kill_switch");
      SendTradeReport("SYSTEM", 0, 0, 0, 0, "flatten", 0, "system", 0, "flatten");
      return;
   }
   if(sysMode == "paused")
   {
      Print("[CapriQuant] SYSTEM PAUSED - HOLD only");
      return;
   }
   // ===============================

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

   if(confidence < MinConfidence) return;

   double spread = (SymbolInfoDouble(currentSymbol, SYMBOL_ASK) - SymbolInfoDouble(currentSymbol, SYMBOL_BID)) / _Point;
   if(spread > MaxSpreadPoints) return;

   if(tradesToday >= MaxTradesPerDay) return;
   if(HasOpenPosition()) return;

   double effRisk = RiskPercent;
   if(server_risk_pct > 0.1) effRisk = server_risk_pct;

   double lots = CalculateLots(stop, effRisk);
   if(lots <= 0) return;

   ulong ticket = ExecuteTrade(signalDir, lots, stop, tp1, tp2, setup);
   if(ticket > 0)
   {
      tradesToday++;
      Print("[CapriQuant] *** TRADE EXECUTED *** ", signalDir, " | Lots: ", lots, " ticket=", ticket);
      SendTradeReport(signalDir, lots, stop, tp1, tp2, setup, ticket, "open");
   }
}

//+------------------------------------------------------------------+
//| Report closed trades with reason (SL/TP) - for dashboard         |
//+------------------------------------------------------------------+
void ReportClosedTrades()
{
   // Look back a couple of days
   if(!HistorySelect(TimeCurrent() - 86400 * 2, TimeCurrent())) return;

   int totalDeals = HistoryDealsTotal();
   for(int i = totalDeals - 1; i >= 0; i--)
   {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;

      string sym   = HistoryDealGetString(dealTicket, DEAL_SYMBOL);
      long   magic = HistoryDealGetInteger(dealTicket, DEAL_MAGIC);
      long   entry = HistoryDealGetInteger(dealTicket, DEAL_ENTRY);

      if(sym != currentSymbol || magic != Magic) continue;
      if(entry != DEAL_ENTRY_OUT) continue;  // only exits/closes

      ulong posTicket = (ulong)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
      double closePr  = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
      double vol      = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);

      // Skip if we already reported this close
      bool already = false;
      for(int k = 0; k < ArraySize(g_reportedClosedTickets); k++)
         if(g_reportedClosedTickets[k] == posTicket) { already = true; break; }
      if(already) continue;

      long reasonCode = HistoryDealGetInteger(dealTicket, DEAL_REASON);
      string closeReason = "manual";
      if(reasonCode == DEAL_REASON_SL)        closeReason = "sl";
      else if(reasonCode == DEAL_REASON_TP)   closeReason = "tp";
      else if(reasonCode == DEAL_REASON_CLIENT) closeReason = "client";

      // Send close report (direction can be approximate or omitted)
      SendTradeReport("CLOSE", vol, 0, 0, 0, closeReason, posTicket, "closed", closePr, closeReason);

      // remember
      int sz = ArraySize(g_reportedClosedTickets);
      ArrayResize(g_reportedClosedTickets, sz + 1);
      g_reportedClosedTickets[sz] = posTicket;
   }
}

//+------------------------------------------------------------------+
//| Report current open positions (so dashboard sees running trades) |
//+------------------------------------------------------------------+
void ReportOpenTradesStatus()
{
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t))
      {
         if(PositionGetString(POSITION_SYMBOL) == currentSymbol &&
            PositionGetInteger(POSITION_MAGIC) == Magic)
         {
            double e   = PositionGetDouble(POSITION_PRICE_OPEN);
            double sl  = PositionGetDouble(POSITION_SL);
            double tp  = PositionGetDouble(POSITION_TP);
            double vol = PositionGetDouble(POSITION_VOLUME);
            string dir = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";

            // report as open (backend will handle as update/insert by ticket)
            SendTradeReport(dir, vol, sl, tp, tp, "open_update", t, "open", e);
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Helper functions                                                 |
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
            return true;
      }
   }
   return false;
}

// Close all for this EA (kill switch support - phase2)
void CloseAllPositions(string reason = "kill_switch")
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong posTicket = PositionGetTicket(i);
      if(PositionSelectByTicket(posTicket))
      {
         if(PositionGetString(POSITION_SYMBOL) == currentSymbol &&
            PositionGetInteger(POSITION_MAGIC) == Magic)
         {
            MqlTradeRequest req = {};
            MqlTradeResult  res = {};
            req.action   = TRADE_ACTION_DEAL;
            req.position = posTicket;
            req.symbol   = currentSymbol;
            req.volume   = PositionGetDouble(POSITION_VOLUME);
            req.type     = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
            req.price    = (req.type == ORDER_TYPE_SELL) ? SymbolInfoDouble(currentSymbol, SYMBOL_BID) : SymbolInfoDouble(currentSymbol, SYMBOL_ASK);
            req.deviation = 30;
            req.magic    = Magic;
            req.comment  = "CapriQuant-" + reason;
            if(OrderSend(req, res))
            {
               Print("[CapriQuant] KILL/CLOSE executed ticket=", posTicket, " reason=", reason);
               SendTradeReport("CLOSE", req.volume, 0, 0, 0, reason, posTicket, "closed", req.price, reason);
            }
         }
      }
   }
}

// CalculateLots now accepts optional riskPct so we can use server value without touching the input
double CalculateLots(double stopPrice, double riskPct = -1.0)
{
   if(riskPct <= 0.0) riskPct = RiskPercent;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * (riskPct / 100.0);
   double entry = SymbolInfoDouble(currentSymbol, SYMBOL_ASK);

   double stopDist = MathAbs(entry - stopPrice);
   if(stopDist < 0.00001) stopDist = SymbolInfoDouble(currentSymbol, SYMBOL_POINT) * 80;

   double point = SymbolInfoDouble(currentSymbol, SYMBOL_POINT);
   double tickValue = SymbolInfoDouble(currentSymbol, SYMBOL_TRADE_TICK_VALUE);
   if(tickValue <= 0) tickValue = 1.0;

   double lots = riskMoney / (stopDist / point * tickValue);
   return NormalizeDouble(MathMax(0.01, MathMin(lots, 50.0)), 2);
}

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
   req.tp        = tp1;
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
   int end = MathMin(comma, brace);
   if(end < 0) end = StringLen(v);
   v = StringSubstr(v, 0, end);
   StringReplace(v, "\"", "");
   StringReplace(v, "[", "");
   StringReplace(v, "]", "");
   return StringToDouble(v);
}

// Extended SendTradeReport supporting close tracking
void SendTradeReport(string direction, double lots, double sl, double tp1, double tp2, string setup,
                     ulong ticket = 0, string status = "open", double close_price = 0.0, string close_reason = "")
{
   double entry = (direction == "BUY") ? SymbolInfoDouble(currentSymbol, SYMBOL_ASK) :
                                          SymbolInfoDouble(currentSymbol, SYMBOL_BID);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"direction\":\"%s\",\"entry_price\":%.5f,\"stop_loss\":%.5f,"
      "\"tp1\":%.5f,\"tp2\":%.5f,\"volume_lots\":%.2f,\"outcome\":\"%s\",\"notes\":\"CapriQuant-%s\","
      "\"ticket\":%I64u,\"status\":\"%s\",\"close_price\":%.5f,\"close_reason\":\"%s\"}",
      currentSymbol, direction, entry, sl, tp1, tp2, lots, status, setup,
      ticket, status, close_price, close_reason);

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
