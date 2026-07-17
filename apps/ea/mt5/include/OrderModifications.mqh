//+------------------------------------------------------------------+
//|                                         OrderModifications.mqh   |
//|                     PineTunnel Compatible Modifications       |
//|                                                                  |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"

#include <Trade\OrderInfo.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\SymbolInfo.mqh>
#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| Target Type Enumeration (PineTunnel EA)             |
//+------------------------------------------------------------------+
enum ENUM_TARGET_TYPE
{
   TARGET_TYPE_PIPS = 0,       // Pips from current price
   TARGET_TYPE_PRICE = 1,      // Specific price level
   TARGET_TYPE_PERCENTAGE = 2  // Percentage from current price
};

//+------------------------------------------------------------------+
//| Order Modification Manager Class                                |
//+------------------------------------------------------------------+
class COrderModificationManager
{
private:
   CTrade*         m_trade;
   COrderInfo      m_order;
   CPositionInfo   m_position;
   CSymbolInfo     m_symbol;
   int             m_magic;
   ENUM_TARGET_TYPE m_target_type;
   bool            m_log_enabled;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   COrderModificationManager(CTrade* trade, int magic, ENUM_TARGET_TYPE target_type = TARGET_TYPE_PIPS)
   {
      m_trade = trade;
      m_magic = magic;
      m_target_type = target_type;
      m_log_enabled = true;
   }

   //+------------------------------------------------------------------+
   //| Set logging enabled/disabled                                    |
   //+------------------------------------------------------------------+
   void SetLogging(bool enabled) { m_log_enabled = enabled; }

   //+------------------------------------------------------------------+
   //| Modify long positions SL/TP (sltp_long)                      |
   //+------------------------------------------------------------------+
   bool ModifyLongPositions(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPositions(symbol, POSITION_TYPE_BUY, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify short positions SL/TP (sltp_short)                    |
   //+------------------------------------------------------------------+
   bool ModifyShortPositions(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPositions(symbol, POSITION_TYPE_SELL, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify buy stop orders SL/TP (sltp_buy_stop)                  |
   //+------------------------------------------------------------------+
   bool ModifyBuyStopOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, ORDER_TYPE_BUY_STOP, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify buy limit orders SL/TP (sltp_buy_limit)                |
   //+------------------------------------------------------------------+
   bool ModifyBuyLimitOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, ORDER_TYPE_BUY_LIMIT, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify sell stop orders SL/TP (sltp_sell_stop)                |
   //+------------------------------------------------------------------+
   bool ModifySellStopOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, ORDER_TYPE_SELL_STOP, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify sell limit orders SL/TP (sltp_sell_limit)              |
   //+------------------------------------------------------------------+
   bool ModifySellLimitOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, ORDER_TYPE_SELL_LIMIT, sl_value, tp_value, comment);
   }

private:
   //+------------------------------------------------------------------+
   //| Modify open positions                                          |
   //+------------------------------------------------------------------+
   bool ModifyPositions(string symbol, ENUM_POSITION_TYPE pos_type,
                       double sl_value, double tp_value, string comment = "")
   {
      if(!m_symbol.Name(symbol))
      {
         if(m_log_enabled)
            PrintFormat("[OrderMod] ERROR: Failed to select symbol %s", symbol);
         return false;
      }

      // Refresh rates
      if(!m_symbol.Refresh() || !m_symbol.RefreshRates())
      {
         if(m_log_enabled)
            PrintFormat("[OrderMod] WARNING: Failed to refresh rates for %s", symbol);
      }

      int modified_count = 0;
      int total = PositionsTotal();

      for(int i = 0; i < total; i++)
      {
         if(!m_position.SelectByIndex(i))
            continue;

         if(m_position.Magic() != m_magic)
            continue;
         if(m_position.Symbol() != symbol)
            continue;
         if(m_position.PositionType() != pos_type)
            continue;
          if(comment != "" && StringFind(m_position.Comment(), comment) < 0)
             continue;

         double new_sl = m_position.StopLoss();
         double new_tp = m_position.TakeProfit();

         // Get current market price
         double current_price = (pos_type == POSITION_TYPE_BUY) ?
                                m_symbol.Bid() : m_symbol.Ask();

         // Calculate new SL if provided
         if(sl_value >= 0)  // sl_value == 0 means breakeven
         {
            if(sl_value == 0)
            {
               // Move to breakeven
               new_sl = m_position.PriceOpen();

               // Check if position is in profit
               bool in_profit = (pos_type == POSITION_TYPE_BUY) ?
                               (current_price > m_position.PriceOpen()) :
                               (current_price < m_position.PriceOpen());

               if(!in_profit)
               {
                  if(m_log_enabled)
                     PrintFormat("[OrderMod] Position #%I64d not in profit, skipping BE",
                                 m_position.Ticket());
                  continue;
               }
            }
            else
            {
               new_sl = CalculatePrice(sl_value, current_price, symbol,
                                       pos_type == POSITION_TYPE_BUY ? false : true);
            }
         }

         // Calculate new TP if provided
         if(tp_value > 0)
         {
            new_tp = CalculatePrice(tp_value, current_price, symbol,
                                    pos_type == POSITION_TYPE_BUY ? true : false);
         }

         // Normalize prices
         int digits = m_symbol.Digits();
         new_sl = NormalizeDouble(new_sl, digits);
         new_tp = NormalizeDouble(new_tp, digits);

         // Validate against minimum stop level
         if(!ValidateStopLevel(symbol, current_price, new_sl, new_tp))
            continue;

         if(m_trade.PositionModify(m_position.Ticket(), new_sl, new_tp))
         {
            modified_count++;
            if(m_log_enabled)
               PrintFormat("[OrderMod] Modified position #%I64d - SL:%.5f TP:%.5f",
                           m_position.Ticket(), new_sl, new_tp);
         }
         else
         {
            if(m_log_enabled)
               PrintFormat("[OrderMod] Failed to modify position #%I64d - Error: %d",
                           m_position.Ticket(), m_trade.ResultRetcode());
         }
      }

      if(m_log_enabled)
         PrintFormat("[OrderMod] Modified %d %s position(s) for %s",
                    modified_count,
                    pos_type == POSITION_TYPE_BUY ? "BUY" : "SELL",
                    symbol);

      return (modified_count > 0);
   }

   //+------------------------------------------------------------------+
   //| Modify pending orders                                          |
   //+------------------------------------------------------------------+
   bool ModifyPendingOrders(string symbol, ENUM_ORDER_TYPE order_type,
                           double sl_value, double tp_value, string comment = "")
   {
      if(!m_symbol.Name(symbol))
      {
         if(m_log_enabled)
            PrintFormat("[OrderMod] ERROR: Failed to select symbol %s", symbol);
         return false;
      }

      int modified_count = 0;
      int total = OrdersTotal();

      for(int i = 0; i < total; i++)
      {
         if(!m_order.SelectByIndex(i))
            continue;

         if(m_order.Magic() != m_magic)
            continue;
         if(m_order.Symbol() != symbol)
            continue;
         if(m_order.OrderType() != order_type)
            continue;
          if(comment != "" && StringFind(m_order.Comment(), comment) < 0)
             continue;

          double order_price = m_order.PriceOpen();
          double new_sl = m_order.StopLoss();
          double new_tp = m_order.TakeProfit();
          bool is_buy = (order_type == ORDER_TYPE_BUY_STOP ||
                        order_type == ORDER_TYPE_BUY_LIMIT);

          // Calculate new SL if provided
          if(sl_value >= 0)
          {
             if(sl_value == 0)
             {
                // For pending orders, sl=0 means remove SL
                new_sl = 0;
             }
             else
             {
                new_sl = CalculatePrice(sl_value, order_price, symbol, !is_buy);
             }
          }

          // Calculate new TP if provided
          if(tp_value > 0)
          {
             new_tp = CalculatePrice(tp_value, order_price, symbol, is_buy);
          }

         // Normalize prices
         int digits = m_symbol.Digits();
         new_sl = NormalizeDouble(new_sl, digits);
         new_tp = NormalizeDouble(new_tp, digits);

         // Validate against minimum stop level using ORDER price (not market price)
         if(!ValidateStopLevel(symbol, order_price, new_sl, new_tp))
            continue;

         if(m_trade.OrderModify(
            m_order.Ticket(),
            m_order.PriceOpen(),     // Keep same entry price
            new_sl,
            new_tp,
            m_order.TypeTime(),
            m_order.TimeExpiration()
         ))
         {
            modified_count++;
            if(m_log_enabled)
               PrintFormat("[OrderMod] Modified order #%I64d - SL:%.5f TP:%.5f",
                           m_order.Ticket(), new_sl, new_tp);
         }
         else
         {
            if(m_log_enabled)
               PrintFormat("[OrderMod] Failed to modify order #%I64d - Error: %d",
                           m_order.Ticket(), m_trade.ResultRetcode());
         }
      }

      if(m_log_enabled)
         PrintFormat("[OrderMod] Modified %d %s order(s) for %s",
                    modified_count, OrderTypeToString(order_type), symbol);

      return (modified_count > 0);
   }

   //+------------------------------------------------------------------+
   //| Calculate price based on target type                           |
   //+------------------------------------------------------------------+
   double CalculatePrice(double value, double reference_price, string symbol, bool add)
   {
      if(!m_symbol.Name(symbol))
         return 0;

      double result_price = 0;
      double point = m_symbol.Point();

      switch(m_target_type)
      {
         case TARGET_TYPE_PIPS:
            {
               double distance = value * point;
               result_price = add ? (reference_price + distance) : (reference_price - distance);

               if(m_log_enabled)
                  PrintFormat("[OrderMod] PIPS mode: %.0f pips from %.5f = %.5f",
                             value, reference_price, result_price);
            }
            break;

         case TARGET_TYPE_PRICE:
            result_price = value;  // Direct price
            if(m_log_enabled)
               PrintFormat("[OrderMod] PRICE mode: Direct price = %.5f", result_price);
            break;

         case TARGET_TYPE_PERCENTAGE:
            {
               double distance = reference_price * (value / 100.0);
               result_price = add ? (reference_price + distance) : (reference_price - distance);

               if(m_log_enabled)
                  PrintFormat("[OrderMod] PERCENT mode: %.2f%% from %.5f = %.5f",
                             value, reference_price, result_price);
            }
            break;
      }

      return result_price;
   }

   //+------------------------------------------------------------------+
   //| Validate stop level requirements                               |
   //+------------------------------------------------------------------+
   bool ValidateStopLevel(string symbol, double current_price, double sl, double tp)
   {
      long stops_level = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      if(stops_level <= 0)
         return true;  // No restrictions

      double min_distance = stops_level * SymbolInfoDouble(symbol, SYMBOL_POINT);

      // Check SL distance
      if(sl > 0)
      {
         double sl_distance = MathAbs(current_price - sl);
         if(sl_distance < min_distance)
         {
            if(m_log_enabled)
               PrintFormat("[OrderMod] WARNING: SL too close (%.5f < %.5f minimum)",
                          sl_distance, min_distance);
            return false;
         }
      }

      // Check TP distance
      if(tp > 0)
      {
         double tp_distance = MathAbs(current_price - tp);
         if(tp_distance < min_distance)
         {
            if(m_log_enabled)
               PrintFormat("[OrderMod] WARNING: TP too close (%.5f < %.5f minimum)",
                          tp_distance, min_distance);
            return false;
         }
      }

      return true;
   }

   //+------------------------------------------------------------------+
   //| Convert order type to string                                   |
   //+------------------------------------------------------------------+
   string OrderTypeToString(ENUM_ORDER_TYPE type)
   {
      switch(type)
      {
         case ORDER_TYPE_BUY_STOP:   return "BUY STOP";
         case ORDER_TYPE_BUY_LIMIT:  return "BUY LIMIT";
         case ORDER_TYPE_SELL_STOP:  return "SELL STOP";
         case ORDER_TYPE_SELL_LIMIT: return "SELL LIMIT";
         default:                    return "UNKNOWN";
      }
   }
};
