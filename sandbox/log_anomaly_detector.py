import re

def detect_errors(log_file):
    error_patterns = [
        r"Exception",
        r"Error",
        r"Failed",
        r"Traceback"
    ]
    
    with open(log_file, "r") as file:
        logs = file.readlines()
    
    for line in logs:
        for pattern in error_patterns:
            if re.search(pattern, line):
                print(f"[ERROR FOUND] {line.strip()}")

detect_errors("/var/log/drupal_backup.log")
print()
detect_errors("/var/log/ai_scanner_cron.log")
