import json
import logging
import os
import time
from datetime import datetime, timezone
import cv2
from dotenv import load_dotenv
from ultralytics import YOLO
from app.mqtt.topics import BOAT_DETECTION, BOAT_DETECTION_IMAGE

load_dotenv()


class BoatDetectionManager:
    def __init__(self):
        self.stream_url = os.getenv("CAMERA_STREAM_URL", "rtsp://127.0.0.1:8554/cam")
        self.model_path = "yolov8s.pt"
        self.boat_class_id = 8
        self.confidence_threshold = 0.2
        self.grace_period_seconds = 60
        self.reconnect_delay_seconds = 2
        self.roi_width = 700
        self.roi_height = 100
        self.roi_x_offset = 300
        self.roi_y_offset = 200
        self.left_is = "WEST"
        self.right_is = "EAST"
        self.direction_lock_threshold_px = 15
        self.capture_mode = cv2.CAP_FFMPEG
        self.model = YOLO(self.model_path)
        self.logger = logging.getLogger("boat_detection_manager")
        self.logger.setLevel(logging.INFO)
        self.reset_event()

    def reset_event(self):
        self.boat_present = False
        self.last_seen_time = None
        self.event_start_x = None
        self.direction = "UNKNOWN"
        self.locked_direction = None
        self.event_published = False
        self.first_detected_at = None
        self.best_confidence = 0.0
        self.screenshot_frame = None
        self.screenshot_box = None

    def build_roi(self, width, height):
        x1 = ((width - self.roi_width) // 2) + self.roi_x_offset
        y1 = ((height - self.roi_height) // 2) + self.roi_y_offset
        x2 = x1 + self.roi_width
        y2 = y1 + self.roi_height
        return max(0, x1), max(0, y1), min(width, x2), min(height, y2)

    def get_direction(self, center_x):
        if self.event_start_x is None:
            self.event_start_x = center_x

        delta_x = center_x - self.event_start_x

        if abs(delta_x) < self.direction_lock_threshold_px:
            return "CALCULATING"

        if delta_x > 0:
            return f"{self.left_is} to {self.right_is}"

        return f"{self.right_is} to {self.left_is}"

    def get_best_boat(self, roi):
        results = self.model.predict(
            source=roi,
            conf=self.confidence_threshold,
            classes=[self.boat_class_id],
            verbose=False,
        )

        best_boat = None
        best_confidence = 0.0

        for result in results:
            for box in result.boxes:
                confidence = float(box.conf[0])

                if confidence > best_confidence:
                    best_confidence = confidence
                    best_boat = box

        return best_boat, best_confidence

    def publish_detection_event(self, mqtt_client, center_x):
        image_id, image_topic = self.publish_detection_image(mqtt_client)
        payload = {
            "direction": self.direction,
            "detected_at": datetime.fromtimestamp(
                self.first_detected_at or time.time(),
                tz=timezone.utc,
            ).isoformat(),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "confidence": round(self.best_confidence, 4),
            "source": self.stream_url,
            "center_x": center_x,
            "image_topic": image_topic,
            "image_id": image_id,
        }

        mqtt_client.publish(
            BOAT_DETECTION,
            payload=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            qos=1,
            retain=False,
        )
        
        self.event_published = True
        self.logger.info("Published boat detection event.")

    def publish_detection_image(self, mqtt_client):
        if self.screenshot_frame is None or self.screenshot_box is None:
            return None, None

        frame = self.screenshot_frame.copy()
        x1, y1, x2, y2 = self.screenshot_box
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

        ok, image_buffer = cv2.imencode(".jpg", frame)
        if not ok:
            self.logger.warning("Failed to encode boat detection image.")
            return None, None

        image_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        image_topic = f"{BOAT_DETECTION_IMAGE}/{image_id}"
        mqtt_client.publish(
            image_topic,
            payload=image_buffer.tobytes(),
            qos=1,
            retain=False,
        )
        self.logger.info("Published boat detection image.")
        return image_id, image_topic

    def process_frame(self, frame, mqtt_client):
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = self.build_roi(width, height)
        roi = frame[y1:y2, x1:x2]
        best_boat, confidence = self.get_best_boat(roi)
        current_time = time.time()

        if best_boat is None:
            if self.boat_present and self.last_seen_time is not None:
                if current_time - self.last_seen_time >= self.grace_period_seconds:
                    self.logger.info("Boat fully gone. Final direction: %s", self.direction)
                    self.reset_event()
            return

        bx1, by1, bx2, by2 = map(int, best_boat.xyxy[0])
        center_x = (x1 + bx1 + x1 + bx2) // 2

        self.screenshot_frame = frame
        self.screenshot_box = (x1 + bx1, y1 + by1, x1 + bx2, y1 + by2)
        self.last_seen_time = current_time
        self.best_confidence = max(self.best_confidence, confidence)

        if not self.boat_present:
            self.boat_present = True
            self.first_detected_at = current_time
            self.event_start_x = center_x
            self.direction = "CALCULATING"
            self.locked_direction = None
            self.event_published = False
            self.best_confidence = confidence
            self.logger.info("Boat first detected.")

        if self.locked_direction is None:
            direction = self.get_direction(center_x)
            self.direction = direction

            if direction != "CALCULATING":
                self.locked_direction = direction
                self.logger.info("Direction locked: %s", direction)

        if self.locked_direction and not self.event_published and mqtt_client.is_connected():
            self.publish_detection_event(mqtt_client, center_x)

    def start_detection_loop(self, mqtt_client, stop_event):
        feed = None

        while not stop_event.is_set():
            try:
                if feed is None or not feed.isOpened():
                    self.logger.info("Opening camera feed: %s", self.stream_url)
                    feed = cv2.VideoCapture(self.stream_url, self.capture_mode)

                    if not feed.isOpened():
                        self.logger.warning("Unable to open camera feed.")
                        feed.release()
                        feed = None
                        stop_event.wait(self.reconnect_delay_seconds)
                        continue

                ok, frame = feed.read()
                if not ok:
                    self.logger.warning("Failed to read frame from camera feed.")
                    feed.release()
                    feed = None
                    stop_event.wait(self.reconnect_delay_seconds)
                    continue

                self.process_frame(frame, mqtt_client)
            except Exception as e:
                self.logger.exception(f"Boat detection failed: {e}")
                if feed is not None:
                    feed.release()
                    feed = None
                stop_event.wait(self.reconnect_delay_seconds)

        if feed is not None:
            feed.release()
