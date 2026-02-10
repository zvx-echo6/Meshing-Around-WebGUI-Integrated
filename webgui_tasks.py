"""
webgui_tasks.py — Fork-only async tasks for WebGUI integration.
These run inside the mesh_bot process to export data for WebGUI consumption.
DO NOT add to upstream meshing-around — this is fork-specific.
"""

import asyncio
import json
import os
from datetime import datetime
from modules.log import logger
import modules.system as sys_mod


# === NodeDB Export ===

NODEDB_EXPORT_PATH = os.environ.get("NODEDB_EXPORT_PATH", "/app/data/nodedb.json")
NODEDB_EXPORT_INTERVAL = int(os.environ.get("NODEDB_EXPORT_INTERVAL", "30"))


def export_nodedb():
    """
    Export node data from all active interfaces to JSON for WebGUI.
    Reads interface objects directly from modules.system — no new connections.
    """
    try:
        data = {
            "updated_at": datetime.now().isoformat(),
            "interfaces": {},
            "nodes": [],
        }

        seen_nodes = set()

        for i in range(1, 10):
            iface = getattr(sys_mod, f'interface{i}', None)
            enabled = getattr(sys_mod, f'interface{i}_enabled', False)

            if not iface or not enabled:
                continue

            iface_data = {
                "enabled": True,
                "type": getattr(sys_mod, f'interface{i}_type', 'unknown'),
            }

            try:
                my_info = iface.getMyNodeInfo()
                user = my_info.get('user', {})
                position = my_info.get('position', {})
                device_metrics = my_info.get('deviceMetrics', {})

                iface_data["myNodeInfo"] = {
                    "num": my_info.get('num'),
                    "shortName": user.get('shortName', 'Unknown'),
                    "longName": user.get('longName', 'Unknown'),
                    "hwModel": user.get('hwModel', 'Unknown'),
                    "nodeId": user.get('id', ''),
                    "batteryLevel": device_metrics.get('batteryLevel'),
                    "voltage": device_metrics.get('voltage'),
                    "channelUtilization": device_metrics.get('channelUtilization'),
                    "airUtilTx": device_metrics.get('airUtilTx'),
                    "position": {
                        "latitude": position.get('latitude'),
                        "longitude": position.get('longitude'),
                        "altitude": position.get('altitude'),
                    } if position else None,
                }

                # Channel info
                channels = []
                try:
                    local_node = iface.getNode('^local')
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
                        if hasattr(local_node, 'localConfig') and local_node.localConfig:
                            for idx, ch in enumerate(local_node.channels):
                                if ch and hasattr(ch, 'role'):
                                    role_str = str(ch.role) if ch.role else 'DISABLED'
                                    if 'DISABLED' not in role_str.upper():
                                        channels.append({
                                            "index": idx,
                                            "name": ch.settings.name if hasattr(ch, 'settings') and ch.settings else f"Channel {idx}",
                                            "role": role_str,
                                        })
                except Exception:
                    pass

                iface_data["myNodeInfo"]["channels"] = channels

            except Exception as e:
                iface_data["myNodeInfo"] = {"error": str(e)}

            data["interfaces"][str(i)] = iface_data

            # All nodes seen by this interface
            try:
                if iface.nodes:
                    for node_id, node_data in iface.nodes.items():
                        node_num = node_data.get('num', 0)
                        if node_num in seen_nodes:
                            continue
                        seen_nodes.add(node_num)

                        user = node_data.get('user', {})
                        position = node_data.get('position', {})
                        device_metrics = node_data.get('deviceMetrics', {})

                        data["nodes"].append({
                            "num": node_num,
                            "nodeId": user.get('id', f"!{node_num:08x}"),
                            "shortName": user.get('shortName', ''),
                            "longName": user.get('longName', ''),
                            "hwModel": user.get('hwModel', 'UNKNOWN'),
                            "role": user.get('role', 'CLIENT'),
                            "lastHeard": node_data.get('lastHeard'),
                            "snr": node_data.get('snr'),
                            "hopsAway": node_data.get('hopsAway', 0),
                            "position": {
                                "latitude": position.get('latitude'),
                                "longitude": position.get('longitude'),
                                "altitude": position.get('altitude'),
                            } if position else None,
                            "batteryLevel": device_metrics.get('batteryLevel'),
                            "voltage": device_metrics.get('voltage'),
                            "channelUtilization": device_metrics.get('channelUtilization'),
                            "airUtilTx": device_metrics.get('airUtilTx'),
                            "interface": i,
                        })
            except Exception:
                pass

        data["nodes"].sort(key=lambda x: x.get('lastHeard') or 0, reverse=True)

        # Atomic write
        os.makedirs(os.path.dirname(NODEDB_EXPORT_PATH), exist_ok=True)
        temp_path = NODEDB_EXPORT_PATH + '.tmp'
        with open(temp_path, 'w') as f:
            json.dump(data, f)
        os.replace(temp_path, NODEDB_EXPORT_PATH)

    except Exception as e:
        logger.debug(f"System: NodeDB export error: {e}")


async def nodedb_export_loop():
    """Periodically export node database for WebGUI."""
    await asyncio.sleep(10)  # Wait for interfaces to initialize
    while True:
        try:
            export_nodedb()
        except Exception as e:
            logger.debug(f"System: NodeDB export error: {e}")
        await asyncio.sleep(NODEDB_EXPORT_INTERVAL)


# === Leaderboard Export ===

LEADERBOARD_EXPORT_PATH = os.environ.get("LEADERBOARD_EXPORT_PATH", "/app/data/leaderboard_webgui.json")
LEADERBOARD_EXPORT_INTERVAL = int(os.environ.get("LEADERBOARD_EXPORT_INTERVAL", "60"))  # seconds


def export_leaderboard():
    """
    Export leaderboard data as JSON with node name resolution for WebGUI.
    Reads meshLeaderboard dict from modules.system and enriches with names.
    """
    try:
        leaderboard = getattr(sys_mod, 'meshLeaderboard', {})
        if not leaderboard:
            return

        # Deep copy so we don't mutate the live dict
        import copy
        export_data = copy.deepcopy(leaderboard)

        # Enrich entries with node names
        get_name = getattr(sys_mod, 'get_name_from_number', None)
        if get_name:
            for key, entry in export_data.items():
                if isinstance(entry, dict) and entry.get('nodeID'):
                    try:
                        entry['shortName'] = get_name(entry['nodeID'], 'short', 1) or None
                        entry['longName'] = get_name(entry['nodeID'], 'long', 1) or None
                    except Exception:
                        entry['shortName'] = None
                        entry['longName'] = None

        # Convert any non-serializable values
        def make_serializable(obj):
            if isinstance(obj, dict):
                return {k: make_serializable(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [make_serializable(i) for i in obj]
            elif isinstance(obj, (int, float, str, bool, type(None))):
                return obj
            else:
                return str(obj)

        data = {
            "updated_at": datetime.now().isoformat(),
            "leaderboard": make_serializable(export_data),
        }

        # Atomic write
        os.makedirs(os.path.dirname(LEADERBOARD_EXPORT_PATH), exist_ok=True)
        temp_path = LEADERBOARD_EXPORT_PATH + '.tmp'
        with open(temp_path, 'w') as f:
            json.dump(data, f)
        os.replace(temp_path, LEADERBOARD_EXPORT_PATH)

    except Exception as e:
        logger.debug(f"System: Leaderboard export error: {e}")


async def leaderboard_export_loop():
    """Periodically export leaderboard for WebGUI."""
    await asyncio.sleep(15)  # Wait for bot to initialize and populate some data
    while True:
        try:
            export_leaderboard()
        except Exception as e:
            logger.debug(f"System: Leaderboard export error: {e}")
        await asyncio.sleep(LEADERBOARD_EXPORT_INTERVAL)


# === WebGUI Schedule Reload ===

import schedule as schedule_lib

SCHEDULES_PATH = os.environ.get("SCHEDULES_PATH", "/app/data/schedules.json")
SCHEDULE_RELOAD_INTERVAL = int(os.environ.get("SCHEDULE_RELOAD_INTERVAL", "15"))

_last_schedules_mtime = 0.0


def _safe_int(val, default=0):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_webgui_schedules(send_message_func, tell_joke_func, handle_wxc_func,
                          default_channel, default_interface):
    """
    Read schedules.json and register enabled schedules with the schedule library.
    Clears previously loaded WebGUI jobs before reloading (tagged with webgui_managed).
    """
    # Clear previous WebGUI-managed jobs
    schedule_lib.jobs = [j for j in schedule_lib.jobs if not getattr(j, 'webgui_managed', False)]

    if not os.path.exists(SCHEDULES_PATH):
        return 0

    try:
        with open(SCHEDULES_PATH, 'r') as f:
            schedules = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"System: Failed to read WebGUI schedules: {e}")
        return 0

    count = 0
    for sched in schedules:
        if not sched.get('enabled', False):
            continue

        name = sched.get('name', 'Unnamed')
        freq = sched.get('frequency', 'day')
        time_val = sched.get('time', '08:00')
        interval = _safe_int(sched.get('interval', 1), 1)
        message = sched.get('message', '')
        action = sched.get('action', 'message')
        channel = _safe_int(sched.get('channel', default_channel), default_channel)
        interface = _safe_int(sched.get('interface', default_interface), default_interface)
        day = sched.get('day')

        try:
            # Build action callable — default args avoid late-binding closure bugs
            if action == 'weather':
                job_func = lambda ch=channel, iface=interface: send_message_func(
                    handle_wxc_func(0, iface, 'wx'), ch, 0, iface
                )
            elif action == 'joke':
                job_func = lambda ch=channel, iface=interface: send_message_func(
                    tell_joke_func(), ch, 0, iface
                )
            else:  # 'message' or default
                job_func = lambda msg=message, ch=channel, iface=interface: send_message_func(
                    msg, ch, 0, iface
                )

            # Build schedule timing
            if freq == 'minutes':
                job = schedule_lib.every(interval).minutes.do(job_func)
            elif freq == 'hours':
                job = schedule_lib.every(interval).hours.do(job_func)
            elif freq == 'day':
                job = schedule_lib.every().day.at(time_val).do(job_func)
            elif freq == 'days':
                job = schedule_lib.every(interval).days.at(time_val).do(job_func)
            elif freq == 'week':
                if day:
                    day_lower = day.lower()
                    day_map = {
                        'monday': schedule_lib.every().monday,
                        'tuesday': schedule_lib.every().tuesday,
                        'wednesday': schedule_lib.every().wednesday,
                        'thursday': schedule_lib.every().thursday,
                        'friday': schedule_lib.every().friday,
                        'saturday': schedule_lib.every().saturday,
                        'sunday': schedule_lib.every().sunday,
                    }
                    sched_day = day_map.get(day_lower)
                    if sched_day:
                        job = sched_day.at(time_val).do(job_func)
                    else:
                        logger.warning(f"System: WebGUI schedule '{name}' has invalid day: {day}")
                        continue
                else:
                    job = schedule_lib.every().week.at(time_val).do(job_func)
            else:
                job = schedule_lib.every().day.at(time_val).do(job_func)

            # Tag so we can clear WebGUI jobs on reload without touching config.ini jobs
            job.webgui_managed = True
            count += 1
            logger.debug(f"System: WebGUI schedule loaded: '{name}' ({freq}, {time_val})")

        except Exception as e:
            logger.warning(f"System: Failed to load WebGUI schedule '{name}': {e}")

    if count > 0:
        logger.info(f"System: Loaded {count} WebGUI schedules from {SCHEDULES_PATH}")
    return count


async def webgui_schedule_reload_loop(send_message_func, tell_joke_func, handle_wxc_func,
                                       default_channel, default_interface):
    """
    Watch schedules.json for changes and reload when modified.
    Checks file mtime every SCHEDULE_RELOAD_INTERVAL seconds (default 15).
    """
    global _last_schedules_mtime

    # Initial load after bot startup
    await asyncio.sleep(5)
    try:
        if os.path.exists(SCHEDULES_PATH):
            _last_schedules_mtime = os.path.getmtime(SCHEDULES_PATH)
            load_webgui_schedules(send_message_func, tell_joke_func, handle_wxc_func,
                                  default_channel, default_interface)
    except Exception as e:
        logger.debug(f"System: Initial WebGUI schedule load error: {e}")

    while True:
        try:
            await asyncio.sleep(SCHEDULE_RELOAD_INTERVAL)
            if os.path.exists(SCHEDULES_PATH):
                current_mtime = os.path.getmtime(SCHEDULES_PATH)
                if current_mtime != _last_schedules_mtime:
                    _last_schedules_mtime = current_mtime
                    load_webgui_schedules(send_message_func, tell_joke_func, handle_wxc_func,
                                          default_channel, default_interface)
                    logger.info("System: WebGUI schedules reloaded (file changed)")
        except asyncio.CancelledError:
            logger.debug("System: WebGUI schedule reload loop cancelled")
            break
        except Exception as e:
            logger.debug(f"System: WebGUI schedule reload error: {e}")


# === Packet Buffer Flush ===

PACKET_BUFFER_PATH = os.environ.get("PACKET_BUFFER_PATH", "/app/data/packets.json")
PACKET_FLUSH_INTERVAL = int(os.environ.get("PACKET_FLUSH_INTERVAL", "2"))  # seconds


async def packet_buffer_flush_loop(packet_buffer, buffer_lock):
    """
    Periodically flush the packet buffer to disk.
    Copies the buffer snapshot under lock (fast), writes to disk outside lock.

    Args:
        packet_buffer: The deque containing packet entries (from mesh_bot._packet_buffer)
        buffer_lock: The threading.Lock protecting the buffer (from mesh_bot._buffer_lock)
    """
    last_count = 0

    while True:
        try:
            await asyncio.sleep(PACKET_FLUSH_INTERVAL)

            # Snapshot under lock (microseconds — just a list copy)
            with buffer_lock:
                current_count = len(packet_buffer)
                if current_count == last_count:
                    continue  # Nothing changed, skip write
                snapshot = list(packet_buffer)

            last_count = current_count

            # Write to disk OUTSIDE the lock
            os.makedirs(os.path.dirname(PACKET_BUFFER_PATH), exist_ok=True)
            temp_path = PACKET_BUFFER_PATH + '.tmp'
            with open(temp_path, 'w') as f:
                json.dump(snapshot, f)
            os.replace(temp_path, PACKET_BUFFER_PATH)

        except asyncio.CancelledError:
            # Final flush on shutdown
            try:
                with buffer_lock:
                    snapshot = list(packet_buffer)
                with open(PACKET_BUFFER_PATH, 'w') as f:
                    json.dump(snapshot, f)
            except Exception:
                pass
            break
        except Exception as e:
            logger.debug(f"System: Packet flush error: {e}")
