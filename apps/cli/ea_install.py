"""PineTunnel EA Auto-Install - Download compiled EA + DLL from GitHub
Releases, detect all MetaTrader installations, and copy files to the
correct directories.

Usage:
    pinetunnel install-ea                  Auto-detect and install on local Windows
    pinetunnel install-ea --all            Install to ALL detected MetaTrader instances
    pinetunnel install-ea --remote HOST    Install to remote Windows VPS via SSH
    pinetunnel install-ea --platform mt5   Install only MT5 EA (default: both)
    pinetunnel install-ea --download       Download EA files to current directory
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from apps.cli.main import (
    _c,
    _CYAN,
    _YELLOW,
    print_fail,
    print_ok,
    print_skip,
    print_warn,
)

GITHUB_API = "https://api.github.com/repos/TheFractalyst/PineTunnel/releases/latest"
GITHUB_DOWNLOAD_BASE = "https://github.com/TheFractalyst/PineTunnel/releases/latest/download"

# ---------------------------------------------------------------------------
# Constants - bundled EA file locations relative to this package
# ---------------------------------------------------------------------------

_EA_BASE = Path(__file__).parent.parent / "ea"

# MT5 compiled EA + source
_MT5_EA = _EA_BASE / "mt5" / "PineTunnel_EA.ex5"
_MT5_SOURCE = _EA_BASE / "mt5" / "PineTunnel_EA.mq5"

# MT4 compiled EA + source
_MT4_EA = _EA_BASE / "mt4" / "PineTunnel_EA_MT4.ex4"
_MT4_SOURCE = _EA_BASE / "mt4" / "PineTunnel_EA_MT4.mq4"

# DLL binaries (may not exist if not compiled by CI)
_DLL_BIN = _EA_BASE / "dll" / "bin"
_MT5_DLL = _DLL_BIN / "PTWebSocket.dll"
_MT4_DLL = _DLL_BIN / "PTWebSocket32.dll"

# Common broker folder names to search for
_COMMON_MT5_NAMES = [
    "MetaTrader 5",
    "MT5",
    "MetaTrader5",
]

_COMMON_MT4_NAMES = [
    "MetaTrader 4",
    "MT4",
    "MetaTrader4",
]


# ---------------------------------------------------------------------------
# 1. find_metatrader_installations()
# ---------------------------------------------------------------------------


def find_metatrader_installations() -> list[dict]:
    """Detect all installed MetaTrader 4 and 5 instances on Windows.

    Checks (in order):
      a. Registry: HKLM\\SOFTWARE\\WOW6432Node\\MetaQuotes\\Terminal\\ (MT4)
         and HKLM\\SOFTWARE\\MetaQuotes\\Terminal\\ (MT5)
      b. File system: glob for terminal64.exe (MT5) and terminal.exe (MT4)
      c. Common broker folder names under Program Files
      d. Data directory: AppData\\Roaming\\MetaQuotes\\Terminal\\<hash>\\

    Returns:
        List of dicts with keys: type, broker, data_dir, install_dir
    """
    installations: list[dict] = []
    seen_paths: set[str] = set()

    # --- Method a: Registry (Uninstall keys - confirmed from MQL5 forum) ---
    # MT5 (64-bit): HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*
    # MT4 (32-bit): HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*
    # Look for Publisher = "MetaQuotes Software Corp."
    installations.extend(
        _find_via_registry(
            "MT5",
            r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            seen_paths,
        )
    )
    installations.extend(
        _find_via_registry(
            "MT4",
            r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            seen_paths,
        )
    )

    # --- Method b: File system search for terminal executables ---
    installations.extend(_find_via_filesystem_mt5(seen_paths))
    installations.extend(_find_via_filesystem_mt4(seen_paths))

    # --- Method c: Common broker folder names ---
    installations.extend(_find_via_common_names(seen_paths))

    # --- Method d: Data directory scan (find data dirs we haven't matched) ---
    data_installs = _find_via_data_directory()
    for inst in data_installs:
        key = inst.get("install_dir", "").lower()
        if key and key not in seen_paths:
            seen_paths.add(key)
            installations.append(inst)

    return installations


def _find_via_registry(
    mt_type: str, reg_path: str, seen_paths: set[str]
) -> list[dict]:
    """Query the Windows registry for MetaTrader installations.

    Searches Uninstall keys for Publisher = "MetaQuotes Software Corp."
    Confirmed registry path (from MQL5 forum):
      HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MetaTrader 5
      "InstallLocation"="C:\\Program Files\\MetaTrader 5"
      "Publisher"="MetaQuotes Software Corp."
    """
    results: list[dict] = []
    if platform.system() != "Windows":
        return results

    try:
        # List all subkeys under the Uninstall path
        result = subprocess.run(
            ["reg", "query", reg_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return results

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("HKEY_"):
                continue

            # Subkey name (e.g., "MetaTrader 5", "MetaTrader 5 IC Markets")
            subkey_name = line.split("\\")[-1].strip()
            if not subkey_name:
                continue

            # Query the subkey for DisplayName, InstallLocation, Publisher
            subkey_path = f"{reg_path}\\{subkey_name}"
            sub_result = subprocess.run(
                ["reg", "query", subkey_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if sub_result.returncode != 0:
                continue

            sub_output = sub_result.stdout
            # Filter: only MetaQuotes installations
            if "MetaQuotes" not in sub_output and "MetaTrader" not in sub_output:
                continue

            # Extract InstallLocation
            install_dir = None
            for sub_line in sub_output.splitlines():
                sub_line = sub_line.strip()
                if "InstallLocation" in sub_line:
                    parts = sub_line.split("InstallLocation", 1)
                    if len(parts) > 1:
                        install_dir = parts[1].strip().lstrip("    ").strip()
                    break

            if not install_dir:
                continue

            install_dir_norm = os.path.normpath(install_dir)
            if install_dir_norm.lower() in seen_paths:
                continue
            seen_paths.add(install_dir_norm.lower())

            broker = _guess_broker_from_path(install_dir)
            # Find data dir via AppData scan + origin.txt
            data_dir = _find_data_dir_for_install(install_dir_norm)

            results.append(
                {
                    "type": mt_type,
                    "broker": broker,
                    "data_dir": data_dir,
                    "install_dir": install_dir_norm,
                }
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return results


def _reg_query_install_location(reg_path: str, hash_subkey: str) -> str | None:
    """Query a registry subkey for InstallLocation or path data. (Legacy helper.)"""
    full_path = f"{reg_path}\\{hash_subkey}"
    try:
        result = subprocess.run(
            ["reg", "query", full_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if "InstallLocation" in line:
                parts = line.split("InstallLocation", 1)
                if len(parts) > 1:
                    loc = parts[1].strip().lstrip("REG_SZ").strip()
                    if loc:
                        return loc
            if "Path" in line and "REG_SZ" in line:
                parts = line.split("REG_SZ", 1)
                if len(parts) > 1:
                    loc = parts[1].strip()
                    if loc and Path(loc).exists():
                        return loc
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _find_data_dir_for_install(install_dir: str) -> str | None:
    """Find the writable data directory for a given install directory.

    MetaTrader stores writable data in:
      C:\\Users\\<username>\\AppData\\Roaming\\MetaQuotes\\Terminal\\<hash>\\

    Each <hash> directory contains an origin.txt file with the install path.
    We scan all hash directories and match by comparing origin.txt content.
    """
    if platform.system() != "Windows":
        return None

    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None

    terminal_base = Path(appdata) / "MetaQuotes" / "Terminal"
    if not terminal_base.is_dir():
        return None

    install_norm = os.path.normpath(install_dir).lower()

    for entry in terminal_base.iterdir():
        if entry.name == "Common" or not entry.is_dir():
            continue

        origin_path = entry / "origin.txt"
        if origin_path.is_file():
            try:
                origin_content = origin_path.read_text().strip()
                if os.path.normpath(origin_content).lower() == install_norm:
                    return str(entry)
            except OSError:
                continue

    # Fallback: if only one hash directory exists, use it
    hash_dirs = [d for d in terminal_base.iterdir() if d.name != "Common" and d.is_dir()]
    if len(hash_dirs) == 1:
        return str(hash_dirs[0])

    return None


def _find_via_filesystem_mt5(seen_paths: set[str]) -> list[dict]:
    """Search Program Files for terminal64.exe (MT5)."""
    results: list[dict] = []
    if platform.system() != "Windows":
        return results

    search_dirs = [
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
    ]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        try:
            for sub in search_dir.iterdir():
                if not sub.is_dir():
                    continue
                terminal = sub / "terminal64.exe"
                if terminal.exists():
                    norm = os.path.normpath(str(sub))
                    if norm.lower() in seen_paths:
                        continue
                    seen_paths.add(norm.lower())
                    broker = _guess_broker_from_path(str(sub))
                    results.append(
                        {
                            "type": "MT5",
                            "broker": broker,
                            "data_dir": None,
                            "install_dir": norm,
                        }
                    )
        except (PermissionError, OSError):
            pass

    return results


def _find_via_filesystem_mt4(seen_paths: set[str]) -> list[dict]:
    """Search Program Files (x86) for terminal.exe (MT4)."""
    results: list[dict] = []
    if platform.system() != "Windows":
        return results

    search_dirs = [
        Path("C:/Program Files (x86)"),
        Path("C:/Program Files"),
    ]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        try:
            for sub in search_dir.iterdir():
                if not sub.is_dir():
                    continue
                # MT4 uses terminal.exe (32-bit), not terminal64.exe
                terminal = sub / "terminal.exe"
                terminal64 = sub / "terminal64.exe"
                if terminal.exists() and not terminal64.exists():
                    norm = os.path.normpath(str(sub))
                    if norm.lower() in seen_paths:
                        continue
                    seen_paths.add(norm.lower())
                    broker = _guess_broker_from_path(str(sub))
                    results.append(
                        {
                            "type": "MT4",
                            "broker": broker,
                            "data_dir": None,
                            "install_dir": norm,
                        }
                    )
        except (PermissionError, OSError):
            pass

    return results


def _find_via_common_names(seen_paths: set[str]) -> list[dict]:
    """Search for common broker folder names under Program Files."""
    results: list[dict] = []
    if platform.system() != "Windows":
        return results

    search_dirs = [Path("C:/Program Files"), Path("C:/Program Files (x86)")]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        try:
            for sub in search_dir.iterdir():
                if not sub.is_dir():
                    continue
                name_lower = sub.name.lower()
                norm = os.path.normpath(str(sub))
                if norm.lower() in seen_paths:
                    continue

                mt_type = None
                for mt5_name in _COMMON_MT5_NAMES:
                    if mt5_name.lower() in name_lower:
                        if (sub / "terminal64.exe").exists():
                            mt_type = "MT5"
                            break
                if not mt_type:
                    for mt4_name in _COMMON_MT4_NAMES:
                        if mt4_name.lower() in name_lower:
                            if (sub / "terminal.exe").exists() and not (
                                sub / "terminal64.exe"
                            ).exists():
                                mt_type = "MT4"
                                break

                if mt_type:
                    seen_paths.add(norm.lower())
                    broker = _guess_broker_from_path(str(sub))
                    results.append(
                        {
                            "type": mt_type,
                            "broker": broker,
                            "data_dir": None,
                            "install_dir": norm,
                        }
                    )
        except (PermissionError, OSError):
            pass

    return results


def _find_via_data_directory() -> list[dict]:
    """Scan the AppData MetaQuotes Terminal directory for data folders.

    Each hash directory may contain an origin.txt that identifies the broker
    and the install path.
    """
    results: list[dict] = []
    if platform.system() != "Windows":
        return results

    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not username:
        return results

    terminal_base = (
        Path("C:/Users")
        / username
        / "AppData"
        / "Roaming"
        / "MetaQuotes"
        / "Terminal"
    )
    if not terminal_base.exists():
        return results

    try:
        for hash_dir in terminal_base.iterdir():
            if not hash_dir.is_dir():
                continue

            origin_file = hash_dir / "origin.txt"
            install_dir = None
            broker = None

            if origin_file.exists():
                try:
                    content = origin_file.read_text(encoding="utf-8", errors="ignore")
                    for line in content.splitlines():
                        line = line.strip()
                        if line and not broker:
                            broker = line
                        # Look for a path-like string
                        if ":" in line and ("\\" in line or "/" in line):
                            if Path(line).exists():
                                install_dir = os.path.normpath(line)
                except OSError:
                    pass

            # Determine MT type from the install dir
            mt_type = None
            if install_dir:
                inst_path = Path(install_dir)
                if (inst_path / "terminal64.exe").exists():
                    mt_type = "MT5"
                elif (inst_path / "terminal.exe").exists():
                    mt_type = "MT4"

            # Also check if MQL5 or MQL4 exists in the data dir
            if not mt_type:
                if (hash_dir / "MQL5").exists():
                    mt_type = "MT5"
                elif (hash_dir / "MQL4").exists():
                    mt_type = "MT4"

            if mt_type:
                results.append(
                    {
                        "type": mt_type,
                        "broker": broker or "Unknown",
                        "data_dir": str(hash_dir),
                        "install_dir": install_dir,
                    }
                )
    except (PermissionError, OSError):
        pass

    return results


def _guess_broker_from_path(path: str) -> str:
    """Extract a broker name from an installation path."""
    name = Path(path).name
    # Remove common MT suffixes
    for suffix in [" MetaTrader 5", " MetaTrader 4", " MT5", " MT4",
                    " MetaTrader5", " MetaTrader4"]:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip() or "Unknown"
    # If the folder name itself is just "MetaTrader 5" etc.
    for generic in ("MetaTrader 5", "MetaTrader 4", "MT5", "MT4"):
        if name == generic:
            return "MetaQuotes"
    return name


# ---------------------------------------------------------------------------
# 2. find_data_directory(install_dir)
# ---------------------------------------------------------------------------


def find_data_directory(install_dir: str) -> str | None:
    """Find the MetaTrader data directory for a given install directory.

    The install dir contains terminal.exe/terminal64.exe (read-only).
    The data dir contains MQL5/ or MQL4/ (writable, where we copy files).
    Data dir is at: C:\\Users\\<username>\\AppData\\Roaming\\MetaQuotes\\Terminal\\<hash>\\

    The hash is found by:
      a. Reading the registry subkey under MetaQuotes\\Terminal
      b. Looking for origin.txt in each hash directory that matches the install path
      c. Searching AppData\\Roaming\\MetaQuotes\\Terminal\\*\\ for origin.txt

    Returns:
        The data directory path, or None if not found.
    """
    if platform.system() != "Windows":
        return None

    install_norm = os.path.normpath(install_dir).lower()

    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not username:
        return None

    terminal_base = (
        Path("C:/Users")
        / username
        / "AppData"
        / "Roaming"
        / "MetaQuotes"
        / "Terminal"
    )
    if not terminal_base.exists():
        return None

    # Method a: Check registry for hash -> match by install path
    for reg_path in [
        r"HKLM\SOFTWARE\MetaQuotes\Terminal",
        r"HKLM\SOFTWARE\WOW6432Node\MetaQuotes\Terminal",
    ]:
        try:
            result = subprocess.run(
                ["reg", "query", reg_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("HKEY_"):
                    continue
                hash_subkey = line.split("\\")[-1].strip()
                if not hash_subkey or len(hash_subkey) < 8:
                    continue
                reg_install = _reg_query_install_location(reg_path, hash_subkey)
                if reg_install and os.path.normpath(reg_install).lower() == install_norm:
                    data_path = terminal_base / hash_subkey
                    if data_path.exists():
                        return str(data_path)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Method b + c: Search each hash directory for origin.txt matching install path
    try:
        for hash_dir in terminal_base.iterdir():
            if not hash_dir.is_dir():
                continue
            origin_file = hash_dir / "origin.txt"
            if not origin_file.exists():
                continue
            try:
                content = origin_file.read_text(
                    encoding="utf-8", errors="ignore"
                )
                for line in content.splitlines():
                    line = line.strip()
                    if ":" in line and ("\\" in line or "/" in line):
                        if os.path.normpath(line).lower() == install_norm:
                            return str(hash_dir)
            except OSError:
                pass
    except (PermissionError, OSError):
        pass

    # Fallback: if only one hash directory exists, use it
    try:
        hash_dirs = [
            d for d in terminal_base.iterdir() if d.is_dir()
        ]
        if len(hash_dirs) == 1:
            return str(hash_dirs[0])
    except (PermissionError, OSError):
        pass

    return None


# ---------------------------------------------------------------------------
# 3. copy_ea_files(install_info, platform="mt5")
# ---------------------------------------------------------------------------


def copy_ea_files(
    install_info: dict, plat: str = "mt5"
) -> tuple[bool, list[str]]:
    """Copy compiled EA and DLL files to the MetaTrader data directory.

    Args:
        install_info: Dict with at least "data_dir" key.
        plat: "mt5", "mt4", or "both".

    Returns:
        (success, list_of_copied_files)
    """
    data_dir = install_info.get("data_dir")
    if not data_dir:
        data_dir = find_data_directory(install_info.get("install_dir", ""))
        if not data_dir:
            print_fail("Could not determine the MetaTrader data directory.")
            print("         The data directory is usually at:")
            print(
                "         C:\\Users\\<username>\\AppData\\Roaming\\"
                "MetaQuotes\\Terminal\\<hash>\\"
            )
            return False, []

    copied: list[str] = []
    success = True

    platforms = []
    if plat in ("mt5", "both"):
        platforms.append("mt5")
    if plat in ("mt4", "both"):
        platforms.append("mt4")

    for p in platforms:
        ok, files = _copy_platform_files(data_dir, p)
        copied.extend(files)
        if not ok:
            success = False

    return success, copied


def _copy_platform_files(
    data_dir: str, plat: str
) -> tuple[bool, list[str]]:
    """Copy files for a single platform (mt5 or mt4)."""
    data_path = Path(data_dir)

    if plat == "mt5":
        ea_source = _MT5_EA
        ea_ext = ".ex5"
        source_file = _MT5_SOURCE
        dll_source = _MT5_DLL
        mql_dir = "MQL5"
        ea_name = "PineTunnel_EA.ex5"
        dll_name = "PTWebSocket.dll"
    else:
        ea_source = _MT4_EA
        ea_ext = ".ex4"
        source_file = _MT4_SOURCE
        dll_source = _MT4_DLL
        mql_dir = "MQL4"
        ea_name = "PineTunnel_EA_MT4.ex4"
        dll_name = "PTWebSocket32.dll"

    experts_dir = data_path / mql_dir / "Experts"
    libraries_dir = data_path / mql_dir / "Libraries"

    copied: list[str] = []
    ok = True

    # Check that the compiled EA exists - try bundled first, then GitHub
    ea_content = None
    if ea_source.exists():
        ea_content = ea_source.read_bytes()
    else:
        # Download from GitHub Releases
        import tempfile
        tmp = Path(tempfile.gettempdir()) / ea_name
        if _download_from_github(ea_name, tmp):
            ea_content = tmp.read_bytes()
            print_ok(f"Downloaded {ea_name} from GitHub Releases")

    if ea_content is None:
        # Last resort: compile from source using local MetaEditor
        print_warn(f"No pre-compiled {ea_name} found. Attempting source compilation...")
        source_file = _MT5_SOURCE if plat == "mt5" else _MT4_SOURCE
        if source_file.exists() and platform.system() == "Windows":
            if _compile_ea_from_source(install_info, plat):
                # After compilation, the .ex5/.ex4 should be in the Experts dir
                compiled_path = experts_dir / ea_name
                if compiled_path.exists():
                    ea_content = compiled_path.read_bytes()
                    print_ok(f"Compiled {ea_name} from source")
                else:
                    print_fail(f"Compilation did not produce {ea_name}")
                    ok = False
            else:
                print_fail(f"Source compilation failed for {ea_name}")
                ok = False
        else:
            print_fail(f"Could not find or download {ea_name}")
            ok = False
    else:
        # Create Experts directory if needed
        try:
            experts_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print_fail(f"Could not create {experts_dir}: {e}")
            return False, copied

        target_ea = experts_dir / ea_name
        try:
            target_ea.write_bytes(ea_content)
            copied.append(str(target_ea))
            print_ok(f"Copied {ea_name} -> {experts_dir}")
        except OSError as e:
            print_fail(f"Could not copy {ea_name}: {e}")
            ok = False

    # Copy DLL - try bundled first, then GitHub
    dll_content = None
    if dll_source.exists():
        dll_content = dll_source.read_bytes()
    else:
        import tempfile
        tmp = Path(tempfile.gettempdir()) / dll_name
        if _download_from_github(dll_name, tmp):
            dll_content = tmp.read_bytes()
            print_ok(f"Downloaded {dll_name} from GitHub Releases")

    if dll_content is not None:
        try:
            libraries_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print_fail(f"Could not create {libraries_dir}: {e}")
            return ok, copied

        target_dll = libraries_dir / dll_name
        try:
            target_dll.write_bytes(dll_content)
            copied.append(str(target_dll))
            print_ok(f"Copied {dll_name} -> {libraries_dir}")
        except OSError as e:
            print_warn(f"Could not copy {dll_name}: {e}")
    else:
        # Last resort: compile DLL from source with CMake
        dll_source_dir = _EA_BASE / "dll" / "PTWebSocket"
        cmake = shutil.which("cmake")
        if dll_source_dir.exists() and cmake and platform.system() == "Windows":
            print_warn(f"No pre-compiled {dll_name}. Attempting CMake build from source...")
            try:
                libraries_dir.mkdir(parents=True, exist_ok=True)
                build_dir = libraries_dir / "build"
                arch = "x64" if plat == "mt5" else "Win32"
                subprocess.run([cmake, "-S", str(dll_source_dir), "-B", str(build_dir), "-A", arch],
                               capture_output=True, text=True, timeout=30)
                subprocess.run([cmake, "--build", str(build_dir), "--config", "Release"],
                               capture_output=True, text=True, timeout=60)
                # Find the built DLL
                for candidate in [build_dir / "Release" / dll_name, build_dir / dll_name]:
                    if candidate.exists():
                        shutil.copy2(candidate, libraries_dir / dll_name)
                        copied.append(str(libraries_dir / dll_name))
                        print_ok(f"DLL compiled from source: {libraries_dir / dll_name}")
                        break
                else:
                    print_warn(f"CMake build did not produce {dll_name}")
            except (subprocess.TimeoutExpired, OSError) as e:
                print_warn(f"DLL compilation failed: {e}")
        else:
            print_warn(
                f"DLL not found ({dll_name}). "
                "Download from GitHub Releases or build from source with CMake."
            )

    # Optionally copy source file
    if source_file.exists():
        try:
            experts_dir.mkdir(parents=True, exist_ok=True)
            target_src = experts_dir / source_file.name
            shutil.copy2(source_file, target_src)
            copied.append(str(target_src))
        except OSError:
            pass  # Source is optional, don't fail

    return ok, copied


# ---------------------------------------------------------------------------
# 4. install_ea_automated() - main orchestrator
# ---------------------------------------------------------------------------


def install_ea_automated(
    plat: str = "both",
    install_all: bool = False,
) -> bool:
    """Main orchestrator for auto-detecting and installing the EA.

    Args:
        plat: "mt5", "mt4", or "both" (default).
        install_all: If True, install to ALL detected installations without prompting.

    Returns:
        True if at least one installation succeeded.
    """
    print()
    print("  ===========================================")
    print("  PineTunnel EA Auto-Install")
    print("  ===========================================")
    print()

    # Step 1: Find all MetaTrader installations
    print("  Step 1: Detecting MetaTrader installations...")
    installations = find_metatrader_installations()

    if not installations:
        print_fail("No MetaTrader installations found on this system.")
        print()
        _offer_download_fallback(plat)
        return False

    print_ok(f"Found {len(installations)} installation(s):")
    for i, inst in enumerate(installations, 1):
        broker = inst.get("broker", "Unknown")
        mt_type = inst.get("type", "?")
        install_dir = inst.get("install_dir", "unknown")
        print(f"    {i}. {mt_type} - {broker}")
        print(f"       Install: {install_dir}")
        if inst.get("data_dir"):
            print(f"       Data:    {inst['data_dir']}")

    print()

    # Step 2: Select which installation(s) to target
    if install_all or len(installations) == 1:
        selected = installations
    else:
        print("  Multiple installations found. Choose one:")
        print(f"    0. All of the above")
        for i, inst in enumerate(installations, 1):
            mt_type = inst.get("type", "?")
            broker = inst.get("broker", "Unknown")
            print(f"    {i}. {mt_type} - {broker}")
        try:
            choice = input(f"  Select [0-{len(installations)}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return False

        try:
            idx = int(choice)
        except ValueError:
            print_fail(f"Invalid selection: {choice}")
            return False

        if idx == 0:
            selected = installations
        elif 1 <= idx <= len(installations):
            selected = [installations[idx - 1]]
        else:
            print_fail(f"Out of range: {idx}")
            return False

    print()

    # Step 3: Copy files to each selected installation
    any_success = False
    for inst in selected:
        mt_type = inst.get("type", "")
        broker = inst.get("broker", "Unknown")
        print(f"  Installing to {mt_type} - {broker}...")

        # Find data directory if not already known
        if not inst.get("data_dir"):
            data_dir = find_data_directory(inst.get("install_dir", ""))
            if data_dir:
                inst["data_dir"] = data_dir
                print_ok(f"Data directory: {data_dir}")
            else:
                print_warn(
                    "Could not auto-detect data directory. "
                    "You will need to copy files manually."
                )
                print(
                    "         The data directory is usually at:"
                )
                print(
                    "         C:\\Users\\<username>\\AppData\\Roaming\\"
                    "MetaQuotes\\Terminal\\<hash>\\"
                )
                print()
                continue

        # Determine which platform to install based on the installation type
        # and the user's --platform flag
        if plat == "both":
            install_plat = "mt5" if mt_type == "MT5" else "mt4"
        else:
            install_plat = plat

        ok, copied = copy_ea_files(inst, install_plat)
        if ok and copied:
            any_success = True
            print_ok(f"Successfully installed {len(copied)} file(s).")
        else:
            print_fail("Installation failed for this instance.")
        print()

    # Step 4: Print next steps
    _print_next_steps()

    return any_success


def _print_next_steps() -> None:
    """Print next steps after EA installation."""
    print("  ===========================================")
    print("  Next Steps:")
    print("  ===========================================")
    print()
    print("  1. Restart MetaTrader (close and reopen)")
    print()
    print("  2. Attach EA to a chart:")
    print("     - Open Navigator panel (Ctrl+N)")
    print("     - Find 'PineTunnel_EA' under Expert Advisors")
    print("     - Drag it onto a chart")
    print()
    print("  3. Set EA inputs:")
    print("     InpLicenseID = YOUR_KEY  (ADMIN_API_KEY from .env)")
    print("     InpServerURL  = http://your-server:8000")
    print()
    print("  4. Enable DLL imports:")
    print("     Tools -> Options -> Expert Advisors")
    print("     Check 'Allow DLL imports'")
    print()
    print("  5. Confirm the EA is connected:")
    print("     Look for connection log in the Experts tab")
    print()
    print("  Full docs: https://github.com/TheFractalyst/PineTunnel#documentation")
    print()


# ---------------------------------------------------------------------------
# 5. install_ea_remote() - remote install via SSH/SCP
# ---------------------------------------------------------------------------


def install_ea_remote(
    host: str,
    username: str | None = None,
    password: str | None = None,
    key_file: str | None = None,
    plat: str = "both",
) -> bool:
    """Copy EA files to a remote Windows VPS via SSH/SCP.

    Uses subprocess to run ssh/scp commands directly.
    Requires ssh client (OpenSSH) on the local machine.

    Args:
        host: Remote host (e.g., "user@1.2.3.4" or "1.2.3.4").
        username: Remote username (if not included in host).
        password: Password (requires sshpass - not recommended).
        key_file: Path to SSH private key file.
        plat: "mt5", "mt4", or "both".

    Returns:
        True if installation succeeded.
    """
    print()
    print("  ===========================================")
    print("  PineTunnel EA Remote Install")
    print("  ===========================================")
    print()

    # Build ssh/scp target
    if username and "@" not in host:
        ssh_target = f"{username}@{host}"
    else:
        ssh_target = host

    # Build base ssh command
    ssh_base = ["ssh"]
    if key_file:
        ssh_base.extend(["-i", key_file])
    ssh_base.append(ssh_target)

    # Build base scp command
    scp_base = ["scp"]
    if key_file:
        scp_base.extend(["-i", key_file])

    # Check ssh availability
    if not shutil.which("ssh"):
        print_fail("ssh not found. Install OpenSSH client.")
        if platform.system() == "Windows":
            print("         Run: Settings -> Apps -> Optional Features -> Add OpenSSH Client")
        else:
            print("         Install openssh-client via your package manager")
        return False

    # Step 1: Detect remote MetaTrader data directory
    print("  Step 1: Detecting MetaTrader on remote host...")

    # Try to find the MetaQuotes Terminal data directory
    remote_cmd = (
        'dir /b "%USERPROFILE%\\AppData\\Roaming\\MetaQuotes\\Terminal\\" '
        "2>nul"
    )
    try:
        result = subprocess.run(
            ssh_base + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print_fail("SSH connection timed out.")
        return False
    except (FileNotFoundError, OSError) as e:
        print_fail(f"SSH failed: {e}")
        return False

    if result.returncode != 0:
        err = result.stderr.strip()[:200] if result.stderr else "unknown"
        print_fail(f"Remote command failed: {err}")
        print("         Is MetaTrader installed on the remote host?")
        return False

    hash_dirs = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    if not hash_dirs:
        print_fail("No MetaTrader data directories found on remote host.")
        return False

    print_ok(f"Found {len(hash_dirs)} data directory(ies) on remote host.")

    # If multiple, let user choose
    if len(hash_dirs) > 1:
        print("  Multiple data directories found:")
        for i, hd in enumerate(hash_dirs, 1):
            print(f"    {i}. {hd}")
        try:
            choice = input(
                f"  Select [1-{len(hash_dirs)}]: "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return False
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(hash_dirs):
                print_fail(f"Out of range: {choice}")
                return False
        except ValueError:
            print_fail(f"Invalid selection: {choice}")
            return False
        selected_hash = hash_dirs[idx]
    else:
        selected_hash = hash_dirs[0]

    remote_data_dir = (
        f"%USERPROFILE%\\AppData\\Roaming\\MetaQuotes\\Terminal\\"
        f"{selected_hash}"
    )

    print_ok(f"Remote data dir: {remote_data_dir}")
    print()

    # Step 2: Copy EA files
    print("  Step 2: Copying EA files to remote host...")

    platforms = []
    if plat in ("mt5", "both"):
        platforms.append("mt5")
    if plat in ("mt4", "both"):
        platforms.append("mt4")

    any_success = False

    for p in platforms:
        if p == "mt5":
            ea_file = _MT5_EA
            dll_file = _MT5_DLL
            mql = "MQL5"
            ea_remote_name = "PineTunnel_EA.ex5"
            dll_remote_name = "PTWebSocket.dll"
        else:
            ea_file = _MT4_EA
            dll_file = _MT4_DLL
            mql = "MQL4"
            ea_remote_name = "PineTunnel_EA_MT4.ex4"
            dll_remote_name = "PTWebSocket32.dll"

        if not ea_file.exists():
            print_fail(f"Compiled {p.upper()} EA not found: {ea_file}")
            continue

        # Create remote directories
        remote_experts = f"{remote_data_dir}\\{mql}\\Experts"
        remote_libraries = f"{remote_data_dir}\\{mql}\\Libraries"

        mkdir_cmd = (
            f'mkdir "{remote_experts}" 2>nul & '
            f'mkdir "{remote_libraries}" 2>nul & echo OK'
        )
        try:
            result = subprocess.run(
                ssh_base + [mkdir_cmd],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print_fail(f"Could not create remote dirs: {e}")
            continue

        # Copy EA file via scp
        # scp uses forward slashes for remote paths
        remote_experts_scp = (
            f"{ssh_target}:"
            f"'{remote_data_dir}/{mql}/Experts/{ea_remote_name}'"
        )
        scp_cmd = scp_base + [str(ea_file), remote_experts_scp]
        print(f"  Copying {ea_remote_name}...")
        try:
            result = subprocess.run(
                scp_cmd, capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                print_ok(f"Copied {ea_remote_name} to remote Experts/")
                any_success = True
            else:
                err = result.stderr.strip()[:200] if result.stderr else ""
                print_fail(f"scp failed: {err}")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print_fail(f"scp failed: {e}")

        # Copy DLL if available
        if dll_file.exists():
            remote_dll_scp = (
                f"{ssh_target}:"
                f"'{remote_data_dir}/{mql}/Libraries/{dll_remote_name}'"
            )
            scp_dll_cmd = scp_base + [str(dll_file), remote_dll_scp]
            print(f"  Copying {dll_remote_name}...")
            try:
                result = subprocess.run(
                    scp_dll_cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    print_ok(
                        f"Copied {dll_remote_name} to remote Libraries/"
                    )
                else:
                    err = (
                        result.stderr.strip()[:200]
                        if result.stderr
                        else ""
                    )
                    print_warn(f"DLL copy failed: {err}")
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                print_warn(f"DLL copy failed: {e}")
        else:
            print_warn(
                f"DLL not found in package ({dll_file}). "
                "Download separately."
            )

        print()

    if any_success:
        _print_next_steps()
    else:
        print_fail("No files were copied to the remote host.")

    return any_success


# ---------------------------------------------------------------------------
# 6. Non-Windows handling + 7. Download fallback
# ---------------------------------------------------------------------------


def _offer_download_fallback(plat: str = "both") -> bool:
    """Offer to download EA files to the current directory (non-Windows fallback).

    Returns True if files were downloaded.
    """
    print("  EA files can be downloaded here for manual transfer to your")
    print("  MetaTrader installation.")
    print()

    try:
        answer = input("  Download EA files here? [Y/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        answer = "n"
        print()

    if answer == "n":
        print_skip("EA file download skipped.")
        return False

    return download_ea_files(dest_dir=str(Path.cwd()), plat=plat)


def _download_from_github(filename: str, dest: Path) -> bool:
    """Download a file from GitHub Releases (latest release).

    Uses the GitHub API to find the latest release, then downloads the
    asset by name. Falls back to the /releases/latest/download/ URL pattern
    which redirects to the actual asset.

    Args:
        filename: Asset filename (e.g., "PineTunnel_EA.ex5")
        dest: Destination path to save the file

    Returns:
        True if download succeeded, False otherwise.
    """
    # Method 1: Try direct download URL (works for public repos with releases)
    url = f"{GITHUB_DOWNLOAD_BASE}/{filename}"
    try:
        req = Request(url, headers={"User-Agent": "PineTunnel-CLI", "Accept": "application/octet-stream"})
        with urlopen(req, timeout=60) as resp:
            if resp.status == 200:
                dest.write_bytes(resp.read())
                return True
    except (HTTPError, URLError, OSError):
        pass

    # Method 2: Use GitHub API to find the asset in the latest release
    try:
        req = Request(GITHUB_API, headers={"User-Agent": "PineTunnel-CLI", "Accept": "application/vnd.github+json"})
        with urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode())

        for asset in release.get("assets", []):
            if asset.get("name") == filename:
                download_url = asset.get("browser_download_url")
                if download_url:
                    req2 = Request(download_url, headers={"User-Agent": "PineTunnel-CLI"})
                    with urlopen(req2, timeout=60) as resp2:
                        if resp2.status == 200:
                            dest.write_bytes(resp2.read())
                            return True
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        pass

    return False


def download_ea_files(dest_dir: str = ".", plat: str = "both") -> bool:
    """Download compiled EA + DLL files from GitHub Releases.

    Tries GitHub Releases first (always fresh from CI). Falls back to
    bundled files in the pip package if no release exists yet.

    Args:
        dest_dir: Destination directory (default: current directory).
        plat: "mt5", "mt4", or "both".

    Returns:
        True if at least one file was downloaded.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    any_copied = False

    # File mapping: (filename, bundled_path, display_name)
    files_to_get = []
    if plat in ("mt5", "both"):
        files_to_get.append(("PineTunnel_EA.ex5", _MT5_EA, "MT5 EA"))
        files_to_get.append(("PTWebSocket.dll", _MT5_DLL, "MT5 DLL (x64)"))
    if plat in ("mt4", "both"):
        files_to_get.append(("PineTunnel_EA_MT4.ex4", _MT4_EA, "MT4 EA"))
        files_to_get.append(("PTWebSocket32.dll", _MT4_DLL, "MT4 DLL (x86)"))

    print("  Downloading EA files from GitHub Releases...")
    print()

    for filename, bundled_path, display_name in files_to_get:
        target = dest / filename
        success = False

        # Try GitHub Releases first
        if _download_from_github(filename, target):
            print_ok(f"Downloaded: {target} ({display_name})")
            any_copied = True
            success = True
            continue

        # Fallback: use bundled file from pip package
        if bundled_path.exists():
            try:
                shutil.copy2(bundled_path, target)
                print_ok(f"Downloaded: {target} ({display_name}, bundled)")
                any_copied = True
                success = True
            except OSError as e:
                print_fail(f"Could not copy {filename}: {e}")
        elif not success:
            print_warn(f"Could not download {filename} (not in releases or bundled)")
            print(f"         Build from source: https://github.com/TheFractalyst/PineTunnel")
            print(f"         Or download manually from GitHub Releases page")

    if any_copied:
        print()
        print("  Files downloaded to:", dest)
        print()
        print("  Manual installation instructions:")
        print()
        if plat in ("mt5", "both"):
            print("  MT5:")
            print(f"    Copy PineTunnel_EA.ex5  -> <MT5 Data>\\MQL5\\Experts\\")
            print(f"    Copy PTWebSocket.dll     -> <MT5 Data>\\MQL5\\Libraries\\")
        if plat in ("mt4", "both"):
            print("  MT4:")
            print(f"    Copy PineTunnel_EA_MT4.ex4 -> <MT4 Data>\\MQL4\\Experts\\")
            print(f"    Copy PTWebSocket32.dll      -> <MT4 Data>\\MQL4\\Libraries\\")
        print()
        print("  The data directory is at:")
        print("    C:\\Users\\<username>\\AppData\\Roaming\\MetaQuotes\\Terminal\\<hash>\\")
        print()
        print("  Then: Restart MetaTrader, attach EA, enable DLL imports.")
        print("  Full docs: https://github.com/TheFractalyst/PineTunnel#documentation")

    return any_copied


def handle_non_windows(plat: str = "both") -> bool:
    """Handle EA install on Linux/macOS.

    MetaTrader only runs on Windows, so we offer 3 options:
    1. Download EA files here (for manual copy to Windows)
    2. SSH to a remote Windows VPS and install there
    3. Print server download URLs (for curl from Windows)
    """
    print()
    print("  MetaTrader only runs on Windows. Choose how to install the EA:")
    print()
    print("  1) Download EA files here (for manual copy to Windows)")
    print("  2) SSH to remote Windows VPS (auto-detect + copy)")
    print("  3) Show download URLs (run curl on Windows)")
    print()
    try:
        choice = input("  Choose [1/2/3] (default: 1): ").strip()
    except (KeyboardInterrupt, EOFError):
        choice = "1"
    if choice not in ("1", "2", "3"):
        choice = "1"

    if choice == "2":
        try:
            host = input("  Windows VPS SSH host (e.g., user@1.2.3.4): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return False
        if not host:
            print("  [FAIL] Host is required")
            return False
        try:
            key_file = input("  SSH key file (or Enter for default): ").strip() or None
        except (KeyboardInterrupt, EOFError):
            key_file = None
        username = host.split("@")[0] if "@" in host else None
        host_only = host.split("@")[1] if "@" in host else host
        return install_ea_remote(
            host=host_only,
            username=username,
            key_file=key_file,
            plat=plat,
        )

    if choice == "3":
        # Print curl commands for Windows users
        server_url = os.environ.get("SERVER_BASE_URL", "http://YOUR_SERVER:8000")
        license_key = os.environ.get("ADMIN_API_KEY", "YOUR_KEY")
        print()
        print("  Run these commands on your Windows VPS:")
        print()
        print(f"    curl -o PineTunnel_EA.ex5 -H \"X-License-Key: {license_key}\" \\")
        print(f"      {server_url}/api/ea/download/mt5")
        print()
        print(f"    curl -o PineTunnel_EA_MT4.ex4 -H \"X-License-Key: {license_key}\" \\")
        print(f"      {server_url}/api/ea/download/mt4")
        print()
        print("  Then copy files to MetaTrader:")
        print("    .ex5 -> MQL5\\Experts\\")
        print("    .ex4 -> MQL4\\Experts\\")
        print("  Build DLL from source or download from CI artifacts.")
        print()
        print("  Restart MetaTrader, attach EA, set InpLicenseID and InpServerURL.")
        return True

    # Default: download files
    return _offer_download_fallback(plat)


def _compile_ea_from_source(install_info: dict, plat: str = "both") -> bool:
    """Compile EA from .mq5/.mq4 source using MetaEditor on the local machine.

    Falls back to this when:
    - GitHub Releases have no compiled .ex5/.ex4 (CI billing issue)
    - Bundled .ex5/.ex4 are missing or outdated
    - User wants fresh compilation from latest source

    Uses MetaEditor command line:
      metaeditor64.exe /compile:"path\\to\\file.mq5" /log

    Args:
        install_info: Dict with install_dir and data_dir from find_metatrader_installations
        plat: "mt5", "mt4", or "both"

    Returns:
        True if compilation produced a .ex5/.ex4 file.
    """
    if platform.system() != "Windows":
        print_fail("Compilation requires Windows with MetaEditor.")
        return False

    install_dir = install_info.get("install_dir")
    data_dir = install_info.get("data_dir")

    if not install_dir or not data_dir:
        print_fail("No MetaTrader installation found for compilation.")
        return False

    # Find MetaEditor in the install directory
    me64 = Path(install_dir) / "metaeditor64.exe"
    me32 = Path(install_dir) / "metaeditor.exe"
    metaeditor = me64 if me64.exists() else (me32 if me32.exists() else None)

    if not metaeditor:
        print_fail(f"MetaEditor not found in {install_dir}")
        return False

    print_ok(f"Found MetaEditor: {metaeditor}")

    # Determine MQL directory and source file
    if plat in ("mt5", "both"):
        mql_dir = Path(data_dir) / "MQL5"
        source = _MT5_SOURCE  # bundled .mq5
        if not source.exists():
            print_fail(f"MT5 source not found: {source}")
        else:
            # Copy source to MQL5/Experts/
            experts = mql_dir / "Experts"
            experts.mkdir(parents=True, exist_ok=True)
            target_src = experts / "PineTunnel_EA.mq5"
            shutil.copy2(source, target_src)
            print_ok(f"Source copied: {target_src}")

            # Compile
            print("  Compiling MT5 EA with MetaEditor...")
            try:
                result = subprocess.run(
                    [str(metaeditor), f'/compile:"{target_src}"', "/log"],
                    capture_output=True, text=True, timeout=60,
                )
                compiled = experts / "PineTunnel_EA.ex5"
                if compiled.exists():
                    print_ok(f"Compiled: {compiled}")
                else:
                    print_warn("Compilation may have failed. Check MetaEditor log.")
            except (subprocess.TimeoutExpired, OSError) as e:
                print_fail(f"Compilation failed: {e}")

    if plat in ("mt4", "both"):
        mql_dir = Path(data_dir) / "MQL4"
        source = _MT4_SOURCE  # bundled .mq4
        if not source.exists():
            print_fail(f"MT4 source not found: {source}")
        else:
            experts = mql_dir / "Experts"
            experts.mkdir(parents=True, exist_ok=True)
            target_src = experts / "PineTunnel_EA_MT4.mq4"
            shutil.copy2(source, target_src)
            print_ok(f"Source copied: {target_src}")

            print("  Compiling MT4 EA with MetaEditor...")
            try:
                result = subprocess.run(
                    [str(metaeditor), f'/compile:"{target_src}"', "/log"],
                    capture_output=True, text=True, timeout=60,
                )
                compiled = experts / "PineTunnel_EA_MT4.ex4"
                if compiled.exists():
                    print_ok(f"Compiled: {compiled}")
                else:
                    print_warn("Compilation may have failed. Check MetaEditor log.")
            except (subprocess.TimeoutExpired, OSError) as e:
                print_fail(f"Compilation failed: {e}")

    # Copy DLL source and compile with CMake if available
    dll_source = _EA_BASE / "dll" / "PTWebSocket"
    cmake = shutil.which("cmake")
    if dll_source.exists() and cmake:
        print("  Compiling DLL with CMake...")
        build_dir = Path(data_dir) / "MQL5" / "Libraries"
        build_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [cmake, "-S", str(dll_source), "-B", str(build_dir / "build"),
                 "-A", "x64" if plat == "mt5" else "Win32"],
                capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                [cmake, "--build", str(build_dir / "build"), "--config", "Release"],
                capture_output=True, text=True, timeout=60,
            )
            dll_name = "PTWebSocket.dll" if plat == "mt5" else "PTWebSocket32.dll"
            compiled_dll = build_dir / dll_name
            if compiled_dll.exists():
                print_ok(f"DLL compiled: {compiled_dll}")
            else:
                print_warn("DLL compilation may have failed. Check CMake output.")
        except (subprocess.TimeoutExpired, OSError) as e:
            print_warn(f"DLL compilation failed: {e}")
    elif not cmake:
        print_warn("CMake not found. DLL must be compiled manually or downloaded from releases.")

    return True
