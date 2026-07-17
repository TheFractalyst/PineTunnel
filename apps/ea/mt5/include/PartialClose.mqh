//+------------------------------------------------------------------+
//|                                              PartialClose.mqh    |
//|                     PineTunnel Compatible Partial Close       |
//|                                                                  |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"

#include <Trade\PositionInfo.mqh>
#include <Trade\SymbolInfo.mqh>
#include <Trade\Trade.mqh>

// Partial close: close volume if >= 99% of position volume (rounding tolerance)
#define FULL_CLOSE_THRESHOLD  0.99
// Delay after partial close for server synchronization (ms)
#define SERVER_SYNC_DELAY_MS  100

//+------------------------------------------------------------------+
//| Partial Close Manager Class                                     |
//+------------------------------------------------------------------+
class CPartialCloseManager
{
private:
   CTrade*         m_trade;
   CPositionInfo   m_position;
   CSymbolInfo     m_symbol;
   int             m_magic;
   double          m_partial_close_pct;  // Default percentage for close_long_pct/close_short_pct
   bool            m_log_enabled;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   CPartialCloseManager(CTrade* trade, int magic, double default_pct = 50.0)
   {
      m_trade = trade;
      m_magic = magic;
      m_partial_close_pct = default_pct;
      m_log_enabled = true;
   }

   //+------------------------------------------------------------------+
   //| Set logging enabled/disabled                                    |
   //+------------------------------------------------------------------+
   void SetLogging(bool enabled) { m_log_enabled = enabled; }

   //+------------------------------------------------------------------+
   //| Close percentage of long positions (close_long_pct)              |
   //+------------------------------------------------------------------+
   bool CloseLongPercentage(string symbol, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByPercentage(symbol, POSITION_TYPE_BUY,
                                        m_partial_close_pct, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close percentage of short positions (close_short_pct)            |
   //+------------------------------------------------------------------+
   bool CloseShortPercentage(string symbol, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByPercentage(symbol, POSITION_TYPE_SELL,
                                        m_partial_close_pct, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close long positions by specific percentage                     |
   //+------------------------------------------------------------------+
   bool CloseLongByPercentage(string symbol, double percentage, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByPercentage(symbol, POSITION_TYPE_BUY,
                                        percentage, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close short positions by specific percentage                    |
   //+------------------------------------------------------------------+
   bool CloseShortByPercentage(string symbol, double percentage, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByPercentage(symbol, POSITION_TYPE_SELL,
                                        percentage, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close specific volume of long positions (close_long_vol)         |
   //+------------------------------------------------------------------+
   bool CloseLongVolume(string symbol, double volume_to_close, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByVolume(symbol, POSITION_TYPE_BUY,
                                    volume_to_close, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close specific volume of short positions (close_short_vol)       |
   //+------------------------------------------------------------------+
   bool CloseShortVolume(string symbol, double volume_to_close, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByVolume(symbol, POSITION_TYPE_SELL,
                                    volume_to_close, move_to_breakeven, comment);
   }

private:
   //+------------------------------------------------------------------+
   //| Close positions by percentage                                  |
   //+------------------------------------------------------------------+
   bool ClosePositionsByPercentage(string symbol, ENUM_POSITION_TYPE pos_type,
                                   double percentage, bool move_to_breakeven, string comment = "")
   {
      // First calculate total volume
      double total_volume = 0;
      int position_count = 0;

      int total = PositionsTotal();
      for(int i = 0; i < total; i++)
      {
         if(!m_position.SelectByIndex(i))
            continue;

         if(m_position.Magic() != m_magic)
            continue;
         if(symbol != "" && m_position.Symbol() != symbol)
            continue;
         if(m_position.PositionType() != pos_type)
            continue;
          if(comment != "" && StringFind(m_position.Comment(), comment) < 0)
             continue;

          total_volume += m_position.Volume();
         position_count++;
      }

      if(position_count == 0)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] No %s positions found for %s",
                       pos_type == POSITION_TYPE_BUY ? "BUY" : "SELL", symbol);
         return false;
      }

      // Calculate volume to close
      double volume_to_close = total_volume * (percentage / 100.0);

      if(m_log_enabled)
         PrintFormat("[PartialClose] Total volume: %.2f, Closing %.1f%% = %.2f lots",
                    total_volume, percentage, volume_to_close);

      // Now close positions using FIFO
      return ClosePositionsByVolume(symbol, pos_type, volume_to_close, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close positions by specific volume (FIFO)                      |
   //+------------------------------------------------------------------+
   bool ClosePositionsByVolume(string symbol, ENUM_POSITION_TYPE pos_type,
                               double volume_to_close, bool move_to_breakeven, string comment = "")
   {
      if(volume_to_close <= 0)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] ERROR: Invalid volume to close: %.2f", volume_to_close);
         return false;
      }

      // Collect positions (for FIFO sorting)
      struct PositionData
      {
         ulong    ticket;
         datetime open_time;
         double   volume;
         double   entry_price;
      };

      PositionData positions[];
      int pos_count = 0;

      // Collect all matching positions
      int total = PositionsTotal();
      for(int i = 0; i < total; i++)
      {
         if(!m_position.SelectByIndex(i))
            continue;

         if(m_position.Magic() != m_magic)
            continue;
         if(symbol != "" && m_position.Symbol() != symbol)
            continue;
         if(m_position.PositionType() != pos_type)
            continue;
          if(comment != "" && StringFind(m_position.Comment(), comment) < 0)
             continue;

          ArrayResize(positions, pos_count + 1);
         positions[pos_count].ticket = m_position.Ticket();
         positions[pos_count].open_time = m_position.Time();
         positions[pos_count].volume = m_position.Volume();
         positions[pos_count].entry_price = m_position.PriceOpen();
         pos_count++;
      }

      if(pos_count == 0)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] No positions found to close");
         return false;
      }

      // Sort by open time (FIFO)
      for(int i = 0; i < pos_count - 1; i++)
      {
         for(int j = i + 1; j < pos_count; j++)
         {
            if(positions[j].open_time < positions[i].open_time)
            {
               PositionData temp = positions[i];
               positions[i] = positions[j];
               positions[j] = temp;
            }
         }
      }

      // Close positions in FIFO order
      double remaining_to_close = volume_to_close;
      int closed_count = 0;
      int modified_count = 0;

      for(int i = 0; i < pos_count && remaining_to_close > 0; i++)
      {
         if(m_position.SelectByTicket(positions[i].ticket))
         {
            double pos_volume = positions[i].volume;
            double close_volume = MathMin(remaining_to_close, pos_volume);

            // Normalize volume using position's actual symbol
            string pos_symbol = m_position.Symbol();
            if(m_symbol.Name(pos_symbol))
            {
               double lot_step = m_symbol.LotsStep();
               double min_lot = m_symbol.LotsMin();
               if(lot_step > 0)
               {
                  close_volume = MathFloor(close_volume / lot_step) * lot_step;
                  int vol_digits = (int)MathCeil(-MathLog10(lot_step));
                  if(vol_digits < 0) vol_digits = 0;
                  close_volume = NormalizeDouble(close_volume, vol_digits);
               }
               if(close_volume < min_lot)
                  continue;
            }

            if(close_volume <= 0)
               continue;

            bool result = false;

            if(close_volume >= pos_volume * FULL_CLOSE_THRESHOLD) // Account for rounding
            {
               // Full close
               result = m_trade.PositionClose(positions[i].ticket);
               if(result)
               {
                  if(m_log_enabled)
                     PrintFormat("[PartialClose] Fully closed position #%I64d (%.2f lots)",
                                 positions[i].ticket, pos_volume);
                  closed_count++;
                  remaining_to_close -= pos_volume;
               }
            }
            else
            {
               // Partial close
               result = m_trade.PositionClosePartial(positions[i].ticket, close_volume);
               if(result)
               {
                  if(m_log_enabled)
                     PrintFormat("[PartialClose] Partially closed position #%I64d (%.2f of %.2f lots)",
                                 positions[i].ticket, close_volume, pos_volume);
                  closed_count++;
                  remaining_to_close -= close_volume;

                  // Move remaining to breakeven if requested
                  if(move_to_breakeven)
                  {
                     Sleep(SERVER_SYNC_DELAY_MS);
                     if(m_position.SelectByTicket(positions[i].ticket))
                     {
                        if(MoveToBreakeven(positions[i].ticket, positions[i].entry_price, pos_type))
                           modified_count++;
                     }
                  }
               }
            }

            if(!result)
            {
               if(m_log_enabled)
                  PrintFormat("[PartialClose] Failed to close position #%I64d - Error: %d",
                              positions[i].ticket, m_trade.ResultRetcode());
            }
         }
      }

      // Move remaining positions to breakeven if requested
      if(move_to_breakeven && remaining_to_close <= 0)
      {
         for(int i = 0; i < pos_count; i++)
         {
            if(m_position.SelectByTicket(positions[i].ticket))
            {
               if(MoveToBreakeven(positions[i].ticket, positions[i].entry_price, pos_type))
                  modified_count++;
            }
         }
      }

      if(m_log_enabled)
      {
         PrintFormat("[PartialClose] Closed %d position(s), Volume closed: %.2f",
                    closed_count, volume_to_close - remaining_to_close);
         if(move_to_breakeven && modified_count > 0)
            PrintFormat("[PartialClose] Moved %d position(s) to breakeven", modified_count);
      }

      return (closed_count > 0);
   }

   //+------------------------------------------------------------------+
   //| Move position to breakeven                                     |
   //+------------------------------------------------------------------+
   bool MoveToBreakeven(ulong ticket, double entry_price, ENUM_POSITION_TYPE pos_type)
   {
      if(!m_position.SelectByTicket(ticket))
         return false;

      // PriceCurrent() returns Bid for BUY positions, Ask for SELL positions (broker standard)
      double current_price = m_position.PriceCurrent();

      bool in_profit = (pos_type == POSITION_TYPE_BUY) ?
                       (current_price > entry_price) :
                       (current_price < entry_price);

      if(!in_profit)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Position #%I64d not in profit, cannot move to BE", ticket);
         return false;
      }

      // Check minimum stop level
      string pos_symbol = m_position.Symbol();
      long stops_level = SymbolInfoInteger(pos_symbol, SYMBOL_TRADE_STOPS_LEVEL);
      double min_distance = stops_level * SymbolInfoDouble(pos_symbol, SYMBOL_POINT);

      double distance = MathAbs(current_price - entry_price);
      if(distance < min_distance)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Position #%I64d too close to entry for BE (%.5f < %.5f)",
                        ticket, distance, min_distance);
         return false;
      }

      // Modify position
      bool result = m_trade.PositionModify(
         ticket,
         NormalizeDouble(entry_price, (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS)),
         m_position.TakeProfit()
      );

      if(result)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Moved position #%I64d to breakeven", ticket);
      }
      else
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Failed to move #%I64d to BE - Error: %d",
                        ticket, m_trade.ResultRetcode());
      }

      return result;
   }
};
