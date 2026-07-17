//+------------------------------------------------------------------+
//|                                     ProductionHardening.mqh      |
//|                   Production Hardening Utilities for PineTunnel  |
//|                                    Phase 4 - Reliability & Safety|
//|                      Unified MT4/MT5 Version                     |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"
#property version   "1.04"
#property strict

#ifndef ProductionHardening_mqh
#define ProductionHardening_mqh

//+------------------------------------------------------------------+
//| Platform-Specific Includes                                        |
//+------------------------------------------------------------------+
#ifdef __MQL5__
   #include <Trade\Trade.mqh>
   #include <Trade\SymbolInfo.mqh>
#else
   // MT4 - use native functions only
#endif

//+------------------------------------------------------------------+
//| Platform-Specific Constants                                      |
//+------------------------------------------------------------------+
#ifdef __MQL5__
   // MT5 uses ORDER_TYPE_* enums
#else
   // MT4 uses OP_* constants - define compatibility aliases
   #define ORDER_TYPE_BUY         OP_BUY
   #define ORDER_TYPE_SELL        OP_SELL
    #define ORDER_TYPE_BUY_LIMIT   OP_BUYLIMIT
    #define ORDER_TYPE_SELL_LIMIT  OP_SELLLIMIT
    #define ORDER_TYPE_BUY_STOP    OP_BUYSTOP
    #define ORDER_TYPE_SELL_STOP   OP_SELLSTOP

   // MT4 trade return codes
   #define TRADE_RETCODE_REQUOTE           10004
   #define TRADE_RETCODE_PRICE_OFF         10015
   #define TRADE_RETCODE_TIMEOUT           10012
   #define TRADE_RETCODE_PRICE_CHANGED     10020
   #define TRADE_RETCODE_REJECT            10006
   #define TRADE_RETCODE_ERROR             10011
   #define TRADE_RETCODE_TRADE_DISABLED    10017
   #define TRADE_RETCODE_MARKET_CLOSED     10018
   #define TRADE_RETCODE_NO_MONEY          10019
   #define TRADE_RETCODE_TOO_MANY_REQUESTS 10043
   #define TRADE_RETCODE_CANCEL            10007
   #define TRADE_RETCODE_PLACED            10008
   #define TRADE_RETCODE_DONE              10009
   #define TRADE_RETCODE_DONE_PARTIAL      10010
   #define TRADE_RETCODE_INVALID           10013
   #define TRADE_RETCODE_INVALID_VOLUME    10014
   #define TRADE_RETCODE_INVALID_PRICE     10015
   #define TRADE_RETCODE_INVALID_STOPS     10016
   #define TRADE_RETCODE_NO_CHANGES        10025
   #define TRADE_RETCODE_SERVER_DISABLES_AT 10026
   #define TRADE_RETCODE_CLIENT_DISABLES_AT 10027
   #define TRADE_RETCODE_LOCKED            10028
   #define TRADE_RETCODE_FROZEN            10029
   #define TRADE_RETCODE_INVALID_FILL      10039

   // MT5 symbol info enum equivalents
   #define SYMBOL_TRADE_MODE_DISABLED      0
   #define SYMBOL_TRADE_MODE_FULL          1
   #define SYMBOL_TRADE_EXECUTION_INSTANT  0
   #define SYMBOL_TRADE_EXECUTION_MARKET   1

   #define SYMBOL_TRADE_STOPS_LEVEL        23
   #define SYMBOL_TRADE_MODE               20
   #define SYMBOL_TRADE_EXEMODE            21
#endif

//+------------------------------------------------------------------+
//| Production Configuration Constants                               |
//+------------------------------------------------------------------+
#define PH_MAX_ORDER_RETRIES        5       // Maximum order retry attempts
#define PH_INITIAL_RETRY_DELAY_MS   100     // Initial retry delay
#define PH_MAX_RETRY_DELAY_MS       5000    // Maximum retry delay
#define PH_MAX_SPREAD_POINTS        500     // Maximum allowed spread in points
#define PH_MAX_PRICE_AGE_SECONDS    30      // Maximum price age before considered stale
#define PH_MAX_PRICE_DEVIATION_PCT  5.0     // Maximum price deviation (5%)
#define PH_MEMORY_CHECK_INTERVAL    300     // Memory check interval (seconds)
#define PH_VOLUME_STEP_TOLERANCE    0.0001  // Tolerance for volume step alignment
#define PH_MEMORY_GROWTH_THRESHOLD  (10 * 1024 * 1024)  // Memory growth warning threshold (10 MB)
#define PH_MIN_LICENSE_LENGTH       5       // Minimum license ID length
#define PH_MAX_TRADING_SESSIONS     3       // Maximum trading sessions to check per day
#define PH_DEFAULT_SLIPPAGE         20      // Default slippage for MT4 wrapper

//+------------------------------------------------------------------+
//| Safe Delete Utilities                                             |
//+------------------------------------------------------------------+
#ifdef __MQL5__
template<typename T>
void SafeDelete(T*& obj)
{
   if(obj != NULL)
   {
      delete obj;
      obj = NULL;
   }
}
#endif

//+------------------------------------------------------------------+
//| Production Logger - Controlled logging for production            |
//+------------------------------------------------------------------+
class CProductionLogger
{
private:
   bool   m_verboseMode;
   string m_eaName;

public:
   CProductionLogger(string eaName = "PineTunnel", bool verbose = false)
   {
      m_verboseMode = verbose;
      m_eaName = eaName;
   }

   void Error(string message)
   {
      Print("[", m_eaName, "] [ERROR] ", message);
   }

   void Warning(string message)
   {
      Print("[", m_eaName, "] [WARN] ", message);
   }

   void Info(string message)
   {
      Print("[", m_eaName, "] [INFO] ", message);
   }

   void Trade(string action, string symbol, double volume, double price, bool success, string error = "")
   {
      if(success)
         Print("[", m_eaName, "] [TRADE] ", action, " ", symbol, " ", DoubleToString(volume, 2),
               " lots @ ", DoubleToString(price, 5));
      else
         Print("[", m_eaName, "] [TRADE FAIL] ", action, " ", symbol, " - ", error);
   }

};

#ifdef __MQL5__
//+------------------------------------------------------------------+
//| Order Execution Manager with Retry Logic                         |
//+------------------------------------------------------------------+
class COrderExecutionManager
{
private:
#ifdef __MQL5__
   CTrade* m_trade;
#else
   CTradeWrapper* m_trade;
#endif
   CProductionLogger* m_logger;
   int     m_maxRetries;
   int     m_initialDelayMs;
   int     m_maxDelayMs;

   int CalculateBackoffDelay(int attempt)
   {
      int delay = m_initialDelayMs * (int)MathPow(2, attempt);
      return MathMin(delay, m_maxDelayMs);
   }

   bool IsRetryableError(uint retcode)
   {
      switch(retcode)
      {
         case TRADE_RETCODE_REQUOTE:
         case TRADE_RETCODE_PRICE_OFF:
         case TRADE_RETCODE_TIMEOUT:
         case TRADE_RETCODE_PRICE_CHANGED:
         case TRADE_RETCODE_REJECT:
         case TRADE_RETCODE_ERROR:
         case TRADE_RETCODE_TRADE_DISABLED:
         case TRADE_RETCODE_MARKET_CLOSED:
         case TRADE_RETCODE_NO_MONEY:
         case TRADE_RETCODE_TOO_MANY_REQUESTS:
            return true;
         default:
            return false;
      }
   }

   string GetErrorDescription(uint retcode)
   {
      switch(retcode)
      {
         case TRADE_RETCODE_REQUOTE:           return "Requote";
         case TRADE_RETCODE_REJECT:            return "Request rejected";
         case TRADE_RETCODE_CANCEL:            return "Request canceled";
         case TRADE_RETCODE_PLACED:            return "Order placed";
         case TRADE_RETCODE_DONE:              return "Request completed";
         case TRADE_RETCODE_DONE_PARTIAL:      return "Partial completion";
         case TRADE_RETCODE_ERROR:             return "Processing error";
         case TRADE_RETCODE_TIMEOUT:           return "Request timeout";
         case TRADE_RETCODE_INVALID:           return "Invalid request";
         case TRADE_RETCODE_INVALID_VOLUME:    return "Invalid volume";
         case TRADE_RETCODE_INVALID_STOPS:     return "Invalid stops";
         case TRADE_RETCODE_TRADE_DISABLED:    return "Trade disabled";
         case TRADE_RETCODE_MARKET_CLOSED:     return "Market closed";
         case TRADE_RETCODE_NO_MONEY:          return "Insufficient funds";
         case TRADE_RETCODE_PRICE_OFF:         return "Price off";
         case TRADE_RETCODE_PRICE_CHANGED:     return "Price changed";
         case TRADE_RETCODE_TOO_MANY_REQUESTS: return "Too many requests";
         case TRADE_RETCODE_NO_CHANGES:        return "No changes";
         case TRADE_RETCODE_SERVER_DISABLES_AT:return "AutoTrading disabled";
         case TRADE_RETCODE_CLIENT_DISABLES_AT:return "AutoTrading disabled";
         case TRADE_RETCODE_LOCKED:            return "Request locked";
         case TRADE_RETCODE_FROZEN:            return "Order/position frozen";
         case TRADE_RETCODE_INVALID_FILL:      return "Invalid fill type";
         default: return "Unknown error (" + IntegerToString(retcode) + ")";
      }
   }

public:
#ifdef __MQL5__
   COrderExecutionManager(CTrade* trade, CProductionLogger* logger)
#else
   COrderExecutionManager(CTradeWrapper* trade, CProductionLogger* logger)
#endif
   {
      m_trade = trade;
      m_logger = logger;
      m_maxRetries = PH_MAX_ORDER_RETRIES;
      m_initialDelayMs = PH_INITIAL_RETRY_DELAY_MS;
      m_maxDelayMs = PH_MAX_RETRY_DELAY_MS;
   }

#ifdef __MQL5__
   bool ExecuteMarketOrder(string symbol, ENUM_ORDER_TYPE orderType, double volume,
                           double sl = 0, double tp = 0, string comment = "")
#else
   bool ExecuteMarketOrder(string symbol, int orderType, double volume,
                           double sl = 0, double tp = 0, string comment = "")
#endif
   {
      if(!ValidateOrderParameters(symbol, volume, sl, tp, orderType))
      {
         m_logger.Error("Order parameter validation failed for " + symbol);
         return false;
      }

      bool success = false;
      uint lastError = 0;
      string lastErrorDesc = "";

      for(int attempt = 0; attempt < m_maxRetries; attempt++)
      {
         if(IsStopped())
            break;

         if(!IsTradeAllowed(symbol))
         {
            m_logger.Warning("Trade not allowed for " + symbol + " - waiting...");
            Sleep(CalculateBackoffDelay(attempt));
            continue;
         }

         success = m_trade.PositionOpen(symbol, orderType, volume, 0, sl, tp, comment);

         if(success)
         {
            m_logger.Trade(orderType == ORDER_TYPE_BUY ? "BUY" : "SELL",
                          symbol, volume, m_trade.ResultPrice(), true);
            return true;
         }

         lastError = m_trade.ResultRetcode();
         lastErrorDesc = GetErrorDescription(lastError);

         if(!IsRetryableError(lastError))
         {
            m_logger.Error("Non-retryable error: " + lastErrorDesc + " (" + IntegerToString(lastError) + ")");
            break;
         }

         if(attempt < m_maxRetries - 1)
         {
            int delay = CalculateBackoffDelay(attempt);
            m_logger.Warning("Order failed (" + lastErrorDesc + ") - retry " +
                           IntegerToString(attempt + 1) + "/" + IntegerToString(m_maxRetries) +
                           " in " + IntegerToString(delay) + "ms");
            Sleep(delay);
         }
      }

      m_logger.Trade(orderType == ORDER_TYPE_BUY ? "BUY" : "SELL",
                    symbol, volume, 0, false, lastErrorDesc);
      return false;
   }

#ifdef __MQL5__
   bool ClosePosition(ulong ticket, string symbol = "")
#else
   bool ClosePosition(int ticket, string symbol = "")
#endif
   {
      bool success = false;
      uint lastError = 0;
      string lastErrorDesc = "";

      for(int attempt = 0; attempt < m_maxRetries; attempt++)
      {
         if(IsStopped())
            break;

         success = m_trade.PositionClose(ticket);

         if(success)
         {
            m_logger.Trade("CLOSE", symbol != "" ? symbol : "#" + IntegerToString(ticket),
                          0, m_trade.ResultPrice(), true);
            return true;
         }

         lastError = m_trade.ResultRetcode();
         lastErrorDesc = GetErrorDescription(lastError);

         if(!IsRetryableError(lastError))
         {
            m_logger.Error("Non-retryable close error: " + lastErrorDesc);
            break;
         }

         if(attempt < m_maxRetries - 1)
         {
            int delay = CalculateBackoffDelay(attempt);
            Sleep(delay);
         }
      }

      m_logger.Trade("CLOSE", symbol != "" ? symbol : "#" + IntegerToString(ticket),
                    0, 0, false, lastErrorDesc);
      return false;
   }

#ifdef __MQL5__
   bool ValidateOrderParameters(string symbol, double volume, double sl, double tp, ENUM_ORDER_TYPE orderType)
#else
   bool ValidateOrderParameters(string symbol, double volume, double sl, double tp, int orderType)
#endif
   {
      // Check volume using platform-specific functions
#ifdef __MQL5__
      CSymbolInfo symInfo;
      if(!symInfo.Name(symbol))
      {
         m_logger.Error("Cannot select symbol: " + symbol);
         return false;
      }

      symInfo.RefreshRates();
      double minLot = symInfo.LotsMin();
      double maxLot = symInfo.LotsMax();
      double lotStep = symInfo.LotsStep();
      double point = symInfo.Point();
      double price = (orderType == ORDER_TYPE_BUY) ? symInfo.Ask() : symInfo.Bid();
      int digits = symInfo.Digits();
#else
      // MT4 native functions
      double minLot = MarketInfo(symbol, MODE_MINLOT);
      double maxLot = MarketInfo(symbol, MODE_MAXLOT);
      double lotStep = MarketInfo(symbol, MODE_LOTSTEP);
      double point = MarketInfo(symbol, MODE_POINT);
      int digits = (int)MarketInfo(symbol, MODE_DIGITS);

      double price, ask, bid;
      ask = MarketInfo(symbol, MODE_ASK);
      bid = MarketInfo(symbol, MODE_BID);
      price = (orderType == ORDER_TYPE_BUY) ? ask : bid;

      RefreshRates();
#endif

      if(volume < minLot)
      {
         m_logger.Error("Volume " + DoubleToString(volume, 2) + " below minimum " + DoubleToString(minLot, 2));
         return false;
      }

      if(volume > maxLot)
      {
         m_logger.Error("Volume " + DoubleToString(volume, 2) + " above maximum " + DoubleToString(maxLot, 2));
         return false;
      }

      if(lotStep > 0)
      {
         double steps = volume / lotStep;
         if(MathAbs(MathRound(steps) - steps) > PH_VOLUME_STEP_TOLERANCE)
         {
            m_logger.Error("Volume " + DoubleToString(volume, 2) + " not multiple of step " + DoubleToString(lotStep, 2));
            return false;
         }
      }

      if(price <= 0)
      {
         m_logger.Error("Invalid price for " + symbol);
         return false;
      }

#ifdef __MQL5__
      long stopsLevel = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
#else
      int stopsLevel = (int)MarketInfo(symbol, MODE_STOPLEVEL);
#endif

       double minDistance = stopsLevel * point;
       bool isBuy = (orderType == ORDER_TYPE_BUY);

       if(sl > 0)
       {
          double slDistance = MathAbs(price - sl);
          if(slDistance < minDistance && minDistance > 0)
          {
             m_logger.Warning("SL too close to price. Distance: " + DoubleToString(slDistance, 5) +
                            ", Minimum: " + DoubleToString(minDistance, 5));
          }

          if(isBuy && sl >= price)
         {
            m_logger.Error("Invalid SL for BUY: " + DoubleToString(sl, 5) + " >= " + DoubleToString(price, 5));
            return false;
         }
         if(!isBuy && sl <= price)
         {
            m_logger.Error("Invalid SL for SELL: " + DoubleToString(sl, 5) + " <= " + DoubleToString(price, 5));
            return false;
         }
      }

      if(tp > 0)
      {
         double tpDistance = MathAbs(price - tp);
         if(tpDistance < minDistance && minDistance > 0)
         {
            m_logger.Warning("TP too close to price. Distance: " + DoubleToString(tpDistance, 5) +
                           ", Minimum: " + DoubleToString(minDistance, 5));
         }

          if(isBuy && tp <= price)
         {
            m_logger.Error("Invalid TP for BUY: " + DoubleToString(tp, 5) + " <= " + DoubleToString(price, 5));
            return false;
         }
         if(!isBuy && tp >= price)
         {
            m_logger.Error("Invalid TP for SELL: " + DoubleToString(tp, 5) + " >= " + DoubleToString(price, 5));
            return false;
         }
      }

      return true;
   }

   bool IsTradeAllowed(string symbol)
   {
#ifdef __MQL5__
      if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
         return false;

      if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
         return false;

      ENUM_SYMBOL_TRADE_MODE tradeMode = (ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
      if(tradeMode == SYMBOL_TRADE_MODE_DISABLED)
         return false;

      if(!TerminalInfoInteger(TERMINAL_CONNECTED))
         return false;
#else
      if(!IsConnected())
         return false;

      if(!IsTradeAllowed())
         return false;
#endif

      return true;
   }
};
#endif

//+------------------------------------------------------------------+
//| Limit Order Price Validator                                       |
//+------------------------------------------------------------------+
class CLimitOrderValidator
{
private:
   CProductionLogger* m_logger;

public:
   CLimitOrderValidator(CProductionLogger* logger)
   {
      m_logger = logger;
   }

#ifdef __MQL5__
   bool ValidateLimitPrice(string symbol, double limitPrice, ENUM_ORDER_TYPE orderType)
#else
   bool ValidateLimitPrice(string symbol, double limitPrice, int orderType)
#endif
   {
#ifdef __MQL5__
      CSymbolInfo symInfo;
      if(!symInfo.Name(symbol))
      {
         m_logger.Error("Cannot select symbol for limit validation: " + symbol);
         return false;
      }

      symInfo.RefreshRates();

      double bid = symInfo.Bid();
      double ask = symInfo.Ask();
      long stopsLevel = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      double point = symInfo.Point();
      int digits = symInfo.Digits();
#else
      double bid = MarketInfo(symbol, MODE_BID);
      double ask = MarketInfo(symbol, MODE_ASK);
      int stopsLevel = (int)MarketInfo(symbol, MODE_STOPLEVEL);
      double point = MarketInfo(symbol, MODE_POINT);
      int digits = (int)MarketInfo(symbol, MODE_DIGITS);

      RefreshRates();
#endif

      double minDistance = stopsLevel * point;

      if(bid <= 0 || ask <= 0)
      {
         m_logger.Error("Invalid bid/ask for " + symbol + " during limit validation");
         return false;
      }

      double normalizedPrice = NormalizeDouble(limitPrice, digits);
      if(MathAbs(normalizedPrice - limitPrice) > 0.000001)
      {
         m_logger.Error("Limit price not properly normalized for " + symbol +
                       ": " + DoubleToString(limitPrice, digits));
         return false;
      }

      if(stopsLevel > 0)
      {
         if(orderType == ORDER_TYPE_BUY_LIMIT)
         {
            if(limitPrice >= ask)
            {
               m_logger.Error("Buy limit price " + DoubleToString(limitPrice, digits) +
                             " >= ask " + DoubleToString(ask, digits) + " for " + symbol);
               return false;
            }

            double distance = ask - limitPrice;
            if(distance < minDistance)
            {
               m_logger.Warning("Buy limit price too close to ask for " + symbol +
                               ". Distance: " + DoubleToString(distance, digits) +
                               ", Minimum: " + DoubleToString(minDistance, digits));
            }
         }
         else if(orderType == ORDER_TYPE_SELL_LIMIT)
         {
            if(limitPrice <= bid)
            {
               m_logger.Error("Sell limit price " + DoubleToString(limitPrice, digits) +
                             " <= bid " + DoubleToString(bid, digits) + " for " + symbol);
               return false;
            }

            double distance = limitPrice - bid;
            if(distance < minDistance)
            {
               m_logger.Warning("Sell limit price too close to bid for " + symbol +
                               ". Distance: " + DoubleToString(distance, digits) +
                               ", Minimum: " + DoubleToString(minDistance, digits));
            }
         }
      }

      if(!IsOrderTypeAllowed(symbol, orderType))
      {
#ifdef __MQL5__
         m_logger.Error("Order type " + EnumToString(orderType) +
                        " not allowed for symbol " + symbol);
#else
         m_logger.Error("Order type not allowed for symbol " + symbol);
#endif
         return false;
      }

      if(!IsSymbolTradeable(symbol))
      {
         m_logger.Error("Symbol " + symbol + " is not tradeable");
         return false;
      }

      return true;
   }

#ifdef __MQL5__
   bool IsOrderTypeAllowed(string symbol, ENUM_ORDER_TYPE orderType)
#else
   bool IsOrderTypeAllowed(string symbol, int orderType)
#endif
   {
#ifdef __MQL5__
      long tradeMode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);

      if(tradeMode == SYMBOL_TRADE_MODE_DISABLED)
      {
         m_logger.Error("Trading disabled for symbol " + symbol);
         return false;
      }

      long tradeExecutionMode = SymbolInfoInteger(symbol, SYMBOL_TRADE_EXEMODE);

      if(orderType == ORDER_TYPE_BUY_LIMIT || orderType == ORDER_TYPE_SELL_LIMIT)
      {
         if(tradeExecutionMode == SYMBOL_TRADE_EXECUTION_INSTANT)
         {
            m_logger.Warning("Symbol " + symbol + " uses INSTANT execution - limit orders may not be supported");
         }
      }
#else
      int tradeMode = (int)MarketInfo(symbol, MODE_TRADEALLOWED);

      if(tradeMode == 0)
      {
         m_logger.Error("Trading disabled for symbol " + symbol);
         return false;
      }
#endif

      return true;
   }

   bool IsSymbolTradeable(string symbol)
   {
#ifdef __MQL5__
      if(!SymbolSelect(symbol, true))
      {
         m_logger.Error("Symbol " + symbol + " does not exist");
         return false;
      }

      long tradeMode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
      if(tradeMode == SYMBOL_TRADE_MODE_DISABLED)
      {
         m_logger.Error("Trading disabled for symbol " + symbol);
         return false;
      }

      if(StringFind(symbol, ".sim") >= 0 || StringFind(symbol, "_sim") >= 0)
      {
         m_logger.Info("Simulation symbol detected: " + symbol);

         CSymbolInfo symInfo;
         if(symInfo.Name(symbol))
         {
            symInfo.RefreshRates();
            if(symInfo.Ask() <= 0 || symInfo.Bid() <= 0)
            {
               m_logger.Error("Simulation symbol " + symbol + " has no valid prices");
               return false;
            }
         }
      }
#else
      if(MarketInfo(symbol, MODE_POINT) == 0)
      {
         m_logger.Error("Symbol " + symbol + " does not exist");
         return false;
      }

      RefreshRates();

      double ask = MarketInfo(symbol, MODE_ASK);
      double bid = MarketInfo(symbol, MODE_BID);

      if(ask <= 0 || bid <= 0)
      {
         m_logger.Error("Symbol " + symbol + " has no valid prices");
         return false;
      }

      if(StringFind(symbol, ".sim") >= 0 || StringFind(symbol, "_sim") >= 0)
      {
         m_logger.Info("Simulation symbol detected: " + symbol);
      }
#endif

      return true;
   }

   string GetOrderErrorDescription(uint retcode)
   {
      switch(retcode)
      {
         case 10004: return "Requote";
         case 10006: return "Request rejected";
         case 10007: return "Request canceled by trader";
         case 10010: return "Only partial execution";
         case 10011: return "Trade error";
         case 10012: return "Request timeout";
         case 10013: return "Invalid request";
         case 10014: return "Invalid volume";
         case 10015: return "Invalid price - price may be outside valid range or not properly normalized";
         case 10016: return "Invalid stops";
         case 10017: return "Trade disabled";
         case 10018: return "Market closed";
         case 10019: return "Insufficient funds";
         case 10020: return "Prices changed";
         case 10021: return "No quotes";
         case 10022: return "Invalid expiration";
         case 10023: return "Order state changed";
         case 10024: return "Too many requests";
         case 10025: return "No changes";
         case 10026: return "Autotrading disabled";
         case 10027: return "Autotrading disabled by client";
         case 10028: return "Request blocked";
         case 10029: return "Connection lost";
         case 10030: return "Only for real accounts";
         case 10031: return "Position limit reached";
         case 10032: return "Pending order limit";
         case 10033: return "Volume limit";
         case 10034: return "Position prohibited";
         case 10035: return "Close by opposite";
         case 10036: return "Close order exist";
         case 10037: return "Multi-close error";
         case 10038: return "Close only";
         case 10039: return "Order filling mode";
         case 10040: return "Connection timeout";
         case 10041: return "Too many requests";
         case 10042: return "No money";
         case 10043: return "Too many requests";
         case 10044: return "Custom error - Trading not allowed on this symbol (common for simulation symbols)";
         default: return "Unknown error (" + IntegerToString(retcode) + ")";
      }
   }
};

//+------------------------------------------------------------------+
//| Price Feed Validator                                             |
//+------------------------------------------------------------------+
class CPriceFeedValidator
{
private:
   CProductionLogger* m_logger;
   double   m_maxSpreadPoints;
   int      m_maxPriceAgeSeconds;
   double   m_maxPriceDeviationPct;
   datetime m_lastValidPriceTime;
   double   m_lastValidPrice;
   string   m_lastSymbol;

public:
   CPriceFeedValidator(CProductionLogger* logger)
   {
      m_logger = logger;
      m_maxSpreadPoints = PH_MAX_SPREAD_POINTS;
      m_maxPriceAgeSeconds = PH_MAX_PRICE_AGE_SECONDS;
      m_maxPriceDeviationPct = PH_MAX_PRICE_DEVIATION_PCT;
      m_lastValidPriceTime = 0;
      m_lastValidPrice = 0;
      m_lastSymbol = "";
   }

   void SetValidationParams(double maxSpread, int maxAgeSec, double maxDeviationPct)
   {
      m_maxSpreadPoints = maxSpread;
      m_maxPriceAgeSeconds = maxAgeSec;
      m_maxPriceDeviationPct = maxDeviationPct;
   }

   bool ValidatePriceFeed(string symbol, bool detailedLogging = false)
   {
#ifdef __MQL5__
      CSymbolInfo symInfo;
      if(!symInfo.Name(symbol))
      {
         m_logger.Error("Cannot select symbol for validation: " + symbol);
         return false;
      }

      if(!symInfo.RefreshRates())
      {
         m_logger.Warning("Failed to refresh rates for " + symbol);
         return false;
      }

      double bid = symInfo.Bid();
      double ask = symInfo.Ask();
      double point = symInfo.Point();
      datetime tradeTime = (datetime)symInfo.Time();
#else
      double bid = MarketInfo(symbol, MODE_BID);
      double ask = MarketInfo(symbol, MODE_ASK);
      double point = MarketInfo(symbol, MODE_POINT);
      datetime tradeTime = TimeCurrent();

      RefreshRates();
#endif

      if(bid <= 0 || ask <= 0 || bid > ask)
      {
         m_logger.Error("Invalid bid/ask relationship for " + symbol +
                       ": Bid=" + DoubleToString(bid, 5) + ", Ask=" + DoubleToString(ask, 5));
         return false;
      }

      // V7.04: Zero spread is valid for simulation symbols - warn but don't block
      if(bid == ask)
      {
         m_logger.Warning("Zero spread detected for " + symbol +
                         ": Bid=Ask=" + DoubleToString(bid, 5) + " (common for .sim symbols)");
      }

       datetime currentTime = TimeCurrent();
      int priceAge = (int)(currentTime - tradeTime);
      if(priceAge > m_maxPriceAgeSeconds)
      {
         m_logger.Warning("Stale price for " + symbol + ": " + IntegerToString(priceAge) +
                         " seconds old (max: " + IntegerToString(m_maxPriceAgeSeconds) + ")");
         return false;
      }

      if(m_lastSymbol == symbol && m_lastValidPrice > 0)
      {
         double currentPrice = (bid + ask) / 2;
         double deviation = MathAbs(currentPrice - m_lastValidPrice) / m_lastValidPrice * 100;

         if(deviation > m_maxPriceDeviationPct)
         {
            m_logger.Warning("Price deviation for " + symbol + ": " +
                           DoubleToString(deviation, 2) + "% (max: " +
                           DoubleToString(m_maxPriceDeviationPct, 2) + "%)");
         }
      }

      m_lastValidPrice = (bid + ask) / 2;
      m_lastValidPriceTime = currentTime;
      m_lastSymbol = symbol;

      return true;
   }
};

//+------------------------------------------------------------------+
//| Connection Manager with Reconnection Logic                       |
//+------------------------------------------------------------------+
class CConnectionManager
{
private:
   CProductionLogger* m_logger;
   bool     m_isConnected;
   datetime m_lastConnectionCheck;
   int      m_consecutiveFailures;

public:
   CConnectionManager(CProductionLogger* logger)
   {
      m_logger = logger;
      m_isConnected = false;
      m_lastConnectionCheck = 0;
      m_consecutiveFailures = 0;
   }

   bool CheckConnection()
   {
      datetime now = TimeCurrent();

      if(now - m_lastConnectionCheck < 1)
         return m_isConnected;

      m_lastConnectionCheck = now;

#ifdef __MQL5__
      bool terminalConnected = TerminalInfoInteger(TERMINAL_CONNECTED);
#else
      bool terminalConnected = IsConnected();
#endif

      if(!terminalConnected)
      {
         if(m_isConnected)
         {
            m_logger.Error("Connection lost - Terminal disconnected");
            m_isConnected = false;
         }
         m_consecutiveFailures++;
         return false;
      }

#ifdef __MQL5__
      datetime serverTime = TimeTradeServer();
#else
      datetime serverTime = TimeCurrent();
#endif

      if(serverTime == 0)
      {
         if(m_isConnected)
         {
            m_logger.Warning("Connection unstable - No server time");
         }
         m_consecutiveFailures++;
         return false;
      }

      if(!m_isConnected && m_consecutiveFailures > 0)
      {
         m_logger.Info("Connection restored after " + IntegerToString(m_consecutiveFailures) + " failures");
      }

      m_isConnected = true;
      m_consecutiveFailures = 0;

      return true;
   }

    static bool ValidateServerURL(string url)
    {
       // Validate that URL uses HTTPS to prevent MITM attacks.
       // Users can set any server URL via InpServerURL input.
       return StringFind(url, "https://") == 0;
    }
};

//+------------------------------------------------------------------+
//| Input Validator                                                  |
//+------------------------------------------------------------------+
class CInputValidator
{
private:
   CProductionLogger* m_logger;

public:
   CInputValidator(CProductionLogger* logger)
   {
      m_logger = logger;
   }

   bool ValidateURL(string url, string paramName)
   {
      if(url == "")
      {
         m_logger.Error(paramName + " cannot be empty");
         return false;
      }

      if(!CConnectionManager::ValidateServerURL(url))
      {
         m_logger.Error(paramName + " must use HTTPS: " + url);
         return false;
      }

      return true;
   }

   bool ValidateRange(double value, double min, double max, string paramName, bool inclusive = true)
   {
      bool valid = inclusive ? (value >= min && value <= max) : (value > min && value < max);

      if(!valid)
      {
         m_logger.Error(paramName + "=" + DoubleToString(value, 2) + " out of range [" +
                       DoubleToString(min, 2) + ", " + DoubleToString(max, 2) + "]");
         return false;
      }

      return true;
   }

   bool ValidateRangeInt(int value, int min, int max, string paramName)
   {
      if(value < min || value > max)
      {
         m_logger.Error(paramName + "=" + IntegerToString(value) + " out of range [" +
                       IntegerToString(min) + ", " + IntegerToString(max) + "]");
         return false;
      }

      return true;
   }

   bool ValidateTimeString(string timeStr, string paramName)
   {
      if(timeStr == "")
         return true;

#ifdef __MQL5__
      string parts[];
      StringSplit(timeStr, ':', parts);
#else
      string parts[];
      int sepPos = StringFind(timeStr, ':');
      if(sepPos > 0)
      {
         ArrayResize(parts, 2);
         parts[0] = StringSubstr(timeStr, 0, sepPos);
         parts[1] = StringSubstr(timeStr, sepPos + 1);
      }
      else
      {
         ArrayResize(parts, 0);
      }
#endif

      if(ArraySize(parts) != 2)
      {
         m_logger.Error(paramName + " has invalid format: " + timeStr + " (expected HH:MM)");
         return false;
      }

      int hour = (int)StringToInteger(parts[0]);
      int minute = (int)StringToInteger(parts[1]);

      if(hour < 0 || hour > 23 || minute < 0 || minute > 59)
      {
         m_logger.Error(paramName + " has invalid time values: " + timeStr);
         return false;
      }

      return true;
   }

   bool ValidateLicenseID(string licenseId)
   {
      if(licenseId == "")
      {
         m_logger.Error("License ID is required");
         return false;
      }

      if(StringLen(licenseId) < PH_MIN_LICENSE_LENGTH)
      {
         m_logger.Error("License ID too short (min " + IntegerToString(PH_MIN_LICENSE_LENGTH) + " characters)");
         return false;
      }

      return true;
   }
};

//+------------------------------------------------------------------+
//| Memory Monitor                                                   |
//+------------------------------------------------------------------+
class CMemoryMonitor
{
private:
   CProductionLogger* m_logger;
   datetime m_lastCheck;
   long     m_initialMemory;
   long     m_peakMemory;
   int      m_checkInterval;

public:
   CMemoryMonitor(CProductionLogger* logger)
   {
      m_logger = logger;
      m_lastCheck = 0;
      m_initialMemory = GetUsedMemory();
      m_peakMemory = m_initialMemory;
      m_checkInterval = PH_MEMORY_CHECK_INTERVAL;
   }

   static long GetUsedMemory()
   {
#ifdef __MQL5__
      long mem = (long)TerminalInfoInteger(TERMINAL_MEMORY_USED);
      return mem > 0 ? mem : 0;
#else
      // MT4 doesn't expose memory usage
      return 0;
#endif
   }

   void CheckMemory(bool forceCheck = false)
   {
      datetime now = TimeCurrent();

      if(!forceCheck && now - m_lastCheck < m_checkInterval)
         return;

      m_lastCheck = now;

      long currentMemory = GetUsedMemory();

      if(currentMemory > m_peakMemory)
      {
         m_peakMemory = currentMemory;
      }

      long memoryGrowth = currentMemory - m_initialMemory;
      if(memoryGrowth > PH_MEMORY_GROWTH_THRESHOLD)
      {
         m_logger.Warning("Memory growth detected: " +
                         DoubleToString(memoryGrowth / (1024.0 * 1024.0), 1) + " MB");
      }
   }

   void LogStatistics()
   {
      long currentMemory = GetUsedMemory();
      long memoryChange = currentMemory - m_initialMemory;

      m_logger.Info("Memory Statistics - Initial: " +
                   DoubleToString(m_initialMemory / (1024.0 * 1024.0), 1) + " MB, " +
                   "Current: " + DoubleToString(currentMemory / (1024.0 * 1024.0), 1) + " MB, " +
                   "Peak: " + DoubleToString(m_peakMemory / (1024.0 * 1024.0), 1) + " MB, " +
                   "Change: " + DoubleToString(memoryChange / (1024.0 * 1024.0), 1) + " MB");
   }
};

//+------------------------------------------------------------------+
//| Market Open Status Check (standalone helper for signal queue)    |
//+------------------------------------------------------------------+
bool IsMarketOpenForSymbol(const string symbol)
{
   // Empty/invalid symbol: return true (don't block) — prevents infinite queue for
   // commands like CLOSEALL that may not carry a real symbol
   if(StringLen(symbol) == 0)
      return true;

#ifdef __MQL5__
   // First check: symbol must be tradeable in general
   ENUM_SYMBOL_TRADE_MODE tradeMode = (ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   if(tradeMode != SYMBOL_TRADE_MODE_FULL)
      return false;

   // Second check: must be within an active trading session
   // This prevents queue drain during pre-market/after-hours for stocks
   datetime currentTime = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(currentTime, dt);
   int currentMinutes = dt.hour * 60 + dt.min;

   // Check if current time is within any trading session for today
   datetime sessionStart, sessionEnd;
   for(int session = 0; session < PH_MAX_TRADING_SESSIONS; session++)  // Most symbols have max 3 sessions
   {
      if(SymbolInfoSessionTrade(symbol, (ENUM_DAY_OF_WEEK)dt.day_of_week, session, sessionStart, sessionEnd))
      {
         MqlDateTime startDt, endDt;
         TimeToStruct(sessionStart, startDt);
         TimeToStruct(sessionEnd, endDt);

         int startMinutes = startDt.hour * 60 + startDt.min;
         int endMinutes   = endDt.hour * 60 + endDt.min;

         if(endMinutes >= startMinutes)
         {
            // Normal intra-day session (e.g. 09:00–17:00)
            if(currentMinutes >= startMinutes && currentMinutes <= endMinutes)
               return true;
         }
         else
         {
            // Overnight session crossing midnight (e.g. 23:00–01:00)
            if(currentMinutes >= startMinutes || currentMinutes <= endMinutes)
               return true;
         }
      }
   }

   // No active session found via SymbolInfoSessionTrade.
   // Many brokers don't report sessions for crypto/CFD symbols (e.g. BTCUSD trades 24/7).
   // Fallback: if trade mode is FULL and we have valid bid/ask, treat as open.
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   return (bid > 0 && ask > 0 && ask > bid);
#else
   // MT4: Check if trading is allowed for the symbol
   if(MarketInfo(symbol, MODE_TRADEALLOWED) <= 0)
      return false;

   // MT4 doesn't have SymbolInfoSessionTrade, so we do a simpler heuristic:
   // Check if we can get valid quotes (bid/ask both > 0)
   RefreshRates();
   double bid = MarketInfo(symbol, MODE_BID);
   double ask = MarketInfo(symbol, MODE_ASK);

   // If we have valid quotes and trading is allowed, assume market is open
   // This is less precise than MT5 but better than the original check
   return (bid > 0 && ask > 0 && ask > bid);
#endif
}

//+------------------------------------------------------------------+
#endif // ProductionHardening_mqh
//+------------------------------------------------------------------+
