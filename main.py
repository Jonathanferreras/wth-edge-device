import logging
import threading
from app.mqtt.handler import connect_mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

connect_mqtt()
threading.Event().wait()



# topics = [
#   "wth/telemetry",
#   "wth/test"
# ]

# def on_connect(mqtt_client, user, flags, reason_code, properties):
#   if reason_code == 0:
#     print("Connected to MQTT Broker!")
#     mqtt_client.publish(topics[2], "Hello World!")
#   else:
#     print("Failed to connect to MQTT Broker!")

# def on_publish(mqtt_client, user, mid, reason_code, properties):
#   print(f"Publish Acknowledged, User: {user}, MID: {mid}, Reason Code: {reason_code}")

# def on_exit(mqtt_client, signal_received, frame):
#   print("Ctrl+C detected! Disconnecting from MQTT and exiting...")
#   mqtt_client.disconnect()
#   sys.exit(0)

# if __name__ == "__main__":
#     load_dotenv()

#     MQTT_BROKER = os.getenv("MQTT_BROKER", "")
#     MQTT_PORT = int(os.getenv("MQTT_PORT", 0))
#     MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
#     MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

#     mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
#     mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
#     # mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

#     mqtt_client.on_connect = on_connect
#     mqtt_client.on_publish = on_publish
#     # mqtt_client.on_message = on_message

#     mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
#     signal.signal(signal.SIGINT, partial(on_exit, mqtt_client))
#     signal.signal(signal.SIGTERM, partial(on_exit, mqtt_client))

#     try:
#       mqtt_client.loop_forever()
#     except KeyboardInterrupt:
#       on_exit(mqtt_client, None, None)
