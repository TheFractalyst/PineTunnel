//+------------------------------------------------------------------+
//|                                     OrderModifications_MT4.mqh   |
//|                     PineTunnel Compatible Modifications       |
//|                     MT4 Version - 1:1 Logic from MT5             |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"
#property strict

//+------------------------------------------------------------------+
//| Target Type Enumeration (PineTunnel EA)             |
//+------------------------------------------------------------------+
enum ENUM_TARGET_TYPE
{
   TARGET_TYPE_PIPS = 0,       // Pips from current price
   TARGET_TYPE_PRICE = 1,      // Specific price level
   TARGET_TYPE_PERCENTAGE = 2     // Percentage from current price
};

//+------------------------------------------------------------------+
//| Order Modification Manager Class                                |
//+------------------------------------------------------------------+
class COrderModificationManager
{
private:
   int             m_magic;
   ENUM_TARGET_TYPE m_target_type;
   bool            m_log_enabled;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   COrderModificationManager(int magic, ENUM_TARGET_TYPE target_type = TARGET_TYPE_PIPS)
   {
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
      return ModifyPositions(symbol, OP_BUY, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify short positions SL/TP (sltp_short)                    |
   //+------------------------------------------------------------------+
   bool ModifyShortPositions(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPositions(symbol, OP_SELL, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify buy stop orders SL/TP (sltp_buy_stop)                  |
   //+------------------------------------------------------------------+
   bool ModifyBuyStopOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, OP_BUYSTOP, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify buy limit orders SL/TP (sltp_buy_limit)                |
   //+------------------------------------------------------------------+
   bool ModifyBuyLimitOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, OP_BUYLIMIT, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify sell stop orders SL/TP (sltp_sell_stop)                |
   //+------------------------------------------------------------------+
   bool ModifySellStopOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, OP_SELLSTOP, sl_value, tp_value, comment);
   }

   //+------------------------------------------------------------------+
   //| Modify sell limit orders SL/TP (sltp_sell_limit)              |
   //+------------------------------------------------------------------+
   bool ModifySellLimitOrders(string symbol, double sl_value = -1, double tp_value = -1, string comment = "")
   {
      return ModifyPendingOrders(symbol, OP_SELLLIMIT, sl_value, tp_value, comment);
   }

private:
   //+------------------------------------------------------------------+
   //| Modify open positions                                          |
   //+------------------------------------------------------------------+
   bool ModifyPositions(string symbol, int pos_type, double sl_value, double tp_value, string comment = "")
   {
      RefreshRates();
      int modified_count = 0;
      int total = OrdersTotal();

      for(int i = 0; i < total; i++)
      {
         if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
            continue;

         // Only market orders (not pending)
         if(OrderType() > OP_SELL)
            continue;

         // Check filters
         if(OrderMagicNumber() != m_magic)
            continue;
         if(OrderSymbol() != symbol)
            continue;
         if(OrderType() != pos_type)
            continue;
         // Check comment filter
         if(comment != "")
         {
            if(StringFind(OrderComment(), comment) < 0)
               continue;
         }

         double new_sl = OrderStopLoss();
         double new_tp = OrderTakeProfit();

         // Get current market price
         double current_price = (pos_type == OP_BUY) ?
                                MarketInfo(symbol, MODE_BID) :
                                MarketInfo(symbol, MODE_ASK);

         // Calculate new SL if provided
         if(sl_value >= 0)  // sl_value == 0 means breakeven
         {
            if(sl_value == 0)
            {
               // Move to breakeven
               new_sl = OrderOpenPrice();

               // Check if position is in profit
               bool in_profit = (pos_type == OP_BUY) ?
                               (current_price > OrderOpenPrice()) :
                               (current_price < OrderOpenPrice());

               if(!in_profit)
               {
                  if(m_log_enabled)
                     PrintFormat("[OrderMod] Position #%d not in profit, skipping BE",
                                OrderTicket());
                  continue;
               }
            }
            else
            {
               new_sl = CalculatePrice(sl_value, current_price, symbol,
                                       pos_type == OP_BUY ? false : true);
            }
         }

         // Calculate new TP if provided
         if(tp_value > 0)
         {
            new_tp = CalculatePrice(tp_value, current_price, symbol,
                                    pos_type == OP_BUY ? true : false);
         }

         // Normalize prices
         int digits = (int)MarketInfo(symbol, MODE_DIGITS);
         new_sl = NormalizeDouble(new_sl, digits);
         new_tp = NormalizeDouble(new_tp, digits);

         // Validate against minimum stop level
         if(!ValidateStopLevel(symbol, current_price, new_sl, new_tp))
            continue;

         // Modify position
         if(OrderModify(OrderTicket(), OrderOpenPrice(), new_sl, new_tp, 0, CLR_NONE))
         {
            modified_count++;
            if(m_log_enabled)
               PrintFormat("[OrderMod] Modified position #%d - SL:%.5f TP:%.5f",
                          OrderTicket(), new_sl, new_tp);
         }
          else
          {
             int err = GetLastError();
             if(err == 1)
             {
                if(m_log_enabled)
                   PrintFormat("[OrderMod] Position #%d SL/TP already at target values", OrderTicket());
                modified_count++;
             }
             else
             {
                if(m_log_enabled)
                   PrintFormat("[OrderMod] Failed to modify position #%d - Error: %d",
                              OrderTicket(), err);
             }
          }
       }

       if(m_log_enabled)
          PrintFormat("[OrderMod] Modified %d %s position(s) for %s",
                    modified_count,
                    pos_type == OP_BUY ? "BUY" : "SELL",
                    symbol);

      return (modified_count > 0);
   }

   //+------------------------------------------------------------------+
   //| Modify pending orders                                          |
   //+------------------------------------------------------------------+
   bool ModifyPendingOrders(string symbol, int order_type, double sl_value, double tp_value, string comment = "")
   {
      RefreshRates();
      int modified_count = 0;
      int total = OrdersTotal();

      for(int i = 0; i < total; i++)
      {
         if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
            continue;

         // Check filters
         if(OrderMagicNumber() != m_magic)
            continue;
         if(OrderSymbol() != symbol)
            continue;
         if(OrderType() != order_type)
            continue;
         // Check comment filter
         if(comment != "")
         {
            if(StringFind(OrderComment(), comment) < 0)
               continue;
         }

          double order_price = OrderOpenPrice();
          double new_sl = OrderStopLoss();
          double new_tp = OrderTakeProfit();

          bool is_buy = (order_type == OP_BUYSTOP || order_type == OP_BUYLIMIT);

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
         int digits = (int)MarketInfo(symbol, MODE_DIGITS);
         new_sl = NormalizeDouble(new_sl, digits);
         new_tp = NormalizeDouble(new_tp, digits);

         // Validate against minimum stop level using ORDER price (not market price)
         if(!ValidateStopLevel(symbol, order_price, new_sl, new_tp))
            continue;

         // Modify order
         if(OrderModify(OrderTicket(), OrderOpenPrice(), new_sl, new_tp, OrderExpiration(), CLR_NONE))
         {
            modified_count++;
            if(m_log_enabled)
               PrintFormat("[OrderMod] Modified order #%d - SL:%.5f TP:%.5f",
                          OrderTicket(), new_sl, new_tp);
         }
          else
          {
             int err = GetLastError();
             if(err == 1)
             {
                if(m_log_enabled)
                   PrintFormat("[OrderMod] Order #%d SL/TP already at target values", OrderTicket());
                modified_count++;
             }
             else
             {
                if(m_log_enabled)
                   PrintFormat("[OrderMod] Failed to modify order #%d - Error: %d",
                              OrderTicket(), err);
             }
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
      double result_price = 0;
      double point = MarketInfo(symbol, MODE_POINT);

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

         case TARGET_TYPE_PERCENTAGE = 2:
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
      int stops_level = (int)MarketInfo(symbol, MODE_STOPLEVEL);
      if(stops_level <= 0)
         return true;  // No restrictions

      double min_distance = stops_level * MarketInfo(symbol, MODE_POINT);

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
   string OrderTypeToString(int type)
   {
      switch(type)
      {
         case OP_BUYSTOP:   return "BUY STOP";
         case OP_BUYLIMIT:  return "BUY LIMIT";
         case OP_SELLSTOP:  return "SELL STOP";
         case OP_SELLLIMIT: return "SELL LIMIT";
         default:           return "UNKNOWN";
      }
   }
};
