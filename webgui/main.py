"""
Meshing-Around Web GUI - FastAPI Backend
Configuration management API for MeshBOT
"""

import os
import re
import json
import gzip
import shutil
import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.security import APIKeyHeader
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from pydantic import BaseModel
import secrets

from config_schema import CONFIG_SCHEMA, SECTION_ORDER, INTERFACE_FIELDS, PRIMARY_INTERFACE_FIELDS
from oidc import (
    OIDC_ENABLED, OIDC_SESSION_SECRET, SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE, SESSION_COOKIE_HTTPONLY, SESSION_COOKIE_SAMESITE,
    oauth, create_session, get_session, destroy_session,
    get_user_from_request, cleanup_expired_sessions,
    OIDC_REDIRECT_URI, OIDC_POST_LOGOUT_REDIRECT, OIDC_SESSION_MAX_AGE,
)

# Configuration
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/opt/meshing-around/config.ini")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/opt/meshing-around/webgui/backups")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "meshbot")
SCHEDULES_PATH = Path(os.environ.get("SCHEDULES_PATH", "/app/data/schedules.json"))
SCHEDULER_LOG_PATH = Path(os.environ.get("SCHEDULER_LOG_PATH", "/app/data/scheduler_log.json"))
MESHBOT_LOG_PATH = os.environ.get("MESHBOT_LOG_PATH", "/opt/meshing-around/logs/meshbot.log")
LOG_ARCHIVE_DIR = os.environ.get("LOG_ARCHIVE_DIR", "/app/log_archives")
BBS_PEERS_PATH = os.environ.get("BBS_PEERS_PATH", "/app/data/bbs_peers.json")
LEADERBOARD_EXPORT_PATH = os.environ.get("LEADERBOARD_EXPORT_PATH", "/app/data/leaderboard_webgui.json")
NODEDB_PATH = os.environ.get("NODEDB_PATH", "/app/data/nodedb.json")
MAX_LOG_ENTRIES = 100  # Keep last 100 log entries
LOG_ARCHIVE_INTERVAL = 3600  # Archive logs every hour
LOG_RETENTION_DAYS = 30  # Keep archives for 30 days

# Authentication
# Set WEBGUI_API_KEY env var to enable API key auth
# If not set, auth is disabled (backward compatible)
WEBGUI_API_KEY = os.environ.get("WEBGUI_API_KEY", "")
AUTH_ENABLED = bool(WEBGUI_API_KEY)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_auth(request: Request, api_key: str = Depends(api_key_header)):
    """Verify authentication via OIDC session or API key."""
    # If no auth methods configured, allow all
    if not AUTH_ENABLED and not OIDC_ENABLED:
        return True

    # Skip auth for public paths
    path = request.url.path
    if path in ("/", "/health") or path.startswith("/static") or path.startswith("/auth/"):
        return True

    # Check OIDC session cookie
    if OIDC_ENABLED:
        user = get_user_from_request(request)
        if user:
            return True

    # Check API key (header or query param)
    if AUTH_ENABLED:
        if api_key and secrets.compare_digest(api_key, WEBGUI_API_KEY):
            return True
        query_key = request.query_params.get("api_key", "")
        if query_key and secrets.compare_digest(query_key, WEBGUI_API_KEY):
            return True

    # All auth methods failed
    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "ApiKey"},
    )

# Lifespan handler for background tasks
@asynccontextmanager
async def lifespan(app: FastAPI):
    global archive_task, session_cleanup_task
    ensure_archive_dir_startup()
    ensure_data_files_startup()
    archive_task = asyncio.create_task(periodic_archive_task())
    session_cleanup_task = asyncio.create_task(periodic_session_cleanup())
    yield
    if archive_task:
        archive_task.cancel()
    if session_cleanup_task:
        session_cleanup_task.cancel()

def ensure_archive_dir_startup():
    """Create archive directory on startup."""
    Path(LOG_ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)

def ensure_data_files_startup():
    """Migrate data files to shared directory on startup."""
    # Migrate schedules.json from old location
    old_schedules = Path(__file__).parent / "schedules.json"
    if old_schedules.exists() and not SCHEDULES_PATH.exists():
        SCHEDULES_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(old_schedules), str(SCHEDULES_PATH))
        print(f"Migrated schedules.json to {SCHEDULES_PATH}")

    # Same for scheduler_log.json
    old_log = Path(__file__).parent / "scheduler_log.json"
    if old_log.exists() and not SCHEDULER_LOG_PATH.exists():
        SCHEDULER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(old_log), str(SCHEDULER_LOG_PATH))
        print(f"Migrated scheduler_log.json to {SCHEDULER_LOG_PATH}")

async def periodic_archive_task():
    """Background task to archive logs periodically."""
    while True:
        await asyncio.sleep(LOG_ARCHIVE_INTERVAL)
        try:
            archive_current_log()
            cleanup_old_archives()
            print(f"Log archived at {datetime.now()}")
        except Exception as e:
            print(f"Archive task error: {e}")

archive_task = None
session_cleanup_task = None

async def periodic_session_cleanup():
    """Periodically clean up expired OIDC sessions."""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        try:
            removed = cleanup_expired_sessions()
            if removed:
                print(f"Cleaned up {removed} expired sessions")
        except Exception as e:
            print(f"Session cleanup error: {e}")

app = FastAPI(
    title="MeshBOT Config Manager",
    description="Web GUI for managing meshing-around configuration",
    version="1.0.0",
    lifespan=lifespan,
    dependencies=[Depends(verify_auth)],
)

# CORS - restrict to same-origin by default
# Override with CORS_ORIGINS env var for specific origins (comma-separated)
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") else []

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Session middleware (required by authlib for OAuth state)
app.add_middleware(SessionMiddleware, secret_key=OIDC_SESSION_SECRET)

# Mount static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


class ConfigUpdate(BaseModel):
    """Model for config updates"""
    section: str
    key: str
    value: Any


class BulkConfigUpdate(BaseModel):
    """Model for bulk config updates"""
    updates: Dict[str, Dict[str, Any]]


class InterfaceUpdate(BaseModel):
    """Model for interface updates"""
    enabled: Optional[bool] = None
    type: Optional[str] = None
    port: Optional[str] = None
    hostname: Optional[str] = None
    mac: Optional[str] = None


class ScheduleItem(BaseModel):
    """Model for a scheduled item"""
    id: Optional[int] = None
    enabled: bool = True
    name: str = ""
    frequency: str = "day"  # minutes, hours, day, days, or weekday name
    time: Optional[str] = None  # HH:MM format
    interval: Optional[int] = None  # for minutes/hours/days
    day: Optional[str] = None  # for specific weekday
    message: str = ""
    action: str = "message"  # message, weather, joke, news, rss, etc.
    channel: int = 0
    interface: int = 1
    targetNode: Optional[str] = None  # For DMs - the target node ID


class SchedulerLogEntry(BaseModel):
    """Model for a scheduler log entry"""
    timestamp: str
    schedule_name: str
    action: str
    message: str
    channel: int
    interface: int
    status: str = "sent"  # sent, failed, pending


class ConfigParser:
    """Custom config parser that preserves comments and formatting"""

    def __init__(self, path: str):
        self.path = path
        self.lines: List[str] = []
        self.sections: Dict[str, Dict[str, str]] = {}
        self.comments: Dict[str, Dict[str, str]] = {}

    def read(self) -> Dict[str, Dict[str, str]]:
        """Read config file preserving structure"""
        self.lines = []
        self.sections = {}
        self.comments = {}

        current_section = None
        current_comment = []

        with open(self.path, 'r', encoding='utf-8') as f:
            for line in f:
                self.lines.append(line)
                stripped = line.strip()

                if stripped.startswith('#') or stripped == '':
                    current_comment.append(line)
                    continue

                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped[1:-1]
                    self.sections[current_section] = {}
                    self.comments[current_section] = {}
                    current_comment = []
                    continue

                if current_section and '=' in stripped:
                    key, value = stripped.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    self.sections[current_section][key] = value
                    if current_comment:
                        self.comments[current_section][key] = ''.join(current_comment)
                    current_comment = []

        return self.sections

    def get(self, section: str, key: str, default: str = '') -> str:
        return self.sections.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        if section not in self.sections:
            self.sections[section] = {}

        if isinstance(value, bool):
            value = str(value)
        elif isinstance(value, (list, tuple)):
            value = ','.join(str(v) for v in value)
        else:
            value = str(value)

        self.sections[section][key] = value

    def add_section(self, section: str) -> None:
        if section not in self.sections:
            self.sections[section] = {}
            self.lines.append(f"\n[{section}]\n")

    def remove_section(self, section: str) -> bool:
        if section not in self.sections:
            return False
        
        del self.sections[section]
        
        new_lines = []
        in_section = False
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                current = stripped[1:-1]
                if current == section:
                    in_section = True
                    continue
                else:
                    in_section = False
            if not in_section:
                new_lines.append(line)
        
        self.lines = new_lines
        return True

    def write(self) -> None:
        new_lines = []
        current_section = None
        written_keys = set()
        written_sections = set()

        for line in self.lines:
            stripped = line.strip()

            if stripped.startswith('#') or stripped == '':
                new_lines.append(line)
                continue

            if stripped.startswith('[') and stripped.endswith(']'):
                if current_section and current_section in self.sections:
                    section_written = written_keys.copy()
                    for key, value in self.sections[current_section].items():
                        if f"{current_section}.{key}" not in section_written:
                            new_lines.append(f"{key} = {value}\n")
                            written_keys.add(f"{current_section}.{key}")

                current_section = stripped[1:-1]
                written_sections.add(current_section)
                new_lines.append(line)
                continue

            if current_section and '=' in stripped:
                key = stripped.split('=', 1)[0].strip()
                if current_section in self.sections and key in self.sections[current_section]:
                    value = self.sections[current_section][key]
                    indent = len(line) - len(line.lstrip())
                    new_lines.append(' ' * indent + f"{key} = {value}\n")
                    written_keys.add(f"{current_section}.{key}")
                else:
                    new_lines.append(line)
                continue

            new_lines.append(line)

        if current_section and current_section in self.sections:
            for key, value in self.sections[current_section].items():
                if f"{current_section}.{key}" not in written_keys:
                    new_lines.append(f"{key} = {value}\n")
                    written_keys.add(f"{current_section}.{key}")

        for section, keys in self.sections.items():
            if section not in written_sections:
                new_lines.append(f"\n[{section}]\n")
                for key, value in keys.items():
                    new_lines.append(f"{key} = {value}\n")

        with open(self.path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)


def create_backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = os.path.join(BACKUP_DIR, f"config-{timestamp}.ini")
    shutil.copy2(CONFIG_PATH, backup_path)
    return backup_path


def parse_value(value: str, field_type: str) -> Any:
    if field_type == 'boolean':
        return value.lower() in ('true', '1', 'yes', 'on')
    elif field_type == 'integer':
        try:
            return int(value)
        except ValueError:
            return 0
    elif field_type == 'float':
        try:
            return float(value)
        except ValueError:
            return 0.0
    elif field_type == 'list':
        return [v.strip() for v in value.split(',') if v.strip()]
    else:
        return value


def format_value(value: Any, field_type: str) -> str:
    if field_type == 'boolean':
        return str(bool(value))
    elif field_type == 'list':
        if isinstance(value, list):
            return ','.join(str(v) for v in value)
        return str(value)
    else:
        return str(value)


def get_interface_section_name(num: int) -> str:
    if num == 1:
        return "interface"
    return f"interface{num}"


def get_all_interfaces(parser: ConfigParser) -> Dict[int, Dict[str, Any]]:
    interfaces = {}
    
    for i in range(1, 10):
        section = get_interface_section_name(i)
        if section in parser.sections:
            config = {}
            fields = PRIMARY_INTERFACE_FIELDS if i == 1 else INTERFACE_FIELDS
            for key, field_info in fields.items():
                raw_value = parser.sections[section].get(key, '')
                if raw_value:
                    config[key] = parse_value(raw_value, field_info['type'])
                else:
                    config[key] = field_info.get('default', '')
            interfaces[i] = config
    
    return interfaces


# Schedule management functions
def load_schedules() -> List[Dict]:
    if SCHEDULES_PATH.exists():
        with open(SCHEDULES_PATH, 'r') as f:
            data = json.load(f)
            return data.get('schedules', [])
    return []


def save_schedules(schedules: List[Dict]) -> None:
    with open(SCHEDULES_PATH, 'w') as f:
        json.dump({'schedules': schedules}, f, indent=2)


def get_next_schedule_id(schedules: List[Dict]) -> int:
    if not schedules:
        return 1
    return max(s.get('id', 0) for s in schedules) + 1


# Scheduler Log Functions
def load_scheduler_log() -> List[Dict]:
    """Load scheduler activity log"""
    if SCHEDULER_LOG_PATH.exists():
        try:
            with open(SCHEDULER_LOG_PATH, 'r') as f:
                data = json.load(f)
                return data.get('entries', [])
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_scheduler_log(entries: List[Dict]) -> None:
    """Save scheduler activity log, keeping only the last MAX_LOG_ENTRIES"""
    # Keep only the most recent entries
    entries = entries[-MAX_LOG_ENTRIES:] if len(entries) > MAX_LOG_ENTRIES else entries
    with open(SCHEDULER_LOG_PATH, 'w') as f:
        json.dump({'entries': entries}, f, indent=2)


def add_scheduler_log_entry(schedule_name: str, action: str, message: str,
                            channel: int, interface: int, status: str = "sent") -> Dict:
    """Add a new entry to the scheduler log"""
    entries = load_scheduler_log()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "schedule_name": schedule_name,
        "action": action,
        "message": message[:200] if message else "",  # Truncate long messages
        "channel": channel,
        "interface": interface,
        "status": status
    }
    entries.append(entry)
    save_scheduler_log(entries)
    return entry


def clear_scheduler_log() -> None:
    """Clear all scheduler log entries"""
    save_scheduler_log([])


def tail_file(filepath: str, max_lines: int = 2000, encoding: str = 'utf-8') -> List[str]:
    """
    Read the last max_lines from a file efficiently by seeking from the end.
    Avoids reading the entire file into memory.
    """
    try:
        file_size = os.path.getsize(filepath)
        if file_size == 0:
            return []

        # Estimate: average log line ~200 bytes, read 2x for safety
        chunk_size = min(file_size, max_lines * 400)

        with open(filepath, 'r', encoding=encoding, errors='ignore') as f:
            # Seek to near the end
            if file_size > chunk_size:
                f.seek(file_size - chunk_size)
                f.readline()  # Discard partial first line
            lines = f.readlines()

        return lines[-max_lines:] if len(lines) > max_lines else lines
    except (IOError, OSError):
        return []


def parse_meshbot_log(max_entries: int = MAX_LOG_ENTRIES) -> List[Dict]:
    """
    Parse meshbot log file for channel broadcasts and their status.
    Returns entries with status: 'sent' (confirmed), 'failed' (error after send), 'pending' (no confirmation)

    Log patterns:
    - Channel send attempt: "Device:X Channel:Y SendingChannel: <message>"
    - DM send (ignored): "Device:X Sending DM: <message> To: <recipient>"
    - Send failure: "Exception during send_message: <error>"
    """
    entries = []
    log_path = Path(MESHBOT_LOG_PATH)

    if not log_path.exists():
        return entries

    lines = tail_file(str(log_path), max_lines=max_entries * 5)
    if not lines:
        return entries

    # Regex patterns
    # Channel send: 2025-11-28 14:38:41,970 |     INFO | Device:1 Channel:2 SendingChannel: bbslink MeshBot looking for peers
    channel_send_pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*Device:(\d+) Channel:(\d+) SendingChannel: (.+)$'
    )
    # Error patterns with capture groups for the error message
    error_patterns = [
        re.compile(r'Exception during send_message:\s*(.+)', re.IGNORECASE),
        re.compile(r'Error Opening interface\d+ on:\s*(.+)', re.IGNORECASE),
        re.compile(r'Error.*send.*?:\s*(.+)', re.IGNORECASE),
        re.compile(r'failed to send.*?:\s*(.+)', re.IGNORECASE),
        re.compile(r'\|\s*ERROR\s*\|\s*(.+)', re.IGNORECASE),
    ]

    # Process lines looking for channel sends
    pending_send = None

    for i, line in enumerate(lines):
        line = line.strip()

        # Check for channel send
        match = channel_send_pattern.match(line)
        if match:
            timestamp_str, device, channel, message = match.groups()

            # If there's a pending send without error, mark it as sent
            if pending_send:
                entries.append(pending_send)

            # Create new pending entry
            try:
                # Parse timestamp
                timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                pending_send = {
                    "timestamp": timestamp.isoformat(),
                    "schedule_name": "Channel Broadcast",
                    "action": "message",
                    "message": message[:200] if message else "",
                    "channel": int(channel),
                    "interface": int(device),
                    "status": "sent"  # Default to sent, will be changed if error found
                }
            except ValueError:
                pending_send = None
            continue

        # Check for error immediately after a send attempt
        if pending_send:
            for error_pattern in error_patterns:
                error_match = error_pattern.search(line)
                if error_match:
                    pending_send["status"] = "failed"
                    pending_send["error"] = error_match.group(1).strip() if error_match.lastindex else line.strip()
                    entries.append(pending_send)
                    pending_send = None
                    break
            if pending_send is None:
                continue

    # Add last pending send if exists
    if pending_send:
        entries.append(pending_send)

    # Sort by timestamp descending and limit
    entries.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return entries[:max_entries]


def get_activity_log() -> List[Dict]:
    """
    Get combined activity log from meshbot logs.
    Parses actual log file to show channel broadcasts with their send status.
    """
    return parse_meshbot_log(MAX_LOG_ENTRIES)


def get_meshbot_logs(max_lines: int = 500, level: str = None, search: str = None) -> List[Dict]:
    """
    Get meshbot log entries with optional filtering.

    Args:
        max_lines: Maximum number of lines to return
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR)
        search: Search term to filter messages

    Returns:
        List of log entry dictionaries with timestamp, level, source, message
    """
    log_path = Path(MESHBOT_LOG_PATH)
    entries = []

    if not log_path.exists():
        return entries

    # Read enough lines to satisfy the request after filtering
    read_count = max_lines * 10 if (level or search) else max_lines * 2
    lines = tail_file(str(log_path), max_lines=read_count)
    if not lines:
        return entries

    # Log pattern: 2025-11-28 17:02:55,911 |    DEBUG | System: Message here
    log_pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|\s*(DEBUG|INFO|WARNING|ERROR)\s*\|\s*(.+)$'
    )

    for line in lines:
        line = line.strip()
        # Remove ANSI color codes
        line = re.sub(r'\x1b\[[0-9;]*m', '', line)

        match = log_pattern.match(line)
        if match:
            timestamp_str, log_level, message = match.groups()

            # Filter by level if specified
            if level and log_level != level.upper():
                continue

            # Filter by search term if specified
            if search and search.lower() not in message.lower():
                continue

            # Parse source and message
            source = "System"
            msg_content = message
            if ': ' in message:
                parts = message.split(': ', 1)
                source = parts[0]
                msg_content = parts[1] if len(parts) > 1 else message

            entries.append({
                "timestamp": timestamp_str,
                "level": log_level,
                "source": source,
                "message": msg_content
            })

    # Return last N entries (most recent)
    return entries[-max_lines:] if len(entries) > max_lines else entries


# Log Archive Functions

def ensure_archive_dir():
    """Ensure the log archive directory exists."""
    archive_path = Path(LOG_ARCHIVE_DIR)
    archive_path.mkdir(parents=True, exist_ok=True)
    return archive_path


def archive_current_log() -> Optional[str]:
    """
    Archive the current meshbot log file.
    Creates a gzipped copy with timestamp in the archive directory.
    Returns the archive filename or None if failed.
    """
    log_path = Path(MESHBOT_LOG_PATH)
    if not log_path.exists():
        return None

    try:
        archive_dir = ensure_archive_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"meshbot_{timestamp}.log.gz"
        archive_path = archive_dir / archive_name

        # Read and compress the log
        with open(log_path, 'rb') as f_in:
            with gzip.open(archive_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

        return archive_name
    except Exception as e:
        print(f"Error archiving log: {e}")
        return None


def cleanup_old_archives():
    """Remove archives older than LOG_RETENTION_DAYS."""
    archive_dir = Path(LOG_ARCHIVE_DIR)
    if not archive_dir.exists():
        return

    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)

    for archive_file in archive_dir.glob("meshbot_*.log.gz"):
        try:
            # Parse timestamp from filename: meshbot_YYYYMMDD_HHMMSS.log.gz
            name_parts = archive_file.stem.replace('.log', '').split('_')
            if len(name_parts) >= 3:
                date_str = f"{name_parts[1]}_{name_parts[2]}"
                file_date = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
                if file_date < cutoff:
                    archive_file.unlink()
        except (ValueError, IndexError):
            continue


def get_log_archives() -> List[Dict]:
    """Get list of available log archives."""
    archive_dir = Path(LOG_ARCHIVE_DIR)
    if not archive_dir.exists():
        return []

    archives = []
    for archive_file in sorted(archive_dir.glob("meshbot_*.log.gz"), reverse=True):
        try:
            stat = archive_file.stat()
            # Parse date from filename
            name_parts = archive_file.stem.replace('.log', '').split('_')
            if len(name_parts) >= 3:
                date_str = f"{name_parts[1]}_{name_parts[2]}"
                file_date = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
                archives.append({
                    "filename": archive_file.name,
                    "date": file_date.isoformat(),
                    "size": stat.st_size,
                    "size_human": f"{stat.st_size / 1024:.1f} KB"
                })
        except (ValueError, IndexError):
            continue

    return archives


def read_archive(filename: str, max_lines: int = 1000) -> List[str]:
    """Read contents of an archived log file."""
    archive_path = Path(LOG_ARCHIVE_DIR) / filename
    if not archive_path.exists() or not filename.endswith('.gz'):
        return []

    try:
        with gzip.open(archive_path, 'rt', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        return lines[-max_lines:] if len(lines) > max_lines else lines
    except Exception:
        return []


# API Routes

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # If OIDC enabled, check for session before serving the page
    if OIDC_ENABLED:
        user = get_user_from_request(request)
        if not user:
            return RedirectResponse(url="/auth/login")

    html_path = Path(__file__).parent / "templates" / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse("<h1>MeshBOT Config Manager</h1><p>Loading...</p>")


@app.get("/health")
async def health_check():
    """Health check endpoint for Docker/monitoring."""
    return {
        "status": "ok",
        "auth_enabled": AUTH_ENABLED,
        "oidc_enabled": OIDC_ENABLED,
    }


# --- OIDC Auth Routes ---

@app.get("/auth/login")
async def auth_login(request: Request):
    """Initiate OIDC login flow."""
    if not OIDC_ENABLED:
        raise HTTPException(status_code=404, detail="OIDC not configured")

    # Build redirect URI
    redirect_uri = OIDC_REDIRECT_URI
    if not redirect_uri:
        redirect_uri = str(request.url_for("auth_callback"))

    return await oauth.oidc.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Handle OIDC callback after provider authentication."""
    if not OIDC_ENABLED:
        raise HTTPException(status_code=404, detail="OIDC not configured")

    try:
        token = await oauth.oidc.authorize_access_token(request)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

    # Get user info from ID token or userinfo endpoint
    user_info = token.get("userinfo")
    if not user_info:
        user_info = await oauth.oidc.userinfo(token=token)

    if not user_info:
        raise HTTPException(status_code=401, detail="Could not retrieve user info")

    # Create session
    user_data = {
        "sub": user_info.get("sub", ""),
        "email": user_info.get("email", ""),
        "name": user_info.get("name", user_info.get("preferred_username", "")),
    }
    signed_session = create_session(user_data)

    # Redirect to app with session cookie
    response = RedirectResponse(url="/")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed_session,
        max_age=OIDC_SESSION_MAX_AGE,
        httponly=SESSION_COOKIE_HTTPONLY,
        secure=SESSION_COOKIE_SECURE,
        samesite=SESSION_COOKIE_SAMESITE,
    )
    return response


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Logout — destroy session and optionally redirect to provider logout."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        destroy_session(cookie)

    response = RedirectResponse(url=OIDC_POST_LOGOUT_REDIRECT)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


@app.get("/auth/me")
async def auth_me(request: Request):
    """Get current authenticated user info."""
    if not OIDC_ENABLED:
        return {"oidc_enabled": False, "user": None}

    user = get_user_from_request(request)
    if user:
        return {"oidc_enabled": True, "user": user}
    return {"oidc_enabled": True, "user": None}


@app.get("/api/auth/status")
async def auth_status():
    """Check if authentication is enabled."""
    return {"auth_enabled": AUTH_ENABLED, "oidc_enabled": OIDC_ENABLED}


@app.get("/api/schema")
async def get_schema():
    return {
        "schema": CONFIG_SCHEMA,
        "order": SECTION_ORDER,
        "interfaceFields": INTERFACE_FIELDS,
        "primaryInterfaceFields": PRIMARY_INTERFACE_FIELDS
    }


# Schedule endpoints

@app.get("/api/schedules")
def get_schedules():
    """Get all custom schedules"""
    schedules = load_schedules()
    return {"schedules": schedules}


@app.get("/api/schedules/{schedule_id}")
def get_schedule(schedule_id: int):
    """Get a specific schedule"""
    schedules = load_schedules()
    for s in schedules:
        if s.get('id') == schedule_id:
            return {"schedule": s}
    raise HTTPException(status_code=404, detail="Schedule not found")


@app.post("/api/schedules")
def create_schedule(schedule: ScheduleItem):
    """Create a new schedule"""
    schedules = load_schedules()
    new_schedule = schedule.model_dump()
    new_schedule['id'] = get_next_schedule_id(schedules)
    schedules.append(new_schedule)
    save_schedules(schedules)
    return {"success": True, "schedule": new_schedule}


@app.put("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: int, schedule: ScheduleItem):
    """Update an existing schedule"""
    schedules = load_schedules()
    for i, s in enumerate(schedules):
        if s.get('id') == schedule_id:
            updated = schedule.model_dump()
            updated['id'] = schedule_id
            schedules[i] = updated
            save_schedules(schedules)
            return {"success": True, "schedule": updated}
    raise HTTPException(status_code=404, detail="Schedule not found")


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: int):
    """Delete a schedule"""
    schedules = load_schedules()
    for i, s in enumerate(schedules):
        if s.get('id') == schedule_id:
            del schedules[i]
            save_schedules(schedules)
            return {"success": True, "deleted": schedule_id}
    raise HTTPException(status_code=404, detail="Schedule not found")


# Scheduler Log endpoints

@app.get("/api/scheduler/log")
def get_scheduler_log():
    """Get activity log from meshbot logs (channel broadcasts only, no DMs)"""
    entries = get_activity_log()
    return {"entries": entries}


@app.post("/api/scheduler/log")
def add_log_entry(entry: SchedulerLogEntry):
    """Add a new scheduler log entry"""
    new_entry = add_scheduler_log_entry(
        schedule_name=entry.schedule_name,
        action=entry.action,
        message=entry.message,
        channel=entry.channel,
        interface=entry.interface,
        status=entry.status
    )
    return {"success": True, "entry": new_entry}


@app.delete("/api/scheduler/log")
def delete_scheduler_log():
    """Clear all scheduler log entries"""
    clear_scheduler_log()
    return {"success": True, "message": "Scheduler log cleared"}

# Log viewer endpoints

@app.get("/api/logs")
def get_logs(
    lines: int = 500,
    level: Optional[str] = None,
    search: Optional[str] = None
):
    """
    Get meshbot log entries with optional filtering.

    Args:
        lines: Maximum number of lines to return (default 500)
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR)
        search: Search term to filter messages
    """
    entries = get_meshbot_logs(max_lines=lines, level=level, search=search)

    # Count by level for stats
    level_counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0}
    for entry in entries:
        lvl = entry.get("level", "")
        if lvl in level_counts:
            level_counts[lvl] += 1

    return {
        "entries": entries,
        "total": len(entries),
        "counts": level_counts,
        "filters": {
            "level": level,
            "search": search,
            "lines": lines
        }
    }


# Log Archive endpoints

@app.get("/api/logs/archives")
def list_archives():
    """Get list of available log archives."""
    archives = get_log_archives()
    return {
        "archives": archives,
        "total": len(archives),
        "retention_days": LOG_RETENTION_DAYS
    }


@app.post("/api/logs/archive")
def create_archive():
    """Create a new archive of the current log."""
    filename = archive_current_log()
    if filename:
        return {"success": True, "filename": filename}
    raise HTTPException(status_code=500, detail="Failed to create archive")


@app.get("/api/logs/archives/{filename}")
def get_archive_content(filename: str, lines: int = 1000):
    """Get contents of a specific archive."""
    if not filename.endswith('.gz') or '..' in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    content = read_archive(filename, max_lines=lines)
    if not content:
        raise HTTPException(status_code=404, detail="Archive not found")

    return {
        "filename": filename,
        "lines": content,
        "total": len(content)
    }


@app.delete("/api/logs/archives/{filename}")
def delete_archive(filename: str):
    """Delete a specific archive."""
    if not filename.endswith('.gz') or '..' in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    archive_path = Path(LOG_ARCHIVE_DIR) / filename
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="Archive not found")

    try:
        archive_path.unlink()
        return {"success": True, "deleted": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Packet Monitor endpoints
PACKET_BUFFER_PATH = os.environ.get("PACKET_BUFFER_PATH", "/opt/meshing-around/data/packets.json")

@app.get("/api/packets")
def get_packets(since: Optional[str] = None):
    """
    Get packet monitor entries.
    
    Args:
        since: Only return packets after this timestamp (ISO format)
    """
    try:
        if not os.path.exists(PACKET_BUFFER_PATH):
            return {"packets": [], "total": 0}
        
        with open(PACKET_BUFFER_PATH, 'r') as f:
            packets = json.load(f)
        
        # Filter by timestamp if provided
        if since:
            packets = [p for p in packets if p.get('timestamp_full', '') > since]
        
        return {
            "packets": packets,
            "total": len(packets)
        }
    except Exception as e:
        return {"packets": [], "total": 0, "error": str(e)}

@app.delete("/api/packets")
def clear_packets():
    """Clear all packet monitor entries."""
    try:
        if os.path.exists(PACKET_BUFFER_PATH):
            with open(PACKET_BUFFER_PATH, 'w') as f:
                json.dump([], f)
        return {"success": True, "message": "Packet buffer cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# BBS Network endpoints

def load_bbs_peers() -> Dict:
    """Load BBS peers data from file."""
    if not os.path.exists(BBS_PEERS_PATH):
        return {"peers": {}, "last_updated": None}
    try:
        with open(BBS_PEERS_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"peers": {}, "last_updated": None}


def save_bbs_peers(data: Dict) -> None:
    """Save BBS peers data to file with atomic write."""
    data["last_updated"] = datetime.now().isoformat()
    temp_path = BBS_PEERS_PATH + ".tmp"
    try:
        with open(temp_path, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, BBS_PEERS_PATH)
    except IOError as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise e


def parse_bbs_events_from_log() -> List[Dict]:
    """
    Parse meshbot log for BBS link events (bbslink, bbsack).

    Log patterns:
    - Channel broadcast: "Device:X Channel:Y SendingChannel: bbslink MeshBot looking for peers"
    - Received bbslink: "Device:X Channel:Y ReceivedChannel: bbslink MeshBot looking for peers From: NodeName"
    - DM bbslink: "Device:X Sending DM: bbslink N $subject #body @node To: NodeName"
    - DM received: "Device:X Channel: Y Received DM: bbslink N $subject #body From: NodeName"
    - Wait to sync: "System: wait to bbslink with peer NODEID"
    - Sending sync: "System: Sending bbslink message N of M to peer NODEID"
    - Sync complete: "System: bbslink sync complete with peer NODEID"
    """
    events = []
    log_path = Path(MESHBOT_LOG_PATH)

    if not log_path.exists():
        return events

    lines = tail_file(str(log_path), max_lines=5000)
    if not lines:
        return events

    # Regex patterns for BBS events
    patterns = {
        # Channel broadcast sent
        'broadcast_sent': re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*Device:(\d+) Channel:(\d+) SendingChannel: (bbslink.*)$',
            re.IGNORECASE
        ),
        # Channel message received with node name
        'broadcast_received': re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*Device:(\d+) Channel:(\d+) ReceivedChannel: (bbslink.*) From: (.+)$',
            re.IGNORECASE
        ),
        # DM sent with node name
        'dm_sent': re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*Device:(\d+) Sending DM: (bbslink.*|bbsack.*) To: (.+)$',
            re.IGNORECASE
        ),
        # DM received with node name
        'dm_received': re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*Device:(\d+) Channel: (\d+) Received DM: (bbslink.*|bbsack.*) From: (.+)$',
            re.IGNORECASE
        ),
        # Debug: wait to sync
        'wait_sync': re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*System: wait to bbslink with peer (\d+)$'
        ),
        # Debug: sending message
        'sending_sync': re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*System: Sending bbslink message (\d+) of (\d+) to peer (\d+)$'
        ),
        # Debug: sync complete
        'sync_complete': re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \|.*System: bbslink sync complete with peer (\d+)$'
        ),
    }

    for line in lines:
        line = line.strip()

        # Check broadcast sent
        match = patterns['broadcast_sent'].match(line)
        if match and 'bbslink' in match.group(4).lower():
            events.append({
                'timestamp': match.group(1),
                'type': 'broadcast_sent',
                'device': int(match.group(2)),
                'channel': int(match.group(3)),
                'message': match.group(4)
            })
            continue

        # Check broadcast received
        match = patterns['broadcast_received'].match(line)
        if match and 'bbslink' in match.group(4).lower():
            events.append({
                'timestamp': match.group(1),
                'type': 'broadcast_received',
                'device': int(match.group(2)),
                'channel': int(match.group(3)),
                'message': match.group(4),
                'node_name': match.group(5)
            })
            continue

        # Check DM sent
        match = patterns['dm_sent'].match(line)
        if match:
            events.append({
                'timestamp': match.group(1),
                'type': 'dm_sent',
                'device': int(match.group(2)),
                'message': match.group(3),
                'node_name': match.group(4)
            })
            continue

        # Check DM received
        match = patterns['dm_received'].match(line)
        if match:
            events.append({
                'timestamp': match.group(1),
                'type': 'dm_received',
                'device': int(match.group(2)),
                'channel': int(match.group(3)),
                'message': match.group(4),
                'node_name': match.group(5)
            })
            continue

        # Check wait sync
        match = patterns['wait_sync'].match(line)
        if match:
            events.append({
                'timestamp': match.group(1),
                'type': 'wait_sync',
                'node_id': int(match.group(2))
            })
            continue

        # Check sending sync
        match = patterns['sending_sync'].match(line)
        if match:
            events.append({
                'timestamp': match.group(1),
                'type': 'sending_sync',
                'message_num': int(match.group(2)),
                'total_messages': int(match.group(3)),
                'node_id': int(match.group(4))
            })
            continue

        # Check sync complete
        match = patterns['sync_complete'].match(line)
        if match:
            events.append({
                'timestamp': match.group(1),
                'type': 'sync_complete',
                'node_id': int(match.group(2))
            })

    return events


def update_bbs_peers_from_events(events: List[Dict]) -> Dict:
    """
    Update BBS peers data structure from parsed events.
    Returns updated peers dictionary.
    """
    data = load_bbs_peers()
    peers = data.get("peers", {})

    for event in events:
        node_key = None
        node_name = event.get('node_name')
        node_id = event.get('node_id')

        # Determine node key (use name if available, otherwise ID)
        if node_name:
            node_key = node_name
        elif node_id:
            node_key = str(node_id)

        if not node_key:
            continue

        # Initialize peer if not exists
        if node_key not in peers:
            peers[node_key] = {
                'node_name': node_name or f"Node {node_id}",
                'node_id': node_id,
                'first_seen': event['timestamp'],
                'last_seen': event['timestamp'],
                'sync_count': 0,
                'messages_synced': 0,
                'last_sync_type': None,
                'sync_history': []
            }

        peer = peers[node_key]

        # Update last seen
        if event['timestamp'] > peer.get('last_seen', ''):
            peer['last_seen'] = event['timestamp']

        # Update node_id if we have it now
        if node_id and not peer.get('node_id'):
            peer['node_id'] = node_id

        # Track sync events
        sync_event = {
            'timestamp': event['timestamp'],
            'type': event['type'],
            'details': event.get('message', '')[:100]
        }

        # Keep only last 20 sync events per peer
        peer['sync_history'] = peer.get('sync_history', [])[-19:] + [sync_event]

        # Update sync statistics
        if event['type'] in ('sync_complete', 'dm_sent', 'dm_received'):
            peer['sync_count'] = peer.get('sync_count', 0) + 1
            peer['last_sync_type'] = event['type']

        if event['type'] == 'sending_sync':
            peer['messages_synced'] = max(
                peer.get('messages_synced', 0),
                event.get('total_messages', 0)
            )

    data['peers'] = peers
    return data


@app.get("/api/bbs/peers")
def get_bbs_peers(refresh: bool = False):
    """
    Get BBS network peer information.

    Args:
        refresh: If True, re-parse log file to update peers
    """
    try:
        if refresh:
            events = parse_bbs_events_from_log()
            data = update_bbs_peers_from_events(events)
            save_bbs_peers(data)
        else:
            data = load_bbs_peers()

        # Convert peers dict to list with computed status
        peers_list = []
        now = datetime.now()

        for key, peer in data.get('peers', {}).items():
            try:
                last_seen = datetime.fromisoformat(peer.get('last_seen', ''))
                minutes_ago = (now - last_seen).total_seconds() / 60

                if minutes_ago < 10:
                    status = 'active'
                elif minutes_ago < 60:
                    status = 'stale'
                else:
                    status = 'offline'
            except (ValueError, TypeError):
                status = 'unknown'
                minutes_ago = None

            peers_list.append({
                **peer,
                'key': key,
                'status': status,
                'minutes_ago': round(minutes_ago) if minutes_ago else None
            })

        # Sort by last_seen descending
        peers_list.sort(key=lambda x: x.get('last_seen', ''), reverse=True)

        return {
            "peers": peers_list,
            "total": len(peers_list),
            "active": sum(1 for p in peers_list if p['status'] == 'active'),
            "last_updated": data.get('last_updated')
        }
    except Exception as e:
        return {"peers": [], "total": 0, "error": str(e)}


@app.get("/api/bbs/events")
def get_bbs_events(limit: int = 50):
    """Get recent BBS link events from log."""
    try:
        events = parse_bbs_events_from_log()
        # Return most recent events first
        events.reverse()
        return {
            "events": events[:limit],
            "total": len(events)
        }
    except Exception as e:
        return {"events": [], "total": 0, "error": str(e)}


@app.delete("/api/bbs/peers")
def clear_bbs_peers():
    """Clear BBS peers tracking data."""
    try:
        if os.path.exists(BBS_PEERS_PATH):
            os.remove(BBS_PEERS_PATH)
        return {"success": True, "message": "BBS peers data cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Leaderboard endpoint

@app.get("/api/leaderboard")
def get_leaderboard():
    """Get mesh leaderboard data from exported JSON."""
    try:
        if not os.path.exists(LEADERBOARD_EXPORT_PATH):
            return {"leaderboard": {}, "error": "Leaderboard data not yet available"}

        with open(LEADERBOARD_EXPORT_PATH, 'r') as f:
            data = json.load(f)

        raw = data.get("leaderboard", {})

        # Format for display
        leaderboard = {}

        metrics = {
            'lowestBattery': {'icon': '🪫', 'label': 'Low Battery', 'unit': '%', 'precision': 1},
            'longestUptime': {'icon': '🕰️', 'label': 'Uptime', 'unit': 'seconds', 'precision': 0},
            'fastestSpeed': {'icon': '🚓', 'label': 'Speed', 'unit': 'km/h', 'precision': 1},
            'highestAltitude': {'icon': '🚀', 'label': 'Altitude', 'unit': 'm', 'precision': 0},
            'tallestNode': {'icon': '🪜', 'label': 'Tallest', 'unit': 'm', 'precision': 0},
            'coldestTemp': {'icon': '🥶', 'label': 'Coldest', 'unit': '°C', 'precision': 1},
            'hottestTemp': {'icon': '🥵', 'label': 'Hottest', 'unit': '°C', 'precision': 1},
            'mostMessages': {'icon': '💬', 'label': 'Most Messages', 'unit': '', 'precision': 0},
            'highestDBm': {'icon': '📶', 'label': 'Strongest Signal', 'unit': 'dBm', 'precision': 0},
            'weakestDBm': {'icon': '📶', 'label': 'Weakest Signal', 'unit': 'dBm', 'precision': 0},
        }

        for key, meta in metrics.items():
            if key in raw and raw[key].get('nodeID'):
                entry = raw[key]
                value = entry.get('value', 0)

                if key == 'longestUptime' and value > 0:
                    days = int(value // 86400)
                    hours = int((value % 86400) // 3600)
                    formatted_value = f"{days}d {hours}h" if days > 0 else f"{hours}h"
                    unit = ''
                else:
                    precision = meta['precision']
                    formatted_value = round(value, precision) if precision > 0 else int(value)
                    unit = meta['unit']

                leaderboard[key] = {
                    'nodeID': entry['nodeID'],
                    'nodeHex': f"!{entry['nodeID']:08x}",
                    'nodeName': entry.get('shortName') or entry.get('longName') or f"!{entry['nodeID']:08x}",
                    'value': value,
                    'formatted': f"{formatted_value}{unit}",
                    'icon': meta['icon'],
                    'label': meta['label'],
                    'timestamp': entry.get('timestamp', 0)
                }

        return {"leaderboard": leaderboard, "updated_at": data.get("updated_at")}
    except Exception as e:
        return {"leaderboard": {}, "error": str(e)}


# Interface endpoints

@app.get("/api/interfaces")
def get_interfaces():
    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()
        interfaces = get_all_interfaces(parser)
        return {"interfaces": interfaces}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/interfaces/{num}")
def get_interface(num: int):
    if num < 1 or num > 9:
        raise HTTPException(status_code=400, detail="Interface number must be 1-9")
    
    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()
        section = get_interface_section_name(num)
        
        if section not in parser.sections:
            raise HTTPException(status_code=404, detail=f"Interface {num} not configured")
        
        interfaces = get_all_interfaces(parser)
        return {"interface": num, "config": interfaces.get(num, {})}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/interfaces")
def add_interface(config: InterfaceUpdate):
    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()
        
        next_num = None
        for i in range(2, 10):
            section = get_interface_section_name(i)
            if section not in parser.sections:
                next_num = i
                break
        
        if next_num is None:
            raise HTTPException(status_code=400, detail="Maximum 9 interfaces supported")
        
        backup_path = create_backup()
        
        section = get_interface_section_name(next_num)
        parser.add_section(section)
        
        for key, field_info in INTERFACE_FIELDS.items():
            value = getattr(config, key, None)
            if value is not None:
                parser.set(section, key, format_value(value, field_info['type']))
            else:
                parser.set(section, key, format_value(field_info['default'], field_info['type']))
        
        parser.write()
        
        return {
            "success": True,
            "interface": next_num,
            "section": section,
            "backup": backup_path
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/interfaces/{num}")
def update_interface(num: int, config: InterfaceUpdate):
    if num < 1 or num > 9:
        raise HTTPException(status_code=400, detail="Interface number must be 1-9")
    
    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()
        section = get_interface_section_name(num)
        
        if section not in parser.sections:
            raise HTTPException(status_code=404, detail=f"Interface {num} not configured")
        
        backup_path = create_backup()
        
        fields = PRIMARY_INTERFACE_FIELDS if num == 1 else INTERFACE_FIELDS
        for key, field_info in fields.items():
            value = getattr(config, key, None)
            if value is not None:
                parser.set(section, key, format_value(value, field_info['type']))
        
        parser.write()
        
        return {
            "success": True,
            "interface": num,
            "backup": backup_path
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/interfaces/{num}")
def delete_interface(num: int):
    if num == 1:
        raise HTTPException(status_code=400, detail="Cannot delete primary interface")
    if num < 2 or num > 9:
        raise HTTPException(status_code=400, detail="Interface number must be 2-9")
    
    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()
        section = get_interface_section_name(num)
        
        if section not in parser.sections:
            raise HTTPException(status_code=404, detail=f"Interface {num} not configured")
        
        backup_path = create_backup()
        
        parser.remove_section(section)
        parser.write()
        
        return {
            "success": True,
            "deleted": num,
            "backup": backup_path
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Config endpoints

@app.get("/api/config")
def get_config():
    try:
        parser = ConfigParser(CONFIG_PATH)
        raw_config = parser.read()

        config = {}
        for section, fields in raw_config.items():
            config[section] = {}
            schema_section = CONFIG_SCHEMA.get(section, {}).get('fields', {})

            for key, value in fields.items():
                field_schema = schema_section.get(key, {})
                field_type = field_schema.get('type', 'string')
                config[section][key] = parse_value(value, field_type)

        return {"config": config, "path": CONFIG_PATH}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Config file not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/backup")
def backup_config():
    try:
        backup_path = create_backup()
        return {"success": True, "path": backup_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config/backups")
def list_backups():
    try:
        if not os.path.exists(BACKUP_DIR):
            return {"backups": []}

        backups = []
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if f.startswith("config-") and f.endswith(".ini"):
                path = os.path.join(BACKUP_DIR, f)
                stat = os.stat(path)
                backups.append({
                    "filename": f,
                    "path": path,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })

        return {"backups": backups}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/validate")
def validate_config(config: Dict[str, Dict[str, Any]]):
    errors = []
    warnings = []

    for section, fields in config.items():
        if section not in CONFIG_SCHEMA:
            warnings.append(f"Unknown section: {section}")
            continue

        schema_section = CONFIG_SCHEMA[section].get('fields', {})

        for key, value in fields.items():
            if key not in schema_section:
                warnings.append(f"Unknown key: {section}.{key}")
                continue

            field_schema = schema_section[key]
            field_type = field_schema.get('type', 'string')

            if field_type == 'integer':
                try:
                    int(value) if not isinstance(value, int) else value
                except (ValueError, TypeError):
                    errors.append(f"{section}.{key}: Expected integer, got {type(value).__name__}")

            elif field_type == 'float':
                try:
                    float(value) if not isinstance(value, (int, float)) else value
                except (ValueError, TypeError):
                    errors.append(f"{section}.{key}: Expected float, got {type(value).__name__}")

            elif field_type == 'boolean':
                if not isinstance(value, bool) and str(value).lower() not in ('true', 'false', '1', '0', 'yes', 'no'):
                    errors.append(f"{section}.{key}: Expected boolean, got {value}")

            elif field_type == 'enum':
                options = field_schema.get('options', [])
                if value not in options:
                    errors.append(f"{section}.{key}: Value '{value}' not in allowed options: {options}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }


@app.post("/api/config/restore/{filename}")
def restore_backup(filename: str):
    try:
        backup_path = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(backup_path):
            raise HTTPException(status_code=404, detail="Backup not found")

        create_backup()

        shutil.copy2(backup_path, CONFIG_PATH)

        return {"success": True, "restored_from": backup_path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config/{section}")
def get_section(section: str):
    try:
        parser = ConfigParser(CONFIG_PATH)
        raw_config = parser.read()

        if section not in raw_config:
            raise HTTPException(status_code=404, detail=f"Section '{section}' not found")

        schema_section = CONFIG_SCHEMA.get(section, {}).get('fields', {})
        config = {}

        for key, value in raw_config[section].items():
            field_schema = schema_section.get(key, {})
            field_type = field_schema.get('type', 'string')
            config[key] = parse_value(value, field_type)

        return {"section": section, "config": config}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/config/{section}")
def update_section(section: str, updates: Dict[str, Any]):
    try:
        backup_path = create_backup()

        parser = ConfigParser(CONFIG_PATH)
        parser.read()

        if section not in parser.sections:
            raise HTTPException(status_code=404, detail=f"Section '{section}' not found")

        schema_section = CONFIG_SCHEMA.get(section, {}).get('fields', {})

        for key, value in updates.items():
            field_schema = schema_section.get(key, {})
            field_type = field_schema.get('type', 'string')
            formatted_value = format_value(value, field_type)
            parser.set(section, key, formatted_value)

        parser.write()

        return {
            "success": True,
            "section": section,
            "backup": backup_path,
            "updated_keys": list(updates.keys())
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/config")
def update_config(bulk: BulkConfigUpdate):
    try:
        backup_path = create_backup()

        parser = ConfigParser(CONFIG_PATH)
        parser.read()

        updated = []

        for section, updates in bulk.updates.items():
            if section not in parser.sections:
                continue

            schema_section = CONFIG_SCHEMA.get(section, {}).get('fields', {})

            for key, value in updates.items():
                field_schema = schema_section.get(key, {})
                field_type = field_schema.get('type', 'string')
                formatted_value = format_value(value, field_type)
                parser.set(section, key, formatted_value)
                updated.append(f"{section}.{key}")

        parser.write()

        return {
            "success": True,
            "backup": backup_path,
            "updated": updated
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/service/status")
def get_service_status():
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", SERVICE_NAME],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            status = result.stdout.strip()
        else:
            result = subprocess.run(
                ["systemctl", "is-active", SERVICE_NAME],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                status = result.stdout.strip()
            else:
                status = "unknown"

        return {
            "status": status,
            "service_name": SERVICE_NAME
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/service/restart")
def restart_service():
    try:
        result = subprocess.run(
            ["docker", "restart", SERVICE_NAME],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", SERVICE_NAME],
                capture_output=True,
                text=True
            )

        if result.returncode == 0:
            return {"success": True, "message": "Service restart initiated"}
        else:
            return {
                "success": False,
                "error": result.stderr or "Unknown error"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def load_nodedb() -> Dict:
    """Load node database exported by the bot process."""
    try:
        with open(NODEDB_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


@app.get("/api/interfaces/{num}/nodeinfo")
def get_interface_node_info(num: int):
    """Get node info for a specific interface from the exported nodedb."""
    if num < 1 or num > 9:
        raise HTTPException(status_code=400, detail="Interface number must be 1-9")

    nodedb = load_nodedb()
    if not nodedb:
        raise HTTPException(status_code=503, detail="Node database not yet available (bot may still be starting)")

    key = str(num)
    interfaces = nodedb.get("interfaces", {})
    if key not in interfaces:
        raise HTTPException(status_code=404, detail=f"Interface {num} not found in node database")

    return {"interface": num, "success": True, "nodeInfo": interfaces[key].get("myNodeInfo", {}), "channels": interfaces[key].get("channels", [])}


@app.get("/api/nodeinfo")
def get_all_node_info():
    """Get node info from all interfaces via the exported nodedb."""
    nodedb = load_nodedb()
    if not nodedb:
        raise HTTPException(status_code=503, detail="Node database not yet available (bot may still be starting)")

    results = {}
    for iface_key, iface_data in nodedb.get("interfaces", {}).items():
        results[int(iface_key)] = {
            "success": True,
            "nodeInfo": iface_data.get("myNodeInfo", {}),
            "channels": iface_data.get("channels", []),
        }

    return {"nodeInfo": results}


@app.get("/api/nodes")
def get_all_nodes():
    """Get all mesh nodes from the exported nodedb."""
    nodedb = load_nodedb()
    if not nodedb:
        raise HTTPException(status_code=503, detail="Node database not yet available (bot may still be starting)")

    nodes_list = nodedb.get("nodes", [])
    # Sort by lastHeard descending
    nodes_list.sort(key=lambda x: x.get('lastHeard') or 0, reverse=True)

    return {
        "nodes": nodes_list,
        "total": len(nodes_list),
        "exported_at": nodedb.get("exported_at"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
