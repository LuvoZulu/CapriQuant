//+------------------------------------------------------------------+
//|           CapriQuant_Structure_EA_FULL_PASTE_READY.mq5           |
//|                                                                  |
//|  COMPLETE COPY-PASTE READY MQL5 EA FOR THE NEW STRUCTURE ENGINE  |
//|  Version 3.4 - Heavy Safety Filters (Daily DD, Min RR, Confluence)|
//|                                                                  |
//|  INSTRUCTIONS:                                                   |
//|  1. Open MetaEditor in MT5                                       |
//|  2. File → New → Expert Advisor (template)                       |
//|  3. Delete everything in the new file                            |
//|  4. Copy EVERYTHING below this header into the file              |
//|  5. Compile (F7)                                                 |
//|  6. Attach to chart (recommended: XAUUSD M5 or NAS100 M5/M15)    |
//|                                                                  |
//|  This EA is designed for the NEW structure-first engine.         |
//|  It will only take high-confluence trades with structural stops. |
//|                                                                  |
//|  WebRequest fix (v3.3): Uses the standard 7-parameter form       |
//|  that works on the vast majority of MQL5 builds.                 |
//+------------------------------------------------------------------+
#property copyright "CapriQuant 2026"
#property version   "3.5"
#property strict
#property description "CapriQuant Structure-First EA - uses engine=structure"

// ==================== INPUTS ====================
input string   ServerURL            = "http://127.0.0.1:8001";
input int      PollSeconds          = 7;
input string   PrimaryTimeframe     = "M5";           // Main signal TF
input string   HigherTimeframe      = "M15";          // For bias filter (optional)

input double   MinConfidence        = 67.0;           // Minimum engine confidence %
input int      MaxTradesPerDay      = 4;
input double   MaxSpreadPoints      = 28;             // Safety (adjust per symbol)
input int      Magic                = 20260701;

input double   RiskPercent          = 1.85;           // Base risk per trade (aggressive but controlled)
input bool     UseDynamicRisk       = true;           // Let server + goal logic influence size
input bool     EnableTrading        = true;           // Master switch

input bool     RequireStructureBias = true;           // Only trade in direction of structure bias
input bool     LogAllSignals        = true;           // Print full rationale to Experts tab

// ==================== NEW SAFETY FILTERS (v3.4) ====================
input double   MinRR                = 1.6;            // Minimum Reward:Risk using TP1 vs Stop (very important)
input double   MinTotalConfluence   = 0.85;           // Minimum sum of contextual scores (amd + fib + pa + liquidity)
input double   DailyLossLimitPct    = 4.5;            // Stop trading for the day if equity drops this % from start of day
input double   MaxEquityDrawdownPct = 12.0;           // Global max drawdown from peak equity (safety brake)
input int      WebRequestRetries    = 2;              // How many times to retry failed web requests
input bool     RequireMinConfluences = true;          // Require at least 3 named confluences from Python engine

// ==================== GLOBALS ====================
datetime lastTradeDay = 0;
int      tradesToday  = 0;
int      httpTimeout  = 8000;
string   currentSymbol;

double   startingDayEquity = 0.0;
double   peakEquity        = 0.0;
bool     tradingHalted     = false;   // Safety brake activated

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   currentSymbol = _Symbol;
   EventSetTimer(PollSeconds);

   startingDayEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   peakEquity        = startingDayEquity;

   Print("=== CapriQuant Structure EA v3.5 (Improved Diagnostics) ===");
   Print("MinRR: ", MinRR, " | MinConfluence: ", MinTotalConfluence);
   Print("Daily Loss Limit: ", DailyLossLimitPct, "% | Max DD: ", MaxEquityDrawdownPct, "%");
   Print("");
   Print("IMPORTANT: If you see 'WebRequest failed (code=-1)', go to:");
   Print("Expert Properties → Common tab → Check 'Allow WebRequest'");
   Print("and add this URL: http://127.0.0.1:8001");
   Print("");
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
//| Timer - Main Loop (v3.4 - Heavy Safety Filters)                  |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!EnableTrading || tradingHalted) return;

   // === Daily Reset + Safety Tracking ===
   MqlDateTime nowStruct, lastStruct;
   TimeToStruct(TimeCurrent(), nowStruct);
   TimeToStruct(lastTradeDay, lastStruct);

   if(nowStruct.day != lastStruct.day || lastTradeDay == 0)
   {
      tradesToday = 0;
      lastTradeDay = TimeCurrent();
      startingDayEquity = AccountInfoDouble(ACCOUNT_EQUITY);
      Print("[CapriQuant] New trading day. Starting equity: ", startingDayEquity);
   }

   // Global Equity Protection (drawdown from peak)
   double currentEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(currentEquity > peakEquity) peakEquity = currentEquity;

   double ddPct = (peakEquity - currentEquity) / peakEquity * 100.0;
   if(ddPct >= MaxEquityDrawdownPct)
   {
      tradingHalted = true;
      Print("!!! [CapriQuant] MAX EQUITY DRAWDOWN REACHED (", ddPct, "%) - TRADING HALTED !!!");
      return;
   }

   // Daily Loss Limit
   double dailyLossPct = (startingDayEquity - currentEquity) / startingDayEquity * 100.0;
   if(dailyLossPct >= DailyLossLimitPct)
   {
      Print("[CapriQuant] Daily loss limit reached (", dailyLossPct, "%). No more trades today.");
      return;
   }

   if(tradesToday >= MaxTradesPerDay) return;

   // Send market data
   SendEnhancedMarketData();

   // Get signal with retry
   string response = FetchStructureSignalWithRetry(PrimaryTimeframe);
   if(response == "") return;

   // === Parse signal ===
   string signalDir   = ExtractJsonString(response, "signal");
   double confidence  = ExtractJsonDouble(response, "confidence");
   string setupName   = ExtractJsonString(response, "setup");
   string rationale   = ExtractJsonString(response, "rationale");
   double stopPrice   = ExtractJsonDouble(response, "stop_suggestion");
   double tp1Price    = ExtractJsonDouble(response, "tp1");
   double tp2Price    = ExtractJsonDouble(response, "tp2");

   // Extract total contextual score if available
   double totalConfluence = ExtractJsonDouble(response, "contextual_scores.total");

   if(signalDir != "BUY" && signalDir != "SELL") return;

   // === NEW FILTERS (v3.4) ===

   // 1. Basic confidence
   if(confidence < MinConfidence)
   {
      if(LogAllSignals) Print("[CapriQuant] REJECTED: Low confidence ", confidence, "% (need >=", MinConfidence, ")");
      return;
   }

   // 2. Structure bias filter
   string bias = ExtractJsonString(response, "bias");
   if(RequireStructureBias && ((signalDir == "BUY" && bias == "BEARISH") || (signalDir == "SELL" && bias == "BULLISH")))
   {
      if(LogAllSignals) Print("[CapriQuant] REJECTED: Against structure bias (", bias, ")");
      return;
   }

   // 3. Minimum Reward:Risk (very important after seeing bad backtest)
   double entryPrice = (signalDir == "BUY") ? SymbolInfoDouble(currentSymbol, SYMBOL_ASK) : SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   double riskDist   = MathAbs(entryPrice - stopPrice);
   double rewardDist = MathAbs(tp1Price - entryPrice);
   double rr = (riskDist > 0) ? rewardDist / riskDist : 0;

   if(rr < MinRR)
   {
      if(LogAllSignals) Print("[CapriQuant] REJECTED: Poor R:R = ", rr, " (minimum required: ", MinRR, ")");
      return;
   }

   // 4. Contextual Confluence Score filter (new powerful filter)
   if(totalConfluence < MinTotalConfluence)
   {
      if(LogAllSignals) Print("[CapriQuant] REJECTED: Weak confluence total = ", totalConfluence, " (need >=", MinTotalConfluence, ")");
      return;
   }

   // 5. Require decent number of confluences
   if(RequireMinConfluences)
   {
      int confluenceCount = 0;
      string confStr = ExtractJsonString(response, "confluences");
      // Very rough count of commas + 1
      for(int i=0; i<StringLen(confStr); i++) if(StringGetCharacter(confStr,i)==',') confluenceCount++;
      if(confluenceCount < 2)   // at least 3 items
      {
         if(LogAllSignals) Print("[CapriQuant] REJECTED: Too few confluences (", confluenceCount+1, ")");
         return;
      }
   }

   // 6. Spread filter
   double spread = (SymbolInfoDouble(currentSymbol, SYMBOL_ASK) - SymbolInfoDouble(currentSymbol, SYMBOL_BID)) / _Point;
   if(spread > MaxSpreadPoints)
   {
      if(LogAllSignals) Print("[CapriQuant] REJECTED: Spread too wide (", spread, ")");
      return;
   }

   // === PASSED ALL FILTERS - Execute ===
   double lots = CalculateStructuralLots(entryPrice, stopPrice);
   ulong ticket = ExecuteTrade(signalDir, lots, stopPrice, tp1Price, tp2Price, setupName);

   if(ticket > 0)
   {
      tradesToday++;
      if(LogAllSignals)
      {
         Print("========================================");
         Print("[CapriQuant] TRADE TAKEN v3.4");
         Print("Direction : ", signalDir, " | R:R = ", rr);
         Print("Setup     : ", setupName);
         Print("Confidence: ", confidence, "% | Confluence: ", totalConfluence);
         Print("Rationale : ", rationale);
         Print("Stop / TP1: ", stopPrice, " / ", tp1Price);
         Print("Lots      : ", lots);
         Print("========================================");
      }
   }
}

//+------------------------------------------------------------------+
//| Send rich market data (includes account info for risk engine)    |
//+------------------------------------------------------------------+
void SendEnhancedMarketData()
{
   double bid = SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(currentSymbol, SYMBOL_ASK);
   double open = iOpen(currentSymbol, PERIOD_M5, 0);
   double high = iHigh(currentSymbol, PERIOD_M5, 0);
   double low  = iLow(currentSymbol, PERIOD_M5, 0);
   double close= iClose(currentSymbol, PERIOD_M5, 0);
   long   vol  = iVolume(currentSymbol, PERIOD_M5, 0);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin  = AccountInfoDouble(ACCOUNT_MARGIN);
   double freeMar = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"M5\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d,"
      "\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,\"free_margin\":%.2f}",
      currentSymbol, bid, ask, open, high, low, close, vol,
      balance, equity, margin, freeMar);

   // === FIXED WebRequest for maximum MQL5 compatibility ===
   string headers = "Content-Type: application/json\r\n";
   uchar  post_data[];
   StringToCharArray(payload, post_data, 0, StringLen(payload));
   uchar  result_data[];
   string response_headers;

   int res = WebRequest("POST", ServerURL + "/market-data", headers, httpTimeout, post_data, result_data, response_headers);
}

//+------------------------------------------------------------------+
//| Fetch signal with improved diagnostics (v3.5)                    |
//+------------------------------------------------------------------+
string FetchStructureSignalWithRetry(string tf)
{
   string url = StringFormat("%s/signal/%s/%s?engine=structure&min_candles=8&spread=0.0", ServerURL, currentSymbol, tf);

   static bool firstFailurePrinted = false;

   for(int attempt = 1; attempt <= WebRequestRetries; attempt++)
   {
      string headers = "";
      uchar  dummy_data[1];
      uchar  result_data[];
      string response_headers;

      ResetLastError();
      int res = WebRequest("GET", url, headers, httpTimeout, dummy_data, result_data, response_headers);
      int lastErr = GetLastError();

      if(res == 200)
      {
         return CharArrayToString(result_data);
      }

      // Enhanced diagnostics
      if(!firstFailurePrinted)
      {
         Print("=== [CapriQuant] WebRequest DIAGNOSTICS ===");
         Print("URL attempted: ", url);
         Print("Returned code: ", res);
         Print("GetLastError(): ", lastErr);
         Print("Common causes of code -1:");
         Print("1. 'Allow WebRequest' is NOT checked in EA Properties (Common tab)");
         Print("2. http://127.0.0.1:8001 is not added in the allowed URL list");
         Print("3. You are running in Strategy Tester without enabling WebRequest for this test");
         Print("4. Python backend is not running on port 8001");
         Print("===========================================");
         firstFailurePrinted = true;
      }

      if(attempt < WebRequestRetries)
         Print("[CapriQuant] WebRequest attempt ", attempt, " failed (code=", res, ", lastErr=", lastErr, "). Retrying...");
      else
         Print("[CapriQuant] All WebRequest attempts failed. Final code: ", res, " | LastError: ", lastErr);
   }
   return "";
}

//+------------------------------------------------------------------+
//| Robust(ish) JSON extractors - sufficient for this use case       |
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
//| Calculate lots from structural stop distance + risk %            |
//+------------------------------------------------------------------+
double CalculateStructuralLots(double entry, double stop)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * (RiskPercent / 100.0);

   double stopDist = MathAbs(entry - stop);
   if(stopDist < 0.00001) stopDist = SymbolInfoDouble(currentSymbol, SYMBOL_POINT) * 80;

   double point = SymbolInfoDouble(currentSymbol, SYMBOL_POINT);
   double tickValue = SymbolInfoDouble(currentSymbol, SYMBOL_TRADE_TICK_VALUE);

   if(tickValue <= 0) tickValue = 1.0; // fallback for some symbols

   double lots = riskMoney / (stopDist / point * tickValue);
   lots = MathMax(0.01, MathMin(lots, 50.0));

   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
//| Send the actual trade with structural SL/TP                      |
//+------------------------------------------------------------------+
ulong ExecuteTrade(string dir, double lots, double sl, double tp1, double tp2, string setup)
{
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};

   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = currentSymbol;
   req.volume    = lots;
   req.type      = (dir == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price     = (dir == "BUY") ? SymbolInfoDouble(currentSymbol, SYMBOL_ASK) :
                                    SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   req.sl        = sl;
   req.tp        = tp1;
   req.deviation = 35;
   req.magic     = Magic;
   req.comment   = "CapriQuant-" + setup;

   if(!OrderSend(req, res))
   {
      Print("[CapriQuant] OrderSend FAILED: ", res.retcode, " - ", res.comment);
      return 0;
   }

   // Optional: you can add a second TP as a pending order here if desired
   return res.order;
}
//+------------------------------------------------------------------+
