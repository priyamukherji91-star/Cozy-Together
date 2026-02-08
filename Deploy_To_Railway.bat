@echo off
cd /d "C:\Users\Cookiechan\OneDrive\Desktop\Cozy Together"
railway up --service refreshing-patience
if errorlevel 1 (
    echo.
    echo ❌ Deployment failed. Check the error above.
    pause
) else (
    echo ✅ Deployment successful.
    pause
)
