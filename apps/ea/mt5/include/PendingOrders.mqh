//+------------------------------------------------------------------+
//|                                              PendingOrders.mqh   |
//|                     PineTunnel Compatible Pending Orders      |
//|                                                                  |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"

#include <Trade\OrderInfo.mqh>
#include <Trade\SymbolInfo.mqh>
#include <Trade\Trade.mqh>

// 1 pip = 10 points (standard 5-digit broker convention) for 5-digit brokers
#define PIPS_TO_POINTS          10
// Tolerance for SL/TP verification after order placement (points)
#define PRICE_TOLERANCE_POINTS  5
// Number of retries for SL/TP correction after order placement
#define SL_TP_VERIFY_RETRIES    3

//+------------------------------------------------------------------+
//| Pending Order Type Enumeration                                  |
//+------------------------------------------------------------------+
enum ENUM_PENDING_TYPE
{
   PENDING_PIPS,      // Pips from current price
   PENDING_PRICE,     // Specific price level
   PENDING_PERCENT    // Percentage from current price
};

//+------------------------------------------------------------------+
//| Pending Order Manager Class                                     |
//+------------------------------------------------------------------+
class CPendingOrderManager
{
private:
   CTrade*      m_trade;
   COrderInfo   m_order;
   CSymbolInfo  m_symbol;
   int          m_magic;
   bool         m_log_enabled;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   CPendingOrderManager(CTrade* trade, int magic)
   {
      m_trade = trade;
      m_magic = magic;
      m_log_enabled = true;
   }

   //+------------------------------------------------------------------+
   //| Set logging enabled/disabled                                    |
   //+------------------------------------------------------------------+
   void SetLogging(bool enabled) { m_log_enabled = enabled; }

   //+------------------------------------------------------------------+
   //| Place Buy Limit Order                                          |
   //+------------------------------------------------------------------+
   bool PlaceBuyLimit(string symbol, double lots, double pending_value,
                      ENUM_PENDING_TYPE pending_type, double sl_points = 0,
                      double tp_points = 0, string comment = "")
   {
      return PlacePendingOrder(ORDER_TYPE_BUY_LIMIT, symbol, lots,
                              pending_value, pending_type, sl_points, tp_points, comment);
   }

   //+------------------------------------------------------------------+
   //| Place Sell Limit Order                                         |
   //+------------------------------------------------------------------+
   bool PlaceSellLimit(string symbol, double lots, double pending_value,
                       ENUM_PENDING_TYPE pending_type, double sl_points = 0,
                       double tp_points = 0, string comment = "")
   {
      return PlacePendingOrder(ORDER_TYPE_SELL_LIMIT, symbol, lots,
                              pending_value, pending_type, sl_points, tp_points, comment);
   }

   //+------------------------------------------------------------------+
   //| Place Buy Stop Order                                           |
   //+------------------------------------------------------------------+
   bool PlaceBuyStop(string symbol, double lots, double pending_value,
                     ENUM_PENDING_TYPE pending_type, double sl_points = 0,
                     double tp_points = 0, string comment = "")
   {
      return PlacePendingOrder(ORDER_TYPE_BUY_STOP, symbol, lots,
                              pending_value, pending_type, sl_points, tp_points, comment);
   }

   //+------------------------------------------------------------------+
   //| Place Sell Stop Order                                          |
   //+------------------------------------------------------------------+
   bool PlaceSellStop(string symbol, double lots, double pending_value,
                      ENUM_PENDING_TYPE pending_type, double sl_points = 0,
                      double tp_points = 0, string comment = "")
   {
      return PlacePendingOrder(ORDER_TYPE_SELL_STOP, symbol, lots,
                              pending_value, pending_type, sl_points, tp_points, comment);
   }

   //+------------------------------------------------------------------+
   //| Cancel Buy Orders (BuyStop and BuyLimit)                       |
   //+------------------------------------------------------------------+
   int CancelBuyOrders(string symbol = "", string comment = "")
   {
      return CancelOrdersByType(symbol, true, false, comment);
   }

   //+------------------------------------------------------------------+
   //| Cancel Sell Orders (SellStop and SellLimit)                    |
   //+------------------------------------------------------------------+
   int CancelSellOrders(string symbol = "", string comment = "")
   {
      return CancelOrdersByType(symbol, false, true, comment);
   }

private:
   //+------------------------------------------------------------------+
   //| Main function to place pending order                           |
   //+------------------------------------------------------------------+
   bool PlacePendingOrder(ENUM_ORDER_TYPE order_type, string symbol, double lots,
                          double pending_value, ENUM_PENDING_TYPE pending_type,
                          double sl_points, double tp_points, string comment)
   {
      // Initialize symbol
      if(!m_symbol.Name(symbol))
      {
         if(m_log_enabled)
            PrintFormat("[PendingOrder] ERROR: Failed to select symbol %s", symbol);
         return false;
      }

      // Select symbol in Market Watch
      if(!SymbolSelect(symbol, true))
      {
         if(m_log_enabled)
            PrintFormat("[PendingOrder] ERROR: Failed to add %s to Market Watch", symbol);
         return false;
      }

      // Refresh rates
      if(!m_symbol.Refresh() || !m_symbol.RefreshRates())
      {
         if(m_log_enabled)
            PrintFormat("[PendingOrder] WARNING: Failed to refresh rates for %s", symbol);
      }

      // Get current prices
      double ask = m_symbol.Ask();
      double bid = m_symbol.Bid();
      double point = m_symbol.Point();
      int digits = m_symbol.Digits();

      if(ask <= 0 || bid <= 0)
      {
         if(m_log_enabled)
            PrintFormat("[PendingOrder] ERROR: Invalid prices for %s (Ask: %.5f, Bid: %.5f)",
                       symbol, ask, bid);
         return false;
      }

      // Calculate entry price
      double entry_price = CalculateEntryPrice(order_type, pending_value, pending_type, ask, bid, point);

      if(entry_price <= 0)
      {
         if(m_log_enabled)
            PrintFormat("[PendingOrder] ERROR: Invalid entry price calculated");
         return false;
      }

      // Validate entry price for order type
      if(!ValidateEntryPrice(order_type, entry_price, ask, bid))
      {
         if(m_log_enabled)
            PrintFormat("[PendingOrder] ERROR: Invalid entry price %.5f for %s (Ask: %.5f, Bid: %.5f)",
                       entry_price, OrderTypeToString(order_type), ask, bid);
         return false;
      }

      // Normalize entry price
      entry_price = NormalizeDouble(entry_price, digits);

       // Calculate SL/TP prices
       double sl_price = 0;
       double tp_price = 0;
       bool is_buy = (order_type == ORDER_TYPE_BUY_LIMIT || order_type == ORDER_TYPE_BUY_STOP);

       if(sl_points > 0)
       {
          double sl_distance = sl_points * point * PIPS_TO_POINTS;
          if(is_buy)
             sl_price = entry_price - sl_distance;
          else
             sl_price = entry_price + sl_distance;

          sl_price = NormalizeDouble(sl_price, digits);
       }

       if(tp_points > 0)
       {
          double tp_distance = tp_points * point * PIPS_TO_POINTS;
          if(is_buy)
             tp_price = entry_price + tp_distance;
          else
             tp_price = entry_price - tp_distance;

          tp_price = NormalizeDouble(tp_price, digits);
       }

       // Check minimum stop level (directional: BUY_LIMIT from ask, SELL_LIMIT from bid)
       long stops_level = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
       if(stops_level > 0)
       {
          double min_distance = stops_level * point;
          double current_distance;
          if(is_buy)
             current_distance = ask - entry_price;     // BUY: distance below ask
          else
             current_distance = entry_price - bid;     // SELL: distance above bid

         if(current_distance < min_distance)
         {
            if(m_log_enabled)
               PrintFormat("[PendingOrder] WARNING: Entry price too close to market. Distance: %.5f, Minimum: %.5f",
                          current_distance, min_distance);
         }
      }

      // Place the order
      bool result = m_trade.OrderOpen(
         symbol,
         order_type,
         lots,
         0,              // limit (not used for pending)
         entry_price,    // pending price
         sl_price,
         tp_price,
         ORDER_TIME_GTC,
         0,
         comment
      );

      if(result)
      {
         // Verify and fix SL/TP after pending order placement
         ulong order_ticket = m_trade.ResultOrder();
         if(order_ticket > 0 && (sl_price > 0 || tp_price > 0))
         {
            if(OrderSelect(order_ticket))
            {
               double current_sl = OrderGetDouble(ORDER_SL);
               double current_tp = OrderGetDouble(ORDER_TP);
               double tolerance = point * PRICE_TOLERANCE_POINTS;

               bool sl_ok = (sl_price <= 0) || (MathAbs(current_sl - sl_price) < tolerance);
               bool tp_ok = (tp_price <= 0) || (MathAbs(current_tp - tp_price) < tolerance);

               if(!sl_ok || !tp_ok)
               {
                  PrintFormat("[PendingOrder] SL/TP MISMATCH for order #%I64d", order_ticket);
                  PrintFormat("[PendingOrder] Expected: SL=%.5f TP=%.5f | Actual: SL=%.5f TP=%.5f",
                              sl_price, tp_price, current_sl, current_tp);

                  bool fixed = false;
                  for(int retry = 0; retry < SL_TP_VERIFY_RETRIES; retry++)
                  {
                     if(m_trade.OrderModify(order_ticket, entry_price, sl_price, tp_price, ORDER_TIME_GTC, 0))
                     {
                        PrintFormat("[PendingOrder] SL/TP FIXED on retry %d", retry + 1);
                        fixed = true;
                        break;
                     }
                     PrintFormat("[PendingOrder] Retry %d/%d failed: %d - %s",
                                 retry + 1, SL_TP_VERIFY_RETRIES, m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
                     Sleep(100);
                  }
                  if(!fixed)
                  {
                     PrintFormat("[PendingOrder] CRITICAL: Could not set SL/TP after %d retries!", SL_TP_VERIFY_RETRIES);
                     PrintFormat("[PendingOrder] Order #%I64d may have NO STOP LOSS protection!", order_ticket);
                  }
               }
            }
         }
      }
      else
      {
         if(m_log_enabled)
         {
            PrintFormat("[PendingOrder] Failed to place %s order - Error %d: %s",
                       OrderTypeToString(order_type),
                       m_trade.ResultRetcode(),
                       m_trade.ResultRetcodeDescription());
         }
      }

      return result;
   }

   //+------------------------------------------------------------------+
   //| Calculate entry price based on pending type                    |
   //+------------------------------------------------------------------+
    double CalculateEntryPrice(ENUM_ORDER_TYPE order_type, double pending_value,
                               ENUM_PENDING_TYPE pending_type, double ask, double bid, double point)
    {
       double entry_price = 0;
       bool is_buy = (order_type == ORDER_TYPE_BUY_LIMIT || order_type == ORDER_TYPE_BUY_STOP);
       double ref_price = is_buy ? ask : bid;
       bool subtract = (order_type == ORDER_TYPE_BUY_LIMIT || order_type == ORDER_TYPE_SELL_STOP);

       // Calculate based on pending type
       switch(pending_type)
       {
          case PENDING_PIPS:
             {
                double distance = pending_value * point * PIPS_TO_POINTS;
                entry_price = subtract ? ref_price - distance : ref_price + distance;

                if(m_log_enabled)
                   PrintFormat("[PendingOrder] PIPS mode: %.0f pips from %.5f = %.5f",
                              pending_value, ref_price, entry_price);
             }
             break;

          case PENDING_PRICE:
             entry_price = pending_value;
             if(m_log_enabled)
                PrintFormat("[PendingOrder] PRICE mode: Direct price = %.5f", entry_price);
             break;

          case PENDING_PERCENT:
             {
                double distance = ref_price * (pending_value / 100.0);
                entry_price = subtract ? ref_price - distance : ref_price + distance;

                if(m_log_enabled)
                   PrintFormat("[PendingOrder] PERCENT mode: %.2f%% from %.5f = %.5f",
                              pending_value, ref_price, entry_price);
             }
             break;
       }

       return entry_price;
    }

   //+------------------------------------------------------------------+
   //| Validate entry price for order type                            |
   //+------------------------------------------------------------------+
   bool ValidateEntryPrice(ENUM_ORDER_TYPE order_type, double entry_price, double ask, double bid)
   {
      switch(order_type)
      {
         case ORDER_TYPE_BUY_LIMIT:
            return entry_price < ask;  // Must be below current ask

         case ORDER_TYPE_SELL_LIMIT:
            return entry_price > bid;  // Must be above current bid

         case ORDER_TYPE_BUY_STOP:
            return entry_price > ask;  // Must be above current ask

         case ORDER_TYPE_SELL_STOP:
            return entry_price < bid;  // Must be below current bid
      }

      return false;
   }

   //+------------------------------------------------------------------+
   //| Cancel pending orders by type                                  |
   //+------------------------------------------------------------------+
   int CancelOrdersByType(string symbol, bool cancel_buy, bool cancel_sell, string comment = "")
   {
      int cancelled = 0;
      int total = OrdersTotal();

       for(int i = total - 1; i >= 0; i--)
       {
          if(!m_order.SelectByIndex(i))
             continue;

          if(m_order.Magic() != m_magic)
             continue;

          if(symbol != "" && m_order.Symbol() != symbol)
             continue;

          if(comment != "" && StringFind(m_order.Comment(), comment) < 0)
             continue;

         ENUM_ORDER_TYPE order_type = m_order.OrderType();
         bool is_buy = (order_type == ORDER_TYPE_BUY_STOP || order_type == ORDER_TYPE_BUY_LIMIT);
         bool is_sell = (order_type == ORDER_TYPE_SELL_STOP || order_type == ORDER_TYPE_SELL_LIMIT);

         if((cancel_buy && is_buy) || (cancel_sell && is_sell))
         {
            if(m_trade.OrderDelete(m_order.Ticket()))
            {
               cancelled++;
               if(m_log_enabled)
                  PrintFormat("[PendingOrder] Cancelled order #%I64d (%s %s)",
                              m_order.Ticket(), OrderTypeToString(order_type), m_order.Symbol());
            }
         }
      }

      if(m_log_enabled)
      {
         if(cancelled > 0)
            PrintFormat("[PendingOrder] Cancelled %d pending order(s)", cancelled);
         else
            PrintFormat("[PendingOrder] No pending orders to cancel");
      }

      return cancelled;
   }

   //+------------------------------------------------------------------+
   //| Convert order type to string                                   |
   //+------------------------------------------------------------------+
   string OrderTypeToString(ENUM_ORDER_TYPE type)
   {
      switch(type)
      {
         case ORDER_TYPE_BUY_LIMIT:  return "BUY LIMIT";
         case ORDER_TYPE_SELL_LIMIT: return "SELL LIMIT";
         case ORDER_TYPE_BUY_STOP:   return "BUY STOP";
         case ORDER_TYPE_SELL_STOP:  return "SELL STOP";
         default:                    return "UNKNOWN";
      }
   }
};
