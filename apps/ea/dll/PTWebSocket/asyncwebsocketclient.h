//+------------------------------------------------------------------+
//| asyncwebsocketclient.h                                           |
//| PineTunnel WebSocket Client — WinHTTP Async WebSocket             |
//|                                                                    |
//| Rewritten: handle management, async callback state machine,        |
//| thread-safe frame queue, receive auto-chain, URL parsing.          |
//|                                                                    |
//| Inspired by MQL5 community WebSocket patterns, fully rewritten for PineTunnel|
//| v2.0 — Complete rewrite fixing critical bugs:                      |
//|   B1: Pointer-as-handle → atomic handle ID + shared_ptr map        |
//|   B2: Dual session → single global session                         |
//|   B3: Data never reached queue → receive auto-chain w/ per-client buffer|
//|   B4: shared_from_this → inherit enable_shared_from_this          |
//|   B5: Auth token discarded → WCHAR-to-UTF8 conversion              |
//|   B7/B8: URL parsing → extract hostname/path for WinHTTP           |
//+------------------------------------------------------------------+
#ifndef ASYNC_WEBSOCKET_CLIENT_H
#define ASYNC_WEBSOCKET_CLIENT_H

#ifdef _WIN32
  // WinHTTP WebSocket API requires Windows 8+ (0x0602).
  #ifndef _WIN32_WINNT
    #define _WIN32_WINNT 0x0602
  #elif _WIN32_WINNT < 0x0602
    #undef _WIN32_WINNT
    #define _WIN32_WINNT 0x0602
  #endif

  // WIN32_LEAN_AND_MEAN prevents winsock.h inclusion (conflicts with ws2tcpip.h)
  // Must be defined before windows.h
  #ifndef WIN32_LEAN_AND_MEAN
    #define WIN32_LEAN_AND_MEAN
  #endif

  // winsock2.h must be included before windows.h to avoid winsock.h conflicts
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #include <windows.h>
  #include <winhttp.h>
  #include <string>
  #include <mutex>
  #include <unordered_map>
  #include <memory>
  #include <atomic>
  #include <vector>
#endif

//+------------------------------------------------------------------+
//| Constants                                                          |
//+------------------------------------------------------------------+
#define PTWS_DLL_VERSION        "1.0.1"
#define PTWS_AUTH_KEY           "PTWS-7f2a9c4e1b"   // Protocol identifier - EA must pass this to PTWS_Connect
#define PTWS_MAX_FRAME_SIZE     65536      // 64KB max receive buffer
#define PTWS_MAX_QUEUE_DEPTH    500        // Max frames in queue (OOM prevention)
#define PTWS_INLINE_FRAME_SIZE  8192      // Inline buffer size per ring slot (covers 99%+ of frames)

//+------------------------------------------------------------------+
//| DLL Logging constants                                               |
//+------------------------------------------------------------------+
#define PTWS_LOG_ERROR    1    // Errors only
#define PTWS_LOG_INFO     2    // Errors + connection events
#define PTWS_LOG_DEBUG    3    // Everything (frame-level detail)
#define PTWS_LOG_BUFFER_SIZE   256    // Max chars per log entry
#define PTWS_LOG_RING_SIZE     1024   // Number of log entries in ring buffer

//+------------------------------------------------------------------+
//| WebSocket state enum                                               |
//+------------------------------------------------------------------+
enum ENUM_WEBSOCKET_STATE
{
   PTWS_CLOSED      = 0,   // Connection closed or not initialized
   PTWS_CLOSING     = 1,   // Close frame sent/received, closing in progress
   PTWS_CONNECTING  = 2,   // Connection in progress (handshake)
   PTWS_CONNECTED   = 3,   // Connected and ready for data
   PTWS_SENDING     = 4,   // Async send in progress
};

//+------------------------------------------------------------------+
//| WinHTTP callback status constants (for MQL5 wrapper)               |
//+------------------------------------------------------------------+
#define PTWS_CALLBACK_READ_COMPLETE     0x00020000
#define PTWS_CALLBACK_WRITE_COMPLETE   0x00040000
#define PTWS_CALLBACK_CLOSE_COMPLETE   0x00080000
#define PTWS_CALLBACK_REQUEST_ERROR    0x00010000
#define PTWS_CALLBACK_SECURE_CHANNEL_ERROR 0x01000000

//+------------------------------------------------------------------+
//| Frame priority for smart queue overflow handling                    |
//| Lower value = higher priority. Used to discard low-priority frames |
//| (telemetry, pings) before trading signals when queue fills up.     |
//+------------------------------------------------------------------+
enum FramePriority : BYTE
{
   PRIORITY_SIGNAL     = 0,   // Trading signals (buy/sell/modify/close)
   PRIORITY_ACK        = 1,   // Signal acknowledgments
   PRIORITY_TELEMETRY  = 2,   // Account stats, positions, health
   PRIORITY_PING       = 3,   // Ping/pong heartbeat frames
};

//+------------------------------------------------------------------+
//| Ring buffer slot for pre-allocated frame queue (F2/F3 optimization)|
//| Each slot has an inline buffer for small payloads (<=8KB) and a    |
//| vector overflow for rare large frames. This eliminates per-frame   |
//| heap allocation in the hot receive path.                           |
//+------------------------------------------------------------------+
struct FrameSlot
{
   BYTE        data[PTWS_INLINE_FRAME_SIZE];   // Inline storage (8KB, covers 99%+ of frames)
   DWORD       dataSize;                         // Valid bytes in data[]
   WINHTTP_WEB_SOCKET_BUFFER_TYPE bufferType;
   FramePriority priority;                       // Frame priority for smart eviction
   bool        hasData;                          // true = slot contains unread data
   std::vector<BYTE> overflow;                   // For frames > PTWS_INLINE_FRAME_SIZE (rare)
   bool        usesOverflow;                     // true = overflow vector holds data

   FrameSlot() : dataSize(0), bufferType(WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE),
                 priority(PRIORITY_TELEMETRY), hasData(false), usesOverflow(false) {}
};

//+------------------------------------------------------------------+
//| Parsed WebSocket URL parts                                          |
//+------------------------------------------------------------------+
struct WsUrlParts
{
   std::wstring hostname;      // Server hostname only (e.g., L"your-server.com")
   std::wstring path;           // URL path (e.g., L"/ws/ABC123")
   INTERNET_PORT port;          // Port number (default 443 for wss, 80 for ws)
   bool secure;                 // true for wss, false for ws
};

//+------------------------------------------------------------------+
//| Connection statistics — returned by PTWS_GetStats                   |
//| Must be kept in sync with MQL wrapper definitions.                  |
//| pack(1) matches MQL5/4 1-byte packing for DLL interop structs.     |
//+------------------------------------------------------------------+
#pragma pack(push, 1)
struct PTWS_ConnectionStats
{
   DWORD uptime_sec;           // Seconds since Connect()
   DWORD bytes_sent;           // Total bytes sent (32-bit, wraps at 4GB — use V2 for 64-bit)
   DWORD bytes_received;       // Total bytes received (32-bit, wraps at 4GB — use V2 for 64-bit)
   DWORD reconnect_count;      // Number of reconnect attempts
   DWORD frames_queued;        // Frames currently in queue
   DWORD frames_dropped;       // Frames dropped due to queue overflow (32-bit — use V2 for 64-bit)
   DWORD ws_latency_ms;        // Last measured WS round-trip latency (ping→pong)
   DWORD terminal_lag_ms;      // Milliseconds since last Poll() call (EA thread responsiveness)
};

//+------------------------------------------------------------------+
//| Connection statistics V2 — 64-bit counters, returned by PTWS_GetStatsV2|
//| Uses DWORD pairs for MT4 compatibility (MQL4 has no 64-bit type).   |
//| Layout is identical on MT4 and MT5 (all fields are 4-byte int).     |
//+------------------------------------------------------------------+
struct PTWS_ConnectionStatsV2
{
   DWORD struct_version;        // Always 2 — allows future detection
   DWORD uptime_sec;            // Seconds since Connect() (wraps at 49 days, acceptable)
   DWORD bytes_sent_low;        // Low 32 bits of total bytes sent
   DWORD bytes_sent_high;       // High 32 bits of total bytes sent
   DWORD bytes_received_low;    // Low 32 bits of total bytes received
   DWORD bytes_received_high;   // High 32 bits of total bytes received
   DWORD reconnect_count;       // Number of reconnect attempts
   DWORD frames_queued;         // Frames currently in queue
   DWORD frames_dropped_low;    // Low 32 bits of frames dropped
   DWORD frames_dropped_high;   // High 32 bits of frames dropped
   DWORD ws_latency_ms;         // Last measured WS round-trip latency (ping→pong)
   DWORD terminal_lag_ms;       // Milliseconds since last Poll() call
};
#pragma pack(pop)

//+------------------------------------------------------------------+
//| WebSocketClient class — async WinHTTP WebSocket                     |
//| Inherits enable_shared_from_this for safe callback dispatch.        |
//+------------------------------------------------------------------+
class WebSocketClient : public std::enable_shared_from_this<WebSocketClient>
{
   // Callback needs access to private handler methods
   friend VOID CALLBACK WebSocketCallback(
      HINTERNET, DWORD_PTR, DWORD, LPVOID, DWORD);

private:
   // WinHTTP handles (NOT owned: m_hSession is global; owned: m_hConnect, m_hRequest, m_hWebSocket)
   HINTERNET                  m_hConnect;           // WinHTTP connection handle
   HINTERNET                  m_hRequest;           // WinHTTP request handle (during upgrade)
   HINTERNET                  m_hWebSocket;         // WinHTTP WebSocket handle (after upgrade)
   std::atomic<DWORD>          m_errorCode;          // Last error code (atomic: read by MQL, set by callback)
   std::atomic<DWORD>          m_lastCallback;       // Last callback status (atomic: read by MQL, set by callback)
   std::atomic<ENUM_WEBSOCKET_STATE> m_state;         // Current connection state (atomic: read by MQL, set by callback)
   FrameSlot                   m_ring[PTWS_MAX_QUEUE_DEPTH];  // Pre-allocated ring buffer
   size_t                      m_ringWriteIdx;        // Ring buffer write index (producer)
   size_t                      m_ringReadIdx;         // Ring buffer read index (consumer)
   size_t                      m_ringCount;           // Number of filled slots in ring buffer
   std::mutex                  m_frameMutex;          // Thread-safe frame queue access
   std::string                 m_licenseKey;         // License key for auth (UTF-8)
   std::wstring                m_hostname;           // Server hostname (for reconnect)
   std::wstring                m_path;               // URL path (for reconnect)
   INTERNET_PORT               m_port;               // Server port
    bool                        m_secure;             // WSS vs WS


   // Persistent receive buffer for async WinHTTP reads
   // Data is copied from this buffer into the ring buffer in the callback,
   // then a new receive is started (auto-chaining).
    BYTE                        m_receiveBuffer[PTWS_MAX_FRAME_SIZE];
    std::vector<BYTE>           m_fragAccumulator;     // Accumulator for fragmented WebSocket frames

   // Callback tracking for async operations (atomic: accessed from MQL thread and WinHTTP callback thread)
   std::atomic<bool>           m_readPending;        // Whether an async read is in progress
   std::atomic<bool>           m_sendPending;        // Whether an async send is in progress
   std::atomic<bool>           m_isValid;            // true = client is usable; false = being destroyed (prevents callback use-after-free)

   // Connection statistics (64-bit to prevent overflow)
   uint64_t                    m_bytesSent;           // Total bytes sent
   uint64_t                    m_bytesReceived;       // Total bytes received from frames
   uint64_t                    m_framesDropped;       // Frames dropped due to queue overflow
   DWORD                       m_connectTimestamp;     // GetTickCount() at Connect() time

   // Latency tracking
   DWORD                       m_lastPingTimestamp;    // GetTickCount() when last ping was sent
   DWORD                       m_wsLatencyMs;          // Last measured WS round-trip (ping→pong) in ms
   DWORD                       m_lastPollTimestamp;     // GetTickCount() of last Poll() call

   // Notification window for PostMessage (OnChartEvent wake-up)
   HWND                        m_notifyWnd;            // Chart window handle for PostMessage notifications
   UINT                        m_notifyMsg;            // Custom message ID (WM_USER + offset)

   // Internal methods
   VOID  ResetState(bool resetError = true);
   VOID  SetError(DWORD error);
   VOID  OnSendRequestComplete();
   VOID  OnHeadersAvailable();
   VOID  OnReadComplete(DWORD bytesRead, WINHTTP_WEB_SOCKET_BUFFER_TYPE bufferType);
   VOID  OnSendComplete(DWORD bytesSent);
   VOID  OnClose();
   VOID  OnError(const WINHTTP_ASYNC_RESULT* result);
   VOID  OnSecureChannelError(DWORD error);
   VOID  CleanupHandles();

   // Frame priority detection (lightweight prefix scan, no full JSON parse)
   static FramePriority DetectPriority(const BYTE* data, DWORD size);

   // URL parsing
   static bool ParseWebSocketUrl(const WCHAR* url, WsUrlParts& parts);

public:
   WebSocketClient();
   ~WebSocketClient();

   // Connection lifecycle
   DWORD Connect(const WCHAR* url, INTERNET_PORT port, DWORD secure, const WCHAR* wsParam);
   DWORD Close(WINHTTP_WEB_SOCKET_CLOSE_STATUS status = WINHTTP_WEB_SOCKET_SUCCESS_CLOSE_STATUS,
               const char* reason = nullptr, DWORD reasonLen = 0);
   VOID  Free();

   // Data transfer
   DWORD Send(WINHTTP_WEB_SOCKET_BUFFER_TYPE bufferType, const void* pBuffer, DWORD dwLength);

   // Start async receive — called by PTWS_Poll to ensure receive chain is active
   DWORD StartAsyncReceive();

   // Frame queue (read by MQL5 OnTimer via PTWS_Read / PTWS_Readable)
   VOID  Read(BYTE* pBuffer, DWORD bufferLength, DWORD* bytesRead,
              WINHTTP_WEB_SOCKET_BUFFER_TYPE* pBufferType);
   DWORD Readable();   // Returns bytes available in front frame, 0 if empty

   // State queries
   ENUM_WEBSOCKET_STATE Status()          const { return m_state; }
   DWORD                LastError()       const { return m_errorCode; }
   DWORD                LastCallback()    const { return m_lastCallback; }
   bool                 IsReadPending()   const { return m_readPending; }
   bool                 IsValid()         const { return m_isValid; }

   // Configuration
   VOID SetNotifyWindow(HWND wnd, UINT msg) { m_notifyWnd = wnd; m_notifyMsg = msg; }

   // Statistics
   VOID GetStats(PTWS_ConnectionStats* stats);
   VOID GetStatsV2(PTWS_ConnectionStatsV2* stats);

   // Ring buffer queue depth (for backpressure signaling)
   size_t GetQueueDepth();

   // Latency tracking
   VOID RecordPingSent();           // Call when EA sends a ping — records timestamp
   VOID UpdatePollTimestamp();      // Called from PTWS_Poll — tracks terminal responsiveness

};

//+------------------------------------------------------------------+
//| Global handle management                                            |
//| Numeric handle IDs (long) map to shared_ptr<WebSocketClient>.       |
//| WinHTTP HINTERNET handles map to weak_ptr for callback dispatch.    |
//+------------------------------------------------------------------+
extern std::mutex g_handleMapMutex;
extern std::atomic<long> g_nextHandleId;
extern std::unordered_map<long, std::shared_ptr<WebSocketClient>> g_handleMap;

extern std::mutex g_internetMutex;
extern std::unordered_map<HINTERNET, std::weak_ptr<WebSocketClient>> g_internetMap;

//+------------------------------------------------------------------+
//| Global WinHTTP session (shared across all connections)               |
//+------------------------------------------------------------------+
extern HINTERNET g_hSession;
extern std::mutex g_sessionMutex;
HINTERNET GetOrCreateSession();

//+------------------------------------------------------------------+
//| Callback function (called by WinHTTP thread pool)                   |
//| Dispatches to the correct WebSocketClient instance via g_internetMap|
//+------------------------------------------------------------------+
VOID CALLBACK WebSocketCallback(
   HINTERNET hInternet,
   DWORD_PTR dwContext,
   DWORD dwInternetStatus,
   LPVOID lpvStatusInformation,
   DWORD dwStatusInformationLength
);

//+------------------------------------------------------------------+
//| DLL-level ring buffer logging                                       |
//| Thread-safe rotating log for debugging connection issues             |
//| without modifying EA code.                                          |
//+------------------------------------------------------------------+
struct LogEntry
{
   DWORD  timestamp;                          // GetTickCount() at log time
   int    level;                              // PTWS_LOG_ERROR, PTWS_LOG_INFO, PTWS_LOG_DEBUG
   char   message[PTWS_LOG_BUFFER_SIZE];       // Formatted message
};

// Global log state (shared across all client instances)
extern std::mutex      g_logMutex;
extern LogEntry        g_logBuffer[PTWS_LOG_RING_SIZE];
extern std::atomic<int> g_logCount;            // Total entries written (wraps)
extern std::atomic<int> g_logLevel;            // Current log level (0=off)

// Log function — called from WebSocketClient methods and callback
void PTWS_Log(int level, const char* format, ...);

//+------------------------------------------------------------------+
//| VPS detection result                                                 |
//| Detects if the EA is running on a known VPS provider.                |
//| Uses WMI queries to check system manufacturer/model.                 |
//| pack(1) matches MQL5/4 1-byte packing for DLL interop structs.     |
//+------------------------------------------------------------------+
#pragma pack(push, 1)
struct PTWS_VpsInfo
{
   int    is_vps;                  // 1 if running on VPS, 0 if physical machine
   char   provider[64];           // VPS provider name (e.g., "AWS", "Hetzner", "Unknown")
   char   manufacturer[128];      // System manufacturer from WMI
   char   model[128];             // System model from WMI
};

//+------------------------------------------------------------------+
//| Network diagnostics result                                           |
//| ICMP ping to a target host + quality grading.                        |
//| Quality grading thresholds:                                          |
//|   Good:     loss<=1%, avg_rtt<=100ms, jitter<=50ms                    |
//|   Moderate: loss<=5%, avg_rtt<=200ms, jitter<=100ms                  |
//|   Bad:      loss>5% or avg_rtt>200ms or jitter>100ms                  |
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
   char   target_host[256];        // Ping target hostname/IP
   int    is_internet_available;   // 1 if internet connectivity confirmed, 0 if not
   char   quality[16];            // "Good", "Moderate", or "Bad"
};

//+------------------------------------------------------------------+
//| NTP time sync result                                                 |
//| Returns NTP UTC time and drift from local system clock.             |
//+------------------------------------------------------------------+
struct PTWS_NtpTime
{
   int    ntp_time_s;              // NTP time as Unix epoch seconds (UTC)
   int    drift_ms;                // Drift from local clock in ms (+10ms buffer included)
   int    sync_success;            // 1 if sync succeeded, 0 if failed
};
#pragma pack(pop)

#endif // ASYNC_WEBSOCKET_CLIENT_H
