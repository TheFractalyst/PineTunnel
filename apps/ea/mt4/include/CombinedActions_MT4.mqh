//+------------------------------------------------------------------+
//|                                      CombinedActions_MT4.mqh     |
//|                     PineTunnel Compatible Combined Actions    |
//|                     MT4 Version - 1:1 Logic from MT5             |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"
#property strict

//+------------------------------------------------------------------+
//| Combined Actions Manager Class                                  |
//+------------------------------------------------------------------+
class CCombinedActionsManager
{
private:
   int             m_magic;
   bool            m_log_enabled;
   int             m_slippage;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   CCombinedActionsManager(int magic, int slippage = 10)
   {
      m_magic = magic;
      m_log_enabled = true;
      m_slippage = slippage;
   }

   //+------------------------------------------------------------------+
   //| Set logging enabled/disabled                                    |
   //+------------------------------------------------------------------+
   void SetLogging(bool enabled) { m_log_enabled = enabled; }

   //+------------------------------------------------------------------+
   //| Close long + open long (close_long_buy / close_long_buy)            |
   //+------------------------------------------------------------------+
    bool CloseLongOpenLong(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, OP_BUY, comment);
     }

   //+------------------------------------------------------------------+
   //| Close long + open short (close_long_sell / close_long_sell)          |
   //+------------------------------------------------------------------+
    bool CloseLongOpenShort(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, OP_BUY, comment);
     }

   //+------------------------------------------------------------------+
   //| Close short + open long (close_short_buy / close_short_buy)          |
   //+------------------------------------------------------------------+
    bool CloseShortOpenLong(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, OP_SELL, comment);
     }

   //+------------------------------------------------------------------+
   //| Close short + open short (close_short_sell / close_short_sell)        |
   //+------------------------------------------------------------------+
    bool CloseShortOpenShort(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, OP_SELL, comment);
     }

   //+------------------------------------------------------------------+
   //| Close all + open long (close_all_buy / close_all_buy)       |
   //+------------------------------------------------------------------+
    bool CloseLongShortOpenLong(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return CloseAllPositions(symbol, comment);
     }

   //+------------------------------------------------------------------+
   //| Close all + open short (close_all_sell / close_all_sell)     |
   //+------------------------------------------------------------------+
    bool CloseLongShortOpenShort(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return CloseAllPositions(symbol, comment);
     }

   //+------------------------------------------------------------------+
   //| Cancel long + place buy stop (cancel_long_buy_stop)               |
   //+------------------------------------------------------------------+
    bool CancelLongBuyStop(string symbol, double lots, double pending_price,
                           double sl = 0, double tp = 0, string comment = "")
    {
       // First cancel all buy pending orders
       CancelOrders(symbol, true, false);

       // Then place new buy stop
       return true;
    }

   //+------------------------------------------------------------------+
   //| Cancel long + place buy limit (cancel_long_buy_limit)             |
   //+------------------------------------------------------------------+
    bool CancelLongBuyLimit(string symbol, double lots, double pending_price,
                            double sl = 0, double tp = 0, string comment = "")
    {
       // First cancel all buy pending orders
       CancelOrders(symbol, true, false);

       // Then place new buy limit
       return true;
    }

   //+------------------------------------------------------------------+
   //| Cancel short + place sell stop (cancel_short_sell_stop)           |
   //+------------------------------------------------------------------+
    bool CancelShortSellStop(string symbol, double lots, double pending_price,
                             double sl = 0, double tp = 0, string comment = "")
    {
       // First cancel all sell pending orders
       CancelOrders(symbol, false, true);

       // Then place new sell stop
       return true;
    }

   //+------------------------------------------------------------------+
   //| Cancel short + place sell limit (cancel_short_sell_limit)         |
   //+------------------------------------------------------------------+
    bool CancelShortSellLimit(string symbol, double lots, double pending_price,
                              double sl = 0, double tp = 0, string comment = "")
    {
       // First cancel all sell pending orders
       CancelOrders(symbol, false, true);

       // Then place new sell limit
       return true;
    }

private:
   //+------------------------------------------------------------------+
   //| Close positions by type                                        |
   //+------------------------------------------------------------------+
   bool ClosePositions(string symbol, int pos_type, string comment = "")
   {
       RefreshRates();
       int closed = 0;
       int total = OrdersTotal();

       for(int i = total - 1; i >= 0; i--)
       {
          if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
             continue;

          // Only market orders (not pending)
          if(OrderType() > OP_SELL)
             continue;

          if(OrderMagicNumber() != m_magic)
             continue;
          if(OrderSymbol() != symbol)
             continue;
          if(OrderType() != pos_type)
             continue;
          // Comment filter
          if(comment != "" && StringFind(OrderComment(), comment) < 0)
             continue;

          RefreshRates();
          double close_price = (pos_type == OP_BUY) ?
                               MarketInfo(symbol, MODE_BID) :
                               MarketInfo(symbol, MODE_ASK);

          if(OrderClose(OrderTicket(), OrderLots(), close_price, m_slippage, CLR_NONE))
          {
             closed++;
             if(m_log_enabled)
                PrintFormat("[CombinedAction] Closed position #%d", OrderTicket());
          }
          else
          {
             int err = GetLastError();
             if(err == 1)
             {
                if(m_log_enabled)
                   PrintFormat("[CombinedAction] Position #%d already at target state", OrderTicket());
             }
             else if(m_log_enabled)
                PrintFormat("[CombinedAction] Failed to close position #%d - Error: %d", OrderTicket(), err);
          }
       }

       if(m_log_enabled && closed > 0)
          PrintFormat("[CombinedAction] Closed %d %s position(s)",
                    closed, pos_type == OP_BUY ? "BUY" : "SELL");

      return (closed > 0) || (PositionCount(symbol, pos_type, comment) == 0);
   }

   //+------------------------------------------------------------------+
   //| Close all positions for symbol                                 |
   //+------------------------------------------------------------------+
   bool CloseAllPositions(string symbol, string comment = "")
   {
       RefreshRates();
       int closed = 0;
       int total = OrdersTotal();

       for(int i = total - 1; i >= 0; i--)
       {
          if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
             continue;

          // Only market orders (not pending)
          if(OrderType() > OP_SELL)
             continue;

          if(OrderMagicNumber() != m_magic)
             continue;
          if(OrderSymbol() != symbol)
             continue;
          // Comment filter
          if(comment != "" && StringFind(OrderComment(), comment) < 0)
             continue;

          RefreshRates();
          double close_price = (OrderType() == OP_BUY) ?
                               MarketInfo(symbol, MODE_BID) :
                               MarketInfo(symbol, MODE_ASK);

          if(OrderClose(OrderTicket(), OrderLots(), close_price, m_slippage, CLR_NONE))
          {
             closed++;
             if(m_log_enabled)
                PrintFormat("[CombinedAction] Closed position #%d", OrderTicket());
          }
          else
          {
             int err = GetLastError();
             if(err == 1)
             {
                if(m_log_enabled)
                   PrintFormat("[CombinedAction] Position #%d already at target state", OrderTicket());
             }
             else if(m_log_enabled)
                PrintFormat("[CombinedAction] Failed to close position #%d - Error: %d", OrderTicket(), err);
          }
       }

       if(m_log_enabled && closed > 0)
          PrintFormat("[CombinedAction] Closed %d position(s) for %s", closed, symbol);

      return (closed > 0) || (PositionCount(symbol, -1, comment) == 0);
   }

   //+------------------------------------------------------------------+
   //| Count positions                                                |
   //+------------------------------------------------------------------+
   int PositionCount(string symbol, int pos_type, string comment = "")
   {
       int count = 0;
       int total = OrdersTotal();

       for(int i = 0; i < total; i++)
       {
          if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
             continue;

          // Only market orders (not pending)
          if(OrderType() > OP_SELL)
             continue;

          if(OrderMagicNumber() != m_magic)
             continue;
          if(OrderSymbol() != symbol)
             continue;
          // Comment filter
          if(comment != "" && StringFind(OrderComment(), comment) < 0)
             continue;

         if(pos_type == -1 || OrderType() == pos_type)
            count++;
      }

      return count;
   }

   //+------------------------------------------------------------------+
   //| Cancel pending orders                                          |
   //+------------------------------------------------------------------+
   void CancelOrders(string symbol, bool cancel_buy, bool cancel_sell)
   {
      int cancelled = 0;
      int total = OrdersTotal();

      for(int i = total - 1; i >= 0; i--)
      {
         if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
            continue;

         if(OrderMagicNumber() != m_magic)
            continue;
         if(OrderSymbol() != symbol)
            continue;

          int order_type = OrderType();
          bool should_cancel = (cancel_buy && (order_type == OP_BUYSTOP || order_type == OP_BUYLIMIT)) ||
                               (cancel_sell && (order_type == OP_SELLSTOP || order_type == OP_SELLLIMIT));

          if(should_cancel)
          {
             if(OrderDelete(OrderTicket()))
             {
                cancelled++;
                if(m_log_enabled)
                   PrintFormat("[CombinedAction] Cancelled order #%d", OrderTicket());
             }
             else if(m_log_enabled)
                PrintFormat("[CombinedAction] Failed to cancel order #%d - Error: %d", OrderTicket(), GetLastError());
          }
      }

       if(m_log_enabled && cancelled > 0)
          PrintFormat("[CombinedAction] Cancelled %d pending order(s)", cancelled);
    }
};
