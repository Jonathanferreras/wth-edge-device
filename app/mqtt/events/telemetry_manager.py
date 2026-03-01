import re
import subprocess

class TelemetryManager:

    def get_cpu_usage(self):
        return self
    
    def get_cpu_temp(self):
        output = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
        match = re.search(r'\d+\.\d+', output)

        return float(match.group()) if match else None
    
    def get_ram_usage(self):
        return self
    
    def get_input_voltage(self):
        return self
    
    def get_camera_connection_status(self):
        return self
    
    # Need to process video feed to determine this
    def get_camera_view_status(self):
        return self