import os
import sys
import asyncio
import logging
import threading
import signal
from dotenv import load_dotenv
from app.mqtt.client import mqtt_client
from app.mqtt.topics import DEVICE_TELEMETRY, TEST
from app.mqtt.events.telemetry_manager import TelemetryManager

load_dotenv()

MQTT_BROKER = os.getenv("MQTT_BROKER", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
telemetry_manager = TelemetryManager()
loop = asyncio.new_event_loop()

logger = logging.getLogger("mqtt_handler")
logger.setLevel(logging.DEBUG)

def start_event_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info("Connected to MQTT Broker!")
        value = telemetry_manager.get_cpu_temp()
        mqtt_client.publish(TEST, "Hello World!")
        mqtt_client.publish(DEVICE_TELEMETRY, value)
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
        logger.warning(
            "MQTT not configured, Skipping MQTT connection."
        )
        return
    logger.info(f"Connecting to MQTT Broker at {MQTT_BROKER}:{MQTT_PORT}")
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)

    mqtt_thread = threading.Thread(target=start_mqtt, daemon=True)
    mqtt_thread.start()

def handle_exit(signal_received, frame):
    logger.info("Shutting down... Disconnecting MQTT...")
    mqtt_client.disconnect()
    sys.exit(0)

threading.Thread(target=start_event_loop, daemon=True).start()

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.on_publish = on_publish

signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)