//+------------------------------------------------------------------+
//|                                          CombinedActions.mqh     |
//|                     PineTunnel Compatible Combined Actions    |
//|                                                                  |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"

#include <Trade\OrderInfo.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\Trade.mqh>

// Sentinel value for "all position types" in CloseAllPositions/PositionCount
#define ALL_POSITION_TYPES  ((ENUM_POSITION_TYPE)-1)

//+------------------------------------------------------------------+
//| Combined Actions Manager Class                                  |
//+------------------------------------------------------------------+
class CCombinedActionsManager
{
private:
   CTrade*         m_trade;
   COrderInfo      m_order;
   CPositionInfo   m_position;
   int             m_magic;
   bool            m_log_enabled;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   CCombinedActionsManager(CTrade* trade, int magic)
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
   //| Close long + open long (close_long_buy / close_long_buy)            |
   //+------------------------------------------------------------------+
    bool CloseLongOpenLong(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, POSITION_TYPE_BUY, comment) ||
              PositionCount(symbol, POSITION_TYPE_BUY, comment) == 0;
    }

   //+------------------------------------------------------------------+
   //| Close long + open short (close_long_sell / close_long_sell)          |
   //+------------------------------------------------------------------+
    bool CloseLongOpenShort(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, POSITION_TYPE_BUY, comment) ||
              PositionCount(symbol, POSITION_TYPE_BUY, comment) == 0;
    }

   //+------------------------------------------------------------------+
   //| Close short + open long (close_short_buy / close_short_buy)          |
   //+------------------------------------------------------------------+
    bool CloseShortOpenLong(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, POSITION_TYPE_SELL, comment) ||
              PositionCount(symbol, POSITION_TYPE_SELL, comment) == 0;
    }

   //+------------------------------------------------------------------+
   //| Close short + open short (close_short_sell / close_short_sell)        |
   //+------------------------------------------------------------------+
    bool CloseShortOpenShort(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return ClosePositions(symbol, POSITION_TYPE_SELL, comment) ||
              PositionCount(symbol, POSITION_TYPE_SELL, comment) == 0;
    }

   //+------------------------------------------------------------------+
   //| Close all + open long (close_all_buy / close_all_buy)       |
   //+------------------------------------------------------------------+
    bool CloseLongShortOpenLong(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return CloseAllPositions(symbol, comment) ||
              PositionCount(symbol, ALL_POSITION_TYPES, comment) == 0;
    }

   //+------------------------------------------------------------------+
   //| Close all + open short (close_all_sell / close_all_sell)     |
   //+------------------------------------------------------------------+
    bool CloseLongShortOpenShort(string symbol, double lots, double sl = 0, double tp = 0, string comment = "")
    {
       return CloseAllPositions(symbol, comment) ||
              PositionCount(symbol, ALL_POSITION_TYPES, comment) == 0;
    }

   //+------------------------------------------------------------------+
   //| Cancel long + place buy stop (cancel_long_buy_stop)               |
   //+------------------------------------------------------------------+
   bool CancelLongBuyStop(string symbol, double lots, double pending_price,
                          double sl = 0, double tp = 0, string comment = "")
   {
      CancelOrders(symbol, true, false);

      return true;
   }

   //+------------------------------------------------------------------+
   //| Cancel long + place buy limit (cancel_long_buy_limit)             |
   //+------------------------------------------------------------------+
   bool CancelLongBuyLimit(string symbol, double lots, double pending_price,
                           double sl = 0, double tp = 0, string comment = "")
   {
      CancelOrders(symbol, true, false);

      return true;
   }

   //+------------------------------------------------------------------+
   //| Cancel short + place sell stop (cancel_short_sell_stop)           |
   //+------------------------------------------------------------------+
   bool CancelShortSellStop(string symbol, double lots, double pending_price,
                            double sl = 0, double tp = 0, string comment = "")
   {
      CancelOrders(symbol, false, true);

      return true;
   }

   //+------------------------------------------------------------------+
   //| Cancel short + place sell limit (cancel_short_sell_limit)         |
   //+------------------------------------------------------------------+
   bool CancelShortSellLimit(string symbol, double lots, double pending_price,
                             double sl = 0, double tp = 0, string comment = "")
   {
      CancelOrders(symbol, false, true);

      return true;
   }

private:
   //+------------------------------------------------------------------+
   //| Close positions by type                                        |
   //+------------------------------------------------------------------+
   bool ClosePositions(string symbol, ENUM_POSITION_TYPE pos_type, string comment = "")
   {
      int closed = 0;
      int total = PositionsTotal();

       for(int i = total - 1; i >= 0; i--)
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

          if(m_trade.PositionClose(m_position.Ticket()))
          {
             closed++;
             if(m_log_enabled)
                PrintFormat("[CombinedAction] Closed position #%I64d", m_position.Ticket());
          }
       }

       if(m_log_enabled && closed > 0)
          PrintFormat("[CombinedAction] Closed %d %s position(s)",
                      closed, pos_type == POSITION_TYPE_BUY ? "BUY" : "SELL");

      return (closed > 0) || (PositionCount(symbol, pos_type, comment) == 0);
   }

   //+------------------------------------------------------------------+
   //| Close all positions for symbol                                 |
   //+------------------------------------------------------------------+
   bool CloseAllPositions(string symbol, string comment = "")
   {
      int closed = 0;
      int total = PositionsTotal();

       for(int i = total - 1; i >= 0; i--)
       {
          if(!m_position.SelectByIndex(i))
             continue;

          if(m_position.Magic() != m_magic)
             continue;
          if(m_position.Symbol() != symbol)
             continue;
          if(comment != "" && StringFind(m_position.Comment(), comment) < 0)
             continue;

          if(m_trade.PositionClose(m_position.Ticket()))
          {
             closed++;
             if(m_log_enabled)
                PrintFormat("[CombinedAction] Closed position #%I64d", m_position.Ticket());
          }
       }

       if(m_log_enabled && closed > 0)
          PrintFormat("[CombinedAction] Closed %d position(s) for %s", closed, symbol);

      return (closed > 0) || (PositionCount(symbol, ALL_POSITION_TYPES, comment) == 0);
   }

   //+------------------------------------------------------------------+
   //| Count positions                                                |
   //+------------------------------------------------------------------+
   int PositionCount(string symbol, ENUM_POSITION_TYPE pos_type, string comment = "")
   {
      int count = 0;
      int total = PositionsTotal();

       for(int i = 0; i < total; i++)
       {
          if(!m_position.SelectByIndex(i))
             continue;

          if(m_position.Magic() != m_magic)
             continue;
          if(m_position.Symbol() != symbol)
             continue;
          if(comment != "" && StringFind(m_position.Comment(), comment) < 0)
             continue;

         if(pos_type == ALL_POSITION_TYPES || m_position.PositionType() == pos_type)
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
         if(!m_order.SelectByIndex(i))
            continue;

         if(m_order.Magic() != m_magic)
            continue;
         if(m_order.Symbol() != symbol)
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
                  PrintFormat("[CombinedAction] Cancelled order #%I64d", m_order.Ticket());
            }
         }
      }

      if(m_log_enabled && cancelled > 0)
         PrintFormat("[CombinedAction] Cancelled %d pending order(s)", cancelled);
   }
};
