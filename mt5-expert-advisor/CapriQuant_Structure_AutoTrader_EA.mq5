//+------------------------------------------------------------------+
//|                          CapriQuant_Structure_AutoTrader_EA.mq5  |
//|                                                                  |
//|  FULLY AUTOMATED TRADING EA                                      |
//|  - Sends market data to Python backend                           |
//|  - Polls the Structure Engine (/signal?engine=structure)         |
//|  - Automatically executes trades using structural levels         |
//|  - Uses stop_suggestion, tp1, tp2 returned by the backend        |
//+------------------------------------------------------------------+
#property copyright "CapriQuant 2026"
#property version   "5.0"
#property strict
#property description "CapriQuant Structure Auto-Trader - Data in, Signal out, Auto execution"

// ==================== INPUTS ====================
input string   ServerURL            = "http://127.0.0.1:8001";
input int      TimerSeconds         = 7;                  // How often to send data + ask for signal
input string   DataTimeframe        = "M5";               // Timeframe to send data for
input string   SignalTimeframe      = "M5";               // Timeframe to request signal for

input double   MinConfidence        = 68.0;               // Only trade if backend confidence >= this
input double   RiskPercent          = 1.8;                // Risk per trade (% of equity)
input int      MaxTradesPerDay      = 3;
input double   MaxSpreadPoints      = 30;                 // Safety filter
input int      Magic                = 20260701;

input bool     EnableTrading        = true;               // Master switch
input bool     LogAllSignals        = true;               // Print every signal received
input bool     CloseOppositeOnSignal = false;             // Close opposite position if new signal in opposite direction

// ==================== GLOBALS ====================
datetime lastTradeDay = 0;
int      tradesToday  = 0;
int      httpTimeout  = 8000;
string   currentSymbol;
ENUM_TIMEFRAMES dataTF;

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   currentSymbol = _Symbol;
   dataTF = StringToTimeframe(DataTimeframe);

   EventSetTimer(TimerSeconds);

   Print("================================================================");
   Print("=== CapriQuant STRUCTURE AUTO-TRADER v5.0                   ===");
   Print("Symbol: ", currentSymbol);
   Print("Sending data on: ", DataTimeframe);
   Print("Requesting signals on: ", SignalTimeframe);
   Print("Min Confidence: ", MinConfidence, "% | Risk: ", RiskPercent, "%");
   Print("");
   Print(">>> This EA sends data + automatically trades on high quality signals <<<");
   Print(">>> Make sure the Python backend is running! <<<");
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
//| Timer - Main Loop                                                |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!EnableTrading) return;

   // Daily reset - fixed version
   MqlDateTime nowStruct, lastStruct;
   TimeToStruct(TimeCurrent(), nowStruct);
   TimeToStruct(lastTradeDay, lastStruct);

   if(nowStruct.day != lastStruct.day || lastTradeDay == 0)
   {
      tradesToday = 0;
      lastTradeDay = TimeCurrent();
   }

   if(tradesToday >= MaxTradesPerDay) return;

   // 1. Send latest market data
   SendMarketData();

   // 2. Ask Python for a signal
   string response = GetStructureSignal(SignalTimeframe);
   if(response == "") return;

   // Always log a short summary of what the backend returned (very useful right now)
   string sig     = ExtractJsonString(response, "signal");
   double conf    = ExtractJsonDouble(response, "confidence");
   string rat     = ExtractJsonString(response, "rationale");
   Print("[CapriQuant] Backend signal for ", currentSymbol, " → ", sig, " | conf=", conf, "% | ", rat);

   ProcessSignalResponse(response);
}

//+------------------------------------------------------------------+
//| Send market data to backend                                      |
//+------------------------------------------------------------------+
void SendMarketData()
{
   double bid   = SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   double ask   = SymbolInfoDouble(currentSymbol, SYMBOL_ASK);
   double open  = iOpen(currentSymbol, dataTF, 0);
   double high  = iHigh(currentSymbol, dataTF, 0);
   double low   = iLow(currentSymbol, dataTF, 0);
   double close = iClose(currentSymbol, dataTF, 0);
   long   vol   = iVolume(currentSymbol, dataTF, 0);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d,"
      "\"balance\":%.2f,\"equity\":%.2f}",
      currentSymbol, DataTimeframe, bid, ask, open, high, low, close, vol,
      balance, equity);

   string headers = "Content-Type: application/json\r\n";
   uchar post_data[];
   StringToCharArray(payload, post_data, 0, StringLen(payload));
   uchar result_data[];
   string response_headers;

   WebRequest("POST", ServerURL + "/market-data", headers, httpTimeout, post_data, result_data, response_headers);
}

//+------------------------------------------------------------------+
//| Request signal from structure engine                             |
//+------------------------------------------------------------------+
string GetStructureSignal(string tf)
{
   // Send the real current spread (in points) instead of hardcoded 0.0
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
   double tp1         = ExtractJsonDouble(json, "tp1");
   double tp2         = ExtractJsonDouble(json, "tp2");

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
   double lots = CalculateLots(stop);
   if(lots <= 0) return;

   ulong ticket = ExecuteTrade(signalDir, lots, stop, tp1, tp2, setup);

   if(ticket > 0)
   {
      tradesToday++;
      Print("[CapriQuant] *** TRADE EXECUTED *** ", signalDir, " | Lots: ", lots, " | Confidence: ", confidence, "%");
      SendTradeReport(signalDir, lots, stop, tp1, tp2, setup);
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
double CalculateLots(double stopPrice)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * (RiskPercent / 100.0);

   double entry = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ?
                  SymbolInfoDouble(currentSymbol, SYMBOL_ASK) :
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
//| Report executed trade to backend for UI / historical tracking    |
//+------------------------------------------------------------------+
void SendTradeReport(string direction, double lots, double sl, double tp1, double tp2, string setup)
{
   double entry = (direction == "BUY") ? SymbolInfoDouble(currentSymbol, SYMBOL_ASK) :
                                          SymbolInfoDouble(currentSymbol, SYMBOL_BID);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"direction\":\"%s\",\"entry_price\":%.5f,\"stop_loss\":%.5f,"
      "\"tp1\":%.5f,\"tp2\":%.5f,\"volume_lots\":%.2f,\"outcome\":\"open\",\"notes\":\"CapriQuant-%s\"}",
      currentSymbol, direction, entry, sl, tp1, tp2, lots, setup);

   string headers = "Content-Type: application/json\r\n";
   uchar post_data[];
   StringToCharArray(payload, post_data, 0, StringLen(payload));
   uchar result_data[];
   string response_headers;

   WebRequest("POST", ServerURL + "/report-trade", headers, 5000, post_data, result_data, response_headers);
}

//+------------------------------------------------------------------+
