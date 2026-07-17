//+------------------------------------------------------------------+
//|                                           PineTunnel_EA_MT4.mq4  |
//|                     PineTunnel EA - MT4 Version                  |
//|                     1:1 Logic Conversion from MT5                |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#define PT_VERSION "1.1.5"
#define PT_VERSION_DESC "PineTunnel v" + PT_VERSION + " - github.com/TheFractalyst/PineTunnel"

#property link      "github.com/TheFractalyst/PineTunnel"
#property version   PT_VERSION
#property strict
#property description PT_VERSION_DESC

// Include MT4 versions of helper classes
// Place these files in MQL4\Include\ folder
#include <PendingOrders_MT4.mqh>
#include <PartialClose_MT4.mqh>
#include <OrderModifications_MT4.mqh>
#include <CombinedActions_MT4.mqh>
#include <ProductionHardening.mqh>   // Production hardening utilities
#include <PTWebSocketClient_MT4.mqh> // Phase 2: WebSocket real-time delivery

//===================================================================
// CODE QUALITY CONSTANTS
//===================================================================
// Watchdog and recovery constants
#define WATCHDOG_COOLDOWN_SEC         30      // Cooldown period between watchdog actions

// Signal tracking constants
#define MAX_EXECUTED_SIGNALS          500     // Maximum number of executed signals to track

// Layer-4 duplicate prevention constants
#define SIGNAL_ID_PREFIX_LENGTH       8       // Number of hex chars for signal_id prefix
#define BROKER_COMMENT_MAX_LENGTH     31      // MT4 broker comment max length

// Cancel-confirmation constants
#define CANCEL_CONFIRM_TIMEOUT_MS     1500    // Timeout for order cancel confirmation (ms)
#define CANCEL_POLL_INTERVAL_MS       50      // Poll interval for cancel confirmation (ms)
#define NM_DELETE_CONFIRM_TIMEOUT_MS  500     // Timeout for NM order delete confirmation (ms)
#define NM_POLL_INTERVAL_MS           10      // Poll interval for NM fill detection (ms)
#define HISTORY_SEARCH_WINDOW_SEC     120     // History search window for fill detection (sec)

// Ticket-signal mapping constants
#define TICKET_SIGNAL_MAP_SIZE     256     // Max entries in ticket-signal map
#define LOT_ROUNDING_EPSILON       0.0000001  // Epsilon for floor-based lot rounding


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
   //+------------------------------------------------------------------+
   double CalculateVolume(
      ENUM_VOLUME_TYPE volume_type,
      string symbol,
      double risk_value,
      double entry_price,
      double stop_loss_price = 0,
      int order_type = OP_BUY
   )
   {
      switch(volume_type)
      {
         case VOLUME_LOTS:
            return CalculateDirectLots(symbol, risk_value);
            
         case VOLUME_DOLLAR_AMOUNT:
            return CalculateDollarAmount(symbol, risk_value, entry_price, stop_loss_price);
            
         case VOLUME_PERCENTAGE_BALANCE_LOTS:
            return CalculatePercentageBalanceLots(symbol, risk_value);
            
         case VOLUME_PERCENTAGE_BALANCE_MARGIN:
            return CalculatePercentageBalanceMargin(symbol, risk_value, order_type, entry_price);
            
         case VOLUME_PERCENTAGE_BALANCE_LOSS:
            return CalculatePercentageBalanceLoss(symbol, risk_value, entry_price, stop_loss_price);
            
         case VOLUME_PERCENTAGE_EQUITY_LOSS:
            return CalculatePercentageEquityLoss(symbol, risk_value, entry_price, stop_loss_price);
            
         case VOLUME_PERCENTAGE_EQUITY_MARGIN:
            return CalculatePercentageEquityMargin(symbol, risk_value, order_type, entry_price);
            
         default:
            Print("[PositionSizer] ERROR: Unknown volume type: ", volume_type);
            return MarketInfo(symbol, MODE_MINLOT);
      }
   }
   
   //+------------------------------------------------------------------+
   //| Mode 1: LOTS - Direct lot size specification                    |
   //+------------------------------------------------------------------+
   double CalculateDirectLots(string symbol, double lots)
   {
      return NormalizeLotSize(symbol, lots);
   }
   
   //+------------------------------------------------------------------+
   //| Mode 2: DOLLAR_AMOUNT - Fixed dollar amount to risk             |
   //+------------------------------------------------------------------+
   double CalculateDollarAmount(string symbol, double dollar_amount, double entry_price, double stop_loss_price)
   {
      if(stop_loss_price <= 0)
      {
         Print("[PositionSizer] ERROR: DOLLAR_AMOUNT mode requires stop loss price");
         return 0.0;
      }
      
      double sl_distance = MathAbs(entry_price - stop_loss_price);
      double tick_size = MarketInfo(symbol, MODE_TICKSIZE);
      double tick_value = MarketInfo(symbol, MODE_TICKVALUE);
      
      if(tick_size <= 0 || tick_value <= 0 || sl_distance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid parameters for calculation");
         return 0.0;
      }
      
      double unit_cost = tick_value;
      double lots = dollar_amount / (sl_distance * unit_cost / tick_size + 2 * m_commission);
      
      lots = NormalizeLotSize(symbol, lots);
      
      return lots;
   }
   
   //+------------------------------------------------------------------+
   //| Mode 3: PERCENTAGE_BALANCE_LOTS - % of balance as lots          |
   //+------------------------------------------------------------------+
   double CalculatePercentageBalanceLots(string symbol, double percent)
   {
      double balance = AccountBalance();
      if(balance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid balance for PCT_BAL_LOTS");
         return 0.0;
      }
      double lots = (balance * percent / 100.0) / 10000.0;
      
      lots = NormalizeLotSize(symbol, lots);
      
      return lots;
   }
   
   //+------------------------------------------------------------------+
   //| Mode 4: PERCENTAGE_BALANCE_MARGIN - % of balance as margin      |
   //+------------------------------------------------------------------+
   double CalculatePercentageBalanceMargin(string symbol, double percent, int order_type, double entry_price = 0)
   {
      double balance = AccountBalance();
      double margin_to_use = balance * (percent / 100.0);

      // Use entry_price for pending orders, fall back to current market price for market orders
      double price;
      if(entry_price > 0)
         price = entry_price;
      else
         price = (order_type == OP_BUY || order_type == OP_BUYLIMIT || order_type == OP_BUYSTOP) ?
                 MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);

      // NOTE: MT4 MODE_MARGINREQUIRED always calculates at current market price regardless of the price variable.
      // For pending orders, the margin may differ from what's calculated here.
      // This is an MT4 platform limitation -- MT5's OrderCalcMargin() correctly uses the entry_price.
      
      // Get margin required for 1 lot using MT4 function
      double margin_required = MarketInfo(symbol, MODE_MARGINREQUIRED);
      
      if(margin_required <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid margin requirement");
         return MarketInfo(symbol, MODE_MINLOT);
      }
      
      double lots = margin_to_use / margin_required;
      
      lots = NormalizeLotSize(symbol, lots);
      
      return lots;
   }
   
   //+------------------------------------------------------------------+
   //| Mode 5: PERCENTAGE_BALANCE_LOSS - % of balance to risk         |
   //+------------------------------------------------------------------+
   double CalculatePercentageBalanceLoss(string symbol, double percent, double entry_price, double stop_loss_price)
   {
      if(stop_loss_price <= 0)
      {
         Print("[PositionSizer] ERROR: PERCENTAGE_BALANCE_LOSS mode requires stop loss price");
         return MarketInfo(symbol, MODE_MINLOT);
      }
      
      double balance = AccountBalance();
      return CalculatePositionSize(symbol, entry_price, stop_loss_price, percent, balance);
   }
   
   //+------------------------------------------------------------------+
   //| Mode 6: PERCENTAGE_EQUITY_LOSS - % of equity to risk           |
   //+------------------------------------------------------------------+
   double CalculatePercentageEquityLoss(string symbol, double percent, double entry_price, double stop_loss_price)
   {
      if(stop_loss_price <= 0)
      {
         Print("[PositionSizer] ERROR: PERCENTAGE_EQUITY_LOSS mode requires stop loss price");
         return MarketInfo(symbol, MODE_MINLOT);
      }
      
       double equity = AccountEquity();
       return CalculatePositionSize(symbol, entry_price, stop_loss_price, percent, equity);
    }
    
    //+------------------------------------------------------------------+
    //| Mode 7: PERCENTAGE_EQUITY_MARGIN - % of equity as notional      |
    //| Formula: lots = (equity * pct/100) / (price * contractSize)     |
    //+------------------------------------------------------------------+
    double CalculatePercentageEquityMargin(string symbol, double percent, int order_type, double entry_price = 0)
    {
       double equity = AccountEquity();
       if(equity <= 0)
       {
          Print("[PositionSizer] ERROR: PERCENTAGE_EQUITY_MARGIN - invalid equity");
          return MarketInfo(symbol, MODE_MINLOT);
       }
       
       double pct = percent / 100.0;
       
       double price;
       if(entry_price > 0)
          price = entry_price;
       else
          price = (order_type == OP_BUY || order_type == OP_BUYLIMIT || order_type == OP_BUYSTOP) ?
                  MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);
       
       double contract_size = MarketInfo(symbol, MODE_LOTSIZE);
       
       if(price <= 0 || contract_size <= 0)
       {
          PrintFormat("[PositionSizer] ERROR: pct_eq_margin - invalid price (%.5f) or contract size (%.2f)", price, contract_size);
          return MarketInfo(symbol, MODE_MINLOT);
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
      double min_lot = MarketInfo(symbol, MODE_MINLOT);
      double max_lot = MarketInfo(symbol, MODE_MAXLOT);
      double lot_step = MarketInfo(symbol, MODE_LOTSTEP);
      
      if(lots <= 0)
      {
         PrintFormat("[PositionSizer] REJECT: Invalid lot size %.4f - aborting trade", lots);
         return 0.0;
      }
      
      // Apply max constraint (min checked after rounding below)
      // Only clamp if broker reports a valid max; if 0, skip the cap
      if(max_lot > 0 && lots > max_lot) lots = max_lot;
      
      if(lot_step > 0)
      {
         if(m_round_down)
            lots = MathFloor(lots / lot_step + LOT_ROUNDING_EPSILON) * lot_step;
         else
            lots = MathRound(lots / lot_step) * lot_step;
      }
      
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
   double CalculatePositionSize(
      string symbol,
      double entry_price,
      double stop_loss_price,
      double risk_percent,
      double account_balance = 0
   )
   {
      if(account_balance <= 0)
         account_balance = AccountBalance();
      
      if(account_balance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid account balance");
         return 0.01;
      }
      
      double risk_money = account_balance * (risk_percent / 100.0);
      double stop_loss_distance = MathAbs(entry_price - stop_loss_price);
      
      if(stop_loss_distance <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid stop loss distance");
         return 0.01;
      }
      
      double tick_size = MarketInfo(symbol, MODE_TICKSIZE);
      double tick_value = MarketInfo(symbol, MODE_TICKVALUE);
      double volume_min = MarketInfo(symbol, MODE_MINLOT);
      double volume_max = MarketInfo(symbol, MODE_MAXLOT);
      double volume_step = MarketInfo(symbol, MODE_LOTSTEP);
      
      if(tick_size <= 0 || tick_value <= 0)
      {
         Print("[PositionSizer] ERROR: Invalid tick size or tick value");
         return 0.0;
      }
      
      double unit_cost = tick_value;
      double position_size = risk_money / (stop_loss_distance * unit_cost / tick_size + 2 * m_commission);
      
      if(volume_step > 0)
      {
         if(m_round_down)
            position_size = MathFloor(position_size / volume_step + LOT_ROUNDING_EPSILON) * volume_step;
         else
            position_size = MathRound(position_size / volume_step) * volume_step;
      }
      
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
input string   InpSyntaxGroup      = "========== Syntax =========="; // ---
input ENUM_TARGET_TYPE InpTargetType = TARGET_TYPE_PIPS;       // Target Type for SL/TP
input ENUM_VOLUME_TYPE InpVolumeType = VOLUME_LOTS;               // Volume Type

enum ENUM_PENDING_ENTRY
{
   PENDING_PIPS_FROM_MARKET = 0,      // Pips from Current Market Price
   PENDING_PRICE_FROM_SIGNAL = 1,     // Specified Price from TradingView Alert
   PENDING_PERCENT_FROM_MARKET = 2    // Percentage from Current Market Price
};
input ENUM_PENDING_ENTRY InpPendingOrderEntry = PENDING_PIPS_FROM_MARKET; // Pending Order Entry Type

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
input string   InpInputGroup       = "========== Input =========="; // ---
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
input string   InpGeneralGroup     = "========== General =========="; // ---
enum ENUM_PYRAMIDING
{
   PYRAMIDING_ON = 0,                 // On
   PYRAMIDING_ON_IF_PROFIT = 1,       // On - Only If In Profit
   PYRAMIDING_OFF_EITHER_OR = 2,      // Off - One Position Per Symbol
   PYRAMIDING_OFF_BOTH = 3            // Off - One Buy and One Sell (Hedge)
};
input ENUM_PYRAMIDING InpPyramiding = PYRAMIDING_ON;              // Pyramiding

enum ENUM_CLOSE_ON_REVERSE
{
   CLOSE_REVERSE_ON_HEDGING = 0,      // On - Close and Reverse
   CLOSE_REVERSE_ON_NETTING = 1,      // On - Close Only
   CLOSE_REVERSE_OFF = 2              // Off
};
input ENUM_CLOSE_ON_REVERSE InpCloseOnReverse = CLOSE_REVERSE_OFF; // Close on Reverse

enum ENUM_HIDDEN_SLTP
{
   HIDDEN_OFF = 0,                    // Off
   HIDDEN_ON = 1                      // On
};
input ENUM_HIDDEN_SLTP InpHiddenSLTP = HIDDEN_OFF;          // Hidden SL/TP
input double   InpHiddenOffset = 100.0;                          // Hidden Offset (pips)

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

input bool     InpEnableSmartMarket = true;                       // Enable Auto Orders
input int      InpNearMarketPoints = 10;                          // Auto Order Offset (points)
input int      InpEntryLimitTimeoutMs = 300;                      // Entry Limit Timeout (ms)
input bool     InpEnableExitLimit = false;                       // Exit Limit Orders (maker rebate)
input int      InpExitLimitTimeoutMs = 500;                        // Exit Limit Timeout (ms)

//===================================================================
// DASHBOARD
//===================================================================
input string   InpDashGroup        = "========== Dashboard =========="; // ---
input int      InpFontSize         = 10;                          // Font Size
input bool     InpShowDashboard    = true;                        // Show Dashboard

//===================================================================
// ACCOUNT PROTECTION
//===================================================================
input string   InpAccountGroup     = "========== Account =========="; // ---
input int      InpDailyTimezoneGMT = 0;                          // Daily Profit/Loss Timezone
input double   InpDailyProfit      = 0.0;                        // Daily Profit
input double   InpDailyLoss        = 0.0;                        // Daily Loss

enum ENUM_ACTION_DAILY
{
   ACTION_DAILY_HALT = 0,             // Halt EA
   ACTION_DAILY_CLOSE = 1,            // Close All Positions
   ACTION_DAILY_CLOSE_HALT = 2        // Close All Positions and Halt EA
};
input ENUM_ACTION_DAILY InpAction1 = ACTION_DAILY_HALT;           // Action (Daily)
input double   InpCumulativeProfit = 0.0;                        // Cumulative Profit
input double   InpCumulativeLoss  = 0.0;                        // Cumulative Loss

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
input string   InpReliabilityGroup = "========== V7.00 Reliability =========="; // ---
input bool     InpEnableSLTPVerify = false;                       // Verify SL/TP After Order
input int      InpSLTPVerifyRetries = 3;                          // SL/TP Verification Retries

//===================================================================
// MISCELLANEOUS
//===================================================================
input string   InpMiscGroup        = "========== Miscellaneous =========="; // ---
enum ENUM_MAGIC_NUMBER
{
   MAGIC_1001 = 1001,                     // 1001
   MAGIC_1002 = 1002,                     // 1002
   MAGIC_1003 = 1003,                     // 1003
   MAGIC_1004 = 1004,                     // 1004
   MAGIC_1005 = 1005                      // 1005
};
input ENUM_MAGIC_NUMBER InpMagicNumber = MAGIC_1001;                // EA Magic Number

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

//===================================================================
// INTERNAL SETTINGS
//===================================================================
int      InpPollInterval    = 250;
bool     g_useLongPoll       = true;
bool     g_longPollFailed    = false;
datetime g_longPollRetryTime = 0;
#define  LONGPOLL_RETRY_INTERVAL 10
#define  BATCH_ACK_MAX         50                                 // Max signals per batch ACK request
#define  MAX_SIGNALS_PER_TICK  50                                 // Max signals processed per OnTimer tick
#define  WS_MAX_CONNECTING_TICKS  200                             // Max ticks in CONNECTING state before forced disconnect (20s at 100ms)
#define  WS_DEAD_CONNECTION_TIMEOUT 90                             // Seconds without receiving data before forcing reconnect (3 missed pongs)
//--- WebSocket globals
CPTWebSocketClient* g_wsClient = NULL;                             // WebSocket client instance (NULL if unavailable)
bool     g_useWebSocket    = false;                              // True when WS is connected and primary transport
int      g_wsReconnectAttempts = 0;                               // Current WS reconnect attempt count
int      g_wsConnectingTicks = 0;                                 // Ticks spent in CONNECTING state (for diagnostics)
datetime g_wsLastReconnectAttempt = 0;                             // Timestamp of last WS reconnect attempt (exponential backoff)
int      g_wsReconnectDelaySec = 1;                                // Current reconnect delay in seconds (doubles each attempt)
datetime g_wsLastStatsSent  = 0;                                  // Last time account stats were sent via WS
datetime g_wsLastHealthSent = 0;                                  // Last time health telemetry was sent via WS
int      g_wsLastPositionCount = -1;                              // Last known position count (-1 = unknown)
bool     g_wsInitialStateSent = false;                            // Whether initial state was sent after WS connect
bool     InpLogSignals      = true;
double   InpDefaultLots     = 0.01;
int      InpMaxSlippage     = 10;
double   InpCommission      = 0.0;
bool     InpRoundDown       = true;
string   InpDefaultComment  = "";
int      InpDashFontSize    = 9;
ENUM_PENDING_TYPE InpPendingType = PENDING_PIPS;

//===================================================================
// HIDDEN SL/TP SYSTEM
//===================================================================
struct HiddenTarget
{
   int      ticket;
   string   symbol;
   int      type;            // OP_BUY or OP_SELL
   double   hidden_sl;
   double   hidden_tp;
   double   entry_price;
};
#define MAX_HIDDEN_TARGETS 1000
HiddenTarget g_hiddenTargets[MAX_HIDDEN_TARGETS];
int g_hiddenCount = 0;

//===================================================================
// CLASS INSTANCES
//===================================================================
CPositionSizer* g_positionSizer  = NULL;
CPendingOrderManager* g_pendingManager = NULL;
CPartialCloseManager* g_partialManager = NULL;
COrderModificationManager* g_modifyManager = NULL;
CCombinedActionsManager* g_combinedManager = NULL;

// Phase 4: Production hardening class instances
CProductionLogger*       g_prodLogger = NULL;
CPriceFeedValidator*     g_priceValidator = NULL;
CLimitOrderValidator*    g_limitOrderValidator = NULL;
CConnectionManager*      g_connectionManager = NULL;
CInputValidator*         g_inputValidator = NULL;
CMemoryMonitor*          g_memoryMonitor = NULL;

//===================================================================
// GLOBAL VARIABLES
//===================================================================
bool g_eaEnabled = true;  // EA on/off state
datetime   g_lastPoll         = 0;
datetime   g_connectedSince   = 0;
datetime   g_lastSuccessfulRequest = 0;
int        g_consecutiveErrors = 0;
int        g_watchdogLevel = 0;
datetime   g_lastWatchdogAction = 0;
datetime   g_httpBackoffUntil = 0;         // Non-blocking HTTP backoff (replaces Sleep for server errors)
long       g_totalSignals     = 0;
long       g_successful       = 0;
long       g_failed           = 0;
long       g_todaySignals     = 0;
long       g_todaySuccessful  = 0;
long       g_todayFailed      = 0;
bool       g_lastPollSuccess  = false;
datetime   g_lastDashboardUpdate = 0;
long       g_spreadSamples     = 0;
double     g_avgSpread         = 0;
datetime   g_dailyResetTime    = 0;
double     g_dailyStartBalance = 0;
double     g_cumulativeStartBalance = 0;
bool       g_protectionHalted  = false;
bool       g_dailyHalted       = false;
int        g_lastExecError     = 0;   // last OrderSend/OrderClose error code - read by POST-CHECK
//===================================================================
// UPDATE NOTIFICATION
//===================================================================
string     g_latestVersion     = "";    // Latest version from server
string     g_updateNotes       = "";    // Release notes for latest version
bool       g_updateAvailable   = false; // True if server version > local version
datetime   g_lastVersionCheck  = 0;     // Last time we checked version (throttle to 5 min)
bool       g_eaJustUpdated     = false; // True if EA update was applied this session
#define    VERSION_CHECK_INTERVAL 300   // Check version every 5 minutes (seconds)
//===================================================================
// AUTO-UPDATE STATE
//===================================================================
string     g_updateFilePath    = "";    // Path to downloaded update file (sandbox)
string     g_updateDLLPath     = "";    // Path to downloaded DLL update (sandbox)
string     g_updateFileVersion = "";    // Version of the downloaded update
bool       g_updateDownloaded  = false; // True when update file is ready to apply
bool       g_dllUpdateDownloaded = false; // True if DLL update file is ready
bool       g_dllJustUpdated    = false; // True if DLL update was applied this session
datetime   g_lastAuditSent    = 0;     // Last time audit data was sent to server
datetime   g_eaStartTime      = 0;     // EA start time (set in OnInit)
bool       g_isVps = false;                 // True if running on a VPS
string     g_vpsProvider = "";              // VPS provider name (e.g., "AWS", "Hetzner")
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
struct TicketSignalEntry { int ticket; string signal_id; };
TicketSignalEntry g_ticketSignalMap[TICKET_SIGNAL_MAP_SIZE];
int        g_ticketSignalCount = 0;

//===================================================================
// COMMAND TYPES
//===================================================================
enum CommandType
{
   COMMAND_NONE = 0,
   COMMAND_BUY,
   COMMAND_SELL,
   COMMAND_CLOSE_ALL,
   COMMAND_CLOSE_LONG,
   COMMAND_CLOSE_SHORT,
   COMMAND_BUY_LIMIT,
   COMMAND_SELL_LIMIT,
   COMMAND_BUY_STOP,
   COMMAND_SELL_STOP,
   COMMAND_CANCEL_LONG,
   COMMAND_CANCEL_SHORT,
   COMMAND_CLOSE_LONG_PCT,
   COMMAND_CLOSE_SHORT_PCT,
   COMMAND_CLOSE_LONG_VOL,
   COMMAND_CLOSE_SHORT_VOL,
   COMMAND_SLTP__LONG,
   COMMAND_SLTP__SHORT,
   COMMAND_SLTP_BUY_STOP,
   COMMAND_SLTP_BUY_LIMIT,
   COMMAND_SLTP_SELL_STOP,
   COMMAND_SLTP_SELL_LIMIT,
   COMMAND_CLOSE_LONG_SHORT,
   COMMAND_EXIT,  // Alias for CLOSE_ALL - closes all positions for symbol with comment filter
   COMMAND_CLOSE_LONG_OPEN_LONG,
   COMMAND_CLOSE_LONG_OPEN_SHORT,
   COMMAND_CLOSE_SHORT_OPEN_LONG,
   COMMAND_CLOSE_SHORT_OPEN_SHORT,
   COMMAND_CLOSE_LONGSHORT_OPEN_LONG,
   COMMAND_CLOSE_LONGSHORT_OPEN_SHORT,
   COMMAND_CANCEL_LONG_BUY_STOP,
   COMMAND_CANCEL_LONG_BUY_LIMIT,
   COMMAND_CANCEL_SHORT_SELL_STOP,
   COMMAND_CANCEL_SHORT_SELL_LIMIT,
   COMMAND_EA_OFF,
   COMMAND_EA_ON,
   COMMAND_CLOSEALL_EA_OFF
};

enum ENUM_ORDER_DELETE_RESOLUTION
{
   ORDER_DELETE_RESOLUTION_UNCERTAIN = 0,
   ORDER_DELETE_RESOLUTION_CONFIRMED = 1,
   ORDER_DELETE_RESOLUTION_FILLED = 2
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
    bool        has_sl;            // Whether SL was present in signal (distinguishes sl=0 breakeven from absent SL)
    bool        nm;                 // Near-Market flag: enables limit order conversion for price improvement
};
//+------------------------------------------------------------------+
//| Signal Queue - holds signals when market is temporarily closed   |
//+------------------------------------------------------------------+
#define SQ_MAX_SIZE         100
#define SQ_MAX_DRAIN_RETRIES 120  // ~2 min at 1-s timer before abandoning a 132 retry
#define SQ_MAX_QUEUE_TIME_SEC 1800  // 30 min max time in queue before expiry

struct SQueuedSignal
{
   SignalCommand cmd;
   datetime      queued_time;
   bool          is_retry;     // true = ExecuteCommand already ran once (lock file may exist)
   int           drain_retries; // count of consecutive 132 retries from DrainSignalQueue
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

//===================================================================
// FORWARD DECLARATIONS
//===================================================================
bool PollForSignals();
string TransformSymbol(const string symbol);
SignalCommand ParseSignal(const string signal_json);
bool ExecuteCommand(const SignalCommand &command, bool from_queue = false);
bool IsQueueableCommandType(CommandType type);
void DrainSignalQueue();
bool SendCloseReport(string symbol, int ticket, double close_price, double profit, string signal_id = "");
bool SendTradeReport(string action, string symbol, double volume, double price, int ticket, bool success, string error_msg = "", string signal_id = "");
bool AddHiddenTarget(int ticket, string symbol, int position_type, double sl_price, double tp_price, double entry_price);
void InitializeSpreadHistory();
void CheckDailyReset();
double CalculateVolumeWithExplicitType(const SignalCommand &cmd, double entry_price, double sl_price, int order_type);
double CalculateSLPriceWithExplicitType(const SignalCommand &cmd, double entry_price, bool is_buy);
double CalculateTPPriceWithExplicitType(const SignalCommand &cmd, double entry_price, bool is_buy);
// V7.06: Layer-4 dedup defense (broker-state reconciliation)
string BuildOrderComment(const string signal_id, const string user_comment);
bool   IsDuplicateSignalPosition(const string signal_id, const string symbol);
// V7.06: Cancel-confirmation helpers (orchestrate OrderDelete + history check)
bool   WaitForOrderToLeavePool(int order_ticket, int timeout_ms);
bool   FindOrderFillInTrades(int order_ticket, double &fill_price, int &fill_ticket);
ENUM_ORDER_DELETE_RESOLUTION ResolveOrderDelete(int order_ticket, int timeout_ms, double &fill_price, int &fill_ticket);

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
    // Convert enum magic number to global int (matching MT5)
    g_magicNumber = (int)InpMagicNumber;
    
    // Phase 4: Initialize production hardening - logger first
    g_prodLogger = new CProductionLogger("PineTunnel", false);
    
    // Phase 4: Input validation using production hardening
    g_inputValidator = new CInputValidator(g_prodLogger);

    bool inputsValid = true;

    if(!g_inputValidator.ValidateLicenseID(InpLicenseID))
       inputsValid = false;

    if(!g_inputValidator.ValidateURL(InpServerURL, "Server URL"))
       inputsValid = false;

    // Validate time inputs
    if(!g_inputValidator.ValidateTimeString(InpStartTime, "Start Time"))
       inputsValid = false;

    if(!g_inputValidator.ValidateTimeString(InpEndTime, "End Time"))
       inputsValid = false;

    // Validate numeric ranges
    if(!g_inputValidator.ValidateRange(InpRisk, 0.01, 100.0, "Risk"))
       inputsValid = false;

    if(!g_inputValidator.ValidateRange(InpStopLoss, 0.0, 10000.0, "Stop Loss"))
       inputsValid = false;

    if(!g_inputValidator.ValidateRange(InpTakeProfit, 0.0, 10000.0, "Take Profit"))
       inputsValid = false;

    if(!g_inputValidator.ValidateRange(InpDefaultLots, 0.01, 999999.0, "Default Lots"))
       inputsValid = false;

    if(!g_inputValidator.ValidateRangeInt(InpPollInterval, 100, 60000, "Poll Interval"))
       inputsValid = false;

    if(!g_inputValidator.ValidateRangeInt(InpMaxSlippage, 0, 1000, "Max Slippage"))
       inputsValid = false;

    if(!inputsValid)
    {
       if(g_prodLogger != NULL) g_prodLogger.Error("Input validation failed - EA initialization aborted");
       if(g_inputValidator != NULL) { delete g_inputValidator; g_inputValidator = NULL; }
       if(g_prodLogger != NULL) { delete g_prodLogger; g_prodLogger = NULL; }
       return INIT_FAILED;
     }

    // Map new inputs to backward compatibility variables
    InpDashFontSize = InpFontSize;

    // Production startup logging
    g_prodLogger.Info("v" + PT_VERSION + " | Lic: " + InpLicenseID + " | " + EnumToString(InpConnectionMode) + " | Magic: " + IntegerToString(g_magicNumber));

    // Validate hidden offset
    if(InpHiddenSLTP == HIDDEN_ON && (InpHiddenOffset < 50.0 || InpHiddenOffset > 500.0))
    {
       g_prodLogger.Warning("Hidden SL/TP offset " + DoubleToString(InpHiddenOffset, 0) +
                          " outside recommended range (50-500 pips)");
    }
   
   // Initialize position sizer
   g_positionSizer = new CPositionSizer(InpCommission, InpRoundDown);
   if(g_positionSizer == NULL)
   {
      Print("[PineTunnel] ERROR: Failed to create position sizer");
      return INIT_FAILED;
   }
   
   // Initialize pending order manager
   g_pendingManager = new CPendingOrderManager(g_magicNumber, InpMaxSlippage);
   if(g_pendingManager == NULL)
   {
      Print("[PineTunnel] ERROR: Failed to create pending order manager");
      return INIT_FAILED;
   }
   
   // Initialize partial close manager
   g_partialManager = new CPartialCloseManager(g_magicNumber, InpPartialClosePercentage, InpMaxSlippage);
   if(g_partialManager == NULL)
   {
      Print("[PineTunnel] ERROR: Failed to create partial close manager");
      return INIT_FAILED;
   }
   
   // Initialize order modification manager
   ENUM_TARGET_TYPE mod_target_type = TARGET_TYPE_PIPS;
   if(InpTargetType == TARGET_TYPE_PRICE) mod_target_type = TARGET_TYPE_PRICE;
   else if(InpTargetType == TARGET_TYPE_PERCENTAGE) mod_target_type = TARGET_TYPE_PERCENTAGE;
   
   g_modifyManager = new COrderModificationManager(g_magicNumber, mod_target_type);
   if(g_modifyManager == NULL)
   {
      Print("[PineTunnel] ERROR: Failed to create order modification manager");
      return INIT_FAILED;
   }
   
   // Initialize combined actions manager
   g_combinedManager = new CCombinedActionsManager(g_magicNumber, InpMaxSlippage);
   if(g_combinedManager == NULL)
   {
      Print("[PineTunnel] ERROR: Failed to create combined actions manager");
      return INIT_FAILED;
   }

   // Respect user's log setting on all managers (parity with MT5)
   g_pendingManager.SetLogging(InpLogSignals);
   g_partialManager.SetLogging(InpLogSignals);
   g_modifyManager.SetLogging(InpLogSignals);
   g_combinedManager.SetLogging(InpLogSignals);

   // Initialize signal queue for market-open delay handling
   if(InpEnableSignalQueue)
   {
      g_signalQueue = new CSignalQueue();
   }

   // Phase 4: Initialize remaining production hardening components
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

    // Initialize state file path for duplicate signal prevention
    g_stateFilePath = "PineTunnel_" + InpLicenseID + "_state.txt";

    // Validate state file path by testing write access
    int testHandle = FileOpen(g_stateFilePath, FILE_WRITE|FILE_TXT|FILE_ANSI);
    if(testHandle == INVALID_HANDLE)
    {
       PrintFormat("[PineTunnel] CRITICAL: Cannot write to state file: %s", g_stateFilePath);
       PrintFormat("[PineTunnel] Error: %d - Check file permissions and terminal directory", GetLastError());
       g_prodLogger.Error("State file write test failed - crash recovery unavailable");
    }
    else
    {
       FileClose(testHandle);
    }

    // Initialize connection tracking
    g_connectedSince = TimeCurrent();
    g_lastSuccessfulRequest = TimeCurrent();
    g_eaStartTime = TimeCurrent();
    g_lastPoll = 0;  // Force initial poll

    // Initialize Account Protection tracking
    g_cumulativeStartBalance = AccountBalance();
    g_protectionHalted = false;
    g_dailyHalted = false;

    // Initialize watchdog tracking
    g_consecutiveErrors = 0;
   
   // V7.06: Clean up stale lock files on startup (transient files - safe to delete all)
   {
      string lock_pattern = "PineTunnel_lock_";
      string found_file = "";
      long search_handle = FileFindFirst(lock_pattern + "*.lock", found_file);
      if(search_handle != INVALID_HANDLE)
      {
         int cleaned = 0;
         do
         {
            if(FileDelete(found_file))
               cleaned++;
         }
         while(FileFindNext(search_handle, found_file));
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

     // Load executed signals from file
   LoadExecutedSignals();
   
    // Initialize spread history
    InitializeSpreadHistory();
    
    // Initialize daily tracking (load today's trades from history)
    CheckDailyReset();
   
    // Start timer for polling (100ms interval for fast WS signal processing)
    EventSetMillisecondTimer(InpWSPollIntervalMs);

    // Map InpPendingOrderEntry to InpPendingType
    if(InpPendingOrderEntry == PENDING_PIPS_FROM_MARKET)
       InpPendingType = PENDING_PIPS;
    else if(InpPendingOrderEntry == PENDING_PRICE_FROM_SIGNAL)
       InpPendingType = PENDING_PRICE;
    else if(InpPendingOrderEntry == PENDING_PERCENT_FROM_MARKET)
       InpPendingType = PENDING_PERCENT;

    // Draw initial dashboard
    if(InpShowDashboard)
       DrawDashboard();
    
    // Apply pending EA update from previous session (swaps _new.ex4 -> .ex4)
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
            int chartWnd = (int)WindowHandle(Symbol(), Period());
            if(chartWnd != 0)
               g_wsClient.SetNotifyWindow(chartWnd, PTWS_NOTIFY_MSG_BASE + 1);
            CPTWebSocketClient::SetLogLevel(InpWSLogLevel);
            PrintFormat("[PineTunnel] WSS enabled (mode=%d, heartbeat=%ds, poll=%dms)",
                        InpConnectionMode, InpWSHeartbeatSec, InpWSPollIntervalMs);
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
   
     // Auto-update and audit deferred to OnTimer (avoid blocking OnInit with RunNetworkDiag)
     g_lastVersionCheck = 0;  // Force first-tick update check
     g_lastAuditSent = 0;     // Force first-tick audit send

    // Input validation
    if(InpWSPollIntervalMs < 50 || InpWSPollIntervalMs > 60000)
    {
       Print("PineTunnel: InpWSPollIntervalMs must be 50-60000, got ", InpWSPollIntervalMs);
       return INIT_PARAMETERS_INCORRECT;
    }
    if(InpWSHeartbeatSec > 0 && InpWSHeartbeatSec < 5)
    {
       Print("PineTunnel: InpWSHeartbeatSec must be >= 5 or 0, got ", InpWSHeartbeatSec);
       return INIT_PARAMETERS_INCORRECT;
    }
    if(InpUpdateCheckInterval > 0 && InpUpdateCheckInterval < 60)
    {
       Print("PineTunnel: InpUpdateCheckInterval must be >= 60 or 0, got ", InpUpdateCheckInterval);
       return INIT_PARAMETERS_INCORRECT;
    }
    if(InpNearMarketPoints < 1)
    {
       Print("PineTunnel: InpNearMarketPoints must be >= 1");
       return INIT_PARAMETERS_INCORRECT;
    }
    if(InpEntryLimitTimeoutMs < 100)
    {
       Print("PineTunnel: InpEntryLimitTimeoutMs must be >= 100");
       return INIT_PARAMETERS_INCORRECT;
    }
    if(InpExitLimitTimeoutMs < 100)
    {
       Print("PineTunnel: InpExitLimitTimeoutMs must be >= 100");
       return INIT_PARAMETERS_INCORRECT;
    }
    if(InpDailyTimezoneGMT < -12 || InpDailyTimezoneGMT > 14)
    {
       Print("PineTunnel: InpDailyTimezoneGMT must be -12 to +14, got ", InpDailyTimezoneGMT);
       return INIT_PARAMETERS_INCORRECT;
    }

     return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   // Stop timer
   EventKillTimer();
   
   // Apply pending EA update if downloaded
   ApplyPendingUpdate();
   
   // Restore normal Windows sleep behavior
   CPTWebSocketClient::AllowSleep();
   
   // Remove dashboard
   RemoveDashboard();
   
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
      g_memoryMonitor.LogStatistics();
   
   // V7.04: Clean up cross-instance signal locks owned by this instance
   // Search for any lock files we created and remove them
   int cleaned = 0;
   string lock_pattern = "PineTunnel_lock_";
   string found_file = "";
   long search_handle = FileFindFirst(lock_pattern + "*.lock", found_file);
   if(search_handle != INVALID_HANDLE)
   {
      do
      {
         // Read the lock file to check if it's ours
         int lock_handle = FileOpen(found_file, FILE_READ|FILE_TXT);
         if(lock_handle != INVALID_HANDLE)
         {
            string lock_owner = FileReadString(lock_handle);
            FileClose(lock_handle);
            
            // If this lock belongs to our chart, delete it
            string our_id = Symbol() + "_" + IntegerToString(ChartID());
            if(StringFind(lock_owner, our_id) >= 0)
            {
               FileDelete(found_file);
               cleaned++;
               PrintFormat("[PineTunnel] Cleaned up signal lock: %s", found_file);
            }
         }
      }
      while(FileFindNext(search_handle, found_file));
      FileFindClose(search_handle);
      if(cleaned > 0)
         PrintFormat("[PineTunnel] Cleaned %d signal lock file(s)", cleaned);
   }
   
   // Delete trading class instances
   if(g_positionSizer != NULL) { delete g_positionSizer; g_positionSizer = NULL; }
   if(g_pendingManager != NULL) { delete g_pendingManager; g_pendingManager = NULL; }
   if(g_partialManager != NULL) { delete g_partialManager; g_partialManager = NULL; }
   if(g_modifyManager != NULL) { delete g_modifyManager; g_modifyManager = NULL; }
   if(g_combinedManager != NULL) { delete g_combinedManager; g_combinedManager = NULL; }
   if(g_signalQueue != NULL)     { delete g_signalQueue;     g_signalQueue = NULL; }
   
   // Phase 4: Delete production hardening instances
   if(g_priceValidator != NULL) { delete g_priceValidator; g_priceValidator = NULL; }
   if(g_limitOrderValidator != NULL) { delete g_limitOrderValidator; g_limitOrderValidator = NULL; }
   if(g_connectionManager != NULL) { delete g_connectionManager; g_connectionManager = NULL; }
   if(g_inputValidator != NULL) { delete g_inputValidator; g_inputValidator = NULL; }
   if(g_memoryMonitor != NULL) { delete g_memoryMonitor; g_memoryMonitor = NULL; }

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
      delete g_prodLogger;
      g_prodLogger = NULL;
   }
   else
   {
      PrintFormat("[PineTunnel] EA stopped | Signals: %d | Success: %d | Failed: %d",
                  g_totalSignals, g_successful, g_failed);
   }
}

//+------------------------------------------------------------------+
//| Process WebSocket signals - route by message type                  |
//| Server messages: {"type":"signal","signals":[{...}]}                |
//|                  {"type":"pong","timestamp":...}                    |
//|                  {"type":"version","latest_version_mt4":"..."}      |
//|                  {"type":"shutdown","reason":"server_restart"}     |
//+------------------------------------------------------------------+
void ProcessWebSocketSignals(string json_message)
{
   if(json_message == "")
      return;
   
   // Extract message type
   string msg_type = ExtractJSONString(json_message, "type");
   
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
      string server_version = ExtractJSONString(json_message, "latest_version_mt4");
      if(server_version != "" && server_version != g_latestVersion)
      {
         g_latestVersion = server_version;
         g_updateNotes = ExtractJSONString(json_message, "update_notes_mt4");
         
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
      int errorCode = (int)StrToInteger(ExtractJSONString(json_message, "code"));
      string errorReason = ExtractJSONString(json_message, "reason");
      PrintFormat("[PineTunnel] WebSocket error received: code=%d reason=%s", errorCode, errorReason);
      // Server will close the connection after this message.
      // EA will fall back to HTTP long-polling automatically in next OnTimer tick.
      // Common codes: 4001=invalid license, 4002=server shutdown, 4003=rate limited, 4004=idle timeout
      return;
   }
   
   // -- SHUTDOWN: Server is shutting down, switch to HTTP immediately --
   if(msg_type == "shutdown")
   {
      string reason = ExtractJSONString(json_message, "reason");
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
         int days_back = (int)StrToInteger(ExtractJSONString(json_message, "days_back"));
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
   while(signals_end < StringLen(json_message) && bracket_depth > 0)
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
   
   int signals_len = StringLen(signals_json);

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
   while(pos < signals_len && signal_count < MAX_SIGNALS_PER_TICK)
   {
      // Find start of JSON object
      int obj_start = StringFind(signals_json, "{", pos);
      if(obj_start < 0) break;
      
      // Find matching closing brace
      int brace_count = 1;
      int obj_end = obj_start + 1;
      bool ws_in_string = false;
      while(obj_end < signals_len && brace_count > 0)
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
            
            // POST-CHECK: queue if execution failed and market closed/error 132 (MT4 market closed)
            if(!exec_ok && InpEnableSignalQueue && g_signalQueue != NULL && IsQueueableCommandType(cmd.type))
            {
               if(!IsMarketOpenForSymbol(cmd.symbol) || g_lastExecError == 132)
               {
                  queued = true;
                  g_signalQueue.Push(cmd, true);
                  string reason = (g_lastExecError == 132 && IsMarketOpenForSymbol(cmd.symbol))
                                  ? "open-transition (132)" : "market closed";
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
   
   if(signal_count >= MAX_SIGNALS_PER_TICK && pos < signals_len)
      PrintFormat("[PineTunnel] WebSocket: %d signal(s) deferred to next tick (limit %d)", signal_count, MAX_SIGNALS_PER_TICK);
}

//+------------------------------------------------------------------+
//| Timer function - Main polling loop                               |
//+------------------------------------------------------------------+
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
   }
   else if(g_watchdogLevel > 0)
   {
      // Success recovered - gradually reduce level
      g_watchdogLevel = MathMax(g_watchdogLevel - 1, 0);
      if(g_watchdogLevel == 0)
      {
         PrintFormat("[PineTunnel] Connection stable - Watchdog returning to normal operation");
      }
   }
   
   // Spread tracking disabled - using fixed historical average only

   // Check for daily reset (midnight)
   static datetime lastDailyCheck = 0;
   if((now - lastDailyCheck) >= 60)
   {
      lastDailyCheck = now;
      CheckDailyReset();
   }
   
   // Process hidden targets if enabled (high priority - check every tick)
   if(InpHiddenSLTP == HIDDEN_ON)
   {
      static uint lastHiddenTick = 0;
      if(g_hiddenCount > 0 && (GetTickCount() - lastHiddenTick) >= 500)
      {
         lastHiddenTick = GetTickCount();
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
            g_wsLastPositionCount = OrdersTotal();
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
         int currentOrders = OrdersTotal();
         if(g_wsLastPositionCount >= 0 && currentOrders != g_wsLastPositionCount)
         {
            g_wsClient.SendOpenPositions();
            g_wsClient.SendAccountStats();  // Account stats change when positions change
            g_wsLastPositionCount = currentOrders;
         }
         else if(g_wsLastPositionCount < 0)
         {
            g_wsLastPositionCount = currentOrders;
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
//| OnTick - Connection monitoring and price feed validation          |
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

//+------------------------------------------------------------------+
//| Chart event handler                                              |
//+------------------------------------------------------------------+
void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
    if(id == CHARTEVENT_CHART_CHANGE && InpShowDashboard)
    {
       DrawDashboard();
       return;
    }

   // PostMessage notification from DLL - frame arrived, process immediately
   // DLL posts WM_USER+1 (0x0401) via PostMessageW when a frame is queued.
   // MQL4 may or may not forward WM_USER messages to OnChartEvent depending on build.
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

//+------------------------------------------------------------------+
//| V7.00: STATE PERSISTENCE FUNCTIONS                               |
//+------------------------------------------------------------------+
void SaveExecutedSignal(const string signal_id)
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
         if(FileCopy(temp_path, 0, g_stateFilePath, FILE_REWRITE))
         {
            FileDelete(temp_path);
         }
      }
   }
}
bool IsSignalDuplicate(const string signal_id)
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
void SaveTicketSignal(int ticket, const string &signal_id)
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
string FindSignalByTicket(int ticket)
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
// AUTO-UPDATE: Download and apply EA/DLL updates from server (MT4)
//===================================================================

//+------------------------------------------------------------------+
//| Base64 decode helper - MT4 does not have a native function        |
//+------------------------------------------------------------------+
int Base64DecodeMQL4(string b64, int &output[])
{
   // Base64 decode table
   static int d[128];
   static bool d_init = false;
   if(!d_init)
   {
      for(int i = 0; i < 128; i++) d[i] = -1;
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
      int ch = StringGetCharacter(b64, i);
      if(ch >= 128 || d[ch] == -1)
      {
         if(ch == '=')
            break;
         continue;
      }
      buf = (buf << 6) | d[ch];
      bits += 6;
      if(bits >= 8)
      {
         bits -= 8;
         if(outIdx < outLen)
         {
            output[outIdx] = (buf >> bits) & 0xFF;
            outIdx++;
         }
      }
   }
   
   return outIdx;
}

//+------------------------------------------------------------------+
//| Check for updates via lightweight version check endpoint          |
//+------------------------------------------------------------------+
bool CheckForEAUpdate()
{
   if(!InpAutoUpdate) return false;
   
   datetime now = TimeCurrent();
   if(g_lastVersionCheck > 0 && (now - g_lastVersionCheck) < InpUpdateCheckInterval)
      return false;
    
   string url = InpServerURL + UPDATE_CHECK_ENDPOINT + "mt4";
   string headers = "X-License-Key: " + InpLicenseID + "\r\n";
   
   char post_data[];
   char result_data[];
   string result_headers;
   int timeout = 5000;
   
   int res = WebRequest("GET", url, headers, timeout, post_data, result_data, result_headers);
   
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
   string server_version = ExtractJSONString(result, "latest_version");
   string file_available = ExtractJSONString(result, "file_available");
   
   if(server_version == "")
   {
      PrintFormat("[PineTunnel] Update check: no version in response");
      return false;
   }
   
   if(CompareVersions(server_version, PT_VERSION) > 0)
   {
      if(!g_updateAvailable)
      {
         PrintFormat("[PineTunnel] UPDATE AVAILABLE: v%s -> v%s", PT_VERSION, server_version);
         g_updateNotes = ExtractJSONString(result, "release_notes");
         if(g_updateNotes != "")
            PrintFormat("[PineTunnel] Release notes: %s", g_updateNotes);
      }
      g_latestVersion = server_version;
      g_updateAvailable = true;
      
      if(InpAutoUpdate && file_available == "true" && !g_updateDownloaded)
         DownloadEAUpdate(server_version);
   }
   else
   {
      g_updateAvailable = false;
      PrintFormat("[PineTunnel] Update check: v%s is current", PT_VERSION);
   }
   
   // -- Check for DLL update --
   string server_dll_version = ExtractJSONString(result, "latest_dll_version");
   string dll_available = ExtractJSONString(result, "dll_available");
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
   // Both .ex4 and .dll files are locked while terminal is running,
   // so the batch script waits for terminal exit before swapping either.
   if((g_updateDownloaded || g_dllUpdateDownloaded) && InpAutoRestart)
      TriggerAutoRestart();
   
   return g_updateAvailable;
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
   if(file_size == 0 || file_size > 10000000)
   {
      FileClose(handle);
      return "";
   }
   
   uchar data[];
   uint read = FileReadArray(handle, data, 0, (int)file_size);
   FileClose(handle);
   
   if(read != (uint)file_size)
      return "";
   
   uchar hash[];
   uchar key[];
   if(!CryptEncode(CRYPT_HASH_SHA256, data, key, hash))
      return "";
   
   string hex = "";
   for(int i = 0; i < ArraySize(hash); i++)
      hex += StringFormat("%02x", hash[i]);
   
   return hex;
}

//+------------------------------------------------------------------+
//| Download EA update from server (MT4 version)                     |
//+------------------------------------------------------------------+
bool DownloadEAUpdate(string target_version)
{
   string url = InpServerURL + UPDATE_DOWNLOAD_ENDPOINT + "mt4";
   string headers = "X-License-Key: " + InpLicenseID + "\r\n";
   
   PrintFormat("[PineTunnel] Downloading EA update v%s...", target_version);
   PrintFormat("[PineTunnel] Download URL: %s", url);
   
   // -- Try DLL-based download first (can write to Experts folder directly) --
   // Note: DownloadFile uses WinHTTP (standalone), not the WebSocket - so it works
   // whenever the DLL is loaded (g_wsClient != NULL), regardless of WS connection state.
   if(g_wsClient != NULL)
   {
      string experts_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL4\\Experts\\";
      string save_path = experts_dir + "PineTunnel_EA_MT4_new.ex4";
      
      int dlResult = CPTWebSocketClient::DownloadFile(url, headers, save_path, 30000);
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
   
   // -- Fallback: WebRequest to MQL4 sandbox --
   char post_data[];
   char result_data[];
   string result_headers;
   int timeout = 30000;
   
   int res = WebRequest("GET", url, headers, timeout, post_data, result_data, result_headers);
   
   if(res == -1)
   {
      PrintFormat("[PineTunnel] EA update download failed: WebRequest error %d", GetLastError());
      return false;
   }
   
   if(res != 200)
   {
      PrintFormat("[PineTunnel] EA update download failed: HTTP %d", res);
      return false;
   }
   
   string result = CharArrayToString(result_data);
   string b64_data = ExtractJSONString(result, "data");
   string file_sha256 = ExtractJSONString(result, "sha256");
   string filename = ExtractJSONString(result, "filename");
   string file_version = ExtractJSONString(result, "version");
   
   // Sanitize filename - strip path separators (MQL4 sandbox rejects paths with \ or /)
   int lastSep = MathMax(StringFind(filename, "\\"), StringFind(filename, "/"));
   if(lastSep >= 0)
      filename = StringSubstr(filename, lastSep + 1);
   
   if(b64_data == "" || filename == "")
   {
      PrintFormat("[PineTunnel] EA update download: invalid response (missing data/filename)");
      return false;
   }
   
   int data_len = StringLen(b64_data);
   if(data_len < 10)
   {
      PrintFormat("[PineTunnel] EA update download: base64 data too short (%d bytes)", data_len);
      return false;
   }
   
   // Write update file to MQL4/Files/ sandbox
   string update_filename = UPDATE_FILE_PREFIX + filename;
   int handle = FileOpen(update_filename, FILE_WRITE | FILE_BIN);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("[PineTunnel] EA update: cannot create file %s (error %d)", update_filename, GetLastError());
      return false;
   }
   
   // Base64 decode and write
   int decoded[];
   int decoded_len = Base64DecodeMQL4(b64_data, decoded);
   if(decoded_len <= 0)
   {
      PrintFormat("[PineTunnel] EA update: base64 decode failed");
      FileClose(handle);
      FileDelete(update_filename);
      return false;
   }
   
   for(int i = 0; i < decoded_len; i++)
      FileWriteInteger(handle, decoded[i], CHAR_VALUE);
   
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
   
   // Store update metadata
   g_updateFilePath = update_filename;
   g_updateFileVersion = file_version;
   g_updateDownloaded = true;
   
   PrintFormat("[PineTunnel] EA update v%s downloaded: %s (%d bytes, SHA-256 verified)",
               file_version, update_filename, decoded_len);
   PrintFormat("[PineTunnel] Restart terminal to apply update v%s", file_version);
   
   return true;
}

//+------------------------------------------------------------------+
//| Download DLL update from server (MT4 version)                   |
//| Downloads new PTWebSocket32.dll via DLL (bypasses sandbox).     |
//| Saves as PTWebSocket32_new.dll in the Libraries folder. On next  |
//| terminal restart, CheckPendingEAUpdate will swap _new -> .dll.  |
//+------------------------------------------------------------------+
bool DownloadDLLUpdate(string target_version)
{
   string url = InpServerURL + DLL_DOWNLOAD_ENDPOINT + "mt4";
   string headers = "X-License-Key: " + InpLicenseID + "\r\n";
   
   PrintFormat("[PineTunnel] Downloading DLL update v%s...", target_version);
   
   // -- Try DLL-based download (can write to Libraries folder directly) --
   if(g_wsClient != NULL)
   {
      string libs_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL4\\Libraries\\";
      string save_path = libs_dir + "PTWebSocket32_new.dll";
      
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
   
   PrintFormat("[PineTunnel] DLL update v%s: WebRequest fallback not supported for DLL files", target_version);
   PrintFormat("[PineTunnel] DLL auto-update requires WebSocket connection. Please reconnect.");
   return false;
}

//+------------------------------------------------------------------+
//| Send audit/telemetry data to server (MT4 version)               |
//+------------------------------------------------------------------+
bool SendAuditData()
{
   if(InpAuditInterval <= 0) return false;
   
   datetime now = TimeCurrent();
   if(g_lastAuditSent > 0 && (now - g_lastAuditSent) < InpAuditInterval)
      return false;
   
   // -- EA & DLL info --
   string platform = "mt4";
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
   string os_info = "N/A";  // MQL4 doesn't expose CPU architecture
   int terminal_pid = 0;  // No MQL4 API for PID
   int terminal_cpu_cores = TerminalInfoInteger(TERMINAL_CPU_CORES);
   int terminal_memory_phys = TerminalInfoInteger(TERMINAL_MEMORY_PHYSICAL);
   int terminal_memory_avail = TerminalInfoInteger(TERMINAL_MEMORY_AVAILABLE);
   int terminal_disk_space = TerminalInfoInteger(TERMINAL_DISK_SPACE);
   
   // -- Account identity --
   int account_number = AccountNumber();
   string account_name = EscapeJSON(AccountName());
   string account_server = EscapeJSON(AccountServer());
   string account_currency = EscapeJSON(AccountCurrency());
   string broker = EscapeJSON(AccountCompany());
   int account_leverage = AccountLeverage();
   
   // -- Account financials --
   double account_balance = AccountBalance();
   double account_credit = AccountCredit();
   double account_equity = AccountEquity();
   double account_profit = AccountProfit();
   double account_margin = AccountMargin();
   double account_margin_free = AccountFreeMargin();
   double account_margin_level = 0.0;
   if(account_margin > 0)
      account_margin_level = account_equity / account_margin * 100.0;
   bool account_trade_allowed = IsTradeAllowed();
   bool account_trade_expert = IsExpertEnabled();
   int symbol_count = SymbolsTotal(true);
   
   // -- Chart info --
   string chart_symbol = Symbol();
   string chart_timeframe = IntegerToString(Period());
   
   // -- Runtime stats --
   int position_count = OrdersTotal();
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
   json += "\"terminal_x64\":false,";  // MT4 is always 32-bit
   json += "\"terminal_pid\":" + IntegerToString(terminal_pid) + ",";
   json += "\"os\":\"" + EscapeJSON(os_info) + "\",";
   json += "\"cpu_cores\":" + IntegerToString(terminal_cpu_cores) + ",";
   json += "\"ram_mb\":" + IntegerToString(terminal_memory_phys) + ",";
   json += "\"ram_avail_mb\":" + IntegerToString(terminal_memory_avail) + ",";
   json += "\"disk_mb\":" + IntegerToString(terminal_disk_space) + ",";
   // Account identity
   json += "\"account_number\":" + IntegerToString(account_number) + ",";
   json += "\"account_name\":\"" + account_name + "\",";
   json += "\"account_server\":\"" + account_server + "\",";
   json += "\"account_currency\":\"" + account_currency + "\",";
   json += "\"broker\":\"" + broker + "\",";
   json += "\"trade_mode\":\"" + (IsDemo() ? "demo" : "real") + "\",";
   json += "\"leverage\":" + IntegerToString(account_leverage) + ",";
   json += "\"trade_allowed\":" + (account_trade_allowed ? "true" : "false") + ",";
   json += "\"trade_expert\":" + (account_trade_expert ? "true" : "false") + ",";
   // Account financials
   json += "\"balance\":" + DoubleToString(account_balance, 2) + ",";
   json += "\"credit\":" + DoubleToString(account_credit, 2) + ",";
   json += "\"equity\":" + DoubleToString(account_equity, 2) + ",";
   json += "\"profit\":" + DoubleToString(account_profit, 2) + ",";
   json += "\"margin\":" + DoubleToString(account_margin, 2) + ",";
   json += "\"margin_free\":" + DoubleToString(account_margin_free, 2) + ",";
   json += "\"margin_level\":" + DoubleToString(account_margin_level, 2) + ",";
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
   
   string url = InpServerURL + AUDIT_ENDPOINT + InpLicenseID;
   string headers = "Content-Type: application/json\r\nX-License-Key: " + InpLicenseID + "\r\n";
   
   char json_data[];
   char result_data[];
   string result_headers;
   int timeout = 5000;
   
   StringToCharArray(json, json_data, 0, WHOLE_ARRAY);
   
   int res = WebRequest("POST", url, headers, timeout, json_data, result_data, result_headers);
   
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
   
   string response = CharArrayToString(result_data);
   string latest_ver = ExtractJSONString(response, "latest_version");
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
//| Apply pending EA update (called from OnDeinit)                    |
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
      // WebRequest-based download saved to MQL4 sandbox
      PrintFormat("[PineTunnel] Update v%s pending: %s", g_updateFileVersion, g_updateFilePath);
      PrintFormat("[PineTunnel] To apply: copy %s to your MQL4/Experts/ folder and restart", g_updateFilePath);
   }
}

//+------------------------------------------------------------------+
//| Check for pending EA update from previous session                 |
//| On terminal restart, the DLL swaps PineTunnel_EA_MT4_new.ex4    |
//| over PineTunnel_EA_MT4.ex4 (old .ex4 not locked on restart).     |
//+------------------------------------------------------------------+
void CheckPendingEAUpdate()
{
   string experts_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL4\\Experts";
   int result = CPTWebSocketClient::ApplyUpdate(experts_dir, "PineTunnel_EA_MT4.ex4", "PineTunnel_EA_MT4_new.ex4");
   if(result == 0)
   {
      g_eaJustUpdated = true;
      PrintFormat("[PineTunnel] Applied pending EA update from previous session");
   }
   
   // Also check for pending DLL update (DLL not locked on terminal restart)
   string libs_dir = TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL4\\Libraries";
   int dllResult = CPTWebSocketClient::ApplyUpdate(libs_dir, "PTWebSocket32.dll", "PTWebSocket32_new.dll");
    if(dllResult == 0)
    {
       g_dllJustUpdated = true;
       PrintFormat("[PineTunnel] Applied pending DLL update from previous session");
    }
   
   // -- Clear restart counter (infinite loop protection) --
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
   
   // -- Safety: require pending update file --
   if(!g_updateDownloaded && !g_dllUpdateDownloaded)
   {
      PrintFormat("[PineTunnel] Auto-restart: no pending update files");
      return false;
   }
   
   string terminal_path = TerminalInfoString(TERMINAL_PATH);
   string data_path = TerminalInfoString(TERMINAL_DATA_PATH);
   
   // For portable mode, use the terminal executable directly
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
// V7.00: SL/TP VERIFICATION AFTER ORDER OPEN (MT4 version)
//===================================================================
bool VerifyAndFixSLTP(int ticket, string symbol, double intended_sl, double intended_tp, int max_retries = 3)
{
   if(ticket <= 0) return true;  // No ticket to verify
   if(intended_sl <= 0 && intended_tp <= 0) return true;  // No SL/TP to verify
   
   // Select the order
   if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
   {
      PrintFormat("[PineTunnel] V7.00 Could not select order #%d for SL/TP verification", ticket);
      return true;  // Order may have closed already, not an error
   }
   
   double current_sl = OrderStopLoss();
   double current_tp = OrderTakeProfit();
   
   // Check if SL/TP matches expected values (with tolerance)
   double point = MarketInfo(symbol, MODE_POINT);
   double tolerance = point * PRICE_TOLERANCE_POINTS;  // 5 point tolerance
   
   bool sl_ok = (intended_sl <= 0) || (MathAbs(current_sl - intended_sl) < tolerance);
   bool tp_ok = (intended_tp <= 0) || (MathAbs(current_tp - intended_tp) < tolerance);
   
   if(sl_ok && tp_ok)
   {
      PrintFormat("[PineTunnel] V7.00 SL/TP verified: SL=%.5f TP=%.5f", current_sl, current_tp);
      return true;
   }
   
   // SL/TP mismatch - attempt to fix
   PrintFormat("[PineTunnel] V7.00 SL/TP MISMATCH detected for #%d", ticket);
   PrintFormat("[PineTunnel] Expected: SL=%.5f TP=%.5f | Actual: SL=%.5f TP=%.5f", 
               intended_sl, intended_tp, current_sl, current_tp);
   
   // Attempt to modify order to correct SL/TP
   for(int retry = 0; retry < max_retries; retry++)
   {
      if(OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
      {
         double price = OrderOpenPrice();
         int digits = (int)MarketInfo(symbol, MODE_DIGITS);
         double sl = intended_sl > 0 ? NormalizeDouble(intended_sl, digits) : 0;
         double tp = intended_tp > 0 ? NormalizeDouble(intended_tp, digits) : 0;
         
         if(OrderModify(ticket, price, sl, tp, 0, clrNONE))
         {
            PrintFormat("[PineTunnel] V7.00 SL/TP FIXED on retry %d", retry + 1);
            return true;
         }
         
         int err = GetLastError();
         if(err == 1)
         {
            PrintFormat("[PineTunnel] V7.00 SL/TP already correct (no modification needed)");
            return true;
         }
         PrintFormat("[PineTunnel] V7.00 Retry %d/%d failed: %d - %s", 
                     retry + 1, max_retries, err, ErrorDescription(err));
      }
      
      Sleep(100);  // Brief pause before retry
   }
   
   // All retries failed - log critical error
   PrintFormat("[PineTunnel] V7.00 CRITICAL: Could not set SL/TP after %d retries!", max_retries);
   PrintFormat("[PineTunnel] V7.00 Order #%d may have NO STOP LOSS protection!", ticket);
   
   return false;
}

//+------------------------------------------------------------------+
//| Initialize spread history                                        |
//+------------------------------------------------------------------+
void InitializeSpreadHistory()
{
    // In MT4, we calculate from current spread
    double ask = MarketInfo(Symbol(), MODE_ASK);
    double bid = MarketInfo(Symbol(), MODE_BID);
    double point = MarketInfo(Symbol(), MODE_POINT);

    // Reset counters
    g_spreadSamples = 0;

    if(point > 0)
    {
       g_avgSpread = (ask - bid) / point;
       g_spreadSamples = 100;
    }
}

//+------------------------------------------------------------------+
//| Check daily reset                                                |
//+------------------------------------------------------------------+
void CheckDailyReset()
{
   datetime current_time = TimeCurrent();
   datetime current_gmt = current_time + (InpDailyTimezoneGMT * 3600);
   
   MqlDateTime dt_current, dt_last;
   TimeToStruct(current_gmt, dt_current);
   TimeToStruct(g_dailyResetTime + (InpDailyTimezoneGMT * 3600), dt_last);
   
   bool is_first_run = (g_dailyResetTime == 0);
   bool day_changed = (dt_current.day != dt_last.day || dt_current.mon != dt_last.mon || dt_current.year != dt_last.year);
   
   if(is_first_run || day_changed)
   {
      g_dailyStartBalance = AccountBalance();
      g_dailyResetTime = current_time;
      g_dailyHalted = false;
      g_todaySignals = 0;
      g_todaySuccessful = 0;
      g_todayFailed = 0;
      
      if(is_first_run)
         CountTodayTrades();
   }
}

//+------------------------------------------------------------------+
//| Count today's trades from history                                |
//+------------------------------------------------------------------+
void CountTodayTrades()
{
   datetime today_start = StringToTime(TimeToString(TimeCurrent(), TIME_DATE));
   
   int historical_count = 0;
   int open_count = 0;
   
   // Count closed trades from history
   int total_history = OrdersHistoryTotal();
   for(int i = 0; i < total_history; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY))
         continue;
      
      if(OrderMagicNumber() != g_magicNumber)
         continue;
      
      if(OrderOpenTime() >= today_start && OrderType() <= OP_SELL)
         historical_count++;
   }
   
   // Count open positions
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      
      if(OrderType() > OP_SELL)
         continue;
      
      if(OrderMagicNumber() == g_magicNumber)
         open_count++;
   }
   
   g_todaySignals = historical_count + open_count;
   g_todaySuccessful = historical_count + open_count;
}

//+------------------------------------------------------------------+
//| HTTP Polling for signals                                         |
//+------------------------------------------------------------------+
bool PollForSignals()
{
   // Non-blocking HTTP backoff check - skip request if still in cooldown
   // (replaces the old Sleep(60000) which blocked the entire OnTimer loop)
   if(g_httpBackoffUntil > 0 && TimeLocal() < g_httpBackoffUntil)
      return false;
   g_httpBackoffUntil = 0;  // Backoff expired

   uint start_time = GetTickCount(); // Track response time

   string correlation_id = IntegerToString(GetTickCount()) + "-" + IntegerToString(MathRand());
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
   
   uint response_time = GetTickCount() - start_time; // Calculate response time
   
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
      string status = ExtractJSONString(json_response, "status");
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
      string server_version = ExtractJSONString(json_response, "latest_version_mt4");
      if(server_version != "")
      {
         if(server_version != g_latestVersion)
         {
            g_latestVersion = server_version;
            g_updateNotes = ExtractJSONString(json_response, "update_notes_mt4");
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
            g_lastVersionCheck = 0;
         }
         else
         {
            g_updateAvailable = false;
         }
      }
   }
   
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
   int json_len = StringLen(json_response);
   int bracket_depth = 1;
   int signals_end = signals_start;
   bool arr_in_string = false;
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
   
   int signals_len = StringLen(signals_json);

   // We have actual signals

   PrintFormat("[Exec] [TIMING] Signal poll response: %ums | tick=%u", response_time, GetTickCount());

   // Parse each signal object in the array
   int pos = 0;
   int signal_count = 0;

   // Batch ACK: collect signal IDs during processing, send one HTTP call at the end
   string  batch_ack_ids[];
   bool    batch_ack_saved[];   // true = SaveExecutedSignal should be called after ACK
   int     batch_ack_count = 0;
   ArrayResize(batch_ack_ids, BATCH_ACK_MAX);
   ArrayResize(batch_ack_saved, BATCH_ACK_MAX);

   while(pos < signals_len && signal_count < MAX_SIGNALS_PER_TICK)
   {
      // Find next object
      int obj_start = StringFind(signals_json, "{", pos);
      if(obj_start < 0) break;

      // V7.01: Use bracket counting to handle nested JSON objects
      int bracket_count = 0;
      int obj_end = obj_start;
      bool found_complete = false;
      bool in_string = false;
      for(int i = obj_start; i < signals_len && !found_complete; i++)
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

            // POST-CHECK: queue if execution FAILED and market is closed OR error 132
            // (132/ERR_MARKET_CLOSED fires when MODE_TRADEALLOWED is true but broker matching
            //  engine is not yet accepting orders during the ~30-90s open-auction window)
            if(!exec_ok && InpEnableSignalQueue && g_signalQueue != NULL && IsQueueableCommandType(cmd.type))
            {
               if(!IsMarketOpenForSymbol(cmd.symbol) || g_lastExecError == 132)
               {
                  queued = true;
                  g_signalQueue.Push(cmd, true);  // is_retry=true: lock file may exist
                  string reason = (g_lastExecError == 132 && IsMarketOpenForSymbol(cmd.symbol))
                                  ? "open-transition (132)" : "market closed";
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
   if(signal_count >= MAX_SIGNALS_PER_TICK && pos < signals_len)
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

      // If broker still rejecting with 132 (open-auction window), keep in queue and retry
      // next OnTimer tick rather than silently dropping the signal.
      if(!exec_ok && g_lastExecError == 132)
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
            PrintFormat("[Queue] 132 retry %d/%d for %s on %s - next tick",
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
          // Non-retryable failure (not 132)
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

//+------------------------------------------------------------------+
//| Update connection health tracking                                |
//+------------------------------------------------------------------+
void UpdateConnectionHealth(uint response_time_ms, bool success)
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

//+------------------------------------------------------------------+
//| Acknowledge signal to server                                     |
//+------------------------------------------------------------------+
bool AcknowledgeSignal(const string signal_id)
{
   string correlation_id = IntegerToString(GetTickCount()) + "-" + IntegerToString(MathRand());
   string url = InpServerURL + "/api/signals/" + InpLicenseID + "/" + signal_id;
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";
   char data[];
   char result[];
   string result_headers;
   
   int timeout = 5000;
    uint start_time = GetTickCount();

    PrintFormat("[ACK] Acknowledging signal: %s | License: %s", signal_id, InpLicenseID);

    // Build JSON body for DELETE request
   string json = "{";
   json += "\"signal_id\":\"" + signal_id + "\",";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   json += "\"account\":" + IntegerToString(AccountNumber()) + ",";
   json += "\"broker\":\"" + EscapeJSON(AccountCompany()) + "\"";
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
   
   uint elapsed = GetTickCount() - start_time;
   
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
          PrintFormat("[ACK] FAILED to acknowledge %s | HTTP Status: %d | Time: %d ms",
                      signal_id, res, elapsed);
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

   string correlation_id = IntegerToString(GetTickCount()) + "-" + IntegerToString(MathRand());
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
    uint start_time = GetTickCount();

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

   uint elapsed = GetTickCount() - start_time;

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
          PrintFormat("[BATCH-ACK] FAILED | WebRequest Error: %d | Time: %d ms", GetLastError(), elapsed);
       else
          PrintFormat("[BATCH-ACK] FAILED | HTTP Status: %d | Time: %d ms", res, elapsed);

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
//| Escape JSON string                                                |
//+------------------------------------------------------------------+
string EscapeJSON(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\n", "\\n");
   StringReplace(value, "\r", "\\r");
   StringReplace(value, "\t", "\\t");
   return value;
}

//+------------------------------------------------------------------+
//| Normalize lots to broker step                                     |
//+------------------------------------------------------------------+
double NormalizeLots(string symbol, double lots)
{
   double min_lot = MarketInfo(symbol, MODE_MINLOT);
   double max_lot = MarketInfo(symbol, MODE_MAXLOT);
   double lot_step = MarketInfo(symbol, MODE_LOTSTEP);

   if(lots <= 0)
      return 0.0;

   // Sub-minimum requested: reject, do NOT inflate (100x risk on stocks)
   if(lots < min_lot)
   {
      PrintFormat("[PineTunnel] REJECT %s: volume %.4f < broker min %.2f (step %.2f). Increase lots or use risk-based sizing.",
                  symbol, lots, min_lot, lot_step);
      return 0.0;
   }

   if(max_lot > 0 && lots > max_lot) lots = max_lot;

   if(lot_step > 0)
       lots = MathFloor(lots / lot_step + LOT_ROUNDING_EPSILON) * lot_step;

   if(lots < min_lot) lots = min_lot;

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
//| Get command name string                                          |
//+------------------------------------------------------------------+
string CommandName(CommandType type)
{
   switch(type)
   {
      case COMMAND_BUY:          return "BUY";
      case COMMAND_SELL:         return "SELL";
      case COMMAND_CLOSE_ALL:    return "CLOSE_ALL";
      case COMMAND_CLOSE_LONG:   return "CLOSE_LONG";
      case COMMAND_CLOSE_SHORT:  return "CLOSE_SHORT";
      case COMMAND_BUY_LIMIT:     return "BUY_LIMIT";
      case COMMAND_SELL_LIMIT:    return "SELL_LIMIT";
      case COMMAND_BUY_STOP:      return "BUY_STOP";
      case COMMAND_SELL_STOP:     return "SELL_STOP";
      case COMMAND_CANCEL_LONG:   return "CANCEL_LONG";
      case COMMAND_CANCEL_SHORT:  return "CANCEL_SHORT";
      case COMMAND_CLOSE_LONG_PCT:   return "CLOSE_LONG_PCT";
      case COMMAND_CLOSE_SHORT_PCT:  return "CLOSE_SHORT_PCT";
      case COMMAND_CLOSE_LONG_VOL:   return "CLOSE_LONG_VOL";
      case COMMAND_CLOSE_SHORT_VOL:  return "CLOSE_SHORT_VOL";
      case COMMAND_SLTP__LONG:     return "SLTP_LONG";
      case COMMAND_SLTP__SHORT:    return "SLTP_SHORT";
      case COMMAND_SLTP_BUY_STOP:   return "SLTP_BUY_STOP";
      case COMMAND_SLTP_BUY_LIMIT:  return "SLTP_BUY_LIMIT";
      case COMMAND_SLTP_SELL_STOP:  return "SLTP_SELL_STOP";
      case COMMAND_SLTP_SELL_LIMIT: return "SLTP_SELL_LIMIT";
      case COMMAND_CLOSE_LONG_SHORT: return "CLOSE_ALL";
      case COMMAND_EXIT:             return "EXIT";
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
      case COMMAND_EA_OFF:           return "EA_OFF";
      case COMMAND_EA_ON:            return "EA_ON";
      case COMMAND_CLOSEALL_EA_OFF:  return "CLOSE_ALL_OFF";
      default:                       return "UNKNOWN";
   }
}

//+------------------------------------------------------------------+
//| Normalize command string to CommandType (parity with MT5)        |
//+------------------------------------------------------------------+
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
   
   // Partial close commands
   if(cmd == "CLOSE_LONG_PCT")
      return COMMAND_CLOSE_LONG_PCT;
   if(cmd == "CLOSE_SHORT_PCT")
      return COMMAND_CLOSE_SHORT_PCT;
   if(cmd == "CLOSE_LONG_VOL")
      return COMMAND_CLOSE_LONG_VOL;
   if(cmd == "CLOSE_SHORT_VOL")
      return COMMAND_CLOSE_SHORT_VOL;
   
   // Modification commands
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
   
   // close_all
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

//+------------------------------------------------------------------+
//| Parse signal from JSON                                           |
//+------------------------------------------------------------------+
SignalCommand ParseSignal(const string signal_json)
{
   SignalCommand cmd;
   cmd.signal_id = "";
   cmd.type = COMMAND_NONE;
   cmd.symbol = "";
   cmd.lots = InpDefaultLots;
   cmd.risk_percent = 0;
   cmd.stop_loss = 0;
   cmd.take_profit = 0;
   cmd.use_risk_sizing = false;
   cmd.comment = InpDefaultComment;  // Use configurable default (empty for PineTunnel compatibility)
   cmd.pending_distance = 0;
   cmd.has_pending = false;
   cmd.account_filter = 0;
   cmd.raw_action = "";
    cmd.vol_type = "";
    cmd.sl_type = "";
    cmd.tp_type = "";
    cmd.entry_type = "";
    cmd.partial_close_pct = 0;   // 0 = use EA default
    cmd.has_sl = false;          // Set to true when "sl" key found in signal JSON
    cmd.nm = false;              // Near-Market flag (set from nm=true param)
   
   // Extract signal_id (server stores as "signal_id")
   cmd.signal_id = ExtractJSONString(signal_json, "signal_id");
   if(cmd.signal_id == "")
      cmd.signal_id = ExtractJSONString(signal_json, "id");
   
   // Extract command
   string command_str = ExtractJSONString(signal_json, "action");
   if(command_str == "")
      command_str = ExtractJSONString(signal_json, "command");
   
   // V7.01: Store raw action string for validation
   cmd.raw_action = command_str;
   
   // Map command string to CommandType directly (parity with MT5)
   cmd.type = NormalizeCommand(command_str);
   
   // Extract symbol and apply prefix/suffix (except for special commands)
   string raw_symbol = ExtractJSONString(signal_json, "symbol");
   bool is_special = (raw_symbol == "ea_on" || raw_symbol == "ea_off" || raw_symbol == "close_all_off");
   cmd.symbol = is_special ? raw_symbol : TransformSymbol(raw_symbol);
   
   // Extract raw signal parameters from JSON
   string lots_str = ExtractJSONString(signal_json, "lots");
   double signal_lots = 0;
   if(lots_str != "" && IsValidNumberString(lots_str))
      signal_lots = StringToDouble(lots_str);
   else if(lots_str != "")
      PrintFormat("[PineTunnel] Invalid lots value: '%s'", lots_str);
   
   string risk_str = ExtractJSONString(signal_json, "risk");
   double signal_risk = 0;
   if(risk_str != "" && IsValidNumberString(risk_str))
      signal_risk = StringToDouble(risk_str);
   else if(risk_str != "")
      PrintFormat("[PineTunnel] Invalid risk value: '%s'", risk_str);
   
   string sl_str = ExtractJSONString(signal_json, "sl");
   double signal_sl = 0;
   if(sl_str != "" && IsValidNumberString(sl_str))
      signal_sl = StringToDouble(sl_str);
   else if(sl_str != "")
      PrintFormat("[PineTunnel] Invalid SL value: '%s'", sl_str);
    cmd.has_sl = (StringFind(signal_json, "\"sl\":") >= 0);
   
   string tp_str = ExtractJSONString(signal_json, "tp");
   double signal_tp = 0;
   if(tp_str != "" && IsValidNumberString(tp_str))
      signal_tp = StringToDouble(tp_str);
   else if(tp_str != "")
      PrintFormat("[PineTunnel] Invalid TP value: '%s'", tp_str);
   
   // Apply Input Setting Mode to determine which parameters to use (parity with MT5)
   double risk = 0;
   double sl = 0;
   double tp = 0;
   
   switch(InpSetting)
   {
      case SETTING_SIGNAL_PARAMS_ONLY:
         risk = signal_risk;
         sl = signal_sl;
         tp = signal_tp;
         break;
         
      case SETTING_EA_PARAMS_ONLY:
         risk = InpRisk;
         sl = InpStopLoss;
         tp = InpTakeProfit;
         break;
         
      case SETTING_SLTP_EA_RISK_SIGNAL:
         risk = signal_risk;
         sl = InpStopLoss;
         tp = InpTakeProfit;
         break;
         
      case SETTING_SLTP_SIGNAL_RISK_EA:
         risk = InpRisk;
         sl = signal_sl;
         tp = signal_tp;
         break;
         
      default:
         risk = signal_risk;
         sl = signal_sl;
         tp = signal_tp;
         break;
    }

    // Extract explicit types BEFORE use_lots_mode check (parity with MT5)
    // vol_type must be populated before the use_lots_mode evaluation below
    cmd.vol_type = ExtractJSONString(signal_json, "vol_type");
    cmd.sl_type = ExtractJSONString(signal_json, "sl_type");
    cmd.tp_type = ExtractJSONString(signal_json, "tp_type");
    cmd.entry_type = ExtractJSONString(signal_json, "entry_type");

     // Store lots from signal (InpSetting doesn't affect lots - only risk/sl/tp)
     cmd.lots = signal_lots > 0 ? signal_lots : cmd.lots;
     
     // Store the risk value appropriately based on volume type (parity with MT5)
      // When vol_type="lots" is present, signal_lots already has the correct value.
      // Preserve it when risk is absent (e.g. lots=0.57 with no "risk" key in JSON).
      // Signal vol_type overrides EA InpVolumeType setting
      bool use_lots_mode = (cmd.vol_type == "lots") || (cmd.vol_type == "" && InpVolumeType == VOLUME_LOTS);
     
     if(use_lots_mode)
     {
        // Use signal_lots when available (lots=), fall back to risk=, then default
        cmd.lots = risk > 0 ? risk : (cmd.lots > 0 ? cmd.lots : InpDefaultLots);
        cmd.risk_percent = 0;
        cmd.use_risk_sizing = false;
     }
    else
    {
       cmd.risk_percent = risk;
       cmd.lots = 0;
       cmd.use_risk_sizing = true;
    }
   
   // Always store SL and TP regardless of volume type
   cmd.stop_loss = sl;
   cmd.take_profit = tp;
   
   // Extract and validate comment (PineTunnel: max 20 chars, else blank)
   string comment_str = ExtractJSONString(signal_json, "comment");
   if(comment_str != "")
   {
      if(StringLen(comment_str) <= 20)
         cmd.comment = comment_str;
      else
      {
         if(InpLogSignals)
            PrintFormat("[PineTunnel] WARNING: Comment too long (%d chars) - max 20 allowed, setting to blank", 
                       StringLen(comment_str));
         cmd.comment = "";  // PineTunnel spec: longer than 20 = blank
      }
   }
   
   // Extract pending
   string pending_str = ExtractJSONString(signal_json, "pending");
   if(pending_str != "")
   {
       cmd.pending_distance = StringToDouble(pending_str);
       if(cmd.pending_distance > 0)
          cmd.has_pending = true;
    }
   
    // Extract account filter (key matches server signal_data and MT5)
    string acc_str = ExtractJSONString(signal_json, "acc_filter");
    if(acc_str != "") cmd.account_filter = StringToDouble(acc_str);
    
    // Extract Near-Market flag from nm=true parameter
   string nm_str = ExtractJSONString(signal_json, "nm");
   cmd.nm = (nm_str == "true" || nm_str == "1" || nm_str == "True");
   // Extract partial close percentage for close_long_pct/close_short_pct commands
   string pct_str = ExtractJSONString(signal_json, "pct");
   if(pct_str != "") cmd.partial_close_pct = StringToDouble(pct_str);

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
   
   // Validate required fields before processing signal (parity with MT5)
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

//+------------------------------------------------------------------+
//| Extract string value from JSON                                   |
//+------------------------------------------------------------------+
string ExtractJSONString(const string json, const string key)
{
   string search_key = "\"" + key + "\":";
   int key_pos = StringFind(json, search_key);
   if(key_pos < 0) return "";
   
   int json_len = StringLen(json);
   int value_start = key_pos + StringLen(search_key);
   
   // Skip whitespace
   while(value_start < json_len && 
         (StringGetCharacter(json, value_start) == ' ' || 
          StringGetCharacter(json, value_start) == '\t'))
   {
      value_start++;
   }
   
   if(value_start >= json_len) return "";
   
   ushort first_char = StringGetCharacter(json, value_start);
   
   if(first_char == '"')
   {
      // String value
      value_start++;
      int value_end = value_start;
      while(value_end < json_len)
      {
         ushort ch = StringGetCharacter(json, value_end);
         if(ch == '"' && (value_end == 0 || StringGetCharacter(json, value_end - 1) != '\\'))
            break;
         value_end++;
      }
      if(value_end >= json_len) return "";
      string raw = StringSubstr(json, value_start, value_end - value_start);
      StringReplace(raw, "\\\"", "\"");
      StringReplace(raw, "\\\\", "\\");
      return raw;
   }
   else
   {
      // Numeric or other value
      int value_end = value_start;
      while(value_end < json_len)
      {
         ushort c = StringGetCharacter(json, value_end);
         if(c == ',' || c == '}' || c == ' ' || c == '\n' || c == '\r')
            break;
         value_end++;
      }
      return StringSubstr(json, value_start, value_end - value_start);
   }
}

// V7.01: Helper function to validate if string is a valid number
bool IsValidNumberString(const string s)
{
   if(s == "") return false;
   bool has_digit = false;
   bool has_decimal = false;
   int len = StringLen(s);

   for(int i = 0; i < len; i++)
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

//+------------------------------------------------------------------+
//| Transform symbol with prefix/suffix                              |
//+------------------------------------------------------------------+
string TransformSymbol(const string symbol)
{
   if(InpPrefix == "" && InpSuffix == "")
      return symbol;
   
   string transformed = "";
   if(InpPrefix != "") transformed += InpPrefix;
   transformed += symbol;
   if(InpSuffix != "") transformed += InpSuffix;
   
   return transformed;
}

//+------------------------------------------------------------------+
//| Close positions                                                  |
//+------------------------------------------------------------------+
bool ClosePositions(string symbol, int pos_type, string comment = "", bool nmClose = false)
{
   bool result = true;
   int closed_count = 0;
   int total = OrdersTotal();
   
   for(int i = total - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      
      // Only market orders
      if(OrderType() > OP_SELL)
         continue;
      
      // Check magic number
      if(InpMagicRestriction == MAGIC_RESTRICT_ON && OrderMagicNumber() != g_magicNumber)
         continue;
      
      // Check symbol
      if(symbol != "" && OrderSymbol() != symbol)
         continue;
      
      // Check position type (-1 means all)
      if(pos_type >= 0 && OrderType() != pos_type)
         continue;
      
      // Check comment filter (use contains-match for broker modifications)
      if(comment != "")
      {
         string orderComment = OrderComment();
         if(StringFind(orderComment, comment) < 0)
            continue;
      }
      
       string order_symbol = OrderSymbol();
       int ticket = OrderTicket();
       double order_lots = OrderLots();
       int order_type = OrderType();
       double position_profit = OrderProfit() + OrderSwap() + OrderCommission();
       string position_type_str = (order_type == OP_BUY) ? "BUY" : "SELL";
       
       // V7.04: RefreshRates before getting close price to avoid stale data
       RefreshRates();
       double close_price = (order_type == OP_BUY) ? 
                            MarketInfo(order_symbol, MODE_BID) : 
                            MarketInfo(order_symbol, MODE_ASK);
       
       PrintFormat("[PineTunnel] Closing position: #%d | %s | %s | %.2f lots | P/L: $%.2f",
                   ticket, order_symbol, position_type_str, order_lots, position_profit);
       uint close_start = GetTickCount();
      
      // Aggressive limit close when opt-in enabled and nm flag is set.
      // TP-modify exit removed: zero commission saving (TP fills at taker rate),
      // adds hold risk, blocks EA thread, removes TP protection.
      bool useExitLimit = InpEnableExitLimit && InpEnableSmartMarket && nmClose;
      bool closed       = false;
      // V7.06: set true if a limit-exit cancel didn't confirm and no fill was recorded.
      // Forces the catch-all market close to be skipped - otherwise the still-live limit
      // could fill and create an orphan opposing position on hedging accounts.
      bool exit_aborted = false;
      
      if(useExitLimit)
      {
         // Place an opposite-direction limit order at a slight offset from market.
         // Original TP/SL remain untouched - position is still protected.
         // On timeout: cancel the limit order and market close.
         double point      = MarketInfo(order_symbol, MODE_POINT);
         int    stopsLevel = (int)MarketInfo(order_symbol, MODE_STOPLEVEL);
         int    effectivePoints = InpNearMarketPoints;
         if(stopsLevel > 0 && effectivePoints <= stopsLevel)
            effectivePoints = stopsLevel + 1;
         
         int    digits    = (int)MarketInfo(order_symbol, MODE_DIGITS);
         RefreshRates();
         double bid       = MarketInfo(order_symbol, MODE_BID);
         double ask       = MarketInfo(order_symbol, MODE_ASK);
         
         if(bid <= 0 || ask <= 0 || point <= 0)
         {
            PrintFormat("[PineTunnel] [DIAG] Invalid price data for %s limit exit - Bid:%.5f Ask:%.5f | falling back to market", order_symbol, bid, ask);
            closed = OrderClose(ticket, order_lots, close_price, InpMaxSlippage, CLR_NONE);
         }
         else
         {
            // BUY position -> SELL LIMIT above bid (higher close price)
            // SELL position -> BUY LIMIT below ask (lower close price)
            int    limit_op;
            double limit_price;
            
            if(order_type == OP_BUY)
            {
               limit_price = NormalizeDouble(bid + (effectivePoints * point), digits);
               limit_op    = OP_SELLLIMIT;
            }
            else
            {
               limit_price = NormalizeDouble(ask - (effectivePoints * point), digits);
               limit_op    = OP_BUYLIMIT;
            }
            
            bool skipExitLimit = false;
            if(g_limitOrderValidator != NULL)
            {
               if(!g_limitOrderValidator.ValidateLimitPrice(order_symbol, limit_price, limit_op))
               {
                  skipExitLimit = true;
                  PrintFormat("[PineTunnel] Exit limit validation failed for #%d - using market close", ticket);
               }
            }
            
            if(!skipExitLimit)
            {
               PrintFormat("[PineTunnel] Limit exit #%d: placing %s @ %.5f (Bid:%.5f Ask:%.5f offset:%d pts)",
                           ticket, (limit_op == OP_SELLLIMIT ? "SELL_LIMIT" : "BUY_LIMIT"),
                           limit_price, bid, ask, effectivePoints);
               
                int limit_ticket = OrderSend(
                   order_symbol, limit_op, order_lots, limit_price,
                   0, 0, 0, "NM_EXIT_" + IntegerToString(ticket),
                   g_magicNumber, 0, clrYellow);
                
                if(limit_ticket > 0)
                {
                   uint poll_start = GetTickCount();
                   bool limit_filled = false;
                   bool limit_cancelled = false;
                   
                    while((int)(GetTickCount() - poll_start) < InpExitLimitTimeoutMs)
                    {
                       if(IsStopped()) break;
                       // Guard: original position may have been closed by TP/SL
                      if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
                      {
                         PrintFormat("[PineTunnel] Original position #%d already closed (TP/SL hit) during exit limit poll", ticket);
                         closed = true;
                         limit_filled = true;
                         // Clean up the limit order if still pending
                         if(OrderSelect(limit_ticket, SELECT_BY_TICKET, MODE_TRADES) && OrderType() > OP_SELL)
                            OrderDelete(limit_ticket);
                         break;
                      }
                      
                      if(OrderSelect(limit_ticket, SELECT_BY_TICKET, MODE_TRADES))
                      {
                         if(OrderType() == OP_BUY || OrderType() == OP_SELL)
                         {
                            // Limit filled -> creates opposing position.
                            // Use OrderCloseBy to net both positions at the limit fill price.
                            // This avoids orphaned positions on hedging accounts.
                            if(OrderCloseBy(ticket, limit_ticket))
                            {
                               closed = true;
                               PrintFormat("[PineTunnel] Limit exit filled for #%d - close-by executed, maker rate achieved!", ticket);
                            }
                            else
                            {
                               // CloseBy failed (netting account may have auto-offset) - check if original is gone
                               if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
                               {
                                  closed = true;
                                  PrintFormat("[PineTunnel] Limit exit filled for #%d - position closed via netting offset", ticket);
                               }
                               else
                               {
                                  // Original still exists - market close as fallback
                                  RefreshRates();
                                  close_price = (order_type == OP_BUY) ? MarketInfo(order_symbol, MODE_BID) : MarketInfo(order_symbol, MODE_ASK);
                                  closed = OrderClose(ticket, order_lots, close_price, InpMaxSlippage, CLR_NONE);
                                  PrintFormat("[PineTunnel] CloseBy failed for #%d - market closing", ticket);
                               }
                            }
                            limit_filled = true;
                            break;
                         }
                      }
                      else
                      {
                         limit_cancelled = true;
                         PrintFormat("[PineTunnel] Limit exit order cancelled for #%d - market closing", ticket);
                         break;
                      }
                      Sleep(CANCEL_POLL_INTERVAL_MS);
                   }
                   
                   if(!limit_filled && !limit_cancelled)
                   {
                      // V7.06: Confirm cancel before market closing - otherwise a late
                      // limit fill during the cancel race creates an orphan opposing
                      // position on hedging accounts.
                      PrintFormat("[PineTunnel] Limit exit timeout for #%d - cancelling order", ticket);
                      if(OrderSelect(limit_ticket, SELECT_BY_TICKET, MODE_TRADES) && OrderType() > OP_SELL)
                         OrderDelete(limit_ticket);

                      bool cancel_confirmed = WaitForOrderToLeavePool(limit_ticket, CANCEL_CONFIRM_TIMEOUT_MS);
                      double late_price  = 0;
                      int    late_ticket = 0;
                      bool   late_filled = FindOrderFillInTrades(limit_ticket, late_price, late_ticket);

                      if(late_filled)
                      {
                         // Limit filled during cancel race - same handling as polling-success path
                         if(OrderCloseBy(ticket, late_ticket))
                         {
                            closed = true;
                            PrintFormat("[PineTunnel] Late-fill close-by executed for #%d (opposing #%d)",
                                        ticket, late_ticket);
                         }
                         else
                         {
                            // CloseBy failed; check if netting auto-offset already closed the original
                            if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES))
                            {
                               closed = true;
                               PrintFormat("[PineTunnel] Late-fill auto-offset closed #%d via netting", ticket);
                            }
                            else
                            {
                               // Original still open and CloseBy failed - best effort market close
                               RefreshRates();
                               close_price = (order_type == OP_BUY) ? MarketInfo(order_symbol, MODE_BID) : MarketInfo(order_symbol, MODE_ASK);
                               closed = OrderClose(ticket, order_lots, close_price, InpMaxSlippage, CLR_NONE);
                               PrintFormat("[PineTunnel] Late-fill CloseBy failed for #%d - market close (orphan may remain)", ticket);
                            }
                         }
                      }
                      else if(!cancel_confirmed)
                      {
                         // Cancel not confirmed AND no fill - limit may still be live.
                         // Market closing now would orphan an opposing limit fill.
                         PrintFormat("[PineTunnel] CRITICAL: Limit exit cancel for #%d not confirmed within %dms and no fill in history. Aborting auto-close to prevent orphan opposing order. Manual review required.",
                                     ticket, CANCEL_CONFIRM_TIMEOUT_MS);
                         exit_aborted = true;
                         result       = false;
                      }
                      // else: cancel confirmed, no fill - fall through to catch-all market close
                   }
                   
                   if(!closed && !exit_aborted)
                   {
                      RefreshRates();
                      close_price = (order_type == OP_BUY) ?
                                    MarketInfo(order_symbol, MODE_BID) :
                                    MarketInfo(order_symbol, MODE_ASK);
                      closed = OrderClose(ticket, order_lots, close_price, InpMaxSlippage, CLR_NONE);
                   }
                }
               else
               {
                  int err = GetLastError();
                  PrintFormat("[PineTunnel] Limit exit placement failed for #%d: %d - %s | market closing",
                              ticket, err, ErrorDescription(err));
                  closed = OrderClose(ticket, order_lots, close_price, InpMaxSlippage, CLR_NONE);
               }
            }
            else
            {
               closed = OrderClose(ticket, order_lots, close_price, InpMaxSlippage, CLR_NONE);
            }
         }
      }
      else
      {
         // Standard market close (default, fastest, most reliable)
         closed = OrderClose(ticket, order_lots, close_price, InpMaxSlippage, CLR_NONE);
      }
      
       if(closed)
       {
          PrintFormat("[Exec] [TIMING] Close position #%d: %ums | Sym=%s", ticket, GetTickCount() - close_start, order_symbol);
          PrintFormat("[PineTunnel] Closed position #%d | P/L: $%.2f",
                      ticket, position_profit);
          SendCloseReport(order_symbol, ticket, close_price, position_profit, FindSignalByTicket(ticket));
          closed_count++;
       }
       else
       {
          int error = GetLastError();
          g_lastExecError = error;
          PrintFormat("[PineTunnel] Failed to close position #%d: Error %d - %s",
                      ticket, error, ErrorDescription(error));
          // V7.04: [DIAG] logging for close failures
          PrintFormat("[PineTunnel] [DIAG] Close failure: Ticket=#%d | Symbol=%s | Type=%s | Volume=%.2f | OpenPrice=%.5f | P/L=$%.2f | Chart=%s",
                     ticket, order_symbol, position_type_str, order_lots, OrderOpenPrice(), position_profit, Symbol());
          PrintFormat("[PineTunnel] [DIAG] Close mode: ExitLimit=%s | Error=%d | LastError=%d",
                      (useExitLimit ? "YES" : "NO"), error, error);

          if(error == 4109)
          {
             PrintFormat("[PineTunnel] CRITICAL: Trading is not allowed on symbol %s", order_symbol);
             PrintFormat("[PineTunnel] Note: This is likely a simulation symbol with trading restrictions");
          }

          result = false;
       }
   }
   
   return result;
}

//+------------------------------------------------------------------+
//| Check pyramiding rules                                           |
//+------------------------------------------------------------------+
bool CanOpenPositionPyramiding(string symbol, int position_type)
{
   switch(InpPyramiding)
   {
      case PYRAMIDING_ON:
         return true;
         
      case PYRAMIDING_ON_IF_PROFIT:
         {
            double total_profit = 0.0;
            int position_count = 0;
            
            int total = OrdersTotal();
            for(int i = 0; i < total; i++)
            {
               if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
                  continue;
               
               if(OrderType() > OP_SELL)
                  continue;
               
               if(InpMagicRestriction == MAGIC_RESTRICT_ON && OrderMagicNumber() != g_magicNumber)
                  continue;
               
               if(OrderSymbol() == symbol && OrderType() == position_type)
               {
                  total_profit += OrderProfit() + OrderSwap() + OrderCommission();
                  position_count++;
               }
            }
            
            if(position_count == 0)
               return true;
            else if(total_profit > 0)
            {
               return true;
            }
            else
            {
               PrintFormat("[PineTunnel] Pyramiding: %d existing positions NOT in profit ($%.2f) - BLOCKING",
                          position_count, total_profit);
               return false;
            }
         }
         
      case PYRAMIDING_OFF_EITHER_OR:
         {
            int total = OrdersTotal();
            for(int i = 0; i < total; i++)
            {
               if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
                  continue;
               
               if(OrderType() > OP_SELL)
                  continue;
               
               if(InpMagicRestriction == MAGIC_RESTRICT_ON && OrderMagicNumber() != g_magicNumber)
                  continue;
               
               if(OrderSymbol() == symbol)
               {
                  PrintFormat("[PineTunnel] Pyramiding: Already have position on %s - BLOCKING", symbol);
                  return false;
               }
            }
            return true;
         }
         
      case PYRAMIDING_OFF_BOTH:
         {
            int total = OrdersTotal();
            for(int i = 0; i < total; i++)
            {
               if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
                  continue;
               
               if(OrderType() > OP_SELL)
                  continue;
               
               if(InpMagicRestriction == MAGIC_RESTRICT_ON && OrderMagicNumber() != g_magicNumber)
                  continue;
               
               if(OrderSymbol() == symbol && OrderType() == position_type)
               {
                  PrintFormat("[PineTunnel] Pyramiding: Already have %s position on %s - BLOCKING",
                             position_type == OP_BUY ? "BUY" : "SELL", symbol);
                  return false;
               }
            }
            return true;
         }
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Check close on reverse                                           |
//+------------------------------------------------------------------+
bool ExecuteCloseOnReverse(const SignalCommand &cmd, int opposite_type)
{
   if(InpCloseOnReverse == CLOSE_REVERSE_OFF)
      return true;
   
   bool has_opposite = false;
   
   // Prepare comment match for filtering
   string matchComment = cmd.comment;
   
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      
      if(OrderType() > OP_SELL)
         continue;
      
      if(InpMagicRestriction == MAGIC_RESTRICT_ON && OrderMagicNumber() != g_magicNumber)
         continue;
      
      if(OrderSymbol() == cmd.symbol && OrderType() == opposite_type)
      {
         // Comment filter - only detect opposite positions from same strategy
         if(matchComment != "" && StringFind(OrderComment(), matchComment) < 0)
            continue;
         has_opposite = true;
         break;
      }
   }
   
   if(!has_opposite)
      return true;
   
   switch(InpCloseOnReverse)
   {
      case CLOSE_REVERSE_ON_HEDGING:
         PrintFormat("[PineTunnel] Close on Reverse (HEDGING): Closing %s positions, will open %s",
                    opposite_type == OP_BUY ? "BUY" : "SELL",
                    cmd.type == COMMAND_BUY ? "BUY" : "SELL");
         if(!ClosePositions(cmd.symbol, opposite_type, cmd.comment, cmd.nm))
         {
            PrintFormat("[PineTunnel] Close on Reverse FAILED - not opening new position");
            return false;
         }
         return true;
         
      case CLOSE_REVERSE_ON_NETTING:
         PrintFormat("[PineTunnel] Close on Reverse (NETTING): Closing %s positions, NOT opening %s",
                    opposite_type == OP_BUY ? "BUY" : "SELL",
                    cmd.type == COMMAND_BUY ? "BUY" : "SELL");
         ClosePositions(cmd.symbol, opposite_type, cmd.comment, cmd.nm);
         return false;
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Check max position limits                                        |
//+------------------------------------------------------------------+
bool CheckMaxPositionLimits(string symbol, int position_type)
{
   if(InpMaxOpenPositions <= 0 && InpMaxOpenPositionsPerSymbol <= 0 && InpMaxUniqueSymbols <= 0)
      return true;
   
   int total_positions = 0;
   int positions_on_symbol = 0;
   string unique_symbols[];
   ArrayResize(unique_symbols, 0);
   
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      
      if(OrderType() > OP_SELL)
         continue;
      
      if(InpMagicRestriction == MAGIC_RESTRICT_ON && OrderMagicNumber() != g_magicNumber)
         continue;
      
      total_positions++;
      
      if(OrderSymbol() == symbol)
         positions_on_symbol++;
      
      // Track unique symbols
      bool found = false;
      for(int j = 0; j < ArraySize(unique_symbols); j++)
      {
         if(unique_symbols[j] == OrderSymbol())
         {
            found = true;
            break;
         }
      }
      if(!found)
      {
         ArrayResize(unique_symbols, ArraySize(unique_symbols) + 1);
         unique_symbols[ArraySize(unique_symbols) - 1] = OrderSymbol();
      }
   }
   
   int unique_symbols_count = ArraySize(unique_symbols);
   
   if(InpMaxOpenPositions > 0 && total_positions >= InpMaxOpenPositions)
   {
      PrintFormat("[PineTunnel] Maximum Open Positions limit reached: %d / %d - BLOCKING new position",
                 total_positions, InpMaxOpenPositions);
      return false;
   }
   
   if(InpMaxOpenPositionsPerSymbol > 0 && positions_on_symbol >= InpMaxOpenPositionsPerSymbol)
   {
      PrintFormat("[PineTunnel] Maximum Positions per Symbol limit reached for %s: %d / %d - BLOCKING new position",
                 symbol, positions_on_symbol, InpMaxOpenPositionsPerSymbol);
      return false;
   }
   
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
   
   return true;
}

//+------------------------------------------------------------------+
//| Calculate SL price with explicit type support                    |
//+------------------------------------------------------------------+
double CalculateSLPriceWithExplicitType(const SignalCommand &cmd, double entry_price, bool is_buy)
{
   if(cmd.stop_loss <= 0) return 0;
   
   string symbol = cmd.symbol;
   double point = MarketInfo(symbol, MODE_POINT);
   int digits = (int)MarketInfo(symbol, MODE_DIGITS);
   
   // Determine target type
   ENUM_TARGET_TYPE target_type = InpTargetType;
   
    if(cmd.sl_type != "")
    {
       string sl_type_upper = cmd.sl_type;
       StringToUpper(sl_type_upper);
       
       if(sl_type_upper == "PIPS" || sl_type_upper == "PIP")
          target_type = TARGET_TYPE_PIPS;
       else if(sl_type_upper == "PRICE")
          target_type = TARGET_TYPE_PRICE;
       else if(sl_type_upper == "PERCENT" || sl_type_upper == "PCT" || sl_type_upper == "PERCENTAGE")
          target_type = TARGET_TYPE_PERCENTAGE;
       else
       {
          PrintFormat("[PineTunnel] Unknown explicit sl_type '%s', falling back to EA setting", cmd.sl_type);
       }
    }
    
    double sl_price = 0;
    
    switch(target_type)
   {
      case TARGET_TYPE_PIPS:
         {
            double sl_distance = cmd.stop_loss * point;
            sl_price = is_buy ? (entry_price - sl_distance) : (entry_price + sl_distance);
         }
         break;
         
      case TARGET_TYPE_PRICE:
         {
            if(is_buy && cmd.stop_loss >= entry_price)
            {
               PrintFormat("[PineTunnel] ERROR: Invalid SL for BUY at %.5f (SL %.5f above entry)", 
                          entry_price, cmd.stop_loss);
               return 0;
            }
            else if(!is_buy && cmd.stop_loss <= entry_price)
            {
               PrintFormat("[PineTunnel] ERROR: Invalid SL for SELL at %.5f (SL %.5f below entry)", 
                          entry_price, cmd.stop_loss);
               return 0;
            }
            sl_price = cmd.stop_loss;
         }
         break;
         
      case TARGET_TYPE_PERCENTAGE:
         {
            double sl_distance = entry_price * (cmd.stop_loss / 100.0);
            sl_price = is_buy ? (entry_price - sl_distance) : (entry_price + sl_distance);
         }
         break;
   }
   
   return NormalizeDouble(sl_price, digits);
}

//+------------------------------------------------------------------+
//| Calculate TP price with explicit type support                    |
//+------------------------------------------------------------------+
double CalculateTPPriceWithExplicitType(const SignalCommand &cmd, double entry_price, bool is_buy)
{
   if(cmd.take_profit <= 0) return 0;
   
   string symbol = cmd.symbol;
   double point = MarketInfo(symbol, MODE_POINT);
   int digits = (int)MarketInfo(symbol, MODE_DIGITS);
   
   ENUM_TARGET_TYPE target_type = InpTargetType;
   
    if(cmd.tp_type != "")
    {
       string tp_type_upper = cmd.tp_type;
       StringToUpper(tp_type_upper);
       
       if(tp_type_upper == "PIPS" || tp_type_upper == "PIP")
          target_type = TARGET_TYPE_PIPS;
       else if(tp_type_upper == "PRICE")
          target_type = TARGET_TYPE_PRICE;
       else if(tp_type_upper == "PERCENT" || tp_type_upper == "PCT" || tp_type_upper == "PERCENTAGE")
          target_type = TARGET_TYPE_PERCENTAGE;
       else
       {
          PrintFormat("[PineTunnel] Unknown explicit tp_type '%s', falling back to EA setting", cmd.tp_type);
       }
    }
    
    double tp_price = 0;
    
    switch(target_type)
   {
      case TARGET_TYPE_PIPS:
         {
            double tp_distance = cmd.take_profit * point;
            tp_price = is_buy ? (entry_price + tp_distance) : (entry_price - tp_distance);
         }
         break;
         
      case TARGET_TYPE_PRICE:
         tp_price = cmd.take_profit;
         break;
         
      case TARGET_TYPE_PERCENTAGE:
         {
            double tp_distance = entry_price * (cmd.take_profit / 100.0);
            tp_price = is_buy ? (entry_price + tp_distance) : (entry_price - tp_distance);
         }
         break;
   }
   
   return NormalizeDouble(tp_price, digits);
}

//+------------------------------------------------------------------+
//| Calculate volume with explicit type support                      |
//+------------------------------------------------------------------+
double CalculateVolumeWithExplicitType(const SignalCommand &cmd, double entry_price, double sl_price, int order_type)
{
    if(g_positionSizer == NULL)
    {
       PrintFormat("[PineTunnel] Position sizer not initialized, using default lots: %.2f", InpDefaultLots);
       return InpDefaultLots;
    }
    
    // Check if explicit vol_type is specified
    if(cmd.vol_type != "")
    {
       string vol_type_upper = cmd.vol_type;
       StringToUpper(vol_type_upper);
       double risk_value = 0;

       if(vol_type_upper == "LOTS")
       {
          risk_value = (cmd.lots > 0) ? cmd.lots : InpDefaultLots;
          return g_positionSizer.CalculateDirectLots(cmd.symbol, risk_value);
       }
       else if(vol_type_upper == "DOLLAR" || vol_type_upper == "USD")
       {
          risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
          if(sl_price <= 0)
          {
             PrintFormat("[PineTunnel] ERROR: usd requires stop loss price");
             return InpDefaultLots;
          }
          return g_positionSizer.CalculateDollarAmount(cmd.symbol, risk_value, entry_price, sl_price);
       }
       else if(vol_type_upper == "BAL_LOSS" || vol_type_upper == "BALANCE_LOSS")
       {
          risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
          if(sl_price <= 0)
          {
             PrintFormat("[PineTunnel] ERROR: risk_bal_pct requires stop loss price");
             return InpDefaultLots;
          }
          return g_positionSizer.CalculatePercentageBalanceLoss(cmd.symbol, risk_value, entry_price, sl_price);
       }
       else if(vol_type_upper == "EQ_LOSS" || vol_type_upper == "EQUITY_LOSS")
       {
          risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
          if(sl_price <= 0)
          {
             PrintFormat("[PineTunnel] ERROR: risk_eq_pct requires stop loss price");
             return InpDefaultLots;
          }
          return g_positionSizer.CalculatePercentageEquityLoss(cmd.symbol, risk_value, entry_price, sl_price);
       }
       else if(vol_type_upper == "EQ_MARGIN" || vol_type_upper == "EQUITY_MARGIN")
       {
          risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
          return g_positionSizer.CalculatePercentageEquityMargin(cmd.symbol, risk_value, order_type, entry_price);
       }
       else if(vol_type_upper == "BAL_MARGIN" || vol_type_upper == "BALANCE_MARGIN")
       {
          risk_value = (cmd.risk_percent > 0) ? cmd.risk_percent : InpRisk;
          return g_positionSizer.CalculatePercentageBalanceMargin(cmd.symbol, risk_value, order_type, entry_price);
       }
       else if(vol_type_upper == "PCT_BAL_LOTS" || vol_type_upper == "BALANCE_LOTS")
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
    double risk_value = (InpVolumeType == VOLUME_LOTS) 
                        ? (cmd.lots > 0 ? cmd.lots : InpDefaultLots)
                        : (cmd.risk_percent > 0 ? cmd.risk_percent : InpRisk);
    
    return g_positionSizer.CalculateVolume(InpVolumeType, cmd.symbol, risk_value, entry_price, sl_price, order_type);
}

//===================================================================
// V7.06: CANCEL-CONFIRMATION HELPERS
//===================================================================
// After OrderDelete, the broker may take 100-1500ms to confirm. During
// that window, the order can still fill. These helpers separate the
// concern of "is the order still alive?" from "did it fill?" so callers
// can make a safe decision: cancelled -> next stage; filled -> reconcile;
// neither -> abort and log (don't risk a duplicate or orphan).

// Polls every 50ms until the order leaves the pending pool, OR transitions
// to a market position (filled - same ticket, OrderType becomes BUY/SELL).
bool WaitForOrderToLeavePool(int order_ticket, int timeout_ms)
{
   uint start = GetTickCount();
   while((int)(GetTickCount() - start) < timeout_ms)
   {
      if(IsStopped()) break;
      if(!OrderSelect(order_ticket, SELECT_BY_TICKET, MODE_TRADES))
         return true;  // gone from trades pool (deleted/cancelled)
      if(OrderType() <= OP_SELL)
         return true;  // resolved as fill (transitioned to market position)
             Sleep(CANCEL_POLL_INTERVAL_MS);
          }
   return false;
}

// Looks for a fill on order_ticket. In MT4, a freshly-filled pending order
// keeps the same ticket and stays in MODE_TRADES (OrderType BUY/SELL); only
// after close does it move to MODE_HISTORY. So we check TRADES first, then
// HISTORY for the rare case the position was already closed by TP/SL.
bool FindOrderFillInTrades(int order_ticket, double &fill_price, int &fill_ticket)
{
   if(OrderSelect(order_ticket, SELECT_BY_TICKET, MODE_TRADES) && OrderType() <= OP_SELL)
   {
      fill_price  = OrderOpenPrice();
      fill_ticket = OrderTicket();
      return true;
   }
   for(int hi = OrdersHistoryTotal() - 1; hi >= 0; hi--)
   {
      if(OrderSelect(hi, SELECT_BY_POS, MODE_HISTORY) &&
         OrderTicket() == order_ticket &&
         OrderType() <= OP_SELL)
      {
         fill_price  = OrderOpenPrice();
         fill_ticket = OrderTicket();
         return true;
      }
   }
   return false;
}

// Resolves the final state of an OrderDelete call within a timeout window.
// Returns:
//   ORDER_DELETE_RESOLUTION_CONFIRMED - order is confirmed cancelled/deleted
//   ORDER_DELETE_RESOLUTION_FILLED    - order filled before or during cancel
//   ORDER_DELETE_RESOLUTION_UNCERTAIN - timeout, could not determine fate
ENUM_ORDER_DELETE_RESOLUTION ResolveOrderDelete(int order_ticket, int timeout_ms, double &fill_price, int &fill_ticket)
{
   fill_price  = 0;
   fill_ticket = 0;

   uint start = GetTickCount();
   while((int)(GetTickCount() - start) < timeout_ms)
   {
      if(IsStopped()) break;
      // Check active trades pool first
      if(OrderSelect(order_ticket, SELECT_BY_TICKET, MODE_TRADES))
      {
         if(OrderType() <= OP_SELL)
         {
            // Pending order transitioned to market position - filled
            fill_price  = OrderOpenPrice();
            fill_ticket = OrderTicket();
            return ORDER_DELETE_RESOLUTION_FILLED;
         }
         // Still pending - order is alive, keep polling
      }
      else
      {
         // Order left the trades pool - check if it filled before leaving
         if(FindOrderFillInTrades(order_ticket, fill_price, fill_ticket))
            return ORDER_DELETE_RESOLUTION_FILLED;

         // Not in trades, not filled - confirmed cancelled/deleted
         return ORDER_DELETE_RESOLUTION_CONFIRMED;
      }

      Sleep(CANCEL_POLL_INTERVAL_MS);
   }

   return ORDER_DELETE_RESOLUTION_UNCERTAIN;
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

// Builds the broker comment prefixed with sig8:<delim>. If signal_id is
// empty, returns user_comment unchanged. Output is capped at 31 chars
// to stay within MT4's broker comment limit on common brokers.
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

// Returns true if any open trade OR pending order on `symbol` already
// carries our signal_id prefix in its comment. In MT4 both open positions
// and pending orders share MODE_TRADES, so a single loop suffices.
// Uses StringFind (substring, not prefix) so a broker that prepends
// modification text to the comment is still detected.
bool IsDuplicateSignalPosition(const string signal_id, const string symbol)
{
   if(signal_id == "" || symbol == "")
      return false;

   string sigKey = StringSubstr(signal_id, 0, SIGNAL_ID_PREFIX_LENGTH) + ":";

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() != symbol) continue;
      string cmt = OrderComment();
      if(StringFind(cmt, sigKey) >= 0)
      {
         PrintFormat("[PineTunnel] LAYER-4 DUPLICATE BLOCKED: order #%d already exists for signal %s (sigKey=%s comment='%s')",
                     OrderTicket(), signal_id, sigKey, cmt);
         return true;
      }
   }

   return false;
}

//+------------------------------------------------------------------+
//| Execute market order                                             |
//+------------------------------------------------------------------+
bool ExecuteMarketOrder(const SignalCommand &cmd)
{
   // V7.06 LAYER-4: pre-flight broker-state reconciliation
   if(IsDuplicateSignalPosition(cmd.signal_id, cmd.symbol))
      return true;  // Already handled by sibling instance - ack as success

   int position_type = (cmd.type == COMMAND_BUY) ? OP_BUY : OP_SELL;
   int opposite_type = (cmd.type == COMMAND_BUY) ? OP_SELL : OP_BUY;
   int order_type = (cmd.type == COMMAND_BUY) ? OP_BUY : OP_SELL;
   
   // Check close on reverse
   if(!ExecuteCloseOnReverse(cmd, opposite_type))
      return true;
   
   // Check pyramiding
   if(!CanOpenPositionPyramiding(cmd.symbol, position_type))
   {
      PrintFormat("[PineTunnel] Pyramiding rules blocked opening %s position on %s",
                 cmd.type == COMMAND_BUY ? "BUY" : "SELL", cmd.symbol);
      return false;
   }
   
   // Check max position limits
   if(!CheckMaxPositionLimits(cmd.symbol, position_type))
      return false;
   
   // Get price
   uint price_start = GetTickCount();
   double price = (order_type == OP_BUY) ?
                  MarketInfo(cmd.symbol, MODE_ASK) :
                  MarketInfo(cmd.symbol, MODE_BID);

   // Wait for valid price
   int retries = 0;
   while(price <= 0 && retries < 10)
   {
      if(IsStopped()) break;
      Sleep(300);
      RefreshRates();
      price = (order_type == OP_BUY) ?
              MarketInfo(cmd.symbol, MODE_ASK) :
              MarketInfo(cmd.symbol, MODE_BID);
      retries++;
   }

   PrintFormat("[Exec] [TIMING] Price fetch: %ums | retries=%d | Sym=%s", GetTickCount() - price_start, retries, cmd.symbol);

   if(price <= 0)
   {
      PrintFormat("[PineTunnel] ERROR: Unable to get prices for %s after %d retries (%.1f seconds)",
                  cmd.symbol, retries, (retries * 300) / 1000.0);
      PrintFormat("[PineTunnel] Market may be closed or symbol not available");
      return false;
   }
   
   PrintFormat("[PineTunnel] Entry price: %.5f | SL value: %.5f | TP value: %.5f", price, cmd.stop_loss, cmd.take_profit);
   
   bool is_buy = (cmd.type == COMMAND_BUY);
   
    // Calculate SL/TP
    double sl_price = CalculateSLPriceWithExplicitType(cmd, price, is_buy);
    double tp_price = CalculateTPPriceWithExplicitType(cmd, price, is_buy);
    
    // Abort if SL was requested but calculation failed
    if(cmd.stop_loss > 0 && sl_price == 0)
    {
       PrintFormat("[PineTunnel] ERROR: SL calculation failed for %s (requested SL=%.5f). Aborting order.", cmd.symbol, cmd.stop_loss);
       return false;
    }
    
    // Calculate volume
   double lots = CalculateVolumeWithExplicitType(cmd, price, sl_price, order_type);
   
   if(lots <= 0)
   {
      PrintFormat("[PineTunnel] Volume calculation failed (lots=%.4f). ABORTING order.", lots);
      return false;
   }
   
   // Normalize prices
   int digits = (int)MarketInfo(cmd.symbol, MODE_DIGITS);
   double pt = MarketInfo(cmd.symbol, MODE_POINT);
   if(sl_price > 0) sl_price = NormalizeDouble(sl_price, digits);
    if(tp_price > 0) tp_price = NormalizeDouble(tp_price, digits);
    
     // Validate and auto-adjust SL/TP to broker minimum stops level
     int stopLevelPoints = (int)MarketInfo(cmd.symbol, MODE_STOPLEVEL);
     double stopLevel = stopLevelPoints * pt;
     if(sl_price > 0 && stopLevel > 0)
     {
        double slDistance = MathAbs(price - sl_price);
        if(slDistance < stopLevel)
        {
           sl_price = is_buy ? price - stopLevel : price + stopLevel;
           sl_price = NormalizeDouble(sl_price, digits);
           PrintFormat("[PineTunnel] SL adjusted to broker min distance: %.5f (was %.5f, min %d points)",
                       sl_price, slDistance, stopLevelPoints);
        }
     }
     if(tp_price > 0 && stopLevel > 0)
     {
        double tpDistance = MathAbs(price - tp_price);
        if(tpDistance < stopLevel)
        {
           tp_price = is_buy ? price + stopLevel : price - stopLevel;
           tp_price = NormalizeDouble(tp_price, digits);
           PrintFormat("[PineTunnel] TP adjusted to broker min distance: %.5f (was %.5f, min %d points)",
                       tp_price, tpDistance, stopLevelPoints);
        }
     }
   
   // Hidden targets handling
   double broker_sl = sl_price;
   double broker_tp = tp_price;
   
    if(InpHiddenSLTP == HIDDEN_ON)
    {
       // Validate hidden offset (PineTunnel spec: typically 100+ pips)
       double hidden_offset = InpHiddenOffset * pt;
       
       if(sl_price > 0)
       {
          if(order_type == OP_BUY)
             broker_sl = sl_price - hidden_offset;
          else
             broker_sl = sl_price + hidden_offset;
       }
       
       if(tp_price > 0)
       {
          if(order_type == OP_BUY)
             broker_tp = tp_price + hidden_offset;
          else
             broker_tp = tp_price - hidden_offset;
       }
       
       PrintFormat("[PineTunnel] Hidden SL/TPs ENABLED - Offset: %.0f pips", InpHiddenOffset);
       if(broker_sl > 0)
          PrintFormat("[PineTunnel] Broker SL: %.5f (Intended: %.5f | Offset: %.5f)",
                     broker_sl, sl_price, MathAbs(broker_sl - sl_price));
       if(broker_tp > 0)
          PrintFormat("[PineTunnel] Broker TP: %.5f (Intended: %.5f | Offset: %.5f)",
                     broker_tp, tp_price, MathAbs(broker_tp - tp_price));
     }
   
   // NM flag: extracted from nm=true parameter
   string cleanComment = cmd.comment;
   bool   convertToNearMarket = InpEnableSmartMarket && cmd.nm;
   if(convertToNearMarket)
   {
      // nm flag from signal parameter
      PrintFormat("[PineTunnel] Auto order detected - converting to near-market limit for price improvement");
   }

   // V7.06 LAYER-4: prefix signal_id into the broker comment so a sibling
   // EA's pre-flight scan can detect this position and short-circuit.
   cleanComment = BuildOrderComment(cmd.signal_id, cleanComment);
   
   double exec_price = price;
   int    ticket     = -1;
   // Set true if a pending-order cancel does NOT confirm before we would place
   // the next stage. Aborting prevents the duplicate-fill race where the prior
   // order is still live in the broker's book while we send a fresh limit.
   bool   nmAborted  = false;
   
    if(convertToNearMarket)
    {
       double point      = pt;
       int    stopsLevel = (int)MarketInfo(cmd.symbol, MODE_STOPLEVEL);
       int    minValidPoints = (stopsLevel > 0) ? stopsLevel + 1 : 1;
       
       // ===================================================================
       // NEAR-MARKET LIMIT: Single-stage with polling
       // ===================================================================
       if(ticket < 0 && !nmAborted)
       {
          RefreshRates();
          double ask = MarketInfo(cmd.symbol, MODE_ASK);
          double bid = MarketInfo(cmd.symbol, MODE_BID);
          int    limit_op;
          double limit_price;
          
          if(order_type == OP_BUY)
          {
             limit_price = NormalizeDouble(ask - (minValidPoints * point), digits);
             limit_op    = OP_BUYLIMIT;
             PrintFormat("[PineTunnel] NM Buy Limit: %.5f (Ask:%.5f offset:%d pts timeout:%dms)",
                         limit_price, ask, minValidPoints, InpEntryLimitTimeoutMs);
          }
          else
          {
             limit_price = NormalizeDouble(bid + (minValidPoints * point), digits);
             limit_op    = OP_SELLLIMIT;
             PrintFormat("[PineTunnel] NM Sell Limit: %.5f (Bid:%.5f offset:%d pts timeout:%dms)",
                         limit_price, bid, minValidPoints, InpEntryLimitTimeoutMs);
          }
          
          bool skipLimit = false;
          if(g_limitOrderValidator != NULL)
          {
             if(!g_limitOrderValidator.ValidateLimitPrice(cmd.symbol, limit_price, limit_op))
             {
                skipLimit = true;
                PrintFormat("[PineTunnel] NM limit validation failed - falling back to market");
             }
          }
          
          if(!skipLimit)
          {
             uint os_start = GetTickCount();
             int limit_ticket = OrderSend(
               cmd.symbol, limit_op, lots, limit_price,
               0, broker_sl, broker_tp, cleanComment,
               g_magicNumber, 0, clrYellow);
             uint os_ms = GetTickCount() - os_start;

             if(limit_ticket < 0)
             {
                int err = GetLastError();
                g_lastExecError = err;
                PrintFormat("[PineTunnel] NM limit placement failed: %d - %s", err, ErrorDescription(err));
                PrintFormat("[Exec] [TIMING] NM OrderSend FAIL: %ums | retcode=%d | Sym=%s Price=%.5f", os_ms, err, cmd.symbol, limit_price);
                PrintFormat("[PineTunnel] [DIAG] Sym=%s Price=%.5f Type=%s Lots=%.2f SL=%.5f TP=%.5f Bid=%.5f Ask=%.5f StopsLvl=%d",
                            cmd.symbol, limit_price, (limit_op==OP_BUYLIMIT?"BUY_LIMIT":"SELL_LIMIT"),
                            lots, broker_sl, broker_tp, bid, ask, stopsLevel);
             }
             else
             {
                PrintFormat("[PineTunnel] NM limit placed #%d - waiting up to %dms...",
                            limit_ticket, InpEntryLimitTimeoutMs);
                PrintFormat("[Exec] [TIMING] NM OrderSend OK: %ums | ticket=#%d | offset=%dpts | Sym=%s", os_ms, limit_ticket, minValidPoints, cmd.symbol);
                
                uint poll_start = GetTickCount();
                bool filled     = false;
                bool cancelled  = false;
                
                while((int)(GetTickCount() - poll_start) < InpEntryLimitTimeoutMs)
                {
                   if(IsStopped()) break;
                   if(OrderSelect(limit_ticket, SELECT_BY_TICKET, MODE_TRADES))
                   {
                      if(OrderType() == OP_BUY || OrderType() == OP_SELL)
                      {
                         exec_price = OrderOpenPrice();
                         ticket     = limit_ticket;
                         filled     = true;
                         PrintFormat("[PineTunnel] NM filled @ %.5f (ticket #%d)", exec_price, ticket);
                         PrintFormat("[Exec] [TIMING] NM fill detected: %ums after placement | ticket=#%d | price=%.5f", GetTickCount() - poll_start, limit_ticket, exec_price);
                         break;
                      }
                   }
                   else
                   {
                      cancelled = true;
                      PrintFormat("[PineTunnel] NM order #%d cancelled by broker - not filled", limit_ticket);
                      break;
                   }
                   Sleep(NM_POLL_INTERVAL_MS);
                }

                if(!filled && !cancelled)
                   {
                   PrintFormat("[PineTunnel] NM timeout after %dms (actual_elapsed=%dms) - cancelling #%d",
                               InpEntryLimitTimeoutMs, (int)(GetTickCount() - poll_start), limit_ticket);
                   PrintFormat("[Exec] [TIMING] NM poll timeout: %ums total | offset=%dpts | Sym=%s", GetTickCount() - poll_start, minValidPoints, cmd.symbol);
                   uint cancel_start = GetTickCount();
                   if(OrderSelect(limit_ticket, SELECT_BY_TICKET, MODE_TRADES) && OrderType() > OP_SELL)
                      OrderDelete(limit_ticket);

                   double late_price  = 0;
                   int    late_ticket = 0;
                   uint resolve_start = GetTickCount();
                   ENUM_ORDER_DELETE_RESOLUTION cancel_resolution = ResolveOrderDelete(limit_ticket, NM_DELETE_CONFIRM_TIMEOUT_MS, late_price, late_ticket);
                   PrintFormat("[PineTunnel] [DIAG] NM cancel resolution: %s in %dms (order=#%d)",
                               cancel_resolution == ORDER_DELETE_RESOLUTION_CONFIRMED ? "CONFIRMED" :
                               cancel_resolution == ORDER_DELETE_RESOLUTION_FILLED ? "FILLED" : "UNCERTAIN",
                               (int)(GetTickCount() - resolve_start), limit_ticket);
                   if(cancel_resolution == ORDER_DELETE_RESOLUTION_FILLED)
                   {
                      ticket     = late_ticket;
                      exec_price = (late_price > 0) ? late_price : exec_price;
                      PrintFormat("[PineTunnel] NM filled during cancel race @ %.5f (ticket #%d)",
                                  exec_price, ticket);
                      PrintFormat("[Exec] [TIMING] NM cancel-race fill: %ums cancel total | Sym=%s", GetTickCount() - cancel_start, cmd.symbol);
                   }
                   else if(cancel_resolution != ORDER_DELETE_RESOLUTION_CONFIRMED)
                   {
                      nmAborted = true;
                      PrintFormat("[PineTunnel] ABORT: NM order #%d cancel not confirmed within %dms - skipping market fallback.",
                                  limit_ticket, NM_DELETE_CONFIRM_TIMEOUT_MS);
                   }
                   else
                   {
                      PrintFormat("[PineTunnel] [DIAG] NM cancel confirmed - falling through to market fallback (order=#%d)", limit_ticket);
                      PrintFormat("[Exec] [TIMING] NM cancel+resolve: %ums | Sym=%s", GetTickCount() - cancel_start, cmd.symbol);
                   }
                }
             }
          }
          
          PrintFormat("[PineTunnel] [DIAG] NM entry result: aborted=%s filled=%s price=%.5f ticket=#%d lots=%.2f sym=%s",
                      nmAborted ? "yes" : "no", (ticket > 0) ? "YES" : "no",
                      exec_price, ticket, lots, cmd.symbol);
       }
       
       // --- Market fallback with FRESH prices ---
       if(nmAborted)
       {
          PrintFormat("[PineTunnel] Skipping market fallback - entry aborted due to unconfirmed cancel (see ABORT above)");
       }
       else if(ticket < 0)
       {
          RefreshRates();
          double fresh_price = (order_type == OP_BUY) ?
                               MarketInfo(cmd.symbol, MODE_ASK) :
                               MarketInfo(cmd.symbol, MODE_BID);
          
           PrintFormat("[PineTunnel] Market fallback @ %.5f (Ask=%.5f Bid=%.5f)", fresh_price,
                       MarketInfo(cmd.symbol, MODE_ASK), MarketInfo(cmd.symbol, MODE_BID));
          uint mkt_start = GetTickCount();
          ticket = OrderSend(
             cmd.symbol, order_type, lots, fresh_price,
             InpMaxSlippage, broker_sl, broker_tp, cleanComment,
             g_magicNumber, 0, order_type == OP_BUY ? clrBlue : clrRed);
          uint mkt_ms = GetTickCount() - mkt_start;
          
          if(ticket > 0)
          {
             exec_price = fresh_price;
             PrintFormat("[PineTunnel] Market order executed as fallback (ticket #%d)", ticket);
             PrintFormat("[Exec] [TIMING] Market fallback OrderSend: %ums | retcode=%d | price=%.5f | Sym=%s", mkt_ms, 0, fresh_price, cmd.symbol);
          }
          else
          {
              int err = GetLastError();
              g_lastExecError = err;
              PrintFormat("[PineTunnel] Market fallback also failed: %d - %s", err, ErrorDescription(err));
              PrintFormat("[Exec] [TIMING] Market fallback FAILED: %ums | retcode=%d | Sym=%s", mkt_ms, err, cmd.symbol);
              PrintFormat("[PineTunnel] [DIAG] Market fail: Sym=%s Type=%s Lots=%.2f Price=%.5f SL=%.5f TP=%.5f",
                          cmd.symbol, CommandName(cmd.type), lots, fresh_price, broker_sl, broker_tp);
              PrintFormat("[PineTunnel] [DIAG] Account: Balance=%.2f Equity=%.2f FreeMargin=%.2f Chart=%s",
                          AccountBalance(), AccountEquity(), AccountFreeMargin(), Symbol());
              if(err == 4109)
              {
                 PrintFormat("[PineTunnel] Trading not allowed on symbol %s", cmd.symbol);
                 PrintFormat("[PineTunnel] Note: This is likely a simulation symbol with trading restrictions");
              }
          }
       }
    }
   else
   {
      // Standard market order (no NM flag) with retry + exponential backoff
      // Parity with MT5 COrderExecutionManager: 5 retries, 200ms initial delay, 5000ms max
      int maxRetries = 5;
      int initialDelayMs = 200;
      int maxDelayMs = 5000;
      uint direct_start = GetTickCount();

      double free_margin_after = AccountFreeMarginCheck(cmd.symbol, order_type, lots);
      if(free_margin_after <= 0)
      {
         PrintFormat("[PineTunnel] Insufficient margin for %.2f lots of %s (free margin after: %.2f). ABORTING.", lots, cmd.symbol, free_margin_after);
         return false;
      }

      for(int attempt = 0; attempt < maxRetries; attempt++)
      {
         RefreshRates();
         price = (order_type == OP_BUY) ? MarketInfo(cmd.symbol, MODE_ASK) : MarketInfo(cmd.symbol, MODE_BID);
         price = NormalizeDouble(price, digits);

         ticket = OrderSend(
            cmd.symbol, order_type, lots, price,
            InpMaxSlippage, broker_sl, broker_tp, cleanComment,
            g_magicNumber, 0, order_type == OP_BUY ? clrBlue : clrRed);
         exec_price = price;

         if(ticket > 0)
         {
             PrintFormat("[Exec] [TIMING] Direct market order: %ums | retcode=%d | Sym=%s",
                        GetTickCount() - direct_start, 0, cmd.symbol);
            break;
         }

         int error = GetLastError();

         // Check if retryable
         bool retryable = (error == 128 || // Trade timeout
                           error == 135 || // Price changed
                           error == 136 || // Off quotes
                           error == 138 || // Requote
                           error == 146);  // Trade context busy

         if(!retryable || attempt >= maxRetries - 1)
         {
             PrintFormat("[Exec] [TIMING] Direct market order FAILED: %ums | retcode=%d | Sym=%s",
                        GetTickCount() - direct_start, error, cmd.symbol);
            break;
         }

         int delay = MathMin(initialDelayMs * (int)MathPow(2, attempt), maxDelayMs);
         PrintFormat("[Exec] Order failed (%d - %s) - retry %d/%d in %dms",
                     error, ErrorDescription(error), attempt + 1, maxRetries, delay);
         Sleep(delay);
      }
   }
   
   if(ticket > 0)
   {
      string sl_tp_info = "";
      if(sl_price > 0) sl_tp_info += StringFormat(" SL:%.5f", sl_price);
      if(tp_price > 0) sl_tp_info += StringFormat(" TP:%.5f", tp_price);
      
       PrintFormat("[PineTunnel] %s %.2f %s @ %.5f%s executed successfully",
                   CommandName(cmd.type), lots, cmd.symbol, exec_price, sl_tp_info);
      
      SendTradeReport(CommandName(cmd.type), cmd.symbol, lots, exec_price, ticket, true, "", cmd.signal_id);

      // Map ticket -> signal_id for close reports
      SaveTicketSignal(ticket, cmd.signal_id);

      // Add hidden target
      if(InpHiddenSLTP == HIDDEN_ON && (sl_price > 0 || tp_price > 0))
      {
         AddHiddenTarget(ticket, cmd.symbol, position_type, sl_price, tp_price, exec_price);
         PrintFormat("[PineTunnel] Hidden targets registered for ticket #%d", ticket);
      }
      
      // V7.00: Verify SL/TP was properly set (only if enabled and not using hidden targets)
      // Use broker_sl/broker_tp (not original sl_price/tp_price) to respect hidden target offset
      if(InpEnableSLTPVerify && InpHiddenSLTP == HIDDEN_OFF && ticket > 0)
      {
         VerifyAndFixSLTP(ticket, cmd.symbol, broker_sl, broker_tp, InpSLTPVerifyRetries);
      }
      
      return true;
   }
   else
   {
       int error = GetLastError();
       g_lastExecError = error;
       string error_msg = StringFormat("%d - %s", error, ErrorDescription(error));
       
       // If error=0, validation failed before broker call - explain why
       if(error == 0)
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
          error_msg = StringFormat("%s | Entry=%.5f SL=%.5f TP=%.5f Lots=%.2f | %s",
              error_msg, price, broker_sl, broker_tp, lots, cmd.symbol);
       }
        PrintFormat("[PineTunnel] %s order failed: %s", CommandName(cmd.type), error_msg);
       
       // V7.04: Comprehensive diagnostics on any trade failure
       PrintFormat("[PineTunnel] [DIAG] Trade failure dump: Cmd=%s | Symbol=%s | Lots=%.2f | Price=%.5f | SL=%.5f | TP=%.5f | Chart=%s",
                   CommandName(cmd.type), cmd.symbol, lots, price, sl_price, tp_price, Symbol());
       PrintFormat("[PineTunnel] [DIAG] Broker SL=%.5f | Broker TP=%.5f | NearMarket=%s | Comment=%s",
                   broker_sl, broker_tp, (convertToNearMarket ? "YES" : "NO"), cmd.comment);
       PrintFormat("[PineTunnel] [DIAG] Account: Balance=%.2f | Equity=%.2f | FreeMargin=%.2f | Leverage=%d",
                   AccountBalance(), AccountEquity(), AccountFreeMargin(), AccountLeverage());

       if(error == 4109)
       {
          PrintFormat("[PineTunnel] Trading not allowed on symbol %s", cmd.symbol);
          PrintFormat("[PineTunnel] Note: This is likely a simulation symbol with trading restrictions");
       }

       SendTradeReport(CommandName(cmd.type), cmd.symbol, lots, price, 0, false, error_msg, cmd.signal_id);
      return false;
   }
}

//+------------------------------------------------------------------+
//| Execute pending order                                            |
//+------------------------------------------------------------------+
bool ExecutePendingOrder(const SignalCommand &cmd)
{
   // V7.06 LAYER-4: pre-flight broker-state reconciliation (see ExecuteMarketOrder for rationale)
   if(IsDuplicateSignalPosition(cmd.signal_id, cmd.symbol))
      return true;  // Already handled by sibling instance - ack as success

   if(!cmd.has_pending)
   {
      PrintFormat("[PineTunnel] ERROR: Pending order %s requires pending= parameter", CommandName(cmd.type));
      return false;
   }
   
   if(g_pendingManager == NULL)
   {
      PrintFormat("[PineTunnel] ERROR: Pending order manager not initialized");
      return false;
   }
   
   double lots = InpDefaultLots;
   
   // Get current prices
   double ask = MarketInfo(cmd.symbol, MODE_ASK);
   double bid = MarketInfo(cmd.symbol, MODE_BID);
   double point = MarketInfo(cmd.symbol, MODE_POINT);
   
   // Wait for valid prices
   int retries = 0;
   while((ask <= 0 || bid <= 0) && retries < 10)
   {
      if(IsStopped()) break;
      Sleep(300);
      RefreshRates();
      ask = MarketInfo(cmd.symbol, MODE_ASK);
      bid = MarketInfo(cmd.symbol, MODE_BID);
      retries++;
   }
   
   if(ask <= 0 || bid <= 0)
   {
      PrintFormat("[PineTunnel] ERROR: Invalid prices for %s", cmd.symbol);
      return false;
   }
   
    // Calculate pending entry price
    double current_price = 0;
    double pending_entry_price = 0;
    bool is_buy = false;
    int pending_order_type = OP_BUY;
    
     switch(cmd.type)
     {
      case COMMAND_BUY_LIMIT:
         current_price = ask;
         is_buy = true;
         pending_order_type = OP_BUYLIMIT;
         break;
      case COMMAND_BUY_STOP:
         current_price = ask;
         is_buy = true;
         pending_order_type = OP_BUYSTOP;
         break;
      case COMMAND_SELL_LIMIT:
         current_price = bid;
         is_buy = false;
         pending_order_type = OP_SELLLIMIT;
         break;
      case COMMAND_SELL_STOP:
         current_price = bid;
         is_buy = false;
         pending_order_type = OP_SELLSTOP;
         break;
      default:
         return false;
    }
   
    // Calculate entry based on entry type
    // Priority: explicit entry_type from signal > EA PendingOrderEntry setting
    string effective_entry_type = cmd.entry_type;
    if(effective_entry_type == "" || effective_entry_type == NULL)
    {
       // Fall back to EA PendingOrderEntry setting
       if(InpPendingOrderEntry == PENDING_PIPS_FROM_MARKET)
          effective_entry_type = "pips";
       else if(InpPendingOrderEntry == PENDING_PRICE_FROM_SIGNAL)
          effective_entry_type = "price";
       else if(InpPendingOrderEntry == PENDING_PERCENT_FROM_MARKET)
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
   
    // Calculate volume using explicit type support (parity with MT5)
    double sl_price = CalculateSLPriceWithExplicitType(cmd, pending_entry_price, is_buy);
    double tp_price = CalculateTPPriceWithExplicitType(cmd, pending_entry_price, is_buy);
    
     if(g_positionSizer != NULL)
     {
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
     else if(cmd.lots > 0)
     {
        lots = cmd.lots;
     }
    
    double stop_level = MarketInfo(cmd.symbol, MODE_STOPLEVEL) * point;
    if(stop_level > 0)
    {
       if(sl_price > 0)
       {
          double sl_dist = MathAbs(pending_entry_price - sl_price);
          if(sl_dist < stop_level)
             sl_price = is_buy ? pending_entry_price - stop_level : pending_entry_price + stop_level;
       }
       if(tp_price > 0)
       {
          double tp_dist = MathAbs(pending_entry_price - tp_price);
          if(tp_dist < stop_level)
             tp_price = is_buy ? pending_entry_price + stop_level : pending_entry_price - stop_level;
       }
    }
    
    // Execute based on command type
    bool result = false;
    
    // Convert SL/TP prices to pips for PendingOrders interface (parity with MT5)
    double sl_points = 0;
    double tp_points = 0;
    
    if(sl_price > 0 && point > 0)
    {
       double sl_distance = MathAbs(pending_entry_price - sl_price);
       sl_points = sl_distance / point / 10.0;
    }
    
    if(tp_price > 0 && point > 0)
    {
       double tp_distance = MathAbs(pending_entry_price - tp_price);
       tp_points = tp_distance / point / 10.0;
    }
    
    // V7.06 LAYER-4: prefix signal_id into the broker comment
    string pendingComment = BuildOrderComment(cmd.signal_id, cmd.comment);

    switch(cmd.type)
    {
       case COMMAND_BUY_LIMIT:
          result = g_pendingManager.PlaceBuyLimit(cmd.symbol, lots, cmd.pending_distance, 
                                                   InpPendingType, sl_points, tp_points, pendingComment);
          break;
       case COMMAND_SELL_LIMIT:
          result = g_pendingManager.PlaceSellLimit(cmd.symbol, lots, cmd.pending_distance,
                                                    InpPendingType, sl_points, tp_points, pendingComment);
          break;
       case COMMAND_BUY_STOP:
          result = g_pendingManager.PlaceBuyStop(cmd.symbol, lots, cmd.pending_distance,
                                                  InpPendingType, sl_points, tp_points, pendingComment);
          break;
       case COMMAND_SELL_STOP:
          result = g_pendingManager.PlaceSellStop(cmd.symbol, lots, cmd.pending_distance,
                                                   InpPendingType, sl_points, tp_points, pendingComment);
          break;
    }
   
   if(result)
   {
      PrintFormat("[PineTunnel] %s pending order placed: %.2f %s @ %.5f",
                 CommandName(cmd.type), lots, cmd.symbol, pending_entry_price);
      SendTradeReport(CommandName(cmd.type), cmd.symbol, lots, pending_entry_price, 0, true, "", cmd.signal_id);
   }
   else
   {
      int error = GetLastError();
      g_lastExecError = error;
      string error_msg = StringFormat("%d - %s", error, ErrorDescription(error));
      PrintFormat("[PineTunnel] %s pending order failed: %s", CommandName(cmd.type), error_msg);
      // V7.04: [DIAG] logging for pending order failures
      PrintFormat("[PineTunnel] [DIAG] ExecutePendingOrder FAILED | Symbol: %s | Type: %s | Lots: %.2f",
                 cmd.symbol, CommandName(cmd.type), lots);
      PrintFormat("[PineTunnel] [DIAG] EntryPrice: %.5f | SL: %.5f | TP: %.5f | Distance: %.5f",
                 pending_entry_price, cmd.stop_loss, cmd.take_profit, cmd.pending_distance);
      PrintFormat("[PineTunnel] [DIAG] Chart: %s | Account: %d | Magic: %d | Error: %d (%s)",
                 Symbol(), AccountNumber(), g_magicNumber, error, ErrorDescription(error));
      SendTradeReport(CommandName(cmd.type), cmd.symbol, lots, pending_entry_price, 0, false, error_msg, cmd.signal_id);
   }
   
   return result;
}

//+------------------------------------------------------------------+
//| Check account protection                                         |
//+------------------------------------------------------------------+
bool CheckAccountProtection()
{
   CheckDailyReset();
   
   if(g_protectionHalted || g_dailyHalted)
      return false;
   
   if(InpDailyProfit <= 0 && InpDailyLoss <= 0 && 
      InpCumulativeProfit <= 0 && InpCumulativeLoss <= 0)
      return true;
   
   double current_balance = AccountBalance();
   double current_equity = AccountEquity();
   
   // Check daily profit
   if(InpDailyProfit > 0)
   {
      double daily_pnl = current_equity - g_dailyStartBalance;
      double target = (InpDailyProfit <= 1.0) ? 
                      (g_dailyStartBalance * InpDailyProfit) : InpDailyProfit;
      
      if(daily_pnl >= target)
      {
         ExecuteProtectionAction(true, InpAction1, 
                                 StringFormat("Daily Profit Target reached: $%.2f / $%.2f", daily_pnl, target));
         return false;
      }
   }
   
   // Check daily loss
   if(InpDailyLoss > 0)
   {
      double daily_pnl = current_equity - g_dailyStartBalance;
      double limit = (InpDailyLoss <= 1.0) ? 
                     -(g_dailyStartBalance * InpDailyLoss) : -InpDailyLoss;
      
      if(daily_pnl <= limit)
      {
         ExecuteProtectionAction(true, InpAction1,
                                 StringFormat("Daily Loss Limit reached: $%.2f / $%.2f", daily_pnl, limit));
         return false;
      }
   }
   
   // Check cumulative profit
   if(InpCumulativeProfit > 0)
   {
      double cum_pnl = current_equity - g_cumulativeStartBalance;
      double target = (InpCumulativeProfit <= 1.0) ?
                      (g_cumulativeStartBalance * InpCumulativeProfit) : InpCumulativeProfit;
      
      if(cum_pnl >= target)
      {
         ExecuteProtectionAction(false, InpAction2,
                                 StringFormat("Cumulative Profit Target reached: $%.2f / $%.2f", cum_pnl, target));
         return false;
      }
   }
   
   // Check cumulative loss
   if(InpCumulativeLoss > 0)
   {
      double cum_pnl = current_equity - g_cumulativeStartBalance;
      double limit = (InpCumulativeLoss <= 1.0) ?
                     -(g_cumulativeStartBalance * InpCumulativeLoss) : -InpCumulativeLoss;
      
      if(cum_pnl <= limit)
      {
         ExecuteProtectionAction(false, InpAction2,
                                 StringFormat("Cumulative Loss Limit reached: $%.2f / $%.2f", cum_pnl, limit));
         return false;
      }
   }
   
   return true;
}

//+------------------------------------------------------------------+
//| Execute protection action                                        |
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
      if(should_halt) g_dailyHalted = true;
   }
   else
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
   
   if(should_close)
   {
      PrintFormat("[PineTunnel] Closing all positions...");
      if(!ClosePositions("", -1, ""))
         PrintFormat("[PineTunnel] CRITICAL: Close-all FAILED - positions remain open!");
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
//| Check active hours                                               |
//+------------------------------------------------------------------+
bool CheckActiveHours()
{
   if(InpStartTime == "00:00" && InpEndTime == "23:59")
      return true;
   
   datetime current = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(current, dt);
   
   string start_parts[], end_parts[];
   int start_count = StringSplit(InpStartTime, ':', start_parts);
   int end_count = StringSplit(InpEndTime, ':', end_parts);

   if(start_count != 2 || end_count != 2)
   {
      Print("[PineTunnel] ERROR: Invalid time format. Use HH:MM");
      return true;  // Default to allowing trading if format error
   }
   
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

   int current_mins = dt.hour * 60 + dt.min;
   int start_mins = start_hour * 60 + start_min;
   int end_mins = end_hour * 60 + end_min;
   
   bool is_active = (start_mins <= end_mins) 
                  ? (current_mins >= start_mins && current_mins <= end_mins)
                  : (current_mins >= start_mins || current_mins <= end_mins);
   
   if(!is_active)
      PrintFormat("[PineTunnel] Outside active hours (%s - %s). Current: %02d:%02d",
                  InpStartTime, InpEndTime, dt.hour, dt.min);
   
   return is_active;
}

//+------------------------------------------------------------------+
//| Error description                                                |
//+------------------------------------------------------------------+
string ErrorDescription(int error_code)
{
   switch(error_code)
   {
      case 0:    return "No error";
      case 1:    return "No error but result unknown";
      case 9:    return "Malfunctional trade operation";
      case 2:    return "Common error";
      case 3:    return "Invalid trade parameters";
      case 4:    return "Trade server is busy";
      case 5:    return "Old client terminal version";
      case 6:    return "No connection with trade server";
      case 7:    return "Not enough rights";
      case 8:    return "Too frequent requests";
      case 64:   return "Account disabled";
      case 65:   return "Invalid account";
      case 128:  return "Trade timeout";
      case 129:  return "Invalid price";
      case 130:  return "Invalid stops";
      case 131:  return "Invalid trade volume";
      case 132:  return "Market closed";
      case 133:  return "Trade disabled";
      case 134:  return "Not enough money";
      case 135:  return "Price changed";
      case 136:  return "Off quotes";
      case 137:  return "Broker busy";
      case 138:  return "Requote";
      case 139:  return "Order locked";
      case 140:  return "Long positions only";
      case 141:  return "Too many requests";
      case 145:  return "Modification denied";
      case 146:  return "Trade context busy";
      case 147: return "Expirations denied";
      case 148: return "Too many orders";
      case 149: return "Hedging prohibited";
      case 150: return "FIFO rule violation";
      case 4060: return "WebRequest not allowed";
      default:   return "Error " + IntegerToString(error_code);
   }
}

//+------------------------------------------------------------------+
//| Execute command                                                  |
//+------------------------------------------------------------------+
bool ExecuteCommand(const SignalCommand &command, bool from_queue = false)
{
   uint exec_start_tick = GetTickCount();
   g_lastExecError = 0;  // reset before each execution attempt

   // Check for duplicate signal
   if(IsSignalDuplicate(command.signal_id))
   {
       PrintFormat("[PineTunnel] DUPLICATE SIGNAL BLOCKED | ID: %s | Chart: %s", command.signal_id, Symbol());
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
                        command.signal_id, Symbol(), lockOwner);
         else
            PrintFormat("[PineTunnel] SIGNAL LOCKED by another instance - SKIPPED | ID: %s | Chart: %s | Lock Owner: (unreadable)",
                        command.signal_id, Symbol());
         return true;  // Another instance is handling this signal
      }
      // Create lock file immediately before execution
      int lockHandle = FileOpen(lockFile, FILE_WRITE|FILE_TXT|FILE_ANSI);
      if(lockHandle != INVALID_HANDLE)
      {
         FileWriteString(lockHandle, Symbol() + "_" + IntegerToString(ChartID()) + "|" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS));
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
   PrintFormat("[Exec] [TIMING] Signal #%d receipt tick=%u", g_totalSignals, GetTickCount());
   
   // Check if EA is enabled
   if(!g_eaEnabled && command.type != COMMAND_EA_ON)
   {
      PrintFormat("[PineTunnel] EA is DISABLED - Ignoring signal");
      return false;
   }
   
   // Check active hours
   if(command.type != COMMAND_EA_ON && command.type != COMMAND_EA_OFF && 
      command.type != COMMAND_CLOSEALL_EA_OFF)
   {
      if(!CheckActiveHours())
         return false;
   }
   
   // Check account protection
   if(command.type != COMMAND_EA_ON && command.type != COMMAND_EA_OFF &&
      command.type != COMMAND_CLOSE_LONG && command.type != COMMAND_CLOSE_SHORT &&
      command.type != COMMAND_CLOSE_LONG_SHORT && command.type != COMMAND_EXIT)
   {
      if(!CheckAccountProtection())
         return false;
   }
   
   // Check account filter
   if(command.account_filter > 0)
   {
      double account_value = 0;
      string filter_name = "";
      
      switch(InpAccountFilterBasis)
      {
         case ACCOUNT_BASIS_BALANCE:
            account_value = AccountBalance();
            filter_name = "Balance";
            break;
         case ACCOUNT_BASIS_EQUITY:
            account_value = AccountEquity();
            filter_name = "Equity";
            break;
         case ACCOUNT_BASIS_FREE_MARGIN:
            account_value = AccountFreeMargin();
            filter_name = "Free Margin";
            break;
         case ACCOUNT_BASIS_MARGIN_PERCENTAGE:
            {
               double margin = AccountMargin();
               if(margin > 0)
                  account_value = (AccountEquity() / margin) * 100;
               else
                  account_value = 999999.0;
               filter_name = "Margin Percentage";
            }
            break;
      }
      
      if(account_value <= command.account_filter)
      {
         PrintFormat("[PineTunnel] Account filter NOT met - %s: %.2f <= %.2f",
                    filter_name, account_value, command.account_filter);
         return false;
      }
   }
   
   if(command.use_risk_sizing)
      PrintFormat("[PineTunnel] Risk: %.2f%% | SL: %.5f | TP: %.5f", 
                 command.risk_percent, command.stop_loss, command.take_profit);
   else
      PrintFormat("[PineTunnel] Lots: %.2f | SL: %.5f | TP: %.5f", 
                 command.lots, command.stop_loss, command.take_profit);
   
   bool success = false;
   
   switch(command.type)
   {
      case COMMAND_BUY:
      case COMMAND_SELL:
         success = ExecuteMarketOrder(command);
         break;
         
      case COMMAND_BUY_LIMIT:
      case COMMAND_SELL_LIMIT:
      case COMMAND_BUY_STOP:
      case COMMAND_SELL_STOP:
         success = ExecutePendingOrder(command);
         break;
         
       case COMMAND_CANCEL_LONG:
          success = (g_pendingManager != NULL) ? (g_pendingManager.CancelBuyOrders(command.symbol, command.comment) > 0) : false;
          break;
          
       case COMMAND_CANCEL_SHORT:
          success = (g_pendingManager != NULL) ? (g_pendingManager.CancelSellOrders(command.symbol, command.comment) > 0) : false;
          break;
         
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
         
      case COMMAND_SLTP__LONG:
         if(g_modifyManager != NULL)
         {
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1; // -1 = no SL specified
            success = g_modifyManager.ModifyLongPositions(command.symbol, mod_sl, command.take_profit, command.comment);
         }
         break;
         
      case COMMAND_SLTP__SHORT:
         if(g_modifyManager != NULL)
         {
            double mod_sl = command.stop_loss;
            if(!command.has_sl && mod_sl == 0) mod_sl = -1;
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
         
       case COMMAND_CLOSE_ALL:
           // PineTunnel spec: closeall requires symbol == ChartSymbol as safety measure
           if(command.symbol != Symbol() && command.symbol != "ea_off" && command.symbol != "ea_on")
           {
              PrintFormat("[PineTunnel] CLOSEALL rejected: signal symbol '%s' != chart symbol '%s' (PineTunnel spec: must match chart)", command.symbol, Symbol());
              break;
           }
           Print("[PineTunnel] Close ALL positions and pending orders");
           success = ClosePositions("", -1, command.comment, command.nm);
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
         success = ClosePositions(command.symbol, OP_BUY, command.comment, command.nm);
         break;
         
      case COMMAND_CLOSE_SHORT:
         success = ClosePositions(command.symbol, OP_SELL, command.comment, command.nm);
         break;
         
      case COMMAND_CLOSE_LONG_SHORT:
         success = ClosePositions(command.symbol, -1, command.comment, command.nm);
         break;
         
      case COMMAND_EXIT:
         success = ClosePositions(command.symbol, -1, command.comment, command.nm);
         break;
         
      case COMMAND_CLOSE_LONG_OPEN_LONG:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongOpenLong(command.symbol, command.lots, 
                                                 command.stop_loss, command.take_profit, command.comment);
            success = ExecuteMarketOrder(command);
         }
         break;
         
      case COMMAND_CLOSE_LONG_OPEN_SHORT:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongOpenShort(command.symbol, command.lots,
                                                  command.stop_loss, command.take_profit, command.comment);
            SignalCommand sellCmd = command;
            sellCmd.type = COMMAND_SELL;
            success = ExecuteMarketOrder(sellCmd);
         }
         break;
         
      case COMMAND_CLOSE_SHORT_OPEN_LONG:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseShortOpenLong(command.symbol, command.lots,
                                                  command.stop_loss, command.take_profit, command.comment);
            SignalCommand buyCmd = command;
            buyCmd.type = COMMAND_BUY;
            success = ExecuteMarketOrder(buyCmd);
         }
         break;
         
      case COMMAND_CLOSE_SHORT_OPEN_SHORT:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseShortOpenShort(command.symbol, command.lots,
                                                   command.stop_loss, command.take_profit, command.comment);
            success = ExecuteMarketOrder(command);
         }
         break;
         
      case COMMAND_CLOSE_LONGSHORT_OPEN_LONG:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongShortOpenLong(command.symbol, command.lots,
                                                      command.stop_loss, command.take_profit, command.comment);
            SignalCommand buyCmd = command;
            buyCmd.type = COMMAND_BUY;
            success = ExecuteMarketOrder(buyCmd);
         }
         break;
         
      case COMMAND_CLOSE_LONGSHORT_OPEN_SHORT:
         if(g_combinedManager != NULL)
         {
            g_combinedManager.CloseLongShortOpenShort(command.symbol, command.lots,
                                                       command.stop_loss, command.take_profit, command.comment);
            SignalCommand sellCmd = command;
            sellCmd.type = COMMAND_SELL;
            success = ExecuteMarketOrder(sellCmd);
         }
         break;
         
      case COMMAND_CANCEL_LONG_BUY_STOP:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelLongBuyStop(command.symbol, command.lots, command.pending_distance,
                                                 command.stop_loss, command.take_profit, command.comment);
            SignalCommand stopCmd = command;
            stopCmd.type = COMMAND_BUY_STOP;
            success = ExecutePendingOrder(stopCmd);
         }
         break;
         
      case COMMAND_CANCEL_LONG_BUY_LIMIT:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelLongBuyLimit(command.symbol, command.lots, command.pending_distance,
                                                  command.stop_loss, command.take_profit, command.comment);
            SignalCommand limitCmd = command;
            limitCmd.type = COMMAND_BUY_LIMIT;
            success = ExecutePendingOrder(limitCmd);
         }
         break;
         
      case COMMAND_CANCEL_SHORT_SELL_STOP:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelShortSellStop(command.symbol, command.lots, command.pending_distance,
                                                   command.stop_loss, command.take_profit, command.comment);
            SignalCommand stopCmd = command;
            stopCmd.type = COMMAND_SELL_STOP;
            success = ExecutePendingOrder(stopCmd);
         }
         break;
         
      case COMMAND_CANCEL_SHORT_SELL_LIMIT:
         if(g_combinedManager != NULL && g_pendingManager != NULL)
         {
            g_combinedManager.CancelShortSellLimit(command.symbol, command.lots, command.pending_distance,
                                                    command.stop_loss, command.take_profit, command.comment);
            SignalCommand limitCmd = command;
            limitCmd.type = COMMAND_SELL_LIMIT;
            success = ExecutePendingOrder(limitCmd);
         }
         break;
         
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
          g_protectionHalted = false;
          g_dailyHalted = false;
          success = true;
          Print("[PineTunnel] Account protection halts cleared");
          break;
         
       case COMMAND_CLOSEALL_EA_OFF:
          // PineTunnel spec: close_all_off requires symbol == ChartSymbol as safety measure
          if(command.symbol != Symbol())
          {
             PrintFormat("[PineTunnel] CLOSE_ALL_OFF rejected: signal symbol '%s' != chart symbol '%s' (PineTunnel spec: must match chart)", command.symbol, Symbol());
             break;
          }
          PrintFormat("[PineTunnel] CLOSE_ALL_OFF %s", command.symbol);
          success = ClosePositions("", -1, command.comment, command.nm);
          // Cancel ALL pending orders (PineTunnel spec)
          if(g_pendingManager != NULL)
          {
             int cancelled_buy = g_pendingManager.CancelBuyOrders("", command.comment);
             int cancelled_sell = g_pendingManager.CancelSellOrders("", command.comment);
             if(cancelled_buy > 0 || cancelled_sell > 0)
                PrintFormat("[PineTunnel] Cancelled %d pending order(s)", cancelled_buy + cancelled_sell);
          }
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
         PrintFormat("[Exec] [TIMING] Signal #%d end-to-end: %ums | Cmd=%s Sym=%s", g_totalSignals, GetTickCount() - exec_start_tick, CommandName(command.type), command.symbol);
      }
      else
      {
         g_failed++;
         g_todayFailed++;
         PrintFormat("[PineTunnel] Signal #%d execution FAILED", g_totalSignals);
         PrintFormat("[Exec] [TIMING] Signal #%d FAILED after %ums | Cmd=%s Sym=%s", g_totalSignals, GetTickCount() - exec_start_tick, CommandName(command.type), command.symbol);
      }
      PrintFormat("[PineTunnel] Stats - Total: %d | Success: %d | Failed: %d", 
                 g_totalSignals, g_successful, g_failed);
   }
   else
   {
      // Queued execution: no additional logging needed
   }
   
   // Mark signal as executed on success (prevents duplicates on restart)
   if(success && command.signal_id != "")
      SaveExecutedSignal(command.signal_id);
   
   return success;
}

//+------------------------------------------------------------------+
//| Add hidden target                                                |
//+------------------------------------------------------------------+
bool AddHiddenTarget(int ticket, string symbol, int position_type, double sl_price, double tp_price, double entry_price)
{
   if(g_hiddenCount >= MAX_HIDDEN_TARGETS)
   {
      Print("[PineTunnel] Hidden targets array full - cannot add more");
      return false;
   }

   // Check for existing
   for(int i = 0; i < g_hiddenCount; i++)
   {
      if(g_hiddenTargets[i].ticket == ticket)
      {
         g_hiddenTargets[i].hidden_sl = sl_price;
         g_hiddenTargets[i].hidden_tp = tp_price;
         PrintFormat("[PineTunnel] Hidden target updated for ticket #%d", ticket);
         return true;
      }
   }

   // Add new
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
//| Remove hidden target                                             |
//+------------------------------------------------------------------+
void RemoveHiddenTarget(int ticket)
{
   for(int i = 0; i < g_hiddenCount; i++)
   {
      if(g_hiddenTargets[i].ticket == ticket)
      {
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
//| Process hidden targets                                           |
//+------------------------------------------------------------------+
void ProcessHiddenSLTP()
{
   if(g_hiddenCount == 0) return;
   RefreshRates();

   for(int i = g_hiddenCount - 1; i >= 0; i--)
   {
      int ticket = g_hiddenTargets[i].ticket;

      // Check if position still exists
      if(!OrderSelect(ticket, SELECT_BY_TICKET))
      {
         RemoveHiddenTarget(ticket);
         continue;
      }

      // Use cached symbol and type (parity with MT5)
      string symbol = g_hiddenTargets[i].symbol;
      bool is_buy = (g_hiddenTargets[i].type == OP_BUY);

      double current_price = is_buy ? MarketInfo(symbol, MODE_BID) : MarketInfo(symbol, MODE_ASK);
      
      bool should_close = false;
      string reason = "";
      
      double tolerance = MarketInfo(symbol, MODE_POINT);
      int digits = (int)MarketInfo(symbol, MODE_DIGITS);
      
      // Check SL
      if(g_hiddenTargets[i].hidden_sl > 0)
      {
         if(is_buy)
         {
            if(current_price <= g_hiddenTargets[i].hidden_sl + tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden SL hit: %." + IntegerToString(digits) + "f <= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_sl);
            }
         }
         else
         {
            if(current_price >= g_hiddenTargets[i].hidden_sl - tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden SL hit: %." + IntegerToString(digits) + "f >= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_sl);
            }
         }
      }
      
      // Check TP
      if(!should_close && g_hiddenTargets[i].hidden_tp > 0)
      {
         if(is_buy)
         {
            if(current_price >= g_hiddenTargets[i].hidden_tp - tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden TP hit: %." + IntegerToString(digits) + "f >= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_tp);
            }
         }
         else
         {
            if(current_price <= g_hiddenTargets[i].hidden_tp + tolerance)
            {
               should_close = true;
               reason = StringFormat("Hidden TP hit: %." + IntegerToString(digits) + "f <= %." + IntegerToString(digits) + "f",
                                   current_price, g_hiddenTargets[i].hidden_tp);
            }
         }
      }
      
      if(should_close)
      {
         PrintFormat("[PineTunnel] Hidden SL/TP Triggered!");
         PrintFormat("[PineTunnel] Ticket: #%d | %s | %s", ticket, symbol, is_buy ? "BUY" : "SELL");
         PrintFormat("[PineTunnel] Reason: %s", reason);
          PrintFormat("[PineTunnel] Entry: %." + IntegerToString(digits) + "f | Current: %." + IntegerToString(digits) + "f",
                     g_hiddenTargets[i].entry_price, current_price);
         
         RefreshRates();
         double close_price = is_buy ? MarketInfo(symbol, MODE_BID) : MarketInfo(symbol, MODE_ASK);
         double close_lots  = OrderLots();
         double close_pnl   = OrderProfit() + OrderSwap() + OrderCommission();

         if(OrderClose(ticket, close_lots, close_price, InpMaxSlippage, CLR_NONE))
         {
            PrintFormat("[PineTunnel] Position closed by Hidden SL/TP | P/L: $%.2f", close_pnl);
            RemoveHiddenTarget(ticket);
         }
         else
         {
            PrintFormat("[PineTunnel] Failed to close position #%d: Error %d", ticket, GetLastError());
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Send trade report                                                |
//+------------------------------------------------------------------+
bool SendTradeReport(string action, string symbol, double volume, double price,
                     int ticket, bool success, string error_msg = "", string signal_id = "")
{
   string correlation_id = IntegerToString(GetTickCount()) + "-" + IntegerToString(MathRand());

   string json = "{";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"action\":\"" + EscapeJSON(action) + "\",";
   json += "\"symbol\":\"" + EscapeJSON(symbol) + "\",";
   json += "\"volume\":" + DoubleToString(volume, 2) + ",";
   json += "\"price\":" + DoubleToString(price, 5) + ",";
   json += "\"ticket\":" + IntegerToString(ticket) + ",";
   json += "\"success\":" + (success ? "true" : "false") + ",";
   json += "\"error_msg\":\"" + EscapeJSON(error_msg) + "\",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   
   // Add position details if successful
   if(success && ticket > 0)
   {
      if(OrderSelect(ticket, SELECT_BY_TICKET))
      {
         json += "\"profit\":" + DoubleToString(OrderProfit(), 2) + ",";
         json += "\"sl\":" + DoubleToString(OrderStopLoss(), 5) + ",";
         json += "\"tp\":" + DoubleToString(OrderTakeProfit(), 5) + ",";
         json += "\"commission\":" + DoubleToString(OrderCommission(), 2) + ",";
         json += "\"swap\":" + DoubleToString(OrderSwap(), 2) + ",";
      }
   }
   
   // Add timestamp
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";
   json += "\"broker_time\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";
   json += "\"account\":" + IntegerToString(AccountNumber()) + ",";
   json += "\"broker\":\"" + EscapeJSON(AccountCompany()) + "\"";
   if(signal_id != "")
      json += ",\"signal_id\":\"" + EscapeJSON(signal_id) + "\"";
   json += "}";

   string url = InpServerURL + "/api/trades/report";
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";

   char post_data[];
   char result[];
   StringToCharArray(json, post_data, 0, StringLen(json));
   
   int res = WebRequest("POST", url, headers, 3000, post_data, result, headers);

   if(res == -1)
      return false;

   if(res == 200 || res == 201)
   {
      // Trade reported - silent
      return true;
   }

   // Silently fail on non-200 responses
   return false;
}

//+------------------------------------------------------------------+
//| Send close report                                                |
//+------------------------------------------------------------------+
bool SendCloseReport(string symbol, int ticket, double close_price, double profit, string signal_id = "")
{
   string correlation_id = IntegerToString(GetTickCount()) + "-" + IntegerToString(MathRand());

   string json = "{";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"action\":\"CLOSE\",";
   json += "\"symbol\":\"" + EscapeJSON(symbol) + "\",";
   json += "\"ticket\":" + IntegerToString(ticket) + ",";
   json += "\"close_price\":" + DoubleToString(close_price, 5) + ",";
   json += "\"profit\":" + DoubleToString(profit, 2) + ",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",";
   json += "\"account\":" + IntegerToString(AccountNumber()) + ",";
   json += "\"broker\":\"" + EscapeJSON(AccountCompany()) + "\"";
   if(signal_id != "")
      json += ",\"signal_id\":\"" + EscapeJSON(signal_id) + "\"";
   json += "}";
   
   string url = InpServerURL + "/api/trades/close";
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";

   char post_data[];
   char result[];
   StringToCharArray(json, post_data, 0, StringLen(json));
   
   int res = WebRequest("POST", url, headers, 3000, post_data, result, headers);

   if(res == -1)
      return false;

   if(res == 200 || res == 201)
   {
      // Close reported - silent
      return true;
   }

   // Silently fail on non-200 responses
   return false;
}

//+------------------------------------------------------------------+
//| Send periodic account stats snapshot to server                  |
//+------------------------------------------------------------------+
void SendAccountStats()
{
   string safe_broker = EscapeJSON(AccountCompany());
   string safe_currency = EscapeJSON(AccountCurrency());
   string safe_name = EscapeJSON(AccountName());
   string dll_ver = "";
   if(g_wsClient != NULL && g_wsClient.IsConnected())
      dll_ver = g_wsClient.GetDllVersion();

   // Count open positions (MT4: OrdersTotal includes pending)
   int open_pos = 0;
   int pending = 0;
    int total = OrdersTotal();
    for(int i = 0; i < total; i++)
   {
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
      {
         if(OrderType() <= OP_SELL)
            open_pos++;
         else
            pending++;
      }
   }

   // Margin level: MT4 doesn't have AccountMarginLevel()
   double margin_level = 0.0;
   double margin = AccountMargin();
   if(margin > 0)
      margin_level = AccountEquity() / margin * 100.0;

   string json = "{";
   json += "\"license_key\":\"" + InpLicenseID + "\",";
   json += "\"account\":" + IntegerToString(AccountNumber()) + ",";
   json += "\"account_name\":\"" + safe_name + "\",";
   json += "\"broker\":\"" + safe_broker + "\",";
   json += "\"currency\":\"" + safe_currency + "\",";
   json += "\"leverage\":" + IntegerToString(AccountLeverage()) + ",";
   json += "\"balance\":" + DoubleToString(AccountBalance(), 2) + ",";
   json += "\"equity\":" + DoubleToString(AccountEquity(), 2) + ",";
   json += "\"profit\":" + DoubleToString(AccountProfit(), 2) + ",";
   json += "\"margin\":" + DoubleToString(margin, 2) + ",";
   json += "\"margin_free\":" + DoubleToString(AccountFreeMargin(), 2) + ",";
   json += "\"margin_level\":" + DoubleToString(margin_level, 2) + ",";
   json += "\"open_positions\":" + IntegerToString(open_pos) + ",";
   json += "\"pending_orders\":" + IntegerToString(pending) + ",";
   json += "\"ea_version\":\"" + PT_VERSION + "\",";
   json += "\"dll_version\":\"" + (dll_ver != "" ? dll_ver : "N/A") + "\",";
   json += "\"magic\":" + IntegerToString(g_magicNumber) + ",";
   json += "\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"";
   json += "}";

   string correlation_id = IntegerToString(GetTickCount()) + "-" + IntegerToString(MathRand());
   string url = InpServerURL + "/api/trades/stats";
   string headers = "X-Correlation-ID: " + correlation_id + "\r\nContent-Type: application/json\r\nAccept-Encoding: identity\r\n";

   char post_data[];
   char result[];
   StringToCharArray(json, post_data, 0, StringLen(json));

   int res = WebRequest("POST", url, headers, 5000, post_data, result, headers);

   if(res == -1)
      return;

   // Account stats sent silently - no logging
}

//+------------------------------------------------------------------+
//| Remove dashboard                                                 |
//+------------------------------------------------------------------+
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
   
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Draw dashboard - EXACT 1:1 copy from MT5                         |
//+------------------------------------------------------------------+
void DrawDashboard()
{
   if(!InpShowDashboard)
      return;
      
   // Table structure with cells
   int num_cols = 4;
   int num_rows = 4;  // Header + 3 data rows
   int cell_width = 220;
   int cell_height = 28;
   int header_height = 32;
   int panel_width = cell_width * num_cols;
   int panel_height = header_height + (cell_height * 3);
   int line_height = InpDashFontSize + 4;
   int cell_padding = 8;
   
   // Auto-center at bottom
   int chart_width = (int)ChartGetInteger(0, CHART_WIDTH_IN_PIXELS);
   int chart_height = (int)ChartGetInteger(0, CHART_HEIGHT_IN_PIXELS);
   int x = (chart_width - panel_width) / 2;  // Center horizontally
   int y = chart_height - panel_height - 30; // Bottom with margin
   
   // Create table cells with borders
   color cell_bg = C'21,23,34';      // #151722
   color cell_border = C'42,46,57';  // #2a2e39
   color header_bg = C'33,38,52';    // Slightly lighter for header
   
   // Create cells
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
   CreateLabel("PT_Signals", "Today: " + IntegerToString(g_todaySignals), col1_x, row1_y, InpDashFontSize, clrWhite, "Arial");
   CreateLabel("PT_Success", "Success: " + IntegerToString(g_todaySuccessful), col1_x, row2_y, InpDashFontSize, clrWhite, "Arial");
   // Failed cell: Red if failed >= 1, white otherwise
   color failedColor = (g_todayFailed >= 1) ? C'255,0,81' : clrWhite;
   CreateLabel("PT_Failed", "Failed: " + IntegerToString(g_todayFailed), col1_x, row3_y, InpDashFontSize, failedColor, "Arial");
   
   // === COLUMN 2: ACCOUNT ===
   double balance = AccountBalance();
   double equity = AccountEquity();
   int total_positions = OrdersTotal();
   double pnl = equity - balance;
   
   CreateLabel("PT_Balance", StringFormat("PnL: $%.0f", pnl), col2_x, row1_y, InpDashFontSize, clrWhite, "Arial");
   CreateLabel("PT_Equity", StringFormat("Eq: $%.0f", equity), col2_x, row2_y, InpDashFontSize, clrWhite, "Arial");
   CreateLabel("PT_Positions", StringFormat("Pos: %d", total_positions), col2_x, row3_y, InpDashFontSize, clrWhite, "Arial");
   
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
         CreateLabel("PT_Connected", "[*] WSS " + wsUptime, col3_x, row1_y, InpDashFontSize, C'0,230,118', "Arial");
         
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
         CreateLabel("PT_LastPoll", "Rx:" + rxStr + " Tx:" + txStr + latencyStr, col3_x, row2_y, InpDashFontSize, C'0,185,255', "Arial");
         
         // Queue depth + reconnect count
         string queueInfo = "Q:" + IntegerToString(wsStats.frames_queued);
         if(wsStats.frames_dropped > 0)
            queueInfo += " Drop:" + IntegerToString(wsStats.frames_dropped);
         if(wsStats.reconnect_count > 0)
            queueInfo += " Rc:" + IntegerToString(wsStats.reconnect_count);
         color queueColor = wsStats.frames_dropped > 0 ? C'255,165,0' : clrWhite;
         CreateLabel("PT_AvgSpread", queueInfo, col3_x, row3_y, InpDashFontSize, queueColor, "Arial");
      }
      else
      {
         int uptime_seconds = (int)(now - g_connectedSince);
         string uptime = FormatUptime(uptime_seconds);
         CreateLabel("PT_Connected", "[*] WSS " + uptime, col3_x, row1_y, InpDashFontSize, C'0,230,118', "Arial");
         CreateLabel("PT_LastPoll", "Connected", col3_x, row2_y, InpDashFontSize, C'0,185,255', "Arial");
         CreateLabel("PT_AvgSpread", "", col3_x, row3_y, InpDashFontSize, clrWhite, "Arial");
      }
   }
   else
   {
      // HTTP long-poll mode - show "Polling" with poll info
      int uptime_seconds = (int)(now - g_connectedSince);
      string uptime = FormatUptime(uptime_seconds);
      
      string pollInfo;
      color pollColor;
      if(!g_lastPollSuccess)
      {
         CreateLabel("PT_Connected", "[*] Offline", col3_x, row1_y, InpDashFontSize, C'255,0,81', "Arial");
         pollInfo = "Disconnected";
         pollColor = C'255,0,81';
      }
      else
      {
         CreateLabel("PT_Connected", "[*] Polling " + uptime, col3_x, row1_y, InpDashFontSize, C'255,185,0', "Arial");
         int secondsAgo = (int)(now - g_lastPoll);
         string lastPollText = secondsAgo == 0 ? "now" : IntegerToString(secondsAgo) + "s ago";
         pollInfo = "Poll: " + lastPollText;
         pollColor = C'255,185,0';
      }
      
      CreateLabel("PT_LastPoll", pollInfo, col3_x, row2_y, InpDashFontSize, pollColor, "Arial");
      
      if(g_spreadSamples > 0)
         CreateLabel("PT_AvgSpread", StringFormat("Spread: %.1f pts", g_avgSpread), col3_x, row3_y, InpDashFontSize, clrWhite, "Arial");
      else
         CreateLabel("PT_AvgSpread", "", col3_x, row3_y, InpDashFontSize, clrWhite, "Arial");
   }
   
   // === COLUMN 4: STATUS ===
   string licenseDisplay = StringLen(InpLicenseID) > 14 ? StringSubstr(InpLicenseID, 0, 11) + "..." : InpLicenseID;
   CreateLabel("PT_License", licenseDisplay, col4_x, row1_y, InpDashFontSize-1, clrWhite, "Arial");

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
   CreateLabel("PT_EAStatus", eaStatus, col4_x, row2_y, InpDashFontSize-1, eaColor, "Arial");
   CreateLabel("PT_Row3Col4", "github.com/TheFractalyst/PineTunnel", col4_x, row3_y, InpDashFontSize-1, C'0,185,255', "Arial");
   
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Create text label - EXACT 1:1 copy from MT5                      |
//+------------------------------------------------------------------+
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

//+------------------------------------------------------------------+
//| Format uptime string - EXACT 1:1 copy from MT5                   |
//+------------------------------------------------------------------+
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
