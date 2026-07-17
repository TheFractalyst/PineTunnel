//+------------------------------------------------------------------+
//|                                          PartialClose_MT4.mqh    |
//|                     PineTunnel Compatible Partial Close       |
//|                     MT4 Version - 1:1 Logic from MT5             |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"
#property strict

#define FULL_CLOSE_THRESHOLD  0.99
#define SERVER_SYNC_DELAY_MS  100

//+------------------------------------------------------------------+
//| Partial Close Manager Class                                     |
//+------------------------------------------------------------------+
class CPartialCloseManager
{
private:
   int             m_magic;
   double          m_partial_close_pct;  // Default percentage for close_long_pct/close_short_pct
   bool            m_log_enabled;
   int             m_slippage;

public:
   //+------------------------------------------------------------------+
   //| Constructor                                                      |
   //+------------------------------------------------------------------+
   CPartialCloseManager(int magic, double default_pct = 50.0, int slippage = 10)
   {
      m_magic = magic;
      m_partial_close_pct = default_pct;
      m_log_enabled = true;
      m_slippage = slippage;
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
      return ClosePositionsByPercentage(symbol, OP_BUY,
                                        m_partial_close_pct, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close percentage of short positions (close_short_pct)            |
   //+------------------------------------------------------------------+
   bool CloseShortPercentage(string symbol, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByPercentage(symbol, OP_SELL,
                                        m_partial_close_pct, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close long positions by specific percentage                     |
   //+------------------------------------------------------------------+
   bool CloseLongByPercentage(string symbol, double percentage, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByPercentage(symbol, OP_BUY,
                                        percentage, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close short positions by specific percentage                    |
   //+------------------------------------------------------------------+
   bool CloseShortByPercentage(string symbol, double percentage, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByPercentage(symbol, OP_SELL,
                                        percentage, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close specific volume of long positions (close_long_vol)         |
   //+------------------------------------------------------------------+
   bool CloseLongVolume(string symbol, double volume_to_close, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByVolume(symbol, OP_BUY,
                                    volume_to_close, move_to_breakeven, comment);
   }

   //+------------------------------------------------------------------+
   //| Close specific volume of short positions (close_short_vol)       |
   //+------------------------------------------------------------------+
   bool CloseShortVolume(string symbol, double volume_to_close, bool move_to_breakeven = false, string comment = "")
   {
      return ClosePositionsByVolume(symbol, OP_SELL,
                                    volume_to_close, move_to_breakeven, comment);
   }

private:
   //+------------------------------------------------------------------+
   //| Close positions by percentage                                  |
   //+------------------------------------------------------------------+
   bool ClosePositionsByPercentage(string symbol, int pos_type,
                                   double percentage, bool move_to_breakeven, string comment = "")
   {
      // First calculate total volume
      double total_volume = 0;
      int position_count = 0;

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
         if(symbol != "" && OrderSymbol() != symbol)
            continue;
         if(OrderType() != pos_type)
            continue;
         // Check comment filter
         if(comment != "")
         {
            if(StringFind(OrderComment(), comment) < 0)
               continue;
         }

         total_volume += OrderLots();
         position_count++;
      }

      if(position_count == 0)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] No %s positions found for %s",
                       pos_type == OP_BUY ? "BUY" : "SELL", symbol);
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
   bool ClosePositionsByVolume(string symbol, int pos_type,
                               double volume_to_close, bool move_to_breakeven, string comment = "")
   {
      if(volume_to_close <= 0)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] ERROR: Invalid volume to close: %.2f", volume_to_close);
         return false;
      }

      // Collect positions (for FIFO sorting)
      int tickets[];
      datetime open_times[];
      double volumes[];
      double entry_prices[];
      int pos_count = 0;

      // Collect all matching positions
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
         if(symbol != "" && OrderSymbol() != symbol)
            continue;
         if(OrderType() != pos_type)
            continue;
         // Check comment filter
         if(comment != "")
         {
            if(StringFind(OrderComment(), comment) < 0)
               continue;
         }

         ArrayResize(tickets, pos_count + 1);
         ArrayResize(open_times, pos_count + 1);
         ArrayResize(volumes, pos_count + 1);
         ArrayResize(entry_prices, pos_count + 1);

         tickets[pos_count] = OrderTicket();
         open_times[pos_count] = OrderOpenTime();
         volumes[pos_count] = OrderLots();
         entry_prices[pos_count] = OrderOpenPrice();
         pos_count++;
      }

      if(pos_count == 0)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] No positions found to close");
         return false;
      }

      // Sort by open time (FIFO) - bubble sort
      for(int i = 0; i < pos_count - 1; i++)
      {
         for(int j = i + 1; j < pos_count; j++)
         {
            if(open_times[j] < open_times[i])
            {
               // Swap all arrays
               int tmp_ticket = tickets[i];
               tickets[i] = tickets[j];
               tickets[j] = tmp_ticket;

               datetime tmp_time = open_times[i];
               open_times[i] = open_times[j];
               open_times[j] = tmp_time;

               double tmp_vol = volumes[i];
               volumes[i] = volumes[j];
               volumes[j] = tmp_vol;

               double tmp_price = entry_prices[i];
               entry_prices[i] = entry_prices[j];
               entry_prices[j] = tmp_price;
            }
         }
      }

      // Close positions in FIFO order
      RefreshRates();
      double remaining_to_close = volume_to_close;
      int closed_count = 0;
      int modified_count = 0;

      for(int i = 0; i < pos_count && remaining_to_close > 0; i++)
      {
         if(OrderSelect(tickets[i], SELECT_BY_TICKET))
         {
            double pos_volume = volumes[i];
            double close_volume = MathMin(remaining_to_close, pos_volume);

            // Get symbol info for lot step
            string pos_symbol = OrderSymbol();
            double lot_step = MarketInfo(pos_symbol, MODE_LOTSTEP);
            double min_lot = MarketInfo(pos_symbol, MODE_MINLOT);

            if(lot_step > 0)
            {
               close_volume = MathFloor(close_volume / lot_step) * lot_step;
               int vol_digits = (int)MathCeil(-MathLog10(lot_step));
               if(vol_digits < 0) vol_digits = 0;
               close_volume = NormalizeDouble(close_volume, vol_digits);
            }

            if(close_volume < min_lot)
               continue;

            if(close_volume <= 0)
               continue;

            bool result = false;
            RefreshRates();
            double close_price = (pos_type == OP_BUY) ?
                                 MarketInfo(pos_symbol, MODE_BID) :
                                 MarketInfo(pos_symbol, MODE_ASK);

            // Check if full or partial close
            if(close_volume >= pos_volume * FULL_CLOSE_THRESHOLD) // Account for rounding
            {
               // Full close
               result = OrderClose(tickets[i], pos_volume, close_price, m_slippage, CLR_NONE);
               if(result)
               {
                  if(m_log_enabled)
                     PrintFormat("[PartialClose] Fully closed position #%d (%.2f lots)",
                                tickets[i], pos_volume);
                  closed_count++;
                  remaining_to_close -= pos_volume;
               }
            }
            else
            {
               // Partial close
               result = OrderClose(tickets[i], close_volume, close_price, m_slippage, CLR_NONE);
               if(result)
               {
                  if(m_log_enabled)
                     PrintFormat("[PartialClose] Partially closed position #%d (%.2f of %.2f lots)",
                                tickets[i], close_volume, pos_volume);
                  closed_count++;
                  remaining_to_close -= close_volume;

                  // Move remaining to breakeven if requested
                  // Note: After partial close in MT4, the remaining position keeps the same ticket
                  if(move_to_breakeven)
                  {
                     // Re-select order after partial close
                      Sleep(SERVER_SYNC_DELAY_MS);  // Brief delay for server sync
                     if(OrderSelect(tickets[i], SELECT_BY_TICKET))
                     {
                        if(MoveToBreakeven(tickets[i], entry_prices[i], pos_type))
                           modified_count++;
                     }
                  }
               }
            }

            if(!result)
            {
               if(m_log_enabled)
                  PrintFormat("[PartialClose] Failed to close position #%d - Error: %d",
                             tickets[i], GetLastError());
            }
         }
      }

      // Move remaining positions to breakeven if requested
      if(move_to_breakeven && remaining_to_close <= 0)
      {
         for(int i = 0; i < pos_count; i++)
         {
            if(OrderSelect(tickets[i], SELECT_BY_TICKET))
            {
               if(MoveToBreakeven(tickets[i], entry_prices[i], pos_type))
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
   bool MoveToBreakeven(int ticket, double entry_price, int pos_type)
   {
      if(!OrderSelect(ticket, SELECT_BY_TICKET))
         return false;

      string pos_symbol = OrderSymbol();
      RefreshRates();

      // Check if position is in profit
      double current_price = (pos_type == OP_BUY) ?
                            MarketInfo(pos_symbol, MODE_BID) :
                            MarketInfo(pos_symbol, MODE_ASK);

      bool in_profit = (pos_type == OP_BUY) ?
                       (current_price > entry_price) :
                       (current_price < entry_price);

      if(!in_profit)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Position #%d not in profit, cannot move to BE", ticket);
         return false;
      }

      // Check minimum stop level
      int stops_level = (int)MarketInfo(pos_symbol, MODE_STOPLEVEL);
      double min_distance = stops_level * MarketInfo(pos_symbol, MODE_POINT);

      double distance = MathAbs(current_price - entry_price);
      if(distance < min_distance)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Position #%d too close to entry for BE (%.5f < %.5f)",
                       ticket, distance, min_distance);
         return false;
      }

      // Modify position
      int digits = (int)MarketInfo(pos_symbol, MODE_DIGITS);
      bool result = OrderModify(
         ticket,
         OrderOpenPrice(),
         NormalizeDouble(entry_price, digits),
         OrderTakeProfit(),
         0,
         CLR_NONE
      );

      if(result)
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Moved position #%d to breakeven", ticket);
      }
      else
      {
         if(m_log_enabled)
            PrintFormat("[PartialClose] Failed to move #%d to BE - Error: %d",
                       ticket, GetLastError());
      }

      return result;
   }
};
