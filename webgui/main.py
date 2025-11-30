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
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from config_schema import CONFIG_SCHEMA, SECTION_ORDER, INTERFACE_FIELDS, PRIMARY_INTERFACE_FIELDS

# Meshtastic imports for node info
try:
    import meshtastic
    import meshtastic.tcp_interface
    import meshtastic.serial_interface
    import meshtastic.ble_interface
    MESHTASTIC_AVAILABLE = True
except ImportError:
    MESHTASTIC_AVAILABLE = False

# Configuration
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/opt/meshing-around/config.ini")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/opt/meshing-around/webgui/backups")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "meshbot")
SCHEDULES_PATH = Path(__file__).parent / "schedules.json"
SCHEDULER_LOG_PATH = Path(__file__).parent / "scheduler_log.json"
MESHBOT_LOG_PATH = os.environ.get("MESHBOT_LOG_PATH", "/opt/meshing-around/logs/meshbot.log")
LOG_ARCHIVE_DIR = os.environ.get("LOG_ARCHIVE_DIR", "/app/log_archives")
BBS_PEERS_PATH = os.environ.get("BBS_PEERS_PATH", "/app/data/bbs_peers.json")
LEADERBOARD_PATH = os.environ.get("LEADERBOARD_PATH", "/app/data/leaderboard.pkl")
MAX_LOG_ENTRIES = 100  # Keep last 100 log entries
LOG_ARCHIVE_INTERVAL = 3600  # Archive logs every hour
LOG_RETENTION_DAYS = 30  # Keep archives for 30 days

# Lifespan handler for background tasks
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create initial archive and start background task
    global archive_task
    ensure_archive_dir_startup()
    archive_task = asyncio.create_task(periodic_archive_task())
    yield
    # Shutdown: cancel background task
    if archive_task:
        archive_task.cancel()

def ensure_archive_dir_startup():
    """Create archive directory on startup."""
    Path(LOG_ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)

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

app = FastAPI(
    title="MeshBOT Config Manager",
    description="Web GUI for managing meshing-around configuration",
    version="1.0.0",
    lifespan=lifespan
)

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except IOError:
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

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except IOError:
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
async def root():
    html_path = Path(__file__).parent / "templates" / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse("<h1>MeshBOT Config Manager</h1><p>Loading...</p>")


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
async def get_schedules():
    """Get all custom schedules"""
    schedules = load_schedules()
    return {"schedules": schedules}


@app.get("/api/schedules/{schedule_id}")
async def get_schedule(schedule_id: int):
    """Get a specific schedule"""
    schedules = load_schedules()
    for s in schedules:
        if s.get('id') == schedule_id:
            return {"schedule": s}
    raise HTTPException(status_code=404, detail="Schedule not found")


@app.post("/api/schedules")
async def create_schedule(schedule: ScheduleItem):
    """Create a new schedule"""
    schedules = load_schedules()
    new_schedule = schedule.dict()
    new_schedule['id'] = get_next_schedule_id(schedules)
    schedules.append(new_schedule)
    save_schedules(schedules)
    return {"success": True, "schedule": new_schedule}


@app.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, schedule: ScheduleItem):
    """Update an existing schedule"""
    schedules = load_schedules()
    for i, s in enumerate(schedules):
        if s.get('id') == schedule_id:
            updated = schedule.dict()
            updated['id'] = schedule_id
            schedules[i] = updated
            save_schedules(schedules)
            return {"success": True, "schedule": updated}
    raise HTTPException(status_code=404, detail="Schedule not found")


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int):
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
async def get_scheduler_log():
    """Get activity log from meshbot logs (channel broadcasts only, no DMs)"""
    entries = get_activity_log()
    return {"entries": entries}


@app.post("/api/scheduler/log")
async def add_log_entry(entry: SchedulerLogEntry):
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
async def delete_scheduler_log():
    """Clear all scheduler log entries"""
    clear_scheduler_log()
    return {"success": True, "message": "Scheduler log cleared"}

@app.post("/api/scheduler/sync")
async def sync_schedules_to_bot():
    """
    Sync schedules from schedules.json to custom_scheduler.py.
    This generates Python code from the JSON schedules and writes it to the custom scheduler file.
    """
    import os
    
    try:
        # Read schedules from JSON
        schedules = load_schedules()
        
        # Path to custom_scheduler.py (relative to webgui, go up one level to modules)
        custom_scheduler_path = Path("/app/modules/custom_scheduler.py")
        
        if not custom_scheduler_path.exists():
            raise HTTPException(status_code=404, detail="custom_scheduler.py not found")
        
        # Read the existing file
        with open(custom_scheduler_path, 'r') as f:
            content = f.read()
        
        # Find the marker where we insert generated schedules
        # We'll look for the try block and insert after the function definitions
        
        # Generate schedule code from JSON
        generated_lines = []
        generated_lines.append("        # === AUTO-GENERATED SCHEDULES FROM WEBGUI ===")
        generated_lines.append("        # Do not edit below this line - changes will be overwritten by WebGUI sync")
        
        for sched in schedules:
            if not sched.get('enabled', False):
                continue
                
            name = sched.get('name', 'Unnamed')
            freq = sched.get('frequency', 'day')
            time_val = sched.get('time', '08:00')
            interval = sched.get('interval', 1)
            message = sched.get('message', '').replace('"', '\\"')
            action = sched.get('action', 'message')
            channel = sched.get('channel', 0)
            interface = sched.get('interface', 1)
            day = sched.get('day')
            
            # Build the schedule line based on frequency
            if freq == 'minutes':
                sched_call = f"schedule.every({interval}).minutes"
            elif freq == 'hours':
                sched_call = f"schedule.every({interval}).hours"
            elif freq == 'day':
                sched_call = f'schedule.every().day.at("{time_val}")'
            elif freq == 'days':
                sched_call = f'schedule.every({interval}).days.at("{time_val}")'
            elif freq == 'week':
                if day:
                    sched_call = f'schedule.every().{day.lower()}.at("{time_val}")'
                else:
                    sched_call = f'schedule.every().week.at("{time_val}")'
            else:
                sched_call = f'schedule.every().day.at("{time_val}")'
            
            # Build the action
            if action == 'message':
                do_action = f'lambda: send_message("{message}", {channel}, 0, {interface})'
            elif action == 'weather':
                do_action = f'lambda: send_message(handle_wxc(0, {interface}, "wx"), {channel}, 0, {interface})'
            elif action == 'joke':
                do_action = f'lambda: send_message(tell_joke(), {channel}, 0, {interface})'
            else:
                do_action = f'lambda: send_message("{message}", {channel}, 0, {interface})'
            
            generated_lines.append(f'        logger.debug("System: Custom Scheduler: {name}")')
            generated_lines.append(f'        {sched_call}.do({do_action})')
        
        generated_lines.append("        # === END AUTO-GENERATED SCHEDULES ===")
        generated_code = "\n".join(generated_lines)
        
        # Remove any existing auto-generated section
        import re
        pattern = r'        # === AUTO-GENERATED SCHEDULES FROM WEBGUI ===.*?# === END AUTO-GENERATED SCHEDULES ==='
        content = re.sub(pattern, '', content, flags=re.DOTALL)
        
        # Insert before "except Exception as e:" in the setup_custom_schedules function
        # Find the last occurrence of "except Exception as e:" after setup_custom_schedules
        insert_marker = "    except Exception as e:"
        if insert_marker in content:
            content = content.replace(insert_marker, generated_code + "\n\n    except Exception as e:", 1)
        else:
            raise HTTPException(status_code=500, detail="Could not find insertion point in custom_scheduler.py")
        
        # Write the updated file
        with open(custom_scheduler_path, 'w') as f:
            f.write(content)
        
        return {
            "success": True, 
            "message": f"Synced {len([s for s in schedules if s.get('enabled')])} enabled schedules to custom_scheduler.py",
            "note": "Restart the mesh_bot service to apply changes"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync schedules: {str(e)}")




# Log viewer endpoints

@app.get("/api/logs")
async def get_logs(
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
async def list_archives():
    """Get list of available log archives."""
    archives = get_log_archives()
    return {
        "archives": archives,
        "total": len(archives),
        "retention_days": LOG_RETENTION_DAYS
    }


@app.post("/api/logs/archive")
async def create_archive():
    """Create a new archive of the current log."""
    filename = archive_current_log()
    if filename:
        return {"success": True, "filename": filename}
    raise HTTPException(status_code=500, detail="Failed to create archive")


@app.get("/api/logs/archives/{filename}")
async def get_archive_content(filename: str, lines: int = 1000):
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
async def delete_archive(filename: str):
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
async def get_packets(since: Optional[str] = None):
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
async def clear_packets():
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

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except IOError:
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
async def get_bbs_peers(refresh: bool = False):
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
async def get_bbs_events(limit: int = 50):
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
async def clear_bbs_peers():
    """Clear BBS peers tracking data."""
    try:
        if os.path.exists(BBS_PEERS_PATH):
            os.remove(BBS_PEERS_PATH)
        return {"success": True, "message": "BBS peers data cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Leaderboard endpoint

@app.get("/api/leaderboard")
async def get_leaderboard():
    """Get mesh leaderboard data from MeshBOT."""
    try:
        if not os.path.exists(LEADERBOARD_PATH):
            return {"leaderboard": {}, "error": "Leaderboard data not yet available"}

        with open(LEADERBOARD_PATH, 'rb') as f:
            data = pickle.load(f)

        # Convert to JSON-serializable format and filter useful entries
        leaderboard = {}

        # Define which metrics to expose and their display info
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
            if key in data and data[key].get('nodeID'):
                entry = data[key]
                value = entry.get('value', 0)

                # Format uptime specially
                if key == 'longestUptime' and value > 0:
                    days = int(value // 86400)
                    hours = int((value % 86400) // 3600)
                    formatted_value = f"{days}d {hours}h" if days > 0 else f"{hours}h"
                    unit = ''  # Already included in formatted_value
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

        return {"leaderboard": leaderboard}
    except Exception as e:
        return {"leaderboard": {}, "error": str(e)}


# Interface endpoints

@app.get("/api/interfaces")
async def get_interfaces():
    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()
        interfaces = get_all_interfaces(parser)
        return {"interfaces": interfaces}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/interfaces/{num}")
async def get_interface(num: int):
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
async def add_interface(config: InterfaceUpdate):
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
async def update_interface(num: int, config: InterfaceUpdate):
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
async def delete_interface(num: int):
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
async def get_config():
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
async def backup_config():
    try:
        backup_path = create_backup()
        return {"success": True, "path": backup_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config/backups")
async def list_backups():
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
async def validate_config(config: Dict[str, Dict[str, Any]]):
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
async def restore_backup(filename: str):
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
async def get_section(section: str):
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
async def update_section(section: str, updates: Dict[str, Any]):
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
async def update_config(bulk: BulkConfigUpdate):
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
async def get_service_status():
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
async def restart_service():
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


# Node info helper function
def get_node_info_from_interface(interface_config: Dict) -> Dict:
    """Connect to a Meshtastic interface and get node info"""
    if not MESHTASTIC_AVAILABLE:
        return {"error": "Meshtastic library not available"}

    iface_type = interface_config.get('type', 'serial')
    interface = None

    try:
        if iface_type == 'tcp':
            hostname = interface_config.get('hostname', '')
            if not hostname:
                return {"error": "No hostname configured"}
            # Parse hostname:port
            if ':' in hostname:
                host, port = hostname.rsplit(':', 1)
                interface = meshtastic.tcp_interface.TCPInterface(hostname=host, portNumber=int(port))
            else:
                interface = meshtastic.tcp_interface.TCPInterface(hostname=hostname)

        elif iface_type == 'serial':
            port = interface_config.get('port', '')
            if not port:
                return {"error": "No serial port configured"}
            interface = meshtastic.serial_interface.SerialInterface(port)

        elif iface_type == 'ble':
            mac = interface_config.get('mac', '')
            if not mac:
                return {"error": "No BLE MAC address configured"}
            interface = meshtastic.ble_interface.BLEInterface(mac)

        else:
            return {"error": f"Unknown interface type: {iface_type}"}

        # Get my node info
        my_info = interface.getMyNodeInfo()

        # Extract useful fields
        node_info = {
            "num": my_info.get('num'),
            "user": my_info.get('user', {}),
            "position": my_info.get('position', {}),
            "deviceMetrics": my_info.get('deviceMetrics', {}),
        }

        # Get human-readable info from user
        user = node_info.get('user', {})
        node_info['shortName'] = user.get('shortName', 'Unknown')
        node_info['longName'] = user.get('longName', 'Unknown')
        node_info['hwModel'] = user.get('hwModel', 'Unknown')
        node_info['nodeId'] = user.get('id', '')

        # Get device metrics if available
        metrics = node_info.get('deviceMetrics', {})
        node_info['batteryLevel'] = metrics.get('batteryLevel')
        node_info['voltage'] = metrics.get('voltage')
        node_info['channelUtilization'] = metrics.get('channelUtilization')
        node_info['airUtilTx'] = metrics.get('airUtilTx')

        # Get channel info
        channels = []
        try:
            local_node = interface.getNode('^local')
            # Try newer API first
            try:
                ch_list = local_node.get_channels_with_hash()
                if ch_list:
                    for ch in ch_list:
                        channels.append({
                            "index": ch.get('index'),
                            "name": ch.get('name', ''),
                            "role": ch.get('role', 'DISABLED'),
                        })
            except AttributeError:
                # Fallback to localConfig channels
                if hasattr(local_node, 'localConfig') and local_node.localConfig:
                    for i, ch in enumerate(local_node.channels):
                        if ch and hasattr(ch, 'role'):
                            role_str = str(ch.role) if ch.role else 'DISABLED'
                            # Only include active channels
                            if 'DISABLED' not in role_str.upper():
                                channels.append({
                                    "index": i,
                                    "name": ch.settings.name if hasattr(ch, 'settings') and ch.settings else f"Channel {i}",
                                    "role": role_str,
                                })
        except Exception as ch_err:
            # Channel fetch failed, but we still have node info
            channels = [{"error": str(ch_err)}]

        node_info['channels'] = channels

        return {"success": True, "nodeInfo": node_info}

    except Exception as e:
        return {"error": str(e)}
    finally:
        if interface:
            try:
                interface.close()
            except:
                pass


@app.get("/api/interfaces/{num}/nodeinfo")
async def get_interface_node_info(num: int):
    """Get node info from a connected Meshtastic interface"""
    if num < 1 or num > 9:
        raise HTTPException(status_code=400, detail="Interface number must be 1-9")

    if not MESHTASTIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Meshtastic library not available")

    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()
        section = get_interface_section_name(num)

        if section not in parser.sections:
            raise HTTPException(status_code=404, detail=f"Interface {num} not configured")

        # Get interface config
        fields = PRIMARY_INTERFACE_FIELDS if num == 1 else INTERFACE_FIELDS
        interface_config = {}
        for key, field_info in fields.items():
            raw_value = parser.sections[section].get(key, '')
            if raw_value:
                interface_config[key] = parse_value(raw_value, field_info['type'])
            else:
                interface_config[key] = field_info.get('default', '')

        # Check if interface is enabled (non-primary interfaces)
        if num != 1 and not interface_config.get('enabled', False):
            return {"interface": num, "nodeInfo": None, "message": "Interface is disabled"}

        # Get node info
        result = get_node_info_from_interface(interface_config)

        return {"interface": num, **result}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/nodeinfo")
async def get_all_node_info():
    """Get node info from all configured and enabled interfaces"""
    if not MESHTASTIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Meshtastic library not available")

    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()

        results = {}

        for i in range(1, 10):
            section = get_interface_section_name(i)
            if section not in parser.sections:
                continue

            fields = PRIMARY_INTERFACE_FIELDS if i == 1 else INTERFACE_FIELDS
            interface_config = {}
            for key, field_info in fields.items():
                raw_value = parser.sections[section].get(key, '')
                if raw_value:
                    interface_config[key] = parse_value(raw_value, field_info['type'])
                else:
                    interface_config[key] = field_info.get('default', '')

            # Skip disabled non-primary interfaces
            if i != 1 and not interface_config.get('enabled', False):
                results[i] = {"enabled": False}
                continue

            result = get_node_info_from_interface(interface_config)
            results[i] = result

        return {"nodeInfo": results}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/nodes")
async def get_all_nodes():
    """Get all nodes seen by the mesh from the primary interface"""
    if not MESHTASTIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Meshtastic library not available")

    try:
        parser = ConfigParser(CONFIG_PATH)
        parser.read()

        # Get primary interface config
        section = get_interface_section_name(1)
        if section not in parser.sections:
            raise HTTPException(status_code=404, detail="No primary interface configured")

        interface_config = {}
        for key, field_info in PRIMARY_INTERFACE_FIELDS.items():
            raw_value = parser.sections[section].get(key, '')
            if raw_value:
                interface_config[key] = parse_value(raw_value, field_info['type'])
            else:
                interface_config[key] = field_info.get('default', '')

        iface_type = interface_config.get('type', 'serial')
        interface = None

        try:
            if iface_type == 'tcp':
                hostname = interface_config.get('hostname', '')
                if not hostname:
                    raise HTTPException(status_code=400, detail="No hostname configured")
                if ':' in hostname:
                    host, port = hostname.rsplit(':', 1)
                    interface = meshtastic.tcp_interface.TCPInterface(hostname=host, portNumber=int(port))
                else:
                    interface = meshtastic.tcp_interface.TCPInterface(hostname=hostname)
            elif iface_type == 'serial':
                port = interface_config.get('port', '')
                if not port:
                    raise HTTPException(status_code=400, detail="No serial port configured")
                interface = meshtastic.serial_interface.SerialInterface(port)
            elif iface_type == 'ble':
                mac = interface_config.get('mac', '')
                if not mac:
                    raise HTTPException(status_code=400, detail="No BLE MAC configured")
                interface = meshtastic.ble_interface.BLEInterface(mac)
            else:
                raise HTTPException(status_code=400, detail=f"Unknown interface type: {iface_type}")

            # Get all nodes from the interface
            nodes_list = []
            for node_id, node_data in interface.nodes.items():
                user = node_data.get('user', {})
                position = node_data.get('position', {})
                device_metrics = node_data.get('deviceMetrics', {})

                node_entry = {
                    'num': node_data.get('num'),
                    'nodeId': user.get('id', f"!{node_data.get('num', 0):08x}"),
                    'shortName': user.get('shortName', ''),
                    'longName': user.get('longName', ''),
                    'hwModel': user.get('hwModel', 'UNKNOWN'),
                    'role': user.get('role', 'CLIENT'),
                    'lastHeard': node_data.get('lastHeard'),
                    'snr': node_data.get('snr'),
                    'hopsAway': node_data.get('hopsAway', 0),
                    'position': {
                        'latitude': position.get('latitude'),
                        'longitude': position.get('longitude'),
                        'altitude': position.get('altitude'),
                    } if position else None,
                    'batteryLevel': device_metrics.get('batteryLevel'),
                    'voltage': device_metrics.get('voltage'),
                    'channelUtilization': device_metrics.get('channelUtilization'),
                    'airUtilTx': device_metrics.get('airUtilTx'),
                }
                nodes_list.append(node_entry)

            # Sort by lastHeard descending
            nodes_list.sort(key=lambda x: x.get('lastHeard') or 0, reverse=True)

            return {
                "nodes": nodes_list,
                "total": len(nodes_list)
            }

        finally:
            if interface:
                interface.close()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
