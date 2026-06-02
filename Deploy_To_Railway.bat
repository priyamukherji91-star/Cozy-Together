@echo off
cd /d "C:\Projects\Cozy Together"
railway up --service worker
if errorlevel 1 (
    echo.
    echo ❌ Deployment failed. Check the error above.
    pause
) else (
    echo ✅ Deployment successful.
    pause
)
