#!/usr/bin/env bash

EMAIL="tdiprima"
LOG_FILE="/var/log/ai_scanner_cron.log"

cd "/opt/infosec-ai-scanner"
source .venv/bin/activate

cd src
/opt/infosec-ai-scanner/.venv/bin/python drupal_ai_scanner.py
EXIT_CODE=$?

if [[ "${EXIT_CODE}" -ne 0 ]]; then
    /usr/bin/mail -s "Python job FAILED on $(hostname)" "${EMAIL}" < "${LOG_FILE}"
fi

deactivate
