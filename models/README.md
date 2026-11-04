# Models

`yolov8s.onnx`: Ultralytics YOLOv8s (COCO, 640x640 input), exported to ONNX. The boat
detector runs it through OpenCV's DNN module (no PyTorch needed) and keeps class 8, `boat`.
It is the same file the Pi 3 code used (`legacy/app/models/`).

To try a smaller or retrained model, export it to ONNX with the same output layout
(`[1, 4 + classes, N]`) and set `boat_detector.model_path` and `class_id`.
