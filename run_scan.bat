@echo off
cd /d "c:\Users\rcodo\Downloads\Prediction Markets"
C:\Users\rcodo\AppData\Local\Programs\Python\Launcher\py.exe -3.12 -m lambda_harvester.main --save-signals --quiet >> "c:\Users\rcodo\Downloads\Prediction Markets\scan_log.txt" 2>&1
