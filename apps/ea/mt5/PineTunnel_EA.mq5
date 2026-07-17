#property copyright "Fractalyst"
#define PT_VERSION "1.1.5"
#define PT_VERSION_DESC "PineTunnel v" PT_VERSION " - github.com/TheFractalyst/PineTunnel"

#property link      "github.com/TheFractalyst/PineTunnel"
#property version   PT_VERSION
#property strict
#property description PT_VERSION_DESC
#include <Trade\Trade.mqh>
#include <Trade\OrderInfo.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\SymbolInfo.mqh>
#include <PendingOrders.mqh>
#include <PartialClose.mqh>
#include <OrderModifications.mqh>
#include <CombinedActions.mqh>
#include <ProductionHardening.mqh>   // Phase 4: Production hardening utilities
#include <PTWebSocketClient.mqh>     // Phase 2: WebSocket real-time delivery
// Volume Type for risk calculation
enum ENUM_VOLUME_TYPE
{
   VOLUME_LOTS = 0,                          // Lots
   VOLUME_DOLLAR_AMOUNT = 1,                 // Dollar Amount
   VOLUME_PERCENTAGE_BALANCE_LOTS = 2,       // Percentage of Balance, Lots
   VOLUME_PERCENTAGE_BALANCE_MARGIN = 3,     // Percentage of Balance, Margin
   VOLUME_PERCENTAGE_BALANCE_LOSS = 4,       // Percentage of Balance, Loss
   VOLUME_PERCENTAGE_EQUITY_LOSS = 5,        // Percentage of Equity, Loss
   VOLUME_PERCENTAGE_EQUITY_MARGIN = 6       // Percentage of Equity, Margin
};
enum ENUM_ORDER_DELETE_RESOLUTION
{
   ORDER_DELETE_RESOLUTION_UNCERTAIN = 0,
   ORDER_DELETE_RESOLUTION_CONFIRMED = 1,
   ORDER_DELETE_RESOLUTION_FILLED = 2
};
//===================================================================
// CODE QUALITY CONSTANTS
//===================================================================
// Price validation constants
// No MAX_VALID_PRICE - any positive finite price is valid (BTC > $100K already)

// Watchdog and recovery constants
#define WATCHDOG_COOLDOWN_SEC         30      // Cooldown period between watchdog actions

// Historical analysis constants
#define HISTORICAL_SPREAD_BARS        20000   // Number of bars to analyze for historical spread

// Signal tracking constants
#define MAX_EXECUTED_SIGNALS          500     // Maximum number of executed signals to track
#define NM_DELETE_CONFIRM_TIMEOUT_MS  500

// Layer-4 duplicate prevention constants
#define SIGNAL_ID_PREFIX_LENGTH       8       // Number of hex chars for signal_id prefix
#define BROKER_COMMENT_MAX_LENGTH     31      // MT5 broker comment max length

// Cancel-confirmation constants
#define CANCEL_CONFIRM_TIMEOUT_MS     1500    // Timeout for order cancel confirmation (ms)
#define CANCEL_POLL_INTERVAL_MS       50      // Poll interval for cancel confirmation (ms)
#define NM_POLL_INTERVAL_MS           10      // Poll interval for NM fill detection (ms)
#define HISTORY_SEARCH_WINDOW_SEC     120     // History search window for fill detection (sec)

// Execution quality logging constants
#define EXEC_LOG_FILE_PREFIX  "PineTunnel_exec_"

// State persistence constants
#define TICKET_SIGNAL_MAP_SIZE     256     // Max entries in ticket-signal map

// Volume rounding
#define LOT_ROUNDING_EPSILON       0.0000001  // Epsilon for floor-based lot rounding

//+------------------------------------------------------------------+
//| Version comparison (handles multi-dot versions like "1.0.2")     |
//| Returns: 1 if v1 > v2, -1 if v1 < v2, 0 if equal                 |
//+------------------------------------------------------------------+
int CompareVersions(string v1, string v2)
{
   string parts1[], parts2[];
   int n1 = StringSplit(v1, '.', parts1);
   int n2 = StringSplit(v2, '.', parts2);
   int maxParts = (int)MathMax(n1, n2);
   for(int i = 0; i < maxParts; i++)
   {
      int p1 = (i < n1) ? (int)StringToInteger(parts1[i]) : 0;
      int p2 = (i < n2) ? (int)StringToInteger(parts2[i]) : 0;
      if(p1 > p2) return 1;
      if(p1 < p2) return -1;
   }
   return 0;
}

//+------------------------------------------------------------------+
//| Position Sizer Class - Calculates lot size based on risk %      |
//+------------------------------------------------------------------+
class CPositionSizer
{
private:
   double m_commission;        // Commission per lot (one-way)
   bool   m_round_down;        // Round down position size

public:
   CPositionSizer(double commission = 0.0, bool round_down = true)
   {
      m_commission = commission;
      m_round_down = round_down;
   }

   //+------------------------------------------------------------------+
   //| Calculate volume based on selected Volume Type                  |
   //| This is the main entry point for all volume calculations        |
   //+------------------------------------------------------------------+
   double CalculateVolume(
      ENUM_VOLUME_TYPE volume_type,    // Volume calculation type
      string symbol,                    // Symbol to trade
      double risk_value,                // The risk= parameter value
      double entry_price,               // Entry price
      double stop_loss_price = 0,       // Stop loss price (required for some modes)
      ENUM_ORDER_TYPE order_type = ORDER_TYPE_BUY  // Order type for margin calculation
   )
   {
      switch(volume_type)
      {

         case VOLUME_LOTS:
            // Direct lot size
            return CalculateDirectLots(symbol, risk_value);

         case VOLUME_DOLLAR_AMOUNT:
            // Fixed dollar amount to risk
            return CalculateDollarAmount(symbol, risk_value, entry_price, stop_loss_price);

         case VOLUME_PERCENTAGE_BALANCE_LOTS:
            // Percentage of balance as lots
            return CalculatePercentageBalanceLots(symbol, risk_value);

         case VOLUME_PERCENTAGE_BALANCE_MARGIN:
            // Percentage of balance as margin
            return CalculatePercentageBalanceMargin(symbol, risk_value, order_type, entry_price);

         case VOLUME_PERCENTAGE_BALANCE_LOSS:
            // Percentage of balance to risk if SL hit
            return CalculatePercentageBalanceLoss(symbol, risk_value, entry_price, stop_loss_price);

         case VOLUME_PERCENTAGE_EQUITY_LOSS:
            // Percentage of equity to risk if SL hit
            return CalculatePercentageEquityLoss(symbol, risk_value, entry_price, stop_loss_price);

         case VOLUME_PERCENTAGE_EQUITY_MARGIN:
            // Percentage of equity as notional value
            return CalculatePercentageEquityMargin(symbol, risk_value, order_type, entry_price);

         default:
            Print("[PositionSizer] ERROR: Unknown volume type: ", volume_type);
            return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      }
   }

   //+------------------------------------------------------------------+
   //| Mode 1: LOTS - Direct lot size specification                    |
   //| Example: risk=1 means 1 standard lot                           |
   //+------------------------------------------------------------------+
   double CalculateDirectLots(string symbol, double lots)
   {
      // This is the simplest mode - risk= directly specifies lots
      double normalized = NormalizeLotSize(symbol, lots);
      return normalized;
   }

   //+------------------------------------------------------------------+

   //| Mode 2: DOLLAR_AMOUNT - Fixed dollar amount to risk             |

   //| Example: risk=10 means risk $10 if SL is hit                   |

   //| REQUIRES: stop_loss_price                                       |

   //+------------------------------------------------------------------+
   double CalculateDollarAmount(string symbol, double dollar_amount, double entry_price, double stop_loss_price)
   {
      if(stop_loss_price <= 0)
      {
         Print("[PositionSizer] ERROR: DOLLAR_AMOUNT mode requires stop loss price");
         return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      }

      CSymbolInfo symbol_info;
      if(!symbol_info.Name(symbol))
      {
         Print("[PositionSizer] ERROR: Failed to get symbol info for ", symbol);
         return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      }

      double sl_distance = MathAbs(entry_price - stop_loss_price);
      double tick_size = symbol_info.TickSize();
      double tick_value = symbol_info.TickValue();

      if(tick_size <= 0 || tick_value <= 0 || sl_distance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid parameters for calculation");
         return symbol_info.LotsMin();
      }

      // Calculate lots needed to risk the specified dollar amount
      // Formula: Lots = Risk_Money / (SL_Distance * TickValue / TickSize + 2*Commission)
      double lots = dollar_amount / (sl_distance * tick_value / tick_size + 2 * m_commission);

      lots = NormalizeLotSize(symbol, lots);

      return lots;
   }

   //+------------------------------------------------------------------+
   //| Mode 3: PERCENTAGE_BALANCE_LOTS - % of balance as lots          |
   //| Example: risk=1 on $10K = 1 lot, on $1K = 0.1 lot             |
   //+------------------------------------------------------------------+
   double CalculatePercentageBalanceLots(string symbol, double percent)
   {
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      if(balance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid balance for PCT_BAL_LOTS");
         return 0.0;
      }

      // PineTunnel logic: lots = (balance * percent / 100) / 10000
      // This means 1% of $10,000 = 1 lot
      double lots = (balance * percent / 100.0) / 10000.0;

      lots = NormalizeLotSize(symbol, lots);

      return lots;
   }

   //+------------------------------------------------------------------+
   //| Mode 4: PERCENTAGE_BALANCE_MARGIN - % of balance as margin      |
   //| Example: risk=1 means use 1% of balance as margin              |
   //+------------------------------------------------------------------+
   double CalculatePercentageBalanceMargin(string symbol, double percent, ENUM_ORDER_TYPE order_type, double entry_price = 0)
   {
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      if(balance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid balance for PCT_BAL_MARGIN");
         return 0.0;
      }
      double margin_to_use = balance * (percent / 100.0);

      // Use entry_price for pending orders, fall back to current market price for market orders
      double price;
      if(entry_price > 0)
         price = entry_price;
      else
         price = (order_type == ORDER_TYPE_BUY || order_type == ORDER_TYPE_BUY_LIMIT || order_type == ORDER_TYPE_BUY_STOP) ?
                 SymbolInfoDouble(symbol, SYMBOL_ASK) : SymbolInfoDouble(symbol, SYMBOL_BID);

      // Get margin required for 1 lot at entry price (pending) or current price (market)
      double margin_required;
      if(!OrderCalcMargin(order_type, symbol, 1.0, price, margin_required))
      {
         Print("[PositionSizer] ERROR: Failed to calculate margin for ", symbol);
         return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      }

      if(margin_required <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid margin requirement");
         return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      }

      // Calculate lots based on margin allocation
      double lots = margin_to_use / margin_required;

      lots = NormalizeLotSize(symbol, lots);

      return lots;
   }

   //+------------------------------------------------------------------+
   //| Mode 5: PERCENTAGE_BALANCE_LOSS - % of balance to risk         |
   //| Example: risk=1 means risk 1% of balance if SL hit             |
   //| REQUIRES: stop_loss_price                                       |
   //+------------------------------------------------------------------+
   double CalculatePercentageBalanceLoss(string symbol, double percent, double entry_price, double stop_loss_price)
   {
      if(stop_loss_price <= 0)
      {
         Print("[PositionSizer] ERROR: PERCENTAGE_BALANCE_LOSS mode requires stop loss price");
         return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      }

      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      double risk_money = balance * (percent / 100.0);

      // Use existing CalculatePositionSize method which implements this logic
      return CalculatePositionSize(symbol, entry_price, stop_loss_price, percent, balance);
   }

   //+------------------------------------------------------------------+
   //| Mode 6: PERCENTAGE_EQUITY_LOSS - % of equity to risk           |
   //| Example: risk=1 means risk 1% of equity if SL hit              |
   //| REQUIRES: stop_loss_price                                       |
   //+------------------------------------------------------------------+
   double CalculatePercentageEquityLoss(string symbol, double percent, double entry_price, double stop_loss_price)
   {
      if(stop_loss_price <= 0)
      {
         Print("[PositionSizer] ERROR: PERCENTAGE_EQUITY_LOSS mode requires stop loss price");
         return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      }
      
      double equity = AccountInfoDouble(ACCOUNT_EQUITY);
      
      // Use existing CalculatePositionSize method but with equity instead of balance
      return CalculatePositionSize(symbol, entry_price, stop_loss_price, percent, equity);
   }

    //+------------------------------------------------------------------+
    //| Mode 7: PERCENTAGE_EQUITY_MARGIN - % of equity as notional      |
    //| Formula: lots = (equity * pct/100) / (price * contractSize)     |
    //+------------------------------------------------------------------+
    double CalculatePercentageEquityMargin(string symbol, double percent, ENUM_ORDER_TYPE order_type, double entry_price = 0)
    {
       double equity = AccountInfoDouble(ACCOUNT_EQUITY);
       if(equity <= 0)
       {
          Print("[PositionSizer] ERROR: PERCENTAGE_EQUITY_MARGIN - invalid equity");
          return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
       }

       double pct = percent / 100.0;

       double price;
       if(entry_price > 0)
          price = entry_price;
       else
          price = (order_type == ORDER_TYPE_BUY || order_type == ORDER_TYPE_BUY_LIMIT || order_type == ORDER_TYPE_BUY_STOP) ?
                  SymbolInfoDouble(symbol, SYMBOL_ASK) : SymbolInfoDouble(symbol, SYMBOL_BID);

       double contract_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);

       if(price <= 0 || contract_size <= 0)
       {
          PrintFormat("[PositionSizer] ERROR: pct_eq_margin - invalid price (%.5f) or contract size (%.2f)", price, contract_size);
          return SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
       }

       double lots = (equity * pct) / (price * contract_size);

       PrintFormat("[PineTunnel] pct_eq_margin: equity=%.2f, pct=%.2f%%, price=%.5f, contractSize=%.2f, raw_lots=%.6f",
                   equity, percent, price, contract_size, lots);

       lots = NormalizeLotSize(symbol, lots);

       return lots;
    }

   //+------------------------------------------------------------------+
   //| Helper: Normalize lot size to broker requirements               |
   //+------------------------------------------------------------------+
   double NormalizeLotSize(string symbol, double lots)
   {
      double min_lot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      double max_lot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
      double lot_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

      // Safety: ensure positive value
      if(lots <= 0)
      {
         PrintFormat("[PositionSizer] REJECT: Invalid lot size %.4f - aborting trade", lots);
         return 0.0;
      }

      // Apply max constraint (min checked after rounding below)
      // Only clamp if broker reports a valid max; if 0, skip the cap
      if(max_lot > 0 && lots > max_lot) lots = max_lot;

      // Round to broker's lot step
      if(lot_step > 0)
      {
         if(m_round_down)
            lots = MathFloor(lots / lot_step + LOT_ROUNDING_EPSILON) * lot_step;
         else
            lots = MathRound(lots / lot_step) * lot_step;
      }

      // Final safety check after rounding
      if(lots < min_lot)
      {
         PrintFormat("[PositionSizer] REJECT: volume %.4f < broker min %.2f", lots, min_lot);
         return 0.0;
      }

      int vol_digits = 2;
      if(lot_step > 0)
      {
         vol_digits = (int)MathCeil(-MathLog10(lot_step));
         if(vol_digits < 0) vol_digits = 0;
         if(vol_digits > 8) vol_digits = 8;
      }
      return NormalizeDouble(lots, vol_digits);
   }
   //+------------------------------------------------------------------+
   //| Calculate position size based on risk percentage                |
   //| Formula: Lots = RiskMoney / (StopLossDistance * TickValue / TickSize + 2*Commission) |
   //+------------------------------------------------------------------+
   double CalculatePositionSize(
      string symbol,              // Symbol to trade
      double entry_price,         // Entry price
      double stop_loss_price,     // Stop loss price
      double risk_percent,        // Risk as % of balance (e.g. 1.0 = 1%)
      double account_balance = 0  // Account balance (0 = use current)
   )
   {
      // Get account balance
      if(account_balance <= 0)
         account_balance = AccountInfoDouble(ACCOUNT_BALANCE);

      if(account_balance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid account balance");
         return 0.0; // Return zero - abort trade
      }

      // Calculate risk money
      double risk_money = account_balance * (risk_percent / 100.0);

      // Get symbol information
      CSymbolInfo symbol_info;
      if(!symbol_info.Name(symbol))
      {
         Print("[PositionSizer] ERROR: Failed to get symbol info for ", symbol);
         return 0.0;
      }

      // Calculate stop loss distance in price
      double stop_loss_distance = MathAbs(entry_price - stop_loss_price);

      if(stop_loss_distance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid stop loss distance");
         return 0.0;
      }

      // Get symbol parameters
      double tick_size = symbol_info.TickSize();
      double tick_value = symbol_info.TickValue();
      double volume_min = symbol_info.LotsMin();
      double volume_max = symbol_info.LotsMax();
      double volume_step = symbol_info.LotsStep();

      if(tick_size <= 0 || tick_value <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid tick size or tick value");
         return volume_min;
      }

      // Calculate position size using Position Sizer formula
      double position_size = risk_money / (stop_loss_distance * tick_value / tick_size + 2 * m_commission);

      // Round to lot step
      if(volume_step > 0)
      {
         if(m_round_down)
            position_size = MathFloor(position_size / volume_step + LOT_ROUNDING_EPSILON) * volume_step;
         else
            position_size = MathRound(position_size / volume_step) * volume_step;
      }

       // Apply min/max constraints
       if(position_size < volume_min) position_size = volume_min;
       if(position_size > volume_max && volume_max > 0) position_size = volume_max;

       return NormalizeDouble(position_size, 2);
   }
};
//===================================================================
// LICENSE
//===================================================================
input string   InpLicenseID        = "";                         // License ID
//===================================================================
// SYNTAX
//===================================================================
input group "Syntax";
// Target Type for SL/TP parameters
input ENUM_TARGET_TYPE InpTargetType = TARGET_TYPE_PIPS;       // Target Type for SL/TP
input ENUM_VOLUME_TYPE InpVolumeType = VOLUME_LOTS;               // Volume Type
// Pending Order Entry
enum ENUM_PENDING_ENTRY
{
   PENDING_PIPS_FROM_MARKET = 0,      // Pips from Current Market Price
   PENDING_PRICE_FROM_SIGNAL = 1,     // Specified Price from TradingView Alert
   PENDING_PERCENT_FROM_MARKET = 2    // Percentage from Current Market Price
};
input ENUM_PENDING_ENTRY InpPendingOrderEntry = PENDING_PIPS_FROM_MARKET; // Pending Order Entry Type
// Account Filter Type
enum ENUM_ACCOUNT_BASIS
{
   ACCOUNT_BASIS_BALANCE = 0,               // Balance
   ACCOUNT_BASIS_EQUITY = 1,                // Equity
   ACCOUNT_BASIS_FREE_MARGIN = 2,           // Free Margin
   ACCOUNT_BASIS_MARGIN_PERCENTAGE = 3      // Margin Percentage
};
input ENUM_ACCOUNT_BASIS InpAccountFilterBasis = ACCOUNT_BASIS_BALANCE; // Account Filter Type
//===================================================================
// INPUT
//===================================================================
input group "Input";
// Input Setting Mode
enum ENUM_INPUT_SETTING
{
   SETTING_SIGNAL_PARAMS_ONLY = 0,    // Use Signal Values
   SETTING_EA_PARAMS_ONLY = 1,        // Use EA Defaults
   SETTING_SLTP_EA_RISK_SIGNAL = 2,   // EA SL/TP, Signal Volume
   SETTING_SLTP_SIGNAL_RISK_EA = 3    // Signal SL/TP, EA Volume
};
input ENUM_INPUT_SETTING InpSetting = SETTING_SIGNAL_PARAMS_ONLY; // Setting
input double   InpStopLoss         = 0.0;                         // Default Stop Loss
input double   InpTakeProfit       = 0.0;                         // Default Take Profit
input double   InpRisk             = 1.0;                         // Default Risk
//===================================================================
// GENERAL
//===================================================================
input group "General";
// Pyramiding
enum ENUM_PYRAMIDING
{
   PYRAMIDING_ON = 0,                 // On
   PYRAMIDING_ON_IF_PROFIT = 1,       // On - Only If In Profit
   PYRAMIDING_OFF_EITHER_OR = 2,      // Off - One Position Per Symbol
   PYRAMIDING_OFF_BOTH = 3            // Off - One Buy and One Sell (Hedge)
};
input ENUM_PYRAMIDING InpPyramiding = PYRAMIDING_ON;              // Pyramiding
// Close on Reverse
enum ENUM_CLOSE_ON_REVERSE
{
   CLOSE_REVERSE_ON_HEDGING = 0,      // On - Close and Reverse
   CLOSE_REVERSE_ON_NETTING = 1,      // On - Close Only
   CLOSE_REVERSE_OFF = 2              // Off
};
input ENUM_CLOSE_ON_REVERSE InpCloseOnReverse = CLOSE_REVERSE_OFF; // Close on Reverse
// Hidden SL/TP
enum ENUM_HIDDEN_SLTP
{
   HIDDEN_OFF = 0,                    // Off
   HIDDEN_ON = 1                      // On
};
input ENUM_HIDDEN_SLTP InpHiddenSLTP = HIDDEN_OFF;          // Hidden SL/TP
input double   InpHiddenOffset = 100.0;                          // Hidden SL/TP Offset (points)
// Partial Close Percentage
enum ENUM_PARTIAL_CLOSE_PCT
{
   PARTIAL_CLOSE_10 = 10,              // 10%
   PARTIAL_CLOSE_20 = 20,              // 20%
   PARTIAL_CLOSE_25 = 25,              // 25%
   PARTIAL_CLOSE_34 = 34,              // 34%
   PARTIAL_CLOSE_50 = 50               // 50%
};
input ENUM_PARTIAL_CLOSE_PCT InpPartialClosePercentage = PARTIAL_CLOSE_25; // Partial Close Percentage
input int      InpMaxOpenPositions = 0;                           // Maximum Open Positions
input int      InpMaxOpenPositionsPerSymbol = 0;                  // Maximum Open Positions per Symbol
input int      InpMaxUniqueSymbols = 0;                           // Maximum Unique Symbols
// Auto Orders (Near-Market Limit Conversion)
input bool     InpEnableSmartMarket = true;                        // Enable Auto Orders
input int      InpNearMarketPoints = 10;                          // Auto Order Offset (points)
input int      InpEntryLimitTimeoutMs = 300;                      // Entry Limit Timeout (ms)
input bool     InpEnableExitLimit = false;                       // Exit Limit Orders (maker rebate)
input int      InpExitLimitTimeoutMs = 500;                        // Exit Limit Timeout (ms)
//===================================================================
// DASHBOARD
//===================================================================
input group "Dashboard";
input int      InpFontSize         = 10;                          // Font Size (for Dashboard)
input bool     InpShowDashboard    = true;                        // Show Dashboard
//===================================================================
// ACCOUNT PROTECTION
//===================================================================
input group "Account";
input int      InpDailyTimezoneGMT = 0;                          // Daily Profit/Loss Timezone
input double   InpDailyProfit      = 0.0;                        // Daily Profit
input double   InpDailyLoss        = 0.0;                        // Daily Loss
// Action Type for Daily Limits
enum ENUM_ACTION_DAILY
{
   ACTION_DAILY_HALT = 0,             // Halt EA
   ACTION_DAILY_CLOSE = 1,            // Close All Positions
   ACTION_DAILY_CLOSE_HALT = 2        // Close All Positions and Halt EA
};
input ENUM_ACTION_DAILY InpAction1 = ACTION_DAILY_HALT;           // Action (Daily)
input double   InpCumulativeProfit = 0.0;                        // Cumulative Profit
input double   InpCumulativeLoss  = 0.0;                        // Cumulative Loss
// Action Type for Cumulative Limits
enum ENUM_ACTION_CUMULATIVE
{
   ACTION_CUM_HALT_DAY = 0,           // Halt EA (Day)
   ACTION_CUM_HALT_PERSIST = 1,       // Halt EA (Persistent)
   ACTION_CUM_CLOSE = 2,              // Close All Positions
   ACTION_CUM_CLOSE_HALT_DAY = 3,     // Close All Positions and Halt EA (Day)
   ACTION_CUM_CLOSE_HALT_PERSIST = 4  // Close All Positions and Halt EA (Persistent)
};
input ENUM_ACTION_CUMULATIVE InpAction2 = ACTION_CUM_HALT_DAY;    // Action (Cumulative)
//===================================================================
// V7.00 RELIABILITY
//===================================================================
input group "V7.00 Reliability";
input bool     InpEnableSLTPVerify = false;                       // Verify SL/TP After Order (prevents naked positions)
input int      InpSLTPVerifyRetries = 3;                          // SL/TP Verification Retries
//===================================================================
// MISCELLANEOUS
//===================================================================
input group "Miscellaneous";
// EA Magic Number
enum ENUM_MAGIC_NUMBER
{
   MAGIC_1001 = 1001,                     // 1001
   MAGIC_1002 = 1002,                     // 1002
   MAGIC_1003 = 1003,                     // 1003
   MAGIC_1004 = 1004,                     // 1004
   MAGIC_1005 = 1005                      // 1005
};
input ENUM_MAGIC_NUMBER InpMagicNumber = MAGIC_1001;                // EA Magic Number
// Magic Restriction
enum ENUM_MAGIC_RESTRICTION
{
   MAGIC_RESTRICT_ON = 0,             // On
   MAGIC_RESTRICT_OFF = 1             // Off
};
input ENUM_MAGIC_RESTRICTION InpMagicRestriction = MAGIC_RESTRICT_ON; // Magic Restriction
input string   InpStartTime        = "00:00";                     // Start Time
input string   InpEndTime          = "23:59";                     // End Time
input string   InpPrefix           = "";                         // Prefix
input string   InpSuffix           = "";                         // Suffix
input bool     InpEnableSignalQueue = true;                      // Queue signals when market is closed

// Connection mode enum (must be defined before input declaration)
enum ENUM_CONNECTION_MODE
{
   CONNECTION_MODE_WEBSOCKET = 0,  // WebSocket only (WSS)
   CONNECTION_MODE_LONGPOLL  = 1,  // Long-poll only (HTTPS)
   CONNECTION_MODE_HYBRID    = 2   // WebSocket + HTTP fallback (Hybrid)
};

input group "WebSocket";
input ENUM_CONNECTION_MODE InpConnectionMode = CONNECTION_MODE_HYBRID; // Connection mode
input int      InpWSPollIntervalMs = 100;                       // WebSocket poll interval (ms)
input int      InpWSHeartbeatSec = 30;                           // WebSocket heartbeat interval (sec)
input int      InpWSMaxReconnectAttempts = 0;                    // Max reconnect attempts (0=unlimited)
input int      InpWSLogLevel = 0;                              // DLL log level (0=off,1=error,2=info,3=debug)
input string InpServerURL  = "https://your-server.com";    // Server URL (your PineTunnel instance)
input int      InpStatsInterval    = 60;                         // Account stats interval seconds (0=off)
//===================================================================
// AUTO-UPDATE & AUDIT
//===================================================================
input group "Auto-Update";
input bool     InpAutoUpdate       = true;                        // Auto-Update EA
input bool     InpAutoUpdateDLL    = true;                        // Auto-Update DLL
input bool     InpAutoRestart     = true;                        // Auto-Restart on Update
input int      InpUpdateCheckInterval = 3600;                    // Update check interval (seconds)
input int      InpAuditInterval    = 300;                         // Audit report interval (seconds, 0=off)
// NOTE: Symbol auto-enable hard-coded to ALWAYS ON
// Symbols will be automatically added to Market Watch when signals arrive
// This ensures all trading pairs work without manual configuration
//===================================================================
// INTERNAL SETTINGS (not visible to user)
//===================================================================
int      InpPollInterval    = 250;                                // Fallback polling interval in milliseconds
bool     g_useLongPoll       = true;                              // Long-polling mode (auto-fallback if unsupported)
bool     g_longPollFailed    = false;                             // Set to true if server doesn't support long-polling
datetime g_longPollRetryTime = 0;                                 // Next time to retry longpoll after fallback (0 = no retry pending)
#define  LONGPOLL_RETRY_INTERVAL 10                               // Seconds between longpoll retry attempts
#define  BATCH_ACK_MAX         50                                 // Max signals per batch ACK request
#define  MAX_SIGNALS_PER_TICK  50                                 // Max signals processed per OnTimer tick
#define  WS_MAX_CONNECTING_TICKS  200                             // Max ticks in CONNECTING state before forced disconnect (20s at 100ms)
#define  WS_DEAD_CONNECTION_TIMEOUT 90                             // Seconds without receiving data before forcing reconnect (3 missed pongs)
//--- WebSocket globals
CPTWebSocketClient* g_wsClient = NULL;                             // WebSocket client instance (NULL if unavailable)
bool     g_useWebSocket    = false;                              // True when WS is connected and primary transport
int      g_wsReconnectAttempts = 0;                               // Current WS reconnect attempt count
int      g_wsConnectingTicks = 0;                                // Ticks spent in CONNECTING state (for diagnostics)
datetime g_wsLastReconnectAttempt = 0;                             // Timestamp of last WS reconnect attempt (exponential backoff)
int      g_wsReconnectDelaySec = 1;                                // Current reconnect delay in seconds (doubles each attempt)
datetime g_wsLastStatsSent  = 0;                                  // Last time account stats were sent via WS
datetime g_wsLastHealthSent = 0;                                  // Last time health telemetry was sent via WS
int      g_wsLastPositionCount = -1;                              // Last known position count (-1 = unknown)
bool     g_wsInitialStateSent = false;                            // Whether initial state was sent after WS connect
bool     InpLogSignals      = true;                               // Log parsed signals to Experts tab
double   InpDefaultLots     = 0.01;                               // Default lot size when none supplied
int      InpMaxSlippage     = 10;                                 // Max slippage in points
double   InpCommission      = 0.0;                                // Commission per lot (one-way)
bool     InpRoundDown       = true;                               // Round down position size
string   InpDefaultComment  = "";                                 // Default comment
// Backward compatibility for existing code (variables, not inputs)
double   InpPartialClosePct = 50.0;                               // Partial close percentage (mapped)
int      InpDashFontSize    = 9;                                  // Font size (mapped)
ENUM_PENDING_TYPE InpPendingType = PENDING_PIPS;                  // Pending type (default)
//===================================================================
// INTERNAL ENUMS (for compatibility)
//===================================================================
// NOTE: ENUM_PENDING_TYPE and ENUM_TARGET_TYPE are now defined in include files
// Removed duplicate declarations to avoid conflicts
//===================================================================
// HIDDEN SL/TP SYSTEM
//===================================================================
// Hidden SL/TP prevent stop hunting by keeping SL/TP in EA memory
// instead of sending them to the broker
struct HiddenTarget
{
   ulong    ticket;          // Position ticket
   string   symbol;          // Symbol
   int      type;            // Position type (POSITION_TYPE_BUY or SELL)
   double   hidden_sl;       // Hidden stop loss price
   double   hidden_tp;       // Hidden take profit price
   double   entry_price;     // Entry price for reference
};
#define MAX_HIDDEN_TARGETS 1000
HiddenTarget g_hiddenTargets[MAX_HIDDEN_TARGETS];
int g_hiddenCount = 0;
//===================================================================
// CLASS DECLARATIONS
//===================================================================
CTrade          g_trade;
CPositionInfo   g_position;
CSymbolInfo     g_symbol;
CPositionSizer* g_positionSizer  = NULL;
CPendingOrderManager* g_pendingManager = NULL;
CPartialCloseManager* g_partialManager = NULL;
COrderModificationManager* g_modifyManager = NULL;
CCombinedActionsManager* g_combinedManager = NULL;

// Phase 4: Production hardening class instances
CProductionLogger*       g_prodLogger = NULL;
COrderExecutionManager*  g_orderManager = NULL;
CPriceFeedValidator*     g_priceValidator = NULL;
CLimitOrderValidator*    g_limitOrderValidator = NULL;
CConnectionManager*      g_connectionManager = NULL;
CInputValidator*         g_inputValidator = NULL;
CMemoryMonitor*          g_memoryMonitor = NULL;

bool g_eaEnabled = true;  // EA on/off state
datetime   g_lastPoll         = 0;
datetime   g_connectedSince   = 0;
datetime   g_lastSuccessfulRequest = 0;  // Track last successful HTTP request
int        g_consecutiveErrors = 0;       // Track consecutive errors for recovery
int        g_watchdogLevel = 0;           // Escalation level (0-3) for progressive recovery
datetime   g_httpBackoffUntil = 0;         // Non-blocking HTTP backoff (replaces Sleep for server errors)
datetime   g_lastWatchdogAction = 0;      // Last time watchdog took action
long       g_totalSignals     = 0;
long       g_successful       = 0;
long       g_failed           = 0;
// Daily tracking (resets at midnight)
long       g_todaySignals     = 0;
long       g_todaySuccessful  = 0;
long       g_todayFailed      = 0;
bool       g_lastPollSuccess  = false;
datetime   g_lastDashboardUpdate = 0; // Track dashboard refresh to prevent flicker
// Spread tracking variables
double     g_totalSpread       = 0;   // Total spread sum for averaging
ulong      g_spreadSamples     = 0;   // Number of spread samples taken
double     g_avgSpread         = 0;   // Average spread in points
// Account Protection tracking variables
datetime   g_dailyResetTime    = 0;    // Last daily reset timestamp
double     g_dailyStartBalance = 0;    // Balance at daily reset
double     g_cumulativeStartBalance = 0; // Balance when EA started
bool       g_protectionHalted  = false; // Persistent halt flag
bool       g_dailyHalted       = false; // Daily halt flag
int        g_lastExecError     = 0;    // last trade operation retcode - read by POST-CHECK
//===================================================================
// UPDATE NOTIFICATION
//===================================================================
string     g_latestVersion     = "";    // Latest version from server
string     g_updateNotes       = "";    // Release notes for latest version
bool       g_updateAvailable   = false; // True if server version > local version
datetime   g_lastVersionCheck  = 0;     // Last time we checked version (throttle to 5 min)
bool       g_eaJustUpdated     = false; // True if EA update was applied this session
bool       g_dllJustUpdated    = false; // True if DLL update was applied this session
#define    VERSION_CHECK_INTERVAL 300   // Check version every 5 minutes (seconds)
//===================================================================
// AUTO-UPDATE STATE
//===================================================================
string     g_updateFilePath    = "";    // Path to downloaded update file (sandbox)
string     g_updateFileVersion = "";    // Version of the downloaded update
bool       g_updateDownloaded  = false; // True when update file is ready to apply
bool       g_dllUpdateDownloaded = false; // True when DLL update file is ready
bool       g_isVps = false;                 // True if running on a VPS
string     g_vpsProvider = "";              // VPS provider name (e.g., "AWS", "Hetzner")
datetime   g_lastAuditSent    = 0;     // Last time audit data was sent to server
datetime   g_eaStartTime      = 0;     // EA start time (set in OnInit)
#define    AUDIT_ENDPOINT      "/api/ea/audit/"
#define    UPDATE_CHECK_ENDPOINT "/api/ea/check/"
#define    UPDATE_DOWNLOAD_ENDPOINT "/api/ea/download/"
#define    DLL_DOWNLOAD_ENDPOINT  "/api/ea/dll/download/"
#define    UPDATE_FILE_PREFIX  "PineTunnel_update_"
//===================================================================
// V7.00: DUPLICATE SIGNAL PREVENTION
//===================================================================
int        g_magicNumber = 0;                        // Global int magic number (converted from enum)
string     g_executedSignals[MAX_EXECUTED_SIGNALS];  // Circular buffer of executed signal IDs
int        g_executedSignalIndex = 0;                // Current index in circular buffer
int        g_executedSignalCount = 0;                // Total signals tracked
string     g_stateFilePath = "";                     // State file path (set in OnInit)
// Ticket-Signal map for close reports
struct TicketSignalEntry { ulong ticket; string signal_id; };
TicketSignalEntry g_ticketSignalMap[TICKET_SIGNAL_MAP_SIZE];
int        g_ticketSignalCount = 0;
//===================================================================
// V7.00: STATE PERSISTENCE FUNCTIONS
//===================================================================
void SaveExecutedSignal(const string &signal_id)
{
   if(signal_id == "") return;

   // Add to circular buffer
   g_executedSignals[g_executedSignalIndex] = signal_id;
   g_executedSignalIndex = (g_executedSignalIndex + 1) % MAX_EXECUTED_SIGNALS;
   if(g_executedSignalCount < MAX_EXECUTED_SIGNALS)
      g_executedSignalCount++;

   // Persist to file for crash recovery
   // V7.01: Use atomic write (temp file + copy) to prevent corruption
   if(g_stateFilePath != "")
   {
      string temp_path = g_stateFilePath + ".tmp";
      int handle = FileOpen(temp_path, FILE_WRITE|FILE_TXT|FILE_ANSI);
      if(handle != INVALID_HANDLE)
      {
         // Write last 100 signals (most recent)
          int count = MathMin(g_executedSignalCount, 100);
          int start = (g_executedSignalCount < 100) ? 0 : (g_executedSignalIndex - count + MAX_EXECUTED_SIGNALS) % MAX_EXECUTED_SIGNALS;
         for(int i = 0; i < count; i++)
         {
            int idx = (start + i) % MAX_EXECUTED_SIGNALS;
            if(g_executedSignals[idx] != "")
               FileWriteString(handle, g_executedSignals[idx] + "\n");
         }
         FileClose(handle);

         // Atomic rename: copy temp to actual file, then delete temp
         // MQL5 doesn't have rename, so we use copy + delete
         if(FileCopy(temp_path, 0, g_stateFilePath, FILE_REWRITE))
         {
            FileDelete(temp_path);
         }
      }
   }
}
bool IsSignalDuplicate(const string &signal_id)
{
   if(signal_id == "") return false;  // No ID = not a duplicate (legacy signals)

   // Check circular buffer
   for(int i = 0; i < g_executedSignalCount; i++)
   {
      if(g_executedSignals[i] == signal_id)
         return true;
   }
   return false;
}
void SaveTicketSignal(ulong ticket, const string &signal_id)
{
   if(signal_id == "" || ticket == 0) return;
   if(g_ticketSignalCount >= TICKET_SIGNAL_MAP_SIZE)
   {
      for(int i = 0; i < TICKET_SIGNAL_MAP_SIZE - 1; i++)
         g_ticketSignalMap[i] = g_ticketSignalMap[i + 1];
      g_ticketSignalCount = TICKET_SIGNAL_MAP_SIZE - 1;
   }
   g_ticketSignalMap[g_ticketSignalCount].ticket = ticket;
   g_ticketSignalMap[g_ticketSignalCount].signal_id = signal_id;
   g_ticketSignalCount++;
}
string FindSignalByTicket(ulong ticket)
{
   for(int i = 0; i < g_ticketSignalCount; i++)
   {
      if(g_ticketSignalMap[i].ticket == ticket)
         return g_ticketSignalMap[i].signal_id;
   }
   return "";
}
void LoadExecutedSignals()
{
   if(g_stateFilePath == "") return;

   if(!FileIsExist(g_stateFilePath)) return;

   int handle = FileOpen(g_stateFilePath, FILE_READ|FILE_TXT|FILE_ANSI);
   if(handle == INVALID_HANDLE) return;

   g_executedSignalCount = 0;
   g_executedSignalIndex = 0;

   while(!FileIsEnding(handle) && g_executedSignalCount < MAX_EXECUTED_SIGNALS)
   {
      string line = FileReadString(handle);
      StringTrimRight(line);
      StringTrimLeft(line);
      if(line != "")
      {
         g_executedSignals[g_executedSignalIndex] = line;
         g_executedSignalIndex = (g_executedSignalIndex + 1) % MAX_EXECUTED_SIGNALS;
         g_executedSignalCount++;
      }
   }
   FileClose(handle);

   PrintFormat("[PineTunnel] Loaded %d executed signals from state file (crash recovery)", g_executedSignalCount);
}
//===================================================================
// AUTO-UPDATE: Download and apply EA/DLL updates from server
//===================================================================

//+------------------------------------------------------------------+
//| Check for updates via lightweight version check endpoint          |
//+------------------------------------------------------------------+
bool CheckForEAUpdate()
{
   if(!InpAutoUpdate) return false;

    // Throttle: check at configured interval
    datetime now = TimeCurrent();
    if(g_lastVersionCheck > 0 && (now - g_lastVersionCheck) < InpUpdateCheckInterval)
       return false;

    string url = InpServerURL + UPDATE_CHECK_ENDPOINT + "mt5";
   string headers = "X-License-Key: " + InpLicenseID + "\r\n";

   uchar post[];  // Empty body for GET request
   uchar result_data[];
   string result_headers;
   int timeout = 5000;

   int res = WebRequest("GET", url, headers, timeout, post, result_data, result_headers);

    if(res == -1)
    {
       PrintFormat("[PineTunnel] Update check failed: WebRequest error %d", GetLastError());
       g_lastVersionCheck = now - InpUpdateCheckInterval + 60;
       return false;
    }

    if(res != 200)
    {
       PrintFormat("[PineTunnel] Update check failed: HTTP %d", res);
       g_lastVersionCheck = now - InpUpdateCheckInterval + 60;
       return false;
    }

    g_lastVersionCheck = now;

    // Parse response
   string result = CharArrayToString(result_data);
   string server_version = ExtractStringValue(result, "latest_version");
   string file_available = ExtractStringValue(result, "file_available");

   if(server_version == "")
   {
      PrintFormat("[PineTunnel] Update check: no version in response");
      return false;
   }

   // Compare versions (proper multi-dot comparison, not StringToDouble)
   if(CompareVersions(server_version, PT_VERSION) > 0)
   {
      if(!g_updateAvailable)
      {
         PrintFormat("[PineTunnel] UPDATE AVAILABLE: v%s -> v%s", PT_VERSION, server_version);
         g_updateNotes = ExtractStringValue(result, "release_notes");
         if(g_updateNotes != "")
            PrintFormat("[PineTunnel] Release notes: %s", g_updateNotes);
      }
      g_latestVersion = server_version;
      g_updateAvailable = true;

      // Download update file if auto-update is enabled and file is available
      if(InpAutoUpdate && file_available == "true" && !g_updateDownloaded)
         DownloadEAUpdate(server_version);
   }
   else
   {
      g_updateAvailable = false;
      PrintFormat("[PineTunnel] Update check: v%s is current", PT_VERSION);
   }

   // -- Check for DLL update --
   string server_dll_version = ExtractStringValue(result, "latest_dll_version");
   string dll_available = ExtractStringValue(result, "dll_available");
   if(server_dll_version != "" && dll_available == "true")
   {
      string current_dll_ver = "";
      if(g_wsClient != NULL && g_wsClient.IsConnected())
         current_dll_ver = g_wsClient.GetDllVersion();
      if(current_dll_ver != "" && CompareVersions(server_dll_version, current_dll_ver) > 0)
      {
         PrintFormat("[PineTunnel] DLL UPDATE AVAILABLE: v%s -> v%s", current_dll_ver, server_dll_version);
         if(InpAutoUpdate && !g_dllUpdateDownloaded)
            DownloadDLLUpdate(server_dll_version);
      }
   }

   // -- Auto-restart if any updates were downloaded --
   // Must be after both EA and DLL downloads complete.
   // Both .ex5 and .dll files are locked while terminal is running,
   // so the batch script waits for terminal exit before swapping either.
   if((g_updateDownloaded || g_dllUpdateDownloaded) && InpAutoRestart)
      TriggerAutoRestart();

   return g_updateAvailable;
}

//+------------------------------------------------------------------+
//| Download EA update from server                                    |
//+------------------------------------------------------------------+
bool DownloadEAUpdate(string target_version)
{
   string url = InpServerURL + UPDATE_DOWNLOAD_ENDPOINT + "mt5";
   string headers = "X-License-Key: " + InpLicenseID + "\r\n";
   string result;

   PrintFormat("[PineTunnel] Downloading EA update v%s...", target_version);
   PrintFormat("[PineTunnel] Download URL: %s", url);

    // -- Try DLL-based download first (can write to Experts folder directly) --
    // Note: DownloadFile uses WinHTTP (standalone), not the WebSocket - so it works
    // whenever the DLL is loaded (g_wsClient != NULL), regardless of WS connection state.
    if(g_wsClient != NULL)
    {
       // Build the target path: MQL5/Experts/PineTunnel_EA.ex5
       string experts_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Experts\\";
       string save_path = experts_dir + "PineTunnel_EA_new.ex5";

       int dlResult = CPTWebSocketClient::DownloadFile(url, headers, save_path, 30000);
       PrintFormat("[PineTunnel] EA DLL download result: %d (url=%s, save=%s)", dlResult, url, save_path);
       if(dlResult == 0)
       {
          g_updateFilePath = save_path;
          g_updateFileVersion = target_version;
          g_updateDownloaded = true;
          PrintFormat("[PineTunnel] EA update v%s downloaded via DLL to: %s", target_version, save_path);
          PrintFormat("[PineTunnel] Restart terminal to apply update v%s", target_version);
          return true;
       }
      PrintFormat("[PineTunnel] DLL download failed (error %d), falling back to WebRequest", dlResult);
   }

   // -- Fallback: WebRequest to MQL5 sandbox --
   uchar post[];
   uchar result_data[];
   string result_headers;
   int timeout = 30000;

   int res = WebRequest("GET", url, headers, timeout, post, result_data, result_headers);

   if(res == -1)
   {
      int err = GetLastError();
      PrintFormat("[PineTunnel] EA update download failed: WebRequest error %d", err);
      return false;
   }

   if(res != 200)
   {
      PrintFormat("[PineTunnel] EA update download failed: HTTP %d", res);
      return false;
   }

   // Parse JSON response
   result = CharArrayToString(result_data);

   // Extract base64 data
   string b64_data = ExtractStringValue(result, "data");
   string file_sha256 = ExtractStringValue(result, "sha256");
    string filename = ExtractStringValue(result, "filename");
    string file_version = ExtractStringValue(result, "version");

    // Sanitize filename - strip path separators (MQL5 sandbox rejects paths with \ or /)
    int lastSep = MathMax(StringFind(filename, "\\"), StringFind(filename, "/"));
    if(lastSep >= 0)
       filename = StringSubstr(filename, lastSep + 1);

    if(b64_data == "" || filename == "")
   {
      PrintFormat("[PineTunnel] EA update download: invalid response (missing data/filename)");
      return false;
   }

   // Decode base64 to binary
   int data_len = StringLen(b64_data);
   if(data_len < 10)
   {
      PrintFormat("[PineTunnel] EA update download: base64 data too short (%d bytes)", data_len);
      return false;
   }

   // Write update file to MQL5/Files/ sandbox
   string update_filename = UPDATE_FILE_PREFIX + filename;
   int handle = FileOpen(update_filename, FILE_WRITE | FILE_BIN | FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("[PineTunnel] EA update: cannot create file %s (error %d)", update_filename, GetLastError());
      return false;
   }

   // Base64 decode and write
   uchar decoded[];
   int decoded_len = Base64DecodeMQL(b64_data, decoded);
   if(decoded_len <= 0)
   {
      PrintFormat("[PineTunnel] EA update: base64 decode failed");
      FileClose(handle);
      FileDelete(update_filename);
      return false;
   }

   FileWriteArray(handle, decoded, 0, decoded_len);
   FileClose(handle);

   // Verify SHA-256 if provided
   if(file_sha256 != "")
   {
      string actual_sha256 = FileHash(update_filename);
       if(actual_sha256 == "")
       {
          PrintFormat("[PineTunnel] EA update: hash computation failed - rejecting");
          FileDelete(update_filename);
          return false;
       }
       if(actual_sha256 != file_sha256)
      {
         PrintFormat("[PineTunnel] EA update: SHA-256 mismatch (expected %s, got %s)", file_sha256, actual_sha256);
         FileDelete(update_filename);
         return false;
      }
   }

   // Store update metadata for OnDeinit to apply
   g_updateFilePath = update_filename;
   g_updateFileVersion = file_version;
   g_updateDownloaded = true;

   PrintFormat("[PineTunnel] EA update v%s downloaded: %s (%d bytes, SHA-256 verified)",
               file_version, update_filename, decoded_len);
   PrintFormat("[PineTunnel] Restart terminal to apply update v%s", file_version);

   return true;
}

//+------------------------------------------------------------------+
//| Download DLL update from server                                    |
//| Downloads new PTWebSocket.dll via DLL (bypasses sandbox) or       |
//| WebRequest (sandbox fallback). Saves as PTWebSocket_new.dll in    |
//| the Libraries folder. On next terminal restart, CheckPendingEAUpdate|
//| will swap _new.dll -> .dll (DLL is not locked on restart).        |
//+------------------------------------------------------------------+
bool DownloadDLLUpdate(string target_version)
{
   string url = InpServerURL + DLL_DOWNLOAD_ENDPOINT + "mt5";
   string headers = "X-License-Key: " + InpLicenseID + "\r\n";

   PrintFormat("[PineTunnel] Downloading DLL update v%s...", target_version);

   // -- Try DLL-based download first (can write to Libraries folder directly) --
   if(g_wsClient != NULL)
   {
      string libs_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Libraries\\";
      string save_path = libs_dir + "PTWebSocket_new.dll";

      int dlResult = CPTWebSocketClient::DownloadFile(url, headers, save_path, 30000);
      if(dlResult == 0)
      {
         g_dllUpdateDownloaded = true;
         PrintFormat("[PineTunnel] DLL update v%s downloaded via DLL to: %s", target_version, save_path);
         PrintFormat("[PineTunnel] Restart terminal to apply DLL update v%s", target_version);
         return true;
      }
      PrintFormat("[PineTunnel] DLL download failed (error %d), falling back to WebRequest", dlResult);
   }

   // -- Fallback: WebRequest to MQL5 sandbox --
   // Note: WebRequest can only save to MQL5/Files/, not Libraries/.
   // The user would need to manually move the file, or we save it for
   // the DLL to move on next startup.
   PrintFormat("[PineTunnel] DLL update v%s: WebRequest fallback not supported for DLL files", target_version);
   PrintFormat("[PineTunnel] DLL auto-update requires WebSocket connection. Please reconnect.");
   return false;
}
//+------------------------------------------------------------------+
//| Compute SHA-256 hash of a file                                    |
//| Uses CryptEncode with CRYPT_HASH_SHA256 method.                   |
//+------------------------------------------------------------------+
string FileHash(string filename)
{
   int handle = FileOpen(filename, FILE_READ | FILE_BIN | FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return "";

   ulong file_size = FileSize(handle);
   if(file_size == 0 || file_size > 10000000) // Max 10MB for EA update
   {
      FileClose(handle);
      return "";
   }

   uchar data[];
   uint read = FileReadArray(handle, data, 0, (int)file_size);
   FileClose(handle);

   if(read != (uint)file_size)
      return "";

   // Compute SHA-256 using CryptEncode
   uchar hash[];
   uchar key[];  // Empty key for hash-only mode
   if(!CryptEncode(CRYPT_HASH_SHA256, data, key, hash))
      return "";

   // Convert hash bytes to hex string
   string hex = "";
   for(int i = 0; i < ArraySize(hash); i++)
      hex += StringFormat("%02x", hash[i]);

   return hex;
}

//+------------------------------------------------------------------+
//| Base64 decode helper - MQL5 does not have a native function       |
//+------------------------------------------------------------------+
int Base64DecodeMQL(const string b64, uchar &output[])
{
   // Base64 decode table (static - built once on first call)
   static int d[128];
   static bool d_init = false;
   if(!d_init)
   {
      ArrayInitialize(d, -1);
      d['A'] = 0;  d['B'] = 1;  d['C'] = 2;  d['D'] = 3;
      d['E'] = 4;  d['F'] = 5;  d['G'] = 6;  d['H'] = 7;
      d['I'] = 8;  d['J'] = 9;  d['K'] = 10; d['L'] = 11;
      d['M'] = 12; d['N'] = 13; d['O'] = 14; d['P'] = 15;
      d['Q'] = 16; d['R'] = 17; d['S'] = 18; d['T'] = 19;
      d['U'] = 20; d['V'] = 21; d['W'] = 22; d['X'] = 23;
      d['Y'] = 24; d['Z'] = 25;
      d['a'] = 26; d['b'] = 27; d['c'] = 28; d['d'] = 29;
      d['e'] = 30; d['f'] = 31; d['g'] = 32; d['h'] = 33;
      d['i'] = 34; d['j'] = 35; d['k'] = 36; d['l'] = 37;
      d['m'] = 38; d['n'] = 39; d['o'] = 40; d['p'] = 41;
      d['q'] = 42; d['r'] = 43; d['s'] = 44; d['t'] = 45;
      d['u'] = 46; d['v'] = 47; d['w'] = 48; d['x'] = 49;
      d['y'] = 50; d['z'] = 51;
      d['0'] = 52; d['1'] = 53; d['2'] = 54; d['3'] = 55;
      d['4'] = 56; d['5'] = 57; d['6'] = 58; d['7'] = 59;
      d['8'] = 60; d['9'] = 61;
      d['+'] = 62; d['/'] = 63;
      d_init = true;
   }

   int len = StringLen(b64);
   if(len == 0) return 0;

   // Calculate output size
   int padding = 0;
   if(len >= 1 && StringGetCharacter(b64, len - 1) == '=') padding++;
   if(len >= 2 && StringGetCharacter(b64, len - 2) == '=') padding++;
   int outLen = (len * 3) / 4 - padding;

   ArrayResize(output, outLen);

   int outIdx = 0;
   int buf = 0;
   int bits = 0;

   for(int i = 0; i < len; i++)
   {
      ushort ch = StringGetCharacter(b64, i);
      if(ch >= 128 || d[ch] == -1)
      {
         if(ch == '=')
            break;
         continue; // Skip invalid chars
      }
      buf = (buf << 6) | d[ch];
      bits += 6;
      if(bits >= 8)
      {
         bits -= 8;
         if(outIdx < outLen)
         {
            output[outIdx] = (uchar)((buf >> bits) & 0xFF);
            outIdx++;
         }
      }
   }

   return outIdx;
}

//+------------------------------------------------------------------+
//| Send audit/telemetry data to server                             |
//+------------------------------------------------------------------+
bool SendAuditData()
{
   if(InpAuditInterval <= 0) return false; // Audit disabled

    datetime now = TimeCurrent();
    if(g_lastAuditSent > 0 && (now - g_lastAuditSent) < InpAuditInterval)
       return false;

    // -- EA & DLL info --
   string platform = "mt5";
   string ea_version = PT_VERSION;
   string dll_version = "";
   string dll_system_info = "";
   if(g_wsClient != NULL && g_wsClient.IsConnected())
   {
      dll_version = g_wsClient.GetDllVersion();
      dll_system_info = g_wsClient.GetSystemInfo();
   }

   // -- Terminal info --
   int mt_build = TerminalInfoInteger(TERMINAL_BUILD);
   string terminal_name = TerminalInfoString(TERMINAL_NAME);
   string terminal_language = TerminalInfoString(TERMINAL_LANGUAGE);
   string os_info = TerminalInfoString(TERMINAL_CPU_ARCHITECTURE) + " " + TerminalInfoString(TERMINAL_OS_VERSION);
   int terminal_pid = 0;  // No MQL5 API for PID
   int terminal_x64 = TerminalInfoInteger(TERMINAL_X64);
   int terminal_disk_space = TerminalInfoInteger(TERMINAL_DISK_SPACE);
   int terminal_memory_phys = TerminalInfoInteger(TERMINAL_MEMORY_PHYSICAL);
   int terminal_memory_avail = TerminalInfoInteger(TERMINAL_MEMORY_AVAILABLE);
   int terminal_cpu_cores = TerminalInfoInteger(TERMINAL_CPU_CORES);
   int terminal_cpu_freq = 0;  // No MQL5 API for CPU frequency

   // -- Account identity --
   long account_number = AccountInfoInteger(ACCOUNT_LOGIN);
   string account_name = EscapeJSON(AccountInfoString(ACCOUNT_NAME));
   string account_server = EscapeJSON(AccountInfoString(ACCOUNT_SERVER));
   string account_currency = EscapeJSON(AccountInfoString(ACCOUNT_CURRENCY));
   string broker = EscapeJSON(AccountInfoString(ACCOUNT_COMPANY));
   long account_leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);
   int account_limit_orders = (int)AccountInfoInteger(ACCOUNT_LIMIT_ORDERS);
   bool account_trade_allowed = (bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED);
   bool account_trade_expert = (bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT);
   ENUM_ACCOUNT_TRADE_MODE trade_mode = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   string trade_mode_str = "real";
   if(trade_mode == ACCOUNT_TRADE_MODE_DEMO) trade_mode_str = "demo";
   else if(trade_mode == ACCOUNT_TRADE_MODE_CONTEST) trade_mode_str = "contest";
   ENUM_ACCOUNT_STOPOUT_MODE so_mode = (ENUM_ACCOUNT_STOPOUT_MODE)AccountInfoInteger(ACCOUNT_MARGIN_SO_MODE);
   string so_mode_str = (so_mode == ACCOUNT_STOPOUT_MODE_PERCENT) ? "percent" : "money";

   // -- Account financials --
   double account_balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double account_credit = AccountInfoDouble(ACCOUNT_CREDIT);
   double account_equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double account_profit = AccountInfoDouble(ACCOUNT_PROFIT);
   double account_margin = AccountInfoDouble(ACCOUNT_MARGIN);
   double account_margin_free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double account_margin_level = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   double account_margin_so_call = AccountInfoDouble(ACCOUNT_MARGIN_SO_CALL);
   double account_margin_so_so = AccountInfoDouble(ACCOUNT_MARGIN_SO_SO);

   // -- Chart info --
   string chart_symbol = _Symbol;
   string chart_timeframe = EnumToString(Period());

   // -- Runtime stats --
   int position_count = PositionsTotal();
   int symbol_count = SymbolsTotal(true);
   long uptime = (long)(TimeCurrent() - g_eaStartTime);
   string ws_status = g_useWebSocket ? "connected" : (g_wsClient != NULL ? "disconnected" : "unavailable");
   string connection_mode = "";
   switch(InpConnectionMode)
   {
      case CONNECTION_MODE_WEBSOCKET: connection_mode = "wss"; break;
      case CONNECTION_MODE_LONGPOLL:  connection_mode = "https"; break;
      case CONNECTION_MODE_HYBRID:    connection_mode = "hybrid"; break;
   }

   // -- VPS info --
   int is_vps = 0;
   string vps_provider = "";
   string vps_manufacturer = "";
   string vps_model = "";
   PTWS_VpsInfo vpsInfo;
   if(CPTWebSocketClient::GetVpsInfo(vpsInfo))
   {
      is_vps = vpsInfo.is_vps;
      vps_provider = CharArrayToString(vpsInfo.provider);
      vps_manufacturer = CharArrayToString(vpsInfo.manufacturer);
      vps_model = CharArrayToString(vpsInfo.model);
   }

    // -- Network diagnostics --
    PTWS_NetDiag netDiag;
    string net_quality = "";
    int net_ping_ms = 0;
    int net_jitter_ms = 0;
    double net_loss_pct = 0.0;
    string diagHost = InpServerURL;
    int diagSchemeEnd = StringFind(diagHost, "://");
    if(diagSchemeEnd >= 0) diagHost = StringSubstr(diagHost, diagSchemeEnd + 3);
    int diagPathStart = StringFind(diagHost, "/");
    if(diagPathStart >= 0) diagHost = StringSubstr(diagHost, 0, diagPathStart);
    if(CPTWebSocketClient::RunNetworkDiag(diagHost, 5000, 10, netDiag))
   {
      net_quality = CharArrayToString(netDiag.quality);
      net_ping_ms = netDiag.ping_ms;
      net_jitter_ms = netDiag.jitter95_ms;
      net_loss_pct = netDiag.packet_loss_pct;
   }

   // -- NTP time sync --
   PTWS_NtpTime ntpInfo;
   int ntp_drift_ms = 0;
   int ntp_sync_success = 0;
   if(CPTWebSocketClient::GetNtpTime(ntpInfo))
   {
      ntp_drift_ms = ntpInfo.drift_ms;
      ntp_sync_success = ntpInfo.sync_success;
   }

   // -- Build comprehensive JSON --
   string json = "{";
   // EA & DLL
   json += "\"platform\":\"" + platform + "\",";
   json += "\"ea_version\":\"" + ea_version + "\",";
   json += "\"dll_version\":\"" + (dll_version != "" ? dll_version : "N/A") + "\",";
   json += "\"dll_system_info\":" + (dll_system_info != "" ? dll_system_info : "null") + ",";
   // Terminal
   json += "\"mt_build\":" + IntegerToString(mt_build) + ",";
   json += "\"terminal_name\":\"" + EscapeJSON(terminal_name) + "\",";
   json += "\"terminal_language\":\"" + EscapeJSON(terminal_language) + "\",";
   json += "\"terminal_x64\":" + IntegerToString(terminal_x64) + ",";
   json += "\"terminal_pid\":" + IntegerToString(terminal_pid) + ",";
   json += "\"os\":\"" + EscapeJSON(os_info) + "\",";
   json += "\"cpu_cores\":" + IntegerToString(terminal_cpu_cores) + ",";
   json += "\"cpu_freq_mhz\":" + IntegerToString(terminal_cpu_freq) + ",";
   json += "\"ram_mb\":" + IntegerToString(terminal_memory_phys) + ",";
   json += "\"ram_avail_mb\":" + IntegerToString(terminal_memory_avail) + ",";
   json += "\"disk_mb\":" + IntegerToString(terminal_disk_space) + ",";
   // Account identity
   json += "\"account_number\":" + IntegerToString(account_number) + ",";
   json += "\"account_name\":\"" + account_name + "\",";
   json += "\"account_server\":\"" + account_server + "\",";
   json += "\"account_currency\":\"" + account_currency + "\",";
   json += "\"broker\":\"" + broker + "\",";
   json += "\"trade_mode\":\"" + trade_mode_str + "\",";
   json += "\"leverage\":" + IntegerToString(account_leverage) + ",";
   json += "\"limit_orders\":" + IntegerToString(account_limit_orders) + ",";
   json += "\"trade_allowed\":" + (account_trade_allowed ? "true" : "false") + ",";
   json += "\"trade_expert\":" + (account_trade_expert ? "true" : "false") + ",";
   json += "\"margin_so_mode\":\"" + so_mode_str + "\",";
   // Account financials
   json += "\"balance\":" + DoubleToString(account_balance, 2) + ",";
   json += "\"credit\":" + DoubleToString(account_credit, 2) + ",";
   json += "\"equity\":" + DoubleToString(account_equity, 2) + ",";
   json += "\"profit\":" + DoubleToString(account_profit, 2) + ",";
   json += "\"margin\":" + DoubleToString(account_margin, 2) + ",";
   json += "\"margin_free\":" + DoubleToString(account_margin_free, 2) + ",";
   json += "\"margin_level\":" + DoubleToString(account_margin_level, 2) + ",";
   json += "\"margin_so_call\":" + DoubleToString(account_margin_so_call, 2) + ",";
   json += "\"margin_so_so\":" + DoubleToString(account_margin_so_so, 2) + ",";
   // Chart
   json += "\"chart_symbol\":\"" + chart_symbol + "\",";
   json += "\"chart_timeframe\":\"" + chart_timeframe + "\",";
   // Runtime
   json += "\"symbol_count\":" + IntegerToString(symbol_count) + ",";
   json += "\"position_count\":" + IntegerToString(position_count) + ",";
   json += "\"uptime_sec\":" + IntegerToString((int)uptime) + ",";
   json += "\"ws_status\":\"" + ws_status + "\",";
   json += "\"error_count\":" + IntegerToString(g_consecutiveErrors) + ",";
   json += "\"connection_mode\":\"" + connection_mode + "\",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   json += "\"auto_update_enabled\":" + (InpAutoUpdate ? "true" : "false") + ",";
   // VPS
   json += "\"is_vps\":" + IntegerToString(is_vps) + ",";
   json += "\"vps_provider\":\"" + EscapeJSON(vps_provider) + "\",";
   json += "\"vps_manufacturer\":\"" + EscapeJSON(vps_manufacturer) + "\",";
   json += "\"vps_model\":\"" + EscapeJSON(vps_model) + "\",";
   // Network
   json += "\"net_quality\":\"" + net_quality + "\",";
   json += "\"net_ping_ms\":" + IntegerToString(net_ping_ms) + ",";
   json += "\"net_jitter_ms\":" + IntegerToString(net_jitter_ms) + ",";
   json += "\"net_loss_pct\":" + DoubleToString(net_loss_pct, 1) + ",";
   // NTP
   json += "\"ntp_drift_ms\":" + IntegerToString(ntp_drift_ms) + ",";
   json += "\"ntp_sync_success\":" + IntegerToString(ntp_sync_success);
   json += "}";

   // POST to audit endpoint
   string url = InpServerURL + AUDIT_ENDPOINT + InpLicenseID;
   string headers = "Content-Type: application/json\r\nX-License-Key: " + InpLicenseID + "\r\n";

   uchar post_data[];
   uchar result_data[];
   string result_headers;

   StringToCharArray(json, post_data, 0, WHOLE_ARRAY, CP_UTF8);

   int timeout = 5000;
   int res = WebRequest("POST", url, headers, timeout, post_data, result_data, result_headers);

     if(res == -1)
     {
        PrintFormat("[PineTunnel] Audit send failed: WebRequest error %d (add %s to Tools->Options->Expert Advisors URL list)", GetLastError(), InpServerURL);
       g_lastAuditSent = now - InpAuditInterval + 60;
        return false;
     }

    if(res != 200)
    {
       g_lastAuditSent = now - InpAuditInterval + 60;
       return false;
    }

    g_lastAuditSent = now;

    // Parse response for update info
   string response = CharArrayToString(result_data);
   string latest_ver = ExtractStringValue(response, "latest_version");
   string update_avail = ExtractStringValue(response, "update_available");

   if(latest_ver != "" && latest_ver != g_latestVersion)
   {
       g_latestVersion = latest_ver;
       if(CompareVersions(latest_ver, PT_VERSION) > 0)
          g_updateAvailable = true;
       else
          g_updateAvailable = false;
   }

   return true;
}

//+------------------------------------------------------------------+
//| Apply downloaded EA update (called from OnDeinit)                |
//| Renames update file to replace current EA on next restart.       |
//| MQL5 sandbox limits: we can only write to Files/ folder.         |
//| The user must manually copy the file or use terminal restart.    |
//+------------------------------------------------------------------+
void ApplyPendingUpdate()
{
   if(!g_updateDownloaded || g_updateFilePath == "")
      return;

   if(StringFind(g_updateFilePath, ":\\") > 0)
   {
      // DLL-based download wrote directly to Experts/Libraries folder
      // Cannot overwrite running files (Windows locks them). The _new files
      // will be picked up on next terminal restart by CheckPendingEAUpdate().
      PrintFormat("[PineTunnel] Update v%s ready: %s", g_updateFileVersion, g_updateFilePath);
      PrintFormat("[PineTunnel] Restart terminal to complete update to v%s", g_updateFileVersion);
   }
   else
   {
      // WebRequest-based download saved to MQL5 sandbox
      PrintFormat("[PineTunnel] Update v%s pending: %s", g_updateFileVersion, g_updateFilePath);
      PrintFormat("[PineTunnel] To apply: copy %s to your MQL5/Experts/ folder and restart", g_updateFilePath);
   }
}

//+------------------------------------------------------------------+
//| Check for pending EA update from previous session                 |
//| On terminal restart, the DLL swaps PineTunnel_EA_new.ex5 over     |
//| PineTunnel_EA.ex5 (old .ex5 is no longer locked on restart).      |
//+------------------------------------------------------------------+
void CheckPendingEAUpdate()
{
   string experts_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Experts";
   int result = CPTWebSocketClient::ApplyUpdate(experts_dir, "PineTunnel_EA.ex5", "PineTunnel_EA_new.ex5");
   if(result == 0)
   {
      g_eaJustUpdated = true;
      PrintFormat("[PineTunnel] Applied pending EA update from previous session");
   }

   // Also check for pending DLL update (DLL not locked on terminal restart)
   string libs_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Libraries";
   int dllResult = CPTWebSocketClient::ApplyUpdate(libs_dir, "PTWebSocket.dll", "PTWebSocket_new.dll");
   if(dllResult == 0)
   {
      g_dllJustUpdated = true;
      PrintFormat("[PineTunnel] Applied pending DLL update from previous session");
   }

   // -- Clear restart counter (infinite loop protection) --
   // The batch script also clears this, but clear it here too in case the
   // terminal was restarted manually (not via auto-restart script).
   CPTWebSocketClient::ClearRestartCounter(TerminalInfoString(TERMINAL_DATA_PATH));
}

//+------------------------------------------------------------------+
//| Trigger auto-restart after downloading an update                   |
//| Uses DLL to create a batch script that waits for terminal exit,    |
//| applies pending updates, and relaunches the terminal.             |
//| Then calls TerminalClose(0) to gracefully shut down.              |
//| Returns true if restart was scheduled, false otherwise.            |
//+------------------------------------------------------------------+
bool TriggerAutoRestart()
{
   if(!InpAutoRestart)
   {
      PrintFormat("[PineTunnel] Auto-restart disabled by InpAutoRestart input");
      return false;
   }

     // -- Cooldown: don't restart if EA just started (< 60 seconds) --
     datetime now = TimeLocal();
    if(g_eaStartTime > 0 && (now - g_eaStartTime) < 60)
    {
       static datetime s_lastCooldownLog = 0;
       if((now - s_lastCooldownLog) >= 10)
       {
          s_lastCooldownLog = now;
          PrintFormat("[PineTunnel] Auto-restart blocked: EA started less than 60s ago (cooldown protection)");
       }
       return false;
    }

   // -- Safety: require both EA and DLL updates to be downloaded --
   // Only restart if we have a pending update file
   if(!g_updateDownloaded && !g_dllUpdateDownloaded)
   {
      PrintFormat("[PineTunnel] Auto-restart: no pending update files");
      return false;
   }

   string terminal_path = TerminalInfoString(TERMINAL_PATH);
   string data_path = TerminalInfoString(TERMINAL_DATA_PATH);

   // For portable mode, use the terminal executable directly
   // For non-portable, terminal64.exe needs /portable or /config flag
   string config_path = "";

   PrintFormat("[PineTunnel] Scheduling auto-restart for update...");
   PrintFormat("[PineTunnel] Terminal: %s", terminal_path);
   PrintFormat("[PineTunnel] Data: %s", data_path);

    int result = CPTWebSocketClient::ScheduleRestart(terminal_path, data_path, config_path, 5000);
    if(result != 0)
    {
       PrintFormat("[PineTunnel] ScheduleRestart returned %d - relying on watchdog task for restart", result);
    }
    else
    {
       PrintFormat("[PineTunnel] Auto-restart scheduled successfully, closing terminal...");
    }

    // Brief delay to let the batch script start
    Sleep(500);

    // Gracefully close the terminal - watchdog or batch script will restart
    PrintFormat("[PineTunnel] Closing terminal - watchdog or batch script will restart...");
     if(!TerminalClose(0))
        PrintFormat("[PineTunnel] CRITICAL: TerminalClose failed - manual restart required");
     return true;
}

//===================================================================
// V7.00: SL/TP VERIFICATION AFTER ORDER OPEN
//===================================================================
bool VerifyAndFixSLTP(ulong ticket, string symbol, double intended_sl, double intended_tp, int max_retries = 3)
{
   if(ticket == 0) return true;  // No ticket to verify
   if(intended_sl <= 0 && intended_tp <= 0) return true;  // No SL/TP to verify

   // Select the position
   if(!PositionSelectByTicket(ticket))
   {
      PrintFormat("[PineTunnel] V7.00 Could not select position #%I64d for SL/TP verification", ticket);
      return true;  // Position may have closed already, not an error
   }

   double current_sl = PositionGetDouble(POSITION_SL);
   double current_tp = PositionGetDouble(POSITION_TP);

   // Check if SL/TP matches expected values (with tolerance)
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double tolerance = point * PRICE_TOLERANCE_POINTS;  // 5 point tolerance

   bool sl_ok = (intended_sl <= 0) || (MathAbs(current_sl - intended_sl) < tolerance);
   bool tp_ok = (intended_tp <= 0) || (MathAbs(current_tp - intended_tp) < tolerance);

   if(sl_ok && tp_ok)
   {
      PrintFormat("[PineTunnel] V7.00 SL/TP verified: SL=%.5f TP=%.5f", current_sl, current_tp);
      return true;
   }

   // SL/TP mismatch - attempt to fix
   PrintFormat("[PineTunnel] V7.00 SL/TP MISMATCH detected for #%I64d", ticket);
   PrintFormat("[PineTunnel] Expected: SL=%.5f TP=%.5f | Actual: SL=%.5f TP=%.5f",
               intended_sl, intended_tp, current_sl, current_tp);

   // Attempt to modify position to correct SL/TP
   for(int retry = 0; retry < max_retries; retry++)
   {
      if(g_trade.PositionModify(ticket, intended_sl, intended_tp))
      {
         PrintFormat("[PineTunnel] V7.00 SL/TP FIXED on retry %d", retry + 1);
         return true;
      }

      PrintFormat("[PineTunnel] V7.00 Retry %d/%d failed: %d - %s",
                  retry + 1, max_retries,
                  g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());

      Sleep(100);  // Brief pause before retry
   }

   // All retries failed - log critical error
   PrintFormat("[PineTunnel] V7.00 CRITICAL: Could not set SL/TP after %d retries!", max_retries);
   PrintFormat("[PineTunnel] V7.00 Position #%I64d may have NO STOP LOSS protection!", ticket);

   return false;
}
enum CommandType
{
   COMMAND_NONE = 0,
   COMMAND_BUY,
   COMMAND_SELL,
   COMMAND_CLOSE_ALL,
   COMMAND_CLOSE_LONG,
   COMMAND_CLOSE_SHORT,
   // Pending order commands
   COMMAND_BUY_LIMIT,
   COMMAND_SELL_LIMIT,
   COMMAND_BUY_STOP,
   COMMAND_SELL_STOP,
   COMMAND_CANCEL_LONG,
   COMMAND_CANCEL_SHORT,
   // Phase 2: Partial close commands
   COMMAND_CLOSE_LONG_PCT,
   COMMAND_CLOSE_SHORT_PCT,
   COMMAND_CLOSE_LONG_VOL,
   COMMAND_CLOSE_SHORT_VOL,
   // Phase 2: Modification commands
   COMMAND_SLTP__LONG,
   COMMAND_SLTP__SHORT,
   COMMAND_SLTP_BUY_STOP,
   COMMAND_SLTP_BUY_LIMIT,
   COMMAND_SLTP_SELL_STOP,
   COMMAND_SLTP_SELL_LIMIT,
   // Priority 1&2: Additional commands
   COMMAND_CLOSE_LONG_SHORT,
   COMMAND_EXIT,  // Alias for CLOSE_ALL - closes all positions for symbol with comment filter
   // Combined actions - close+open
   COMMAND_CLOSE_LONG_OPEN_LONG,
   COMMAND_CLOSE_LONG_OPEN_SHORT,
   COMMAND_CLOSE_SHORT_OPEN_LONG,
   COMMAND_CLOSE_SHORT_OPEN_SHORT,
   COMMAND_CLOSE_LONGSHORT_OPEN_LONG,
   COMMAND_CLOSE_LONGSHORT_OPEN_SHORT,
   // Combined actions - cancel+place
   COMMAND_CANCEL_LONG_BUY_STOP,
   COMMAND_CANCEL_LONG_BUY_LIMIT,
   COMMAND_CANCEL_SHORT_SELL_STOP,
   COMMAND_CANCEL_SHORT_SELL_LIMIT,
   // EA Management
   COMMAND_EA_OFF,
   COMMAND_EA_ON,
   COMMAND_CLOSEALL_EA_OFF
};
struct SignalCommand
{
   string      signal_id;
   CommandType type;
   string      raw_action;      // V7.01: Store raw action string for validation
   string      symbol;
   double      lots;
   double      risk_percent;    // Risk as % of balance
   double      stop_loss;       // Stop loss in pips/points or price
   double      take_profit;     // Take profit in pips/points or price
   bool        use_risk_sizing; // Whether to calculate lots from risk%
   string      comment;         // Order comment
   double      pending_distance; // Distance/price for pending orders
   bool        has_pending;      // Flag if pending parameter exists
   double      account_filter;   // Account filter value

    // Explicit type indicators for PineTunnel explicit syntax parameters
    // When set, these override EA settings (VolumeType, TargetType)
    // Empty string = use EA setting, otherwise use explicit type
     string      vol_type;        // "lots", "dollar", "bal_loss", "eq_loss", "bal_margin", "eq_margin", or "" (use EA setting)
    string      sl_type;         // "pips", "price", "pct", or "" (use EA setting)
    string      tp_type;         // "pips", "price", "pct", or "" (use EA setting)
    string      entry_type;      // "price", "pips", "pct", or "" (use EA PendingType setting)
    double      partial_close_pct; // Signal-provided pct for close_long_pct/close_short_pct (0 = use EA default)
    bool        has_sl;            // Whether SL param was present in signal
    bool        nm;                 // Near-Market flag: enables limit order conversion for price improvement
};
//+------------------------------------------------------------------+
//| Signal Queue - holds signals when market is temporarily closed   |
//+------------------------------------------------------------------+
#define SQ_MAX_SIZE         100
#define SQ_MAX_DRAIN_RETRIES 120  // ~2 min at 1-s timer before abandoning a 10018 retry
#define SQ_MAX_QUEUE_TIME_SEC 1800  // 30 min max time in queue before expiry

struct SQueuedSignal
{
   SignalCommand cmd;
   datetime      queued_time;
   bool          is_retry;     // true = ExecuteCommand already ran once (lock file may exist)
   int           drain_retries; // count of consecutive 10018 retries from DrainSignalQueue
};

class CSignalQueue
{
private:
   SQueuedSignal m_items[SQ_MAX_SIZE];
   int           m_size;

public:
   CSignalQueue() { m_size = 0; }

   bool Push(const SignalCommand &cmd, bool is_retry = false)
   {
      if(m_size >= SQ_MAX_SIZE)
      {
         PrintFormat("[Queue] Queue full - dropping oldest: %s on %s (ID: %s)",
                     CommandName(m_items[0].cmd.type), m_items[0].cmd.symbol, m_items[0].cmd.signal_id);
         if(m_items[0].cmd.signal_id != "")
            AcknowledgeSignal(m_items[0].cmd.signal_id);
         for(int i = 0; i < m_size - 1; i++)
            m_items[i] = m_items[i + 1];
         m_size--;
      }
      m_items[m_size].cmd          = cmd;
      m_items[m_size].queued_time  = TimeCurrent();
      m_items[m_size].is_retry     = is_retry;
      m_items[m_size].drain_retries = 0;
      m_size++;
      return true;
   }

   bool Peek(int index, SQueuedSignal &out) const
   {
      if(index < 0 || index >= m_size) return false;
      out = m_items[index];
      return true;
   }

   void Remove(int index)
   {
      if(index < 0 || index >= m_size) return;
      for(int i = index; i < m_size - 1; i++)
         m_items[i] = m_items[i + 1];
      m_items[m_size - 1].cmd.signal_id  = "";
      m_items[m_size - 1].cmd.type       = COMMAND_NONE;
      m_items[m_size - 1].drain_retries  = 0;
      m_size--;
   }

   int  Size()    const { return m_size; }
   bool IsEmpty() const { return m_size == 0; }

   // Increment drain retry counter; returns new count. Returns -1 on bad index.
   int IncrDrainRetries(int index)
   {
      if(index < 0 || index >= m_size) return -1;
      return ++m_items[index].drain_retries;
   }

   string GetStatus() const
   {
      if(m_size == 0) return "Empty";
      return IntegerToString(m_size) + " signal(s) queued";
   }
};

CSignalQueue* g_signalQueue = NULL;

//+------------------------------------------------------------------+
//| Forward declarations                                             |
//+------------------------------------------------------------------+
bool PollForSignals();
string TransformSymbol(const string symbol);
SignalCommand ParseSignal(const string signal_json);
bool ExecuteCommand(const SignalCommand &command, bool from_queue = false);
bool IsQueueableCommandType(CommandType type);
void DrainSignalQueue();
bool SendCloseReport(string symbol, ulong ticket, double close_price, double profit, string signal_id = "");
bool SendTradeReport(string action, string symbol, double volume, double price, ulong ticket, bool success, string error_msg = "", string signal_id = "");
bool AddHiddenTarget(ulong ticket, string symbol, int position_type, double sl_price, double tp_price, double entry_price);
void InitializeSpreadHistory();
void CheckDailyReset();
// V7.06: Layer-4 dedup defense (broker-state reconciliation)
string BuildOrderComment(const string signal_id, const string user_comment);
bool   IsDuplicateSignalPosition(const string signal_id, const string symbol);
// V7.06: Cancel-confirmation helpers (orchestrate OrderDelete + history check)
bool   WaitForOrderToLeavePool(ulong order_ticket, int timeout_ms);
bool   FindOrderFillInHistory(ulong order_ticket, double &fill_price, ulong &fill_position_id);
ENUM_ORDER_DELETE_RESOLUTION ResolveOrderDelete(ulong order_ticket, int timeout_ms, double &fill_price, ulong &fill_position_id);
void LogExecutionQuality(ulong deal_ticket);
double CalculateVolumeWithExplicitType(const SignalCommand &cmd, double entry_price, double sl_price, ENUM_ORDER_TYPE order_type);
double CalculateSLPriceWithExplicitType(const SignalCommand &cmd, double entry_price, bool is_buy);
double CalculateTPPriceWithExplicitType(const SignalCommand &cmd, double entry_price, bool is_buy);
void ProcessWebSocketSignals(string json_message);
int  ProcessPendingWSSignals();
void ProcessWSSignalsArray(string json_message);
string EscapeJSON(string str);
void CountTodayTrades();
//+------------------------------------------------------------------+
//| Calculate volume with explicit type support                       |
//| If vol_type is set, use explicit calculation method              |
//| Otherwise fall back to EA VolumeType setting                     |
//| Requirements: 1.7, 6.1, 6.5                                      |
//+------------------------------------------------------------------+
double CalculateVolumeWithExplicitType(
   const SignalCommand &cmd,
   double entry_price,
   double sl_price,
   ENUM_ORDER_TYPE order_type
)
{
   // Safety check for position sizer
   if(g_positionSizer == NULL)
   {
      PrintFormat("[PineTunnel] Position sizer not initialized, using default lots: %.2f", InpDefaultLots);
      return InpDefaultLots;
   }

   // Determine risk value based on what's available
   double risk_value = 0;

       // Check if explicit vol_type is specified
       if(cmd.vol_type != "")
       {
          // Use explicit volume type - override EA setting
          // Normalize to uppercase for case-insensitive matching (parity with MT4)
          string vt = cmd.vol_type;
          StringToUpper(vt);

          if(vt == "LOTS")
         {
            risk_value = (cmd.lots > 0) ? cmd.lots : InpDefaultLots;
            return g_positionSizer.CalculateDirectLots(cmd.symbol, risk_value);
         }
         else if(vt == "DOLLAR" || vt == "USD")
         {
            risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
            if(sl_price <= 0)
            {
               PrintFormat("[PineTunnel] ERROR: usd requires stop loss price");
               return InpDefaultLots;
            }
            return g_positionSizer.CalculateDollarAmount(cmd.symbol, risk_value, entry_price, sl_price);
         }
         else if(vt == "BAL_LOSS" || vt == "BALANCE_LOSS")
         {
            risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
            if(sl_price <= 0)
            {
               PrintFormat("[PineTunnel] ERROR: risk_bal_pct requires stop loss price");
               return InpDefaultLots;
            }
            return g_positionSizer.CalculatePercentageBalanceLoss(cmd.symbol, risk_value, entry_price, sl_price);
         }
         else if(vt == "EQ_LOSS" || vt == "EQUITY_LOSS")
          {
             risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
             if(sl_price <= 0)
             {
                PrintFormat("[PineTunnel] ERROR: risk_eq_pct requires stop loss price");
                return InpDefaultLots;
             }
             return g_positionSizer.CalculatePercentageEquityLoss(cmd.symbol, risk_value, entry_price, sl_price);
          }
          else if(vt == "EQ_MARGIN" || vt == "EQUITY_MARGIN")
          {
             risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
             return g_positionSizer.CalculatePercentageEquityMargin(cmd.symbol, risk_value, order_type, entry_price);
          }
         else if(vt == "BAL_MARGIN" || vt == "BALANCE_MARGIN")
         {
            risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
            return g_positionSizer.CalculatePercentageBalanceMargin(cmd.symbol, risk_value, order_type, entry_price);
         }
         else if(vt == "PCT_BAL_LOTS" || vt == "BALANCE_LOTS")
         {
            risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
            return g_positionSizer.CalculatePercentageBalanceLots(cmd.symbol, risk_value);
          }
          else
          {
             PrintFormat("[PineTunnel] Unknown explicit vol_type '%s', falling back to EA setting", cmd.vol_type);
          }
       }

    // Fall back to EA VolumeType setting

   // Determine risk value based on volume type
   risk_value = (InpVolumeType == VOLUME_LOTS)
               ? (cmd.lots > 0 ? cmd.lots : InpDefaultLots)
               : (cmd.risk_percent > 0 ? cmd.risk_percent : InpRisk);

   // Calculate lots using EA's configured volume type
   return g_positionSizer.CalculateVolume(
      InpVolumeType,
      cmd.symbol,
      risk_value,
      entry_price,
      sl_price,
      order_type
   );
}
//+------------------------------------------------------------------+
//| Calculate SL price with explicit type support                     |
//| If sl_type is set, use explicit calculation method               |
//| Otherwise fall back to EA TargetType setting                     |
//| Requirements: 2.5, 6.2, 6.5                                      |
//+------------------------------------------------------------------+
double CalculateSLPriceWithExplicitType(
   const SignalCommand &cmd,
   double entry_price,
   bool is_buy
)
{
   double sl_value = cmd.stop_loss;

   // If no SL value provided, return 0
   if(sl_value <= 0)
      return 0;

   // Get symbol info for calculations
   if(!g_symbol.Name(cmd.symbol))
   {
      PrintFormat("[PineTunnel] ERROR: Failed to get symbol info for %s", cmd.symbol);
      return 0;
   }

   double point = g_symbol.Point();
   int digits = g_symbol.Digits();

   // Determine which type to use
   string sl_type = cmd.sl_type;

   // Check if explicit sl_type is specified
    if(sl_type != "")
    {
       StringToUpper(sl_type);

        if(sl_type == "PRICE")
        {
           double sl_price_val = sl_value;
           if(is_buy && sl_price_val >= entry_price)
           {
              PrintFormat("[PineTunnel] ERROR: Invalid SL for BUY at %.5f (SL %.5f above entry)",
                         entry_price, sl_price_val);
              return 0;
           }
           else if(!is_buy && sl_price_val <= entry_price)
           {
              PrintFormat("[PineTunnel] ERROR: Invalid SL for SELL at %.5f (SL %.5f below entry)",
                         entry_price, sl_price_val);
              return 0;
           }
           return NormalizeDouble(sl_price_val, digits);
        }
        else if(sl_type == "PIPS" || sl_type == "PIP")
        {
           double sl_distance = sl_value * point;
           double sl_price = 0;

           if(is_buy)
              sl_price = entry_price - sl_distance;
           else
              sl_price = entry_price + sl_distance;

           return NormalizeDouble(sl_price, digits);
        }
        else if(sl_type == "PCT" || sl_type == "PERCENT" || sl_type == "PERCENTAGE")
        {
           double sl_percentage = sl_value / 100.0;
           double sl_price = 0;

           if(is_buy)
              sl_price = entry_price * (1 - sl_percentage);
           else
              sl_price = entry_price * (1 + sl_percentage);

           return NormalizeDouble(sl_price, digits);
        }
       else
       {
          PrintFormat("[PineTunnel] Unknown explicit sl_type '%s', falling back to EA setting", sl_type);
       }
    }

    // Fall back to EA TargetType setting

    switch(InpTargetType)
    {
       case TARGET_TYPE_PRICE:
          // Direct price level - validate direction
          if(is_buy && sl_value >= entry_price)
          {
             PrintFormat("[PineTunnel] ERROR: Invalid SL for BUY at %.5f (SL above entry)", entry_price);
             return 0;
          }
          else if(!is_buy && sl_value <= entry_price)
          {
             PrintFormat("[PineTunnel] ERROR: Invalid SL for SELL at %.5f (SL below entry)", entry_price);
             return 0;
          }
          return NormalizeDouble(sl_value, digits);

      case TARGET_TYPE_PIPS:
         {
            double sl_distance = sl_value * point;
            double sl_price = 0;

            if(is_buy)
               sl_price = entry_price - sl_distance;
            else
               sl_price = entry_price + sl_distance;

            return NormalizeDouble(sl_price, digits);
         }

      case TARGET_TYPE_PERCENTAGE:
         {
            double sl_percentage = sl_value / 100.0;
            double sl_price = 0;

            if(is_buy)
               sl_price = entry_price * (1 - sl_percentage);
            else
               sl_price = entry_price * (1 + sl_percentage);

            return NormalizeDouble(sl_price, digits);
         }
   }

   return 0;
}
//+------------------------------------------------------------------+
//| Calculate TP price with explicit type support                     |
//| If tp_type is set, use explicit calculation method               |
//| Otherwise fall back to EA TargetType setting                     |
//| Requirements: 3.5, 6.3, 6.5                                      |
//+------------------------------------------------------------------+
double CalculateTPPriceWithExplicitType(
   const SignalCommand &cmd,
   double entry_price,
   bool is_buy
)
{
   double tp_value = cmd.take_profit;

   // If no TP value provided, return 0
   if(tp_value <= 0)
      return 0;

   // Get symbol info for calculations
   if(!g_symbol.Name(cmd.symbol))
   {
      PrintFormat("[PineTunnel] ERROR: Failed to get symbol info for %s", cmd.symbol);
      return 0;
   }

   double point = g_symbol.Point();
   int digits = g_symbol.Digits();

   // Determine which type to use
   string tp_type = cmd.tp_type;

   // Check if explicit tp_type is specified
    if(tp_type != "")
    {
       StringToUpper(tp_type);

       if(tp_type == "PRICE")
       {
          return NormalizeDouble(tp_value, digits);
       }
       else if(tp_type == "PIPS" || tp_type == "PIP")
       {
          double tp_distance = tp_value * point;
          double tp_price = 0;

          if(is_buy)
             tp_price = entry_price + tp_distance;
          else
             tp_price = entry_price - tp_distance;

          return NormalizeDouble(tp_price, digits);
       }
       else if(tp_type == "PCT" || tp_type == "PERCENT" || tp_type == "PERCENTAGE")
       {
          double tp_percentage = tp_value / 100.0;
          double tp_price = 0;

          if(is_buy)
             tp_price = entry_price * (1 + tp_percentage);
          else
             tp_price = entry_price * (1 - tp_percentage);

          return NormalizeDouble(tp_price, digits);
       }
       else
       {
          PrintFormat("[PineTunnel] Unknown explicit tp_type '%s', falling back to EA setting", tp_type);
       }
    }

   // Fall back to EA TargetType setting

   switch(InpTargetType)
   {
      case TARGET_TYPE_PRICE:
         // Direct price level
         return NormalizeDouble(tp_value, digits);

      case TARGET_TYPE_PIPS:
         {
            double tp_distance = tp_value * point;
            double tp_price = 0;

            if(is_buy)
               tp_price = entry_price + tp_distance;
            else
               tp_price = entry_price - tp_distance;

            return NormalizeDouble(tp_price, digits);
         }

      case TARGET_TYPE_PERCENTAGE:
         {
            double tp_percentage = tp_value / 100.0;
            double tp_price = 0;

            if(is_buy)
               tp_price = entry_price * (1 + tp_percentage);
            else
               tp_price = entry_price * (1 - tp_percentage);

            return NormalizeDouble(tp_price, digits);
         }
   }

   return 0;
}
//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   // Phase 4: Initialize production logger first (for validation logging)
   g_prodLogger = new CProductionLogger("PineTunnel", false); // Production mode: verbose=false

   // Phase 4: Validate critical inputs before proceeding
   g_inputValidator = new CInputValidator(g_prodLogger);

   bool inputsValid = true;

   // Validate License ID
   if(!g_inputValidator.ValidateLicenseID(InpLicenseID))
      inputsValid = false;

   // Validate Server URL
   if(!g_inputValidator.ValidateURL(InpServerURL, "Server URL"))
      inputsValid = false;

   // Validate numeric ranges for critical inputs
   if(!g_inputValidator.ValidateRange(InpRisk, 0.01, 100.0, "Risk"))
      inputsValid = false;

   if(!g_inputValidator.ValidateRange(InpStopLoss, 0.0, 10000.0, "Stop Loss"))
      inputsValid = false;

   if(!g_inputValidator.ValidateRange(InpTakeProfit, 0.0, 10000.0, "Take Profit"))
      inputsValid = false;

   if(!g_inputValidator.ValidateRangeInt(InpPollInterval, 100, 60000, "Poll Interval"))
      inputsValid = false;

   if(!g_inputValidator.ValidateRangeInt(InpMaxSlippage, 0, 1000, "Max Slippage"))
      inputsValid = false;

   if(!g_inputValidator.ValidateRange(InpDefaultLots, 0.01, 999999.0, "Default Lots"))
      inputsValid = false;

   // Validate time strings
   if(!g_inputValidator.ValidateTimeString(InpStartTime, "Start Time"))
      inputsValid = false;

   if(!g_inputValidator.ValidateTimeString(InpEndTime, "End Time"))
      inputsValid = false;

   if(!inputsValid)
   {
      g_prodLogger.Error("Input validation failed - EA initialization aborted");
      SafeDelete(g_prodLogger);
      SafeDelete(g_inputValidator);
      return INIT_FAILED;
   }

   // Map new inputs to backward compatibility variables
   InpPartialClosePct = (double)InpPartialClosePercentage;  // Convert enum to double
   InpDashFontSize = InpFontSize;

   // Map pending entry type to internal pending type
   if(InpPendingOrderEntry == PENDING_PIPS_FROM_MARKET)
      InpPendingType = PENDING_PIPS;
   else if(InpPendingOrderEntry == PENDING_PRICE_FROM_SIGNAL)
      InpPendingType = PENDING_PRICE;
   else if(InpPendingOrderEntry == PENDING_PERCENT_FROM_MARKET)
      InpPendingType = PENDING_PERCENT;

   // Map target type enum from PineTunnel-style to our internal enum
   ENUM_TARGET_TYPE targetType;
   if(InpTargetType == TARGET_TYPE_PIPS)
      targetType = TARGET_TYPE_PIPS;
   else if(InpTargetType == TARGET_TYPE_PRICE)
      targetType = TARGET_TYPE_PRICE;
   else
      targetType = TARGET_TYPE_PERCENTAGE;

   // Convert magic number enum to int for consistent comparisons (needed before log)
   g_magicNumber = (int)InpMagicNumber;

   // Phase 4: Production-optimized startup logging
   g_prodLogger.Info("v" + PT_VERSION + " | Lic: " + InpLicenseID + " | " + EnumToString(InpConnectionMode) + " | Magic: " + IntegerToString(g_magicNumber));

   // Validate hidden offset
   if(InpHiddenSLTP == HIDDEN_ON && (InpHiddenOffset < 50.0 || InpHiddenOffset > 500.0))
   {
      g_prodLogger.Warning("Hidden SL/TP offset " + DoubleToString(InpHiddenOffset, 0) +
                         " outside recommended range (50-500 pips)");
   }

   // Phase 4: Initialize production hardening components
   g_orderManager = new COrderExecutionManager(&g_trade, g_prodLogger);
   g_priceValidator = new CPriceFeedValidator(g_prodLogger);
   g_limitOrderValidator = new CLimitOrderValidator(g_prodLogger);
   g_connectionManager = new CConnectionManager(g_prodLogger);
   g_memoryMonitor = new CMemoryMonitor(g_prodLogger);

   // Configure price validation parameters
   g_priceValidator.SetValidationParams(PH_MAX_SPREAD_POINTS, PH_MAX_PRICE_AGE_SECONDS, PH_MAX_PRICE_DEVIATION_PCT);

   if(InpLicenseID == "")
   {
      g_prodLogger.Error("License ID is required");
      return INIT_FAILED;
   }

   // V7.00: Initialize state file for crash recovery
   g_stateFilePath = "PineTunnel_" + InpLicenseID + "_state.txt";

   // Validate state file path by testing write access
   int testHandle = FileOpen(g_stateFilePath, FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(testHandle == INVALID_HANDLE)
   {
      PrintFormat("[PineTunnel] CRITICAL: Cannot write to state file: %s", g_stateFilePath);
      PrintFormat("[PineTunnel] Error: %d - Check file permissions and terminal directory", GetLastError());
      g_prodLogger.Error("State file write test failed - crash recovery unavailable");
      // Continue anyway - LoadExecutedSignals will handle missing file gracefully
   }
   else
   {
      FileClose(testHandle);
   }

   // V7.06: Clean up stale lock files on startup (transient files - safe to delete all)
   {
      string search_result = "";
      long search_handle = FileFindFirst("PineTunnel_lock_*.lock", search_result);
      if(search_handle != INVALID_HANDLE)
      {
         int cleaned = 0;
         do {
            if(FileDelete(search_result))
               cleaned++;
         } while(FileFindNext(search_handle, search_result));
         FileFindClose(search_handle);
         if(cleaned > 0)
            PrintFormat("[PineTunnel] Cleaned %d stale lock file(s) on startup", cleaned);
      }
   }

   // Prevent Windows from sleeping while EA is running
   CPTWebSocketClient::PreventSleep();

   // Detect if running on a VPS
   {
      PTWS_VpsInfo vpsInfo;
      ZeroMemory(vpsInfo);
      if(CPTWebSocketClient::GetVpsInfo(vpsInfo))
      {
         g_isVps = (vpsInfo.is_vps == 1);
         g_vpsProvider = CharArrayToString(vpsInfo.provider);
      }

   }

   LoadExecutedSignals();  // Restore executed signals from previous session

   // Initialize position sizer
   g_positionSizer = new CPositionSizer(InpCommission, InpRoundDown);

   // Initialize pending order manager
   g_pendingManager = new CPendingOrderManager(&g_trade, g_magicNumber);
   g_pendingManager.SetLogging(InpLogSignals);

   // Initialize partial close manager
   g_partialManager = new CPartialCloseManager(&g_trade, g_magicNumber, InpPartialClosePct);
   g_partialManager.SetLogging(InpLogSignals);

   // Initialize order modification manager
   g_modifyManager = new COrderModificationManager(&g_trade, g_magicNumber, targetType);
   g_modifyManager.SetLogging(InpLogSignals);

   // Initialize combined actions manager
   g_combinedManager = new CCombinedActionsManager(&g_trade, g_magicNumber);
   g_combinedManager.SetLogging(InpLogSignals);

   // Initialize signal queue for market-open delay handling
   if(InpEnableSignalQueue)
   {
      g_signalQueue = new CSignalQueue();
   }

   // Setup trade object
   g_trade.SetExpertMagicNumber(g_magicNumber);
   g_trade.SetDeviationInPoints(InpMaxSlippage);
   g_trade.SetTypeFilling(ORDER_FILLING_FOK);
   g_trade.SetAsyncMode(false);

   // Test connection
   g_connectedSince = TimeCurrent();
   // Test initial connection
   g_lastPoll = 0;

   // Apply pending EA update from previous session (swaps _new.ex5 -> .ex5)
   CheckPendingEAUpdate();

   //--- Phase 2: WebSocket connection attempt ---
   // Connection mode controls transport behavior:
   //   WEBSOCKET: WSS only, no HTTP fallback. EA stops if WS fails.
   //   LONGPOLL:  HTTPS long-poll only, no WS attempt.
   //   HYBRID:    WSS primary, automatic HTTP fallback on disconnect.
   if(InpConnectionMode != CONNECTION_MODE_LONGPOLL)
   {
      g_wsClient = new CPTWebSocketClient();
      if(g_wsClient != NULL)
      {
         if(g_wsClient.IsConnected() || g_wsClient.Connect(InpServerURL, 443, true, InpLicenseID))
         {
            g_useWebSocket = true;
            g_wsReconnectAttempts = 0;
            g_wsConnectingTicks = 0;
            g_wsClient.SetHeartbeat(InpWSHeartbeatSec);
            // Register chart window for PostMessage wake-up - DLL posts WM_USER+1
            // on frame arrival so OnChartEvent fires instantly instead of waiting for OnTimer.
            long chartWnd = ChartGetInteger(0, CHART_WINDOW_HANDLE);
            if(chartWnd != 0)
               g_wsClient.SetNotifyWindow(chartWnd, PTWS_NOTIFY_MSG_BASE + 1);
            CPTWebSocketClient::SetLogLevel(InpWSLogLevel);
            PrintFormat("[PineTunnel] WSS enabled (mode=%s, heartbeat=%ds, poll=%dms)",
                        EnumToString(InpConnectionMode), InpWSHeartbeatSec, InpWSPollIntervalMs);
         }
         else
         {
            if(InpConnectionMode == CONNECTION_MODE_WEBSOCKET)
            {
               Print("[PineTunnel] WSS-only mode: connect failed - EA will retry in OnTimer");
               g_useWebSocket = false;
            }
            else
            {
               PrintFormat("[PineTunnel] WSS connect failed - falling back to HTTPS polling (attempt %d)", g_wsReconnectAttempts);
               g_useWebSocket = false;
            }
            // Keep g_wsClient alive for reconnect attempts in OnTimer
         }
      }
      else
      {
         if(InpConnectionMode == CONNECTION_MODE_WEBSOCKET)
            Print("[PineTunnel] WSS-only mode: DLL not available - will retry in OnTimer");
         else
            Print("[PineTunnel] WSS DLL not available - using HTTPS polling");
         g_useWebSocket = false;
      }
   }
   else
   {

      g_useWebSocket = false;
   }

   // Initialize Account Protection tracking
   g_cumulativeStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   // Note: g_dailyStartBalance and g_dailyResetTime are initialized in CheckDailyReset()
   // Do NOT set them here, as CheckDailyReset() needs g_dailyResetTime == 0 to detect first run
   g_protectionHalted = false;
   g_dailyHalted = false;

   // Initialize watchdog tracking
   g_lastSuccessfulRequest = TimeCurrent();  // Prevent false watchdog trigger on startup
   g_consecutiveErrors = 0;

   // Set EA start time for uptime tracking
   g_eaStartTime = TimeCurrent();

   EventSetMillisecondTimer(InpWSPollIntervalMs); // Timer at WS poll interval for fast signal processing

   if(InpShowDashboard)
   {
      DrawDashboard();
   }

   // Initialize spread tracking from historical bars
   InitializeSpreadHistory();

   // Initialize daily tracking (load today's trades from history)
   CheckDailyReset();

    // Auto-update and audit deferred to OnTimer (avoid blocking OnInit with RunNetworkDiag)
    g_lastVersionCheck = 0;  // Force first-tick update check
    g_lastAuditSent = 0;     // Force first-tick audit send

    return(INIT_SUCCEEDED);
}
//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   RemoveDashboard();

   // Restore normal Windows sleep behavior
   CPTWebSocketClient::AllowSleep();
   // Sleep prevention disabled - silent
   // Apply pending EA update if downloaded
   ApplyPendingUpdate();

   //--- Phase 2: WebSocket cleanup --
   if(g_wsClient != NULL)
   {
      // Dump DLL logs to EA journal if logging was enabled
      if(InpWSLogLevel >= 1)
      {
         int logCount = CPTWebSocketClient::GetLogCount();
         int maxLog = MathMin(logCount, 50);  // Dump last 50 entries max
         if(maxLog > 0)
         {
            PrintFormat("[PineTunnel] DLL log dump (%d entries):", maxLog);
            for(int i = MathMax(0, logCount - maxLog); i < logCount; i++)
            {
               string logEntry;
               if(CPTWebSocketClient::GetLogEntry(i, logEntry))
                  PrintFormat("[PineTunnel] DLL[%d]: %s", i, logEntry);
            }
         }
      }
      g_wsClient.Disconnect();
      // CRITICAL: Give DLL background threads time to exit before destroying the client.
      // WinHTTP callbacks run in a thread pool; if we delete the client while a callback
      // is still running, it causes use-after-free and "Abnormal termination".
      Sleep(1000);
      g_wsClient.Reset();
      delete g_wsClient;
      g_wsClient = NULL;
      g_useWebSocket = false;
      g_wsInitialStateSent = false;

   }

   // Phase 4: Log memory statistics before cleanup
   if(g_memoryMonitor != NULL)
   {
      g_memoryMonitor.LogStatistics();
   }

   // Phase 4: SafeDelete pattern for all heap objects
   SafeDelete(g_positionSizer);
   SafeDelete(g_pendingManager);
   SafeDelete(g_partialManager);
   SafeDelete(g_modifyManager);
   SafeDelete(g_combinedManager);
   SafeDelete(g_signalQueue);

   // Phase 4: SafeDelete for production hardening objects
   SafeDelete(g_orderManager);
   SafeDelete(g_priceValidator);
   SafeDelete(g_limitOrderValidator);
   SafeDelete(g_connectionManager);
   SafeDelete(g_inputValidator);
   SafeDelete(g_memoryMonitor);

   // V7.04: Clean up cross-instance signal locks owned by this instance
   {
      string search_result = "";
      long search_handle = FileFindFirst("PineTunnel_lock_*.lock", search_result);
      if(search_handle != INVALID_HANDLE)
      {
         string our_id = _Symbol + "_" + IntegerToString(ChartID());
         int cleaned = 0;
         do {
            // Read the lock file to check if it's ours
            int lock_handle = FileOpen(search_result, FILE_READ|FILE_TXT|FILE_ANSI);
            if(lock_handle != INVALID_HANDLE)
            {
               string lock_owner = "";
               while(!FileIsEnding(lock_handle))
                  lock_owner += FileReadString(lock_handle);
               FileClose(lock_handle);

               // If this lock belongs to our chart, delete it
               if(StringFind(lock_owner, our_id) >= 0)
               {
                  FileDelete(search_result);
                  cleaned++;
                  PrintFormat("[PineTunnel] Cleaned up signal lock: %s", search_result);
               }
            }
         } while(FileFindNext(search_handle, search_result));
         FileFindClose(search_handle);
         if(cleaned > 0)
            PrintFormat("[PineTunnel] Cleaned %d signal lock file(s)", cleaned);
      }
   }

   string reason_text = "";
   switch(reason)
   {
      case REASON_PROGRAM:     reason_text = "Expert removed from chart"; break;
      case REASON_REMOVE:      reason_text = "Expert removed"; break;
      case REASON_RECOMPILE:   reason_text = "Expert recompiled"; break;
      case REASON_CHARTCHANGE: reason_text = "Symbol or timeframe changed"; break;
      case REASON_CHARTCLOSE:  reason_text = "Chart closed"; break;
      case REASON_PARAMETERS:  reason_text = "Parameters changed"; break;
      case REASON_ACCOUNT:     reason_text = "Account changed"; break;
      case REASON_TEMPLATE:    reason_text = "Template applied"; break;
      case REASON_INITFAILED:  reason_text = "Initialization failed"; break;
      case REASON_CLOSE:       reason_text = "Terminal closed"; break;
      default:                 reason_text = "Unknown reason " + IntegerToString(reason);
   }

   PrintFormat("[PineTunnel] EA deinitialized: %s", reason_text);

   // Phase 4: Final log with statistics
   if(g_prodLogger != NULL)
   {
      g_prodLogger.Info("EA stopped | Signals: " + IntegerToString(g_totalSignals) +
                       " | Success: " + IntegerToString(g_successful) +
                       " | Failed: " + IntegerToString(g_failed));
      SafeDelete(g_prodLogger);
   }
   else
   {
      PrintFormat("[PineTunnel] EA stopped | Signals: %d | Success: %d | Failed: %d",
                  g_totalSignals, g_successful, g_failed);
   }
}

//+------------------------------------------------------------------+
//| Expert tick function - Production hardening monitoring           |
//+------------------------------------------------------------------+
void OnTick()
{
   // Price validation - throttled to 1s
   if(g_priceValidator != NULL)
   {
      static datetime lastPriceCheck = 0;
      datetime now = TimeCurrent();
      if(now - lastPriceCheck >= 1)
      {
         lastPriceCheck = now;
         g_priceValidator.ValidatePriceFeed(Symbol(), false);
      }
   }

   // Process WS signals on every tick for sub-100ms latency
   if(g_useWebSocket && g_wsClient != NULL && g_wsClient.IsConnected())
      ProcessPendingWSSignals();
}
//--- helpers -------------------------------------------------------
string CommandName(const CommandType type)
{
   switch(type)
   {
      case COMMAND_BUY:         return "BUY";
      case COMMAND_SELL:        return "SELL";
      case COMMAND_CLOSE_ALL:   return "CLOSE_ALL";
      case COMMAND_CLOSE_LONG:  return "CLOSE_LONG";
      case COMMAND_CLOSE_SHORT: return "CLOSE_SHORT";
      case COMMAND_BUY_LIMIT:    return "BUY_LIMIT";
      case COMMAND_SELL_LIMIT:   return "SELL_LIMIT";
      case COMMAND_BUY_STOP:     return "BUY_STOP";
      case COMMAND_SELL_STOP:    return "SELL_STOP";
      case COMMAND_CANCEL_LONG:  return "CANCEL_LONG";
      case COMMAND_CANCEL_SHORT: return "CANCEL_SHORT";
      // Phase 2
      case COMMAND_CLOSE_LONG_PCT:    return "CLOSE_LONG_PCT";
      case COMMAND_CLOSE_SHORT_PCT:   return "CLOSE_SHORT_PCT";
      case COMMAND_CLOSE_LONG_VOL:    return "CLOSE_LONG_VOL";
      case COMMAND_CLOSE_SHORT_VOL:   return "CLOSE_SHORT_VOL";
      case COMMAND_SLTP__LONG:      return "SLTP_LONG";
      case COMMAND_SLTP__SHORT:     return "SLTP_SHORT";
      case COMMAND_SLTP_BUY_STOP:   return "SLTP_BUY_STOP";
      case COMMAND_SLTP_BUY_LIMIT:  return "SLTP_BUY_LIMIT";
      case COMMAND_SLTP_SELL_STOP:  return "SLTP_SELL_STOP";
      case COMMAND_SLTP_SELL_LIMIT: return "SLTP_SELL_LIMIT";
      // Priority 1&2
      case COMMAND_CLOSE_LONG_SHORT:   return "CLOSE_ALL";
      case COMMAND_EXIT:               return "EXIT";
      case COMMAND_CLOSE_LONG_OPEN_LONG:     return "CLOSE_LONG_BUY";
      case COMMAND_CLOSE_LONG_OPEN_SHORT:    return "CLOSE_LONG_SELL";
      case COMMAND_CLOSE_SHORT_OPEN_LONG:    return "CLOSE_SHORT_BUY";
      case COMMAND_CLOSE_SHORT_OPEN_SHORT:   return "CLOSE_SHORT_SELL";
      case COMMAND_CLOSE_LONGSHORT_OPEN_LONG:  return "CLOSE_ALL_BUY";
      case COMMAND_CLOSE_LONGSHORT_OPEN_SHORT: return "CLOSE_ALL_SELL";
      case COMMAND_CANCEL_LONG_BUY_STOP:    return "CANCEL_LONG_BUY_STOP";
      case COMMAND_CANCEL_LONG_BUY_LIMIT:   return "CANCEL_LONG_BUY_LIMIT";
      case COMMAND_CANCEL_SHORT_SELL_STOP:  return "CANCEL_SHORT_SELL_STOP";
      case COMMAND_CANCEL_SHORT_SELL_LIMIT: return "CANCEL_SHORT_SELL_LIMIT";
      case COMMAND_EA_OFF:             return "EA_OFF";
      case COMMAND_EA_ON:              return "EA_ON";
      case COMMAND_CLOSEALL_EA_OFF:    return "CLOSE_ALL_OFF";
      default:                  return "UNKNOWN";
   }
}
CommandType NormalizeCommand(string command)
{
    string cmd = command;
    StringToUpper(cmd);
    StringTrimLeft(cmd);
    StringTrimRight(cmd);

    // Market orders
   if(cmd == "BUY" || cmd == "LONG" || cmd == "BULL" || cmd == "BULLISH")
      return COMMAND_BUY;
   if(cmd == "SELL" || cmd == "SHORT" || cmd == "BEAR" || cmd == "BEARISH")
      return COMMAND_SELL;

   // Pending orders
   if(cmd == "BUY_LIMIT")
      return COMMAND_BUY_LIMIT;
   if(cmd == "SELL_LIMIT")
      return COMMAND_SELL_LIMIT;
   if(cmd == "BUY_STOP")
      return COMMAND_BUY_STOP;
   if(cmd == "SELL_STOP")
      return COMMAND_SELL_STOP;

   // Close commands
   if(cmd == "CLOSE_ALL" || cmd == "CLOSE" || cmd == "CA")
      return COMMAND_CLOSE_ALL;
   if(cmd == "CLOSE_LONG" || cmd == "CLOSE_BUY" || cmd == "CL")
      return COMMAND_CLOSE_LONG;
   if(cmd == "CLOSE_SHORT" || cmd == "CLOSE_SELL" || cmd == "CS")
      return COMMAND_CLOSE_SHORT;

   // Cancel commands
   if(cmd == "CANCEL_LONG" || cmd == "CANCELBUY")
      return COMMAND_CANCEL_LONG;
   if(cmd == "CANCEL_SHORT" || cmd == "CANCELSELL")
      return COMMAND_CANCEL_SHORT;

   // Phase 2: Partial close commands
   if(cmd == "CLOSE_LONG_PCT")
      return COMMAND_CLOSE_LONG_PCT;
   if(cmd == "CLOSE_SHORT_PCT")
      return COMMAND_CLOSE_SHORT_PCT;
   if(cmd == "CLOSE_LONG_VOL")
      return COMMAND_CLOSE_LONG_VOL;
   if(cmd == "CLOSE_SHORT_VOL")
      return COMMAND_CLOSE_SHORT_VOL;

   // Phase 2: Modification commands
   if(cmd == "SLTP_LONG")
      return COMMAND_SLTP__LONG;
   if(cmd == "SLTP_SHORT")
      return COMMAND_SLTP__SHORT;
   if(cmd == "SLTP_BUY_STOP")
      return COMMAND_SLTP_BUY_STOP;
   if(cmd == "SLTP_BUY_LIMIT")
      return COMMAND_SLTP_BUY_LIMIT;
   if(cmd == "SLTP_SELL_STOP")
      return COMMAND_SLTP_SELL_STOP;
   if(cmd == "SLTP_SELL_LIMIT")
      return COMMAND_SLTP_SELL_LIMIT;

   // Priority 1: close_all
   if(cmd == "CLOSE_ALL" || cmd == "CLS")
      return COMMAND_CLOSE_LONG_SHORT;

   // EXIT command - closes all positions (long+short) for symbol with comment filter
   if(cmd == "EXIT")
      return COMMAND_EXIT;

   // Combined actions - close+open
   if(cmd == "CLOSE_LONG_BUY" || cmd == "close_long_buy")
      return COMMAND_CLOSE_LONG_OPEN_LONG;
   if(cmd == "CLOSE_LONG_SELL" || cmd == "close_long_sell")
      return COMMAND_CLOSE_LONG_OPEN_SHORT;
   if(cmd == "CLOSE_SHORT_BUY" || cmd == "close_short_buy")
      return COMMAND_CLOSE_SHORT_OPEN_LONG;
   if(cmd == "CLOSE_SHORT_SELL" || cmd == "close_short_sell")
      return COMMAND_CLOSE_SHORT_OPEN_SHORT;
   if(cmd == "CLOSE_ALL_BUY" || cmd == "close_all_buy")
      return COMMAND_CLOSE_LONGSHORT_OPEN_LONG;
   if(cmd == "CLOSE_ALL_SELL" || cmd == "close_all_sell")
      return COMMAND_CLOSE_LONGSHORT_OPEN_SHORT;

   // Combined actions - cancel+place
   if(cmd == "CANCEL_LONG_BUY_STOP")
      return COMMAND_CANCEL_LONG_BUY_STOP;
   if(cmd == "CANCEL_LONG_BUY_LIMIT")
      return COMMAND_CANCEL_LONG_BUY_LIMIT;
   if(cmd == "CANCEL_SHORT_SELL_STOP")
      return COMMAND_CANCEL_SHORT_SELL_STOP;
   if(cmd == "CANCEL_SHORT_SELL_LIMIT")
      return COMMAND_CANCEL_SHORT_SELL_LIMIT;

   // EA Management
   if(cmd == "EA_OFF")
      return COMMAND_EA_OFF;
   if(cmd == "EA_ON")
      return COMMAND_EA_ON;
   if(cmd == "CLOSE_ALL_OFF")
      return COMMAND_CLOSEALL_EA_OFF;
   return COMMAND_NONE;
}
 double NormalizeLots(const string symbolName, const double lots)
 {
    if(!g_symbol.Name(symbolName))
       return lots;
    double minLot = g_symbol.LotsMin();
    double maxLot = g_symbol.LotsMax();
    double step   = g_symbol.LotsStep();

    if(lots <= 0)
       return 0.0;

    // Sub-minimum requested: reject, do NOT inflate (100x risk on stocks)
    if(lots < minLot)
    {
       PrintFormat("[PineTunnel] REJECT %s: volume %.4f < broker min %.2f (step %.2f). Increase lots or use risk-based sizing.",
                   symbolName, lots, minLot, step);
       return 0.0;
    }

    double result = lots;
    if(maxLot > 0 && result > maxLot) result = maxLot;
    if(step > 0)
       result = MathFloor(result / step + LOT_ROUNDING_EPSILON) * step;
    if(result < minLot) result = minLot;
    int vol_digits = 2;
   if(step > 0)
   {
      vol_digits = (int)MathCeil(-MathLog10(step));
      if(vol_digits < 0) vol_digits = 0;
      if(vol_digits > 8) vol_digits = 8;
   }
   return NormalizeDouble(result, vol_digits);
 }
//--- Simple JSON helpers ------------------------------------------------
string ExtractStringValue(const string json, const string key)
{
   string search = "\"" + key + "\":";
   int start = StringFind(json, search);
   if(start < 0) return "";
   start += StringLen(search);
   int len = StringLen(json);
   while(start < len &&
         (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '\t'))
      start++;
   if(start >= len || StringGetCharacter(json, start) != '"')
      return "";
   start++;
   int end = start;
   while(end < len)
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == '"' && (end == 0 || StringGetCharacter(json, end - 1) != '\\'))
         break;
      end++;
   }
   if(end >= len) return "";
   string raw = StringSubstr(json, start, end - start);
   StringReplace(raw, "\\\"", "\"");
   StringReplace(raw, "\\\\", "\\");
   return raw;
}
// V7.01: Helper function to validate if string is a valid number
bool IsValidNumberString(const string s)
{
   if(s == "") return false;
   bool has_digit = false;
   bool has_decimal = false;

   for(int i = 0; i < StringLen(s); i++)
   {
      ushort ch = StringGetCharacter(s, i);

      // Check for digit
      if(ch >= '0' && ch <= '9')
         has_digit = true;
      // Check for decimal point (only one allowed)
      else if(ch == '.')
      {
         if(has_decimal) return false;  // Multiple decimal points
         has_decimal = true;
      }
      // Allow leading minus or plus sign only at start
      else if((ch == '-' || ch == '+') && i == 0)
         continue;
      // Any other character is invalid
      else
         return false;
   }

   return has_digit;  // Must have at least one digit
}

// V7.01: Enhanced ExtractDoubleValue with validation flag
double ExtractDoubleValue(const string json, const string key)
{
   string search = "\"" + key + "\":";
   int start = StringFind(json, search);
   if(start < 0) return 0.0;

   start += StringLen(search);
   int len = StringLen(json);

   // Skip whitespace
   while(start < len && (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '\t'))
      start++;

   // Find end of number
   int end = start;
   while(end < len)
   {
      ushort ch = StringGetCharacter(json, end);
      if(ch == ',' || ch == '}' || ch == ' ' || ch == '\r' || ch == '\n')
         break;
      end++;
   }

   // Bounds check - if no valid number found, return 0 silently
   if(end < 0 || end <= start)
      return 0.0;

   string value = StringSubstr(json, start, end - start);

   // V7.01: Validate it's actually a number before parsing
   if(!IsValidNumberString(value))
   {
      PrintFormat("[PineTunnel] Invalid number format for key '%s': '%s'", key, value);
      return 0.0;
   }

   return StringToDouble(value);
}
SignalCommand ParseSignal(const string signal_json)
{
    SignalCommand cmd;
    cmd.type = COMMAND_NONE;
    cmd.signal_id = ExtractStringValue(signal_json, "signal_id");
    if(cmd.signal_id == "")
       cmd.signal_id = ExtractStringValue(signal_json, "id");

    string action = ExtractStringValue(signal_json, "action");
    if(action == "")
       action = ExtractStringValue(signal_json, "command");
    cmd.raw_action = action;

   // Extract symbol and apply prefix/suffix (except for special commands)
   string raw_symbol = ExtractStringValue(signal_json, "symbol");

   // Optimize special command check with single comparison for common case
   bool is_special = (raw_symbol == "ea_on" || raw_symbol == "ea_off" || raw_symbol == "close_all_off");
   cmd.symbol = is_special ? raw_symbol : TransformSymbol(raw_symbol);
   cmd.lots = InpDefaultLots;
   cmd.risk_percent = 0;
   cmd.stop_loss = 0;
   cmd.take_profit = 0;
   cmd.use_risk_sizing = false;
   cmd.comment = InpDefaultComment;  // Use configurable default (empty for PineTunnel compatibility)
   cmd.pending_distance = 0;
   cmd.has_pending = false;
   cmd.account_filter = 0;  // Initialize account filter
    cmd.partial_close_pct = 0;  // Initialize partial close pct (0 = use EA default)
    cmd.has_sl = false;          // Set to true when "sl" key found in signal JSON

    // Initialize explicit type fields to empty (use EA setting by default)
    cmd.vol_type = "";        // Empty = use EA VolumeType setting
    cmd.sl_type = "";         // Empty = use EA TargetType setting for SL
    cmd.tp_type = "";         // Empty = use EA TargetType setting for TP
    cmd.entry_type = "";      // Empty = use EA PendingType setting
    cmd.nm = false;           // Near-Market flag (set from nm=true param)

   // Normalize the command type (action was already extracted above)
   cmd.type = NormalizeCommand(action);

    // Extract risk parameters from signal
    double signal_lots = ExtractDoubleValue(signal_json, "lots");
    double signal_risk = ExtractDoubleValue(signal_json, "risk");
    double signal_sl = ExtractDoubleValue(signal_json, "sl");
    double signal_tp = ExtractDoubleValue(signal_json, "tp");
    cmd.has_sl = (StringFind(signal_json, "\"sl\":") >= 0);

   // Apply Input Setting Mode to determine which parameters to use
   double risk = 0;
   double sl = 0;
   double tp = 0;

   switch(InpSetting)
   {
      case SETTING_SIGNAL_PARAMS_ONLY:
         // Use all parameters from signal
         risk = signal_risk;
         sl = signal_sl;
         tp = signal_tp;
         break;

      case SETTING_EA_PARAMS_ONLY:
         // Use all parameters from EA inputs, ignore signal
         risk = InpRisk;
         sl = InpStopLoss;
         tp = InpTakeProfit;
         break;

      case SETTING_SLTP_EA_RISK_SIGNAL:
         // SL/TP from EA, Risk from signal (strict - no fallback)
         risk = signal_risk;  // Always use signal risk, even if 0
         sl = InpStopLoss;    // Always use EA's SL
         tp = InpTakeProfit;  // Always use EA's TP
         break;

      case SETTING_SLTP_SIGNAL_RISK_EA:
         // SL/TP from signal, Risk from EA (strict - no fallback)
         risk = InpRisk;     // Always use EA's risk
         sl = signal_sl;     // Always use signal SL, even if 0
         tp = signal_tp;     // Always use signal TP, even if 0
         break;

      default:
         // Fallback to signal parameters
         risk = signal_risk;
         sl = signal_sl;
         tp = signal_tp;
         break;
   }

   // Extract and validate comment (PineTunnel: max 20 chars, else blank)
   string comment = ExtractStringValue(signal_json, "comment");
   if(comment != "")
   {
      if(StringLen(comment) <= 20)
         cmd.comment = comment;
      else
      {
         if(InpLogSignals)
            PrintFormat("[PineTunnel] WARNING: Comment too long (%d chars) - max 20 allowed, setting to blank",
                       StringLen(comment));
         cmd.comment = "";  // PineTunnel spec: longer than 20 = blank
      }
   }

   // Extract pending parameter for pending orders
   double pending = ExtractDoubleValue(signal_json, "pending");
   if(pending > 0)
   {
      cmd.pending_distance = pending;
      cmd.has_pending = true;
   }

   // Extract account filter parameter
   double acc_filter = ExtractDoubleValue(signal_json, "acc_filter");
   cmd.account_filter = acc_filter;  // Store in command structure

   // Extract explicit type fields from JSON (PineTunnel explicit syntax parameters)
   // These override EA settings when present (non-empty)
    // vol_type: "lots", "dollar", "bal_loss", "eq_loss", "eq_margin"
   // sl_type: "pips", "price", "pct"
   // tp_type: "pips", "price", "pct"
    cmd.vol_type = ExtractStringValue(signal_json, "vol_type");
    cmd.sl_type = ExtractStringValue(signal_json, "sl_type");
    cmd.tp_type = ExtractStringValue(signal_json, "tp_type");
    cmd.entry_type = ExtractStringValue(signal_json, "entry_type");

   // Extract Near-Market flag from nm=true parameter
   string nm_str = ExtractStringValue(signal_json, "nm");
   cmd.nm = (nm_str == "true" || nm_str == "1" || nm_str == "True");

   // Extract partial close percentage for close_long_pct/close_short_pct commands
   cmd.partial_close_pct = ExtractDoubleValue(signal_json, "pct");

     // Store lots from signal first (InpSetting doesn't affect lots - only risk/sl/tp)
     cmd.lots = signal_lots > 0 ? signal_lots : cmd.lots;

     // Store the risk value appropriately based on volume type
     // The risk parameter interpretation depends on the Volume Type setting
     // When vol_type="lots" is present, signal_lots already has the correct value.
     // Preserve it when risk is absent (e.g. lots=0.57 with no "risk" key in JSON).

      // Signal vol_type overrides EA InpVolumeType setting
      bool use_lots_mode = (cmd.vol_type == "lots") || (cmd.vol_type == "" && InpVolumeType == VOLUME_LOTS);

      if(use_lots_mode)
      {
         // In LOTS mode, risk= directly means lot size
         // Use signal_lots when available (lots=), fall back to risk=, then default
         cmd.lots = risk > 0 ? risk : (cmd.lots > 0 ? cmd.lots : InpDefaultLots);
         cmd.risk_percent = 0;
         cmd.use_risk_sizing = false;
      }
    else
    {
       // In all other volume modes, risk= is used for calculation
       // Store as risk_percent (which will be used as the "risk value")
       cmd.risk_percent = risk;  // This stores the risk value for any volume type
       cmd.lots = 0;  // Will be calculated based on volume type
       cmd.use_risk_sizing = true;
    }

   // Always store SL and TP regardless of volume type
   cmd.stop_loss = sl;
   cmd.take_profit = tp;

    // Normalize lots if not using risk sizing
    if(!cmd.use_risk_sizing && cmd.symbol != "")
    {
       double norm = NormalizeLots(cmd.symbol, cmd.lots);
       if(norm <= 0.0)
       {
          PrintFormat("[PineTunnel] ERROR: Signal rejected - volume below broker minimum (signal_id: %s)", cmd.signal_id);
          cmd.type = COMMAND_NONE;
          return cmd;
       }
       cmd.lots = norm;
    }

   // V7.01: Validate required fields before processing signal
   if(cmd.signal_id == "")
   {
      PrintFormat("[PineTunnel] ERROR: Signal missing signal_id - rejecting");
      cmd.type = COMMAND_NONE;  // Mark as invalid
      return cmd;
   }

   // Check for special commands that don't require symbol
   bool is_special_command = (cmd.raw_action == "ea_on" || cmd.raw_action == "ea_off" ||
                               cmd.raw_action == "close_all_off" || cmd.symbol == "ea_on" ||
                               cmd.symbol == "ea_off" || cmd.symbol == "close_all_off");

   if(cmd.symbol == "" && !is_special_command)
   {
      PrintFormat("[PineTunnel] ERROR: Signal missing symbol - rejecting (signal_id: %s)", cmd.signal_id);
      cmd.type = COMMAND_NONE;  // Mark as invalid
      return cmd;
   }

   return cmd;
}
//--- dashboard -----------------------------------------------------
void RemoveDashboard()
{
   // Remove all table cells
   for(int row = 0; row < 4; row++)
   {
      for(int col = 0; col < 4; col++)
      {
         ObjectDelete(0, "PT_Cell_" + IntegerToString(row) + "_" + IntegerToString(col));
      }
   }

   // Remove header labels
   ObjectDelete(0, "PT_Header1");
   ObjectDelete(0, "PT_Header2");
   ObjectDelete(0, "PT_Header3");
   ObjectDelete(0, "PT_Header4");
   ObjectDelete(0, "PT_License");
   ObjectDelete(0, "PT_Connected");
   ObjectDelete(0, "PT_LastPoll");
   ObjectDelete(0, "PT_Signals");
   ObjectDelete(0, "PT_Success");
   ObjectDelete(0, "PT_Failed");
   ObjectDelete(0, "PT_AvgSpread");

   // New comprehensive info labels
   ObjectDelete(0, "PT_Balance");
   ObjectDelete(0, "PT_Equity");
   ObjectDelete(0, "PT_Positions");
   ObjectDelete(0, "PT_EAStatus");
   ObjectDelete(0, "PT_Row3Col4");

   ChartRedraw();
}
void DrawDashboard()
{
   if(!InpShowDashboard)
      return;

   // Update existing objects instead of recreating (prevents flicker)

   // Table structure with cells
   int num_cols = 4;
   int num_rows = 4;  // Header + 3 data rows
   int cell_width = 220;
   int cell_height = 28;
   int header_height = 32;
   int panel_width = cell_width * num_cols;
   int panel_height = header_height + (cell_height * 3);
   int cell_padding = 8;

   // Auto-center at bottom
   int chart_width = (int)ChartGetInteger(0, CHART_WIDTH_IN_PIXELS);
   int chart_height = (int)ChartGetInteger(0, CHART_HEIGHT_IN_PIXELS);
   int x = (chart_width - panel_width) / 2;  // Center horizontally
   int y = chart_height - panel_height - 30; // Bottom with margin

   // Create table cells with borders
   color cell_bg = C'21,23,34';     // #151722
   color cell_border = C'42,46,57'; // #2a2e39
   color header_bg = C'33,38,52';   // Slightly lighter for header

   // Helper function to create a cell
   for(int row = 0; row < num_rows; row++)
   {
      for(int col = 0; col < num_cols; col++)
      {
         string cell_name = "PT_Cell_" + IntegerToString(row) + "_" + IntegerToString(col);
         int cell_x = x + (col * cell_width);
         int cell_y = y + (row == 0 ? 0 : header_height + ((row - 1) * cell_height));
         int this_height = row == 0 ? header_height : cell_height;

         // Only create cell if it doesn't exist (prevents flicker)
         if(ObjectFind(0, cell_name) < 0)
         {
            ObjectCreate(0, cell_name, OBJ_RECTANGLE_LABEL, 0, 0, 0);
            ObjectSetInteger(0, cell_name, OBJPROP_XSIZE, cell_width);
            ObjectSetInteger(0, cell_name, OBJPROP_YSIZE, this_height);
            ObjectSetInteger(0, cell_name, OBJPROP_BGCOLOR, row == 0 ? header_bg : cell_bg);
            ObjectSetInteger(0, cell_name, OBJPROP_BORDER_TYPE, BORDER_FLAT);
            ObjectSetInteger(0, cell_name, OBJPROP_COLOR, cell_border);
            ObjectSetInteger(0, cell_name, OBJPROP_WIDTH, 1);
            ObjectSetInteger(0, cell_name, OBJPROP_BACK, false);
            ObjectSetInteger(0, cell_name, OBJPROP_SELECTABLE, false);
            ObjectSetInteger(0, cell_name, OBJPROP_HIDDEN, true);
         }

         // Always update position for chart resize handling
         ObjectSetInteger(0, cell_name, OBJPROP_XDISTANCE, cell_x);
         ObjectSetInteger(0, cell_name, OBJPROP_YDISTANCE, cell_y);

         // No merged cells
      }
   }

   // === TABLE CONTENT ===
   int col1_x = x + cell_padding;
   int col2_x = x + cell_width + cell_padding;
   int col3_x = x + (cell_width * 2) + cell_padding;
   int col4_x = x + (cell_width * 3) + cell_padding;

   // Header row (row 0) - centered in cells
   int header_y = y + 8;
   int col1_center = x + (cell_width / 2);
   int col2_center = x + cell_width + (cell_width / 2);
   int col3_center = x + (cell_width * 2) + (cell_width / 2);
   int col4_center = x + (cell_width * 3) + (cell_width / 2);

   // Headers
   CreateLabel("PT_Header1", "Signals", col1_center, header_y, InpDashFontSize, C'0,185,255', "Arial Bold");
   ObjectSetInteger(0, "PT_Header1", OBJPROP_ANCHOR, ANCHOR_UPPER);

   CreateLabel("PT_Header2", "Account", col2_center, header_y, InpDashFontSize, C'0,185,255', "Arial Bold");
   ObjectSetInteger(0, "PT_Header2", OBJPROP_ANCHOR, ANCHOR_UPPER);

   CreateLabel("PT_Header3", "Connection", col3_center, header_y, InpDashFontSize, C'0,185,255', "Arial Bold");
   ObjectSetInteger(0, "PT_Header3", OBJPROP_ANCHOR, ANCHOR_UPPER);

   CreateLabel("PT_Header4", "Status", col4_center, header_y, InpDashFontSize, C'0,185,255', "Arial Bold");
   ObjectSetInteger(0, "PT_Header4", OBJPROP_ANCHOR, ANCHOR_UPPER);

   // Data rows start after header
   int row1_y = y + header_height + 6;
   int row2_y = row1_y + cell_height;
   int row3_y = row2_y + cell_height;

   // === COLUMN 1: SIGNALS (Today's counts) ===
   CreateLabel("PT_Signals", "Today: " + IntegerToString(g_todaySignals), col1_x, row1_y, InpDashFontSize, clrWhite);
   CreateLabel("PT_Success", "Success: " + IntegerToString(g_todaySuccessful), col1_x, row2_y, InpDashFontSize, clrWhite);
   // Failed cell: Red if failed >= 1, white otherwise
   color failedColor = (g_todayFailed >= 1) ? C'255,0,81' : clrWhite;
   CreateLabel("PT_Failed", "Failed: " + IntegerToString(g_todayFailed), col1_x, row3_y, InpDashFontSize, failedColor);

   // === COLUMN 2: ACCOUNT ===
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   int total_positions = PositionsTotal();
   double pnl = equity - balance;

   CreateLabel("PT_Balance", StringFormat("PnL: $%.0f", pnl), col2_x, row1_y, InpDashFontSize, clrWhite);
   CreateLabel("PT_Equity", StringFormat("Eq: $%.0f", equity), col2_x, row2_y, InpDashFontSize, clrWhite);
   CreateLabel("PT_Positions", StringFormat("Pos: %d", total_positions), col2_x, row3_y, InpDashFontSize, clrWhite);

   // === COLUMN 3: CONNECTION ===
   datetime now = TimeCurrent();

   // Show WebSocket stats when connected, otherwise HTTP poll info
   if(g_wsClient != NULL && g_wsClient.IsConnected() && g_useWebSocket)
   {
      // WebSocket mode - show "WSS" with WS stats
      PTWS_ConnectionStats wsStats;
      if(g_wsClient.GetStats(wsStats))
      {
         string wsUptime = FormatUptime(wsStats.uptime_sec);
         CreateLabel("PT_Connected", "[*] WSS " + wsUptime, col3_x, row1_y, InpDashFontSize, C'0,230,118');

         // Bytes - show B/KB/MB with at least 1 decimal for KB
         string rxStr, txStr;
         if(wsStats.bytes_received < 1024)
            rxStr = IntegerToString(wsStats.bytes_received) + "B";
         else if(wsStats.bytes_received < 1048576)
            rxStr = StringFormat("%.1fKB", wsStats.bytes_received / 1024.0);
         else
            rxStr = StringFormat("%.1fMB", wsStats.bytes_received / 1048576.0);
         if(wsStats.bytes_sent < 1024)
            txStr = IntegerToString(wsStats.bytes_sent) + "B";
         else if(wsStats.bytes_sent < 1048576)
            txStr = StringFormat("%.1fKB", wsStats.bytes_sent / 1024.0);
         else
            txStr = StringFormat("%.1fMB", wsStats.bytes_sent / 1048576.0);

         string latencyStr = wsStats.ws_latency_ms > 0 ? StringFormat(" %dms", wsStats.ws_latency_ms) : "";
         CreateLabel("PT_LastPoll", "Rx:" + rxStr + " Tx:" + txStr + latencyStr, col3_x, row2_y, InpDashFontSize, C'0,185,255');

         // Queue depth + reconnect count
         string queueInfo = "Q:" + IntegerToString(wsStats.frames_queued);
         if(wsStats.frames_dropped > 0)
            queueInfo += " Drop:" + IntegerToString(wsStats.frames_dropped);
         if(wsStats.reconnect_count > 0)
            queueInfo += " Rc:" + IntegerToString(wsStats.reconnect_count);
         color queueColor = wsStats.frames_dropped > 0 ? C'255,165,0' : clrWhite;
         CreateLabel("PT_AvgSpread", queueInfo, col3_x, row3_y, InpDashFontSize, queueColor);
      }
      else
      {
         int uptime_seconds = (int)(now - g_connectedSince);
         string uptime = FormatUptime(uptime_seconds);
         CreateLabel("PT_Connected", "[*] WSS " + uptime, col3_x, row1_y, InpDashFontSize, C'0,230,118');
         CreateLabel("PT_LastPoll", "Connected", col3_x, row2_y, InpDashFontSize, C'0,185,255');
         CreateLabel("PT_AvgSpread", "", col3_x, row3_y, InpDashFontSize, clrWhite);
      }
   }
   else
   {
      // HTTP long-poll mode - show "Polling" with poll interval
      int uptime_seconds = (int)(now - g_connectedSince);
      string uptime = FormatUptime(uptime_seconds);

      string pollInfo;
      color pollColor;
      if(!g_lastPollSuccess)
      {
         CreateLabel("PT_Connected", "[*] Offline", col3_x, row1_y, InpDashFontSize, C'255,0,81');
         pollInfo = "Disconnected";
         pollColor = C'255,0,81';
      }
      else
      {
         CreateLabel("PT_Connected", "[*] Polling " + uptime, col3_x, row1_y, InpDashFontSize, C'255,185,0');
         int secondsAgo = (int)(now - g_lastPoll);
         string lastPollText = secondsAgo == 0 ? "now" : IntegerToString(secondsAgo) + "s ago";
         pollInfo = "Poll: " + lastPollText;
         pollColor = C'255,185,0';
      }

      CreateLabel("PT_LastPoll", pollInfo, col3_x, row2_y, InpDashFontSize, pollColor);

      if(g_spreadSamples > 0)
         CreateLabel("PT_AvgSpread", StringFormat("Spread: %.1f pts", g_avgSpread), col3_x, row3_y, InpDashFontSize, clrWhite);
      else
         CreateLabel("PT_AvgSpread", "", col3_x, row3_y, InpDashFontSize, clrWhite);
   }

   // === COLUMN 4: STATUS ===
   string licenseDisplay = StringLen(InpLicenseID) > 14 ? StringSubstr(InpLicenseID, 0, 11) + "..." : InpLicenseID;
   CreateLabel("PT_License", licenseDisplay, col4_x, row1_y, InpDashFontSize-1, clrWhite);

   // -- EA Update Status --
   string eaStatus;
   color  eaColor;
   if(g_eaJustUpdated)
   {
      eaStatus = "EA Updated";
      eaColor = C'0,230,118';       // Green: just updated this session
   }
   else if(g_updateDownloaded)
   {
      eaStatus = InpAutoRestart ? "Updating..." : "Restart to apply";
      eaColor = C'0,200,255';       // Blue: update downloaded
   }
   else if(g_updateAvailable)
   {
      eaStatus = "Update available";
      eaColor = C'255,165,0';       // Orange: update available
   }
   else if(!InpAutoUpdate)
   {
      eaStatus = "EA (auto-off)";
      eaColor = C'180,180,180';     // Gray: auto-update disabled
   }
   else if(g_lastVersionCheck == 0)
   {
      eaStatus = "EA (checking...)";
      eaColor = C'180,180,180';     // Gray: haven't checked yet
   }
   else
   {
      eaStatus = "EA Up to date";
      eaColor = C'0,230,118';       // Green: current
   }
   CreateLabel("PT_EAStatus", eaStatus, col4_x, row2_y, InpDashFontSize-1, eaColor);
   CreateLabel("PT_Row3Col4", "github.com/TheFractalyst/PineTunnel", col4_x, row3_y, InpDashFontSize-1, C'0,185,255');

   ChartRedraw();
}
void CreateLabel(string name, string text, int x, int y, int fontSize, color clr, string font = "Arial")
{
   // Create object only if it doesn't exist (prevents recreation flicker)
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_FONTSIZE, fontSize);
      ObjectSetString(0, name, OBJPROP_FONT, font);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   }

   // Always update dynamic data and position for chart resize handling
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
}
string FormatUptime(int seconds)
{
   int days = seconds / 86400;
   int hours = (seconds % 86400) / 3600;
   int mins = (seconds % 3600) / 60;
   int secs = seconds % 60;

   if(days > 0)
      return StringFormat("%dd %dh", days, hours);
   else if(hours > 0)
      return StringFormat("%dh %dm", hours, mins);
   else if(mins > 0)
      return StringFormat("%dm %ds", mins, secs);
   else
      return StringFormat("%ds", secs);
}
//+------------------------------------------------------------------+
//| Close positions matching filter criteria                         |
//| Returns: true if all matching positions closed successfully     |
//| Always uses instant market close per PineTunnel gold standard  |
//+------------------------------------------------------------------+
bool ClosePositions(const string symbolFilter, const ENUM_POSITION_TYPE typeFilter, const string closeComment = "", const bool nmClose = false)
{
   bool result = true;
   int total = PositionsTotal();
   int closed_count = 0;

    // Use aggressive limit close when opt-in enabled and nm flag is set.
    // Otherwise use instant market close (PineTunnel gold standard).
    // TP-modify exit removed: it gives zero commission saving (TP fills at taker rate),
    // adds 2s hold risk, blocks EA thread, and temporarily removes TP protection.
    bool useExitLimit = InpEnableExitLimit && InpEnableSmartMarket && nmClose;

    for(int i = total - 1; i >= 0; i--)
    {
       if(!g_position.SelectByIndex(i))
          continue;
       // Check magic number if restriction is on
       if(InpMagicRestriction == MAGIC_RESTRICT_ON && g_position.Magic() != g_magicNumber)
          continue;
       if(symbolFilter != "" && g_position.Symbol() != symbolFilter)
          continue;
       if(typeFilter != -1 && g_position.PositionType() != typeFilter)
          continue;
       // Check comment filter (use contains-match for broker modifications)
       if(closeComment != "")
       {
          string posComment = g_position.Comment();
          if(StringFind(posComment, closeComment) < 0)
             continue;
       }
       // Capture position details BEFORE closing (critical - position object becomes invalid after close)
       ulong ticket = g_position.Ticket();
       string symbol = g_position.Symbol();
       double volume = g_position.Volume();
       double open_price = g_position.PriceOpen();
       double position_profit = g_position.Profit() + g_position.Swap() + g_position.Commission();
       ENUM_POSITION_TYPE pos_type = g_position.PositionType();
       string position_type = (pos_type == POSITION_TYPE_BUY) ? "BUY" : "SELL";

       // Get close price BEFORE closing (current market price)
       double close_price = (pos_type == POSITION_TYPE_BUY) ?
                             SymbolInfoDouble(symbol, SYMBOL_BID) :
                             SymbolInfoDouble(symbol, SYMBOL_ASK);

        PrintFormat("[PineTunnel] Closing position: #%I64d | %s | %s | %.2f lots | P/L: $%.2f",
                    ticket, symbol, position_type, volume, position_profit);
        ulong close_start = GetTickCount64();

       bool closed = false;
       // V7.06: set true if a limit-exit cancel didn't confirm and no fill was recorded.
       // Forces the catch-all market close to be skipped - otherwise the still-live limit
       // could fill and create an orphan opposing position on hedging accounts.
       bool exit_aborted = false;

       // ========================================
       // AGGRESSIVE LIMIT EXIT (opt-in, maker rate)
       // ========================================
       if(useExitLimit)
       {
          // Place an opposite-direction limit order at a slight offset from market.
          // If it fills, we close the position at maker rate (commission saving).
          // Original TP/SL remain untouched - position is still protected.
          // On timeout: cancel the limit order and market close the position.
          SymbolSelect(symbol, true);
          double bid   = SymbolInfoDouble(symbol, SYMBOL_BID);
          double ask   = SymbolInfoDouble(symbol, SYMBOL_ASK);
          double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
          int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
          long   stopsLevel = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);

          if(bid <= 0 || ask <= 0 || point <= 0)
          {
             PrintFormat("[PineTunnel] [DIAG] Invalid price data for %s limit exit - Bid:%.5f Ask:%.5f | falling back to market",
                         symbol, bid, ask);
             if(g_orderManager != NULL)
                closed = g_orderManager.ClosePosition(ticket, symbol);
             else
                closed = g_trade.PositionClose(ticket);
             if(closed) { SendCloseReport(symbol, ticket, close_price, position_profit, FindSignalByTicket(ticket)); closed_count++; }
             else { result = false; }
             continue;
          }

          int effectivePoints = InpNearMarketPoints;
          if(stopsLevel > 0 && effectivePoints <= (int)stopsLevel)
             effectivePoints = (int)stopsLevel + 1;

          // BUY position -> SELL LIMIT above bid (we want a higher close price)
          // SELL position -> BUY LIMIT below ask (we want a lower close price)
          // These are marketable limits - close to market, likely to fill quickly.
          ENUM_ORDER_TYPE limit_type;
          double          limit_price;

          if(pos_type == POSITION_TYPE_BUY)
          {
             limit_price = NormalizeDouble(bid + (effectivePoints * point), digits);
             limit_type  = ORDER_TYPE_SELL_LIMIT;
          }
          else
          {
             limit_price = NormalizeDouble(ask - (effectivePoints * point), digits);
             limit_type  = ORDER_TYPE_BUY_LIMIT;
          }

          // Validate limit price before placement
          bool skipExitLimit = false;
          if(g_limitOrderValidator != NULL)
          {
             if(!g_limitOrderValidator.ValidateLimitPrice(symbol, limit_price, limit_type))
             {
                skipExitLimit = true;
                PrintFormat("[PineTunnel] Exit limit validation failed for #%I64d - using market close", ticket);
             }
          }

          if(!skipExitLimit)
          {
             PrintFormat("[PineTunnel] Limit exit #%I64d: placing %s @ %.5f (Bid:%.5f Ask:%.5f offset:%d pts) | %s",
                         ticket, EnumToString(limit_type), limit_price, bid, ask, effectivePoints,
                         pos_type == POSITION_TYPE_BUY ? "Long->Sell Limit above bid" : "Short->Buy Limit below ask");

             // Use raw OrderSend with correct filling mode (not g_trade which inherits FOK)
             ENUM_ORDER_TYPE_FILLING exitFillMode = GetBestFillingMode(symbol);
             MqlTradeRequest exit_req = {};
             MqlTradeResult  exit_res = {};
             exit_req.action       = TRADE_ACTION_PENDING;
             exit_req.symbol       = symbol;
             exit_req.volume       = volume;
             exit_req.type         = limit_type;
             exit_req.price        = limit_price;
             exit_req.type_filling  = exitFillMode;
             exit_req.type_time     = ORDER_TIME_DAY;
             exit_req.magic         = g_magicNumber;
             exit_req.comment       = "NM_EXIT_" + IntegerToString((int)ticket);

             bool placed = OrderSend(exit_req, exit_res);

              if(placed && (exit_res.retcode == TRADE_RETCODE_DONE || exit_res.retcode == TRADE_RETCODE_PLACED))
              {
                 ulong order_ticket = exit_res.order;
                 PrintFormat("[PineTunnel] Limit exit placed #%I64d (fill=%s) - waiting up to %dms...",
                             order_ticket, EnumToString(exitFillMode), InpExitLimitTimeoutMs);

                 ulong poll_start = GetTickCount64();
                 bool  limit_filled = false;

                  while(GetTickCount64() - poll_start < (ulong)InpExitLimitTimeoutMs)
                  {
                     if(IsStopped()) break;
                     // Guard: original position may have been closed by TP/SL during poll
                     if(!PositionSelectByTicket(ticket))
                    {
                       PrintFormat("[PineTunnel] Original position #%I64d already closed (TP/SL hit) during exit limit poll", ticket);
                       closed = true;
                       limit_filled = true;
                       // Clean up the limit order if still pending
                       g_trade.OrderDelete(order_ticket);
                       break;
                     }

                     if(!OrderSelect(order_ticket))
                     {
                        HistorySelect(TimeCurrent() - HISTORY_SEARCH_WINDOW_SEC, TimeCurrent() + 1);
                        ulong opposing_ticket = 0;
                       for(int di = HistoryDealsTotal() - 1; di >= 0; di--)
                       {
                          ulong deal_ticket = HistoryDealGetTicket(di);
                          if(HistoryDealGetInteger(deal_ticket, DEAL_ORDER) == order_ticket)
                          {
                             opposing_ticket = HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
                             break;
                          }
                       }

                       if(!PositionSelectByTicket(ticket))
                       {
                          // Netting account: limit fill auto-offset the position
                          closed = true;
                          PrintFormat("[PineTunnel] Limit exit filled for #%I64d - closed via netting offset", ticket);
                       }
                       else if(opposing_ticket > 0)
                       {
                          // Hedging account: limit fill created opposing position.
                          // Use CloseBy to net both at the limit fill price.
                          MqlTradeRequest cb_req = {};
                          MqlTradeResult  cb_res = {};
                          cb_req.action   = TRADE_ACTION_CLOSE_BY;
                          cb_req.position = ticket;
                          cb_req.position_by = opposing_ticket;
                          if(OrderSend(cb_req, cb_res))
                          {
                             closed = true;
                             PrintFormat("[PineTunnel] Limit exit filled for #%I64d - close-by executed, maker rate achieved!", ticket);
                          }
                          else
                          {
                             PrintFormat("[PineTunnel] CloseBy failed for #%I64d - market closing", ticket);
                             closed = g_trade.PositionClose(ticket);
                          }
                       }
                       else
                       {
                          PrintFormat("[PineTunnel] Limit exit #%I64d filled but no opposing deal found - market closing", ticket);
                          closed = g_trade.PositionClose(ticket);
                       }
                       limit_filled = true;
                break;
             }
             Sleep(CANCEL_POLL_INTERVAL_MS);
          }

                 if(!limit_filled)
                 {
                    // V7.06: Confirm cancel before market closing - otherwise a late
                    // limit fill during the cancel race creates an orphan opposing
                    // position on hedging accounts.
                    PrintFormat("[PineTunnel] Limit exit timeout for #%I64d - cancelling order", ticket);
                    g_trade.OrderDelete(order_ticket);

                    bool cancel_confirmed = WaitForOrderToLeavePool(order_ticket, CANCEL_CONFIRM_TIMEOUT_MS);
                    double late_price = 0;
                    ulong  late_pos   = 0;
                    bool   late_filled = FindOrderFillInHistory(order_ticket, late_price, late_pos);

                    if(late_filled)
                    {
                       // Limit filled during the cancel race - reconcile the same way
                       // the polling-success path does (see above).
                       if(!PositionSelectByTicket(ticket))
                       {
                          closed = true;
                          PrintFormat("[PineTunnel] Limit exit filled during cancel race for #%I64d - netted via offset", ticket);
                       }
                       else if(late_pos > 0)
                       {
                          MqlTradeRequest cb_req = {};
                          MqlTradeResult  cb_res = {};
                          cb_req.action      = TRADE_ACTION_CLOSE_BY;
                          cb_req.position    = ticket;
                          cb_req.position_by = late_pos;
                          if(OrderSend(cb_req, cb_res))
                          {
                             closed = true;
                             PrintFormat("[PineTunnel] Late-fill close-by executed for #%I64d (opposing #%I64d)",
                                         ticket, late_pos);
                          }
                          else
                          {
                             PrintFormat("[PineTunnel] Late-fill CloseBy failed for #%I64d - market close (orphan may remain)", ticket);
                             closed = g_trade.PositionClose(ticket);
                          }
                       }
                       else
                       {
                          PrintFormat("[PineTunnel] Late fill found but no position-id for #%I64d - market close", ticket);
                          closed = g_trade.PositionClose(ticket);
                       }
                    }
                    else if(!cancel_confirmed)
                    {
                       // Cancel not confirmed AND no fill - limit may still be live.
                       // Market closing now would orphan an opposing limit fill.
                       PrintFormat("[PineTunnel] CRITICAL: Limit exit cancel for #%I64d not confirmed within %dms and no fill in history. Aborting auto-close to prevent orphan opposing order. Manual review required.",
                                   ticket, CANCEL_CONFIRM_TIMEOUT_MS);
                       exit_aborted = true;
                       result       = false;
                    }
                    // else: cancel confirmed, no fill - fall through to catch-all market close
                 }

                 if(!closed && !exit_aborted)
                 {
                    closed = g_trade.PositionClose(ticket);
                 }
              }
              else
              {
                 uint rc = exit_res.retcode;
                 PrintFormat("[PineTunnel] Limit exit placement failed for #%I64d: %d - market closing", ticket, rc);
                 closed = g_trade.PositionClose(ticket);
              }
          }
          else
          {
             // Validation failed - fall back to market close
             if(g_orderManager != NULL)
                closed = g_orderManager.ClosePosition(ticket, symbol);
             else
                closed = g_trade.PositionClose(ticket);
          }
       }
       else
       {
          // Standard market close (default, fastest, most reliable)
          if(g_orderManager != NULL)
          {
             closed = g_orderManager.ClosePosition(ticket, symbol);
          }
          else
          {
             closed = g_trade.PositionClose(ticket);
           }
         }

        if(closed)
           PrintFormat("[Exec] [TIMING] Close position #%I64d: %I64ums | Sym=%s", ticket, (ulong)(GetTickCount64() - close_start), symbol);

         if(!closed)
        {
           uint retcode = g_trade.ResultRetcode();
           g_lastExecError = (int)retcode;
           string errorDesc = (g_limitOrderValidator != NULL) ?
                            g_limitOrderValidator.GetOrderErrorDescription(retcode) :
                            g_trade.ResultRetcodeDescription();

           if(g_prodLogger != NULL)
           {
              g_prodLogger.Error("Failed to close position #" + IntegerToString((int)ticket) + ": " + errorDesc);
           }
           else
           {
              PrintFormat("[PineTunnel] Failed to close position #%I64d: %d - %s",
                          ticket, retcode, errorDesc);
           }

           PrintFormat("[PineTunnel] [DIAG] Close failure: Ticket=#%I64d | Symbol=%s | Type=%s | Volume=%.2f | OpenPrice=%.5f | P/L=$%.2f | Chart=%s",
                       ticket, symbol, position_type, volume, open_price, position_profit, _Symbol);
           PrintFormat("[PineTunnel] [DIAG] Close mode: ExitLimit=%s | Retcode=%d | LastError=%d",
                       (useExitLimit ? "YES" : "NO"), retcode, GetLastError());

           if(retcode == 10044)
           {
              PrintFormat("[PineTunnel] CRITICAL: Trading is not allowed on symbol %s", symbol);
               PrintFormat("[PineTunnel] Note: This is likely a simulation symbol with trading restrictions");
           }

           result = false;
        }
        else
        {
           if(g_prodLogger != NULL)
           {
              g_prodLogger.Info("Closed position #" + IntegerToString((int)ticket) +
                              " | P/L: $" + DoubleToString(position_profit, 2));
           }
           else
           {
              PrintFormat("[PineTunnel] Closed position #%I64d | P/L: $%.2f",
                          ticket, position_profit);
           }

           SendCloseReport(symbol, ticket, close_price, position_profit, FindSignalByTicket(ticket));
           closed_count++;
        }
    }

    return result;
}
//+------------------------------------------------------------------+
//| Check if pyramiding rules allow opening a new position          |
//+------------------------------------------------------------------+
bool CanOpenPositionPyramiding(string symbol, ENUM_POSITION_TYPE position_type)
{
   switch(InpPyramiding)
   {
      case PYRAMIDING_ON:
         // No restrictions - always allow
         return true;

      case PYRAMIDING_ON_IF_PROFIT:
         // Only open if existing positions for same symbol+direction are in profit
         {
            double total_profit = 0.0;
            int position_count = 0;

            for(int i = PositionsTotal() - 1; i >= 0; i--)
            {
               if(!g_position.SelectByIndex(i)) continue;

               // Check magic number if restriction is on
               if(InpMagicRestriction == MAGIC_RESTRICT_ON && g_position.Magic() != g_magicNumber)
                  continue;

               // Check symbol and direction
               if(g_position.Symbol() == symbol && g_position.PositionType() == position_type)
               {
                  total_profit += g_position.Profit() + g_position.Swap() + g_position.Commission();
                  position_count++;
               }
            }

            if(position_count == 0)
            {
               // No existing positions - allow
               return true;
            }
            else if(total_profit > 0)
               return true;
            else
            {
               PrintFormat("[PineTunnel] Pyramiding: %d existing %s position(s) NOT in profit ($%.2f) - BLOCKING new position",
                          position_count, position_type == POSITION_TYPE_BUY ? "BUY" : "SELL", total_profit);
               return false;
            }
         }

      case PYRAMIDING_OFF_EITHER_OR:
         // Only 1 position per symbol (buy OR sell, not both)
         {
            for(int i = PositionsTotal() - 1; i >= 0; i--)
            {
               if(!g_position.SelectByIndex(i)) continue;

               // Check magic number if restriction is on
               if(InpMagicRestriction == MAGIC_RESTRICT_ON && g_position.Magic() != g_magicNumber)
                  continue;

               // Check if we have ANY position for this symbol
               if(g_position.Symbol() == symbol)
               {
                  PrintFormat("[PineTunnel] Pyramiding: Already have %s position on %s - BLOCKING new %s",
                             g_position.PositionType() == POSITION_TYPE_BUY ? "BUY" : "SELL",
                             symbol,
                             position_type == POSITION_TYPE_BUY ? "BUY" : "SELL");
                  return false;
               }
            }
            return true;  // No positions found - allow
         }

      case PYRAMIDING_OFF_BOTH:
         // Allow 1 buy AND 1 sell per symbol (hedging mode)
         {
            for(int i = PositionsTotal() - 1; i >= 0; i--)
            {
               if(!g_position.SelectByIndex(i)) continue;

               // Check magic number if restriction is on
               if(InpMagicRestriction == MAGIC_RESTRICT_ON && g_position.Magic() != g_magicNumber)
                  continue;

               // Check if we have a position for this symbol+direction
               if(g_position.Symbol() == symbol && g_position.PositionType() == position_type)
               {
                  PrintFormat("[PineTunnel] Pyramiding: Already have %s position on %s - BLOCKING new %s",
                             position_type == POSITION_TYPE_BUY ? "BUY" : "SELL",
                             symbol,
                             position_type == POSITION_TYPE_BUY ? "BUY" : "SELL");
                  return false;
               }
            }
            return true;  // No matching position found - allow
         }
   }

   return true;  // Default: allow
}
//+------------------------------------------------------------------+
//| Execute Close on Reverse logic                                   |
//+------------------------------------------------------------------+
bool ExecuteCloseOnReverse(const SignalCommand &cmd, ENUM_POSITION_TYPE opposite_type)
{
   if(InpCloseOnReverse == CLOSE_REVERSE_OFF)
      return true;  // No close on reverse - continue normally

   bool has_opposite = false;

   // Prepare comment match for filtering
   string matchComment = cmd.comment;

   // Check if we have opposite positions
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!g_position.SelectByIndex(i)) continue;

      // Check magic number if restriction is on
      if(InpMagicRestriction == MAGIC_RESTRICT_ON && g_position.Magic() != g_magicNumber)
         continue;

      if(g_position.Symbol() == cmd.symbol && g_position.PositionType() == opposite_type)
      {
         // Comment filter - only detect opposite positions from same strategy
         if(matchComment != "" && StringFind(g_position.Comment(), matchComment) < 0)
            continue;
         has_opposite = true;
         break;
      }
   }

   if(!has_opposite)
      return true;  // No opposite positions - continue normally

   // We have opposite positions - handle based on mode
   switch(InpCloseOnReverse)
   {
      case CLOSE_REVERSE_ON_HEDGING:
         // Close opposite positions, then allow new position to open
         PrintFormat("[PineTunnel] Close on Reverse (HEDGING): Closing %s positions, will open %s",
                    opposite_type == POSITION_TYPE_BUY ? "BUY" : "SELL",
                    cmd.type == COMMAND_BUY ? "BUY" : "SELL");
         if(!ClosePositions(cmd.symbol, opposite_type, cmd.comment, cmd.nm))
         {
            PrintFormat("[PineTunnel] Close on Reverse FAILED - not opening new position");
            return false;
         }
         return true;  // Allow new position to open

      case CLOSE_REVERSE_ON_NETTING:
         // Close opposite positions, DON'T open new position
         PrintFormat("[PineTunnel] Close on Reverse (NETTING): Closing %s positions, NOT opening %s",
                    opposite_type == POSITION_TYPE_BUY ? "BUY" : "SELL",
                    cmd.type == COMMAND_BUY ? "BUY" : "SELL");
         ClosePositions(cmd.symbol, opposite_type, cmd.comment, cmd.nm);
         return false;  // Block new position from opening
   }

   return true;  // Default: allow
}
//+------------------------------------------------------------------+
//| Check maximum position limits                                    |
//+------------------------------------------------------------------+
bool CheckMaxPositionLimits(string symbol, ENUM_POSITION_TYPE position_type)
{
   // Early exit if no limits set
   if(InpMaxOpenPositions <= 0 && InpMaxOpenPositionsPerSymbol <= 0 && InpMaxUniqueSymbols <= 0)
      return true;  // No limits configured

   // Count current positions by symbol
   int total_positions = 0;
   int positions_on_symbol = 0;
   int unique_symbols_count = 0;
   string unique_symbols[];
   ArrayResize(unique_symbols, 0);
   ArraySetAsSeries(unique_symbols, false);  // Optimize array access

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!g_position.SelectByIndex(i)) continue;

      // Check magic number if restriction is on
      if(InpMagicRestriction == MAGIC_RESTRICT_ON && g_position.Magic() != g_magicNumber)
         continue;

      total_positions++;

      // Count positions on the requested symbol
      if(g_position.Symbol() == symbol)
         positions_on_symbol++;

      // Track unique symbols
      bool found = false;
      for(int j = 0; j < ArraySize(unique_symbols); j++)
      {
         if(unique_symbols[j] == g_position.Symbol())
         {
            found = true;
            break;
         }
      }
      if(!found)
      {
         ArrayResize(unique_symbols, ArraySize(unique_symbols) + 1);
         unique_symbols[ArraySize(unique_symbols) - 1] = g_position.Symbol();
      }
   }

   unique_symbols_count = ArraySize(unique_symbols);

   // Check 1: Maximum Open Positions (total across all symbols)
   if(InpMaxOpenPositions > 0 && total_positions >= InpMaxOpenPositions)
   {
      PrintFormat("[PineTunnel] Maximum Open Positions limit reached: %d / %d - BLOCKING new position",
                 total_positions, InpMaxOpenPositions);
      return false;
   }

   // Check 2: Maximum Open Positions per Symbol
   if(InpMaxOpenPositionsPerSymbol > 0 && positions_on_symbol >= InpMaxOpenPositionsPerSymbol)
   {
      PrintFormat("[PineTunnel] Maximum Positions per Symbol limit reached for %s: %d / %d - BLOCKING new position",
                 symbol, positions_on_symbol, InpMaxOpenPositionsPerSymbol);
      return false;
   }

   // Check 3: Maximum Unique Symbols
   // Only check if opening a position on a NEW symbol
   bool is_new_symbol = true;
   for(int i = 0; i < ArraySize(unique_symbols); i++)
   {
      if(unique_symbols[i] == symbol)
      {
         is_new_symbol = false;
         break;
      }
   }

   if(InpMaxUniqueSymbols > 0 && is_new_symbol && unique_symbols_count >= InpMaxUniqueSymbols)
   {
      PrintFormat("[PineTunnel] Maximum Unique Symbols limit reached: %d / %d - BLOCKING new symbol %s",
                 unique_symbols_count, InpMaxUniqueSymbols, symbol);
      return false;
   }

   // All checks passed
   return true;
}

ENUM_ORDER_TYPE_FILLING GetBestFillingMode(string symbol)
{
   long fillingMode = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);

   // SYMBOL_FILLING_MODE is a bitfield: FOK=1, IOC=2, BOC=8
   // Prefer BOC (guaranteed maker), then IOC (partial fill), then FOK
   if((fillingMode & 8) != 0)   // SYMBOL_FILLING_BOC
      return ORDER_FILLING_BOC;

   if((fillingMode & 2) != 0)   // SYMBOL_FILLING_IOC
      return ORDER_FILLING_IOC;

   return ORDER_FILLING_FOK;
}

//===================================================================
// V7.06: SIGNAL-ID DEDUP DEFENSE (LAYER 4 - broker-state reconciliation)
//===================================================================
// Belt-and-suspenders defense against the cross-instance race where two
// EA instances sharing a license key both pass the per-EA `IsSignalDuplicate`
// circular buffer AND the lock-file TOCTOU window, then both place an order.
//
// Layer 1 (server ACK)             - server-side, fastest path
// Layer 2 (lock file)               - cross-instance, has TOCTOU
// Layer 3 (executed_signals buffer) - per-instance, persistent
// Layer 4 (THIS)                    - broker state IS source of truth
//
// We embed the first 8 hex chars of signal_id as a prefix in the broker
// comment of every order we place. Before placing any new order, we scan
// open positions and pending orders on the same symbol for that prefix.
// 8 hex chars = 4.3 billion combinations - collision MTBF is ~12000 years
// at 1000 signals/day, so false-positive blocking is negligible.

// Builds the broker comment prefixed with sig8:<delim>. If signal_id is
// empty, returns user_comment unchanged. Output is capped at 31 chars
// to stay within MT5's broker comment limit on common brokers.
string BuildOrderComment(const string signal_id, const string user_comment)
{
   if(signal_id == "")
      return user_comment;

   string sig = StringSubstr(signal_id, 0, SIGNAL_ID_PREFIX_LENGTH);
   string out = sig + ":" + user_comment;

   if(StringLen(out) > BROKER_COMMENT_MAX_LENGTH)
      out = StringSubstr(out, 0, BROKER_COMMENT_MAX_LENGTH);

   return out;
}

// Reads the lock file contents and returns the owner info string.
// Returns empty string if file cannot be read.
string ReadLockFileContents(const string lockFile)
{
   int handle = FileOpen(lockFile, FILE_READ|FILE_TXT|FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return "";

   string contents = "";
   while(!FileIsEnding(handle))
   {
      string line = FileReadString(handle);
      if(contents == "")
         contents = line;
      else
         contents += line;
   }
   FileClose(handle);
   return contents;
}

// Returns true if any open position OR pending order on `symbol` already
// carries our signal_id prefix in its comment. Uses StringFind (substring,
// not prefix) so a broker that prepends modification text ("by stop", etc.)
// to the comment is still detected.
bool IsDuplicateSignalPosition(const string signal_id, const string symbol)
{
   if(signal_id == "" || symbol == "")
      return false;

   string sigKey = StringSubstr(signal_id, 0, SIGNAL_ID_PREFIX_LENGTH) + ":";

   // Open positions
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;
      string cmt = PositionGetString(POSITION_COMMENT);
      if(StringFind(cmt, sigKey) >= 0)
      {
         PrintFormat("[PineTunnel] LAYER-4 DUPLICATE BLOCKED: position #%I64d already exists for signal %s (sigKey=%s comment='%s')",
                     ticket, signal_id, sigKey, cmt);
         return true;
      }
   }

   // Pending orders
   for(int j = OrdersTotal() - 1; j >= 0; j--)
   {
      ulong ticket = OrderGetTicket(j);
      if(ticket == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != symbol) continue;
      string cmt = OrderGetString(ORDER_COMMENT);
      if(StringFind(cmt, sigKey) >= 0)
      {
         PrintFormat("[PineTunnel] LAYER-4 DUPLICATE BLOCKED: pending order #%I64d already exists for signal %s (sigKey=%s comment='%s')",
                     ticket, signal_id, sigKey, cmt);
         return true;
      }
   }

   return false;
}

//===================================================================
// V7.06: CANCEL-CONFIRMATION HELPERS
//===================================================================
// After OrderDelete, the broker may take 100-1500ms to confirm. During
// that window, the order can still fill. These helpers separate the
// concern of "is the order still alive?" from "did it fill?" so callers
// can make a safe decision: cancelled -> next stage; filled -> reconcile;
// neither -> abort and log (don't risk a duplicate or orphan).

// Polls every 50ms until the order leaves the pending pool, or until
// timeout_ms elapses. Returns true if the order is no longer pending.
bool WaitForOrderToLeavePool(ulong order_ticket, int timeout_ms)
{
   ulong start = GetTickCount64();
   while((long)(GetTickCount64() - start) < timeout_ms)
   {
      if(IsStopped()) break;
      if(!OrderSelect(order_ticket)) return true;
      Sleep(CANCEL_POLL_INTERVAL_MS);
   }
   return false;
}

// Searches the most recent 120s of deal history for a fill on order_ticket.
// Returns true if found, populating fill_price and fill_position_id (the
// DEAL_POSITION_ID, which is the position-level ticket needed for CloseBy).
bool FindOrderFillInHistory(ulong order_ticket, double &fill_price, ulong &fill_position_id)
{
   fill_price = 0;
   fill_position_id = 0;

   HistorySelect(TimeCurrent() - HISTORY_SEARCH_WINDOW_SEC, TimeCurrent() + 1);
   for(int di = HistoryDealsTotal() - 1; di >= 0; di--)
   {
      ulong deal_ticket = HistoryDealGetTicket(di);
      if(HistoryDealGetInteger(deal_ticket, DEAL_ORDER) == order_ticket)
      {
         ENUM_DEAL_TYPE dtype = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
         if(dtype == DEAL_TYPE_BUY || dtype == DEAL_TYPE_SELL)
         {
            fill_price       = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
            fill_position_id = HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
            return true;
         }
         break;  // first matching deal is authoritative
      }
   }

   if(HistoryOrderSelect(order_ticket))
   {
      ENUM_ORDER_STATE state = (ENUM_ORDER_STATE)HistoryOrderGetInteger(order_ticket, ORDER_STATE);
      if(state == ORDER_STATE_FILLED || state == ORDER_STATE_PARTIAL)
      {
         fill_price       = HistoryOrderGetDouble(order_ticket, ORDER_PRICE_OPEN);
         fill_position_id = (ulong)HistoryOrderGetInteger(order_ticket, ORDER_POSITION_ID);
         return true;
      }
   }

   return false;
}

ENUM_ORDER_DELETE_RESOLUTION ResolveOrderDelete(ulong order_ticket, int timeout_ms, double &fill_price, ulong &fill_position_id)
{
   fill_price = 0;
   fill_position_id = 0;

    ulong start = GetTickCount64();
    while((long)(GetTickCount64() - start) < timeout_ms)
    {
       if(IsStopped()) break;
       if(OrderSelect(order_ticket))
      {
         ENUM_ORDER_STATE active_state = (ENUM_ORDER_STATE)OrderGetInteger(ORDER_STATE);
         if(active_state == ORDER_STATE_FILLED)
         {
            fill_price       = OrderGetDouble(ORDER_PRICE_OPEN);
            fill_position_id = (ulong)OrderGetInteger(ORDER_POSITION_ID);
            return ORDER_DELETE_RESOLUTION_FILLED;
         }
         if(active_state == ORDER_STATE_CANCELED ||
            active_state == ORDER_STATE_REJECTED ||
            active_state == ORDER_STATE_EXPIRED)
            return ORDER_DELETE_RESOLUTION_CONFIRMED;
      }
      else
      {
         if(FindOrderFillInHistory(order_ticket, fill_price, fill_position_id))
            return ORDER_DELETE_RESOLUTION_FILLED;

         if(HistoryOrderSelect(order_ticket))
         {
            ENUM_ORDER_STATE history_state = (ENUM_ORDER_STATE)HistoryOrderGetInteger(order_ticket, ORDER_STATE);
            if(history_state == ORDER_STATE_CANCELED ||
               history_state == ORDER_STATE_REJECTED ||
               history_state == ORDER_STATE_EXPIRED)
               return ORDER_DELETE_RESOLUTION_CONFIRMED;
         }
      }

      Sleep(CANCEL_POLL_INTERVAL_MS);
   }

   return ORDER_DELETE_RESOLUTION_UNCERTAIN;
}

bool ExecuteMarketOrder(const SignalCommand &cmd)
{
   // ===================================================================
   // V7.06 LAYER-4: pre-flight broker-state reconciliation
   // Catches the residual cross-instance race that Layers 1-3 may miss.
   // ===================================================================
   if(IsDuplicateSignalPosition(cmd.signal_id, cmd.symbol))
      return true;  // Already handled by sibling instance - ack as success

   // ===================================================================
   // AUTO ORDER DETECTION: Check nm flag
   // ===================================================================

   bool convertToNearMarket = false;
   string cleanComment = cmd.comment;

   if(InpEnableSmartMarket && cmd.nm)
   {
      convertToNearMarket = true;
      // nm flag from signal parameter

      PrintFormat("[PineTunnel] Auto order detected - converting to near-market limit for price improvement");
   }

   // V7.06 LAYER-4: prefix signal_id into the broker comment so a sibling
   // EA's pre-flight scan can detect this position and short-circuit.
   cleanComment = BuildOrderComment(cmd.signal_id, cleanComment);
   // Determine position and order types
   ENUM_POSITION_TYPE position_type = (cmd.type == COMMAND_BUY) ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   ENUM_POSITION_TYPE opposite_type = (cmd.type == COMMAND_BUY) ? POSITION_TYPE_SELL : POSITION_TYPE_BUY;
   ENUM_ORDER_TYPE orderType = (cmd.type == COMMAND_BUY) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   // Check Close on Reverse FIRST (before pyramiding)
   if(!ExecuteCloseOnReverse(cmd, opposite_type))
   {
      // NETTING mode: positions closed, DON'T open new position
      return true;  // Return success since we closed positions as intended
   }

   // Check Pyramiding rules
   if(!CanOpenPositionPyramiding(cmd.symbol, position_type))
   {
      PrintFormat("[PineTunnel] Pyramiding rules blocked opening %s position on %s",
                 cmd.type == COMMAND_BUY ? "BUY" : "SELL", cmd.symbol);
      return false;
   }

   // Check Maximum Position Limits
   if(!CheckMaxPositionLimits(cmd.symbol, position_type))
   {
      // Error message already printed in CheckMaxPositionLimits
      return false;
   }

   if(!g_symbol.Name(cmd.symbol))
   {
      PrintFormat("[PineTunnel] Symbol %s not found", cmd.symbol);
      return false;
   }

   // Auto-enable symbol in Market Watch (hard-coded to always ON)
   // This ensures all symbols work without manual configuration
   if(!SymbolSelect(cmd.symbol, true))
   {
      PrintFormat("[PineTunnel] Failed to select symbol %s", cmd.symbol);
   }

    // Wait for price data to become available (newly added symbols need time)
    ulong price_start = GetTickCount64();
    double price = 0;
    int retries = 0;
    int max_retries = 10;  // 10 attempts (matches MT4)
    int wait_ms = 300;     // 300ms between attempts = max 3.0s total

   while(retries < max_retries && price <= 0)
   {
      // Refresh symbol data to get current prices
      if(g_symbol.Refresh() && g_symbol.RefreshRates())
      {
         price = (orderType == ORDER_TYPE_BUY) ? g_symbol.Ask() : g_symbol.Bid();
      }

      // Fallback: try direct symbol info functions
      if(price <= 0)
      {
         price = (orderType == ORDER_TYPE_BUY) ? SymbolInfoDouble(cmd.symbol, SYMBOL_ASK) : SymbolInfoDouble(cmd.symbol, SYMBOL_BID);
      }

      // If we got a valid price, break out of retry loop
      if(price > 0)
      {
         if(retries > 0)
         {
            PrintFormat("[PineTunnel] Price data received after %d retries (%.0fms)", retries, retries * wait_ms);
         }
         break;
      }

      // Wait before next attempt (only if we'll retry)
      if(retries < max_retries - 1)
      {
         Sleep(wait_ms);
         retries++;
      }
      else
      {
         retries++;  // Increment for final attempt
      }
    }

    PrintFormat("[Exec] [TIMING] Price fetch: %I64ums | retries=%d | Sym=%s", (ulong)(GetTickCount64() - price_start), retries, cmd.symbol);

    // Validate price with defensive check
   if(!MathIsValidNumber(price) || price <= 0)
   {
      PrintFormat("[PineTunnel] ERROR: Invalid price (%.5f) for %s after %d retries (%.1f seconds)",
                  price, cmd.symbol, retries, (retries * wait_ms) / 1000.0);
      PrintFormat("[PineTunnel] Market may be closed or symbol not available");
      return false;
   }
   double lots = InpDefaultLots;  // Initialize with default
   double sl_price = 0;
   double tp_price = 0;

   // Log the target type being used
   PrintFormat("[PineTunnel] Entry price: %.5f | SL value: %.5f | TP value: %.5f", price, cmd.stop_loss, cmd.take_profit);

   // Determine if this is a buy order
   bool is_buy = (cmd.type == COMMAND_BUY);

   // Calculate SL price using explicit type support (Requirements: 7.1)
   // This function checks if sl_type is set and uses explicit calculation,
   // otherwise falls back to EA TargetType setting
   sl_price = CalculateSLPriceWithExplicitType(cmd, price, is_buy);

   // If SL calculation returned 0 due to validation error (invalid direction), abort
   if(cmd.stop_loss > 0 && sl_price == 0)
   {
      PrintFormat("[PineTunnel] ERROR: SL calculation failed for %s (requested SL=%.5f). Aborting order.", cmd.symbol, cmd.stop_loss);
      return false;
   }

   // Calculate TP price using explicit type support (Requirements: 7.1)
   // This function checks if tp_type is set and uses explicit calculation,
   // otherwise falls back to EA TargetType setting
   tp_price = CalculateTPPriceWithExplicitType(cmd, price, is_buy);

   // Calculate volume using explicit type support (Requirements: 7.1)
   // This function checks if vol_type is set and uses explicit calculation,
   // otherwise falls back to EA VolumeType setting
   lots = CalculateVolumeWithExplicitType(cmd, price, sl_price, orderType);

   // Validation: Ensure we have a valid lot size
   if(lots <= 0)
   {
      PrintFormat("[PineTunnel] Volume calculation failed (lots=%.4f). ABORTING order.", lots);
      return false;
   }

   // Normalize SL/TP prices
   if(sl_price > 0) sl_price = NormalizeDouble(sl_price, g_symbol.Digits());
   if(tp_price > 0) tp_price = NormalizeDouble(tp_price, g_symbol.Digits());

    // Validate and auto-adjust SL/TP to broker minimum stops level
    long stopsLevel = SymbolInfoInteger(cmd.symbol, SYMBOL_TRADE_STOPS_LEVEL);
    double minDistance = stopsLevel * g_symbol.Point();

    if(sl_price > 0 && minDistance > 0)
    {
       double slDistance = MathAbs(price - sl_price);
       if(slDistance < minDistance)
       {
          sl_price = is_buy ? price - minDistance : price + minDistance;
          sl_price = NormalizeDouble(sl_price, g_symbol.Digits());
          PrintFormat("[PineTunnel] SL adjusted to broker min distance: %.5f (was %.5f, min %d points)",
                      sl_price, slDistance, stopsLevel);
       }
    }

    if(tp_price > 0 && minDistance > 0)
    {
       double tpDistance = MathAbs(price - tp_price);
       if(tpDistance < minDistance)
       {
          tp_price = is_buy ? price + minDistance : price - minDistance;
          tp_price = NormalizeDouble(tp_price, g_symbol.Digits());
          PrintFormat("[PineTunnel] TP adjusted to broker min distance: %.5f (was %.5f, min %d points)",
                      tp_price, tpDistance, stopsLevel);
       }
    }

   // Execute order with or without Hidden SL/TPs (offset SL/TP)
   double broker_sl = 0;
   double broker_tp = 0;

   if(InpHiddenSLTP == HIDDEN_OFF)
   {
      // Normal mode: send actual SL/TP to broker
      broker_sl = sl_price;
      broker_tp = tp_price;
   }
   else
   {
      // Hidden SL/TP mode: send OFFSET SL/TP to broker (100+ pips away)
      // EA monitors TRUE intended levels and closes when those are hit
      double hidden_offset_pips = InpHiddenOffset * g_symbol.Point();

      // Calculate offset SL/TP to send to broker (further away from intended)
      if(sl_price > 0)
      {
         if(orderType == ORDER_TYPE_BUY)
            broker_sl = sl_price - hidden_offset_pips;  // BUY: Place broker SL BELOW intended SL
         else
            broker_sl = sl_price + hidden_offset_pips;  // SELL: Place broker SL ABOVE intended SL
      }

      if(tp_price > 0)
      {
         if(orderType == ORDER_TYPE_BUY)
            broker_tp = tp_price + hidden_offset_pips;  // BUY: Place broker TP ABOVE intended TP
         else
            broker_tp = tp_price - hidden_offset_pips;  // SELL: Place broker TP BELOW intended TP
      }

      PrintFormat("[PineTunnel] Hidden SL/TPs ENABLED - Offset: %.0f pips", InpHiddenOffset);
      if(broker_sl > 0)
         PrintFormat("[PineTunnel] Broker SL: %.5f (Intended: %.5f | Offset: %.5f)",
                    broker_sl, sl_price, MathAbs(broker_sl - sl_price));
      if(broker_tp > 0)
         PrintFormat("[PineTunnel] Broker TP: %.5f (Intended: %.5f | Offset: %.5f)",
                    broker_tp, tp_price, MathAbs(broker_tp - tp_price));
   }

   // ===================================================================
   // AUTO ORDER CONVERSION: Convert to near-market limit if flag detected
   // ===================================================================

   bool result = false;

   // NM limit-fill tracking (used in post-trade block below)
   bool   nmFillByLimit = false;
   ulong  nmFillTicket  = 0;
   double nmFillPrice   = 0;
   // Set true if a pending-order cancel does NOT confirm before we would place
   // the next stage. Aborting prevents the duplicate-fill race where the prior
   // order is still live in the broker's book while we send a fresh limit.
   bool   nmAborted     = false;

    if(convertToNearMarket)
    {
       double ask   = SymbolInfoDouble(cmd.symbol, SYMBOL_ASK);
       double bid   = SymbolInfoDouble(cmd.symbol, SYMBOL_BID);
       double point = SymbolInfoDouble(cmd.symbol, SYMBOL_POINT);
       int    digits = (int)SymbolInfoInteger(cmd.symbol, SYMBOL_DIGITS);

       // Minimum broker-enforced offset to avoid "too close" rejection
        long entryStopsLevel = SymbolInfoInteger(cmd.symbol, SYMBOL_TRADE_STOPS_LEVEL);
         int  minValidPoints    = (entryStopsLevel > 0) ? (int)entryStopsLevel + 1 : 1;

        // ===================================================================
        // NEAR-MARKET LIMIT: Single-stage with polling
        // (BOC/IOC fast-path removed - broker ignores IOC flag, wastes ~100ms)
        // ===================================================================
        ENUM_ORDER_TYPE_FILLING bestFillMode = GetBestFillingMode(cmd.symbol);
        g_trade.SetTypeFilling(bestFillMode);

         if(!nmFillByLimit && !nmAborted)
         {
            // Refresh prices
            ask = SymbolInfoDouble(cmd.symbol, SYMBOL_ASK);
            bid = SymbolInfoDouble(cmd.symbol, SYMBOL_BID);

            ENUM_ORDER_TYPE limit_order_type;
            double          limit_price;

            if(cmd.type == COMMAND_BUY)
            {
               limit_price      = NormalizeDouble(ask - (minValidPoints * point), digits);
               limit_order_type = ORDER_TYPE_BUY_LIMIT;
               PrintFormat("[PineTunnel] NM Buy Limit: %.5f (Ask:%.5f offset:%d pts timeout:%dms)",
                           limit_price, ask, minValidPoints, InpEntryLimitTimeoutMs);
            }
            else
            {
               limit_price      = NormalizeDouble(bid + (minValidPoints * point), digits);
               limit_order_type = ORDER_TYPE_SELL_LIMIT;
               PrintFormat("[PineTunnel] NM Sell Limit: %.5f (Bid:%.5f offset:%d pts timeout:%dms)",
                           limit_price, bid, minValidPoints, InpEntryLimitTimeoutMs);
            }

            // Validate limit price before placement
            bool skipLimit = false;
            if(g_limitOrderValidator != NULL)
            {
               if(!g_limitOrderValidator.ValidateLimitPrice(cmd.symbol, limit_price, limit_order_type))
               {
                  skipLimit = true;
                  PrintFormat("[PineTunnel] NM limit validation failed - falling back to market");
               }
            }

            if(!skipLimit)
             {
                ulong os_start = GetTickCount64();
                bool placed = g_trade.OrderOpen(
                   cmd.symbol, limit_order_type, lots,
                   0, limit_price, broker_sl, broker_tp,
                   ORDER_TIME_DAY, 0, cleanComment);
                ulong os_ms = (ulong)(GetTickCount64() - os_start);

                if(!placed)
                {
                   uint   rc     = g_trade.ResultRetcode();
                   g_lastExecError = (int)rc;
                   string rcDesc = (g_limitOrderValidator != NULL) ?
                                   g_limitOrderValidator.GetOrderErrorDescription(rc) :
                                   g_trade.ResultRetcodeDescription();
                   PrintFormat("[PineTunnel] NM limit placement failed: %d - %s", rc, rcDesc);
                   PrintFormat("[Exec] [TIMING] NM OrderSend FAIL: %I64ums | retcode=%d | Sym=%s Price=%.5f", os_ms, rc, cmd.symbol, limit_price);
                   PrintFormat("[PineTunnel] [DIAG] Sym=%s Price=%.5f Type=%s Lots=%.2f SL=%.5f TP=%.5f Bid=%.5f Ask=%.5f StopsLvl=%d Sim=%s",
                               cmd.symbol, limit_price, EnumToString(limit_order_type),
                               lots, broker_sl, broker_tp, bid, ask, entryStopsLevel,
                               StringFind(cmd.symbol, ".sim") >= 0 ? "YES" : "NO");
                }
                else
                {
                   ulong order_ticket = g_trade.ResultOrder();
                   PrintFormat("[PineTunnel] NM limit placed #%I64d - waiting up to %dms...",
                               order_ticket, InpEntryLimitTimeoutMs);
                   PrintFormat("[Exec] [TIMING] NM OrderSend OK: %I64ums | ticket=#%I64d | offset=%dpts | Sym=%s", os_ms, order_ticket, minValidPoints, cmd.symbol);

                  ulong poll_start = GetTickCount64();
                  bool  poll_done  = false;

                  while(GetTickCount64() - poll_start < (ulong)InpEntryLimitTimeoutMs)
                  {
                     if(IsStopped()) break;
                     if(!OrderSelect(order_ticket))
                     {
                        HistorySelect(TimeCurrent() - HISTORY_SEARCH_WINDOW_SEC, TimeCurrent() + 1);
                        for(int di = HistoryDealsTotal() - 1; di >= 0; di--)
                        {
                           ulong deal_ticket = HistoryDealGetTicket(di);
                           if(HistoryDealGetInteger(deal_ticket, DEAL_ORDER) == order_ticket)
                           {
                              ENUM_DEAL_TYPE dtype = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
                              if(dtype == DEAL_TYPE_BUY || dtype == DEAL_TYPE_SELL)
                              {
                                 nmFillPrice  = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
                                 nmFillTicket = HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
                                 if(nmFillPrice <= 0) nmFillPrice = limit_price;
                                  nmFillByLimit = true;
                                  PrintFormat("[PineTunnel] NM filled @ %.5f (Deal #%I64d Pos #%I64d)",
                                              nmFillPrice, deal_ticket, nmFillTicket);
                                  PrintFormat("[Exec] [TIMING] NM fill detected: %I64ums after placement | ticket=#%I64d | price=%.5f", (ulong)(GetTickCount64() - poll_start), order_ticket, nmFillPrice);
                              }
                              else
                              {
                                 PrintFormat("[PineTunnel] NM order #%I64d cancelled by broker - not filled", order_ticket);
                              }
                              break;
                           }
                        }
                        poll_done = true;
                        break;
                     }
                      Sleep(NM_POLL_INTERVAL_MS);
                  }

                   if(!poll_done && !nmFillByLimit)
                   {
                      PrintFormat("[PineTunnel] NM timeout after %dms (actual_elapsed=%dms) - cancelling #%I64d",
                                  InpEntryLimitTimeoutMs, (long)(GetTickCount64() - poll_start), order_ticket);
                      PrintFormat("[Exec] [TIMING] NM poll timeout: %I64ums total | offset=%dpts | Sym=%s", (ulong)(GetTickCount64() - poll_start), minValidPoints, cmd.symbol);
                      ulong cancel_start = GetTickCount64();
                      g_trade.OrderDelete(order_ticket);

                     double late_price = 0;
                     ulong  late_pos   = 0;
                     ulong resolve_start = GetTickCount64();
                     ENUM_ORDER_DELETE_RESOLUTION cancel_resolution = ResolveOrderDelete(order_ticket, NM_DELETE_CONFIRM_TIMEOUT_MS, late_price, late_pos);
                     PrintFormat("[PineTunnel] [DIAG] NM cancel resolution: %s in %dms (order=#%I64d)",
                                 cancel_resolution == ORDER_DELETE_RESOLUTION_CONFIRMED ? "CONFIRMED" :
                                 cancel_resolution == ORDER_DELETE_RESOLUTION_FILLED ? "FILLED" : "UNCERTAIN",
                                 (long)(GetTickCount64() - resolve_start), order_ticket);
                     if(cancel_resolution == ORDER_DELETE_RESOLUTION_FILLED)
                     {
                        nmFillPrice   = (late_price > 0) ? late_price : limit_price;
                        nmFillTicket  = late_pos;
                        nmFillByLimit = true;
                         PrintFormat("[PineTunnel] NM filled during cancel race @ %.5f (Pos #%I64d)",
                                     nmFillPrice, nmFillTicket);
                         PrintFormat("[Exec] [TIMING] NM cancel-race fill: %I64ums cancel total | Sym=%s", (ulong)(GetTickCount64() - resolve_start), cmd.symbol);
                     }
                     else if(cancel_resolution != ORDER_DELETE_RESOLUTION_CONFIRMED)
                     {
                        nmAborted = true;
                        PrintFormat("[PineTunnel] ABORT: NM order #%I64d cancel not confirmed within %dms - skipping market fallback.",
                                    order_ticket, NM_DELETE_CONFIRM_TIMEOUT_MS);
                     }
                     else
                     {
                         PrintFormat("[PineTunnel] [DIAG] NM cancel confirmed - falling through to market fallback (order=#%I64d)", order_ticket);
                         PrintFormat("[Exec] [TIMING] NM cancel+resolve: %I64ums | Sym=%s", (ulong)(GetTickCount64() - cancel_start), cmd.symbol);
                     }
                  }
               }
            }

            PrintFormat("[PineTunnel] [DIAG] NM entry result: aborted=%s filled=%s price=%.5f pos=#%I64d lots=%.2f sym=%s",
                        nmAborted ? "yes" : "no", nmFillByLimit ? "YES" : "no",
                        nmFillPrice, nmFillTicket, lots, cmd.symbol);
         }

      // --- Market fallback with FRESH prices ---
      if(nmAborted)
      {
         result = false;
         PrintFormat("[PineTunnel] Skipping market fallback - entry aborted due to unconfirmed cancel (see ABORT above)");
      }
       else if(!nmFillByLimit)
       {
          // Restore default filling mode before market fallback
          g_trade.SetTypeFilling(ORDER_FILLING_FOK);
          ask = SymbolInfoDouble(cmd.symbol, SYMBOL_ASK);
         bid = SymbolInfoDouble(cmd.symbol, SYMBOL_BID);
         double fresh_price = (orderType == ORDER_TYPE_BUY) ? ask : bid;

            PrintFormat("[PineTunnel] Market fallback @ %.5f (Ask=%.5f Bid=%.5f)", fresh_price, ask, bid);
           ulong mkt_start = GetTickCount64();
           result = g_trade.PositionOpen(cmd.symbol, orderType, lots, fresh_price, broker_sl, broker_tp, cleanComment);
           ulong mkt_ms = (ulong)(GetTickCount64() - mkt_start);

           if(result)
           {
              PrintFormat("[PineTunnel] Market order executed as fallback (retcode=%d)", g_trade.ResultRetcode());
              PrintFormat("[Exec] [TIMING] Market fallback OrderSend: %I64ums | retcode=%d | price=%.5f | Sym=%s", mkt_ms, g_trade.ResultRetcode(), fresh_price, cmd.symbol);
          }
          else
          {
             uint   rc     = g_trade.ResultRetcode();
             g_lastExecError = (int)rc;
             string rcDesc = (g_limitOrderValidator != NULL) ?
                             g_limitOrderValidator.GetOrderErrorDescription(rc) :
                             g_trade.ResultRetcodeDescription();
             PrintFormat("[PineTunnel] Market fallback also failed: %d - %s", rc, rcDesc);
             PrintFormat("[Exec] [TIMING] Market fallback FAILED: %I64ums | retcode=%d | Sym=%s", mkt_ms, rc, cmd.symbol);
            PrintFormat("[PineTunnel] [DIAG] Market fail: Sym=%s Type=%s Lots=%.2f Price=%.5f SL=%.5f TP=%.5f",
                        cmd.symbol, EnumToString(orderType), lots, fresh_price, broker_sl, broker_tp);
            PrintFormat("[PineTunnel] [DIAG] Account: Balance=%.2f Equity=%.2f FreeMargin=%.2f Chart=%s",
                        AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY),
                        AccountInfoDouble(ACCOUNT_MARGIN_FREE), _Symbol);
            if(rc == 10044)
            {
               PrintFormat("[PineTunnel] Trading not allowed on symbol %s", cmd.symbol);
                PrintFormat("[PineTunnel] Note: This is likely a simulation symbol with trading restrictions");
            }
         }
      }
        else
        {
           result = true; // limit fill counts as success
        }

      // Restore default filling mode after NM block (moved before market fallback above)
   }
     else
     {
        ulong direct_start = GetTickCount64();
        // Free margin check before order execution
        double required_margin = 0;
        if(OrderCalcMargin(orderType, cmd.symbol, lots, price, required_margin))
        {
           double free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
           if(required_margin > free_margin * 0.95)
           {
              PrintFormat("[PineTunnel] Insufficient free margin: need %.2f, have %.2f. ABORTING.", required_margin, free_margin);
              return false;
           }
        }
        // Phase 4: Use hardened order execution manager if available, fallback to standard
        if(g_orderManager != NULL)
        {
           // V7.05: Price validation is informational only - never blocks trade execution
           if(g_priceValidator != NULL)
              g_priceValidator.ValidatePriceFeed(cmd.symbol, false);

           result = g_orderManager.ExecuteMarketOrder(cmd.symbol, orderType, lots, broker_sl, broker_tp, cleanComment);
        }
        else
        {
           // Fallback to standard execution
           result = g_trade.PositionOpen(cmd.symbol, orderType, lots, price, broker_sl, broker_tp, cleanComment);
        }
        PrintFormat("[Exec] [TIMING] Direct market order: %I64ums | retcode=%d | Sym=%s", (ulong)(GetTickCount64() - direct_start), g_trade.ResultRetcode(), cmd.symbol);
     }

    if(result)
    {
      // Resolve exec_price and ticket - for limit fills use history data, for market use ResultDeal/ResultPrice
      double exec_price;
      ulong  ticket;

      if(nmFillByLimit)
      {
         exec_price = nmFillPrice;
         ticket     = nmFillTicket;
      }
      else
      {
         exec_price = g_trade.ResultPrice();
         if(exec_price <= 0) exec_price = price;
         ticket = g_trade.ResultOrder();
         if(ticket == 0) ticket = g_trade.ResultDeal();
      }

      string sl_tp_info = "";
      if(sl_price > 0) sl_tp_info += StringFormat(" SL:%.5f", sl_price);
      if(tp_price > 0) sl_tp_info += StringFormat(" TP:%.5f", tp_price);

      // Phase 4: Use production logger if available
      if(g_prodLogger != NULL)
      {
         g_prodLogger.Trade(CommandName(cmd.type), cmd.symbol, lots, exec_price, true);
      }
      else
      {
         PrintFormat("[PineTunnel] %s %.2f %s @ %.5f%s executed successfully",
                     CommandName(cmd.type), lots, cmd.symbol, exec_price, sl_tp_info);
      }

      // Send trade report to server for analytics
      SendTradeReport(
         CommandName(cmd.type),
         cmd.symbol,
         lots,
         exec_price,
         ticket,
         true,  // success
         "",    // no error
          cmd.signal_id
       );

      // Map ticket -> signal_id for close reports
      SaveTicketSignal(ticket, cmd.signal_id);

      // V7.00: Verify SL/TP was properly set (only if enabled and not using hidden targets)
      // Use broker_sl/broker_tp (not original sl_price/tp_price) to respect hidden target offset
      if(InpEnableSLTPVerify && InpHiddenSLTP == HIDDEN_OFF && ticket > 0)
      {
         VerifyAndFixSLTP(ticket, cmd.symbol, broker_sl, broker_tp, InpSLTPVerifyRetries);
      }

      // If Hidden SL/TPs enabled, store SL/TP in memory
      if(InpHiddenSLTP == HIDDEN_ON && (sl_price > 0 || tp_price > 0))
      {
         if(ticket > 0)
         {
            AddHiddenTarget(ticket, cmd.symbol, orderType == ORDER_TYPE_BUY ? POSITION_TYPE_BUY : POSITION_TYPE_SELL,
                          sl_price, tp_price, exec_price);
            if(g_prodLogger != NULL)
            {
               g_prodLogger.Info("Hidden targets registered for Ticket #" + IntegerToString((int)ticket));
            }
         }
      }
   }
    else
    {
        g_lastExecError = (int)g_trade.ResultRetcode();
        string error_msg = StringFormat("%d - %s", g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());

        // If retcode=0, validation failed before broker call - explain why
        if(g_trade.ResultRetcode() == 0)
        {
           string reason = "Pre-trade validation failed";
           if(cmd.type == COMMAND_BUY && tp_price > 0 && tp_price <= price)
              reason = StringFormat("TP %.5f <= entry %.5f (TP must be above price for BUY)", tp_price, price);
           else if(cmd.type == COMMAND_SELL && tp_price > 0 && tp_price >= price)
              reason = StringFormat("TP %.5f >= entry %.5f (TP must be below price for SELL)", tp_price, price);
           else if(cmd.type == COMMAND_BUY && sl_price > 0 && sl_price >= price)
              reason = StringFormat("SL %.5f >= entry %.5f (SL must be below price for BUY)", sl_price, price);
           else if(cmd.type == COMMAND_SELL && sl_price > 0 && sl_price <= price)
              reason = StringFormat("SL %.5f <= entry %.5f (SL must be above price for SELL)", sl_price, price);
           error_msg = StringFormat("%s | Entry=%.5f SL=%.5f TP=%.5f Lots=%.2f | %s",
               reason, price, broker_sl, broker_tp, lots, cmd.symbol);
        }
        else
        {
           // Enrich broker error with diagnostic context for Telegram
           error_msg = StringFormat("%s | Entry=%.5f SL=%.5f TP=%.5f Lots=%.2f | %s",
               error_msg, price, broker_sl, broker_tp, lots, cmd.symbol);
        }

       // Phase 4: Use production logger if available
       if(g_prodLogger != NULL)
       {
          g_prodLogger.Trade(CommandName(cmd.type), cmd.symbol, lots, price, false, error_msg);
       }
       else
       {
          PrintFormat("[PineTunnel] %s order failed: %s", CommandName(cmd.type), error_msg);
       }

       // V7.04: Comprehensive diagnostics on any trade failure
       PrintFormat("[PineTunnel] [DIAG] Trade failure dump: Cmd=%s | Symbol=%s | Lots=%.2f | Price=%.5f | SL=%.5f | TP=%.5f | Chart=%s",
                   CommandName(cmd.type), cmd.symbol, lots, price, sl_price, tp_price, _Symbol);
       PrintFormat("[PineTunnel] [DIAG] Broker SL=%.5f | Broker TP=%.5f | NearMarket=%s | Comment=%s",
                   broker_sl, broker_tp, (convertToNearMarket ? "YES" : "NO"), cmd.comment);
       PrintFormat("[PineTunnel] [DIAG] Account: Balance=%.2f | Equity=%.2f | FreeMargin=%.2f | Leverage=%d",
                   AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY),
                   AccountInfoDouble(ACCOUNT_MARGIN_FREE), (int)AccountInfoInteger(ACCOUNT_LEVERAGE));

       // Report failed trade execution to server
       SendTradeReport(
          CommandName(cmd.type),
          cmd.symbol,
          lots,
          price,
          0,      // no ticket
          false,  // failed
          error_msg,
          cmd.signal_id
       );
    }

    return result;
}
//+------------------------------------------------------------------+
//| Execute pending order using pending order manager              |
//+------------------------------------------------------------------+
bool ExecutePendingOrder(const SignalCommand &cmd)
{
   // V7.06 LAYER-4: pre-flight broker-state reconciliation (see ExecuteMarketOrder for rationale)
   if(IsDuplicateSignalPosition(cmd.signal_id, cmd.symbol))
      return true;  // Already handled by sibling instance - ack as success

   if(!cmd.has_pending)
   {
      PrintFormat("[PineTunnel] ERROR: Pending order %s requires pending= parameter",
                  CommandName(cmd.type));
      return false;
   }

   if(g_pendingManager == NULL)
   {
      PrintFormat("[PineTunnel] ERROR: Pending order manager not initialized");
      return false;
   }

   // Initialize lots with default
   double lots = InpDefaultLots;

   // First, calculate the pending order entry price to use for volume calculation
   double current_price = 0;
   double pending_entry_price = 0;
   ENUM_ORDER_TYPE pending_order_type;

   // Auto-enable symbol in Market Watch (hard-coded to always ON)
   if(!SymbolSelect(cmd.symbol, true))
   {
      PrintFormat("[PineTunnel] Failed to select symbol %s", cmd.symbol);
   }

   // Get current market price
   CSymbolInfo symbol_info;
   if(!symbol_info.Name(cmd.symbol))
   {
      PrintFormat("[PineTunnel] ERROR: Failed to get symbol info for %s", cmd.symbol);
      return false;
   }

   // Wait for price data to become available (with retry mechanism)
   int retries = 0;
   int max_retries = 10;
   int wait_ms = 300;
   bool price_ready = false;

   while(retries < max_retries && !price_ready)
   {
      symbol_info.Refresh();
      symbol_info.RefreshRates();

      // Check if we have valid prices
      if(symbol_info.Ask() > 0 && symbol_info.Bid() > 0)
      {
         price_ready = true;
         if(retries > 0)
         {
            PrintFormat("[PineTunnel] Price data ready for %s after %d retries (%.0fms)",
                        cmd.symbol, retries, retries * wait_ms);
         }
         break;
      }

      // Wait before next attempt
      if(retries < max_retries - 1)
      {
         Sleep(wait_ms);
      }
      retries++;
   }

   if(!price_ready)
   {
      PrintFormat("[PineTunnel] ERROR: Unable to get prices for %s after %d retries (%.1f seconds)",
                  cmd.symbol, retries, (retries * wait_ms) / 1000.0);
      return false;
   }

   // Determine order type and get appropriate price
   switch(cmd.type)
   {
      case COMMAND_BUY_LIMIT:
         current_price = symbol_info.Ask();
         pending_order_type = ORDER_TYPE_BUY_LIMIT;
         break;
      case COMMAND_SELL_LIMIT:
         current_price = symbol_info.Bid();
         pending_order_type = ORDER_TYPE_SELL_LIMIT;
         break;
      case COMMAND_BUY_STOP:
         current_price = symbol_info.Ask();
         pending_order_type = ORDER_TYPE_BUY_STOP;
         break;
      case COMMAND_SELL_STOP:
         current_price = symbol_info.Bid();
         pending_order_type = ORDER_TYPE_SELL_STOP;
         break;
      default:
         PrintFormat("[PineTunnel] ERROR: Invalid pending order type");
         return false;
   }

    // Calculate pending entry price based on entry type
    // Priority: explicit entry_type from signal > EA PendingType setting
    string effective_entry_type = cmd.entry_type;
    if(effective_entry_type == "")
    {
       // Fall back to EA PendingType setting
       if(InpPendingType == PENDING_PIPS)
          effective_entry_type = "pips";
       else if(InpPendingType == PENDING_PRICE)
          effective_entry_type = "price";
       else if(InpPendingType == PENDING_PERCENT)
          effective_entry_type = "pct";
    }

    if(effective_entry_type == "price")
    {
       // entry_price: direct absolute price level
       pending_entry_price = cmd.pending_distance;
    }
    else if(effective_entry_type == "pips")
    {
       // entry_points: distance in pips from current price
       // Stop orders (buy_stop, sell_stop) = worse than current price
       // Limit orders (buy_limit, sell_limit) = better than current price
       double point = symbol_info.Point();
       double distance = cmd.pending_distance * point * 10; // 1 pip = 10 points (standard 5-digit broker convention)

       if(cmd.type == COMMAND_BUY_STOP)
          pending_entry_price = current_price + distance;
       else if(cmd.type == COMMAND_SELL_STOP)
          pending_entry_price = current_price - distance;
       else if(cmd.type == COMMAND_BUY_LIMIT)
          pending_entry_price = current_price - distance;
       else if(cmd.type == COMMAND_SELL_LIMIT)
          pending_entry_price = current_price + distance;
    }
    else if(effective_entry_type == "pct")
    {
       // entry_pct: percentage distance from current price
       // Same directional logic as entry_points
       double percentage = cmd.pending_distance / 100.0;

       if(cmd.type == COMMAND_BUY_STOP)
          pending_entry_price = current_price * (1 + percentage);
       else if(cmd.type == COMMAND_SELL_STOP)
          pending_entry_price = current_price * (1 - percentage);
       else if(cmd.type == COMMAND_BUY_LIMIT)
          pending_entry_price = current_price * (1 - percentage);
       else if(cmd.type == COMMAND_SELL_LIMIT)
          pending_entry_price = current_price * (1 + percentage);
    }

    // Now calculate volume using the pending entry price
    // Determine if this is a buy or sell order for explicit type calculations
    bool is_buy = (cmd.type == COMMAND_BUY_LIMIT || cmd.type == COMMAND_BUY_STOP);

    // Calculate SL price using explicit type support (same as market orders)
    double sl_price = CalculateSLPriceWithExplicitType(cmd, pending_entry_price, is_buy);

    // Calculate TP price using explicit type support (same as market orders)
    double tp_price = CalculateTPPriceWithExplicitType(cmd, pending_entry_price, is_buy);

    if(g_positionSizer != NULL)
    {
       // Calculate volume using explicit type support (same as market orders)
       lots = CalculateVolumeWithExplicitType(cmd, pending_entry_price, sl_price, pending_order_type);

       // Log explicit parameter usage for debugging
       if(InpLogSignals)
       {
          if(cmd.vol_type != "")
             PrintFormat("[PineTunnel] Pending order using explicit vol_type=%s, lots=%.4f", cmd.vol_type, lots);
          if(cmd.sl_type != "")
             PrintFormat("[PineTunnel] Pending order using explicit sl_type=%s, sl_price=%.5f", cmd.sl_type, sl_price);
          if(cmd.tp_type != "")
             PrintFormat("[PineTunnel] Pending order using explicit tp_type=%s, tp_price=%.5f", cmd.tp_type, tp_price);
       }

       // Validation
       if(lots <= 0)
       {
          PrintFormat("[PineTunnel] Volume calculation failed. Using default lots: %.2f", InpDefaultLots);

          // Provide specific guidance if SL-dependent mode is used without SL
          // Check both explicit vol_type and EA setting
          bool needs_sl = (cmd.vol_type == "dollar" || cmd.vol_type == "bal_loss" || cmd.vol_type == "eq_loss");
          if(!needs_sl && (InpVolumeType == VOLUME_DOLLAR_AMOUNT ||
              InpVolumeType == VOLUME_PERCENTAGE_BALANCE_LOSS ||
              InpVolumeType == VOLUME_PERCENTAGE_EQUITY_LOSS))
          {
             needs_sl = true;
          }

          if(needs_sl && sl_price <= 0)
          {
             PrintFormat("[PineTunnel] Volume Type requires stop loss (sl=) parameter");
             PrintFormat("[PineTunnel] Please add sl=, sl_points=, sl_price=, or sl_pct= to your pending order signal");
          }

          lots = InpDefaultLots;
       }
    }
    else
    {
       // Fallback if position sizer not available
       if(cmd.lots > 0)
          lots = cmd.lots;
    }

    // Execute based on command type
    bool result = false;

    // Convert SL/TP prices to pips for PendingOrders.mqh interface
    // The PendingOrders class expects pips, so we convert back from price
    double sl_points = 0;
    double tp_points = 0;
    double point = symbol_info.Point();

    if(sl_price > 0 && point > 0)
    {
       double sl_distance = MathAbs(pending_entry_price - sl_price);
       sl_points = sl_distance / point / 10.0;  // Convert points to pips (1 pip = 10 points for 5-digit brokers)
    }

    if(tp_price > 0 && point > 0)
    {
       double tp_distance = MathAbs(pending_entry_price - tp_price);
       tp_points = tp_distance / point / 10.0;  // Convert points to pips
    }

    // V7.06 LAYER-4: prefix signal_id into the broker comment
    string pendingComment = BuildOrderComment(cmd.signal_id, cmd.comment);

    switch(cmd.type)
    {
       case COMMAND_BUY_LIMIT:
          result = g_pendingManager.PlaceBuyLimit(
             cmd.symbol, lots, cmd.pending_distance, InpPendingType,
             sl_points, tp_points, pendingComment
          );
          break;

       case COMMAND_SELL_LIMIT:
          result = g_pendingManager.PlaceSellLimit(
             cmd.symbol, lots, cmd.pending_distance, InpPendingType,
             sl_points, tp_points, pendingComment
          );
          break;

       case COMMAND_BUY_STOP:
          result = g_pendingManager.PlaceBuyStop(
             cmd.symbol, lots, cmd.pending_distance, InpPendingType,
             sl_points, tp_points, pendingComment
          );
          break;

       case COMMAND_SELL_STOP:
          result = g_pendingManager.PlaceSellStop(
             cmd.symbol, lots, cmd.pending_distance, InpPendingType,
             sl_points, tp_points, pendingComment
          );
          break;

       default:
          PrintFormat("[PineTunnel] ERROR: Invalid pending order type");
          return false;
    }

   if(!result)
      g_lastExecError = (int)g_trade.ResultRetcode();

   return result;
}
//+------------------------------------------------------------------+
//| Check if daily reset is needed                                   |
//+------------------------------------------------------------------+
void CheckDailyReset()
{
   datetime current_time = TimeCurrent();
   datetime current_gmt = current_time + (InpDailyTimezoneGMT * 3600);

   MqlDateTime dt_current, dt_last;
   TimeToStruct(current_gmt, dt_current);
   TimeToStruct(g_dailyResetTime + (InpDailyTimezoneGMT * 3600), dt_last);

   // Check if we've crossed into a new day (first run or day changed)
   bool is_first_run = (g_dailyResetTime == 0);
   bool day_changed = (dt_current.day != dt_last.day || dt_current.mon != dt_last.mon || dt_current.year != dt_last.year);

   // Only proceed if there's actually a reset needed
   if(is_first_run || day_changed)
   {
      // Daily reset triggered (silent)

      // Reset account protection tracking
      g_dailyStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      g_dailyResetTime = current_time;
      g_dailyHalted = false;  // Reset daily halt

      // Reset daily signal counters (for dashboard)
      g_todaySignals = 0;
      g_todaySuccessful = 0;
      g_todayFailed = 0;

      // Load today's executed trades from history ONLY on EA initialization
      // On day change (midnight), counters should stay at 0 (new day = fresh start)
      // Note: Failed signals are NOT recoverable from history
      if(is_first_run)  // Load history on first run (EA restart mid-day scenario)
      {
         CountTodayTrades();
      }
   }
}
//+------------------------------------------------------------------+
//| Execute protection action                                         |
//+------------------------------------------------------------------+
void ExecuteProtectionAction(bool is_daily, int action_type, string reason)
{
   PrintFormat("[PineTunnel] ACCOUNT PROTECTION TRIGGERED: %s", reason);

   bool should_close = false;
   bool should_halt = false;
   bool should_halt_persistent = false;

   if(is_daily)
   {
      switch(action_type)
      {
         case ACTION_DAILY_HALT:
            should_halt = true;
            PrintFormat("[PineTunnel] Action: Halt EA for remainder of day");
            break;

         case ACTION_DAILY_CLOSE:
            should_close = true;
            PrintFormat("[PineTunnel] Action: Close all positions");
            break;

         case ACTION_DAILY_CLOSE_HALT:
            should_close = true;
            should_halt = true;
            PrintFormat("[PineTunnel] Action: Close all positions and halt EA for day");
            break;
      }

      if(should_halt)
         g_dailyHalted = true;
   }
   else // Cumulative
   {
      switch(action_type)
      {
         case ACTION_CUM_HALT_DAY:
            should_halt = true;
            PrintFormat("[PineTunnel] Action: Halt EA for remainder of day");
            break;

         case ACTION_CUM_HALT_PERSIST:
            should_halt = true;
            should_halt_persistent = true;
            PrintFormat("[PineTunnel] Action: Halt EA persistently (manual restart required)");
            break;

         case ACTION_CUM_CLOSE:
            should_close = true;
            PrintFormat("[PineTunnel] Action: Close all positions");
            break;

         case ACTION_CUM_CLOSE_HALT_DAY:
            should_close = true;
            should_halt = true;
            PrintFormat("[PineTunnel] Action: Close all positions and halt EA for day");
            break;

         case ACTION_CUM_CLOSE_HALT_PERSIST:
            should_close = true;
            should_halt = true;
            should_halt_persistent = true;
            PrintFormat("[PineTunnel] Action: Close all positions and halt EA persistently");
            break;
      }

      if(should_halt)
      {
         if(should_halt_persistent)
            g_protectionHalted = true;
         else
            g_dailyHalted = true;
      }
   }

   // Execute actions
   if(should_close)
   {
      PrintFormat("[PineTunnel] Closing all positions...");
      ClosePositions("", -1, "");  // Close all positions regardless of symbol/type (market order for safety)
   }

   if(should_halt)
   {
      if(should_halt_persistent)
         PrintFormat("[PineTunnel] EA HALTED PERSISTENTLY - Send 'eaon' command or restart EA to resume");
      else
         PrintFormat("[PineTunnel] EA HALTED until next trading day");
   }
}
//+------------------------------------------------------------------+
//| Check account protection limits                                  |
//+------------------------------------------------------------------+
bool CheckAccountProtection()
{
   // Check daily reset first
   CheckDailyReset();

   // Early exit for halted states
   if(g_protectionHalted || g_dailyHalted)
      return false;  // Block trading

   // Skip all checks if no limits are set
   if(InpDailyProfit <= 0 && InpDailyLoss <= 0 &&
      InpCumulativeProfit <= 0 && InpCumulativeLoss <= 0)
      return true;  // No limits configured

   // Get account values once
   double current_balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double current_equity = AccountInfoDouble(ACCOUNT_EQUITY);

   // Check Daily Profit Target
   if(InpDailyProfit > 0)
   {
      double daily_pnl = current_equity - g_dailyStartBalance;
      double target_value = 0;

      if(InpDailyProfit <= 1.0)
         target_value = g_dailyStartBalance * InpDailyProfit;  // Percentage
      else
         target_value = InpDailyProfit;  // Dollar amount

      if(daily_pnl >= target_value)
      {
         string reason = StringFormat("Daily Profit Target reached: $%.2f / $%.2f", daily_pnl, target_value);
         ExecuteProtectionAction(true, InpAction1, reason);
         return false;
      }
   }

   // Check Daily Loss Limit
   if(InpDailyLoss > 0)
   {
      double daily_pnl = current_equity - g_dailyStartBalance;
      double limit_value = 0;

      if(InpDailyLoss <= 1.0)
         limit_value = -(g_dailyStartBalance * InpDailyLoss);  // Percentage (negative)
      else
         limit_value = -InpDailyLoss;  // Dollar amount (negative)

      if(daily_pnl <= limit_value)
      {
         string reason = StringFormat("Daily Loss Limit reached: $%.2f / $%.2f", daily_pnl, limit_value);
         ExecuteProtectionAction(true, InpAction1, reason);
         return false;
      }
   }

   // Check Cumulative Profit Target
   if(InpCumulativeProfit > 0)
   {
      double cumulative_pnl = current_equity - g_cumulativeStartBalance;
      double target_value = 0;

      if(InpCumulativeProfit <= 1.0)
         target_value = g_cumulativeStartBalance * InpCumulativeProfit;  // Percentage
      else
         target_value = InpCumulativeProfit;  // Dollar amount

      if(cumulative_pnl >= target_value)
      {
         string reason = StringFormat("Cumulative Profit Target reached: $%.2f / $%.2f", cumulative_pnl, target_value);
         ExecuteProtectionAction(false, InpAction2, reason);
         return false;
      }
   }

   // Check Cumulative Loss Limit
   if(InpCumulativeLoss > 0)
   {
      double cumulative_pnl = current_equity - g_cumulativeStartBalance;
      double limit_value = 0;

      if(InpCumulativeLoss <= 1.0)
         limit_value = -(g_cumulativeStartBalance * InpCumulativeLoss);  // Percentage (negative)
      else
         limit_value = -InpCumulativeLoss;  // Dollar amount (negative)

      if(cumulative_pnl <= limit_value)
      {
         string reason = StringFormat("Cumulative Loss Limit reached: $%.2f / $%.2f", cumulative_pnl, limit_value);
         ExecuteProtectionAction(false, InpAction2, reason);
         return false;
      }
   }

   return true;  // All checks passed, allow trading
}
bool CheckActiveHours()
{
   // Fast path for default settings
   if(InpStartTime == "00:00" && InpEndTime == "23:59")
      return true;

   // Get current broker time
   datetime current = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(current, dt);

   // Parse start and end times with validation
   string start_parts[], end_parts[];
   int start_count = StringSplit(InpStartTime, ':', start_parts);
   int end_count = StringSplit(InpEndTime, ':', end_parts);

   if(start_count != 2 || end_count != 2)
   {
      Print("[PineTunnel] ERROR: Invalid time format. Use HH:MM");
      return true;  // Default to allowing trading if format error
   }

   // Parse with bounds checking
   int start_hour = (int)StringToInteger(start_parts[0]);
   int start_min = (int)StringToInteger(start_parts[1]);
   int end_hour = (int)StringToInteger(end_parts[0]);
   int end_min = (int)StringToInteger(end_parts[1]);

   // Validate time ranges
   if(start_hour < 0 || start_hour > 23 || end_hour < 0 || end_hour > 23 ||
      start_min < 0 || start_min > 59 || end_min < 0 || end_min > 59)
   {
      Print("[PineTunnel] ERROR: Invalid time values. Hours: 0-23, Minutes: 0-59");
      return true;  // Default to allowing trading
   }

   // Convert current time to minutes since midnight
   int current_mins = dt.hour * 60 + dt.min;
   int start_mins = start_hour * 60 + start_min;
   int end_mins = end_hour * 60 + end_min;

   // Check if trading is allowed - optimized logic
   bool is_active = (start_mins <= end_mins)
                  ? (current_mins >= start_mins && current_mins <= end_mins)  // Normal hours
                  : (current_mins >= start_mins || current_mins <= end_mins);  // Overnight

   // Log only when blocked
   if(!is_active)
   {
      PrintFormat("[PineTunnel] Outside active hours (%s - %s). Current: %02d:%02d",
                 InpStartTime, InpEndTime, dt.hour, dt.min);
   }

   return is_active;
}
//+------------------------------------------------------------------+
//| Apply symbol prefix/suffix                                       |
//+------------------------------------------------------------------+
string TransformSymbol(const string symbol)
{
   // Early return if no transformation needed
   if(InpPrefix == "" && InpSuffix == "")
      return symbol;

   // Build transformed symbol efficiently
   string transformed = (InpPrefix != "" ? InpPrefix : "")
                      + symbol
                      + (InpSuffix != "" ? InpSuffix : "");

   return transformed;
}
bool ExecuteCommand(const SignalCommand &command, bool from_queue = false)
{
   ulong exec_start_tick = GetTickCount64();
   g_lastExecError = 0;  // reset before each execution attempt

   // V7.00: Check for duplicate signal FIRST (before counting)
   if(IsSignalDuplicate(command.signal_id))
   {
      PrintFormat("[PineTunnel] DUPLICATE SIGNAL BLOCKED | ID: %s | Chart: %s", command.signal_id, _Symbol);
      return true;  // Return true to acknowledge (not a failure, just skipped)
   }

   // V7.04: Cross-instance signal lock - prevents multiple EA instances from
   // executing the same signal simultaneously when sharing a license key.
   // The first instance to create the lock file wins; others skip.
   // from_queue=true: skip this block - lock file was created on the first (failed) attempt
   if(!from_queue && command.signal_id != "")
   {
      string lockFile = "PineTunnel_lock_" + command.signal_id + ".lock";
      if(FileIsExist(lockFile))
      {
         string lockOwner = ReadLockFileContents(lockFile);
         if(lockOwner != "")
            PrintFormat("[PineTunnel] SIGNAL LOCKED by another instance - SKIPPED | ID: %s | Chart: %s | Lock Owner: %s",
                        command.signal_id, _Symbol, lockOwner);
         else
            PrintFormat("[PineTunnel] SIGNAL LOCKED by another instance - SKIPPED | ID: %s | Chart: %s | Lock Owner: (unreadable)",
                        command.signal_id, _Symbol);
         return true;  // Another instance is handling this signal
      }
      // Create lock file immediately before execution
      int lockHandle = FileOpen(lockFile, FILE_WRITE|FILE_TXT|FILE_ANSI);
      if(lockHandle != INVALID_HANDLE)
      {
         FileWriteString(lockHandle, _Symbol + "_" + IntegerToString(ChartID()) + "|" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS));
         FileClose(lockHandle);
      }
   }

   // from_queue=true: counters already incremented on first attempt - skip to avoid double-count
   if(!from_queue)
   {
      g_totalSignals++;
      g_todaySignals++;
   }

   PrintFormat("[PineTunnel] Signal #%d: %s %s (ID: %s)", g_totalSignals, CommandName(command.type), command.symbol, command.signal_id);
   PrintFormat("[Exec] [TIMING] Signal #%d receipt tick=%I64u", g_totalSignals, GetTickCount64());

   // Check if EA is enabled (except for EAON command)
   if(!g_eaEnabled && command.type != COMMAND_EA_ON)
   {
      PrintFormat("[PineTunnel] EA is DISABLED - Ignoring signal");
      PrintFormat("[PineTunnel] To enable EA, send: %s,eaon,eaon", InpLicenseID);
      return false;
   }

   // Check active hours (except for EA management commands)
   if(command.type != COMMAND_EA_ON &&
      command.type != COMMAND_EA_OFF &&
      command.type != COMMAND_CLOSEALL_EA_OFF)
   {
      if(!CheckActiveHours())
      {
         // Outside active hours - ignore signal
         return false;
      }
   }

   // Check account protection limits (except for close/EA management commands)
   if(command.type != COMMAND_EA_ON &&
      command.type != COMMAND_EA_OFF &&
      command.type != COMMAND_CLOSE_LONG &&
      command.type != COMMAND_CLOSE_SHORT &&
      command.type != COMMAND_CLOSE_LONG_SHORT &&
      command.type != COMMAND_EXIT)
   {
      if(!CheckAccountProtection())
      {
         // Protection triggered - trading blocked
         return false;
      }
   }

   // Check account filter if specified
   if(command.account_filter > 0)
   {
      double account_value = 0;
      string filter_name = "";

      switch(InpAccountFilterBasis)
      {
         case ACCOUNT_BASIS_BALANCE:
            account_value = AccountInfoDouble(ACCOUNT_BALANCE);
            filter_name = "Balance";
            break;

         case ACCOUNT_BASIS_EQUITY:
            account_value = AccountInfoDouble(ACCOUNT_EQUITY);
            filter_name = "Equity";
            break;

         case ACCOUNT_BASIS_FREE_MARGIN:
            account_value = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
            filter_name = "Free Margin";
            break;

         case ACCOUNT_BASIS_MARGIN_PERCENTAGE:
         {
            // Margin Level = (Equity / Margin) * 100
            double margin = AccountInfoDouble(ACCOUNT_MARGIN);
            if(margin > 0)
               account_value = (AccountInfoDouble(ACCOUNT_EQUITY) / margin) * 100;
            else
               account_value = 999999.0;  // No positions - effectively infinite margin level
            filter_name = "Margin Percentage";
            break;
         }
      }

      // Check if account value meets the filter requirement
      if(account_value <= command.account_filter)
      {
         PrintFormat("[PineTunnel] Account filter NOT met - %s: %.2f <= %.2f",
                    filter_name, account_value, command.account_filter);
         return false;
      }
   }
   if(command.use_risk_sizing)
      PrintFormat("[PineTunnel] Risk: %.2f%% | SL: %.5f | TP: %.5f", command.risk_percent, command.stop_loss, command.take_profit);
   else
      PrintFormat("[PineTunnel] Lots: %.2f | SL: %.5f | TP: %.5f", command.lots, command.stop_loss, command.take_profit);
   if(command.comment != "")
      PrintFormat("[PineTunnel] Comment: %s", command.comment);
   bool success = false;
   switch(command.type)
   {
      case COMMAND_BUY:
      case COMMAND_SELL:
         success = ExecuteMarketOrder(command);
         break;

      // Pending orders
      case COMMAND_BUY_LIMIT:
      case COMMAND_SELL_LIMIT:
      case COMMAND_BUY_STOP:
      case COMMAND_SELL_STOP:
         success = ExecutePendingOrder(command);
         break;

      // Cancel orders
      case COMMAND_CANCEL_LONG:
         success = (g_pendingManager != NULL) ?
                   (g_pendingManager.CancelBuyOrders(command.symbol, command.comment) > 0) : false;
         break;

      case COMMAND_CANCEL_SHORT:
         success = (g_pendingManager != NULL) ?
                   (g_pendingManager.CancelSellOrders(command.symbol, command.comment) > 0) : false;
         break;

      // Phase 2: Partial close commands
      case COMMAND_CLOSE_LONG_PCT:
         if(g_partialManager != NULL)
         {
             bool move_to_be = (command.has_sl && command.stop_loss == 0);
            if(command.partial_close_pct > 0)
               success = g_partialManager.CloseLongByPercentage(command.symbol, command.partial_close_pct, move_to_be, command.comment);
            else
               success = g_partialManager.CloseLongPercentage(command.symbol, move_to_be, command.comment);
         }
         break;

      case COMMAND_CLOSE_SHORT_PCT:
         if(g_partialManager != NULL)
         {
             bool move_to_be = (command.has_sl && command.stop_loss == 0);
            if(command.partial_close_pct > 0)
               success = g_partialManager.CloseShortByPercentage(command.symbol, command.partial_close_pct, move_to_be, command.comment);
            else
               success = g_partialManager.CloseShortPercentage(command.symbol, move_to_be, command.comment);
         }
         break;

       case COMMAND_CLOSE_LONG_VOL:
          {
             bool move_to_be = (command.has_sl && command.stop_loss == 0);
             double close_volume = command.risk_percent > 0 ? command.risk_percent : command.lots;
             if(command.vol_type == "lots" || command.vol_type == "")
                close_volume = command.lots > 0 ? command.lots : (command.risk_percent > 0 ? command.risk_percent : InpDefaultLots);
             if(g_partialManager != NULL)
                success = g_partialManager.CloseLongVolume(command.symbol, close_volume, move_to_be, command.comment);
          }
          break;

       case COMMAND_CLOSE_SHORT_VOL:
          {
             bool move_to_be = (command.has_sl && command.stop_loss == 0);
             double close_volume = command.risk_percent > 0 ? command.risk_percent : command.lots;
             if(command.vol_type == "lots" || command.vol_type == "")
                close_volume = command.lots > 0 ? command.lots : (command.risk_percent > 0 ? command.risk_percent : InpDefaultLots);
             if(g_partialManager != NULL)
                success = g_partialManager.CloseShortVolume(command.symbol, close_volume, move_to_be, command.comment);
          }
          break;

      // Phase 2: Modification commands
       case COMMAND_SLTP__LONG:
          if(g_modifyManager != NULL)
         {
            // sl=0 means breakeven; absent sl means don't modify SL
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1; // -1 = no SL specified
            success = g_modifyManager.ModifyLongPositions(command.symbol, mod_sl, command.take_profit, command.comment);
         }
          break;

       case COMMAND_SLTP__SHORT:
          if(g_modifyManager != NULL)
         {
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1; // -1 = no SL specified
            success = g_modifyManager.ModifyShortPositions(command.symbol, mod_sl, command.take_profit, command.comment);
         }
         break;

       case COMMAND_SLTP_BUY_STOP:
          if(g_modifyManager != NULL)
         {
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1;
            success = g_modifyManager.ModifyBuyStopOrders(command.symbol, mod_sl, command.take_profit, command.comment);
         }
          break;

       case COMMAND_SLTP_BUY_LIMIT:
          if(g_modifyManager != NULL)
         {
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1;
            success = g_modifyManager.ModifyBuyLimitOrders(command.symbol, mod_sl, command.take_profit, command.comment);
         }
          break;

       case COMMAND_SLTP_SELL_STOP:
          if(g_modifyManager != NULL)
         {
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1;
            success = g_modifyManager.ModifySellStopOrders(command.symbol, mod_sl, command.take_profit, command.comment);
         }
          break;

       case COMMAND_SLTP_SELL_LIMIT:
          if(g_modifyManager != NULL)
         {
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1;
            success = g_modifyManager.ModifySellLimitOrders(command.symbol, mod_sl, command.take_profit, command.comment);
         }
          break;

       // Close positions
       case COMMAND_CLOSE_ALL:
          // PineTunnel spec: closeall requires symbol == ChartSymbol as safety measure
          if(command.symbol != _Symbol && command.symbol != "ea_off" && command.symbol != "ea_on")
          {
             PrintFormat("[PineTunnel] CLOSEALL rejected: signal symbol '%s' != chart symbol '%s' (PineTunnel spec: must match chart)", command.symbol, _Symbol);
             break;
          }
          Print("[PineTunnel] Close ALL positions and pending orders");
          // Close all positions
         success = ClosePositions("", (ENUM_POSITION_TYPE)-1, command.comment, command.nm);
         // Cancel ALL pending orders (PineTunnel spec)
         if(g_pendingManager != NULL)
         {
            int cancelled_buy = g_pendingManager.CancelBuyOrders("", command.comment);
            int cancelled_sell = g_pendingManager.CancelSellOrders("", command.comment);
            if(cancelled_buy > 0 || cancelled_sell > 0)
               PrintFormat("[PineTunnel] Cancelled %d pending order(s)", cancelled_buy + cancelled_sell);
         }
         break;
      case COMMAND_CLOSE_LONG:
         success = ClosePositions(command.symbol, POSITION_TYPE_BUY, command.comment, command.nm);
         break;
      case COMMAND_CLOSE_SHORT:
         success = ClosePositions(command.symbol, POSITION_TYPE_SELL, command.comment, command.nm);
         break;

      // Priority 1: close_all
      case COMMAND_CLOSE_LONG_SHORT:
         success = ClosePositions(command.symbol, (ENUM_POSITION_TYPE)-1, command.comment, command.nm);
         break;

      case COMMAND_EXIT:
         success = ClosePositions(command.symbol, (ENUM_POSITION_TYPE)-1, command.comment, command.nm);
         break;

      // Combined actions - close+open
      case COMMAND_CLOSE_LONG_OPEN_LONG:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongOpenLong(command.symbol, command.lots,
                                                 command.stop_loss, command.take_profit, command.comment);
            success = ExecuteMarketOrder(command);  // Open new long
         }
         break;

      case COMMAND_CLOSE_LONG_OPEN_SHORT:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongOpenShort(command.symbol, command.lots,
                                                  command.stop_loss, command.take_profit, command.comment);
            SignalCommand sellCmd = command;
            sellCmd.type = COMMAND_SELL;
            success = ExecuteMarketOrder(sellCmd);  // Open new short
         }
         break;

      case COMMAND_CLOSE_SHORT_OPEN_LONG:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseShortOpenLong(command.symbol, command.lots,
                                                  command.stop_loss, command.take_profit, command.comment);
            SignalCommand buyCmd = command;
            buyCmd.type = COMMAND_BUY;
            success = ExecuteMarketOrder(buyCmd);  // Open new long
         }
         break;

      case COMMAND_CLOSE_SHORT_OPEN_SHORT:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseShortOpenShort(command.symbol, command.lots,
                                                   command.stop_loss, command.take_profit, command.comment);
            success = ExecuteMarketOrder(command);  // Open new short
         }
         break;

      case COMMAND_CLOSE_LONGSHORT_OPEN_LONG:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongShortOpenLong(command.symbol, command.lots,
                                                      command.stop_loss, command.take_profit, command.comment);
            SignalCommand buyCmd = command;
            buyCmd.type = COMMAND_BUY;
            success = ExecuteMarketOrder(buyCmd);  // Open new long
         }
         break;

      case COMMAND_CLOSE_LONGSHORT_OPEN_SHORT:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongShortOpenShort(command.symbol, command.lots,
                                                       command.stop_loss, command.take_profit, command.comment);
            SignalCommand sellCmd = command;
            sellCmd.type = COMMAND_SELL;
            success = ExecuteMarketOrder(sellCmd);  // Open new short
         }
         break;

      // Combined actions - cancel+place
      case COMMAND_CANCEL_LONG_BUY_STOP:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelLongBuyStop(command.symbol, command.lots,
                                                 command.pending_distance, command.stop_loss,
                                                 command.take_profit, command.comment);
            SignalCommand stopCmd = command;
            stopCmd.type = COMMAND_BUY_STOP;
            success = ExecutePendingOrder(stopCmd);
         }
         break;

      case COMMAND_CANCEL_LONG_BUY_LIMIT:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelLongBuyLimit(command.symbol, command.lots,
                                                  command.pending_distance, command.stop_loss,
                                                  command.take_profit, command.comment);
            SignalCommand limitCmd = command;
            limitCmd.type = COMMAND_BUY_LIMIT;
            success = ExecutePendingOrder(limitCmd);
         }
         break;

      case COMMAND_CANCEL_SHORT_SELL_STOP:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelShortSellStop(command.symbol, command.lots,
                                                   command.pending_distance, command.stop_loss,
                                                   command.take_profit, command.comment);
            SignalCommand stopCmd = command;
            stopCmd.type = COMMAND_SELL_STOP;
            success = ExecutePendingOrder(stopCmd);
         }
         break;

      case COMMAND_CANCEL_SHORT_SELL_LIMIT:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelShortSellLimit(command.symbol, command.lots,
                                                    command.pending_distance, command.stop_loss,
                                                    command.take_profit, command.comment);
            SignalCommand limitCmd = command;
            limitCmd.type = COMMAND_SELL_LIMIT;
            success = ExecutePendingOrder(limitCmd);
         }
         break;

      // EA Management
      case COMMAND_EA_OFF:
         // PineTunnel spec: symbol must also be "ea_off"
         if(command.symbol != "ea_off")
         {
            PrintFormat("[PineTunnel] ERROR: EAOFF requires symbol='eaoff', got '%s'", command.symbol);
            PrintFormat("[PineTunnel] Correct syntax: %s,eaoff,eaoff", InpLicenseID);
            success = false;
            break;
         }
         PrintFormat("[PineTunnel] EAOFF - Disabling EA");
         g_eaEnabled = false;
         success = true;
         break;

      case COMMAND_EA_ON:
         // PineTunnel spec: symbol must also be "ea_on"
         if(command.symbol != "ea_on")
         {
            PrintFormat("[PineTunnel] ERROR: EAON requires symbol='eaon', got '%s'", command.symbol);
            PrintFormat("[PineTunnel] Correct syntax: %s,eaon,eaon", InpLicenseID);
            success = false;
            break;
         }
         PrintFormat("[PineTunnel] EAON - Enabling EA");
         g_eaEnabled = true;
         g_protectionHalted = false;  // Clear persistent halt
         g_dailyHalted = false;       // Clear daily halt
         success = true;
         Print("[PineTunnel] Account protection halts cleared");
         break;

       case COMMAND_CLOSEALL_EA_OFF:
          // PineTunnel spec: close_all_off requires symbol == ChartSymbol as safety measure
          if(command.symbol != _Symbol)
          {
             PrintFormat("[PineTunnel] CLOSE_ALL_OFF rejected: signal symbol '%s' != chart symbol '%s' (PineTunnel spec: must match chart)", command.symbol, _Symbol);
             break;
          }
          PrintFormat("[PineTunnel] CLOSE_ALL_OFF %s", command.symbol);
         // Close all positions
         success = ClosePositions("", (ENUM_POSITION_TYPE)-1, command.comment, command.nm);
         // Cancel ALL pending orders (PineTunnel spec)
         if(g_pendingManager != NULL)
         {
            int cancelled_buy = g_pendingManager.CancelBuyOrders("", command.comment);
            int cancelled_sell = g_pendingManager.CancelSellOrders("", command.comment);
            if(cancelled_buy > 0 || cancelled_sell > 0)
               PrintFormat("[PineTunnel] Cancelled %d pending order(s)", cancelled_buy + cancelled_sell);
         }
         // Then disable EA
         g_eaEnabled = false;
         Print("[PineTunnel] All positions/orders closed and EA DISABLED");
         break;

      default:
         PrintFormat("[PineTunnel] ERROR: Unknown command type: %d", command.type);
         break;
   }
   // from_queue=true: counters updated by DrainSignalQueue on final outcome, not per-retry
   if(!from_queue)
   {
       if(success)
       {
          g_successful++;
          g_todaySuccessful++;
          PrintFormat("[PineTunnel] Signal #%d executed successfully", g_totalSignals);
          PrintFormat("[Exec] [TIMING] Signal #%d end-to-end: %I64ums | Cmd=%s Sym=%s", g_totalSignals, (ulong)(GetTickCount64() - exec_start_tick), CommandName(command.type), command.symbol);
       }
       else
       {
          g_failed++;
          g_todayFailed++;
          PrintFormat("[PineTunnel] Signal #%d execution FAILED", g_totalSignals);
          PrintFormat("[Exec] [TIMING] Signal #%d FAILED after %I64ums | Cmd=%s Sym=%s", g_totalSignals, (ulong)(GetTickCount64() - exec_start_tick), CommandName(command.type), command.symbol);
       }
      PrintFormat("[PineTunnel] Stats - Total: %d | Success: %d | Failed: %d", g_totalSignals, g_successful, g_failed);
    }
    // Mark signal as executed on success (prevents duplicates on restart)
   if(success && command.signal_id != "")
      SaveExecutedSignal(command.signal_id);
   return success;
}
//--- connection health tracking ----------------------------------------
void UpdateConnectionHealth(ulong response_time_ms, bool success)
{
   if(success)
   {
      g_lastPollSuccess = true;
      g_consecutiveErrors = 0;
   }
   else
   {
      g_lastPollSuccess = false;
      g_consecutiveErrors++;
   }
}
//--- HTTP polling ---------------------------------------------------
bool PollForSignals()
{
   // Non-blocking HTTP backoff check - skip request if still in cooldown
   // (replaces the old Sleep(60000) which blocked the entire OnTimer loop)
   if(g_httpBackoffUntil > 0 && TimeLocal() < g_httpBackoffUntil)
      return false;
   g_httpBackoffUntil = 0;  // Backoff expired

   ulong start_time = GetTickCount64(); // Track response time

    string correlation_id = IntegerToString(GetTickCount64()) + "-" + IntegerToString(GetMicrosecondCount());
    string url;
    int timeout;

    if(g_useLongPoll && !g_longPollFailed)
    {
       url = InpServerURL + "/api/signals-longpoll/" + InpLicenseID + "?timeout=5";
       timeout = 6000;
    }
    else if(g_useLongPoll && g_longPollFailed && g_longPollRetryTime > 0 && TimeCurrent() >= g_longPollRetryTime)
    {

       g_longPollFailed = false;
       url = InpServerURL + "/api/signals-longpoll/" + InpLicenseID + "?timeout=5";
       timeout = 6000;
    }
    else
    {
       url = InpServerURL + "/api/signals/" + InpLicenseID;
       timeout = 5000;
    }

    string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";
    char data[];
    char result[];
    string result_headers;

   // Polling silently - only log on signals or errors

   // Make GET request
   int res = WebRequest(
      "GET",
      url,
      headers,
      timeout,
      data,
      result,
      result_headers
   );

   ulong response_time = GetTickCount64() - start_time; // Calculate response time

   if(res == -1)
   {
      int error = GetLastError();
      PrintFormat("[PineTunnel] HTTP request failed, error: %d", error);

      if(error == 4060)
      {
         PrintFormat("[PineTunnel] WebRequest not allowed for URL: %s", url);
         PrintFormat("[PineTunnel] Add this URL to Tools -> Options -> Expert Advisors -> Allow WebRequest for listed URLs");
      }

      UpdateConnectionHealth(response_time, false);
      return false;
   }

    if(res != 200)
    {
        // Long-polling fallback: if server returns 404 on longpoll, switch to standard polling
        if(res == 404 && g_useLongPoll && !g_longPollFailed)
        {
           g_longPollFailed = true;
           g_longPollRetryTime = TimeCurrent() + LONGPOLL_RETRY_INTERVAL;
           PrintFormat("[PineTunnel] Long-polling not available (404), falling back to standard polling. Retry at %s", TimeToString(g_longPollRetryTime));
           return PollForSignals();
        }
      if(res == 429)
      {
         // HTTP 429 - Too Many Requests - implement rate limiting backoff
         PrintFormat("[PineTunnel] Rate limit hit (HTTP 429) - applying 30 second backoff");
         UpdateConnectionHealth(response_time, false);
         g_httpBackoffUntil = TimeLocal() + 30;
         return false;
      }
      // Server error backoff - non-blocking (no Sleep) to avoid stalling WS reconnect
      else if(res == 500 || res == 503 || res == 502 || res == 504)
      {
         // 502/504 are typically transient (worker cycling, load balancer) - short backoff
         // 500/503 suggest real server problems - longer backoff
         int backoffSec;
         if(res == 502 || res == 504)
         {
            // Transient: 5s first attempt, escalating to max 30s
            backoffSec = MathMin(5 * g_consecutiveErrors, 30);
            if(backoffSec < 5) backoffSec = 5;
         }
         else
         {
            // Persistent: 15s first attempt, escalating to max 60s
            backoffSec = MathMin(15 * g_consecutiveErrors, 60);
            if(backoffSec < 15) backoffSec = 15;
         }
         PrintFormat("[PineTunnel] Server error (HTTP %d) - applying %d second backoff (non-blocking)", res, backoffSec);
         UpdateConnectionHealth(response_time, false);
         g_httpBackoffUntil = TimeLocal() + backoffSec;
         return false;
      }
      else
      {
         PrintFormat("[PineTunnel] Server returned status: %d", res);
      }
      UpdateConnectionHealth(response_time, false);
      return false;
   }

   // Parse JSON response
   string json_response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);

   // V7.01: Validate server status field
   int status_pos = StringFind(json_response, "\"status\":");
   if(status_pos >= 0)
   {
      string status = ExtractStringValue(json_response, "status");
      if(status != "success" && status != "ok" && status != "")
      {
         PrintFormat("[PineTunnel] Server returned status: %s - treating as error", status);
         UpdateConnectionHealth(response_time, false);
         return false;
      }
   }

   // SUCCESS: HTTP 200 received - reset error tracking
   g_consecutiveErrors = 0;  // Reset on success
   g_lastSuccessfulRequest = TimeCurrent();  // Update last success time
   g_httpBackoffUntil = 0;  // Clear any pending HTTP backoff

   // Check for EA update notification (throttled to every 5 minutes)
   if((TimeCurrent() - g_lastVersionCheck) >= VERSION_CHECK_INTERVAL)
   {
      g_lastVersionCheck = TimeCurrent();
      string server_version = ExtractStringValue(json_response, "latest_version_mt5");
      if(server_version != "")
      {
         if(server_version != g_latestVersion)
         {
            g_latestVersion = server_version;
            g_updateNotes = ExtractStringValue(json_response, "update_notes_mt5");
         }
         // Compare versions: server > local means update available
         if(CompareVersions(server_version, PT_VERSION) > 0)
         {
            if(!g_updateAvailable)
            {
               // First time detecting update - log once
               PrintFormat("[PineTunnel] UPDATE AVAILABLE: v%s -> v%s", PT_VERSION, server_version);
               if(g_updateNotes != "")
                  PrintFormat("[PineTunnel] Release notes: %s", g_updateNotes);
            }
            g_updateAvailable = true;
            g_lastVersionCheck = 0;  // Force CheckForEAUpdate on next OnTimer tick
         }
         else
         {
            g_updateAvailable = false;
         }
      }
   }

   // Server response received - only log if there are signals

   // Check for signals
   if(StringFind(json_response, "signals") < 0)
   {
      UpdateConnectionHealth(response_time, true);
      return true; // No signals - silent
   }

   // Find signals array in JSON
   int signals_start = StringFind(json_response, "\"signals\":[");
   if(signals_start < 0)
   {
      UpdateConnectionHealth(response_time, true);
      return true;
   }

   signals_start += 11; // Move past "signals":[
   int bracket_depth = 1;
   int signals_end = signals_start;
   bool arr_in_string = false;
   int json_len = StringLen(json_response);
   while(signals_end < json_len && bracket_depth > 0)
   {
      ushort ch = StringGetCharacter(json_response, signals_end);
      if(ch == '"' && (signals_end == 0 || StringGetCharacter(json_response, signals_end - 1) != '\\'))
         arr_in_string = !arr_in_string;
      if(!arr_in_string)
      {
         if(ch == '[' || ch == '{') bracket_depth++;
         else if(ch == ']' || ch == '}') bracket_depth--;
      }
      signals_end++;
   }
   if(bracket_depth != 0)
   {
      UpdateConnectionHealth(response_time, true);
      return true;
   }

   string signals_json = StringSubstr(json_response, signals_start, signals_end - signals_start);

   // Check if signals array is empty
   StringTrimLeft(signals_json);
   StringTrimRight(signals_json);
   if(signals_json == "")
   {
      UpdateConnectionHealth(response_time, true);
      return true; // Empty signals array - silent
   }

    // We have actual signals

    PrintFormat("[Exec] [TIMING] Signal poll response: %I64ums | tick=%I64u", response_time, GetTickCount64());

   // Parse each signal object in the array
   int pos = 0;
   int signal_count = 0;

   // Batch ACK: collect signal IDs during processing, send one HTTP call at the end
   string  batch_ack_ids[];
   bool    batch_ack_saved[];   // true = SaveExecutedSignal should be called after ACK
   int     batch_ack_count = 0;
   ArrayResize(batch_ack_ids, BATCH_ACK_MAX);
   ArrayResize(batch_ack_saved, BATCH_ACK_MAX);

    while(pos < StringLen(signals_json) && signal_count < MAX_SIGNALS_PER_TICK)
    {
       // Find next object
       int obj_start = StringFind(signals_json, "{", pos);
       if(obj_start < 0) break;

       // V7.01: Use bracket counting to handle nested JSON objects
       int bracket_count = 0;
       int obj_end = obj_start;
       bool found_complete = false;
       bool in_string = false;
      int sig_len = StringLen(signals_json);
       for(int i = obj_start; i < sig_len && !found_complete; i++)
      {
         ushort ch = StringGetCharacter(signals_json, i);
         if(ch == '"' && (i == 0 || StringGetCharacter(signals_json, i - 1) != '\\'))
            in_string = !in_string;
         if(!in_string)
         {
            if(ch == '{')
               bracket_count++;
               else if(ch == '}')
               {
                  bracket_count--;
                  if(bracket_count == 0)
                  {
                     obj_end = i;
                     found_complete = true;
                  }
               }
            }
         }

       if(!found_complete) break;  // Incomplete JSON object

      string signal_json = StringSubstr(signals_json, obj_start, obj_end - obj_start + 1);

      SignalCommand cmd = ParseSignal(signal_json);

      if(cmd.type != COMMAND_NONE)
      {
         signal_count++;
         PrintFormat("[PineTunnel] Signal #%d: %s on %s (ID: %s)",
                     signal_count, CommandName(cmd.type), cmd.symbol, cmd.signal_id);

         bool queued = false;

         // PRE-CHECK: queue signal immediately if market is closed (belt-and-suspenders part 1)
         if(InpEnableSignalQueue && g_signalQueue != NULL && IsQueueableCommandType(cmd.type))
         {
            if(!IsMarketOpenForSymbol(cmd.symbol))
            {
               queued = true;
               g_signalQueue.Push(cmd, false);  // is_retry=false: not yet attempted
               PrintFormat("[Queue] Queued (market closed): %s on %s | Queue size: %d",
                           CommandName(cmd.type), cmd.symbol, g_signalQueue.Size());
            }
         }

         if(!queued)
         {
            // V7.01: Execute command FIRST
            bool exec_ok = ExecuteCommand(cmd);

            // POST-CHECK: queue if execution FAILED and market is closed OR error 10018
            // (10018/TRADE_RETCODE_MARKET_CLOSED fires when SYMBOL_TRADE_MODE_FULL is set but broker matching engine is not
            //  yet accepting orders during the ~30-90s open-auction window - PRE-CHECK misses this)
            if(!exec_ok && InpEnableSignalQueue && g_signalQueue != NULL && IsQueueableCommandType(cmd.type))
            {
               if(!IsMarketOpenForSymbol(cmd.symbol) || g_lastExecError == 10018)
               {
                  queued = true;
                  g_signalQueue.Push(cmd, true);  // is_retry=true: lock file may exist
                  string reason = (g_lastExecError == 10018 && IsMarketOpenForSymbol(cmd.symbol))
                                  ? "open-transition (10018)" : "market closed";
                  PrintFormat("[Queue] Queued (%s): %s on %s | Queue size: %d",
                              reason, CommandName(cmd.type), cmd.symbol, g_signalQueue.Size());
               }
            }
         }

         // Collect signal ID for batch ACK instead of individual ACK per signal
         if(cmd.signal_id != "" && batch_ack_count < BATCH_ACK_MAX)
         {
            batch_ack_ids[batch_ack_count] = cmd.signal_id;
            batch_ack_saved[batch_ack_count] = !queued;
            batch_ack_count++;
         }
      }

      pos = obj_end + 1;
   }

   // Send batch ACK for all processed signals in one HTTP call
   if(batch_ack_count > 0)
   {
      bool ack_results[];
      bool all_ok = AcknowledgeSignalsBatch(batch_ack_ids, batch_ack_count, ack_results);

      // Record executed signals where ACK succeeded
      for(int i = 0; i < batch_ack_count; i++)
      {
         if(batch_ack_saved[i] && (all_ok || (i < ArraySize(ack_results) && ack_results[i])))
         {
            SaveExecutedSignal(batch_ack_ids[i]);
         }
      }

      if(all_ok)
         PrintFormat("[BATCH-ACK] All %d signals acknowledged", batch_ack_count);
      else
         PrintFormat("[BATCH-ACK] Batch ACK had failures - signals will be re-delivered on next poll");
   }

   if(signal_count > 0)
      PrintFormat("[PineTunnel] Processed %d signal(s)", signal_count);
   if(signal_count >= MAX_SIGNALS_PER_TICK && pos < StringLen(signals_json))
      PrintFormat("[PineTunnel] %d signal(s) deferred to next poll (limit %d)", signal_count, MAX_SIGNALS_PER_TICK);

   UpdateConnectionHealth(response_time, true);
   return true;
}
//+------------------------------------------------------------------+
//| Returns true if this command type should be queued on mkt close  |
//+------------------------------------------------------------------+
bool IsQueueableCommandType(CommandType type)
{
   // EA management commands are never queued - they should execute immediately
   if(type == COMMAND_EA_OFF || type == COMMAND_EA_ON || type == COMMAND_CLOSEALL_EA_OFF)
      return false;
   return (type != COMMAND_NONE);
}
//+------------------------------------------------------------------+
//| Drain queued signals now that market may have opened             |
//+------------------------------------------------------------------+
void DrainSignalQueue()
{
   if(g_signalQueue == NULL || g_signalQueue.IsEmpty())
      return;

   // Throttle status logging - only print every 60s to avoid log spam
   static datetime lastDrainLog = 0;
   bool shouldLog = (TimeCurrent() - lastDrainLog >= 60);

   int size = g_signalQueue.Size();
   if(shouldLog)
   {
      PrintFormat("[Queue] Market open check - draining %d queued signal(s)", size);
      lastDrainLog = TimeCurrent();
   }

   int i = 0;
   int drain_count = 0;
   while(i < g_signalQueue.Size() && drain_count < MAX_SIGNALS_PER_TICK)
   {
      SQueuedSignal item;
      if(!g_signalQueue.Peek(i, item)) { i++; continue; }

      // Check if signal has expired (queued too long)
      int queueTime = (int)(TimeCurrent() - item.queued_time);
      if(queueTime > SQ_MAX_QUEUE_TIME_SEC)
      {
         PrintFormat("[Queue] Signal expired: %s on %s (queued %ds, max %ds) - abandoning",
                     CommandName(item.cmd.type), item.cmd.symbol, queueTime, SQ_MAX_QUEUE_TIME_SEC);

         g_failed++;
         g_todayFailed++;
         g_signalQueue.Remove(i);
         continue;  // i stays same, next item shifted into position
      }

      // Only execute if market is now open for this symbol
      if(!IsMarketOpenForSymbol(item.cmd.symbol))
      {
         i++;
         continue;
      }

      int waited = (int)(TimeCurrent() - item.queued_time);
      PrintFormat("[Queue] Executing queued signal: %s on %s (waited %ds)",
                  CommandName(item.cmd.type), item.cmd.symbol, waited);

      // Execute - pass is_retry so lock-file check is skipped when needed
      bool exec_ok = ExecuteCommand(item.cmd, item.is_retry);
      drain_count++;

      // If broker still rejecting with 10018 (open-auction window), keep in queue and retry
      // next OnTimer tick rather than silently dropping the signal.
      if(!exec_ok && g_lastExecError == 10018)
      {
         int retries = g_signalQueue.IncrDrainRetries(i);
         if(retries >= SQ_MAX_DRAIN_RETRIES)
         {
            PrintFormat("[Queue] Max drain retries (%d) for %s on %s - signal abandoned after %.0fs",
                        SQ_MAX_DRAIN_RETRIES, CommandName(item.cmd.type), item.cmd.symbol,
                        (double)(TimeCurrent() - item.queued_time));

            // Signal permanently failed - update counters
            g_failed++;
            g_todayFailed++;

            g_signalQueue.Remove(i);
            // i stays the same - next item shifted into position i
         }
         else
         {
            PrintFormat("[Queue] 10018 retry %d/%d for %s on %s - next tick",
                        retries, SQ_MAX_DRAIN_RETRIES,
                        CommandName(item.cmd.type), item.cmd.symbol);
            i++;  // Leave item, advance to next
         }
         continue;
      }

      // Remove from queue (success or non-retryable failure)
      g_signalQueue.Remove(i);
      // i stays the same - next item shifted into position i

      // Update counters based on final outcome
      if(exec_ok)
      {
         g_successful++;
         g_todaySuccessful++;
         PrintFormat("[Queue] Signal executed from queue successfully");
      }
      else
      {
         // Non-retryable failure (not 10018)
         g_failed++;
         g_todayFailed++;
         PrintFormat("[Queue] Signal failed with non-retryable error");
      }

      // Record execution in persistent executed-signal log (only on success)
      if(exec_ok && item.cmd.signal_id != "")
         SaveExecutedSignal(item.cmd.signal_id);
   }

   if(g_signalQueue.IsEmpty())
      Print("[Queue] Signal queue drained");
   else if(drain_count >= MAX_SIGNALS_PER_TICK)
      PrintFormat("[Queue] Drained %d/%d signals this tick (limit %d) - %d remaining",
                  drain_count, size, MAX_SIGNALS_PER_TICK, g_signalQueue.Size());
   else if(shouldLog)
      PrintFormat("[Queue] %d signal(s) still waiting (market closed for their symbols)",
                  g_signalQueue.Size());
}
bool AcknowledgeSignal(const string signal_id)
{
   string correlation_id = IntegerToString(GetTickCount64()) + "-" + IntegerToString(GetMicrosecondCount());
   string url = InpServerURL + "/api/signals/" + InpLicenseID + "/" + signal_id;
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";
   char data[];
   char result[];
   string result_headers;

   int timeout = 5000;
   ulong start_time = GetTickCount64();

   PrintFormat("[ACK] Acknowledging signal: %s | License: %s", signal_id, InpLicenseID);

   // Build JSON body for DELETE request
   string json = "{";
   json += "\"signal_id\":\"" + signal_id + "\",";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   json += "\"account\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   json += "\"broker\":\"" + EscapeJSON(AccountInfoString(ACCOUNT_COMPANY)) + "\"";
   json += "}";

   // Convert JSON to char array
   StringToCharArray(json, data, 0, StringLen(json));

   // Make DELETE request to acknowledge
   int res = WebRequest(
      "DELETE",
      url,
      headers,
      timeout,
      data,
      result,
      result_headers
   );

   ulong elapsed = GetTickCount64() - start_time;

   if(res == 200)
   {
      PrintFormat("[ACK] Signal %s acknowledged successfully | Response time: %d ms", signal_id, elapsed);

      return true;
   }
   else
   {
      if(res == -1)
      {
         int error = GetLastError();
         PrintFormat("[ACK] FAILED to acknowledge %s | WebRequest Error: %d (%s) | Time: %d ms",
                     signal_id, error, ErrorDescription(error), elapsed);
      }
      else
      {
         PrintFormat("[ACK] FAILED to acknowledge %s | HTTP Status: %d | Time: %d ms",
                     signal_id, res, elapsed);
      }

      return false;
   }
}

//+------------------------------------------------------------------+
//| Batch acknowledge multiple signals in one HTTP call              |
//+------------------------------------------------------------------+
bool AcknowledgeSignalsBatch(const string &signal_ids[], int count, bool &results[])
{
   if(count <= 0) return true;
   if(count == 1)  // Single signal: use existing individual ACK
   {
      ArrayResize(results, 1);
      results[0] = AcknowledgeSignal(signal_ids[0]);
      return results[0];
   }

   string correlation_id = IntegerToString(GetTickCount64()) + "-" + IntegerToString(GetMicrosecondCount());
   string url = InpServerURL + "/api/signals-batch-ack/" + InpLicenseID;
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";

   // Build JSON body: {"signal_ids": ["id1", "id2", ...]}
   string json = "{\"signal_ids\":[";
   for(int i = 0; i < count && i < BATCH_ACK_MAX; i++)
   {
      if(i > 0) json += ",";
      json += "\"" + signal_ids[i] + "\"";
   }
   json += "]}";

   char data[];
   char result[];
   string result_headers;
   StringToCharArray(json, data, 0, StringLen(json));

   int timeout = 5000;
   ulong start_time = GetTickCount64();

   PrintFormat("[BATCH-ACK] Sending %d signal ACKs", count);

   int res = WebRequest(
      "POST",
      url,
      headers,
      timeout,
      data,
      result,
      result_headers
   );

   ulong elapsed = GetTickCount64() - start_time;

   if(res == 200)
   {
      PrintFormat("[BATCH-ACK] %d signals acknowledged | Response time: %d ms", count, elapsed);

      // Parse acknowledged array from response to set per-signal results
      ArrayResize(results, count);
      string response = CharArrayToString(result);

      // Default: all acknowledged (server returns 200)
      for(int i = 0; i < count; i++)
         results[i] = true;

      // Check for partial failures in response: {"acknowledged": [...], "failed": [...]}
      int failed_pos = StringFind(response, "\"failed\":[");
      if(failed_pos >= 0)
      {
         int arr_start = failed_pos + 10;
         int arr_end = StringFind(response, "]", arr_start);
         if(arr_end > arr_start)
         {
            string failed_arr = StringSubstr(response, arr_start, arr_end - arr_start);
            for(int i = 0; i < count; i++)
            {
               // Search with surrounding quotes to avoid partial ID matches
               if(StringFind(failed_arr, "\"" + signal_ids[i] + "\"") >= 0)
                  results[i] = false;
            }
         }
      }

      // Return true only if ALL signals were acknowledged
      bool all_ok = true;
      for(int i = 0; i < count; i++)
      {
         if(!results[i]) { all_ok = false; break; }
      }
      return all_ok;
   }
   else
   {
      if(res == -1)
      {
         int error = GetLastError();
         PrintFormat("[BATCH-ACK] FAILED | WebRequest Error: %d | Time: %d ms", error, elapsed);
      }
      else
      {
         PrintFormat("[BATCH-ACK] FAILED | HTTP Status: %d | Time: %d ms", res, elapsed);
      }

      // Fallback: send individual ACKs for each signal
      PrintFormat("[BATCH-ACK] Falling back to individual ACKs");
      ArrayResize(results, count);
      bool all_ok = true;
      for(int i = 0; i < count; i++)
      {
         results[i] = AcknowledgeSignal(signal_ids[i]);
         if(!results[i]) all_ok = false;
      }
      return all_ok;
   }
}
//+------------------------------------------------------------------+
//| Helper function to get error description                         |
//+------------------------------------------------------------------+
string ErrorDescription(int error_code)
{
   switch(error_code)
   {
      case 4014: return "Function not allowed (WebRequest blocked)";
      case 5203: return "Invalid URL";
      case 4060: return "Function not confirmed";
      case 4062: return "Array overflow";
      case 4066: return "No memory for buffers";
      case 4067: return "Invalid buffer size";
      default:  return "Error code " + IntegerToString(error_code);
   }
}
//+------------------------------------------------------------------+
//| Helper: Escape JSON strings (replace quotes and backslashes)    |
//+------------------------------------------------------------------+
string EscapeJSON(string str)
{
   string result = str;
   // Escape backslashes first (must be first!)
   StringReplace(result, "\\", "\\\\");
   // Escape quotes
   StringReplace(result, "\"", "\\\"");
   // Escape newlines and tabs for safety
   StringReplace(result, "\n", "\\n");
   StringReplace(result, "\r", "\\r");
   StringReplace(result, "\t", "\\t");
   return result;
}
//+------------------------------------------------------------------+
//| Send trade execution report to server for analytics             |
//+------------------------------------------------------------------+
bool SendTradeReport(string action, string symbol, double volume, double price,
                     ulong ticket, bool success, string error_msg = "", string signal_id = "")
{
   // Escape strings for JSON safety
   string safe_error = EscapeJSON(error_msg);
   string safe_broker = EscapeJSON(AccountInfoString(ACCOUNT_COMPANY));
   string safe_action = EscapeJSON(action);
   string safe_symbol = EscapeJSON(symbol);
   string safe_signal_id = EscapeJSON(signal_id);

   // Build the JSON payload for trade report
   string json = "{";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"action\":\"" + safe_action + "\",";
   json += "\"symbol\":\"" + safe_symbol + "\",";
   json += "\"volume\":" + DoubleToString(volume, 2) + ",";
   json += "\"price\":" + DoubleToString(price, 5) + ",";
   json += "\"ticket\":" + IntegerToString(ticket) + ",";
   json += "\"success\":" + (success ? "true" : "false") + ",";
   json += "\"error_msg\":\"" + safe_error + "\",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";

   // Add position details if successful
   if(success && ticket > 0)
   {
      if(g_position.SelectByTicket(ticket))
      {
         json += "\"profit\":" + DoubleToString(g_position.Profit(), 2) + ",";
         json += "\"sl\":" + DoubleToString(g_position.StopLoss(), 5) + ",";
         json += "\"tp\":" + DoubleToString(g_position.TakeProfit(), 5) + ",";
         json += "\"commission\":" + DoubleToString(g_position.Commission(), 2) + ",";
         json += "\"swap\":" + DoubleToString(g_position.Swap(), 2) + ",";
      }
   }

   // Add timestamp
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";
   json += "\"broker_time\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";
   json += "\"account\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   json += "\"broker\":\"" + safe_broker + "\"";
   if(safe_signal_id != "")
      json += ",\"signal_id\":\"" + safe_signal_id + "\"";
   json += "}";

   // Prepare request
   string correlation_id = IntegerToString(GetTickCount64()) + "-" + IntegerToString(GetMicrosecondCount());
   string url = InpServerURL + "/api/trades/report";
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";

   // Convert JSON to char array
   char post_data[];
   char result[];
   StringToCharArray(json, post_data, 0, StringLen(json));

   // Send async POST request (fire and forget for performance)
   int res = WebRequest(
      "POST",
      url,
      headers,
      3000,  // 3 second timeout (shorter for non-blocking)
      post_data,
      result,
      headers
   );

   if(res == -1)
   {
      int error = GetLastError();
      if(error == 4014)  // WebRequest not allowed
         return false;
      // Silently fail for other errors
      return false;
   }

   // Check response (but don't block trading on failure)
   if(res == 200 || res == 201)
   {
      // Trade reported - silent
      return true;
   }

   // Silently fail on non-200 responses
   return false;
}
//+------------------------------------------------------------------+
//| Send position close report to server                            |
//+------------------------------------------------------------------+
bool SendCloseReport(string symbol, ulong ticket, double close_price, double profit, string signal_id = "")
{
   // Escape strings for JSON safety
   string safe_symbol = EscapeJSON(symbol);
   string safe_broker = EscapeJSON(AccountInfoString(ACCOUNT_COMPANY));
   string safe_signal_id = EscapeJSON(signal_id);

   // Build JSON for close report
   string json = "{";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"action\":\"CLOSE\",";
   json += "\"symbol\":\"" + safe_symbol + "\",";
   json += "\"ticket\":" + IntegerToString(ticket) + ",";
   json += "\"close_price\":" + DoubleToString(close_price, 5) + ",";
   json += "\"profit\":" + DoubleToString(profit, 2) + ",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";
   json += "\"account\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   json += "\"broker\":\"" + safe_broker + "\"";
   if(safe_signal_id != "")
      json += ",\"signal_id\":\"" + safe_signal_id + "\"";
   json += "}";

   // Send to server
   string correlation_id = IntegerToString(GetTickCount64()) + "-" + IntegerToString(GetMicrosecondCount());
   string url = InpServerURL + "/api/trades/close";
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";

   char post_data[];
   char result[];
   StringToCharArray(json, post_data, 0, StringLen(json));

   int res = WebRequest(
      "POST",
      url,
      headers,
      3000,  // 3 second timeout
      post_data,
      result,
      headers
   );

   // Check result but don't interrupt trading
   if(res == 200 || res == 201)
   {
      // Close reported - silent
      return true;
   }

   return false;
}
//+------------------------------------------------------------------+
//| Send periodic account stats snapshot to server                  |
//+------------------------------------------------------------------+
void SendAccountStats()
{
   string safe_broker = EscapeJSON(AccountInfoString(ACCOUNT_COMPANY));
   string safe_currency = EscapeJSON(AccountInfoString(ACCOUNT_CURRENCY));
   string safe_name = EscapeJSON(AccountInfoString(ACCOUNT_NAME));
   string dll_ver = "";
   if(g_wsClient != NULL && g_wsClient.IsConnected())
      dll_ver = g_wsClient.GetDllVersion();

   string json = "{";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"account\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   json += "\"account_name\":\"" + safe_name + "\",";
   json += "\"broker\":\"" + safe_broker + "\",";
   json += "\"currency\":\"" + safe_currency + "\",";
   json += "\"leverage\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
   json += "\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
   json += "\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
   json += "\"profit\":" + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2) + ",";
   json += "\"margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + ",";
   json += "\"margin_free\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ",";
   json += "\"margin_level\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2) + ",";
   json += "\"open_positions\":" + IntegerToString(PositionsTotal()) + ",";
   json += "\"pending_orders\":" + IntegerToString(OrdersTotal()) + ",";
   json += "\"ea_version\":\"" + PT_VERSION + "\",";
   json += "\"dll_version\":\"" + (dll_ver != "" ? dll_ver : "N/A") + "\",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"";
   json += "}";

   string correlation_id = IntegerToString(GetTickCount64()) + "-" + IntegerToString(GetMicrosecondCount());
   string url = InpServerURL + "/api/trades/stats";
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";

   char post_data[];
   char result[];
   StringToCharArray(json, post_data, 0, StringLen(json));

   int res = WebRequest("POST", url, headers, 5000, post_data, result, headers);

   if(res == -1)
   {
      int error = GetLastError();
      if(error == 4014)
         return;  // WebRequest not allowed - silent
      return;
   }

   // Account stats sent silently - no logging
}
//+------------------------------------------------------------------+
//| Process WebSocket signals - route by message type                  |
//| Server messages: {"type":"signal","signals":[{...}]}                |
//|                  {"type":"pong","timestamp":...}                    |
//|                  {"type":"version","latest_version_mt5":"..."}      |
//|                  {"type":"shutdown","reason":"server_restart"}     |
//+------------------------------------------------------------------+
void ProcessWebSocketSignals(string json_message)
{
   if(json_message == "")
      return;

   // Extract message type
   string msg_type = ExtractStringValue(json_message, "type");

    // -- PONG: Heartbeat response --
     if(msg_type == "pong")
     {
        return;
     }

    // -- KEEPALIVE: Server-initiated keepalive (every 15s) --
     if(msg_type == "keepalive")
     {
        return;
     }

   // -- VERSION: Update notification --
   if(msg_type == "version")
   {
      string server_version = ExtractStringValue(json_message, "latest_version_mt5");
      if(server_version != "" && server_version != g_latestVersion)
      {
         g_latestVersion = server_version;
         g_updateNotes = ExtractStringValue(json_message, "update_notes_mt5");

         // Compare versions: server > local means update available
         if(CompareVersions(server_version, PT_VERSION) > 0)
         {
            if(!g_updateAvailable)
            {
               PrintFormat("[PineTunnel] UPDATE AVAILABLE (WS): v%s -> v%s", PT_VERSION, server_version);
               if(g_updateNotes != "")
                  PrintFormat("[PineTunnel] Release notes: %s", g_updateNotes);
               // Reset throttle so next OnTimer triggers immediate CheckForEAUpdate + download
               g_lastVersionCheck = 0;
            }
            g_updateAvailable = true;
         }
         else
         {
            g_updateAvailable = false;
         }
      }
      return;
   }

   // -- ACK: Server confirmed our ACK --
   if(msg_type == "ack")
   {
      // No action needed
      return;
   }

   // -- ERROR: Server rejected connection or sent error before closing --
   if(msg_type == "error")
   {
      int errorCode = (int)StringToInteger(ExtractStringValue(json_message, "code"));
      string errorReason = ExtractStringValue(json_message, "reason");
      PrintFormat("[PineTunnel] WebSocket error received: code=%d reason=%s", errorCode, errorReason);
      // Server will close the connection after this message.
      // EA will fall back to HTTP long-polling automatically in next OnTimer tick.
      // Common codes: 4001=invalid license, 4002=server shutdown, 4003=rate limited, 4004=idle timeout
      return;
   }

   // -- SHUTDOWN: Server is shutting down, switch to HTTP immediately --
   if(msg_type == "shutdown")
   {
      string reason = ExtractStringValue(json_message, "reason");
      PrintFormat("[PineTunnel] WebSocket server shutdown received: %s", reason);
      // Server will close the connection after this message
      // EA will fall back to HTTP long-polling automatically in next OnTimer tick
      return;
   }

   // -- REQUEST_STATS: Server requests account stats --
   if(msg_type == "request_stats")
   {
      if(g_wsClient != NULL && g_wsClient.IsConnected())
         g_wsClient.SendAccountStats();
      return;
   }

   // -- REQUEST_POSITIONS: Server requests open positions --
   if(msg_type == "request_positions")
   {
      if(g_wsClient != NULL && g_wsClient.IsConnected())
         g_wsClient.SendOpenPositions();
      return;
   }

   // -- REQUEST_HISTORY: Server requests trade history --
   if(msg_type == "request_history")
   {
      if(g_wsClient != NULL && g_wsClient.IsConnected())
      {
         int days_back = (int)StringToInteger(ExtractStringValue(json_message, "days_back"));
         if(days_back <= 0) days_back = 7;
         g_wsClient.SendTradeHistory(days_back);
      }
      return;
   }

   // -- SIGNAL: Trading signal(s) --
   if(msg_type == "signal")
   {
       ProcessWSSignalsArray(json_message);
      return;
   }

   // Unknown message type
   PrintFormat("[PineTunnel] WebSocket: unknown message type '%s'", msg_type);
}

//+------------------------------------------------------------------+
//| ProcessWSSignalsArray - parse signals from WS message              |
//| Uses the same signal format and batch ACK as PollForSignals        |
//+------------------------------------------------------------------+
void ProcessWSSignalsArray(string json_message)
{
   // Find signals array - same format as HTTP polling
   int signals_start = StringFind(json_message, "\"signals\":[");
   if(signals_start < 0)
      return;

   signals_start += 11; // Move past "signals":[
   int bracket_depth = 1;
   int signals_end = signals_start;
   bool arr_in_string = false;
   int json_len = StringLen(json_message);
   while(signals_end < json_len && bracket_depth > 0)
   {
      ushort ch = StringGetCharacter(json_message, signals_end);
      if(ch == '"' && (signals_end == 0 || StringGetCharacter(json_message, signals_end - 1) != '\\'))
         arr_in_string = !arr_in_string;
      if(!arr_in_string)
      {
         if(ch == '[' || ch == '{') bracket_depth++;
         else if(ch == ']' || ch == '}') bracket_depth--;
      }
      signals_end++;
   }
   if(bracket_depth != 0)
      return;

   string signals_json = StringSubstr(json_message, signals_start, signals_end - signals_start);
   StringTrimLeft(signals_json);
   StringTrimRight(signals_json);

   if(signals_json == "")
      return;

   // Batch ACK collection (same pattern as PollForSignals)
   string  batch_ack_ids[];
   bool    batch_ack_saved[];
   int     batch_ack_count = 0;
   ArrayResize(batch_ack_ids, BATCH_ACK_MAX);
   ArrayResize(batch_ack_saved, BATCH_ACK_MAX);

   int signal_count = 0;

   // Parse each signal object - reuse PollForSignals parsing logic
   // Limit signals processed per tick to prevent long blocking
    int pos = 0;
    int sig_len = StringLen(signals_json);
    while(pos < sig_len && signal_count < MAX_SIGNALS_PER_TICK)
   {
      // Find start of JSON object
      int obj_start = StringFind(signals_json, "{", pos);
      if(obj_start < 0) break;

      // Find matching closing brace
      int brace_count = 1;
      int obj_end = obj_start + 1;
       bool ws_in_string = false;
      while(obj_end < sig_len && brace_count > 0)
      {
         int ch = StringGetCharacter(signals_json, obj_end);
         if(ch == '"' && (obj_end == 0 || StringGetCharacter(signals_json, obj_end - 1) != '\\'))
            ws_in_string = !ws_in_string;
         if(!ws_in_string)
         {
            if(ch == '{') brace_count++;
            else if(ch == '}') brace_count--;
         }
         obj_end++;
      }

      if(brace_count != 0) break; // Incomplete JSON

      string signal_obj = StringSubstr(signals_json, obj_start, obj_end - obj_start);

      // Parse signal using existing ParseSignal()
      SignalCommand cmd = ParseSignal(signal_obj);

      if(cmd.type != COMMAND_NONE)
      {
         // DEDUP: Skip if signal was already executed (same as PollForSignals)
         if(IsSignalDuplicate(cmd.signal_id))
         {
            // Still ACK it so the server stops sending it
            if(cmd.signal_id != "" && batch_ack_count < BATCH_ACK_MAX)
            {
               batch_ack_ids[batch_ack_count] = cmd.signal_id;
               batch_ack_saved[batch_ack_count] = false;  // Don't save dedup record again
               batch_ack_count++;
            }
            PrintFormat("[PineTunnel] DUPLICATE SIGNAL (WS): %s on %s (ID: %s)",
                        CommandName(cmd.type), cmd.symbol, cmd.signal_id);
            pos = obj_end;
            continue;
         }

         signal_count++;
         PrintFormat("[PineTunnel] Signal #%d (WS): %s on %s (ID: %s)",
                     signal_count, CommandName(cmd.type), cmd.symbol, cmd.signal_id);

         bool queued = false;

         // PRE-CHECK: queue signal if market is closed (same as PollForSignals)
         if(InpEnableSignalQueue && g_signalQueue != NULL && IsQueueableCommandType(cmd.type))
         {
            if(!IsMarketOpenForSymbol(cmd.symbol))
            {
               queued = true;
               g_signalQueue.Push(cmd, false);
               PrintFormat("[Queue] Queued (market closed, WS): %s on %s | Queue size: %d",
                           CommandName(cmd.type), cmd.symbol, g_signalQueue.Size());
            }
         }

         if(!queued)
         {
            bool exec_ok = ExecuteCommand(cmd);

            // POST-CHECK: queue if execution failed and market closed/error 10018
            if(!exec_ok && InpEnableSignalQueue && g_signalQueue != NULL && IsQueueableCommandType(cmd.type))
            {
               if(!IsMarketOpenForSymbol(cmd.symbol) || g_lastExecError == 10018)
               {
                  queued = true;
                  g_signalQueue.Push(cmd, true);
                  string reason = (g_lastExecError == 10018 && IsMarketOpenForSymbol(cmd.symbol))
                                  ? "open-transition (10018)" : "market closed";
                  PrintFormat("[Queue] Queued (%s, WS): %s on %s | Queue size: %d",
                              reason, CommandName(cmd.type), cmd.symbol, g_signalQueue.Size());
               }
            }
         }

         // Collect signal ID for batch ACK
         if(cmd.signal_id != "" && batch_ack_count < BATCH_ACK_MAX)
         {
            batch_ack_ids[batch_ack_count] = cmd.signal_id;
            batch_ack_saved[batch_ack_count] = !queued;
            batch_ack_count++;
         }
      }

      pos = obj_end;
   }

   // -- Batch ACK via WebSocket (with HTTP fallback) --
   if(batch_ack_count > 0)
   {
      // Try WebSocket batch ACK first
      if(g_wsClient != NULL && g_wsClient.IsConnected() && g_useWebSocket)
      {
         // Build ACK JSON: {"type":"ack","signal_ids":["id1","id2",...]}
         string ack_json = "{\"type\":\"ack\",\"signal_ids\":[";
         for(int i = 0; i < batch_ack_count; i++)
         {
            if(i > 0) ack_json += ",";
            ack_json += "\"" + batch_ack_ids[i] + "\"";
         }
         ack_json += "]}";

         if(g_wsClient.SendString(ack_json))
         {
            // WS ACK sent - save dedup records for successfully executed signals
            for(int i = 0; i < batch_ack_count; i++)
            {
               if(batch_ack_saved[i])
                  SaveExecutedSignal(batch_ack_ids[i]);
            }
            PrintFormat("[PineTunnel] WS batch ACK sent: %d signals", batch_ack_count);
         }
         else
         {
            // WS ACK failed - fall back to HTTP batch ACK
            bool ack_results[];
            AcknowledgeSignalsBatch(batch_ack_ids, batch_ack_count, ack_results);
            for(int i = 0; i < batch_ack_count; i++)
            {
               if(batch_ack_saved[i] && ack_results[i])
                  SaveExecutedSignal(batch_ack_ids[i]);
            }
         }
      }
      else
      {
         // No WS - use HTTP batch ACK
         bool ack_results[];
         AcknowledgeSignalsBatch(batch_ack_ids, batch_ack_count, ack_results);
         for(int i = 0; i < batch_ack_count; i++)
         {
            if(batch_ack_saved[i] && ack_results[i])
               SaveExecutedSignal(batch_ack_ids[i]);
         }
      }
   }

    if(signal_count >= MAX_SIGNALS_PER_TICK && pos < StringLen(signals_json))
      PrintFormat("[PineTunnel] WebSocket: %d signal(s) deferred to next tick (limit %d)", signal_count, MAX_SIGNALS_PER_TICK);
}

//--- lifecycle -----------------------------------------------------
void OnTimer()
{
   datetime now = TimeCurrent();
   // wsNow uses TimeLocal() (wall clock) for WebSocket heartbeat / periodic-task
   // timing. TimeCurrent() returns the last server quote time and FREEZES when
   // there are no ticks (weekends, thin markets, inter-symbol gaps), which
   // would stall the WS heartbeat and trigger the server's 10-min idle timeout.
   datetime wsNow = TimeLocal();

   // Signal queue: attempt to execute any signals that were queued while market was closed
   if(InpEnableSignalQueue && g_signalQueue != NULL && !g_signalQueue.IsEmpty())
      DrainSignalQueue();

   // Phase 4: Connection and memory monitoring (throttled to every 5s)
   static datetime lastHealthCheck = 0;
   if((wsNow - lastHealthCheck) >= 5)
   {
      lastHealthCheck = wsNow;
      if(g_connectionManager != NULL)
         g_connectionManager.CheckConnection();
      if(g_memoryMonitor != NULL)
         g_memoryMonitor.CheckMemory();
   }

   // AGGRESSIVE WATCHDOG: Progressive connection recovery system
   // Level 0: Normal operation
   // Level 1: 2 errors + 20 sec -> Soft recovery
   // Level 2: 3 errors + 45 sec -> Medium recovery
   // Level 3: 4 errors + 90 sec -> Hard recovery
   if(g_consecutiveErrors > 0)
   {
      bool shouldTriggerWatchdog = false;
      string recoveryReason = "";
      // Determine if watchdog should trigger and at what level
      if(g_consecutiveErrors >= 2 && (now - g_lastSuccessfulRequest) > 20 && g_watchdogLevel < 1)
      {
         shouldTriggerWatchdog = true;
         g_watchdogLevel = 1;
         recoveryReason = "Early detection (2 errors + 20s)";
      }
      else if(g_consecutiveErrors >= 3 && (now - g_lastSuccessfulRequest) > 45 && g_watchdogLevel < 2)
      {
         shouldTriggerWatchdog = true;
         g_watchdogLevel = 2;
         recoveryReason = "Medium detection (3 errors + 45s)";
      }
      else if(g_consecutiveErrors >= 4 && (now - g_lastSuccessfulRequest) > 90 && g_watchdogLevel < 3)
      {
         shouldTriggerWatchdog = true;
         g_watchdogLevel = 3;
         recoveryReason = "Critical detection (4 errors + 90s)";
      }
      // Anti-spam: Don't trigger too frequently
      if(shouldTriggerWatchdog && (now - g_lastWatchdogAction) < WATCHDOG_COOLDOWN_SEC)
      {
         // Too soon after last action - skip
         shouldTriggerWatchdog = false;
         PrintFormat("[PineTunnel] Watchdog cooldown active (%d sec remaining)", WATCHDOG_COOLDOWN_SEC - (int)(now - g_lastWatchdogAction));
      }
      if(shouldTriggerWatchdog)
      {
         g_lastWatchdogAction = now;
         PrintFormat("[PineTunnel] WATCHDOG LEVEL %d ACTIVATED!", g_watchdogLevel);
         PrintFormat("[PineTunnel] Reason: %s", recoveryReason);
         PrintFormat("[PineTunnel] Errors: %d | Time since success: %d sec",
                    g_consecutiveErrors, (int)(now - g_lastSuccessfulRequest));
         // Recovery strategy based on level
         switch(g_watchdogLevel)
         {
            case 1:  // Soft recovery
               PrintFormat("[PineTunnel] SOFT RECOVERY: Forcing poll in 5s...");
               g_lastPoll = now - 5;
               break;
            case 2:  // Medium recovery
               PrintFormat("[PineTunnel] MEDIUM RECOVERY: Forcing immediate poll...");
               g_lastPoll = now - 10;
               g_consecutiveErrors = MathMax(g_consecutiveErrors - 1, 0);
               break;
            case 3:  // Hard recovery
               PrintFormat("[PineTunnel] HARD RECOVERY: Forcing aggressive polling...");
               g_lastPoll = now - 15;
               g_consecutiveErrors = 0;
               g_watchdogLevel = 0;
               break;
         }
         PrintFormat("[PineTunnel] Recovery complete - monitoring connection health");
      }
      else if(g_consecutiveErrors == 0 && g_watchdogLevel > 0)
      {
         // Success recovered - gradually reduce level
         g_watchdogLevel = MathMax(g_watchdogLevel - 1, 0);
         if(g_watchdogLevel == 0)
         {
            PrintFormat("[PineTunnel] Connection stable - Watchdog returning to normal operation");
         }
      }
   }

   // Check for daily reset (midnight)
   static datetime lastDailyCheck = 0;
   if((now - lastDailyCheck) >= 60)
   {
      lastDailyCheck = now;
      CheckDailyReset();
   }

   // Process hidden targets if enabled (high priority - check every 500ms)
   if(InpHiddenSLTP == HIDDEN_ON)
   {
      static ulong lastHiddenTick = 0;
      if(g_hiddenCount > 0 && (GetTickCount64() - lastHiddenTick) >= 500)
      {
         lastHiddenTick = GetTickCount64();
         ProcessHiddenSLTP();
      }
   }

   // === TRANSPORT SELECTION: WebSocket primary, long-poll fallback ===
   if(g_wsClient != NULL && g_wsClient.IsConnected() && g_useWebSocket)
   {
      // -- PRIMARY PATH: WebSocket connected --
      // Poll the DLL for incoming frames (signals, pongs, version info)
      // Poll() also handles heartbeat ping internally when interval elapses
      g_wsClient.Poll();

      // -- Dead connection detection --
      // If the server or Cloudflare silently drops the connection, WinHTTP
      // may not detect it. Force reconnect if no data received recently.
      // Pongs come every 30s (heartbeat interval), so 90s = 3 missed pongs.
      // Uses wsNow (wall clock) since GetLastReceived() is in TimeLocal() domain.
      if(g_wsClient.GetLastReceived() > 0 &&
         (wsNow - g_wsClient.GetLastReceived()) >= WS_DEAD_CONNECTION_TIMEOUT)
      {
         PrintFormat("[PineTunnel] WebSocket DEAD CONNECTION - no data for %ds, forcing reconnect",
                     (int)(wsNow - g_wsClient.GetLastReceived()));
         g_wsClient.Disconnect();
         g_useWebSocket = false;
         // Fall through to RECONNECTING path on next tick
      }
      else
      {

      // Process any received WebSocket messages (limit per tick to prevent long blocking)
      string wsMessage;
      int ws_signals_drained = 0;
      while(g_wsClient.ReceiveSignals(wsMessage) && ws_signals_drained < MAX_SIGNALS_PER_TICK)
      {
         ProcessWebSocketSignals(wsMessage);
         ws_signals_drained++;
      }
      if(ws_signals_drained >= MAX_SIGNALS_PER_TICK)
         PrintFormat("[PineTunnel] WS drain: %d messages this tick (limit %d) - more in DLL queue", ws_signals_drained, MAX_SIGNALS_PER_TICK);

      // -- Send initial state on first WS connection --
      if(!g_wsInitialStateSent)
      {
         g_wsClient.SendAccountStats();
         g_wsClient.SendOpenPositions();
         g_wsClient.SendTradeHistory(7);
         g_wsClient.SendHealthTelemetry();
         g_wsInitialStateSent = true;
         g_wsLastStatsSent = wsNow;
         g_wsLastHealthSent = wsNow;
         g_wsLastPositionCount = PositionsTotal();
      }

      // -- Periodic stats reporting (uses wsNow - wall clock) --
      if(wsNow - g_wsLastStatsSent >= PTWS_STATS_INTERVAL_SEC)
      {
         g_wsClient.SendAccountStats();
         g_wsLastStatsSent = wsNow;
      }

      // -- Periodic health telemetry (uses wsNow - wall clock) --
      if(wsNow - g_wsLastHealthSent >= PTWS_HEALTH_INTERVAL_SEC)
      {
         g_wsClient.SendHealthTelemetry();
         g_wsLastHealthSent = wsNow;
      }

      // -- Detect position changes and send updated state --
      int currentPositions = PositionsTotal();
      if(g_wsLastPositionCount >= 0 && currentPositions != g_wsLastPositionCount)
      {
         g_wsClient.SendOpenPositions();
         g_wsClient.SendAccountStats();  // Account stats change when positions change
         g_wsLastPositionCount = currentPositions;
      }
      else if(g_wsLastPositionCount < 0)
      {
         g_wsLastPositionCount = currentPositions;
      }

      // WebSocket delivers signals in real-time - no need to call PollForSignals()
      } // end else (connection alive)
   }
   else if(g_wsClient != NULL && !g_wsClient.IsConnected() && g_wsClient.GetStatus() >= PTWS_CONNECTING && g_useWebSocket)
   {
      // -- INITIALIZING PATH: WebSocket connected/handshaking but m_connected not yet set --
      // The DLL reports CONNECTING (handshake in progress), CONNECTED, SENDING, or POLLING
      // but IsConnected() is still false because Poll() hasn't run yet to set m_connected.
      // Call Poll() to drive the handshake forward and set m_connected=true.
      g_wsClient.Poll();
      g_wsConnectingTicks++;

      // Timeout guard: if handshake hasn't completed in WS_MAX_CONNECTING_TICKS,
      // force disconnect and enter RECONNECTING path with proper backoff
      if(g_wsConnectingTicks >= WS_MAX_CONNECTING_TICKS)
      {
         PrintFormat("[PineTunnel] WebSocket INITIALIZING timed out after %d ticks - forcing disconnect (status=%d, handle=%d, lastError=%d)",
                     g_wsConnectingTicks, g_wsClient.GetStatus(), g_wsClient.GetHandle(), g_wsClient.GetLastError());
         g_wsClient.Disconnect();
         g_useWebSocket = false;
         // Fall through to RECONNECTING path on next tick
      }
      else if(g_wsConnectingTicks == 1 || g_wsConnectingTicks % 10 == 0)
         PrintFormat("[PineTunnel] WebSocket INITIALIZING (status=%d, handle=%d, tick=%d, lastCallback=%d, lastError=%d)",
                     g_wsClient.GetStatus(), g_wsClient.GetHandle(), g_wsConnectingTicks,
                     g_wsClient.GetLastCallback(), g_wsClient.GetLastError());
   }
   else if(g_wsClient != NULL && !g_wsClient.IsConnected() && InpConnectionMode != CONNECTION_MODE_LONGPOLL)
   {
      // -- RECONNECTING PATH: WS disconnected, attempt reconnect with exponential backoff --
      // Log DLL state for diagnostics
      if(g_wsReconnectAttempts == 0)
         PrintFormat("[PineTunnel] WebSocket DISCONNECTED - status=%d, handle=%d, lastCallback=%d, lastError=%d",
                     g_wsClient.GetStatus(), g_wsClient.GetHandle(),
                     g_wsClient.GetLastCallback(), g_wsClient.GetLastError());
      // In HYBRID mode: fall back to HTTP if reconnect limit reached
      // In WEBSOCKET mode: keep retrying forever (no HTTP fallback)
      bool shouldFallback = g_wsClient.ShouldFallbackToHTTP();
      if(shouldFallback && InpConnectionMode == CONNECTION_MODE_HYBRID)
      {
         PrintFormat("[PineTunnel] WSS reconnect limit (%d) reached - switching to HTTPS polling",
                     InpWSMaxReconnectAttempts);
         g_useWebSocket = false;
         g_wsInitialStateSent = false;
         // Fall through to long-poll below

         // Use long-poll while WS is permanently failed
         // wsNow (TimeLocal/wall-clock) used instead of now (TimeCurrent/server-time)
         // because TimeCurrent() freezes when no ticks arrive, stalling the fallback
         if(g_useLongPoll && !g_longPollFailed)
         {
            if((wsNow - g_lastPoll) * 1000 >= 1000)
            {
               g_lastPoll = wsNow;
               PollForSignals();
            }
         }
         else if(g_longPollFailed && g_longPollRetryTime > 0 && wsNow >= g_longPollRetryTime)
         {
            g_lastPoll = wsNow;
            PollForSignals();
         }
         else if((wsNow - g_lastPoll) * 1000 >= InpPollInterval)
         {
            g_lastPoll = wsNow;
            PollForSignals();
         }
      }
      else
      {
         // -- Exponential backoff: wait before next reconnect attempt (wsNow - wall clock) --
         int delaySec = MathMin(g_wsReconnectDelaySec, PTWS_RECONNECT_MAX_DELAY_MS / 1000);

         if(wsNow - g_wsLastReconnectAttempt >= delaySec)
         {
            g_wsLastReconnectAttempt = wsNow;
            g_wsReconnectAttempts++;

            if(g_wsClient.Connect(InpServerURL, 443, true, InpLicenseID))
            {
               // Connection initiated async - do NOT reset backoff here.
               // Backoff resets only when IsConnected() becomes true (handshake completes).
               g_useWebSocket = true;
               g_wsConnectingTicks = 0;
               PrintFormat("[PineTunnel] WebSocket reconnect initiated (attempt %d, delay was %ds)",
                           g_wsReconnectAttempts, g_wsReconnectDelaySec);
               // Double backoff for next attempt if this one fails
               g_wsReconnectDelaySec = MathMin(g_wsReconnectDelaySec * 2, PTWS_RECONNECT_MAX_DELAY_MS / 1000);
            }
            else
            {
               // Connection initiation failed - double the backoff delay (capped at max)
               g_wsReconnectDelaySec = MathMin(g_wsReconnectDelaySec * 2, PTWS_RECONNECT_MAX_DELAY_MS / 1000);
               PrintFormat("[PineTunnel] WebSocket reconnect failed (attempt %d, next retry in %ds)",
                           g_wsReconnectAttempts, g_wsReconnectDelaySec);
            }
         }

         // Check if async WebSocket handshake has completed - THIS is where backoff resets
         if(g_wsClient.IsConnected())
         {
            int prevAttempts = g_wsReconnectAttempts;
            g_useWebSocket = true;
            g_wsReconnectAttempts = 0;
            g_wsConnectingTicks = 0;
            g_wsReconnectDelaySec = PTWS_RECONNECT_BASE_DELAY_MS / 1000;  // Reset backoff only on confirmed connect
            g_wsInitialStateSent = false;  // Will re-send initial state on next OnTimer
            PrintFormat("[PineTunnel] WebSocket connected after %d attempts", prevAttempts);
         }

         // Use HTTP long-poll as interim fallback while reconnecting (HYBRID mode only)
         // wsNow (TimeLocal/wall-clock) - TimeCurrent() freezes without ticks
         if(InpConnectionMode == CONNECTION_MODE_HYBRID)
         {
            if((wsNow - g_lastPoll) * 1000 >= 1000)
            {
               g_lastPoll = wsNow;
               PollForSignals();
            }
         }
      }
   }
   else
   {
      // -- FALLBACK PATH: Normal long-polling --
      // This runs when:
      // 1. Connection mode = LONGPOLL (user chose HTTP only)
      // 2. HYBRID mode: WS permanently failed (max reconnect exceeded)
      // 3. DLL not available
      // wsNow (TimeLocal/wall-clock) - TimeCurrent() freezes without ticks

      if(g_useLongPoll && !g_longPollFailed)
      {
         if((wsNow - g_lastPoll) * 1000 >= 1000)
         {
            g_lastPoll = wsNow;
            PollForSignals();
         }
      }
      else if(g_longPollFailed && g_longPollRetryTime > 0 && wsNow >= g_longPollRetryTime)
      {
         g_lastPoll = wsNow;
         PollForSignals();
      }
      else if((wsNow - g_lastPoll) * 1000 >= InpPollInterval)
      {
         g_lastPoll = wsNow;
         PollForSignals();
      }
   }

   // Account stats reporting (time-gated)
   static datetime s_lastStatsReport = 0;
   if(InpStatsInterval > 0 && (now - s_lastStatsReport) >= InpStatsInterval)
   {
      s_lastStatsReport = now;
      SendAccountStats();
   }

   // Auto-update check (time-gated by InpUpdateCheckInterval)
   if(InpAutoUpdate && (now - g_lastVersionCheck) >= InpUpdateCheckInterval)
      CheckForEAUpdate();

   // Retry auto-restart if update was downloaded but restart was blocked by cooldown
   if((g_updateDownloaded || g_dllUpdateDownloaded) && InpAutoRestart)
      TriggerAutoRestart();

   // Audit/telemetry reporting (time-gated by InpAuditInterval)
   if(InpAuditInterval > 0 && (now - g_lastAuditSent) >= InpAuditInterval)
      SendAuditData();

   // Refresh dashboard every 2 seconds to reduce CPU overhead
   if(InpShowDashboard && (now - g_lastDashboardUpdate) >= 2)
   {
      g_lastDashboardUpdate = now;
      DrawDashboard();
   }
}

//+------------------------------------------------------------------+
//| ProcessPendingWSSignals - Drain WS receive queue                   |
//| Called from OnChartEvent (PostMessage wake-up) and OnTimer.       |
//| Returns count of signals processed.                                |
//+------------------------------------------------------------------+
int ProcessPendingWSSignals()
{
   if(!g_wsClient || !g_wsClient.IsConnected())
      return 0;

   g_wsClient.Poll();
   string wsMessage;
   int processed = 0;
   while(g_wsClient.ReceiveSignals(wsMessage) && processed < MAX_SIGNALS_PER_TICK)
   {
      ProcessWebSocketSignals(wsMessage);
      processed++;
   }
   return processed;
}

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   // Redraw dashboard if chart changes
   if(id == CHARTEVENT_CHART_CHANGE && InpShowDashboard)
   {
      DrawDashboard();
      return;
   }

   // PostMessage notification from DLL - frame arrived, process immediately
   // DLL posts WM_USER+1 (0x0401) via PostMessageW when a frame is queued.
   // MQL5 may or may not forward WM_USER messages to OnChartEvent depending on build.
   // OnTick + OnTimer (100ms) provide the primary signal processing loop.
   if(id == PTWS_NOTIFY_MSG_BASE + 1 && g_useWebSocket && g_wsClient != NULL)
   {
      if(g_wsClient.IsConnected())
      {
         int processed = ProcessPendingWSSignals();
         if(processed > 0)
            PrintFormat("[PineTunnel] OnChartEvent: processed %d WS signal(s) via PostMessage wake-up", processed);
      }
      return;
   }
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
      return;

   ulong deal_ticket = trans.deal;
   HistorySelect(TimeCurrent() - 300, TimeCurrent());
   if(!HistoryDealSelect(deal_ticket))
      return;

   long deal_magic = HistoryDealGetInteger(deal_ticket, DEAL_MAGIC);
   if(deal_magic != g_magicNumber)
      return;

   long deal_type = HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
   if(deal_type != DEAL_TYPE_BUY && deal_type != DEAL_TYPE_SELL)
      return;

   LogExecutionQuality(deal_ticket);
}

void LogExecutionQuality(ulong deal_ticket)
{
   long   deal_time_msc  = HistoryDealGetInteger(deal_ticket, DEAL_TIME_MSC);
   ulong  order_ticket   = (ulong)HistoryDealGetInteger(deal_ticket, DEAL_ORDER);
   ulong  position_id    = (ulong)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
   string symbol         = HistoryDealGetString(deal_ticket, DEAL_SYMBOL);
   long   deal_type      = HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
   long   deal_entry     = HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);
   double fill_price     = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
   double filled_vol     = HistoryDealGetDouble(deal_ticket, DEAL_VOLUME);
   double commission     = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
   double fee            = HistoryDealGetDouble(deal_ticket, DEAL_FEE);
   double swap           = HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
   double deal_profit    = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
   string deal_comment   = HistoryDealGetString(deal_ticket, DEAL_COMMENT);

   double requested_price = fill_price;
   long   order_time_msc  = deal_time_msc;
   double requested_vol   = filled_vol;

   if(HistoryOrderSelect(order_ticket))
   {
      double op = HistoryOrderGetDouble(order_ticket, ORDER_PRICE_OPEN);
      if(op > 0) requested_price = op;
      requested_vol = HistoryOrderGetDouble(order_ticket, ORDER_VOLUME_INITIAL);
      order_time_msc = HistoryOrderGetInteger(order_ticket, ORDER_TIME_SETUP_MSC);
   }

   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double slippage_price = 0;
   if(point > 0)
      slippage_price = fill_price - requested_price;

   double slippage_points = 0;
   if(point > 0)
      slippage_points = NormalizeDouble(slippage_price / point, 1);

   MqlTick tick;
   double spread = 0;
   int    spread_points = 0;
   if(SymbolInfoTick(symbol, tick))
   {
      spread = tick.ask - tick.bid;
      double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
      if(tick_size > 0)
         spread_points = (int)MathRound(spread / tick_size);
   }

   double total_cost = MathAbs(commission) + MathAbs(fee) + MathAbs(swap);
   double net_pnl    = deal_profit - total_cost;
   long   latency_ms = deal_time_msc - order_time_msc;
   double balance    = AccountInfoDouble(ACCOUNT_BALANCE);

   string dir_str   = (deal_type == DEAL_TYPE_BUY) ? "BUY" : "SELL";
   string entry_str = "IN";
   if(deal_entry == DEAL_ENTRY_OUT)       entry_str = "OUT";
   else if(deal_entry == DEAL_ENTRY_INOUT) entry_str = "INOUT";
   else if(deal_entry == DEAL_ENTRY_OUT_BY) entry_str = "OUT_BY";

   string timestamp = TimeToString((datetime)(deal_time_msc / 1000), TIME_DATE | TIME_SECONDS);

   string filename = EXEC_LOG_FILE_PREFIX + InpLicenseID + "_" +
                     TimeToString((datetime)(deal_time_msc / 1000), TIME_DATE) + ".csv";

   bool file_exists = FileIsExist(filename);

   int handle = FileOpen(filename, FILE_WRITE | FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("[PineTunnel] [EXEC] Failed to open exec log: %s (err=%d)", filename, GetLastError());
      return;
   }

   if(!file_exists)
   {
      string header = "timestamp,deal_ticket,order_ticket,position_id,symbol,direction,entry_type,"
                      "requested_price,fill_price,slippage_price,slippage_points,"
                      "requested_vol,filled_vol,"
                      "spread,spread_points,"
                      "commission,fee,swap,total_cost,"
                      "deal_profit,net_pnl,"
                      "latency_ms,"
                      "balance,magic,comment\n";
      FileWriteString(handle, header);
   }
   else
   {
      FileSeek(handle, 0, SEEK_END);
   }

   string row = StringFormat("%s,%I64u,%I64u,%I64u,%s,%s,%s,"
                             "%.5f,%.5f,%.5f,%.1f,"
                             "%.2f,%.2f,"
                             "%.5f,%d,"
                             "%.4f,%.4f,%.4f,%.4f,"
                             "%.2f,%.2f,"
                             "%I64d,"
                             "%.2f,%d,%s\n",
                             timestamp, deal_ticket, order_ticket, position_id,
                             symbol, dir_str, entry_str,
                             requested_price, fill_price, slippage_price, slippage_points,
                             requested_vol, filled_vol,
                             spread, spread_points,
                             commission, fee, swap, total_cost,
                             deal_profit, net_pnl,
                             latency_ms,
                              balance, (int)HistoryDealGetInteger(deal_ticket, DEAL_MAGIC), deal_comment);

   FileWriteString(handle, row);
   FileClose(handle);
}

//+------------------------------------------------------------------+
//| Count Today's Executed Trades from History + Open Positions      |
//+------------------------------------------------------------------+
void CountTodayTrades()
{
   datetime today_start = StringToTime(TimeToString(TimeCurrent(), TIME_DATE));
   datetime now = TimeCurrent();

   int historical_count = 0;
   int open_count = 0;

   // 1. Count closed trades from history (deals that happened today)
   HistorySelect(today_start, now);
   int total_deals = HistoryDealsTotal();

   for(int i = 0; i < total_deals; i++)
   {
      ulong deal_ticket = HistoryDealGetTicket(i);
      if(deal_ticket == 0) continue;

      // Check if this deal was made by our EA (magic number match)
      long magic = HistoryDealGetInteger(deal_ticket, DEAL_MAGIC);
      ENUM_DEAL_ENTRY entry_type = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);

      if(magic == g_magicNumber && entry_type == DEAL_ENTRY_IN)
      {
         historical_count++;
      }
   }

   // 2. Count currently open positions
   CPositionInfo pos;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!pos.SelectByIndex(i)) continue;

      // Count ALL positions with matching magic (not just today's)
      if(pos.Magic() == g_magicNumber)
      {
         open_count++;
      }
   }

   // Update counters
   g_todaySignals = historical_count + open_count;
   g_todaySuccessful = historical_count + open_count;

   // Silently loaded today's trades
}
//+------------------------------------------------------------------+
//| Initialize Spread History from Past Bars (Using CopySpread)     |
//+------------------------------------------------------------------+
void InitializeSpreadHistory()
{
   int bars_to_analyze = HISTORICAL_SPREAD_BARS;  // Analyze last 20,000 bars for stable historical average
   int available_bars = Bars(_Symbol, PERIOD_CURRENT);

   if(available_bars < bars_to_analyze)
      bars_to_analyze = available_bars;

   if(bars_to_analyze < 10)
      return;  // Not enough data

   // Use MT5's CopySpread() to get actual historical spread data
   int spread_array[];
   ArraySetAsSeries(spread_array, true);

   int copied = CopySpread(_Symbol, PERIOD_CURRENT, 0, bars_to_analyze, spread_array);

   if(copied <= 0)
   {
      PrintFormat("[PineTunnel] Warning: Could not load historical spread data (error %d)", GetLastError());
      return;
   }

   // Reset counters
   g_totalSpread = 0;
   g_spreadSamples = 0;

   // Calculate average from actual historical spread values
   // Note: MT5 stores spread in integer points
   for(int i = 0; i < copied; i++)
   {
      double spread_points = (double)spread_array[i];

      // Sanity check: spreads typically between 0.1 and 200 points
      if(spread_points > 0.1 && spread_points < 200)
      {
         g_totalSpread += spread_points;
         g_spreadSamples++;
      }
   }

   if(g_spreadSamples > 0)
   {
      g_avgSpread = g_totalSpread / g_spreadSamples;
   }
   else
   {
      PrintFormat("[PineTunnel] Warning: No valid spread data found in %d bars", copied);
   }
}
//+------------------------------------------------------------------+
//| Hidden SL/TPs Management Functions                               |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Add hidden target for a position                                  |
//+------------------------------------------------------------------+
bool AddHiddenTarget(ulong ticket, string symbol, int position_type, double sl_price, double tp_price, double entry_price)
{
   if(g_hiddenCount >= MAX_HIDDEN_TARGETS)
   {
      Print("[PineTunnel] Hidden targets array full - cannot add more");
      return false;
   }

   // Check if already exists (avoid duplicates)
   for(int i = 0; i < g_hiddenCount; i++)
   {
      if(g_hiddenTargets[i].ticket == ticket)
      {
         // Update existing hidden target
         g_hiddenTargets[i].hidden_sl = sl_price;
         g_hiddenTargets[i].hidden_tp = tp_price;
         PrintFormat("[PineTunnel] Hidden target updated for ticket #%I64d", ticket);
         return true;
      }
   }

   // Add new hidden target
   g_hiddenTargets[g_hiddenCount].ticket = ticket;
   g_hiddenTargets[g_hiddenCount].symbol = symbol;
   g_hiddenTargets[g_hiddenCount].type = position_type;
   g_hiddenTargets[g_hiddenCount].hidden_sl = sl_price;
   g_hiddenTargets[g_hiddenCount].hidden_tp = tp_price;
   g_hiddenTargets[g_hiddenCount].entry_price = entry_price;
   g_hiddenCount++;

   return true;
}
//+------------------------------------------------------------------+
//| Remove hidden target after position closed                        |
//+------------------------------------------------------------------+
void RemoveHiddenTarget(ulong ticket)
{
   for(int i = 0; i < g_hiddenCount; i++)
   {
      if(g_hiddenTargets[i].ticket == ticket)
      {
         // Shift array to remove element
         for(int j = i; j < g_hiddenCount - 1; j++)
         {
            g_hiddenTargets[j] = g_hiddenTargets[j + 1];
         }
         g_hiddenCount--;
         break;
      }
   }
}
//+------------------------------------------------------------------+
//| Process all hidden targets - check if any should trigger          |
//+------------------------------------------------------------------+
void ProcessHiddenSLTP()
{
   if(g_hiddenCount == 0) return;

   CPositionInfo pos;
   CSymbolInfo sym;

   // Check each hidden target
   for(int i = g_hiddenCount - 1; i >= 0; i--)
   {
      ulong ticket = g_hiddenTargets[i].ticket;

      // Check if position still exists
      if(!pos.SelectByTicket(ticket))
      {
         // Position closed externally - remove hidden
         RemoveHiddenTarget(ticket);
         continue;
      }

      // Get current price with high precision
      if(!sym.Name(g_hiddenTargets[i].symbol))
         continue;

      if(!sym.RefreshRates())
         continue;

      double current_price = 0;
      bool is_buy = (g_hiddenTargets[i].type == POSITION_TYPE_BUY);

      // Use BID price for BUY positions, ASK price for SELL positions
      // (opposite of entry because we're checking exit prices)
      if(is_buy)
         current_price = sym.Bid();
      else
         current_price = sym.Ask();

      bool should_close = false;
      string reason = "";

      // High-precision comparison with 1 pip tolerance for slippage
      int digits = (int)sym.Digits();
      double tolerance = sym.Point(); // 1 pip tolerance

      // Check Hidden Stop Loss
      if(g_hiddenTargets[i].hidden_sl > 0)
      {
         if(is_buy)
         {
            // BUY position: close if current price <= hidden SL
            if(current_price <= g_hiddenTargets[i].hidden_sl + tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden SL hit: %." + IntegerToString(digits) + "f <= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_sl);
            }
         }
         else
         {
            // SELL position: close if current price >= hidden SL
            if(current_price >= g_hiddenTargets[i].hidden_sl - tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden SL hit: %." + IntegerToString(digits) + "f >= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_sl);
            }
         }
      }

      // Check Hidden Take Profit
      if(!should_close && g_hiddenTargets[i].hidden_tp > 0)
      {
         if(is_buy)
         {
            // BUY position: close if current price >= hidden TP
            if(current_price >= g_hiddenTargets[i].hidden_tp - tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden TP hit: %." + IntegerToString(digits) + "f >= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_tp);
            }
         }
         else
         {
            // SELL position: close if current price <= hidden TP
            if(current_price <= g_hiddenTargets[i].hidden_tp + tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden TP hit: %." + IntegerToString(digits) + "f <= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_tp);
            }
         }
      }

      // Close position if hidden target hit
      if(should_close)
      {
         PrintFormat("[PineTunnel] Hidden SL/TP Triggered!");
         PrintFormat("[PineTunnel] Ticket: #%I64d | %s | %s",
                    ticket, g_hiddenTargets[i].symbol, is_buy ? "BUY" : "SELL");
         PrintFormat("[PineTunnel] Reason: %s", reason);
         PrintFormat("[PineTunnel] Entry: %." + IntegerToString(digits) + "f | Current: %." + IntegerToString(digits) + "f",
                    g_hiddenTargets[i].entry_price, current_price);

         // Close the position
         double pnl = pos.Profit() + pos.Swap() + pos.Commission();
         if(g_trade.PositionClose(ticket))
         {
            PrintFormat("[PineTunnel] Position closed by Hidden SL/TP | P/L: $%.2f", pnl);
            RemoveHiddenTarget(ticket);
         }
         else
         {
            PrintFormat("[PineTunnel] Failed to close position #%I64d: %s",
                       ticket, g_trade.ResultRetcodeDescription());
         }
      }
   }
}

