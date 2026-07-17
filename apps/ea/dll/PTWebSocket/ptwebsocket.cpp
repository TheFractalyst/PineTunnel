// WinHTTP WebSocket API requires Windows 8+ (0x0602)
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#elif _WIN32_WINNT < 0x0602
#undef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif

// WIN32_LEAN_AND_MEAN + winsock2.h included via asyncwebsocketclient.h
// to prevent winsock.h conflicts with ws2tcpip.h (NTP code)

#include "asyncwebsocketclient.h"

#ifdef _WIN32

#include <cstdio>
#include <cstring>
#include <comdef.h>
#include <wbemidl.h>
#include <iphlpapi.h>
#include <icmpapi.h>
#include <wincrypt.h>     // CryptStringToBinaryA, CRYPT_STRING_BASE64 (base64 decode)
#include <shellapi.h>      // ShellExecuteA (auto-restart batch launch)

#pragma comment(lib, "wbemuuid.lib")
#pragma comment(lib, "iphlpapi.lib")
#pragma comment(lib, "ws2_32.lib")

//+------------------------------------------------------------------+
//| Static session handle — shared across all connections                |
//| Initialized on first Connect(), freed on DLL_PROCESS_DETACH       |
//+------------------------------------------------------------------+
static CRITICAL_SECTION g_cs;  // For DLL exports (MQL calls from single thread)

//+------------------------------------------------------------------+
//| DLL Entry Point                                                     |
//+------------------------------------------------------------------+
BOOL APIENTRY DllMain(HMODULE /*hModule*/, DWORD ul_reason_for_call, LPVOID /*lpReserved*/)
{
   switch(ul_reason_for_call)
   {
      case DLL_PROCESS_ATTACH:
         InitializeCriticalSection(&g_cs);
         break;
      case DLL_PROCESS_DETACH:
         // Clean up global session
         if(g_hSession)
         {
            WinHttpCloseHandle(g_hSession);
            g_hSession = NULL;
         }
         DeleteCriticalSection(&g_cs);
         break;
      case DLL_THREAD_ATTACH:
      case DLL_THREAD_DETACH:
         break;
   }
   return TRUE;
}

//+------------------------------------------------------------------+
//| Helper: Look up client by handle ID, returns shared_ptr (or null)  |
//| Thread-safe. Returns null if handle is invalid.                     |
//+------------------------------------------------------------------+
static std::shared_ptr<WebSocketClient> GetClient(long handle)
{
   if(handle <= 0)
      return nullptr;

   std::lock_guard<std::mutex> lock(g_handleMapMutex);
   auto it = g_handleMap.find(handle);
   if(it != g_handleMap.end())
      return it->second;
   return nullptr;
}

//+------------------------------------------------------------------+
//| Helper: Check if file extension is in allowed list                  |
//+------------------------------------------------------------------+
static bool IsAllowedExtension(const char* filename)
{
   static const char* allowedExts[] = {".ex5", ".ex4", ".dll", NULL};
   size_t pathLen = strlen(filename);
   for(int i = 0; allowedExts[i]; i++)
   {
      size_t extLen = strlen(allowedExts[i]);
      if(pathLen >= extLen && _stricmp(filename + pathLen - extLen, allowedExts[i]) == 0)
         return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Helper: Write data buffer to file (CreateFileW + WriteFile)        |
//| Returns 0 on success, createErrCode on CreateFile fail, -17 on     |
//| WriteFile fail.                                                    |
//+------------------------------------------------------------------+
static int WriteDataToFile(const char* save_path, const void* data, DWORD dataSize, int createErrCode)
{
   int wideLen = MultiByteToWideChar(CP_UTF8, 0, save_path, -1, NULL, 0);
   std::wstring widePath(wideLen, L'\0');
   MultiByteToWideChar(CP_UTF8, 0, save_path, -1, &widePath[0], wideLen);

   HANDLE hFile = CreateFileW(widePath.c_str(), GENERIC_WRITE, 0, NULL,
      CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
   if(hFile == INVALID_HANDLE_VALUE)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: CreateFile(%s) failed: %lu", save_path, GetLastError());
      return createErrCode;
   }

   DWORD written = 0;
   if(!WriteFile(hFile, data, dataSize, &written, NULL) || written != dataSize)
   {
      DWORD err = GetLastError();
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: WriteFile failed: %lu (wrote %lu of %lu)",
               err, written, dataSize);
      CloseHandle(hFile);
      DeleteFileA(save_path);
      return -17;
   }
   CloseHandle(hFile);
   return 0;
}

//+------------------------------------------------------------------+
//| Helper: Convert string to lowercase in-place                        |
//+------------------------------------------------------------------+
static void ToLowerStr(char* s)
{
   for(int i = 0; s[i]; i++)
      s[i] = (char)tolower((unsigned char)s[i]);
}

//+------------------------------------------------------------------+
//| PTWS_ConnectAuth — Connect to WebSocket server with authentication   |
//| Returns: numeric handle ID (>0) on success, 0 on failure             |
//| MT5: long, MT4: int (same function, different import type)         |
//| auth_key: must match PTWS_AUTH_KEY or connection is rejected        |
//+------------------------------------------------------------------+
__declspec(dllexport) long PTWS_ConnectAuth(const wchar_t* url, int port, int secure,
                                             const wchar_t* wsParam, const wchar_t* auth_key)
{
   // ── Process verification: only MetaTrader terminals may use this DLL ──
   {
      WCHAR exePath[MAX_PATH] = {0};
      GetModuleFileNameW(NULL, exePath, MAX_PATH);

      // Extract filename from full path
      std::wstring exeName = exePath;
      size_t lastSlash = exeName.find_last_of(L"\\/");
      if(lastSlash != std::wstring::npos)
         exeName = exeName.substr(lastSlash + 1);

      // Case-insensitive comparison
      if(exeName != L"terminal64.exe" && exeName != L"terminal.exe" &&
         exeName != L"metatrader.exe" && exeName != L"mt4terminal.exe")
      {
         PTWS_Log(PTWS_LOG_ERROR, "PTWS_Connect: unauthorized process '%ls' - only MetaTrader allowed", exeName.c_str());
         return 0;
      }
   }

   // ── Auth key validation: caller must provide the correct key ──
   if(!auth_key || wcslen(auth_key) == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_Connect: auth_key is required");
      return 0;
   }

   // Convert wide auth_key to narrow string for comparison
   std::string keyStr;
   for(size_t i = 0; auth_key[i] != L'\0'; i++)
   {
      if(auth_key[i] < 128)
         keyStr += (char)auth_key[i];
   }

   if(keyStr != PTWS_AUTH_KEY)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_Connect: invalid auth_key - connection rejected");
      return 0;
   }

   EnterCriticalSection(&g_cs);

   // Create client instance
   auto client = std::make_shared<WebSocketClient>();

   // Verify global session is available
   if(!GetOrCreateSession())
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_Connect: global session unavailable");
      LeaveCriticalSection(&g_cs);
      return 0;
   }

   // Start async connection
   DWORD result = client->Connect(
      url ? url : L"",
      port > 0 ? (INTERNET_PORT)port : 0,
      secure ? WINHTTP_FLAG_SECURE : 0,
      wsParam
   );

   if(result != 0 && result != ERROR_IO_PENDING)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_Connect: Connect failed with %lu", result);
      LeaveCriticalSection(&g_cs);
      return 0;
   }

   // Generate unique handle ID
   long handle = g_nextHandleId.fetch_add(1);
   if(handle <= 0)
   {
      // Overflow protection (extremely unlikely)
      g_nextHandleId.store(1);
      handle = 1;
   }

   // Store in handle map
   {
      std::lock_guard<std::mutex> lock(g_handleMapMutex);
      g_handleMap[handle] = client;
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_Connect: handle=%ld, async connection started", handle);

   LeaveCriticalSection(&g_cs);
   return handle;
}

//+------------------------------------------------------------------+
//| PTWS_Disconnect — Close WebSocket connection                         |
//+------------------------------------------------------------------+
__declspec(dllexport) void PTWS_Disconnect(long handle)
{
   EnterCriticalSection(&g_cs);

   auto client = GetClient(handle);
   if(!client)
   {
      LeaveCriticalSection(&g_cs);
      return;
   }

   client->Close(WINHTTP_WEB_SOCKET_SUCCESS_CLOSE_STATUS);
   client->Free();
   {
      std::lock_guard<std::mutex> lock(g_handleMapMutex);
      g_handleMap.erase(handle);
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_Disconnect: handle=%ld", handle);
   LeaveCriticalSection(&g_cs);
}

//+------------------------------------------------------------------+
//| PTWS_Reset — Free all resources and remove from handle map          |
//+------------------------------------------------------------------+
__declspec(dllexport) void PTWS_Reset(long handle)
{
   EnterCriticalSection(&g_cs);

   if(handle <= 0)
   {
      LeaveCriticalSection(&g_cs);
      return;
   }

   auto client = GetClient(handle);
   if(!client)
   {
      LeaveCriticalSection(&g_cs);
      return;
   }

   // Free resources (closes handles, removes from internet map, etc.)
   client->Free();

   // Remove from handle map (releases our shared_ptr)
   {
      std::lock_guard<std::mutex> lock(g_handleMapMutex);
      g_handleMap.erase(handle);
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_Reset: handle=%ld freed", handle);
   LeaveCriticalSection(&g_cs);
}

//+------------------------------------------------------------------+
//| PTWS_Send — Send data via WebSocket                                  |
//| Returns: 0 on success (or ERROR_IO_PENDING for async), error code on failure|
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_Send(long handle, const char* data, int length)
{
   if(handle <= 0 || !data || length <= 0)
      return ERROR_INVALID_PARAMETER;

   auto client = GetClient(handle);
   if(!client)
      return ERROR_INVALID_HANDLE;

   return (int)client->Send(
      WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE,
      data,
      (DWORD)length
   );
}

//+------------------------------------------------------------------+
//| PTWS_Read — Read data from frame queue                               |
//| Returns: 0 on success, error code on failure                         |
//| bytes_read: number of bytes read into buffer                        |
//| buffer_type: 0 = UTF8 text, 1 = binary                             |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_Read(long handle, char* buffer, int buffer_size,
                                      int* bytes_read, int* buffer_type)
{
   if(handle <= 0 || !buffer || buffer_size <= 0)
      return ERROR_INVALID_PARAMETER;

   auto client = GetClient(handle);
   if(!client)
      return ERROR_INVALID_HANDLE;

   DWORD dwBytesRead = 0;
   WINHTTP_WEB_SOCKET_BUFFER_TYPE bt = WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE;

   client->Read(
      (BYTE*)buffer,
      (DWORD)buffer_size,
      &dwBytesRead,
      &bt
   );

   if(bytes_read)
      *bytes_read = (int)dwBytesRead;
   if(buffer_type)
      *buffer_type = (bt == WINHTTP_WEB_SOCKET_UTF8_MESSAGE_BUFFER_TYPE) ? 0 : 1;

   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_Poll — Kick off async receive if none pending, update state    |
//| Must be called from OnTimer() at regular intervals (e.g., 100ms)     |
//| Returns: 0 on success, error code on failure                        |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_Poll(long handle)
{
   if(handle <= 0)
      return ERROR_INVALID_PARAMETER;

   auto client = GetClient(handle);
   if(!client)
      return ERROR_INVALID_HANDLE;

   // Update terminal poll timestamp for lag measurement
   client->UpdatePollTimestamp();

   // Ensure a receive is pending (auto-chain should handle this, but
   // Poll provides a safety net in case the chain broke)
   if(!client->IsReadPending() && client->Status() == PTWS_CONNECTED)
   {
      client->StartAsyncReceive();
   }

   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_Readable — Check if data is available in frame queue            |
//| Returns: number of bytes available, 0 if queue is empty              |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_Readable(long handle)
{
   if(handle <= 0)
      return 0;

   auto client = GetClient(handle);
   if(!client)
      return 0;

   return (int)client->Readable();
}

//+------------------------------------------------------------------+
//| PTWS_Status — Get current connection state                           |
//| Returns: ENUM_WEBSOCKET_STATE value                                 |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_Status(long handle)
{
   if(handle <= 0)
      return PTWS_CLOSED;

   auto client = GetClient(handle);
   if(!client)
      return PTWS_CLOSED;

   return (int)client->Status();
}

//+------------------------------------------------------------------+
//| PTWS_LastError — Get last error code                                |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_LastError(long handle)
{
   if(handle <= 0)
      return ERROR_INVALID_HANDLE;

   auto client = GetClient(handle);
   if(!client)
      return ERROR_INVALID_HANDLE;

   return (int)client->LastError();
}

//+------------------------------------------------------------------+
//| PTWS_LastCallback — Get last callback status                        |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_LastCallback(long handle)
{
   if(handle <= 0)
      return 0;

   auto client = GetClient(handle);
   if(!client)
      return 0;

   return (int)client->LastCallback();
}

//+------------------------------------------------------------------+
//| PTWS_SetLogLevel — Set DLL log level                               |
//| 0=off, 1=error, 2=info, 3=debug                                    |
//+------------------------------------------------------------------+
__declspec(dllexport) void PTWS_SetLogLevel(int level)
{
   g_logLevel = level;
}

//+------------------------------------------------------------------+
//| PTWS_GetLogCount — Get total number of log entries written          |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetLogCount()
{
   return g_logCount;
}

//+------------------------------------------------------------------+
//| PTWS_GetLogEntry — Get a log entry by index                         |
//| index: 0 = oldest entry, g_logCount-1 = newest                      |
//| Returns 0 on success, non-zero on error                             |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetLogEntry(int index, char* buffer, int buffer_size)
{
   if(!buffer || buffer_size <= 0)
      return 1;

   std::lock_guard<std::mutex> lock(g_logMutex);

   // Ring buffer — valid indices are from max(0, g_logCount - PTWS_LOG_RING_SIZE) to g_logCount-1
   if(index < 0 || index >= g_logCount)
   {
      buffer[0] = '\0';
      return 2;
   }

   int ringIndex;
   if(g_logCount <= PTWS_LOG_RING_SIZE)
      ringIndex = index;  // Buffer not yet full
   else
      ringIndex = index % PTWS_LOG_RING_SIZE;  // Buffer wrapped

   // Format: "[LEVEL] TIMESTAMP: Message"
   const char* levelStr = "???";
   switch(g_logBuffer[ringIndex].level)
   {
      case PTWS_LOG_ERROR:  levelStr = "ERR"; break;
      case PTWS_LOG_INFO:   levelStr = "INF"; break;
      case PTWS_LOG_DEBUG:  levelStr = "DBG"; break;
   }

   snprintf(buffer, buffer_size, "[%s] %lu: %s",
            levelStr,
            (unsigned long)g_logBuffer[ringIndex].timestamp,
            g_logBuffer[ringIndex].message);

   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_GetStats — Get connection statistics                            |
//| Fills the stats struct with connection uptime, bytes sent/received, |
//| queue depth, and dropped frames. Returns 0 on success.              |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetStats(long handle, PTWS_ConnectionStats* stats)
{
   if(!stats)
      return ERROR_INVALID_PARAMETER;

   auto client = GetClient(handle);
   if(!client)
   {
      // Return zeroed stats for invalid handle
      memset(stats, 0, sizeof(PTWS_ConnectionStats));
      return ERROR_INVALID_HANDLE;
   }

   client->GetStats(stats);
   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_GetStatsV2 — Get connection statistics (V2, 64-bit counters)  |
//| Returns 0 on success. Uses DWORD pairs for MT4 compatibility.      |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetStatsV2(long handle, PTWS_ConnectionStatsV2* stats)
{
   if(!stats)
      return ERROR_INVALID_PARAMETER;

   auto client = GetClient(handle);
   if(!client)
   {
      memset(stats, 0, sizeof(PTWS_ConnectionStatsV2));
      return ERROR_INVALID_HANDLE;
   }

   client->GetStatsV2(stats);
   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_GetQueueDepth — Get current ring buffer depth                   |
//| Returns the number of frames currently in the queue.                |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetQueueDepth(long handle)
{
   if(handle <= 0)
      return 0;

   auto client = GetClient(handle);
   if(!client)
      return 0;

   return (int)client->GetQueueDepth();
}

//+------------------------------------------------------------------+
//| PTWS_SetNotifyWindow — Set chart window for PostMessage notification|
//| When a frame arrives, the DLL calls PostMessageW to this window with |
//| the specified message ID, triggering OnChartEvent in the EA.       |
//| wnd: chart window handle (from ChartGetInteger(0, CHART_WINDOW_HANDLE))|
//| msg: custom message offset (WM_USER + N, e.g., 0x0400 + 1 = CHARTEVENT_CUSTOM+1)|
//| Returns 0 on success, non-zero on error.                           |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_SetNotifyWindow(long handle, HWND wnd, UINT msg)
{
   if(handle <= 0)
      return ERROR_INVALID_PARAMETER;

   auto client = GetClient(handle);
   if(!client)
      return ERROR_INVALID_HANDLE;

   client->SetNotifyWindow(wnd, msg);
   PTWS_Log(PTWS_LOG_INFO, "PTWS_SetNotifyWindow: handle=%ld, wnd=%p, msg=0x%04X", handle, wnd, msg);
   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_GetDllVersion — Return DLL version string                       |
//| Copies up to buffer_size-1 chars of the version string into buffer.  |
//| Returns the length of the version string (not including null term).   |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetDllVersion(long handle, char* buffer, int buffer_size)
{
   // Handle parameter included for API consistency with other functions
   // Version is a global constant, not per-connection

   if(!buffer || buffer_size <= 0)
      return -1;

   const char* version = PTWS_DLL_VERSION;
   int len = (int)strlen(version);

   if(len >= buffer_size)
      len = buffer_size - 1;

   memcpy(buffer, version, len);
   buffer[len] = '\0';

   return len;
}

//+------------------------------------------------------------------+
//| PTWS_RecordPingSent — Mark timestamp when EA sends a ping            |
//+------------------------------------------------------------------+
__declspec(dllexport) void PTWS_RecordPingSent(long handle)
{
   if(handle <= 0) return;
   auto client = GetClient(handle);
   if(client)
      client->RecordPingSent();
}

//+------------------------------------------------------------------+
//| PTWS_PreventSleep — Prevent Windows from sleeping while EA runs     |
//| Uses SetThreadExecutionState to keep the system awake.              |
//| Call from OnInit(). Must call PTWS_AllowSleep in OnDeinit().        |
//| Returns 1 on success, 0 on failure.                                |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_PreventSleep()
{
   // Prevent both system sleep and display sleep
   // ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001) | ES_DISPLAY_REQUIRED (0x00000002)
   EXECUTION_STATE prev = SetThreadExecutionState(
      ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED);
   if(prev == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_PreventSleep: SetThreadExecutionState failed");
      return 0;
   }
   PTWS_Log(PTWS_LOG_INFO, "PTWS_PreventSleep: sleep prevention enabled (system+display)");
   return 1;
}

//+------------------------------------------------------------------+
//| PTWS_AllowSleep — Restore normal Windows sleep behavior             |
//| Call from OnDeinit() to undo PTWS_PreventSleep.                     |
//| Returns 1 on success, 0 on failure.                                |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_AllowSleep()
{
   EXECUTION_STATE prev = SetThreadExecutionState(ES_CONTINUOUS);
   if(prev == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_AllowSleep: SetThreadExecutionState failed");
      return 0;
   }
   PTWS_Log(PTWS_LOG_INFO, "PTWS_AllowSleep: sleep prevention disabled");
   return 1;
}

//+------------------------------------------------------------------+
//| PTWS_GetVpsInfo — Detect if running on a VPS                        |
//| Uses WMI to query Win32_ComputerSystem manufacturer/model.          |
//| Compares against known VPS provider signatures.                     |
//| Returns 0 on success, non-zero on error.                           |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetVpsInfo(PTWS_VpsInfo* info)
{
   if(!info)
      return ERROR_INVALID_PARAMETER;

   static PTWS_VpsInfo cachedVps;
   static bool vpsCached = false;
   if(vpsCached) { *info = cachedVps; return 0; }

   memset(info, 0, sizeof(PTWS_VpsInfo));
   strncpy_s(info->provider, "Unknown", _TRUNCATE);
   strncpy_s(info->manufacturer, "Unknown", _TRUNCATE);
   strncpy_s(info->model, "Unknown", _TRUNCATE);

   HRESULT hr = CoInitializeEx(NULL, COINIT_MULTITHREADED);
   bool coUninitNeeded = SUCCEEDED(hr);

   IWbemLocator* pLoc = NULL;
   IWbemServices* pSvc = NULL;
   IEnumWbemClassObject* pEnumerator = NULL;

   hr = CoInitializeSecurity(NULL, -1, NULL, NULL,
      RPC_C_AUTHN_LEVEL_DEFAULT, RPC_C_IMP_LEVEL_IMPERSONATE,
      NULL, EOAC_NONE, NULL);
   // Ignore security init errors (may already be initialized)

   hr = CoCreateInstance(CLSID_WbemLocator, 0, CLSCTX_INPROC_SERVER,
      IID_IWbemLocator, (LPVOID*)&pLoc);
   if(FAILED(hr))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_GetVpsInfo: CoCreateInstance failed: 0x%08X", (unsigned)hr);
      if(coUninitNeeded) CoUninitialize();
      return 1;
   }

   hr = pLoc->ConnectServer(
      _bstr_t(L"ROOT\\CIMV2"), NULL, NULL, 0, NULL, 0, 0, &pSvc);
   if(FAILED(hr))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_GetVpsInfo: ConnectServer failed: 0x%08X", (unsigned)hr);
      pLoc->Release();
      if(coUninitNeeded) CoUninitialize();
      return 2;
   }

   hr = CoSetProxyBlanket(pSvc, RPC_C_AUTHN_WINNT, RPC_C_AUTHZ_NONE,
      NULL, RPC_C_AUTHN_LEVEL_CALL, RPC_C_IMP_LEVEL_IMPERSONATE,
      NULL, EOAC_NONE);
   if(FAILED(hr))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_GetVpsInfo: CoSetProxyBlanket failed: 0x%08X", (unsigned)hr);
      pSvc->Release();
      pLoc->Release();
      if(coUninitNeeded) CoUninitialize();
      return 3;
   }

   hr = pSvc->ExecQuery(bstr_t("WQL"), bstr_t("SELECT Manufacturer,Model FROM Win32_ComputerSystem"),
      WBEM_FLAG_FORWARD_ONLY | WBEM_FLAG_RETURN_IMMEDIATELY, NULL, &pEnumerator);

   if(FAILED(hr))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_GetVpsInfo: ExecQuery failed: 0x%08X", (unsigned)hr);
      pSvc->Release();
      pLoc->Release();
      if(coUninitNeeded) CoUninitialize();
      return 4;
   }

   IWbemClassObject* pclsObj = NULL;
   ULONG uReturn = 0;
   bool found = false;

   while(pEnumerator)
   {
      hr = pEnumerator->Next(WBEM_INFINITE, 1, &pclsObj, &uReturn);
      if(uReturn == 0 || FAILED(hr))
         break;

      VARIANT vtManu, vtModel;
      VariantInit(&vtManu);
      VariantInit(&vtModel);

      hr = pclsObj->Get(L"Manufacturer", 0, &vtManu, 0, 0);
      if(SUCCEEDED(hr) && vtManu.vt == VT_BSTR)
      {
         WideCharToMultiByte(CP_UTF8, 0, vtManu.bstrVal, -1,
            info->manufacturer, sizeof(info->manufacturer), NULL, NULL);
         VariantClear(&vtManu);
      }

      hr = pclsObj->Get(L"Model", 0, &vtModel, 0, 0);
      if(SUCCEEDED(hr) && vtModel.vt == VT_BSTR)
      {
         WideCharToMultiByte(CP_UTF8, 0, vtModel.bstrVal, -1,
            info->model, sizeof(info->model), NULL, NULL);
         VariantClear(&vtModel);
      }

      pclsObj->Release();
      found = true;
   }

   pEnumerator->Release();

   if(!found)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_GetVpsInfo: no WMI results");
      pSvc->Release();
      pLoc->Release();
      if(coUninitNeeded) CoUninitialize();
      return 5;
   }

   // ── VPS provider detection ──
   // Known VPS manufacturer/model signatures (case-insensitive substring match)
   struct VpsSignature { const char* name; const char* keyword; };
   static const VpsSignature vpsSigs[] = {
      {"AWS",            "Amazon"},
      {"AWS",            "EC2"},
      {"Google Cloud",   "Google"},
      {"Google Cloud",   "GCE"},
      {"VMware",         "VMware"},
      {"VirtualBox",     "VirtualBox"},
      {"KVM",            "KVM"},
      {"QEMU",           "QEMU"},
      {"Azure",          "Virtual Machine"},
      {"DigitalOcean",   "DigitalOcean"},
      {"Hetzner",        "Hetzner"},
      {"OVH",            "OVH"},
      {"Linode",         "Linode"},
      {"Vultr",          "Vultr"},
      {"Oracle Cloud",   "Oracle"},
      {"Contabo",        "Contabo"},
      {"Cloudflare",     "Cloudflare"},
      {"Alibaba Cloud",  "Alibaba"},
      {"Alibaba Cloud",  "Aliyun"},
      {"Gcore",          "Gcore"},
      {"IONOS",          "IONOS"},
      {"Kamatera",       "Kamatera"},
      {"UpCloud",        "UpCloud"},
      {"LeaseWeb",        "LeaseWeb"},
      {"Scaleway",       "Scaleway"},
      {"FiberHub",       "FiberHub"},
   };

   // Convert manufacturer and model to lowercase for comparison
    char manuLower[128], modelLower[128];
    strncpy_s(manuLower, info->manufacturer, _TRUNCATE);
    strncpy_s(modelLower, info->model, _TRUNCATE);
    ToLowerStr(manuLower);
    ToLowerStr(modelLower);

   info->is_vps = 0;
   for(int i = 0; i < (int)(sizeof(vpsSigs) / sizeof(vpsSigs[0])); i++)
   {
       char keywordLower[64];
       strncpy_s(keywordLower, vpsSigs[i].keyword, _TRUNCATE);
       ToLowerStr(keywordLower);

      if(strstr(manuLower, keywordLower) || strstr(modelLower, keywordLower))
      {
         info->is_vps = 1;
         strncpy_s(info->provider, vpsSigs[i].name, _TRUNCATE);
         break;
      }
   }

   // ── Also check Win32_Processor for virtual CPU signatures ──
   if(!info->is_vps)
   {
      IEnumWbemClassObject* pEnumProc = NULL;
      hr = pSvc->ExecQuery(bstr_t("WQL"), bstr_t("SELECT Name,Manufacturer FROM Win32_Processor"),
         WBEM_FLAG_FORWARD_ONLY | WBEM_FLAG_RETURN_IMMEDIATELY, NULL, &pEnumProc);

      if(SUCCEEDED(hr) && pEnumProc)
      {
         IWbemClassObject* pProcObj = NULL;
         ULONG uRet2 = 0;

         while(pEnumProc)
         {
            hr = pEnumProc->Next(WBEM_INFINITE, 1, &pProcObj, &uRet2);
            if(uRet2 == 0 || FAILED(hr))
               break;

            VARIANT vtName, vtManu2;
            VariantInit(&vtName);
            VariantInit(&vtManu2);

            char procName[256] = {0};
            char procManu[128] = {0};

            hr = pProcObj->Get(L"Name", 0, &vtName, 0, 0);
            if(SUCCEEDED(hr) && vtName.vt == VT_BSTR)
            {
               WideCharToMultiByte(CP_UTF8, 0, vtName.bstrVal, -1, procName, sizeof(procName), NULL, NULL);
               VariantClear(&vtName);
            }

            hr = pProcObj->Get(L"Manufacturer", 0, &vtManu2, 0, 0);
            if(SUCCEEDED(hr) && vtManu2.vt == VT_BSTR)
            {
               WideCharToMultiByte(CP_UTF8, 0, vtManu2.bstrVal, -1, procManu, sizeof(procManu), NULL, NULL);
               VariantClear(&vtManu2);
            }

            pProcObj->Release();

            // Check processor name for "virtual" and manufacturer for "vmware"
            char procNameLower[256] = {0};
            char procManuLower[128] = {0};
            strncpy_s(procNameLower, procName, _TRUNCATE);
            strncpy_s(procManuLower, procManu, _TRUNCATE);
            ToLowerStr(procNameLower);
            ToLowerStr(procManuLower);

            if(strstr(procNameLower, "virtual") || strstr(procManuLower, "vmware"))
            {
               info->is_vps = 1;
               if(strlen(info->provider) == 0 || strcmp(info->provider, "Unknown") == 0)
                  strncpy_s(info->provider, "VM", _TRUNCATE);
               break;
            }
         }
         pEnumProc->Release();
      }
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_GetVpsInfo: VPS=%d, provider=%s, manufacturer=%s, model=%s",
      info->is_vps, info->provider, info->manufacturer, info->model);

   pSvc->Release();
   pLoc->Release();
   if(coUninitNeeded) CoUninitialize();

   cachedVps = *info;
   vpsCached = true;

   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_RunNetworkDiag — Run network diagnostics                        |
//| Pings the target host using IcmpSendEcho and computes quality.       |
//| target_host: hostname or IP to ping (NULL = "8.8.8.8")             |
//| timeout_ms: ICMP timeout in ms (0 = 3000 default)                  |
//| num_pings: number of pings to send (0 = 3 default)                 |
//| Returns 0 on success, non-zero on error.                           |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_RunNetworkDiag(const char* target_host, int timeout_ms, int num_pings,
                                               PTWS_NetDiag* diag)
{
   if(!diag)
      return ERROR_INVALID_PARAMETER;

   static PTWS_NetDiag cachedDiag;
   static DWORD diagCacheTick = 0;
   if(GetTickCount() - diagCacheTick < 60000) { *diag = cachedDiag; return 0; }

   memset(diag, 0, sizeof(PTWS_NetDiag));

   const char* host = (target_host && strlen(target_host) > 0) ? target_host : "8.8.8.8";
   int timeout = (timeout_ms > 0) ? timeout_ms : 3000;
   int pings = (num_pings > 0) ? num_pings : 3;

   strncpy_s(diag->target_host, host, _TRUNCATE);
   diag->ping_ms = -2;   // Default: error
   diag->min_rtt_ms = -1;
   diag->max_rtt_ms = -1;
   diag->p95_rtt_ms = -1;
   diag->jitter95_ms = -1;
   diag->packet_loss_pct = 100.0;
   strncpy_s(diag->quality, "Bad", _TRUNCATE);
   diag->is_internet_available = 0;

   // Resolve target to IP (use getaddrinfo instead of deprecated inet_addr/gethostbyname)
   IPAddr destIp = INADDR_NONE;
   struct addrinfo hints = {0};
   hints.ai_family = AF_INET;
   hints.ai_socktype = SOCK_RAW;
   struct addrinfo* result = NULL;
   if(getaddrinfo(host, NULL, &hints, &result) == 0 && result)
   {
      struct sockaddr_in* addr = (struct sockaddr_in*)result->ai_addr;
      destIp = addr->sin_addr.S_un.S_addr;
      freeaddrinfo(result);
   }
   else
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_RunNetworkDiag: cannot resolve host '%s'", host);
      return 1;
   }

   // Open ICMP handle
   HANDLE hIcmp = IcmpCreateFile();
   if(hIcmp == INVALID_HANDLE_VALUE)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_RunNetworkDiag: IcmpCreateFile failed");
      return 2;
   }

   // Collect per-ping RTTs for stats
   int* rtts = (int*)malloc(pings * sizeof(int));
   if(!rtts)
   {
      IcmpCloseHandle(hIcmp);
      return 3;
   }

   DWORD replySize = sizeof(ICMP_ECHO_REPLY) + 8;
   BYTE* replyBuf = (BYTE*)malloc(replySize);
   if(!replyBuf)
   {
      free(rtts);
      IcmpCloseHandle(hIcmp);
      return 3;
   }

    char sendData[] = "PineTunnel";
    const size_t sendDataLen = strlen(sendData);
    int received = 0;

   for(int i = 0; i < pings; i++)
   {
      memset(replyBuf, 0, replySize);
       DWORD pingResult = IcmpSendEcho(hIcmp, destIp, sendData, (WORD)sendDataLen,
         NULL, replyBuf, replySize, (DWORD)timeout);

      if(pingResult > 0)
      {
         PICMP_ECHO_REPLY pReply = (PICMP_ECHO_REPLY)replyBuf;
         if(pReply->Status == IP_SUCCESS)
         {
            rtts[received] = (int)pReply->RoundTripTime;
            received++;
         }
      }
   }

   free(replyBuf);
   IcmpCloseHandle(hIcmp);

   diag->packets_sent = pings;
   diag->packets_received = received;

   if(received > 0)
   {
      // Compute packet loss
      diag->packet_loss_pct = ((double)(pings - received) / (double)pings) * 100.0;

      // Sort RTTs for percentile computation
      for(int i = 0; i < received - 1; i++)
      {
         for(int j = i + 1; j < received; j++)
         {
            if(rtts[j] < rtts[i])
            {
               int tmp = rtts[i]; rtts[i] = rtts[j]; rtts[j] = tmp;
            }
         }
      }

      diag->min_rtt_ms = rtts[0];
      diag->max_rtt_ms = rtts[received - 1];

      // Average RTT
      int totalRtt = 0;
      for(int i = 0; i < received; i++) totalRtt += rtts[i];
      diag->ping_ms = totalRtt / received;

       // P95 RTT
       int p95Idx = (int)((double)received * 0.95) - 1;
       diag->p95_rtt_ms = rtts[p95Idx < 0 ? 0 : p95Idx];

      // Jitter95: 95th percentile of consecutive RTT deltas
      if(received >= 2)
      {
         int* deltas = (int*)malloc((received - 1) * sizeof(int));
         if(deltas)
         {
            for(int i = 1; i < received; i++)
               deltas[i - 1] = abs(rtts[i] - rtts[i - 1]);

            // Sort deltas
            for(int i = 0; i < received - 2; i++)
            {
               for(int j = i + 1; j < received - 1; j++)
               {
                  if(deltas[j] < deltas[i])
                  {
                     int tmp = deltas[i]; deltas[i] = deltas[j]; deltas[j] = tmp;
                  }
               }
            }

            int j95Idx = (int)((double)(received - 1) * 0.95) - 1;
            diag->jitter95_ms = deltas[j95Idx < 0 ? 0 : j95Idx];
            free(deltas);
         }
      }
      else
      {
         diag->jitter95_ms = 0;
      }

      // Quality grading
      if(diag->packet_loss_pct <= 1.0 && diag->ping_ms <= 100 && diag->jitter95_ms <= 50)
         strncpy_s(diag->quality, "Good", _TRUNCATE);
      else if(diag->packet_loss_pct <= 5.0 && diag->ping_ms <= 200 && diag->jitter95_ms <= 100)
         strncpy_s(diag->quality, "Moderate", _TRUNCATE);
      else
         strncpy_s(diag->quality, "Bad", _TRUNCATE);

      diag->is_internet_available = 1;
   }
   else
   {
      diag->ping_ms = -1;  // Timeout
      diag->packet_loss_pct = 100.0;
      diag->is_internet_available = 0;
   }

   free(rtts);

   PTWS_Log(PTWS_LOG_INFO, "PTWS_RunNetworkDiag: host=%s, avg=%dms, min=%d, max=%d, p95=%d, jitter=%d, loss=%.1f%%, quality=%s",
      host, diag->ping_ms, diag->min_rtt_ms, diag->max_rtt_ms, diag->p95_rtt_ms, diag->jitter95_ms, diag->packet_loss_pct, diag->quality);

   cachedDiag = *diag;
   diagCacheTick = GetTickCount();

   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_GetSystemInfo — Return system/terminal info as JSON string     |
//| Used by EA for audit/telemetry reporting.                           |
//| Returns JSON string length (not including null term), -1 on error.  |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetSystemInfo(long handle, char* buffer, int buffer_size)
{
   if(!buffer || buffer_size <= 0)
      return -1;

   // Collect system info
   OSVERSIONINFOEXW osvi;
   ZeroMemory(&osvi, sizeof(osvi));
   osvi.dwOSVersionInfoSize = sizeof(osvi);

   // Get OS version info
   DWORD major = 0, minor = 0, build = 0;
   HMODULE hNtDll = GetModuleHandleA("ntdll.dll");
   if(hNtDll) {
      typedef void (WINAPI *RtlGetVersionPtr)(OSVERSIONINFOEXW*);
      RtlGetVersionPtr RtlGetVersion = (RtlGetVersionPtr)GetProcAddress(hNtDll, "RtlGetVersion");
      if(RtlGetVersion) {
         RtlGetVersion(&osvi);
         major = osvi.dwMajorVersion;
         minor = osvi.dwMinorVersion;
         build = osvi.dwBuildNumber;
      }
   }

   // Get memory info
   MEMORYSTATUSEX memInfo;
   memInfo.dwLength = sizeof(memInfo);
   GlobalMemoryStatusEx(&memInfo);
   DWORDLONG totalPhysMB = memInfo.ullTotalPhys / (1024 * 1024);
   DWORDLONG availPhysMB = memInfo.ullAvailPhys / (1024 * 1024);

   // Get DLL version
   const char* dllVer = PTWS_DLL_VERSION;

   // Build JSON string
   // Format: {"os":"Windows X.Y Build Z","ram_mb":NNNN,"avail_mb":NNNN,"dll_ver":"X.Y.Z"}
   char json[512];
   int len = _snprintf_s(json, sizeof(json), _TRUNCATE,
      "{\"os\":\"Windows %lu.%lu Build %lu\",\"ram_mb\":%llu,\"avail_mb\":%llu,\"dll_ver\":\"%s\"}",
      major, minor, build, (unsigned long long)totalPhysMB, (unsigned long long)availPhysMB, dllVer);

   if(len <= 0 || len >= buffer_size)
      len = buffer_size - 1;

   memcpy(buffer, json, len);
   buffer[len] = '\0';

   PTWS_Log(PTWS_LOG_DEBUG, "PTWS_GetSystemInfo: %s", json);
   return len;
}

//+------------------------------------------------------------------+
//| PTWS_DownloadFile — Download a file from server to local disk       |
//| Uses WinHTTP to fetch the file, then writes to the target path.     |
//| Bypasses MQL5 sandbox — DLL can write anywhere on disk.             |
//|                                                                      |
//| url: full URL to download (e.g., "https://your-server.com/api/ea/download/mt5") |
//| headers: HTTP headers string (e.g., "X-License-Key: ABC123\r\n")   |
//| save_path: local file path to save the downloaded file              |
//| timeout_ms: request timeout in ms (0 = 30000 default)               |
//|                                                                      |
//| Returns: 0 on success, positive = HTTP status code, negative = error|
//| On success, the file is written to save_path.                        |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_DownloadFile(const char* url, const char* headers,
                                             const char* save_path, int timeout_ms)
{
   if(!url || !save_path || strlen(url) == 0 || strlen(save_path) == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: invalid parameters");
      return -1;
   }

    // ── Security: Only allow HTTPS downloads ──
    // Prevents DLL from downloading over unencrypted connections
    if(strncmp(url, "https://", 8) != 0)
    {
       PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: URL must use HTTPS");
       return -99;
    }

    // Extract hostname from URL for logging
    const char* schemeEnd = strstr(url, "://");
    if(!schemeEnd) return -3;
    const char* ansiHostStart = schemeEnd + 3;
    const char* ansiPathStart = strchr(ansiHostStart, '/');
    const char* portStart = strchr(ansiHostStart, ':');
    size_t hostLen;
    if(portStart && (!ansiPathStart || portStart < ansiPathStart))
       hostLen = portStart - ansiHostStart;
    else if(ansiPathStart)
       hostLen = ansiPathStart - ansiHostStart;
    else
       hostLen = strlen(ansiHostStart);

    char extractedHost[256] = {0};
    if(hostLen >= sizeof(extractedHost)) return -3;
    strncpy_s(extractedHost, sizeof(extractedHost), ansiHostStart, hostLen);
    extractedHost[hostLen] = '\0';

    // ── Security: Only allow compiled binary file extensions ──
    // Prevents saving source code files (.mq5, .mq4, .mqh, .py, etc.)
    if(!IsAllowedExtension(save_path))
    {
       PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: file extension not allowed (security restriction)");
       return -98;
    }

   int timeout = (timeout_ms > 0) ? timeout_ms : 30000;

   // ── Parse URL into components for WinHTTP ──
   // Convert URL to wide string
   int urlWideLen = MultiByteToWideChar(CP_UTF8, 0, url, -1, NULL, 0);
   if(urlWideLen <= 0) return -2;
   WCHAR* urlWide = new WCHAR[urlWideLen];
   MultiByteToWideChar(CP_UTF8, 0, url, -1, urlWide, urlWideLen);

   // Parse: extract scheme, host, path
   bool secure = (wcsstr(urlWide, L"https://") == urlWide);
   WCHAR* hostStart = wcsstr(urlWide, secure ? L"https://" : L"http://");
   if(!hostStart) { delete[] urlWide; return -3; }
   hostStart += secure ? 8 : 7;

   WCHAR hostname[256] = {0};
   WCHAR path[1024] = {0};
   INTERNET_PORT port = secure ? 443 : 80;

   // Extract hostname
   WCHAR* pathStart = wcschr(hostStart, L'/');
   if(pathStart)
   {
      wcsncpy_s(hostname, 256, hostStart, (size_t)(pathStart - hostStart));
      wcsncpy_s(path, 1024, pathStart, _TRUNCATE);
   }
   else
   {
      wcsncpy_s(hostname, 256, hostStart, _TRUNCATE);
      wcsncpy_s(path, 1024, L"/", _TRUNCATE);
   }

   // Check for port in hostname
   WCHAR* portStr = wcschr(hostname, L':');
   if(portStr)
   {
      *portStr = L'\0';
      port = (INTERNET_PORT)_wtoi(portStr + 1);
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_DownloadFile: host=%ls, path=%ls, port=%d, secure=%d",
      hostname, path, (int)port, secure);

   // ── WinHTTP download ──
   HINTERNET hSession = WinHttpOpen(L"PineTunnel Auto-Update/3.1",
      WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, NULL, NULL, 0);
   if(!hSession)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: WinHttpOpen failed: %lu", GetLastError());
      delete[] urlWide;
      return -4;
   }

   WinHttpSetTimeouts(hSession, timeout, timeout, timeout, timeout);

   HINTERNET hConnect = WinHttpConnect(hSession, hostname, port, 0);
   if(!hConnect)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: WinHttpConnect failed: %lu", GetLastError());
      WinHttpCloseHandle(hSession);
      delete[] urlWide;
      return -5;
   }

   DWORD flags = secure ? WINHTTP_FLAG_SECURE : 0;
   HINTERNET hRequest = WinHttpOpenRequest(hConnect, L"GET", path, NULL, NULL, NULL, flags);
   if(!hRequest)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: WinHttpOpenRequest failed: %lu", GetLastError());
      WinHttpCloseHandle(hConnect);
      WinHttpCloseHandle(hSession);
      delete[] urlWide;
      return -6;
   }

   // Enforce TLS 1.2+ for HTTPS connections
   if(secure)
   {
      DWORD protocols = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 | WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3;
      WinHttpSetOption(hRequest, WINHTTP_OPTION_SECURE_PROTOCOLS, &protocols, sizeof(protocols));
   }

   // Add custom headers if provided
   if(headers && strlen(headers) > 0)
   {
      int hdrWideLen = MultiByteToWideChar(CP_UTF8, 0, headers, -1, NULL, 0);
      if(hdrWideLen > 0)
      {
         WCHAR* hdrWide = new WCHAR[hdrWideLen];
         MultiByteToWideChar(CP_UTF8, 0, headers, -1, hdrWide, hdrWideLen);
         WinHttpAddRequestHeaders(hRequest, hdrWide, -1, WINHTTP_ADDREQ_FLAG_ADD);
         delete[] hdrWide;
      }
   }

   // Send request
   if(!WinHttpSendRequest(hRequest, NULL, 0, NULL, 0, 0, 0))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: WinHttpSendRequest failed: %lu", GetLastError());
      WinHttpCloseHandle(hRequest);
      WinHttpCloseHandle(hConnect);
      WinHttpCloseHandle(hSession);
      delete[] urlWide;
      return -7;
   }

   // Receive response
   if(!WinHttpReceiveResponse(hRequest, NULL))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: WinHttpReceiveResponse failed: %lu", GetLastError());
      WinHttpCloseHandle(hRequest);
      WinHttpCloseHandle(hConnect);
      WinHttpCloseHandle(hSession);
      delete[] urlWide;
      return -8;
   }

   // Check HTTP status
   DWORD statusCode = 0;
   DWORD statusCodeSize = sizeof(statusCode);
   WinHttpQueryHeaders(hRequest, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
      NULL, &statusCode, &statusCodeSize, NULL);

   if(statusCode != 200)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: HTTP %lu", statusCode);
      WinHttpCloseHandle(hRequest);
      WinHttpCloseHandle(hConnect);
      WinHttpCloseHandle(hSession);
      delete[] urlWide;
      return (int)statusCode;
   }

   // ── Read response body ──
   DWORD totalDownloaded = 0;
   DWORD bytesAvailable = 0;
   DWORD bytesRead = 0;
   char* responseBody = NULL;
   DWORD responseBodyCapacity = 1024 * 1024;  // Start with 1MB

   responseBody = (char*)malloc(responseBodyCapacity);
   if(!responseBody)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: malloc failed");
      WinHttpCloseHandle(hRequest);
      WinHttpCloseHandle(hConnect);
      WinHttpCloseHandle(hSession);
      delete[] urlWide;
      return -9;
   }

   while(WinHttpQueryDataAvailable(hRequest, &bytesAvailable) && bytesAvailable > 0)
   {
      // Download size limit (100 MB max)
      if(totalDownloaded + bytesAvailable > 100 * 1024 * 1024)
      {
         PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: exceeds max size %d", 100 * 1024 * 1024);
         free(responseBody);
         WinHttpCloseHandle(hRequest);
         WinHttpCloseHandle(hConnect);
         WinHttpCloseHandle(hSession);
         delete[] urlWide;
         return -18;
      }

      // Grow buffer if needed
      if(totalDownloaded + bytesAvailable > responseBodyCapacity)
      {
         responseBodyCapacity = (totalDownloaded + bytesAvailable) * 2;
         char* newBuf = (char*)realloc(responseBody, responseBodyCapacity);
         if(!newBuf)
         {
            PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: realloc failed at %lu bytes", responseBodyCapacity);
            free(responseBody);
            WinHttpCloseHandle(hRequest);
            WinHttpCloseHandle(hConnect);
            WinHttpCloseHandle(hSession);
            delete[] urlWide;
            return -10;
         }
         responseBody = newBuf;
      }

      if(!WinHttpReadData(hRequest, responseBody + totalDownloaded, bytesAvailable, &bytesRead))
         break;

      totalDownloaded += bytesRead;
   }

   WinHttpCloseHandle(hRequest);
   WinHttpCloseHandle(hConnect);
   WinHttpCloseHandle(hSession);
   delete[] urlWide;

   PTWS_Log(PTWS_LOG_INFO, "PTWS_DownloadFile: downloaded %lu bytes, HTTP 200", totalDownloaded);

   // ── Parse JSON response to extract base64 data ──
   // The server returns: {"status":"success","data":"<base64>","sha256":"<hash>",...}
   // Find "data" field in JSON
   const char* dataKey = "\"data\":\"";
   const char* dataStart = strstr(responseBody, dataKey);
   if(!dataStart)
   {
      // Not a JSON response — write raw bytes to file (binary download)
       PTWS_Log(PTWS_LOG_INFO, "PTWS_DownloadFile: raw binary download, writing %lu bytes to %s",
          totalDownloaded, save_path);

       int writeResult = WriteDataToFile(save_path, responseBody, totalDownloaded, -11);
       free(responseBody);
       if(writeResult != 0) return writeResult;

       PTWS_Log(PTWS_LOG_INFO, "PTWS_DownloadFile: saved %lu bytes to %s", totalDownloaded, save_path);
       return 0;
   }

   // ── Base64 decode ──
   dataStart += strlen(dataKey);
   const char* dataEnd = strchr(dataStart, '"');
   if(!dataEnd)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: malformed JSON data field");
      free(responseBody);
      return -12;
   }

   int b64Len = (int)(dataEnd - dataStart);
   DWORD decodedSize = 0;

   // Get required output size
   CryptStringToBinaryA(dataStart, b64Len, CRYPT_STRING_BASE64, NULL, &decodedSize, NULL, NULL);
   if(decodedSize == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: base64 decode size query failed");
      free(responseBody);
      return -13;
   }

   BYTE* decoded = (BYTE*)malloc(decodedSize);
   if(!decoded)
   {
      free(responseBody);
      return -14;
   }

   if(!CryptStringToBinaryA(dataStart, b64Len, CRYPT_STRING_BASE64, decoded, &decodedSize, NULL, NULL))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_DownloadFile: base64 decode failed: %lu", GetLastError());
      free(decoded);
      free(responseBody);
      return -15;
   }

   free(responseBody);

    // ── Write decoded file to disk ──
    int writeResult = WriteDataToFile(save_path, decoded, decodedSize, -16);
    free(decoded);
    if(writeResult != 0) return writeResult;

    PTWS_Log(PTWS_LOG_INFO, "PTWS_DownloadFile: decoded %lu bytes, saved to %s", decodedSize, save_path);
    return 0;
}

//+------------------------------------------------------------------+
//| PTWS_ApplyUpdate — Apply a pending EA update on restart             |
//| Swaps new_file -> cur_file in target_dir                            |
//| Backs up current file as .bak                                       |
//| Only works on restart (current file is not locked).                |
//| Returns 0 on success, 1 if no pending update, non-zero on error.   |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_ApplyUpdate(const char* target_dir,
                                           const char* current_filename,
                                           const char* new_filename)
{
   if(!target_dir || strlen(target_dir) == 0 ||
      !current_filename || strlen(current_filename) == 0 ||
      !new_filename || strlen(new_filename) == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ApplyUpdate: invalid parameters");
      return -1;
   }

    // ── Security: Only allow compiled binary file extensions ──
    if(!IsAllowedExtension(current_filename) || !IsAllowedExtension(new_filename))
    {
       PTWS_Log(PTWS_LOG_ERROR, "PTWS_ApplyUpdate: file extension not allowed (security restriction)");
       return -2;
    }

   // ── Security: target_dir must be under MQL4/5 Experts or Libraries ──
   // Canonicalize path first to prevent traversal bypass
   char canonicalDir[MAX_PATH] = {0};
   GetFullPathNameA(target_dir, MAX_PATH, canonicalDir, NULL);
   if(strstr(canonicalDir, "\\MQL5\\Experts") == NULL &&
      strstr(canonicalDir, "\\MQL5\\Libraries") == NULL &&
      strstr(canonicalDir, "\\MQL4\\Experts") == NULL &&
      strstr(canonicalDir, "\\MQL4\\Libraries") == NULL)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ApplyUpdate: target_dir not in MQL Experts/Libraries");
      return -3;
   }

   // Reject filenames containing path separators or traversal sequences
   if(strchr(new_filename, '\\') || strchr(new_filename, '/') ||
      strstr(new_filename, ".."))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ApplyUpdate: new_filename contains path traversal");
      return -4;
   }
   if(strchr(current_filename, '\\') || strchr(current_filename, '/') ||
      strstr(current_filename, ".."))
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ApplyUpdate: current_filename contains path traversal");
      return -4;
   }

   // Build file paths
   char newPath[MAX_PATH] = {0};
   char curPath[MAX_PATH] = {0};
   char bakPath[MAX_PATH] = {0};
   _snprintf_s(newPath, MAX_PATH, _TRUNCATE, "%s\\%s", target_dir, new_filename);
   _snprintf_s(curPath, MAX_PATH, _TRUNCATE, "%s\\%s", target_dir, current_filename);
   _snprintf_s(bakPath, MAX_PATH, _TRUNCATE, "%s\\%s.bak", target_dir, current_filename);

   // Check if _new file exists
   DWORD attrs = GetFileAttributesA(newPath);
   if(attrs == INVALID_FILE_ATTRIBUTES)
   {
      // No pending update
      return 1;
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_ApplyUpdate: found pending update at %s", newPath);

   // Delete old backup if it exists
   DeleteFileA(bakPath);

   // Rename current -> .bak (may fail if current doesn't exist, that's OK)
   MoveFileA(curPath, bakPath);

   // Rename _new -> current
   if(!MoveFileA(newPath, curPath))
   {
      DWORD err = GetLastError();
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ApplyUpdate: MoveFile(%s -> %s) failed: %lu", newPath, curPath, err);

      // Try to restore backup
      MoveFileA(bakPath, curPath);
      return (int)err;
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_ApplyUpdate: successfully applied update");
   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_ScheduleRestart — Schedule terminal restart after update       |
//| Creates a batch script that waits for the current terminal process  |
//| to exit, then applies pending updates and relaunches the terminal.  |
//|                                                                      |
//| terminal_path: Full path to terminal64.exe or terminal.exe          |
//| data_path: Terminal data directory (for locating MQL5 folder)       |
//| config_path: Optional /config: path for terminal startup             |
//| restart_delay_ms: Delay before relaunch (ms, minimum 3000)         |
//|                                                                      |
//| Returns 0 on success, non-zero on error.                             |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_ScheduleRestart(const char* terminal_path,
                                               const char* data_path,
                                               const char* config_path,
                                               int restart_delay_ms)
{
   if(!terminal_path || strlen(terminal_path) == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ScheduleRestart: terminal_path is required");
      return -1;
   }

   if(restart_delay_ms < 3000)
      restart_delay_ms = 3000;

   // ── Infinite restart loop protection ──
   // Write a restart counter file. The EA clears this on successful init.
   // If counter >= 3, refuse to restart (likely infinite loop).
   char counterPath[MAX_PATH] = {0};
   _snprintf_s(counterPath, MAX_PATH, _TRUNCATE, "%s\\PineTunnel_restart_count.txt",
               data_path ? data_path : ".");
   int restartCount = 0;
   FILE* cf = fopen(counterPath, "r");
   if(cf)
   {
      fscanf(cf, "%d", &restartCount);
      fclose(cf);
   }
   restartCount++;
   cf = fopen(counterPath, "w");
   if(cf)
   {
      fprintf(cf, "%d", restartCount);
      fclose(cf);
   }
   if(restartCount > 3)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ScheduleRestart: restart count %d exceeds limit (3), aborting to prevent loop",
               restartCount);
      return -2;
   }

   // ── Build batch script ──
   char batchPath[MAX_PATH] = {0};
   _snprintf_s(batchPath, MAX_PATH, _TRUNCATE, "%s\\PineTunnel_restart.cmd",
               data_path ? data_path : ".");

   DWORD parentPid = GetCurrentProcessId();
   int delaySec = restart_delay_ms / 1000;

   FILE* f = fopen(batchPath, "w");
   if(!f)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ScheduleRestart: cannot create batch script at %s", batchPath);
      return -3;
   }

   // Batch script: wait for parent PID to exit, apply updates, relaunch terminal
   fprintf(f, "@echo off\n");
   fprintf(f, "echo PineTunnel auto-restart: waiting for terminal (PID %lu) to exit...\n", parentPid);
   fprintf(f, "set TIMEOUT=0\n");
   fprintf(f, ":wait_loop\n");
   fprintf(f, "tasklist /FI \"PID eq %lu\" 2>NUL | find /I \"terminal\" >NUL\n", parentPid);
   fprintf(f, "if %%ERRORLEVEL%%==0 (\n");
   fprintf(f, "    set /a TIMEOUT+=1\n");
   fprintf(f, "    if %%TIMEOUT%% GEQ %d goto timeout\n", delaySec + 10);
   fprintf(f, "    timeout /t 1 /nobreak >NUL\n");
   fprintf(f, "    goto wait_loop\n");
   fprintf(f, ")\n");
   fprintf(f, "echo Terminal exited, waiting for file handles to release...\n");
   fprintf(f, "timeout /t 3 /nobreak >NUL\n");

   // Apply EA update: swap _new.ex5/ex4 -> .ex5/.ex4
   // Support both MT5 (MQL5) and MT4 (MQL4) directory structures
   if(data_path && strlen(data_path) > 0)
   {
      // MT5 paths
      fprintf(f, "if exist \"%s\\MQL5\\Experts\\PineTunnel_EA_new.ex5\" (\n", data_path);
      fprintf(f, "    echo Applying EA update (MT5)...\n");
      fprintf(f, "    if exist \"%s\\MQL5\\Experts\\PineTunnel_EA.ex5\" del /f \"%s\\MQL5\\Experts\\PineTunnel_EA.ex5\"\n",
              data_path, data_path);
      fprintf(f, "    ren \"%s\\MQL5\\Experts\\PineTunnel_EA_new.ex5\" PineTunnel_EA.ex5\n", data_path);
      fprintf(f, "    echo EA update applied.\n");
      fprintf(f, ")\n");

      // MT4 paths
      fprintf(f, "if exist \"%s\\MQL4\\Experts\\PineTunnel_EA_MT4_new.ex4\" (\n", data_path);
      fprintf(f, "    echo Applying EA update (MT4)...\n");
      fprintf(f, "    if exist \"%s\\MQL4\\Experts\\PineTunnel_EA_MT4.ex4\" del /f \"%s\\MQL4\\Experts\\PineTunnel_EA_MT4.ex4\"\n",
              data_path, data_path);
      fprintf(f, "    ren \"%s\\MQL4\\Experts\\PineTunnel_EA_MT4_new.ex4\" PineTunnel_EA_MT4.ex4\n", data_path);
      fprintf(f, "    echo EA update applied.\n");
      fprintf(f, ")\n");

      // MT5 DLL update
      fprintf(f, "if exist \"%s\\MQL5\\Libraries\\PTWebSocket_new.dll\" (\n", data_path);
      fprintf(f, "    echo Applying DLL update (x64)...\n");
      fprintf(f, "    if exist \"%s\\MQL5\\Libraries\\PTWebSocket.dll\" del /f \"%s\\MQL5\\Libraries\\PTWebSocket.dll\"\n",
              data_path, data_path);
      fprintf(f, "    ren \"%s\\MQL5\\Libraries\\PTWebSocket_new.dll\" PTWebSocket.dll\n", data_path);
      fprintf(f, "    echo DLL update applied.\n");
      fprintf(f, ")\n");

      // MT4 DLL update (32-bit)
      fprintf(f, "if exist \"%s\\MQL4\\Libraries\\PTWebSocket32_new.dll\" (\n", data_path);
      fprintf(f, "    echo Applying DLL update (x86)...\n");
      fprintf(f, "    if exist \"%s\\MQL4\\Libraries\\PTWebSocket32.dll\" del /f \"%s\\MQL4\\Libraries\\PTWebSocket32.dll\"\n",
              data_path, data_path);
      fprintf(f, "    ren \"%s\\MQL4\\Libraries\\PTWebSocket32_new.dll\" PTWebSocket32.dll\n", data_path);
      fprintf(f, "    echo DLL update applied.\n");
      fprintf(f, ")\n");

      // Clear restart counter on success
      fprintf(f, "del /f \"%s\\PineTunnel_restart_count.txt\" 2>NUL\n", data_path);
   }

   // Relaunch terminal
   fprintf(f, "echo Restarting terminal...\n");
   fprintf(f, "start \"\" \"%s\"", terminal_path);
   if(config_path && strlen(config_path) > 0)
      fprintf(f, " /config:%s", config_path);
   fprintf(f, "\n");

   // Self-delete the batch script
   fprintf(f, "del /f \"%%~f0\" 2>NUL\n");
   fprintf(f, "exit /b 0\n");
   fprintf(f, ":timeout\n");
   fprintf(f, "echo Restart timed out after %d seconds, aborting\n", delaySec + 10);
   fprintf(f, "del /f \"%%~f0\" 2>NUL\n");
   fprintf(f, "exit /b 1\n");
   fclose(f);

   // ── Launch the batch script detached (hidden) ──
   INT_PTR result = (INT_PTR)ShellExecuteA(NULL, "open", batchPath, NULL, NULL, SW_HIDE);
   if(result <= 32)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ScheduleRestart: ShellExecute failed: %d", (int)result);
      return -4;
   }

   PTWS_Log(PTWS_LOG_INFO, "PTWS_ScheduleRestart: restart script launched (parent PID=%lu, counter=%d)",
            parentPid, restartCount);
   return 0;
}

//+------------------------------------------------------------------+
//| PTWS_ClearRestartCounter — Clear the restart counter file           |
//| Called by EA after successful init to allow future auto-restarts.    |
//| Returns 0 on success, non-zero on error.                            |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_ClearRestartCounter(const char* data_path)
{
   if(!data_path || strlen(data_path) == 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_ClearRestartCounter: data_path is required");
      return -1;
   }

   char counterPath[MAX_PATH] = {0};
   _snprintf_s(counterPath, MAX_PATH, _TRUNCATE, "%s\\PineTunnel_restart_count.txt", data_path);

   if(GetFileAttributesA(counterPath) == INVALID_FILE_ATTRIBUTES)
   {
      // Counter file doesn't exist — nothing to clear
      return 1;
   }

   if(DeleteFileA(counterPath))
   {
      PTWS_Log(PTWS_LOG_INFO, "PTWS_ClearRestartCounter: cleared restart counter");
      return 0;
   }

   DWORD err = GetLastError();
   PTWS_Log(PTWS_LOG_ERROR, "PTWS_ClearRestartCounter: DeleteFile failed: %lu", err);
   return (int)err;
}

//+------------------------------------------------------------------+
//| PTWS_GetNtpTime — Get current UTC time from NTP server              |
//| Queries multiple NTP servers with fallback and retry.               |
//| Returns 0 on success, non-zero on error.                           |
//| Fills PTWS_NtpTime struct with Unix epoch seconds + drift.           |
//+------------------------------------------------------------------+
__declspec(dllexport) int PTWS_GetNtpTime(PTWS_NtpTime* info)
{
   if(!info)
      return ERROR_INVALID_PARAMETER;

   static PTWS_NtpTime cachedNtp;
   static DWORD ntpCacheTick = 0;
   if(GetTickCount() - ntpCacheTick < 300000) { *info = cachedNtp; return 0; }

   memset(info, 0, sizeof(PTWS_NtpTime));

   static const char* ntpServers[] = {
      "time.windows.com",
      "pool.ntp.org",
      "time.nist.gov",
      "time.google.com"
   };
   static const int kNumServers = 4;
   static const int kMaxRetries = 1;
   static const int kTimeoutMs = 1000;

   // NTP epoch: Jan 1, 1900. Unix epoch: Jan 1, 1970.
   // Difference: 2208988800 seconds
   static const unsigned long long NTP_EPOCH_DIFF_S = 2208988800ULL;

   WSADATA wsaData;
   int wsaResult = WSAStartup(MAKEWORD(2, 2), &wsaData);
   if(wsaResult != 0)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_GetNtpTime: WSAStartup failed: %d", wsaResult);
      return 1;
   }

   SOCKET ntpSock = INVALID_SOCKET;
   bool success = false;

   for(int serverIdx = 0; serverIdx < kNumServers && !success; serverIdx++)
   {
      for(int retry = 0; retry < kMaxRetries && !success; retry++)
      {
         // DNS resolve
         struct addrinfo hints = {0};
         hints.ai_family = AF_INET;
         hints.ai_socktype = SOCK_DGRAM;
         hints.ai_protocol = IPPROTO_UDP;
         struct addrinfo* result = NULL;

         if(getaddrinfo(ntpServers[serverIdx], "123", &hints, &result) != 0 || !result)
         {
            PTWS_Log(PTWS_LOG_DEBUG, "PTWS_GetNtpTime: cannot resolve %s (retry %d)",
               ntpServers[serverIdx], retry);
            break; // Server unresolvable, try next
         }

         ntpSock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
         if(ntpSock == INVALID_SOCKET)
         {
            freeaddrinfo(result);
            continue;
         }

         // Set timeout
         DWORD tv = (DWORD)kTimeoutMs;
         setsockopt(ntpSock, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(tv));
         setsockopt(ntpSock, SOL_SOCKET, SO_SNDTIMEO, (const char*)&tv, sizeof(tv));

         // NTP request packet: LI=0, VN=4, Mode=3 (client)
         unsigned char ntpData[48] = {0};
         ntpData[0] = 0x23; // LI=0, VN=4, Mode=3

         if(sendto(ntpSock, (const char*)ntpData, 48, 0,
            result->ai_addr, (int)result->ai_addrlen) == SOCKET_ERROR)
         {
            freeaddrinfo(result);
            closesocket(ntpSock);
            ntpSock = INVALID_SOCKET;
            continue;
         }

         // Save destination address for source verification
         struct sockaddr_in destAddr;
         memcpy(&destAddr, result->ai_addr, sizeof(struct sockaddr_in));

         freeaddrinfo(result);

         // Receive response
         unsigned char recvBuf[48] = {0};
         struct sockaddr_in fromAddr;
         int fromLen = sizeof(fromAddr);
         int recvLen = recvfrom(ntpSock, (char*)recvBuf, 48, 0,
                                (struct sockaddr*)&fromAddr, &fromLen);
         if(recvLen > 0)
         {
            if(destAddr.sin_addr.S_un.S_addr != fromAddr.sin_addr.S_un.S_addr)
            {
               PTWS_Log(PTWS_LOG_INFO, "PTWS_GetNtpTime: response from wrong source, ignoring");
               closesocket(ntpSock);
               ntpSock = INVALID_SOCKET;
               continue;
            }
         }

         // Record local time immediately after NTP response
         FILETIME ft;
         GetSystemTimeAsFileTime(&ft);
         unsigned long long localTime100ns = ((unsigned long long)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
         // Convert to Unix epoch ms (1601 -> 1970 = 11644473600000 ms)
         long long localTimeMs = (long long)(localTime100ns / 10000) - 11644473600000LL;

         closesocket(ntpSock);
         ntpSock = INVALID_SOCKET;

         if(recvLen < 48)
         {
            PTWS_Log(PTWS_LOG_DEBUG, "PTWS_GetNtpTime: short response from %s (retry %d)",
               ntpServers[serverIdx], retry);
            Sleep(200 * (1 << retry));
            continue;
         }

         // Parse NTP transmit timestamp (bytes 40-47)
         unsigned int secPart = ((unsigned int)recvBuf[40] << 24) |
                               ((unsigned int)recvBuf[41] << 16) |
                               ((unsigned int)recvBuf[42] << 8) |
                               ((unsigned int)recvBuf[43]);
         unsigned int fracPart = ((unsigned int)recvBuf[44] << 24) |
                                ((unsigned int)recvBuf[45] << 16) |
                                ((unsigned int)recvBuf[46] << 8) |
                                ((unsigned int)recvBuf[47]);

         // Convert NTP timestamp to Unix epoch seconds
         unsigned long long ntpTimeMs = ((unsigned long long)secPart * 1000ULL) +
                                        ((unsigned long long)fracPart * 1000ULL / 4294967296ULL);
         ntpTimeMs -= (NTP_EPOCH_DIFF_S * 1000ULL); // Convert from NTP epoch (1900) to Unix epoch (1970)

         // Compute drift with +10ms buffer
         long long drift = (long long)(ntpTimeMs - (unsigned long long)localTimeMs);
         if(drift < 0) drift = -drift;
         drift += 10;

         info->ntp_time_s = (int)(ntpTimeMs / 1000ULL);
         info->drift_ms = (int)drift;
         info->sync_success = 1;
         success = true;

         PTWS_Log(PTWS_LOG_INFO, "PTWS_GetNtpTime: server=%s, time=%d, drift=%dms",
            ntpServers[serverIdx], info->ntp_time_s, info->drift_ms);
      }
   }

   WSACleanup();

   if(!success)
   {
      PTWS_Log(PTWS_LOG_ERROR, "PTWS_GetNtpTime: all NTP servers failed");
      return 2;
   }

   cachedNtp = *info;
   ntpCacheTick = GetTickCount();

   return 0;
}

#endif // _WIN32
