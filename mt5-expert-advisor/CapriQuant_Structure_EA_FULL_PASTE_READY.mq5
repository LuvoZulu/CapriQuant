//+------------------------------------------------------------------+
//|           CapriQuant_Structure_EA_FULL_PASTE_READY.mq5           |
//|                                                                  |
//|  PURE DATA FEEDER EA  (v4.0 - No Timer, No Signals, No Trading)  |
//|                                                                  |
//|  PURPOSE:                                                        |
//|  - ONLY sends market data (OHLCV + account snapshot) to backend  |
//|  - Triggers on NEW BARS (no EventSetTimer / OnTimer used)        |
//|  - Completely stops all /signal polling (eliminates 400 errors)  |
//|                                                                  |
//|  INSTRUCTIONS:                                                   |
//|  1. Open MetaEditor in MT5                                       |
//|  2. File → New → Expert Advisor (template)                       |
//|  3. Delete everything in the new file                            |
//|  4. Copy EVERYTHING below this header into the file              |
//|  5. Compile (F7)                                                 |
//|  6. Attach ONE EA per symbol (e.g. XAUUSD M5 chart, US30 M5, etc)|
//|                                                                  |
//|  The Python backend will receive fresh bars for /signal queries  |
//|  from other tools / future EAs / manual testing.                 |
//|                                                                  |
//|  WebRequest: In EA Properties → Common tab → "Allow WebRequest"  |
//|  and add the exact URL: http://127.0.0.1:8001                    |
//+------------------------------------------------------------------+
#property copyright "CapriQuant 2026"
#property version   "4.0"
#property strict
#property description "CapriQuant Pure Data Feeder - sends market data only (no timer, no trading, no signal polling)"

// ==================== INPUTS (DATA FEEDER - MINIMAL) ====================
input string   ServerURL         = "http://127.0.0.1:8001";
input string   DataTimeframe     = "M5";            // Primary TF to feed (M1/M5/M15 etc). EA reacts on new bars of this TF.
input bool     AlsoSendHigherTF  = true;            // Also push HigherTimeframe bars (very useful for backend)
input string   HigherTimeframe   = "M15";

input bool     EnableDataPush    = true;            // Master switch
input bool     LogEveryPush      = false;           // Verbose: log every successful POST (good for first tests)
input int      HttpTimeoutMs     = 6000;

// ==================== GLOBALS ====================
string            currentSymbol;
datetime          lastPrimaryBarTime = 0;
datetime          lastHigherBarTime  = 0;
ENUM_TIMEFRAMES   primaryTF;
ENUM_TIMEFRAMES   higherTF;

//+------------------------------------------------------------------+
//| OnInit - Pure data feeder setup                                  |
//+------------------------------------------------------------------+
int OnInit()
{
   currentSymbol = _Symbol;
   primaryTF     = StringToTimeframe(DataTimeframe);
   higherTF      = StringToTimeframe(HigherTimeframe);

   Print("================================================================");
   Print("=== CapriQuant DATA FEEDER v4.0 (No Timer / No Trading)     ===");
   Print("Symbol: ", currentSymbol, " | Primary TF: ", DataTimeframe);
   if(AlsoSendHigherTF) Print("Also feeding: ", HigherTimeframe);
   Print("Backend: ", ServerURL);
   Print("");
   Print(">>> This EA ONLY sends data on new bars. No /signal calls. <<<");
   Print(">>> No trading logic. No OnTimer. No 400 errors from this EA. <<<");
   Print(">>> Old 400s were from premature /signal calls before DB had 30+ candles. <<<");
   Print("");
   Print("1. In EA Properties → Common tab: CHECK 'Allow WebRequest'");
   Print("2. Add exactly this address to the list: ", ServerURL);
   Print("3. Attach to chart and leave running (one EA per symbol is ideal)");
   Print("================================================================");

   // Immediate bootstrap push of current bar
   if(EnableDataPush)
   {
      PushMarketData(DataTimeframe);
      // Seed the bar times so the very next OnTick does not re-push the same bar
      lastPrimaryBarTime = iTime(currentSymbol, primaryTF, 0);
      if(AlsoSendHigherTF)
         lastHigherBarTime = iTime(currentSymbol, higherTF, 0);
   }

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   // No timer to kill - we use OnTick + new-bar detection only
}

//+------------------------------------------------------------------+
//| OnTick - New bar detection (NO TIMER USED)                       |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!EnableDataPush) return;

   // --- Primary timeframe new bar? ---
   datetime barTime = iTime(currentSymbol, primaryTF, 0);
   if(barTime != lastPrimaryBarTime && barTime > 0)
   {
      lastPrimaryBarTime = barTime;
      PushMarketData(DataTimeframe);
   }

   // --- Optional higher timeframe (e.g. M15) on its own new bars ---
   if(AlsoSendHigherTF && higherTF != primaryTF)
   {
      datetime hbar = iTime(currentSymbol, higherTF, 0);
      if(hbar != lastHigherBarTime && hbar > 0)
      {
         lastHigherBarTime = hbar;
         PushMarketData(HigherTimeframe);
      }
   }
}

//+------------------------------------------------------------------+
//| Convert string like "M5" / "M15" to ENUM_TIMEFRAMES              |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES StringToTimeframe(string tf)
{
   string s = tf;
   StringToUpper(s);
   if(s == "M1"  || s == "1")   return PERIOD_M1;
   if(s == "M5"  || s == "5")   return PERIOD_M5;
   if(s == "M15" || s == "15")  return PERIOD_M15;
   if(s == "M30" || s == "30")  return PERIOD_M30;
   if(s == "H1"  || s == "60")  return PERIOD_H1;
   if(s == "H4"  || s == "240") return PERIOD_H4;
   if(s == "D1")                return PERIOD_D1;
   if(s == "W1")                return PERIOD_W1;
   return PERIOD_CURRENT;   // fallback = whatever chart the EA is on
}

//+------------------------------------------------------------------+
//| Push latest bar data for a given timeframe (the ONLY network op) |
//+------------------------------------------------------------------+
void PushMarketData(string tfStr)
{
   ENUM_TIMEFRAMES tfEnum = StringToTimeframe(tfStr);

   double bid   = SymbolInfoDouble(currentSymbol, SYMBOL_BID);
   double ask   = SymbolInfoDouble(currentSymbol, SYMBOL_ASK);
   double open  = iOpen (currentSymbol, tfEnum, 0);
   double high  = iHigh (currentSymbol, tfEnum, 0);
   double low   = iLow  (currentSymbol, tfEnum, 0);
   double close = iClose(currentSymbol, tfEnum, 0);
   long   vol   = iVolume(currentSymbol, tfEnum, 0);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d,"
      "\"balance\":%.2f,\"equity\":%.2f}",
      currentSymbol, tfStr, bid, ask, open, high, low, close, vol,
      balance, equity);

   string headers = "Content-Type: application/json\r\n";
   uchar  post_data[];
   StringToCharArray(payload, post_data, 0, StringLen(payload));
   uchar  result_data[];
   string response_headers;

   ResetLastError();
   int res = WebRequest("POST", ServerURL + "/market-data", headers, HttpTimeoutMs, post_data, result_data, response_headers);

   if(LogEveryPush || res != 200)
   {
      Print("[CapriQuant Data] ", tfStr, " | ", currentSymbol,
            " | close=", DoubleToString(close, 5),
            " | HTTP=", res,
            (res != 200 ? "  <-- check backend + WebRequest permissions" : ""));
   }
}
//+------------------------------------------------------------------+
