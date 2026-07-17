//+------------------------------------------------------------------+
//|                                     PTWebSocketClient_MT4.mqh    |
//|              PineTunnel WebSocket Client - MT4 Wrapper              |
//|                   Phase 2: Real-time signal delivery via WebSocket   |
//|                                                                      |
//| This file wraps the PTWebSocket32.dll (x86) for use in MT4 EAs.     |
//| MT5 uses PTWebSocketClient.mqh with PTWebSocket.dll (x64).          |
//|                                                                      |
//| Platform: MT4 only (uses int handle type)                             |
//| DLL: PTWebSocket32.dll (x86)                                         |
//|                                                                      |
//| *** PARITY: All constants, state values, reconnect logic, heartbeat  |
//| defaults, and error handling MUST be identical to MT5 version. ***   |
//| Only the handle type (int vs long) and DLL filename differ.          |
//+------------------------------------------------------------------+
#property copyright "Fractalyst"
#property link      "github.com/TheFractalyst/PineTunnel"
#property version   "1.00"
#property strict

#ifndef PT_WEBSOCKET_CLIENT_MT4_MQH
#define PT_WEBSOCKET_CLIENT_MT4_MQH

//+------------------------------------------------------------------+
//| Constants - MUST match asyncwebsocketclient.h and MT5 version exactly|
//| These values are IDENTICAL to PTWebSocketClient.mqh.                 |
//| Do NOT change these without updating the MT5 version.               |
//+------------------------------------------------------------------+
#define PTWS_CLOSED             0     // Connection closed or not initialized
#define PTWS_CLOSING            1     // Close frame sent/received
#define PTWS_CONNECTING         2     // Connection in progress (handshake)
#define PTWS_CONNECTED          3     // Connected and ready for data
#define PTWS_SENDING            4     // Async send in progress
#define PTWS_POLLING            5     // Async receive in progress

// Callback status constants (from WinHTTP) - MUST match DLL
#define PTWS_CALLBACK_READ_COMPLETE    0x00020000
#define PTWS_CALLBACK_WRITE_COMPLETE   0x00040000
#define PTWS_CALLBACK_CLOSE_COMPLETE  0x00080000
#define PTWS_CALLBACK_REQUEST_ERROR   0x00010000
#define PTWS_CALLBACK_SECURE_CHANNEL_ERROR 0x01000000

// WebSocket close codes (from server)
#define PTWS_CLOSE_INVALID_LICENSE    4001
#define PTWS_CLOSE_SERVER_SHUTDOWN    4002
#define PTWS_CLOSE_RATE_LIMITED       4003

// Default configuration - MUST match MT5 exactly
#define PTWS_DEFAULT_HEARTBEAT_SEC    30    // Ping interval (seconds)
#define PTWS_DEFAULT_POLL_INTERVAL_MS 100   // Poll interval (milliseconds)
#define PTWS_MAX_RECONNECT_ATTEMPTS   0     // 0 = unlimited
#define PTWS_RECONNECT_BASE_DELAY_MS  1000   // Exponential backoff base (1 second)
#define PTWS_RECONNECT_MAX_DELAY_MS   30000   // Max reconnect delay (30 seconds)
#define PTWS_RX_BUFFER_SIZE          65536   // 64KB receive buffer
#define PTWS_STATS_INTERVAL_SEC      60      // Send account stats every 60s
#define PTWS_HEALTH_INTERVAL_SEC     30      // Send health telemetry every 30s
#define PTWS_INLINE_FRAME_SIZE       8192    // Must match DLL PTWS_INLINE_FRAME_SIZE
#define PTWS_NOTIFY_MSG_BASE         0x0400  // WM_USER base for PostMessage notifications

// DLL auth key - assembled at runtime from XOR-encoded fragments to resist decompilation.
// Must match PTWS_AUTH_KEY in asyncwebsocketclient.h.

#define PTWS_AUTH_XOR_KEY0  0x5A
#define PTWS_AUTH_XOR_KEY1  0xC3
#define PTWS_AUTH_XOR_KEY2  0x7F
#define PTWS_AUTH_XOR_KEY3  0x21

// DLL log levels - MUST match asyncwebsocketclient.h
#define PTWS_LOG_OFF      0     // No logging
#define PTWS_LOG_ERROR    1     // Errors only
#define PTWS_LOG_INFO     2     // Errors + connection events
#define PTWS_LOG_DEBUG    3     // Everything (frame-level detail)

// Buffer type constants for PTWS_Read
#define PTWS_BUFFER_UTF8_TEXT         0
#define PTWS_BUFFER_BINARY            1

//+------------------------------------------------------------------+
//| Connection statistics struct - MUST match DLL definition            |
//+------------------------------------------------------------------+
struct PTWS_ConnectionStats
{
   int    uptime_sec;           // Seconds since Connect()
   int    bytes_sent;           // Total bytes sent (32-bit, wraps at 4GB - use V2)
   int    bytes_received;       // Total bytes received (32-bit, wraps at 4GB - use V2)
   int    reconnect_count;      // Number of reconnect attempts
   int    frames_queued;        // Frames currently in queue
   int    frames_dropped;       // Frames dropped (32-bit - use V2 for 64-bit)
   int    ws_latency_ms;        // Last measured WS round-trip latency (ping→pong)
   int    terminal_lag_ms;     // Milliseconds since last Poll() call
};

//+------------------------------------------------------------------+
//| Connection statistics V2 - 64-bit counters (MUST match DLL)        |
//| Uses int pairs for MT4 compatibility (MQL4 has no 64-bit type).     |
//| Layout is identical on both MT4 and MT5 (all fields 4-byte int).   |
//+------------------------------------------------------------------+
struct PTWS_ConnectionStatsV2
{
   int    struct_version;       // Always 2
   int    uptime_sec;           // Seconds since Connect()
   int    bytes_sent_low;       // Low 32 bits of total bytes sent
   int    bytes_sent_high;      // High 32 bits of total bytes sent
   int    bytes_received_low;   // Low 32 bits of total bytes received
   int    bytes_received_high;  // High 32 bits of total bytes received
   int    reconnect_count;      // Number of reconnect attempts
   int    frames_queued;        // Frames currently in queue
   int    frames_dropped_low;   // Low 32 bits of frames dropped
   int    frames_dropped_high;  // High 32 bits of frames dropped
   int    ws_latency_ms;         // Last measured WS round-trip latency
   int    terminal_lag_ms;       // Milliseconds since last Poll() call
};

//+------------------------------------------------------------------+
//| VPS detection info - MUST match DLL PTWS_VpsInfo struct            |
//+------------------------------------------------------------------+
struct PTWS_VpsInfo
{
   int    is_vps;                  // 1 if running on VPS, 0 if physical machine
   uchar  provider[64];           // VPS provider name
   uchar  manufacturer[128];      // System manufacturer from WMI
   uchar  model[128];             // System model from WMI
};

//+------------------------------------------------------------------+
//| Network diagnostics - MUST match DLL PTWS_NetDiag struct            |
//+------------------------------------------------------------------+
struct PTWS_NetDiag
{
   int    ping_ms;                 // Average round-trip time in ms (-1=timeout, -2=error)
   int    min_rtt_ms;              // Minimum RTT in ms
   int    max_rtt_ms;              // Maximum RTT in ms
   int    p95_rtt_ms;              // 95th percentile RTT in ms
   int    jitter95_ms;             // 95th percentile jitter in ms
   int    packets_sent;            // Number of ICMP packets sent
   int    packets_received;        // Number of ICMP packets received
   double packet_loss_pct;        // Packet loss percentage
   uchar  target_host[256];        // Ping target hostname/IP
   int    is_internet_available;   // 1 if internet connectivity confirmed, 0 if not
   uchar  quality[16];            // "Good", "Moderate", or "Bad"
};

//+------------------------------------------------------------------+
//| NTP time sync result                                                 |
//+------------------------------------------------------------------+
struct PTWS_NtpTime
{
   int    ntp_time_s;              // NTP time as Unix epoch seconds (UTC)
   int    drift_ms;                // Drift from local clock in ms (+10ms buffer)
   int    sync_success;            // 1 if sync succeeded, 0 if failed
};

//+------------------------------------------------------------------+
//| DLL Imports - MT4 uses int handle type and PTWebSocket32.dll        |
//| Function signatures are IDENTICAL to MT5 except handle type (int).   |
//+------------------------------------------------------------------+
#import "PTWebSocket32.dll"
    int    PTWS_ConnectAuth(string url, int port, int secure, string ws_param, string auth_key);
   void   PTWS_Disconnect(int handle);
   void   PTWS_Reset(int handle);
   int    PTWS_Send(int handle, uchar &data[], int length);
   int    PTWS_Read(int handle, uchar &buffer[], int buffer_size, int &bytes_read, int &buffer_type);
   int    PTWS_Poll(int handle);
   int    PTWS_Readable(int handle);
   int    PTWS_Status(int handle);
   int    PTWS_LastError(int handle);
   int    PTWS_LastCallback(int handle);
    void   PTWS_SetLogLevel(int level);
   int    PTWS_GetLogCount();
   int    PTWS_GetLogEntry(int index, uchar &buffer[], int buffer_size);
   int    PTWS_GetStats(int handle, PTWS_ConnectionStats &stats);
   int    PTWS_GetStatsV2(int handle, PTWS_ConnectionStatsV2 &stats);
   int    PTWS_GetQueueDepth(int handle);
    int    PTWS_SetNotifyWindow(int handle, int wnd, int msg);
   int    PTWS_GetDllVersion(int handle, uchar &buffer[], int buffer_size);
    void   PTWS_RecordPingSent(int handle);
    int    PTWS_PreventSleep();
   int    PTWS_AllowSleep();
   int    PTWS_GetVpsInfo(PTWS_VpsInfo &info);
   int    PTWS_RunNetworkDiag(uchar &target_host[], int timeout_ms, int num_pings, PTWS_NetDiag &diag);
   int    PTWS_GetSystemInfo(int handle, uchar &buffer[], int buffer_size);
   int    PTWS_DownloadFile(uchar &url[], uchar &headers[], uchar &save_path[], int timeout_ms);
   int    PTWS_ApplyUpdate(uchar &target_dir[], uchar &current_filename[], uchar &new_filename[]);
   int    PTWS_ScheduleRestart(uchar &terminal_path[], uchar &data_path[], uchar &config_path[], int restart_delay_ms);
   int    PTWS_ClearRestartCounter(uchar &data_path[]);
    int    PTWS_GetNtpTime(PTWS_NtpTime &info);
#import

//+------------------------------------------------------------------+
//| CPTWebSocketClient - MT4 WebSocket client wrapper                   |
//|                                                                      |
//| Wraps the DLL functions into a convenient class. The EA creates     |
//| an instance of this class and calls Connect()/Poll()/ReceiveSignals() |
//| from OnTimer(). If the DLL is not loaded or Connect fails, the EA    |
//| falls back to HTTP long-polling automatically.                       |
//|                                                                      |
//| *** PARITY: All logic MUST be identical to MT5 version. ***          |
//| Only differences: int handle (vs long), PTWebSocket32.dll (vs .dll) |
//+------------------------------------------------------------------+
class CPTWebSocketClient
{
private:
   int     m_handle;              // DLL connection handle (int for MT4)
   bool    m_initialized;          // Whether DLL was loaded successfully
   bool    m_connected;            // Whether WebSocket is connected
   string  m_serverUrl;            // Full WSS URL (e.g., wss://your-server.com/ws/{key})
   string  m_licenseKey;           // License key for auth
   int     m_heartbeatSec;        // Ping interval (seconds)
   int     m_reconnectAttempts;   // Current reconnect attempt count
   int     m_maxReconnects;        // Max attempts before permanent fallback (0=unlimited)
   datetime m_lastHeartbeat;       // Last ping sent time
   datetime m_lastReceived;        // Last data received time
   datetime m_lastPoll;            // Last PTWS_Poll() call time
   uchar   m_rxBuffer[];          // Pre-allocated receive buffer (64KB)
   int     m_rxBufferSize;         // Valid bytes in receive buffer

   // Assemble DLL auth key at runtime from XOR-encoded fragments.
   // Prevents trivial extraction from decompiled .ex4 files.
   static string GetAuthKey();

public:
   //+------------------------------------------------------------------+
   //| Constructor / Destructor                                            |
   //+------------------------------------------------------------------+
   CPTWebSocketClient();
  ~CPTWebSocketClient();

   //+------------------------------------------------------------------+
   //| Connection lifecycle                                                |
   //+------------------------------------------------------------------+
   bool Connect(string base_url, int port, bool secure, string license_key);
   void Disconnect();
   void Reset();
   bool IsConnected();
   int  GetStatus();

   //+------------------------------------------------------------------+
   //| Data transfer                                                       |
   //+------------------------------------------------------------------+
   bool Poll();                     // Call from OnTimer() - triggers async receive
   bool ReceiveSignals(string &json_output);  // Read next frame from DLL queue
   bool SendString(string message);           // Send text message (ACK, ping, etc.)
   bool SendAccountStats();                  // Send account info via WS
   bool SendOpenPositions();                 // Send open positions via WS
   bool SendTradeHistory(int days_back=7);  // Send trade history via WS
   bool SendHealthTelemetry();               // Send WS health + terminal metrics

   //+------------------------------------------------------------------+
   //| Error and state queries                                             |
   //+------------------------------------------------------------------+
   int  GetLastError();
   int  GetLastCallback();
   int  GetHandle();               // Get current DLL handle (for diagnostics)

   //+------------------------------------------------------------------+
   //| Configuration                                                       |
   //+------------------------------------------------------------------+
   void SetHeartbeat(int interval_seconds);
   bool SetNotifyWindow(int wnd, int msg);  // Set chart HWND for PostMessage notifications

   //+------------------------------------------------------------------+
   //| DLL Logging                                                         |
   //+------------------------------------------------------------------+
   static void SetLogLevel(int level);       // 0=off, 1=error, 2=info, 3=debug
   static int  GetLogCount();                // Total entries written
   static bool GetLogEntry(int index, string &entry);  // Get entry by index

   //+------------------------------------------------------------------+
   //| Connection statistics                                               |
   //+------------------------------------------------------------------+
   bool GetStats(PTWS_ConnectionStats &stats);  // Get connection stats from DLL (V1, 32-bit)
   bool GetStatsV2(PTWS_ConnectionStatsV2 &stats); // Get connection stats (V2, 64-bit counters)
   int  GetQueueDepth();                // Frames currently in receive queue
   string GetDllVersion();                      // Get DLL version string
   string GetSystemInfo();                      // Get system info JSON for audit

   //+------------------------------------------------------------------+
   //| Latency tracking                                                    |
   //+------------------------------------------------------------------+
   void RecordPingSent();                       // Call when sending a ping

   //+------------------------------------------------------------------+
   //| State checks for EA fallback logic                                  |
   //+------------------------------------------------------------------+
   bool ShouldFallbackToHTTP();     // Returns true if WS has failed too many times
   datetime GetLastReceived();      // Last time data was received from server

   //+------------------------------------------------------------------+
   //| System utilities (no handle needed)                                 |
   //+------------------------------------------------------------------+
   static bool PreventSleep();                       // Prevent Windows from sleeping
   static bool AllowSleep();                         // Restore normal sleep behavior
   static bool GetVpsInfo(PTWS_VpsInfo &info);       // Detect if running on VPS
   static bool RunNetworkDiag(string host, int timeout_ms, int num_pings, PTWS_NetDiag &diag);  // Ping test + quality
   static int  DownloadFile(string url, string headers, string save_path, int timeout_ms=30000);  // Download file via DLL
   static int  ApplyUpdate(string target_dir, string current_filename, string new_filename);  // Swap files on restart
   static int  ScheduleRestart(string terminal_path, string data_path, string config_path, int restart_delay_ms);  // Schedule terminal restart for update
   static int  ClearRestartCounter(string data_path);  // Clear restart counter after successful init
   static bool GetNtpTime(PTWS_NtpTime &info);       // Get NTP time + drift

private:
   static string EscapeJSON(string str);   // Escape special chars for JSON strings
};

//+------------------------------------------------------------------+
//| Escape special characters for JSON string values                     |
//| Must be applied to all string fields before StringFormat with %s     |
//| Prevents JSON parse errors from quotes, backslashes, newlines        |
//+------------------------------------------------------------------+
string CPTWebSocketClient::EscapeJSON(string str)
{
   string result = str;
   StringReplace(result, "\\", "\\\\");   // Backslashes first
   StringReplace(result, "\"", "\\\"");    // Quotes
   StringReplace(result, "\n", "\\n");     // Newlines
   StringReplace(result, "\r", "\\r");     // Carriage returns
   StringReplace(result, "\t", "\\t");     // Tabs
   return result;
}

//+------------------------------------------------------------------+
//| Constructor                                                         |
//+------------------------------------------------------------------+
CPTWebSocketClient::CPTWebSocketClient()
   : m_handle(0)
   , m_initialized(false)
   , m_connected(false)
   , m_serverUrl("")
   , m_licenseKey("")
   , m_heartbeatSec(PTWS_DEFAULT_HEARTBEAT_SEC)
   , m_reconnectAttempts(0)
   , m_maxReconnects(PTWS_MAX_RECONNECT_ATTEMPTS)
   , m_lastHeartbeat(0)
   , m_lastReceived(0)
   , m_lastPoll(0)
   , m_rxBufferSize(0)
{
   ArrayResize(m_rxBuffer, PTWS_RX_BUFFER_SIZE);
   ArrayInitialize(m_rxBuffer, 0);

   // Test if the DLL is available by calling a harmless function
   // PTWS_Status with handle=0 should return PTWS_CLOSED without crashing
   // If the DLL is not loaded, MQL4 will set an error and this will fail
   ResetLastError();
   int testStatus = PTWS_Status(0);
   if(GetLastError() == 0 || testStatus == PTWS_CLOSED)
      m_initialized = true;
   else
      m_initialized = false;
}

//+------------------------------------------------------------------+
//| Destructor                                                           |
//+------------------------------------------------------------------+
CPTWebSocketClient::~CPTWebSocketClient()
{
   if(m_handle > 0)
   {
      PTWS_Disconnect(m_handle);
      PTWS_Reset(m_handle);
      m_handle = 0;
   }
   m_connected = false;
}

//+------------------------------------------------------------------+
//| Connect to WebSocket server                                         |
//| base_url: the server base URL (e.g., "your-server.com")      |
//| port: 443 for WSS, 80 for WS                                        |
//| secure: true for WSS, false for WS                                   |
//| license_key: authentication token                                   |
//| Returns true on successful connection attempt (handshake in progress)|
//| GetAuthKey - Assemble DLL auth key at runtime from XOR fragments       |
//| Prevents trivial key extraction from decompiled .ex4 files.           |
//+------------------------------------------------------------------+
string CPTWebSocketClient::GetAuthKey()
{
   
   uchar fragments[] = {0x0A,0x97,0x28,0x72,0x77,0xF4,0x19,0x13,0x3B,0xFA,0x1C,0x15,0x3F,0xF2,0x1D};
   uchar keys[]      = {PTWS_AUTH_XOR_KEY0,PTWS_AUTH_XOR_KEY1,PTWS_AUTH_XOR_KEY2,PTWS_AUTH_XOR_KEY3};
   string result = "";
   for(int i = 0; i < ArraySize(fragments); i++)
      result += CharToString((uchar)(fragments[i] ^ keys[i % 4]));
   return result;
}

//+------------------------------------------------------------------+
//| Connect - Start async WebSocket connection                            |
//|                                                                      |
//| Note: m_connected is NOT set here - Poll() sets it once the         |
//| DLL reports PTWS_CONNECTED state after handshake completes.        |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::Connect(string base_url, int port, bool secure, string license_key)
{
   if(!m_initialized)
      return false;

   // Build WebSocket URL from base URL
   // "https://your-server.com" → "wss://your-server.com/ws/{license_key}"
   // "http://your-server.com"  → "ws://your-server.com/ws/{license_key}"
   m_licenseKey = license_key;
   m_serverUrl = base_url;
   StringReplace(m_serverUrl, "https://", "wss://");
   StringReplace(m_serverUrl, "http://", "ws://");
   m_serverUrl += "/ws/" + license_key;

   // Attempt WebSocket connection
   // The DLL connects asynchronously via WinHTTP callbacks
   m_handle = PTWS_ConnectAuth(m_serverUrl, port, secure ? 1 : 0, license_key, GetAuthKey());

   if(m_handle <= 0)
   {
      m_connected = false;
      m_reconnectAttempts++;
      PrintFormat("[PineTunnel] WebSocket connect failed (error %d, attempt %d)", GetLastError(), m_reconnectAttempts);
      return false;
   }

   // Connection is in progress - PTWS_Connect returns handle immediately
   // Do NOT set m_connected = true here - the handshake is still in progress.
   // Poll() will check PTWS_Status and set m_connected = true when the DLL
   // reports PTWS_CONNECTED (handshake complete).
   m_connected = false;
   m_reconnectAttempts = 0;
   // Use TimeLocal() (wall clock) - not TimeCurrent() - for heartbeat/last-received
   // tracking. TimeCurrent() returns the last server quote time and FREEZES when
   // there are no ticks (weekends, thin markets, inter-symbol gaps). Freezing
   // would block heartbeats and let the server's idle timeout drop the connection.
    m_lastHeartbeat = TimeLocal();
    m_lastReceived = TimeLocal();

    PrintFormat("[PineTunnel] WebSocket connecting to %s port %d (handle=%d)", m_serverUrl, port, m_handle);
   return true;
}

//+------------------------------------------------------------------+
//| Disconnect from WebSocket server                                    |
//+------------------------------------------------------------------+
void CPTWebSocketClient::Disconnect()
{
   if(m_handle > 0)
   {
      PTWS_Disconnect(m_handle);
      m_connected = false;
      PrintFormat("[PineTunnel] WebSocket disconnected (handle=%d)", m_handle);
      // Don't free here - caller should call Reset() after Disconnect()
   }
}

//+------------------------------------------------------------------+
//| Reset DLL state and free resources                                  |
//+------------------------------------------------------------------+
void CPTWebSocketClient::Reset()
{
   if(m_handle > 0)
   {
      PTWS_Reset(m_handle);
      m_handle = 0;
   }
   m_connected = false;
   m_rxBufferSize = 0;
}

//+------------------------------------------------------------------+
//| Poll the DLL - triggers async receive if needed, sends heartbeat    |
//| Must be called from OnTimer() at regular intervals (e.g., 100ms).    |
//| Returns true if poll was successful or connected.                    |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::Poll()
{
   if(!m_initialized || m_handle <= 0)
      return false;

   int status = PTWS_Status(m_handle);

   // Update connection state based on DLL status
   if(status == PTWS_CONNECTED || status == PTWS_SENDING || status == PTWS_POLLING)
   {
      if(!m_connected)
      {
         PrintFormat("[PineTunnel] WebSocket handshake complete - connected (handle=%d)", m_handle);
      }
      m_connected = true;
   }
   else if(status == PTWS_CLOSING)
   {
      m_connected = false;
      return false;
   }
   else if(status == PTWS_CLOSED)
   {
      m_connected = false;
      return false;
   }
   // PTWS_CONNECTING: handshake still in progress, don't set m_connected

    // Trigger async receive (safety net - auto-chain handles most receives)
    PTWS_Poll(m_handle);

   // Check callback notifications
   int callback = PTWS_LastCallback(m_handle);

   if(callback == PTWS_CALLBACK_READ_COMPLETE)
      m_lastReceived = TimeLocal();
   else if(callback == PTWS_CALLBACK_REQUEST_ERROR)
   {
      m_connected = false;
      return false;
   }

   // ── Heartbeat: send ping if interval has elapsed ──
   if(m_connected && m_heartbeatSec > 0)
   {
      datetime now = TimeLocal();
      if(now - m_lastHeartbeat >= m_heartbeatSec)
      {
         string pingJson = "{\"type\":\"ping\",\"timestamp\":" + IntegerToString((long)now) + "}";
         if(SendString(pingJson))
         {
            RecordPingSent();  // Mark timestamp for latency measurement
            m_lastHeartbeat = now;
         }
      }
   }

   m_lastPoll = TimeLocal();
   return true;
}

//+------------------------------------------------------------------+
//| Read next frame from DLL queue                                       |
//| Returns true if data was read, false if queue is empty               |
//| json_output: receives the signal JSON string                         |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::ReceiveSignals(string &json_output)
{
   if(!m_initialized || m_handle <= 0)
      return false;

   // Check if data is available
   int readable = PTWS_Readable(m_handle);
   if(readable <= 0)
      return false;

   // Read from DLL frame queue
   int bytes_read = 0;
   int buffer_type = 0;
   ArrayResize(m_rxBuffer, PTWS_RX_BUFFER_SIZE);

   int result = PTWS_Read(m_handle, m_rxBuffer, PTWS_RX_BUFFER_SIZE, bytes_read, buffer_type);
   if(result != 0 || bytes_read <= 0)
      return false;

   // Convert buffer to string (UTF-8)
   m_rxBufferSize = bytes_read;
   json_output = CharArrayToString(m_rxBuffer, 0, bytes_read, CP_UTF8);

   return (StringLen(json_output) > 0);
}

//+------------------------------------------------------------------+
//| Send a text string via WebSocket                                     |
//| Returns true on successful send initiation                           |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::SendString(string message)
{
   if(!m_initialized || m_handle <= 0 || !m_connected)
      return false;

    // Convert string to uchar array for DLL (UTF-8 for WebSocket text frames)
    uchar sendBuffer[];
    int len = StringToCharArray(message, sendBuffer, 0, WHOLE_ARRAY, CP_UTF8);
    if(len <= 0)
       return false;
    len--;  // Exclude null terminator

    int result = PTWS_Send(m_handle, sendBuffer, len);
    return (result == 0);
}

//+------------------------------------------------------------------+
//| Send account stats via WebSocket (MT4 version)                       |
//| Note: MT4 does not support margin_initial, margin_maintenance,       |
//| assets, liabilities, commission_blocked, currency_digits,            |
//| fifo_close, hedge_allowed - those are MT5-only.                     |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::SendAccountStats()
{
   if(!m_initialized || m_handle <= 0 || !m_connected)
      return false;

   // String properties - escape for JSON safety
   string accName = EscapeJSON(AccountInfoString(ACCOUNT_NAME));
   string accServer = EscapeJSON(AccountInfoString(ACCOUNT_SERVER));
   string accCurrency = EscapeJSON(AccountInfoString(ACCOUNT_CURRENCY));
   string accCompany = EscapeJSON(AccountInfoString(ACCOUNT_COMPANY));

   // Integer properties
   int accLogin = (int)AccountInfoInteger(ACCOUNT_LOGIN);
   int accLeverage = (int)AccountInfoInteger(ACCOUNT_LEVERAGE);
   int accLimitOrders = (int)AccountInfoInteger(ACCOUNT_LIMIT_ORDERS);
   bool accTradeAllowed = (bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED);
   bool accTradeExpert = (bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT);

   // Trade mode enum → string
   int tradeModeRaw = (int)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   string tradeModeStr = "real";
   if(tradeModeRaw == 0) tradeModeStr = "demo";       // ACCOUNT_TRADE_MODE_DEMO
   else if(tradeModeRaw == 1) tradeModeStr = "contest"; // ACCOUNT_TRADE_MODE_CONTEST

   // StopOut mode → string
   int soModeRaw = (int)AccountInfoInteger(ACCOUNT_MARGIN_SO_MODE);
   string soModeStr = (soModeRaw == 0) ? "percent" : "money"; // 0=ACCOUNT_STOPOUT_MODE_PERCENT

   // Double properties (MT4 supports all of these)
   double accBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   double accCredit = AccountInfoDouble(ACCOUNT_CREDIT);
   double accProfit = AccountInfoDouble(ACCOUNT_PROFIT);
   double accEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   double accMargin = AccountInfoDouble(ACCOUNT_MARGIN);
   double accMarginFree = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double accMarginLevel = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
    double accMarginSoCall = AccountInfoDouble(ACCOUNT_MARGIN_SO_CALL);
    double accMarginSoSo = AccountInfoDouble(ACCOUNT_MARGIN_SO_SO);

    int pos_count = 0;
    for(int i = 0; i < OrdersTotal(); i++)
    {
       if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES) && (OrderType() == OP_BUY || OrderType() == OP_SELL))
          pos_count++;
    }

    string json = StringFormat(
      "{\"type\":\"account_stats\","
      "\"login\":%d,"
      "\"name\":\"%s\","
      "\"server\":\"%s\","
      "\"currency\":\"%s\","
      "\"company\":\"%s\","
      "\"trade_mode\":\"%s\","
      "\"leverage\":%d,"
      "\"limit_orders\":%d,"
      "\"trade_allowed\":%s,"
      "\"trade_expert\":%s,"
      "\"margin_so_mode\":\"%s\","
      "\"balance\":%.2f,"
      "\"credit\":%.2f,"
      "\"equity\":%.2f,"
      "\"profit\":%.2f,"
      "\"margin\":%.2f,"
      "\"margin_free\":%.2f,"
      "\"margin_level\":%.2f,"
      "\"margin_so_call\":%.2f,"
      "\"margin_so_so\":%.2f,"
      "\"positions\":%d}",
      accLogin,
      accName,
      accServer,
      accCurrency,
      accCompany,
      tradeModeStr,
      accLeverage,
      accLimitOrders,
      accTradeAllowed ? "true" : "false",
      accTradeExpert ? "true" : "false",
      soModeStr,
      accBalance,
      accCredit,
      accEquity,
      accProfit,
      accMargin,
      accMarginFree,
      accMarginLevel,
      accMarginSoCall,
       accMarginSoSo,
       pos_count
    );

    return SendString(json);
}

//+------------------------------------------------------------------+
//| Send health telemetry via WebSocket                                  |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::SendHealthTelemetry()
{
   if(!m_initialized || m_handle <= 0 || !m_connected)
      return false;

   // Use V2 stats for 64-bit counters (no overflow at 4GB)
   PTWS_ConnectionStatsV2 stats;
   if(!GetStatsV2(stats))
      return false;

   string dllVer = GetDllVersion();
   int queueDepth = GetQueueDepth();

   // VPS detection
   string vpsInfo = "";
   PTWS_VpsInfo vps;
   ZeroMemory(vps);
   if(GetVpsInfo(vps))
   {
      string vpsStr = (vps.is_vps == 1) ? "true" : "false";
      string providerStr = CharArrayToString(vps.provider);
      vpsInfo = StringFormat(",\"is_vps\":%s,\"vps_provider\":\"%s\"", vpsStr, EscapeJSON(providerStr));
   }

   // Reassemble 64-bit counters from int pairs
   // MT4 has no long type (4 bytes), so we use double for the JSON value
   double bytesSent = (double)((double)stats.bytes_sent_high * 4294967296.0 + (double)stats.bytes_sent_low);
   double bytesRecv = (double)((double)stats.bytes_received_high * 4294967296.0 + (double)stats.bytes_received_low);
   double framesDropped = (double)((double)stats.frames_dropped_high * 4294967296.0 + (double)stats.frames_dropped_low);

   // Use %.0f to format 64-bit values as integers in JSON (MT4 StringFormat has no %lld)
   string json = StringFormat(
      "{\"type\":\"health\","
      "\"ws_latency_ms\":%d,"
      "\"terminal_lag_ms\":%d,"
      "\"ws_uptime_sec\":%d,"
      "\"ws_bytes_sent\":%.0f,"
      "\"ws_bytes_recv\":%.0f,"
      "\"ws_reconnects\":%d,"
      "\"ws_frames_queued\":%d,"
      "\"ws_frames_dropped\":%.0f,"
      "\"ws_queue_depth\":%d,"
      "\"dll_version\":\"%s\"%s}",
      stats.ws_latency_ms,
      stats.terminal_lag_ms,
      stats.uptime_sec,
      bytesSent,
      bytesRecv,
      stats.reconnect_count,
      stats.frames_queued,
      framesDropped,
      queueDepth,
      dllVer,
      vpsInfo
   );

   return SendString(json);
}

//+------------------------------------------------------------------+
//| Send open positions via WebSocket (MT4 version)                     |
//| Iterates all market orders (OP_BUY/OP_SELL) and sends as JSON       |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::SendOpenPositions()
{
   if(!m_initialized || m_handle <= 0 || !m_connected)
      return false;

   int total = OrdersTotal();
   string json = "{\"type\":\"open_positions\",\"positions\":[";

   int sent = 0;
   for(int i = 0; i < total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;

      // Only include market orders (open positions), not pending orders
      int orderType = OrderType();
      if(orderType != OP_BUY && orderType != OP_SELL)
         continue;

      string typeStr = (orderType == OP_BUY) ? "buy" : "sell";
      string symbol = OrderSymbol();

      if(sent > 0)
         json += ",";
      json += StringFormat(
         "{\"ticket\":%d,"
         "\"symbol\":\"%s\","
         "\"type\":\"%s\","
         "\"volume\":%.2f,"
         "\"open_price\":%.5f,"
         "\"current_price\":%.5f,"
         "\"sl\":%.5f,"
         "\"tp\":%.5f,"
         "\"profit\":%.2f,"
         "\"swap\":%.2f,"
         "\"commission\":%.2f,"
         "\"magic\":%d,"
         "\"comment\":\"%s\","
         "\"open_time\":%d}",
         OrderTicket(),
         EscapeJSON(symbol),
         typeStr,
         OrderLots(),
         OrderOpenPrice(),
         OrderClosePrice() > 0 ? OrderClosePrice() : MarketInfo(symbol, MODE_BID),
         OrderStopLoss(),
         OrderTakeProfit(),
         OrderProfit(),
         OrderSwap(),
         OrderCommission(),
         OrderMagicNumber(),
         EscapeJSON(OrderComment()),
         (int)OrderOpenTime()
      );
      sent++;
   }

   json += "]}";
   return SendString(json);
}

//+------------------------------------------------------------------+
//| Send trade history via WebSocket (MT4 version)                       |
//| Sends closed orders for the last days_back days (default 7)         |
//| Note: MT4 uses OrderSelect(MODE_HISTORY); history availability      |
//| depends on user's "Account History" tab settings in terminal         |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::SendTradeHistory(int days_back)
{
   if(!m_initialized || m_handle <= 0 || !m_connected)
      return false;

   datetime fromTime = TimeCurrent() - days_back * 86400;

   int total = OrdersHistoryTotal();
    string json = "{\"type\":\"trade_history\",\"days_back\":" + IntegerToString(days_back) + ",\"deals\":[";

   int sent = 0;
   for(int i = 0; i < total; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY))
         continue;

      // Skip pending orders that were cancelled - only include market orders
      int orderType = OrderType();
      if(orderType != OP_BUY && orderType != OP_SELL)
         continue;

      // Skip orders outside time range
      if(OrderCloseTime() < fromTime)
         continue;

      string typeStr = (orderType == OP_BUY) ? "buy" : "sell";
      string symbol = OrderSymbol();

      if(sent > 0)
         json += ",";
      json += StringFormat(
         "{\"ticket\":%d,"
         "\"symbol\":\"%s\","
         "\"type\":\"%s\","
         "\"volume\":%.2f,"
         "\"open_price\":%.5f,"
         "\"close_price\":%.5f,"
         "\"sl\":%.5f,"
         "\"tp\":%.5f,"
         "\"profit\":%.2f,"
         "\"swap\":%.2f,"
         "\"commission\":%.2f,"
         "\"magic\":%d,"
         "\"comment\":\"%s\","
         "\"open_time\":%d,"
         "\"close_time\":%d}",
         OrderTicket(),
         EscapeJSON(symbol),
         typeStr,
         OrderLots(),
         OrderOpenPrice(),
         OrderClosePrice(),
         OrderStopLoss(),
         OrderTakeProfit(),
         OrderProfit(),
         OrderSwap(),
         OrderCommission(),
         OrderMagicNumber(),
         EscapeJSON(OrderComment()),
         (int)OrderOpenTime(),
         (int)OrderCloseTime()
      );
      sent++;
   }

   json += "]}";
   return SendString(json);
}

//+------------------------------------------------------------------+
//| Query connection state                                               |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::IsConnected()
{
   if(!m_initialized || m_handle <= 0)
      return false;
   return m_connected;
}

int CPTWebSocketClient::GetStatus()
{
   if(!m_initialized || m_handle <= 0)
      return PTWS_CLOSED;
   return PTWS_Status(m_handle);
}

int CPTWebSocketClient::GetLastError()
{
   if(!m_initialized || m_handle <= 0)
      return 0;
   return PTWS_LastError(m_handle);
}

int CPTWebSocketClient::GetLastCallback()
{
   if(!m_initialized || m_handle <= 0)
      return 0;
   return PTWS_LastCallback(m_handle);
}

int CPTWebSocketClient::GetHandle()
{
   return m_handle;
}

//+------------------------------------------------------------------+
//| Configuration                                                       |
//+------------------------------------------------------------------+
void CPTWebSocketClient::SetHeartbeat(int interval_seconds)
{
   m_heartbeatSec = interval_seconds;
}

//+------------------------------------------------------------------+
//| SetNotifyWindow - Register chart window for PostMessage wake-up    |
//| MT4 uses int for HWND (32-bit). When a frame arrives, the DLL     |
//| posts a custom message to this window, triggering OnChartEvent.   |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::SetNotifyWindow(int wnd, int msg)
{
   if(!m_initialized || m_handle <= 0)
      return false;
   return (PTWS_SetNotifyWindow(m_handle, wnd, msg) == 0);
}

//+------------------------------------------------------------------+
//| DLL Logging - static methods, no handle needed                      |
//+------------------------------------------------------------------+
void CPTWebSocketClient::SetLogLevel(int level)
{
   PTWS_SetLogLevel(level);
}

int CPTWebSocketClient::GetLogCount()
{
   return PTWS_GetLogCount();
}

bool CPTWebSocketClient::GetLogEntry(int index, string &entry)
{
   uchar buffer[512];
   int result = PTWS_GetLogEntry(index, buffer, 512);
   if(result != 0)
   {
      entry = "";
      return false;
   }
    entry = CharArrayToString(buffer, 0, WHOLE_ARRAY, CP_UTF8);
    return true;
}

//+------------------------------------------------------------------+
//| Get connection statistics from DLL                                    |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::GetStats(PTWS_ConnectionStats &stats)
{
   if(!m_initialized || m_handle <= 0)
   {
      ZeroMemory(stats);
      return false;
   }
   return (PTWS_GetStats(m_handle, stats) == 0);
}

//+------------------------------------------------------------------+
//| Get connection statistics V2 (64-bit counters)                        |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::GetStatsV2(PTWS_ConnectionStatsV2 &stats)
{
   if(!m_initialized || m_handle <= 0)
   {
      ZeroMemory(stats);
      return false;
   }
   return (PTWS_GetStatsV2(m_handle, stats) == 0);
}

//+------------------------------------------------------------------+
//| Get current queue depth (for backpressure signaling)               |
//+------------------------------------------------------------------+
int CPTWebSocketClient::GetQueueDepth()
{
   if(!m_initialized || m_handle <= 0)
      return 0;
   return PTWS_GetQueueDepth(m_handle);
}

//+------------------------------------------------------------------+
//| Get DLL version string                                               |
//+------------------------------------------------------------------+
string CPTWebSocketClient::GetDllVersion()
{
   if(!m_initialized)
      return "";
   uchar buffer[64];
   int len = PTWS_GetDllVersion(m_handle, buffer, 64);
   if(len <= 0)
      return "";
   return CharArrayToString(buffer, 0, len, CP_UTF8);
}

//+------------------------------------------------------------------+
//| Record ping sent - marks timestamp for latency measurement          |
//+------------------------------------------------------------------+
void CPTWebSocketClient::RecordPingSent()
{
   if(m_initialized && m_handle > 0)
      PTWS_RecordPingSent(m_handle);
}

//+------------------------------------------------------------------+
//| Check if EA should fall back to HTTP long-polling                    |
//| Returns true if too many reconnect attempts or DLL not available     |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::ShouldFallbackToHTTP()
{
   // DLL not loaded - always fall back
   if(!m_initialized)
      return true;

   // No handle - connection not established
   if(m_handle <= 0)
      return true;

   // Max reconnect attempts exceeded
   if(m_maxReconnects > 0 && m_reconnectAttempts >= m_maxReconnects)
      return true;

   return false;
}

//+------------------------------------------------------------------+
//| Get last received time - for dead connection detection              |
//+------------------------------------------------------------------+
datetime CPTWebSocketClient::GetLastReceived()
{
   return m_lastReceived;
}

//+------------------------------------------------------------------+
//| PreventSleep - Prevent Windows from sleeping while EA runs          |
//| Call from OnInit(). Must call AllowSleep in OnDeinit().              |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::PreventSleep()
{
   return (PTWS_PreventSleep() == 1);
}

//+------------------------------------------------------------------+
//| AllowSleep - Restore normal Windows sleep behavior                   |
//| Call from OnDeinit() to undo PreventSleep.                           |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::AllowSleep()
{
   return (PTWS_AllowSleep() == 1);
}

//+------------------------------------------------------------------+
//| GetVpsInfo - Detect if running on VPS                                |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::GetVpsInfo(PTWS_VpsInfo &info)
{
   return (PTWS_GetVpsInfo(info) == 0);
}

//+------------------------------------------------------------------+
//| RunNetworkDiag - Ping a host and check connectivity                   |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::RunNetworkDiag(string host, int timeout_ms, int num_pings, PTWS_NetDiag &diag)
{
   uchar host_buf[];
   StringToCharArray(host, host_buf, 0, WHOLE_ARRAY, CP_UTF8);
   return (PTWS_RunNetworkDiag(host_buf, timeout_ms, num_pings, diag) == 0);
}

//+------------------------------------------------------------------+
//| GetSystemInfo - Return system/terminal info as JSON string          |
//| Used by EA for audit/telemetry reporting.                           |
//| Returns JSON string with OS info, RAM, DLL version.                 |
//+------------------------------------------------------------------+
string CPTWebSocketClient::GetSystemInfo()
{
   if(!m_initialized || m_handle <= 0)
      return "";
   uchar buffer[512];
   int len = PTWS_GetSystemInfo(m_handle, buffer, 512);
   if(len <= 0)
      return "";
   return CharArrayToString(buffer, 0, len, CP_UTF8);
}

//+------------------------------------------------------------------+
//| DownloadFile - Download a file from server to local disk              |
//| Uses WinHTTP (bypasses MQL5 sandbox).                               |
//| Returns: 0 on success, HTTP status code, or negative error code.     |
//+------------------------------------------------------------------+
int CPTWebSocketClient::DownloadFile(string url, string headers, string save_path, int timeout_ms)
{
   uchar url_buf[], hdr_buf[], path_buf[];
   StringToCharArray(url, url_buf, 0, WHOLE_ARRAY, CP_UTF8);
   StringToCharArray(headers, hdr_buf, 0, WHOLE_ARRAY, CP_UTF8);
   StringToCharArray(save_path, path_buf, 0, WHOLE_ARRAY, CP_UTF8);
   return PTWS_DownloadFile(url_buf, hdr_buf, path_buf, timeout_ms);
}

//+------------------------------------------------------------------+
//| ApplyUpdate - Swap pending EA update on restart                       |
//+------------------------------------------------------------------+
int CPTWebSocketClient::ApplyUpdate(string target_dir, string current_filename, string new_filename)
{
   uchar td_buf[], cf_buf[], nf_buf[];
   StringToCharArray(target_dir, td_buf, 0, WHOLE_ARRAY, CP_UTF8);
   StringToCharArray(current_filename, cf_buf, 0, WHOLE_ARRAY, CP_UTF8);
   StringToCharArray(new_filename, nf_buf, 0, WHOLE_ARRAY, CP_UTF8);
   return PTWS_ApplyUpdate(td_buf, cf_buf, nf_buf);
}

//+------------------------------------------------------------------+
//| ScheduleRestart - Schedule terminal restart after update            |
//+------------------------------------------------------------------+
int CPTWebSocketClient::ScheduleRestart(string terminal_path, string data_path, string config_path, int restart_delay_ms)
{
   uchar tp_buf[], dp_buf[], cp_buf[];
   StringToCharArray(terminal_path, tp_buf, 0, WHOLE_ARRAY, CP_UTF8);
   StringToCharArray(data_path, dp_buf, 0, WHOLE_ARRAY, CP_UTF8);
   StringToCharArray(config_path, cp_buf, 0, WHOLE_ARRAY, CP_UTF8);
   return PTWS_ScheduleRestart(tp_buf, dp_buf, cp_buf, restart_delay_ms);
}

//+------------------------------------------------------------------+
//| ClearRestartCounter - Clear the restart counter file               |
//+------------------------------------------------------------------+
int CPTWebSocketClient::ClearRestartCounter(string data_path)
{
   uchar dp_buf[];
   StringToCharArray(data_path, dp_buf, 0, WHOLE_ARRAY, CP_UTF8);
   return PTWS_ClearRestartCounter(dp_buf);
}

//+------------------------------------------------------------------+
//| GetNtpTime - Get NTP time + drift (MT4 compatible)                    |
//+------------------------------------------------------------------+
bool CPTWebSocketClient::GetNtpTime(PTWS_NtpTime &info)
{
   return (PTWS_GetNtpTime(info) == 0);
}

#endif // PT_WEBSOCKET_CLIENT_MT4_MQH
