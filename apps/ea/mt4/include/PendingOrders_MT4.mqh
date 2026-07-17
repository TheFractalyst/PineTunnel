//+------------------------------------------------------------------+
//|                                          PendingOrders_MT4.mqh   |
//|                     PineTunnel Compatible Pending Orders      |
//|                     MT4 Version - 1:1 Logic from MT5             |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"
#property strict

#define PIPS_TO_POINTS          10
#define PRICE_TOLERANCE_POINTS  5
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
   int          m_magic;
   int          m_slippage;
   bool         m_log_enabled;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   CPendingOrderManager(int magic, int slippage = 10)
   {
      m_magic = magic;
      m_slippage = slippage;
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
      return PlacePendingOrder(OP_BUYLIMIT, symbol, lots,
                              pending_value, pending_type, sl_points, tp_points, comment);
   }

   //+------------------------------------------------------------------+
   //| Place Sell Limit Order                                         |
   //+------------------------------------------------------------------+
   bool PlaceSellLimit(string symbol, double lots, double pending_value,
                       ENUM_PENDING_TYPE pending_type, double sl_points = 0,
                       double tp_points = 0, string comment = "")
   {
      return PlacePendingOrder(OP_SELLLIMIT, symbol, lots,
                              pending_value, pending_type, sl_points, tp_points, comment);
   }

   //+------------------------------------------------------------------+
   //| Place Buy Stop Order                                           |
   //+------------------------------------------------------------------+
   bool PlaceBuyStop(string symbol, double lots, double pending_value,
                     ENUM_PENDING_TYPE pending_type, double sl_points = 0,
                     double tp_points = 0, string comment = "")
   {
      return PlacePendingOrder(OP_BUYSTOP, symbol, lots,
                              pending_value, pending_type, sl_points, tp_points, comment);
   }

   //+------------------------------------------------------------------+
   //| Place Sell Stop Order                                          |
   //+------------------------------------------------------------------+
   bool PlaceSellStop(string symbol, double lots, double pending_value,
                      ENUM_PENDING_TYPE pending_type, double sl_points = 0,
                      double tp_points = 0, string comment = "")
   {
      return PlacePendingOrder(OP_SELLSTOP, symbol, lots,
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
   bool PlacePendingOrder(int order_type, string symbol, double lots,
                          double pending_value, ENUM_PENDING_TYPE pending_type,
                          double sl_points, double tp_points, string comment)
   {
       // Validate symbol exists and refresh prices
       double ask = MarketInfo(symbol, MODE_ASK);
       double bid = MarketInfo(symbol, MODE_BID);
       if(ask <= 0 || bid <= 0)
       {
          PrintFormat("[PendingOrder] ERROR: Failed to select symbol %s", symbol);
          return false;
       }
       RefreshRates();

       ask = MarketInfo(symbol, MODE_ASK);
       bid = MarketInfo(symbol, MODE_BID);
      double point = MarketInfo(symbol, MODE_POINT);
      int digits = (int)MarketInfo(symbol, MODE_DIGITS);

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

      if(sl_points > 0)
      {
         double sl_distance = sl_points * point * PIPS_TO_POINTS;
         if(order_type == OP_BUYLIMIT || order_type == OP_BUYSTOP)
            sl_price = entry_price - sl_distance;
         else
            sl_price = entry_price + sl_distance;

         sl_price = NormalizeDouble(sl_price, digits);
      }

      if(tp_points > 0)
      {
         double tp_distance = tp_points * point * PIPS_TO_POINTS;
         if(order_type == OP_BUYLIMIT || order_type == OP_BUYSTOP)
            tp_price = entry_price + tp_distance;
         else
            tp_price = entry_price - tp_distance;

         tp_price = NormalizeDouble(tp_price, digits);
      }

      // Check minimum stop level (directional: BUY from ask, SELL from bid)
      int stops_level = (int)MarketInfo(symbol, MODE_STOPLEVEL);
      if(stops_level > 0)
      {
         double min_distance = stops_level * point;
         double current_distance;
         if(order_type == OP_BUYLIMIT || order_type == OP_BUYSTOP)
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

       // Normalize lot size
       double min_lot = MarketInfo(symbol, MODE_MINLOT);
       double max_lot = MarketInfo(symbol, MODE_MAXLOT);
       double lot_step = MarketInfo(symbol, MODE_LOTSTEP);
       if(lot_step > 0) lots = MathFloor(lots / lot_step + 0.0000001) * lot_step;
       if(lots < min_lot) lots = min_lot;
       if(max_lot > 0 && lots > max_lot) lots = max_lot;

       // Place the order using OrderSend
       int ticket = OrderSend(
         symbol,
         order_type,
         lots,
         entry_price,
         m_slippage,
         sl_price,
         tp_price,
         comment,
         m_magic,
         0,           // expiration
         CLR_NONE      // arrow color
      );

      if(ticket > 0)
      {
         // Verify and fix SL/TP after pending order placement
         if(sl_price > 0 || tp_price > 0)
         {
            if(OrderSelect(ticket, SELECT_BY_TICKET))
            {
               double current_sl = OrderStopLoss();
               double current_tp = OrderTakeProfit();
               double tolerance = point * PRICE_TOLERANCE_POINTS;

               bool sl_ok = (sl_price <= 0) || (MathAbs(current_sl - sl_price) < tolerance);
               bool tp_ok = (tp_price <= 0) || (MathAbs(current_tp - tp_price) < tolerance);

               if(!sl_ok || !tp_ok)
               {
                  PrintFormat("[PendingOrder] SL/TP MISMATCH for order #%d", ticket);
                  PrintFormat("[PendingOrder] Expected: SL=%.5f TP=%.5f | Actual: SL=%.5f TP=%.5f",
                              sl_price, tp_price, current_sl, current_tp);

                  bool fixed = false;
                  for(int retry = 0; retry < SL_TP_VERIFY_RETRIES; retry++)
                  {
                     if(OrderModify(ticket, OrderOpenPrice(), sl_price, tp_price, OrderExpiration(), CLR_NONE))
                     {
                        PrintFormat("[PendingOrder] SL/TP FIXED on retry %d", retry + 1);
                        fixed = true;
                        break;
                     }
                     PrintFormat("[PendingOrder] Retry %d/%d failed: %d - %s",
                                 retry + 1, SL_TP_VERIFY_RETRIES, GetLastError(), ErrorDescription(GetLastError()));
                     Sleep(100);
                  }
                  if(!fixed)
                  {
                     PrintFormat("[PendingOrder] CRITICAL: Could not set SL/TP after %d retries!", SL_TP_VERIFY_RETRIES);
                     PrintFormat("[PendingOrder] Order #%d may have NO STOP LOSS protection!", ticket);
                  }
               }
            }
         }

         return true;
      }
      else
      {
         int error = GetLastError();
         if(m_log_enabled)
         {
            PrintFormat("[PendingOrder] Failed to place %s order - Error %d: %s",
                       OrderTypeToString(order_type), error, ErrorDescription(error));
         }
         return false;
      }
   }

   //+------------------------------------------------------------------+
   //| Calculate entry price based on pending type                    |
   //+------------------------------------------------------------------+
   double CalculateEntryPrice(int order_type, double pending_value,
                              ENUM_PENDING_TYPE pending_type, double ask, double bid, double point)
   {
      double entry_price = 0;
      double ref_price = 0;

      // Determine reference price
      if(order_type == OP_BUYLIMIT || order_type == OP_BUYSTOP)
         ref_price = ask;
      else
         ref_price = bid;

      // Calculate based on pending type
      switch(pending_type)
      {
         case PENDING_PIPS:
            {
               double distance = pending_value * point * PIPS_TO_POINTS;

               if(order_type == OP_BUYLIMIT)
                  entry_price = ref_price - distance;  // Buy limit below ask
               else if(order_type == OP_SELLLIMIT)
                  entry_price = ref_price + distance;  // Sell limit above bid
               else if(order_type == OP_BUYSTOP)
                  entry_price = ref_price + distance;  // Buy stop above ask
               else if(order_type == OP_SELLSTOP)
                  entry_price = ref_price - distance;  // Sell stop below bid

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

               if(order_type == OP_BUYLIMIT)
                  entry_price = ref_price - distance;
               else if(order_type == OP_SELLLIMIT)
                  entry_price = ref_price + distance;
               else if(order_type == OP_BUYSTOP)
                  entry_price = ref_price + distance;
               else if(order_type == OP_SELLSTOP)
                  entry_price = ref_price - distance;

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
   bool ValidateEntryPrice(int order_type, double entry_price, double ask, double bid)
   {
      switch(order_type)
      {
         case OP_BUYLIMIT:
            return entry_price < ask;  // Must be below current ask

         case OP_SELLLIMIT:
            return entry_price > bid;  // Must be above current bid

         case OP_BUYSTOP:
            return entry_price > ask;  // Must be above current ask

         case OP_SELLSTOP:
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
          if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
             continue;

          // Check magic number
          if(OrderMagicNumber() != m_magic)
             continue;

          // Check symbol
          if(symbol != "" && OrderSymbol() != symbol)
             continue;

          // Check comment filter
          if(comment != "" && StringFind(OrderComment(), comment) < 0)
             continue;

          // Check order type (only pending orders)
          int order_type = OrderType();
          bool should_cancel = (cancel_buy && (order_type == OP_BUYSTOP || order_type == OP_BUYLIMIT)) ||
                               (cancel_sell && (order_type == OP_SELLSTOP || order_type == OP_SELLLIMIT));

          if(should_cancel)
          {
             if(OrderDelete(OrderTicket()))
             {
                cancelled++;
                if(m_log_enabled)
                   PrintFormat("[PendingOrder] Cancelled order #%d (%s %s)",
                              OrderTicket(), OrderTypeToString(order_type), OrderSymbol());
             }
             else if(m_log_enabled)
                PrintFormat("[PendingOrder] Failed to cancel order #%d - Error: %d", OrderTicket(), GetLastError());
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
   string OrderTypeToString(int type)
   {
      switch(type)
      {
         case OP_BUYLIMIT:  return "BUY LIMIT";
         case OP_SELLLIMIT: return "SELL LIMIT";
         case OP_BUYSTOP:   return "BUY STOP";
         case OP_SELLSTOP:  return "SELL STOP";
         default:           return "UNKNOWN";
      }
   }

   //+------------------------------------------------------------------+
   //| Error description helper                                        |
   //+------------------------------------------------------------------+
   string ErrorDescription(int error_code)
   {
      switch(error_code)
      {
         case 0:    return "No error";
         case 1:    return "No error but result unknown";
         case 2:    return "Common error";
         case 3:    return "Invalid trade parameters";
         case 4:    return "Trade server is busy";
         case 5:    return "Old version of client terminal";
         case 6:    return "No connection with trade server";
         case 7:    return "Not enough rights";
         case 8:    return "Too frequent requests";
         case 9:    return "Malfunctional trade operation";
         case 64:   return "Account disabled";
         case 65:   return "Invalid account";
         case 128:  return "Trade timeout";
         case 129:  return "Invalid price";
         case 130:  return "Invalid stops";
         case 131:  return "Invalid trade volume";
         case 132:  return "Market is closed";
         case 133:  return "Trade is disabled";
         case 134:  return "Not enough money";
         case 135:  return "Price changed";
         case 136:  return "Off quotes";
         case 137:  return "Broker is busy";
         case 138:  return "Requote";
         case 139:  return "Order is locked";
         case 140:  return "Long positions only allowed";
         case 141:  return "Too many requests";
         case 145:  return "Modification denied";
         case 146:  return "Trade context is busy";
         case 147:  return "Expirations are denied by broker";
         case 148:  return "Too many orders";
         default:   return "Error " + IntegerToString(error_code);
      }
   }
};
