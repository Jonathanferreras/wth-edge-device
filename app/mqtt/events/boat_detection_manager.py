import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv

from app.mqtt.topics import BOAT_DETECTION, BOAT_DETECTION_IMAGE

load_dotenv()


class BoatDetectionManager:
    def __init__(self):
        self.stream_url = os.getenv("CAMERA_STREAM_URL", "rtsp://127.0.0.1:8554/cam")
        default_model_path = Path(__file__).resolve().parents[2] / "models" / "yolov8s.onnx"
        self.model_path = os.getenv("BOAT_DETECTION_MODEL", str(default_model_path))
        self.boat_class_id = int(os.getenv("BOAT_CLASS_ID", "8"))
        self.confidence_threshold = float(os.getenv("BOAT_DETECTION_CONFIDENCE", "0.2"))
        self.nms_threshold = float(os.getenv("BOAT_DETECTION_NMS_THRESHOLD", "0.45"))
        self.model_input_size = int(os.getenv("BOAT_DETECTION_INPUT_SIZE", "640"))
        self.grace_period_seconds = int(os.getenv("BOAT_GRACE_PERIOD_SECONDS", "60"))
        self.reconnect_delay_seconds = float(os.getenv("CAMERA_RECONNECT_DELAY_SECONDS", "2"))
        self.roi_width = int(os.getenv("BOAT_ROI_WIDTH", "700"))
        self.roi_height = int(os.getenv("BOAT_ROI_HEIGHT", "100"))
        self.roi_x_offset = int(os.getenv("BOAT_ROI_X_OFFSET", "300"))
        self.roi_y_offset = int(os.getenv("BOAT_ROI_Y_OFFSET", "200"))
        self.left_is = os.getenv("BOAT_LEFT_IS", "WEST")
        self.right_is = os.getenv("BOAT_RIGHT_IS", "EAST")
        self.direction_lock_threshold_px = int(os.getenv("BOAT_DIRECTION_LOCK_THRESHOLD_PX", "15"))
        self.capture_mode = cv2.CAP_FFMPEG
        self.model = self.load_model()
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

    def letterbox(self, image, color=(114, 114, 114)):
        height, width = image.shape[:2]
        scale = min(self.model_input_size / height, self.model_input_size / width)
        resized_width = int(round(width * scale))
        resized_height = int(round(height * scale))
        resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)

        pad_w = self.model_input_size - resized_width
        pad_h = self.model_input_size - resized_height
        pad_left = pad_w // 2
        pad_right = pad_w - pad_left
        pad_top = pad_h // 2
        pad_bottom = pad_h - pad_top

        bordered = cv2.copyMakeBorder(
            resized,
            pad_top,
            pad_bottom,
            pad_left,
            pad_right,
            cv2.BORDER_CONSTANT,
            value=color,
        )

        return bordered, scale, pad_left, pad_top

    def load_model(self):
        return cv2.dnn.readNetFromONNX(self.model_path)

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
        input_image, scale, pad_left, pad_top = self.letterbox(roi)
        blob = cv2.dnn.blobFromImage(
            input_image,
            scalefactor=1 / 255.0,
            size=(self.model_input_size, self.model_input_size),
            swapRB=True,
            crop=False,
        )
        self.model.setInput(blob)
        predictions = self.model.forward()[0].T

        roi_height, roi_width = roi.shape[:2]
        boxes = []
        confidences = []

        for prediction in predictions:
            class_scores = prediction[4:]
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])

            if class_id != self.boat_class_id or confidence < self.confidence_threshold:
                continue

            center_x, center_y, width, height = prediction[:4]
            x1 = (center_x - width / 2 - pad_left) / scale
            y1 = (center_y - height / 2 - pad_top) / scale
            x2 = (center_x + width / 2 - pad_left) / scale
            y2 = (center_y + height / 2 - pad_top) / scale

            x1 = max(0, min(int(round(x1)), roi_width - 1))
            y1 = max(0, min(int(round(y1)), roi_height - 1))
            x2 = max(0, min(int(round(x2)), roi_width - 1))
            y2 = max(0, min(int(round(y2)), roi_height - 1))

            if x2 <= x1 or y2 <= y1:
                continue

            boxes.append([x1, y1, x2 - x1, y2 - y1])
            confidences.append(confidence)

        if not boxes:
            return None, 0.0

        indices = cv2.dnn.NMSBoxes(boxes, confidences, self.confidence_threshold, self.nms_threshold)
        if len(indices) == 0:
            return None, 0.0

        flattened_indices = np.array(indices).flatten()
        best_index = max(flattened_indices, key=lambda idx: confidences[idx])
        x1, y1, width, height = boxes[best_index]

        return {
            "box": (x1, y1, x1 + width, y1 + height),
            "confidence": confidences[best_index],
        }, confidences[best_index]

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

        bx1, by1, bx2, by2 = best_boat["box"]
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
