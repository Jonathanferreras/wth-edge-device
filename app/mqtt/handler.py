import os
import sys
import json
import asyncio
import logging
import threading
import signal
import time
from dotenv import load_dotenv
from app.mqtt.client import mqtt_client
from app.mqtt.topics import DEVICE_TELEMETRY, TEST
from app.mqtt.events.telemetry_manager import TelemetryManager

load_dotenv()

MQTT_BROKER = os.getenv("MQTT_BROKER", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
telemetry_manager = TelemetryManager()
loop = asyncio.new_event_loop()
_stop_telemetry = threading.Event()
_telemetry_thread: threading.Thread | None = None

logger = logging.getLogger("mqtt_handler")
logger.setLevel(logging.DEBUG)

def start_event_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

def _publish_telemetry_once():
    payload = telemetry_manager.generate_telemetry_report()
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    mqtt_client.publish(DEVICE_TELEMETRY, payload=payload_json, qos=1, retain=True)

def _telemetry_publisher_loop():
    logger.info("Telemetry publisher loop started (every 60s).")

    while not _stop_telemetry.is_set():
        try:
            if mqtt_client.is_connected():
                _publish_telemetry_once()
            else:
                logger.debug("MQTT not connected; skipping telemetry publish.")
        except Exception as e:
            logger.exception(f"Telemetry publish failed: {e}")

        _stop_telemetry.wait(60)
    logger.info("Telemetry publisher loop stopped.")

def on_connect(client, userdata, flags, reason_code, properties):
    global _telemetry_thread

    if reason_code == 0:
        logger.info("Connected to MQTT Broker!")

        mqtt_client.publish(TEST, "Hello World!")

        if _telemetry_thread is None or not _telemetry_thread.is_alive():
            _stop_telemetry.clear()
            _telemetry_thread = threading.Thread(target=_telemetry_publisher_loop, daemon=True)
            _telemetry_thread.start()

    else:
        logger.error(f"Failed to connect to MQTT Broker! Reason: {reason_code}")

def on_message(client, userdata, message):
    topic = message.topic
    payload = message.payload.decode()

    try:
        logger.info(f"Payload: {payload}")
        # TODO: uncomment to work on processing data from edge device
        # future = asyncio.run_coroutine_threadsafe(process_mqtt_message(topic, payload), loop)
        # result = future.result()
    except Exception as e:
        logger.error(f"Error processing MQTT message: {e}")

def on_publish(client, userdata, mid, reason_code, properties):
    logger.info(f"Publish Acknowledged, MID: {mid}, Reason Code: {reason_code}")

def start_mqtt():
    logger.info("Starting MQTT loop...")
    mqtt_client.loop_forever()

def connect_mqtt():
    if not MQTT_BROKER or MQTT_PORT <= 0:
        logger.warning("MQTT not configured, Skipping MQTT connection.")
        return

    logger.info(f"Connecting to MQTT Broker at {MQTT_BROKER}:{MQTT_PORT}")

    last_will_payload = telemetry_manager.generate_telemetry_report()
    last_will_payload["is_online"] = False
    lw_payload_json = json.dumps(last_will_payload, separators=(",", ":"), ensure_ascii=False)

    mqtt_client.will_set(topic=DEVICE_TELEMETRY, payload=lw_payload_json, qos=1, retain=True)
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)

    mqtt_thread = threading.Thread(target=start_mqtt, daemon=True)
    mqtt_thread.start()

def handle_exit(signal_received, frame):
    logger.info("Shutting down... Disconnecting MQTT...")

    _stop_telemetry.set()

    try:
        payload = telemetry_manager.generate_telemetry_report()
        payload["is_online"] = False
        mqtt_client.publish(
            DEVICE_TELEMETRY,
            payload=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            qos=1,
            retain=True,
        )
    except Exception:
        pass

    mqtt_client.disconnect()
    sys.exit(0)

threading.Thread(target=start_event_loop, daemon=True).start()

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.on_publish = on_publish

signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)