import re
import subprocess
import psutil

class TelemetryManager:

    def get_cpu_usage(self)->str:
        return f"{psutil.cpu_percent(interval=1)}%"
    
    def get_cpu_temp(self)->str:
        try:
            output = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
            match = re.search(r'\d+\.\d+', output)

            return f"{float(match.group())}F" if match else None
        
        except subprocess.CalledProcessError:
            return None
    
    def get_ram_usage(self)->str:
        return f"{psutil.virtual_memory().percent}%"
    
    def get_input_voltage(self)->str:
        try:
            output = subprocess.check_output(["vcgencmd", "measure_volts core"]).decode()
            match = re.search(r'=(.*)', output)

            return match.group() if match else None

        except subprocess.CalledProcessError:
            return None

    def get_camera_connection_status(self)->bool:
        try:
            output = subprocess.run(["rpicam-hello", "--list-cameras"],
                capture_output=True,
                text=True,
                check=True
            )
            match = re.search(r"\d+\s*:", output.stdout)

            return bool(match)

        except subprocess.CalledProcessError:
            return False

    
    # Need to process video feed to determine this
    def get_camera_view_status(self):
        return None

    def generate_telemetry_report(self):
        report = {
            "cpu": self.get_cpu_usage(),
            "ram": self.get_ram_usage(),
            "temperature": self.get_cpu_temp(),
            "voltage": self.get_input_voltage(),
            "camera_connected": self.get_camera_connection_status(),
            "camera_view_status": self.get_camera_view_status(),
            "is_online": bool(True)
        }

        return report