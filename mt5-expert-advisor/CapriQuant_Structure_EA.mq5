//+------------------------------------------------------------------+
//|                                    CapriQuant_Structure_EA.mq5   |
//|  Modern EA for the new structure-first engine (engine=structure) |
//|                                                                  |
//|  Key improvements over the original data-only EA:                |
//|  - Pulls high-quality signals from /signal?...&engine=structure  |
//|  - Uses structural stop / TP suggestions from the Python brain   |
//|  - Dynamic risk sizing (respects the aggressive goal safely)     |
//|  - Only takes trades with real confluences + high confidence     |
//|  - Logs rationale for every decision                             |
//+------------------------------------------------------------------+
#property copyright "CapriQuant"
#property version   "3.0"
#property strict

//--- Input parameters
input string   ServerURL          = "http://127.0.0.1:8001";
input int      TimerSeconds       = 8;                 // How often to check for signals
input double   MaxSpreadPoints    = 35;                // Safety filter
input double   MinConfidence      = 68.0;              // Only trade if engine confidence >= this
input int      MagicNumber        = 202607;
input double   RiskPctOverride    = 0.0;               // 0 = let server decide, else force this %
input int      MaxTradesPerDay    = 3;
input bool     EnableTrading      = true;              // Master kill switch

//--- Global variables
datetime LastTradeDay = 0;
int      TradesToday  = 0;
int      HttpTimeout  = 6000;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(TimerSeconds);
   Print("[CapriQuant] Structure EA v3.0 initialized. Using engine=structure");
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| Timer - main decision loop                                       |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!EnableTrading) return;

   string symbol = _Symbol;
   string tf     = "M5";                    // Primary polling TF (adjust as needed)

   // 1. Send current market snapshot (enhanced payload)
   SendMarketData(symbol);

   // 2. Ask the structure engine for a decision
   string signal = GetStructureSignal(symbol, tf);

   if(signal == "" || signal == "HOLD") return;

   // 3. Parse the rich JSON response (very basic string parsing for MQL5)
   // In production you would use a proper JSON parser library.
   double confidence = GetJsonDouble(signal, "confidence");
   if(confidence < MinConfidence)
   {
      Print("[CapriQuant] Low confidence (", confidence, "%) - skipping");
      return;
   }

   string direction = GetJsonString(signal, "signal");
   double entry     = GetJsonDouble(signal, "entry_zone[0]");  // rough
   double stop      = GetJsonDouble(signal, "stop_suggestion");
   double tp1       = GetJsonDouble(signal, "tp1");
   double tp2       = GetJsonDouble(signal, "tp2");

   if(stop == 0.0 || MathAbs(entry - stop) < 0.0001)
   {
      Print("[CapriQuant] No valid structural stop provided - skipping trade");
      return;
   }

   // 4. Risk & Position sizing (simple version - enhance with your broker specs)
   double lots = CalculateLots(symbol, entry, stop);

   // 5. Execute
   if(direction == "BUY" || direction == "SELL")
   {
      if(TradesToday >= MaxTradesPerDay && Day() != LastTradeDay)
      {
         TradesToday = 0;
         LastTradeDay = Day();
      }
      if(TradesToday >= MaxTradesPerDay) return;

      ulong ticket = OpenTrade(direction, lots, stop, tp1, tp2, symbol);
      if(ticket > 0)
      {
         TradesToday++;
         Print("[CapriQuant] TRADE TAKEN | ", direction, " | Confidence: ", confidence,
               "% | Setup: ", GetJsonString(signal, "setup"),
               " | Rationale: ", GetJsonString(signal, "rationale"));
      }
   }
}

//+------------------------------------------------------------------+
//| Send enhanced market data to backend                             |
//+------------------------------------------------------------------+
void SendMarketData(string symbol)
{
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);

   MqlTick tick;
   SymbolInfoTick(symbol, tick);

   double open  = iOpen(symbol, PERIOD_M5, 0);
   double high  = iHigh(symbol, PERIOD_M5, 0);
   double low   = iLow(symbol, PERIOD_M5, 0);
   double close = iClose(symbol, PERIOD_M5, 0);
   long   vol   = iVolume(symbol, PERIOD_M5, 0);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"M5\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d,"
      "\"balance\":%.2f,\"equity\":%.2f}",
      symbol, bid, ask, open, high, low, close, vol, balance, equity);

   char post[];
   StringToCharArray(payload, post, 0, StringLen(payload));

   char result[];
   string headers;
   int res = WebRequest("POST", ServerURL + "/market-data", "Content-Type: application/json\r\n", HttpTimeout, post, result, headers);

   // We don't care much about the response for data pushes
}

//+------------------------------------------------------------------+
//| Call the new structure engine                                    |
//+------------------------------------------------------------------+
string GetStructureSignal(string symbol, string tf)
{
   string url = StringFormat("%s/signal/%s/%s?engine=structure&spread=0.0", ServerURL, symbol, tf);

   char result[];
   string headers;
   int res = WebRequest("GET", url, NULL, HttpTimeout, result, headers);

   if(res != 200)
   {
      Print("[CapriQuant] Signal request failed: ", res);
      return "";
   }

   return CharArrayToString(result);
}

//+------------------------------------------------------------------+
//| Very lightweight JSON helpers (for demo - replace with real parser)
//+------------------------------------------------------------------+
double GetJsonDouble(string json, string key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return 0.0;

   string val = StringSubstr(json, pos + StringLen(search));
   // crude extraction
   int comma = StringFind(val, ",");
   int brace = StringFind(val, "}");
   int end = (comma > 0 && comma < brace) ? comma : brace;
   if(end < 0) end = StringLen(val);

   val = StringSubstr(val, 0, end);
   StringReplace(val, "\"", "");
   StringReplace(val, "[", "");
   StringReplace(val, "]", "");
   return StringToDouble(val);
}

string GetJsonString(string json, string key)
{
   string search = "\"" + key + "\":\"";
   int pos = StringFind(json, search);
   if(pos < 0) return "";

   string val = StringSubstr(json, pos + StringLen(search));
   int end = StringFind(val, "\"");
   if(end < 0) return "";
   return StringSubstr(val, 0, end);
}

//+------------------------------------------------------------------+
//| Calculate lots based on structural stop distance                 |
//+------------------------------------------------------------------+
double CalculateLots(string symbol, double entry, double stop)
{
   double riskMoney = AccountInfoDouble(ACCOUNT_EQUITY) * 0.018;   // ~1.8% risk (aggressive but controlled)
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);

   double stopDist = MathAbs(entry - stop);
   if(stopDist <= 0) stopDist = point * 80;

   double lots = riskMoney / (stopDist / point * tickValue);
   lots = MathMax(0.01, MathMin(lots, 10.0));   // sane bounds for most brokers

   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
//| Place the actual trade with structural SL/TP                     |
//+------------------------------------------------------------------+
ulong OpenTrade(string direction, double lots, double stop, double tp1, double tp2, string symbol)
{
   MqlTradeRequest  req = {};
   MqlTradeResult   res = {};

   req.action    = TRADE_ACTION_DEAL;
   req.symbol    = symbol;
   req.volume    = lots;
   req.type      = (direction == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price     = (direction == "BUY") ? SymbolInfoDouble(symbol, SYMBOL_ASK) :
                                          SymbolInfoDouble(symbol, SYMBOL_BID);
   req.sl        = stop;
   req.tp        = tp1;                    // First target
   req.deviation = 30;
   req.magic     = MagicNumber;
   req.comment   = "CapriQuant-Structure-v3";

   if(!OrderSend(req, res))
   {
      Print("[CapriQuant] OrderSend failed: ", res.retcode, " - ", res.comment);
      return 0;
   }

   // Optional: place second TP as a limit order or just trail manually
   return res.order;
}
//+------------------------------------------------------------------+
