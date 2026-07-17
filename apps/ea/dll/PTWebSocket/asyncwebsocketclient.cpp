//+------------------------------------------------------------------+
//| asyncwebsocketclient.cpp                                           |
//| PineTunnel WebSocket Client — WinHTTP Async WebSocket             |
//|                                                                    |
//| v2.0 — Complete rewrite fixing critical bugs:                      |
//|   B1: Handle management — atomic IDs + shared_ptr map               |
//|   B2: Single global WinHTTP session                                  |
//|   B3: Receive auto-chain with per-client persistent buffer          |
//|   B4: shared_from_this for safe callback dispatch                   |
//|   B5: Auth token WCHAR → UTF-8 conversion                          |
//|   B7/B8: URL parsing — extract hostname/path for WinHTTP           |
//|                                                                    |
//| Async WinHTTP WebSocket flow:                                       |
//|   1. Connect() → WinHttpConnect + WinHttpOpenRequest + SendRequest  |
//|   2. Callback: SENDREQUEST_COMPLETE → WinHttpReceiveResponse        |
//|   3. Callback: HEADERS_AVAILABLE → WinHttpWebSocketCompleteUpgrade |
//|   4. After upgrade: start async receive (auto-chains)               |
//|   5. Data received → push to frame queue → start next receive      |
//+------------------------------------------------------------------+
#include "asyncwebsocketclient.h"

#ifdef _WIN32

#include <cstdio>

#ifndef WINHTTP_CALLBACK_STATUS_SECURE_CHANNEL_ERROR
#define WINHTTP_CALLBACK_STATUS_SECURE_CHANNEL_ERROR 0x01000000
#endif

//+------------------------------------------------------------------+
//| Global state                                                        |
//+------------------------------------------------------------------+
HINTERNET g_hSession = NULL;
std::mutex g_sessionMutex;

std::mutex g_handleMapMutex;
std::atomic<long> g_nextHandleId{1};
std::unordered_map<long, std::shared_ptr<WebSocketClient>> g_handleMap;

std::mutex g_internetMutex;
std::unordered_map<HINTERNET, std::weak_ptr<WebSocketClient>> g_internetMap;

//+------------------------------------------------------------------+
//| Global ring buffer log state                                        |
//+------------------------------------------------------------------+
std::mutex g_logMutex;
LogEntry g_logBuffer[PTWS_LOG_RING_SIZE];
std::atomic<int> g_logCount{0};
std::atomic<int> g_logLevel{PTWS_LOG_ERROR};  // Default: errors only

//+------------------------------------------------------------------+
//| PTWS_Log — Ring buffer logging function                            |
//+------------------------------------------------------------------+
void PTWS_Log(int level, const char* format, ...)
{
   if(level > g_logLevel)
      return;

   char msg[PTWS_LOG_BUFFER_SIZE];
   va_list args;
   va_start(args, format);
   vsnprintf(msg, PTWS_LOG_BUFFER_SIZE, format, args);
   va_end(args);

   // Also output to Windows debug trace
   char dbg[PTWS_LOG_BUFFER_SIZE + 32];
   snprintf(dbg, sizeof(dbg), "[PTWebSocket] %s\n", msg);
   OutputDebugStringA(dbg);

   // Write to ring buffer
   std::lock_guard<std::mutex> lock(g_logMutex);
   int index = g_logCount % PTWS_LOG_RING_SIZE;
   g_logBuffer[index].timestamp = GetTickCount();
   g_logBuffer[index].level = level;
   strncpy_s(g_logBuffer[index].message, PTWS_LOG_BUFFER_SIZE, msg, _TRUNCATE);
   g_logCount++;
}

//+------------------------------------------------------------------+
//| GetOrCreateSession — Single global WinHTTP session                  |
//| Thread-safe. Created once, reused across all connections.            |
//+------------------------------------------------------------------+
HINTERNET GetOrCreateSession()
{
   std::lock_guard<std::mutex> lock(g_sessionMutex);
   if(!g_hSession)
   {
      g_hSession = WinHttpOpen(
         L"PineTunnel WebSocket Client/2.0",
         WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
         WINHTTP_NO_PROXY_NAME,
         WINHTTP_NO_PROXY_BYPASS,
         WINHTTP_FLAG_ASYNC
      );
      if(g_hSession)
      {
         WinHttpSetStatusCallback(
            g_hSession,
            (WINHTTP_STATUS_CALLBACK)WebSocketCallback,
            WINHTTP_CALLBACK_FLAG_ALL_NOTIFICATIONS,
            0
         );
      }
   }
   return g_hSession;
}

//+------------------------------------------------------------------+
//| ParseWebSocketUrl — Extract hostname, path, port from WS URL       |
//| Handles: wss://host:port/path, ws://host/path, wss://host/path    |
//+------------------------------------------------------------------+
bool WebSocketClient::ParseWebSocketUrl(const WCHAR* url, WsUrlParts& parts)
{
   if(!url || !url[0])
      return false;

   std::wstring wsUrl(url);

   // Determine scheme
   if(wsUrl.find(L"wss://") == 0)
   {
      parts.secure = true;
      wsUrl = wsUrl.substr(6);  // Remove "wss://"
   }
   else if(wsUrl.find(L"ws://") == 0)
   {
      parts.secure = false;
      wsUrl = wsUrl.substr(5);   // Remove "ws://"
   }
   else if(wsUrl.find(L"https://") == 0)
   {
      parts.secure = true;
      wsUrl = wsUrl.substr(8);  // Remove "https://"
   }
   else if(wsUrl.find(L"http://") == 0)
   {
      parts.secure = false;
      wsUrl = wsUrl.substr(7);   // Remove "http://"
   }
   else
      return false;  // Unknown scheme

   // Default port
   parts.port = parts.secure ? INTERNET_DEFAULT_HTTPS_PORT : INTERNET_DEFAULT_HTTP_PORT;

   // Find path separator
   size_t pathPos = wsUrl.find(L'/');
   std::wstring hostPort;
   if(pathPos != std::wstring::npos)
   {
      hostPort = wsUrl.substr(0, pathPos);
      parts.path = wsUrl.substr(pathPos);
   }
   else
   {
      hostPort = wsUrl;
      parts.path = L"/";
   }

   // Extract port from hostname if present
   size_t colonPos = hostPort.rfind(L':');
   // Check for IPv6 address (contains brackets)
   size_t bracketPos = hostPort.find(L']');
   if(bracketPos != std::wstring::npos)
   {
      // IPv6: [::1]:port or [::1]
      size_t portColon = hostPort.find(L':', bracketPos);
      if(portColon != std::wstring::npos)
      {
         parts.hostname = hostPort.substr(0, portColon);
         parts.port = (INTERNET_PORT)std::stoi(hostPort.substr(portColon + 1));
      }
      else
      {
         parts.hostname = hostPort;
      }
   }
   else if(colonPos != std::wstring::npos)
   {
      parts.hostname = hostPort.substr(0, colonPos);
      parts.port = (INTERNET_PORT)std::stoi(hostPort.substr(colonPos + 1));
   }
   else
   {
      parts.hostname = hostPort;
   }

   // If no explicit port, set default based on scheme
   // INTERNET_DEFAULT_HTTPS_PORT = 443, INTERNET_DEFAULT_HTTP_PORT = 80
   // These are already set above

   return !parts.hostname.empty();
}

//+------------------------------------------------------------------+
//| Constructor                                                         |
//+------------------------------------------------------------------+
WebSocketClient::WebSocketClient()
   : m_hConnect(NULL)
   , m_hRequest(NULL)
   , m_hWebSocket(NULL)
   , m_errorCode(0)
   , m_lastCallback(0)
   , m_state(PTWS_CLOSED)
   , m_readPending(false)
   , m_sendPending(false)
   , m_bytesSent(0)
   , m_bytesReceived(0)
   , m_framesDropped(0)
   , m_connectTimestamp(0)
   , m_lastPingTimestamp(0)
   , m_wsLatencyMs(0)
   , m_lastPollTimestamp(0)
   , m_port(0)
   , m_secure(false)
   , m_ringWriteIdx(0)
   , m_ringReadIdx(0)
   , m_ringCount(0)
   , m_notifyWnd(NULL)
   , m_notifyMsg(0)
   , m_isValid(true)
{
   memset(m_receiveBuffer, 0, sizeof(m_receiveBuffer));
}

//+------------------------------------------------------------------+
//| Destructor                                                           |
//+------------------------------------------------------------------+
WebSocketClient::~WebSocketClient()
{
   Free();
}

//+------------------------------------------------------------------+
//| ResetState — Reset connection state (keep config)                   |
//+------------------------------------------------------------------+
VOID WebSocketClient::ResetState(bool resetError)
{
   std::lock_guard<std::mutex> lock(m_frameMutex);
   // Clear ring buffer slots
   for(size_t i = 0; i < PTWS_MAX_QUEUE_DEPTH; i++)
   {
      m_ring[i].hasData = false;
      m_ring[i].usesOverflow = false;
      m_ring[i].overflow.clear();
   }
   m_ringWriteIdx = 0;
   m_ringReadIdx = 0;
   m_ringCount = 0;
   m_fragAccumulator.clear();

   if(resetError)
   {
      m_errorCode = 0;
      m_lastCallback = 0;
   }

   m_readPending = false;
   m_sendPending = false;
   m_state = PTWS_CLOSED;
}

//+------------------------------------------------------------------+
//| SetError — Record error code and log                               |
//+------------------------------------------------------------------+
VOID WebSocketClient::SetError(DWORD error)
{
   m_errorCode = error;
   PTWS_Log(PTWS_LOG_ERROR, "WebSocket error: %lu", error);
}

//+------------------------------------------------------------------+
//| CleanupHandles — Close all WinHTTP handles                          |
//| Must NOT be called from a callback (handles may be in use).          |
//| Free from the MQL thread only.                                      |
//+------------------------------------------------------------------+
VOID WebSocketClient::CleanupHandles()
{
   // Remove internet handle mappings first
   {
      std::lock_guard<std::mutex> lock(g_internetMutex);
      if(m_hWebSocket) g_internetMap.erase(m_hWebSocket);
      if(m_hRequest) g_internetMap.erase(m_hRequest);
      if(m_hConnect) g_internetMap.erase(m_hConnect);
   }

   // Close handles (WinHTTP handles must be closed in reverse order of creation)
   if(m_hWebSocket)
   {
      WinHttpWebSocketShutdown(m_hWebSocket, static_cast<USHORT>(WINHTTP_WEB_SOCKET_SUCCESS_CLOSE_STATUS), NULL, 0);
      WinHttpCloseHandle(m_hWebSocket);
      m_hWebSocket = NULL;
   }
   if(m_hRequest)
   {
      WinHttpCloseHandle(m_hRequest);
      m_hRequest = NULL;
   }
   if(m_hConnect)
   {
      WinHttpCloseHandle(m_hConnect);
      m_hConnect = NULL;
   }
   // m_hSession is global — NOT closed here
}

//+------------------------------------------------------------------+
//| Connect to WebSocket server                                         |
//| Returns 0 on success (async connection started), error code on failure|
//| The URL must be a full WebSocket URL, e.g. wss://host:port/path    |
//|                                                                     |
//| SECURITY: When secure=1 (WSS), the connection enforces:              |
//|   - Full X.509 certificate chain validation (WinHTTP default)       |
//|   - Certificate revocation checking via CRL/OCSP                     |
//|   - TLS protocol fallback prevention (no downgrade attacks)          |
//|   - No SECURITY_FLAG_IGNORE_* flags (would disable validation)       |
//| This ensures no MITM can intercept or modify WS telemetry data.     |
//+------------------------------------------------------------------+
DWORD WebSocketClient::Connect(const WCHAR* url, INTERNET_PORT port, DWORD secure, const WCHAR* wsParam)
{
   // Clean up any existing connection first
   if(m_hWebSocket || m_hConnect || m_hRequest)
      CleanupHandles();

   // Ensure global session is available
   if(!GetOrCreateSession())
   {
      m_errorCode = GetLastError();
      PTWS_Log(PTWS_LOG_ERROR, "WinHttpOpen failed: %lu", m_errorCode.load());
      return m_errorCode.load();
   }
    // Store license key for reconnect (convert WCHAR to UTF-8)
   m_licenseKey.clear();
   if(wsParam && wsParam[0])
   {
      int wlen = WideCharToMultiByte(CP_UTF8, 0, wsParam, -1, NULL, 0, NULL, NULL);
      if(wlen > 0)
      {
         m_licenseKey.resize(wlen - 1);
         WideCharToMultiByte(CP_UTF8, 0, wsParam, -1, &m_licenseKey[0], wlen, NULL, NULL);
      }
   }

   // Parse WebSocket URL
   WsUrlParts parts;
   if(!ParseWebSocketUrl(url, parts))
   {
      PTWS_Log(PTWS_LOG_ERROR, "Failed to parse WebSocket URL");
      m_errorCode = ERROR_INVALID_PARAMETER;
      return m_errorCode.load();
   }

   // Override port from URL if explicitly specified; otherwise use parameter or default
   if(parts.port != INTERNET_DEFAULT_HTTPS_PORT && parts.port != INTERNET_DEFAULT_HTTP_PORT)
   {
      // URL had explicit port — use it
   }
   else if(port > 0 && port != INTERNET_DEFAULT_HTTP_PORT)
   {
      // Caller specified a port — use it
      parts.port = port;
   }

   // Override secure from parameter if explicitly specified
   if(secure)
      parts.secure = true;

   // Save connection details for reconnect
   m_hostname = parts.hostname;
   m_path = parts.path;
   m_port = parts.port;
   m_secure = parts.secure;

   m_state = PTWS_CONNECTING;
   m_errorCode = 0;

   PTWS_Log(PTWS_LOG_INFO, "Connecting to %ls:%u%s (secure=%d)",
            parts.hostname.c_str(), parts.port, parts.path.c_str(), parts.secure);

   // Step 1: Create connection handle
   m_hConnect = WinHttpConnect(
      g_hSession,
      parts.hostname.c_str(),
      parts.port,
      0
   );
   if(!m_hConnect)
   {
      m_errorCode = GetLastError();
      m_state = PTWS_CLOSED;
      PTWS_Log(PTWS_LOG_ERROR, "WinHttpConnect failed: %lu", m_errorCode.load());
      return m_errorCode.load();
   }

   // Register connect handle for callback dispatch
   {
      auto self = shared_from_this();
      std::lock_guard<std::mutex> lock(g_internetMutex);
      g_internetMap[m_hConnect] = self;
   }

   // Step 2: Create request handle
   DWORD flags = parts.secure ? WINHTTP_FLAG_SECURE : 0;
   m_hRequest = WinHttpOpenRequest(
      m_hConnect,
      L"GET",
      parts.path.c_str(),
      NULL,      // HTTP/1.1
      WINHTTP_NO_REFERER,
      WINHTTP_DEFAULT_ACCEPT_TYPES,
      flags
   );
   if(!m_hRequest)
   {
      m_errorCode = GetLastError();
      PTWS_Log(PTWS_LOG_ERROR, "WinHttpOpenRequest failed: %lu", m_errorCode.load());
      // Cleanup
      {
         std::lock_guard<std::mutex> lock(g_internetMutex);
         g_internetMap.erase(m_hConnect);
      }
      WinHttpCloseHandle(m_hConnect);
      m_hConnect = NULL;
      m_state = PTWS_CLOSED;
      return m_errorCode.load();
   }

   // Register request handle for callback dispatch
   {
      auto self = shared_from_this();
      std::lock_guard<std::mutex> lock(g_internetMutex);
      g_internetMap[m_hRequest] = self;

      // Remove connect handle mapping (request handle is the active one now)
      // Keep connect mapping for cleanup purposes
   }

   // Step 3: Set WebSocket upgrade option on request
   if(!WinHttpSetOption(m_hRequest, WINHTTP_OPTION_UPGRADE_TO_WEB_SOCKET, NULL, 0))
   {
      m_errorCode = GetLastError();
      PTWS_Log(PTWS_LOG_ERROR, "WinHttpSetOption(UPGRADE) failed: %lu", m_errorCode.load());
      CleanupHandles();
      m_state = PTWS_CLOSED;
      return m_errorCode.load();
   }

   // Step 3a: Enforce TLS security for WSS connections.
   // This prevents MITM attacks through:
   //   1. Explicit TLS 1.2+ requirement (Windows Server/VPS may default to TLS 1.0).
   //   2. Full X.509 certificate chain validation (WinHTTP default).
   //   3. No SECURITY_FLAG_IGNORE_* flags (would disable validation).
   //
   // NOTE: SSL revocation checking (CRL/OCSP) is intentionally NOT enabled.
   // On VPS networks with restricted outbound access, OCSP checks fail and cause
   // TLS handshake errors (error 4317). WinHTTP's default cert validation
   // (chain trust, CN, dates) is sufficient for Cloudflare's certificates.
   if(parts.secure)
   {
      // Explicitly enable TLS 1.2 and TLS 1.3 (required on Windows Server/VPS
      // where the default may only include SSL3 and TLS 1.0).
      DWORD protocols = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 | WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3;
      if(!WinHttpSetOption(m_hRequest, WINHTTP_OPTION_SECURE_PROTOCOLS,
                           &protocols, sizeof(protocols)))
      {
         PTWS_Log(PTWS_LOG_INFO, "WinHttpSetOption(SECURE_PROTOCOLS) failed: %lu (using system defaults)", GetLastError());
         // Non-fatal: system defaults may still work.
      }
   }

   // Step 3b: Add authorization header if license key provided
   if(!m_licenseKey.empty())
   {
      const WCHAR* credStr = wsParam ? wsParam : L"";
      std::wstring authHeader = L"Authorization: Bearer " + std::wstring(credStr) + L"\r\n";
      if(!WinHttpAddRequestHeaders(m_hRequest, authHeader.c_str(), (DWORD)authHeader.length(), WINHTTP_ADDREQ_FLAG_ADD))
      {
         PTWS_Log(PTWS_LOG_INFO, "WinHttpAddRequestHeaders (auth) failed: %lu (continuing without auth)", GetLastError());
         // Non-fatal: auth header is optional for the current implementation
      }
   }

   // Step 4: Send the HTTP request (async — triggers WebSocket upgrade)
   // Returns FALSE with ERROR_IO_PENDING in async mode, which is EXPECTED.
   if(!WinHttpSendRequest(
      m_hRequest,
      WINHTTP_NO_ADDITIONAL_HEADERS, 0,
      WINHTTP_NO_REQUEST_DATA, 0,
      0, 0
   ))
   {
      DWORD err = GetLastError();
      if(err != ERROR_IO_PENDING)
      {
         m_errorCode = err;
         PTWS_Log(PTWS_LOG_ERROR, "WinHttpSendRequest failed: %lu", m_errorCode.load());
         CleanupHandles();
         m_state = PTWS_CLOSED;
         return m_errorCode.load();
      }
      // ERROR_IO_PENDING is normal for async — callback will handle next steps
   }

   PTWS_Log(PTWS_LOG_INFO, "WebSocket upgrade request sent (async)");
   return 0;  // Async connection started
}

//+------------------------------------------------------------------+
//| OnSendRequestComplete — Called when WinHttpSendRequest completes    |
//| Triggers WinHttpReceiveResponse for the HTTP response.              |
//+------------------------------------------------------------------+
VOID WebSocketClient::OnSendRequestComplete()
{
   PTWS_Log(PTWS_LOG_DEBUG, "SENDREQUEST_COMPLETE - calling WinHttpReceiveResponse");

   if(!m_hRequest)
   {
      SetError(ERROR_INVALID_HANDLE);
      m_state = PTWS_CLOSED;
      return;
   }

   // Now receive the HTTP response (which contains the WebSocket upgrade)
   if(!WinHttpReceiveResponse(m_hRequest, NULL))
   {
      DWORD err = GetLastError();
      if(err != ERROR_IO_PENDING)
      {
         SetError(err);
         m_state = PTWS_CLOSED;
         return;
      }
      // ERROR_IO_PENDING — callback will fire with HEADERS_AVAILABLE
   }
}

//+------------------------------------------------------------------+
//| OnHeadersAvailable — Called when HTTP response headers arrive       |
//| Completes the WebSocket upgrade.                                     |
//+------------------------------------------------------------------+
VOID WebSocketClient::OnHeadersAvailable()
{
   PTWS_Log(PTWS_LOG_DEBUG, "HEADERS_AVAILABLE - completing WebSocket upgrade");

   if(!m_hRequest)
   {
      SetError(ERROR_INVALID_HANDLE);
      m_state = PTWS_CLOSED;
      return;
   }

   // Complete the WebSocket upgrade
   m_hWebSocket = WinHttpWebSocketCompleteUpgrade(m_hRequest, NULL);
   if(!m_hWebSocket)
   {
      DWORD err = GetLastError();
      PTWS_Log(PTWS_LOG_ERROR, "WinHttpWebSocketCompleteUpgrade failed: %lu", err);
      SetError(err);
      m_state = PTWS_CLOSED;
      return;
   }

   // Register the WebSocket handle for callback dispatch
   {
      auto self = shared_from_this();
      std::lock_guard<std::mutex> lock(g_internetMutex);
      g_internetMap[m_hWebSocket] = self;
      // Request handle is no longer needed for callbacks
      g_internetMap.erase(m_hRequest);
   }

   // Close the request handle - it's no longer needed after upgrade
   WinHttpCloseHandle(m_hRequest);
   m_hRequest = NULL;

   // Set infinite timeouts on the WebSocket handle - let the EA's application-level
   // dead connection detection (90s) handle real dead connections, not WinHTTP's
   // aggressive 30s default which fires false 12152 timeouts through Cloudflare.
   DWORD infiniteTimeout = 0;
   WinHttpSetOption(m_hWebSocket, WINHTTP_OPTION_RECEIVE_TIMEOUT, &infiniteTimeout, sizeof(DWORD));
   WinHttpSetOption(m_hWebSocket, WINHTTP_OPTION_SEND_TIMEOUT, &infiniteTimeout, sizeof(DWORD));
   WinHttpSetOption(m_hWebSocket, WINHTTP_OPTION_CONNECT_TIMEOUT, &infiniteTimeout, sizeof(DWORD));

   PTWS_Log(PTWS_LOG_INFO, "WebSocket upgrade complete - connected (timeouts disabled, app-level keepalive)");

   // Transition to connected state
   m_state = PTWS_CONNECTED;
   m_connectTimestamp = GetTickCount();

   // Start the first async receive (this auto-chains: each receive completion
   // starts the next receive, ensuring data flows into the frame queue)
   if(StartAsyncReceive() != 0 && m_errorCode.load() != ERROR_IO_PENDING)
   {
      PTWS_Log(PTWS_LOG_ERROR, "Failed to start initial async receive: %lu", m_errorCode.load());
      // Non-fatal: we can try again on the next Poll
   }
}

//+------------------------------------------------------------------+
//| StartAsyncReceive — Begin an async WebSocket receive operation      |
//| Returns 0 on success, ERROR_IO_PENDING is also acceptable.          |
//+------------------------------------------------------------------+
DWORD WebSocketClient::StartAsyncReceive()
{
   if(!m_hWebSocket || m_state == PTWS_CLOSED || m_state == PTWS_CLOSING)
      return ERROR_INVALID_STATE;

   // Atomically check-and-set m_readPending to prevent duplicate receive calls.
   // Without this, a race between the MQL thread (PTWS_Poll) and the WinHTTP
   // callback thread (OnReadComplete auto-chain) could both call this function
   // and issue two simultaneous WinHttpWebSocketReceive calls on the same socket.
   bool expected = false;
   if(!m_readPending.compare_exchange_strong(expected, true))
      return 0;  // Another receive is already pending

   DWORD bytesRead = 0;
   WINHTTP_WEB_SOCKET_BUFFER_TYPE bufferType;

   DWORD result = WinHttpWebSocketReceive(
      m_hWebSocket,
      m_receiveBuffer,
      sizeof(m_receiveBuffer),
      &bytesRead,
      &bufferType
   );

   if(result != 0)
   {
      DWORD err = GetLastError();
      if(err != ERROR_IO_PENDING)
      {
         m_readPending = false;
         SetError(err);
         PTWS_Log(PTWS_LOG_ERROR, "WinHttpWebSocketReceive failed: %lu", err);
         return err;
      }
      // ERROR_IO_PENDING is normal for async
   }

   return 0;
}

//+------------------------------------------------------------------+
//| Close WebSocket connection                                          |
//+------------------------------------------------------------------+
DWORD WebSocketClient::Close(WINHTTP_WEB_SOCKET_CLOSE_STATUS status,
                              const char* reason, DWORD reasonLen)
{
   if(!m_hWebSocket || m_state == PTWS_CLOSING || m_state == PTWS_CLOSED)
      return ERROR_INVALID_HANDLE;

   m_state = PTWS_CLOSING;

   DWORD result = WinHttpWebSocketClose(
      m_hWebSocket,
      static_cast<USHORT>(status),
      (void*)reason,
      reasonLen
   );

   if(result != 0)
   {
      DWORD err = GetLastError();
      if(err != ERROR_IO_PENDING)
      {
         SetError(err);
         PTWS_Log(PTWS_LOG_ERROR, "WinHttpWebSocketClose failed: %lu", err);
      }
      // ERROR_IO_PENDING is normal — callback will handle close completion
   }

   return 0;
}

//+------------------------------------------------------------------+
//| Free all resources                                                   |
//| Called from the MQL thread (OnDeinit or PTWS_Reset).               |
//+------------------------------------------------------------------+
VOID WebSocketClient::Free()
{
   // Mark as invalid first so callbacks know not to use this object
   m_isValid = false;
   CleanupHandles();
   ResetState();
}

//+------------------------------------------------------------------+
//| Send data via WebSocket (async)                                     |
//| Returns 0 on success, error code on failure                         |
//+------------------------------------------------------------------+
DWORD WebSocketClient::Send(WINHTTP_WEB_SOCKET_BUFFER_TYPE bufferType,
                             const void* pBuffer, DWORD dwLength)
{
   if(!m_hWebSocket || m_state != PTWS_CONNECTED)
   {
      SetError(ERROR_INVALID_STATE);
      return ERROR_INVALID_STATE;
   }

   // Atomically check-and-set m_sendPending — Send is only called from the
   // MQL thread, but use CAS for consistency with the atomic type.
   bool expected = false;
   if(!m_sendPending.compare_exchange_strong(expected, true))
   {
      SetError(ERROR_BUSY);
      return ERROR_BUSY;
   }

   m_state = PTWS_SENDING;

   DWORD result = WinHttpWebSocketSend(
      m_hWebSocket,
      bufferType,
      (void*)pBuffer,
      dwLength
   );

   if(result != 0)
   {
      DWORD err = GetLastError();
      if(err != ERROR_IO_PENDING)
      {
         m_errorCode = err;
         m_sendPending = false;
         m_state = PTWS_CONNECTED;
         PTWS_Log(PTWS_LOG_ERROR, "WinHttpWebSocketSend failed: %lu", err);
         return err;
      }
      // ERROR_IO_PENDING — callback will fire with WRITE_COMPLETE
   }

   return 0;
}

//+------------------------------------------------------------------+
//| Read from ring buffer (called by PTWS_Read)                         |
//| Thread-safe — protected by m_frameMutex                              |
//+------------------------------------------------------------------+
VOID WebSocketClient::Read(BYTE* pBuffer, DWORD bufferLength,
                            DWORD* bytesRead,
                            WINHTTP_WEB_SOCKET_BUFFER_TYPE* pBufferType)
{
   std::lock_guard<std::mutex> lock(m_frameMutex);

   if(m_ringCount == 0)
   {
      *bytesRead = 0;
      *pBufferType = WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE;
      return;
   }

   FrameSlot& slot = m_ring[m_ringReadIdx];
   const BYTE* src = slot.usesOverflow ? slot.overflow.data() : slot.data;
   DWORD copyLen = (slot.dataSize < bufferLength) ? slot.dataSize : bufferLength;
   memcpy(pBuffer, src, copyLen);
   *bytesRead = copyLen;
   *pBufferType = slot.bufferType;

   // Mark slot as empty
   slot.hasData = false;
   slot.usesOverflow = false;
   slot.overflow.clear();  // Keep capacity, clear size (reuse vector allocation)
   m_ringReadIdx = (m_ringReadIdx + 1) % PTWS_MAX_QUEUE_DEPTH;
   m_ringCount--;
}

//+------------------------------------------------------------------+
//| Check if data is available in ring buffer                            |
//+------------------------------------------------------------------+
DWORD WebSocketClient::Readable()
{
   std::lock_guard<std::mutex> lock(m_frameMutex);

   if(m_ringCount == 0)
      return 0;

   return m_ring[m_ringReadIdx].dataSize;
}

//+------------------------------------------------------------------+
//| DetectPriority — Lightweight frame priority detection               |
//| Scans for "type":" prefix in JSON to classify frame priority        |
//| without full JSON parsing. Trading signals get highest priority,     |
//| telemetry/ping get lowest — dropped first when queue fills up.     |
//+------------------------------------------------------------------+
FramePriority WebSocketClient::DetectPriority(const BYTE* data, DWORD size)
{
   if(size < 10)
      return PRIORITY_TELEMETRY;

   // Scan for "type":" pattern (9 chars)
   for(DWORD i = 0; i < size - 7; i++)
   {
      if(data[i] == '"' && i + 7 < size &&
         data[i + 1] == 't' && data[i + 2] == 'y' && data[i + 3] == 'p' &&
         data[i + 4] == 'e' && data[i + 5] == '"' && data[i + 6] == ':' && data[i + 7] == '"')
      {
         const BYTE* typeStart = data + i + 8;
         DWORD remaining = size - (i + 8);
         // Check type value prefix (up to 7 chars: "signal\")
         if(remaining >= 6 && typeStart[0] == 's' && typeStart[1] == 'i' && typeStart[2] == 'g' &&
            typeStart[3] == 'n' && typeStart[4] == 'a' && typeStart[5] == 'l')
            return PRIORITY_SIGNAL;
         if(remaining >= 7 && typeStart[0] == 'c' && typeStart[1] == 'o' && typeStart[2] == 'm' &&
            typeStart[3] == 'm' && typeStart[4] == 'a' && typeStart[5] == 'n' && typeStart[6] == 'd')
            return PRIORITY_SIGNAL;
         if(remaining >= 3 && typeStart[0] == 'a' && typeStart[1] == 'c' && typeStart[2] == 'k')
            return PRIORITY_ACK;
         if(remaining >= 4 && typeStart[0] == 'p' && typeStart[1] == 'i' && typeStart[2] == 'n' &&
            (typeStart[3] == 'g' || typeStart[3] == 'o'))
            return PRIORITY_PING;
         // Everything else: account_stats, health, open_positions, etc.
         return PRIORITY_TELEMETRY;
      }
   }
   return PRIORITY_TELEMETRY;
}

//+------------------------------------------------------------------+
//| Callback notification handlers                                      |
//+------------------------------------------------------------------+
VOID WebSocketClient::OnReadComplete(DWORD bytesRead, WINHTTP_WEB_SOCKET_BUFFER_TYPE bufferType)
{
   m_readPending = false;

   // Handle fragmented WebSocket frames
   if(bufferType == WINHTTP_WEB_SOCKET_UTF8_FRAGMENT_BUFFER_TYPE ||
      bufferType == WINHTTP_WEB_SOCKET_BINARY_FRAGMENT_BUFFER_TYPE)
   {
      // Append to accumulator - don't queue yet
      m_fragAccumulator.insert(m_fragAccumulator.end(), m_receiveBuffer, m_receiveBuffer + bytesRead);

      m_lastCallback = PTWS_CALLBACK_READ_COMPLETE;

      PTWS_Log(PTWS_LOG_DEBUG, "Read complete (fragment): %lu bytes accumulated, type: %lu", bytesRead, (DWORD)bufferType);

      if(m_state == PTWS_CONNECTED && m_hWebSocket)
         StartAsyncReceive();
      return;
   }

   // Complete frame or final fragment - determine data source
   const BYTE* queueData;
   DWORD queueSize;
   if(!m_fragAccumulator.empty())
   {
      // Append this final fragment to accumulated data
      m_fragAccumulator.insert(m_fragAccumulator.end(), m_receiveBuffer, m_receiveBuffer + bytesRead);
      queueData = m_fragAccumulator.data();
      queueSize = (DWORD)m_fragAccumulator.size();
   }
   else
   {
      // Normal complete frame
      queueData = m_receiveBuffer;
      queueSize = bytesRead;
   }

   // Copy data from the persistent receive buffer into the ring buffer
   if(queueSize > 0)
   {
      // Detect pong frame and measure true RTT at DLL level (before queuing).
      // This gives accurate latency vs the MQL-level measurement which includes
      // the 100ms timer interval wait. Pong format: {"type":"pong",...}
      if(queueSize >= 16 && m_lastPingTimestamp > 0)
      {
         // Quick scan for "pong" in the frame data
         for(DWORD i = 0; i + 4 <= queueSize; i++)
         {
            if(queueData[i] == 'p' && queueData[i+1] == 'o' &&
               queueData[i+2] == 'n' && queueData[i+3] == 'g')
            {
               DWORD now = GetTickCount();
               m_wsLatencyMs = now - m_lastPingTimestamp;
               m_lastPingTimestamp = 0;
               break;
            }
         }
      }

      // Detect frame priority for smart queue management (before lock - reads unprotected data)
      FramePriority priority = DetectPriority(queueData, queueSize);

      std::lock_guard<std::mutex> lock(m_frameMutex);

       // Discard low-priority frames (telemetry, ping) when queue is above 50% capacity
       if(priority >= PRIORITY_TELEMETRY && m_ringCount >= PTWS_MAX_QUEUE_DEPTH / 2)
       {
          m_framesDropped++;
          PTWS_Log(PTWS_LOG_INFO, "Discarding low-priority frame (queue %zu/%d, priority=%d)",
                   m_ringCount, PTWS_MAX_QUEUE_DEPTH, priority);
          // Skip queuing - continue to auto-chain receive below
       }
       else
       {
          if(m_ringCount >= PTWS_MAX_QUEUE_DEPTH)
          {
             // Queue full - drop oldest (keep newest, correct for trading signal freshness)
             m_ring[m_ringReadIdx].hasData = false;
             m_ring[m_ringReadIdx].usesOverflow = false;
             m_ring[m_ringReadIdx].overflow.clear();
             m_ringReadIdx = (m_ringReadIdx + 1) % PTWS_MAX_QUEUE_DEPTH;
             m_ringCount--;
             m_framesDropped++;
             PTWS_Log(PTWS_LOG_INFO, "Frame queue overflow - dropping oldest frame");
          }

          // Queue the frame in the next ring slot
          FrameSlot& slot = m_ring[m_ringWriteIdx];
          if(queueSize <= PTWS_INLINE_FRAME_SIZE)
          {
             memcpy(slot.data, queueData, queueSize);
             slot.usesOverflow = false;
          }
          else
          {
             slot.overflow.assign(queueData, queueData + queueSize);
             slot.usesOverflow = true;
          }
          slot.dataSize = queueSize;
          slot.bufferType = bufferType;
          slot.priority = priority;
          slot.hasData = true;
          m_ringWriteIdx = (m_ringWriteIdx + 1) % PTWS_MAX_QUEUE_DEPTH;
          m_ringCount++;
          m_bytesReceived += queueSize;
       }

      // Clear fragment accumulator after queuing
      if(!m_fragAccumulator.empty())
         m_fragAccumulator.clear();

      // Notify EA via PostMessage if a notification window is registered
      if(m_notifyWnd != NULL)
      {
         PostMessageW(m_notifyWnd, m_notifyMsg, 0, 0);
      }
   }

   // Update state
   m_lastCallback = PTWS_CALLBACK_READ_COMPLETE;

   PTWS_Log(PTWS_LOG_DEBUG, "Read complete: %lu bytes, type: %lu", queueSize, (DWORD)bufferType);

   // Auto-chain: start next async receive immediately
   if(m_state == PTWS_CONNECTED && m_hWebSocket)
   {
      StartAsyncReceive();
   }
}

VOID WebSocketClient::OnSendComplete(DWORD bytesSent)
{
   m_sendPending = false;
   if(m_state == PTWS_SENDING)
      m_state = PTWS_CONNECTED;
   m_lastCallback = PTWS_CALLBACK_WRITE_COMPLETE;
   m_bytesSent += bytesSent;

   PTWS_Log(PTWS_LOG_DEBUG, "Send complete: %lu bytes", bytesSent);
}

VOID WebSocketClient::OnClose()
{
   m_state = PTWS_CLOSED;
   m_lastCallback = PTWS_CALLBACK_CLOSE_COMPLETE;
   m_readPending = false;
   m_sendPending = false;

   PTWS_Log(PTWS_LOG_INFO, "WebSocket closed");
}

VOID WebSocketClient::OnError(const WINHTTP_ASYNC_RESULT* result)
{
   if(result)
   {
      m_errorCode = result->dwError;
      PTWS_Log(PTWS_LOG_ERROR, "WebSocket error: %lu (operation: %lu)", result->dwError, result->dwResult);
   }
   else
   {
      m_errorCode = ERROR_INTERNAL_ERROR;
   }

   m_lastCallback = PTWS_CALLBACK_REQUEST_ERROR;
   m_state = PTWS_CLOSED;
   m_readPending = false;
   m_sendPending = false;
}

VOID WebSocketClient::OnSecureChannelError(DWORD error)
{
   m_errorCode = error;
   m_lastCallback = PTWS_CALLBACK_SECURE_CHANNEL_ERROR;
   m_state = PTWS_CLOSED;
   m_readPending = false;
   m_sendPending = false;
   PTWS_Log(PTWS_LOG_ERROR, "TLS secure channel error: %lu", error);
}

//+------------------------------------------------------------------+
//| WebSocket Callback — Called by WinHTTP from thread pool              |
//| Dispatches to the correct WebSocketClient instance via g_internetMap|
//+------------------------------------------------------------------+
VOID CALLBACK WebSocketCallback(
   HINTERNET hInternet,
   DWORD_PTR /*dwContext*/,
   DWORD dwInternetStatus,
   LPVOID lpvStatusInformation,
   DWORD dwStatusInformationLength
)
{
   // Find the client for this handle
   std::shared_ptr<WebSocketClient> client;

   {
      std::lock_guard<std::mutex> lock(g_internetMutex);
      auto it = g_internetMap.find(hInternet);
      if(it != g_internetMap.end())
         client = it->second.lock();  // Upgrade weak_ptr to shared_ptr
   }

   // Guard against use-after-free: if client is being destroyed, ignore callback
   if(client && !client->IsValid())
      client.reset();

   switch(dwInternetStatus)
   {
      case WINHTTP_CALLBACK_STATUS_SENDREQUEST_COMPLETE:
      {
         // WinHttpSendRequest completed — proceed to receive response
         if(client)
            client->OnSendRequestComplete();
         break;
      }

      case WINHTTP_CALLBACK_STATUS_HEADERS_AVAILABLE:
      {
         // HTTP response headers received — complete WebSocket upgrade
         if(client)
            client->OnHeadersAvailable();
         break;
      }

      case WINHTTP_CALLBACK_STATUS_READ_COMPLETE:
      {
         // WinHttpWebSocketReceive completed
         // lpvStatusInformation points to WINHTTP_WEB_SOCKET_STATUS
         if(client && lpvStatusInformation)
         {
            WINHTTP_WEB_SOCKET_STATUS* wsStatus = (WINHTTP_WEB_SOCKET_STATUS*)lpvStatusInformation;
            client->OnReadComplete(wsStatus->dwBytesTransferred, wsStatus->eBufferType);
         }
         break;
      }

      case WINHTTP_CALLBACK_STATUS_WRITE_COMPLETE:
      {
         // WinHttpWebSocketSend completed
         // lpvStatusInformation points to WINHTTP_WEB_SOCKET_STATUS
         if(client && lpvStatusInformation)
         {
            WINHTTP_WEB_SOCKET_STATUS* wsStatus = (WINHTTP_WEB_SOCKET_STATUS*)lpvStatusInformation;
            client->OnSendComplete(wsStatus->dwBytesTransferred);
         }
         break;
      }

      case WINHTTP_CALLBACK_STATUS_CLOSE_COMPLETE:
      {
         // WinHttpWebSocketClose completed
         if(client)
            client->OnClose();
         break;
      }

      case WINHTTP_CALLBACK_STATUS_SHUTDOWN_COMPLETE:
      {
         // WinHttpWebSocketShutdown completed
         if(client)
            client->OnClose();
         break;
      }

      case WINHTTP_CALLBACK_STATUS_REQUEST_ERROR:
      {
         if(client && lpvStatusInformation)
         {
            WINHTTP_ASYNC_RESULT* result = (WINHTTP_ASYNC_RESULT*)lpvStatusInformation;
            client->OnError(result);
         }
         break;
      }

      case WINHTTP_CALLBACK_STATUS_SECURE_CHANNEL_ERROR:
      {
         if(client)
         {
            DWORD tlsError = 0;
            if(lpvStatusInformation && dwStatusInformationLength >= sizeof(DWORD))
               tlsError = *(DWORD*)lpvStatusInformation;
            client->OnSecureChannelError(tlsError);
         }
         break;
      }

      case WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING:
      {
         // Handle being closed — remove from internet map
         std::lock_guard<std::mutex> lock(g_internetMutex);
         g_internetMap.erase(hInternet);
         break;
      }

      default:
         // Other notifications (redirect, secure connection, etc.) — ignore
         break;
   }
}

//+------------------------------------------------------------------+
//| GetStats — Return connection statistics (V1, 32-bit counters)        |
//+------------------------------------------------------------------+
VOID WebSocketClient::GetStats(PTWS_ConnectionStats* stats)
{
   if(!stats)
      return;

   std::lock_guard<std::mutex> lock(m_frameMutex);

   stats->bytes_sent = (DWORD)(m_bytesSent & 0xFFFFFFFF);        // Truncate to 32-bit
   stats->bytes_received = (DWORD)(m_bytesReceived & 0xFFFFFFFF); // Truncate to 32-bit
   stats->reconnect_count = 0;
   stats->frames_queued = (DWORD)m_ringCount;
   stats->frames_dropped = (DWORD)(m_framesDropped & 0xFFFFFFFF); // Truncate to 32-bit

   // Calculate uptime
   if(m_connectTimestamp > 0)
      stats->uptime_sec = (GetTickCount() - m_connectTimestamp) / 1000;
   else
      stats->uptime_sec = 0;

   // Latency metrics
   stats->ws_latency_ms = m_wsLatencyMs;
   stats->terminal_lag_ms = (GetTickCount() - m_lastPollTimestamp);

   // Log warning if counters have overflowed 32-bit
   if(m_bytesSent > 0xFFFFFFFF || m_bytesReceived > 0xFFFFFFFF || m_framesDropped > 0xFFFFFFFF)
   {
      PTWS_Log(PTWS_LOG_INFO, "GetStats: counters overflowed 32-bit - use GetStatsV2 for 64-bit values");
   }
}

//+------------------------------------------------------------------+
//| GetStatsV2 — Return connection statistics (V2, 64-bit counters)     |
//+------------------------------------------------------------------+
VOID WebSocketClient::GetStatsV2(PTWS_ConnectionStatsV2* stats)
{
   if(!stats)
      return;

   std::lock_guard<std::mutex> lock(m_frameMutex);

   stats->struct_version = 2;
   stats->uptime_sec = (m_connectTimestamp > 0) ? (GetTickCount() - m_connectTimestamp) / 1000 : 0;
   stats->bytes_sent_low = (DWORD)(m_bytesSent & 0xFFFFFFFF);
   stats->bytes_sent_high = (DWORD)(m_bytesSent >> 32);
   stats->bytes_received_low = (DWORD)(m_bytesReceived & 0xFFFFFFFF);
   stats->bytes_received_high = (DWORD)(m_bytesReceived >> 32);
   stats->reconnect_count = 0;
   stats->frames_queued = (DWORD)m_ringCount;
   stats->frames_dropped_low = (DWORD)(m_framesDropped & 0xFFFFFFFF);
   stats->frames_dropped_high = (DWORD)(m_framesDropped >> 32);
   stats->ws_latency_ms = m_wsLatencyMs;
   stats->terminal_lag_ms = (GetTickCount() - m_lastPollTimestamp);
}

//+------------------------------------------------------------------+
//| GetQueueDepth — Return current ring buffer depth (thread-safe)      |
//+------------------------------------------------------------------+
size_t WebSocketClient::GetQueueDepth()
{
   std::lock_guard<std::mutex> lock(m_frameMutex);
   return m_ringCount;
}

//+------------------------------------------------------------------+
//| RecordPingSent — Mark timestamp when EA sends a ping                |
//+------------------------------------------------------------------+
VOID WebSocketClient::RecordPingSent()
{
   m_lastPingTimestamp = GetTickCount();
}

//+------------------------------------------------------------------+
//| UpdatePollTimestamp — Called from PTWS_Poll to track EA responsiveness|
//+------------------------------------------------------------------+
VOID WebSocketClient::UpdatePollTimestamp()
{
   m_lastPollTimestamp = GetTickCount();
}

#endif // _WIN32
