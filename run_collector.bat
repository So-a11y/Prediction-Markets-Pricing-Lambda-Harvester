@echo off
cd /d "c:\Users\rcodo\Downloads\Prediction Markets"
C:\Users\rcodo\AppData\Local\Programs\Python\Launcher\py.exe -3.12 -m lambda_harvester.price_collector --quiet >> "c:\Users\rcodo\Downloads\Prediction Markets\collector_log.txt" 2>&1
