# Meshing-Around WebGUI

Web-based configuration and monitoring interface for the Meshing-Around Meshtastic BBS bot.

## Upstream Relationship

This WebGUI is built on top of [SpudGunMan/meshing-around](https://github.com/spudgunman/meshing-around).

**Current status: NOT a standalone addon.**

The WebGUI requires modifications to the core `modules/system.py` file, meaning you cannot install vanilla meshing-around and then drop in the WebGUI as an addon.

## Modifications to Upstream

### 1. `modules/system.py` - `saveLeaderboard()` function

Adds node names (shortName/longName) to leaderboard entries when saving to pickle file. This allows the WebGUI to display human-readable names instead of just hex node IDs.

```python
# Added: resolve node names before saving
short_name = get_name_from_number(entry['nodeID'], 'short', 1)
long_name = get_name_from_number(entry['nodeID'], 'long', 1)
entry['shortName'] = short_name if short_name else None
entry['longName'] = long_name if long_name else None
```

**Impact:** Non-breaking addition. Leaderboard data gains optional name fields.

### 2. `modules/system.py` - `retry_interface()` function

Fixes TCP hostname:port parsing bug. Original code didn't properly handle `hostname:port` format for TCP interfaces.

```python
# Added: parse host:port format
if isinstance(host, str) and ':' in host:
    maybe_host, maybe_port = host.rsplit(':', 1)
    if maybe_port.isdigit():
        host = maybe_host
        port = int(maybe_port)
```

**Impact:** Bug fix. Allows TCP connections with custom ports.

## Making This a Standalone Addon

To make the WebGUI installable on vanilla meshing-around:

| Option | Description |
|--------|-------------|
| **PR upstream** | Submit the system.py changes to SpudGunMan. If merged, WebGUI becomes a drop-in addon. |
| **Remove dependency** | Modify WebGUI to work without system.py changes. Leaderboard would show hex IDs only. |
| **Auto-patch** | Have addon installation script patch system.py. Fragile, breaks on upstream updates. |

**Recommended approach:** Submit PR upstream for the TCP fix (legitimate bug fix) and leaderboard names (minor enhancement). Once merged, the `webgui/` directory becomes a pure addon.

## Features

- **Dashboard** - Service status, quick stats, leaderboard
- **Radio Connections** - Configure up to 9 Meshtastic interfaces (Serial/TCP/BLE)
- **Scheduler** - Manage scheduled broadcasts and messages
- **Mesh Nodes** - View all nodes seen by the mesh network
- **BBS Network** - Track BBS peer synchronization
- **Packet Monitor** - Real-time packet inspection (requires DEBUGpacket)
- **System Logs** - Filterable log viewer with auto-refresh
- **Configuration** - All config.ini sections with validation

## Running

The WebGUI runs as a FastAPI application, typically via Docker:

```bash
docker-compose up -d meshing-webgui
```

Default port: 8085

## Files

```
webgui/
├── main.py              # FastAPI application
├── config_schema.py     # Configuration field definitions
├── templates/
│   └── index.html       # Single-page application (Tailwind CSS)
├── schedules.json       # Scheduler data
└── README.md            # This file
```
