import os
import json
import asyncio
import logging
from typing import Set, Dict, Any, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

logger = logging.getLogger("server")


class MarkerRequest(BaseModel):
    label: str
    note: str = ""


class SessionStartRequest(BaseModel):
    name: str = "drive"


def create_app(conn, recorder, scheduler, config: dict, is_mock: bool = False) -> FastAPI:
    connected_clients: Set[WebSocket] = set()
    server_loop: Optional[asyncio.AbstractEventLoop] = None

    async def broadcast_ws(payload: dict):
        if not connected_clients:
            return
        dead = []
        msg_json = json.dumps(payload)
        for client in list(connected_clients):
            try:
                await client.send_text(msg_json)
            except Exception:
                dead.append(client)
        for d in dead:
            connected_clients.discard(d)

    def safe_broadcast(payload: dict):
        nonlocal server_loop
        if server_loop and server_loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(broadcast_ws(payload), server_loop)
            except Exception as e:
                logger.debug(f"Broadcast error: {e}")

    # Wire telemetry callback to WebSocket broadcast
    def on_telemetry(cycle_data: dict):
        stats = recorder.get_stats()
        payload = {
            "type": "telemetry",
            "data": cycle_data,
            "recorder": stats,
            "connection": {
                "state": conn.state,
                "reconnect_count": getattr(conn, "reconnect_count", 0),
                "error_count": getattr(conn, "error_count", 0),
            },
            "benchmark": scheduler.benchmark_results,
        }
        safe_broadcast(payload)

    scheduler.on_telemetry = on_telemetry

    # Wire state change callback
    def on_state_change(old_state: str, new_state: str):
        payload = {
            "type": "state_change",
            "old_state": old_state,
            "new_state": new_state,
            "reconnect_count": getattr(conn, "reconnect_count", 0),
            "error_count": getattr(conn, "error_count", 0),
        }
        safe_broadcast(payload)

    conn.on_state_change = on_state_change

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal server_loop
        server_loop = asyncio.get_running_loop()
        logger.info("Server lifespan initialized with event loop.")
        yield

    app = FastAPI(title="FleetPulse OBD-II Recorder", lifespan=lifespan)

    # --- REST API ---

    @app.get("/api/status")
    def get_status():
        return {
            "connection": {
                "state": conn.state,
                "reconnect_count": getattr(conn, "reconnect_count", 0),
                "error_count": getattr(conn, "error_count", 0),
                "address": getattr(conn, "address", "MOCK"),
                "channel": getattr(conn, "channel", 2),
            },
            "recorder": recorder.get_stats(),
            "benchmark": scheduler.benchmark_results,
            "config": config,
            "is_mock": is_mock,
        }

    @app.post("/api/connect")
    def api_connect():
        if conn.state in ("Connected", "Connecting", "Waking adapter"):
            return {"status": "already_connected_or_connecting", "state": conn.state}
        
        import threading
        threading.Thread(target=conn.connect, daemon=True).start()
        return {"status": "initiating_connection", "state": conn.state}

    @app.post("/api/disconnect")
    def api_disconnect():
        conn.disconnect()
        return {"status": "disconnected", "state": conn.state}

    @app.post("/api/recording/start")
    def api_start_recording(req: SessionStartRequest):
        session_dir = recorder.start_session(
            name=req.name,
            vehicle_info=config.get("vehicle"),
            adapter_info={"address": getattr(conn, "address", "MOCK"), "channel": getattr(conn, "channel", 2)},
            polling_config=config.get("polling"),
            benchmark_results=scheduler.benchmark_results,
        )
        return {"status": "started", "session_dir": session_dir, "stats": recorder.get_stats()}

    @app.post("/api/recording/stop")
    def api_stop_recording():
        recorder.stop_session()
        if scheduler.keep_armed and conn.state == "Connected":
            recorder.start_session(
                name="armed_drive",
                vehicle_info=config.get("vehicle"),
                adapter_info={"address": getattr(conn, "address", "MOCK"), "channel": getattr(conn, "channel", 2)},
                polling_config=config.get("polling"),
                benchmark_results=scheduler.benchmark_results,
            )
        return {"status": "stopped", "stats": recorder.get_stats()}

    @app.post("/api/markers")
    def api_add_marker(req: MarkerRequest):
        speed = scheduler.cache.get("0D", {}).get("val")
        rpm = scheduler.cache.get("0C", {}).get("val")
        marker = recorder.add_marker(label=req.label, note=req.note, speed=speed, rpm=rpm)
        safe_broadcast({"type": "marker_added", "marker": marker})
        return {"status": "ok", "marker": marker}

    @app.post("/api/markers/undo")
    def api_undo_marker():
        removed = recorder.undo_last_marker()
        if not removed:
            raise HTTPException(status_code=400, detail="No markers to undo")
        safe_broadcast({"type": "marker_undone", "marker": removed})
        return {"status": "undone", "marker": removed}

    @app.post("/api/mock/disconnect")
    def api_mock_disconnect():
        """Simulate a temporary 3.5-second disconnect (Mock mode only)."""
        if hasattr(conn, "trigger_temporary_disconnect"):
            conn.trigger_temporary_disconnect(duration_s=3.5)
            return {"status": "triggered_disconnect"}
        raise HTTPException(status_code=400, detail="Not in mock mode")

    # --- WebSocket Endpoint ---

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        connected_clients.add(websocket)
        logger.info(f"WebSocket client connected ({len(connected_clients)} active)")
        try:
            init_payload = {
                "type": "init",
                "connection": {
                    "state": conn.state,
                    "reconnect_count": getattr(conn, "reconnect_count", 0),
                    "error_count": getattr(conn, "error_count", 0),
                },
                "recorder": recorder.get_stats(),
                "benchmark": scheduler.benchmark_results,
                "config": config,
                "is_mock": is_mock,
            }
            await websocket.send_text(json.dumps(init_payload))

            while True:
                data = await websocket.receive_text()
                try:
                    cmd = json.loads(data)
                    action = cmd.get("action")
                    if action == "add_marker":
                        speed = scheduler.cache.get("0D", {}).get("val")
                        rpm = scheduler.cache.get("0C", {}).get("val")
                        recorder.add_marker(cmd.get("label", "OTHER"), cmd.get("note", ""), speed, rpm)
                    elif action == "undo_marker":
                        recorder.undo_last_marker()
                    elif action == "start_recording":
                        recorder.start_session(
                            name=cmd.get("name", "drive"),
                            vehicle_info=config.get("vehicle"),
                            adapter_info={"address": getattr(conn, "address", "MOCK"), "channel": getattr(conn, "channel", 2)},
                            polling_config=config.get("polling"),
                            benchmark_results=scheduler.benchmark_results,
                        )
                    elif action == "stop_recording":
                        recorder.stop_session()
                except Exception as e:
                    logger.warning(f"Error handling ws message: {e}")
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        finally:
            connected_clients.discard(websocket)

    # Static assets mounting
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.exists(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/")
        def serve_index():
            index_path = os.path.join(static_dir, "index.html")
            return FileResponse(index_path)

    return app
