import os
import ssl
import logging
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

logger = logging.getLogger("mqtt_client")
logger.setLevel(logging.DEBUG)

class MQTTClient:
    _instance = None

    @staticmethod
    def get_instance():
        if MQTTClient._instance is None:
            MQTTClient._instance = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            MQTTClient._instance.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            # MQTTClient._instance.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            logger.info("Created new MQTT client instance.")
        return MQTTClient._instance

mqtt_client = MQTTClient.get_instance()