import logging
import threading
from app.mqtt.handler import connect_mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

connect_mqtt()
threading.Event().wait()
